# -*- coding: utf-8 -*-
"""
app.py
------
localhost üzerinde çalışan Flask web paneli.

Çalıştırma:
    python app.py

Ardından tarayıcıda:
    http://127.0.0.1:5050

Panel, taramayı arka planda bir thread'de çalıştırır; ilerleme durumunu
ve sonuçları JSON API üzerinden döndürür, grafikleri /charts/<dosya>
üzerinden servis eder.
"""

import os
import re
import threading
import traceback
from datetime import datetime

from flask import Flask, jsonify, render_template, request, send_from_directory, send_file

from scanner import run_scan, scan_single_symbol, top_opportunities, TIMEFRAMES
from excel_export import build_excel, export_filename, build_journal_excel, journal_export_filename
import journal
from db import get_db
from backtest import run_backtest
from utils import get_latest_prices
from market_direction import compute_market_snapshot
from ayarlar import WEB_HOST, WEB_PORT, DEBUG_MODE, DEFAULT_TOP_N

app = Flask(__name__)

# Klasör adından otomatik versiyon etiketi (ör. "crypto_pattern_scannerv2" -> "v2").
# Böylece aynı anda birden fazla klasör/versiyon çalıştırıldığında panelde ve
# indirilen dosya adlarında hangisinin hangisi olduğu karışmaz.
_FOLDER_NAME = os.path.basename(os.path.dirname(os.path.abspath(__file__)))
APP_VERSION = _FOLDER_NAME if _FOLDER_NAME else "crypto_pattern_scanner"

CHARTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# Uygulama durumu (tek kullanıcılı basit panel için thread-safe global state)
# --------------------------------------------------------------------------
state_lock = threading.Lock()
state = {
    "running": False,
    "stage": "idle",           # idle | symbols | scanning | done | error
    "current": 0,
    "total": 0,
    "message": "Tarama henüz başlatılmadı.",
    "results": [],
    "top_long": [],
    "top_short": [],
    "started_at": None,
    "finished_at": None,
    "error": None,
}


def _progress_callback(info: dict):
    with state_lock:
        state["stage"] = info.get("stage", state["stage"])
        state["current"] = info.get("current", state["current"])
        state["total"] = info.get("total", state["total"])
        state["message"] = info.get("message", state["message"])


# --------------------------------------------------------------------------
# Backtest state (ayrı bir işlem, taramayla eşzamanlı çalışabilir)
# --------------------------------------------------------------------------
backtest_lock = threading.Lock()
backtest_state = {
    "running": False,
    "current": 0,
    "total": 0,
    "message": "Backtest henüz çalıştırılmadı.",
    "result": None,
    "error": None,
}


def _backtest_progress_callback(info: dict):
    with backtest_lock:
        backtest_state["current"] = info.get("current", backtest_state["current"])
        backtest_state["total"] = info.get("total", backtest_state["total"])
        backtest_state["message"] = info.get("message", backtest_state["message"])


def _run_backtest_background(symbol: str, timeframe: str):
    try:
        result = run_backtest(symbol, timeframe, progress_cb=_backtest_progress_callback)
        with backtest_lock:
            backtest_state["result"] = result
            backtest_state["message"] = f"Backtest tamamlandı. {len(result['trades'])} sinyal simüle edildi."
            backtest_state["running"] = False
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with backtest_lock:
            backtest_state["error"] = str(exc)
            backtest_state["message"] = f"Hata oluştu: {exc}"
            backtest_state["running"] = False


# --------------------------------------------------------------------------
# Coin Ara state (tek bir coin için anlık, ayrıntılı tarama)
# --------------------------------------------------------------------------
coin_search_lock = threading.Lock()
coin_search_state = {
    "running": False,
    "current": 0,
    "total": 0,
    "message": "Henüz bir coin aranmadı.",
    "result": None,
    "error": None,
}


def _coin_search_progress_callback(info: dict):
    with coin_search_lock:
        coin_search_state["current"] = info.get("current", coin_search_state["current"])
        coin_search_state["total"] = info.get("total", coin_search_state["total"])
        coin_search_state["message"] = info.get("message", coin_search_state["message"])


def _run_coin_search_background(symbol: str):
    try:
        result = scan_single_symbol(symbol, progress_cb=_coin_search_progress_callback)
        with coin_search_lock:
            coin_search_state["result"] = result
            coin_search_state["message"] = (
                f"{result['symbol']}: {len(result['qualified'])} aktif sinyal, "
                f"{len(result['below_threshold'])} eşiği geçemeyen formasyon."
            )
            coin_search_state["running"] = False
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with coin_search_lock:
            coin_search_state["error"] = str(exc)
            coin_search_state["message"] = f"Hata oluştu: {exc}"
            coin_search_state["running"] = False


def _run_scan_background(top_n: int, timeframes: list):
    try:
        results = run_scan(top_n=top_n, timeframes=timeframes, progress_cb=_progress_callback)
        with state_lock:
            state["results"] = results
            state["top_long"] = top_opportunities(results, "LONG", 10)
            state["top_short"] = top_opportunities(results, "SHORT", 10)
            state["stage"] = "done"
            state["message"] = f"Tarama tamamlandı. {len(results)} formasyon bulundu."
            state["running"] = False
            state["finished_at"] = datetime.now().isoformat(timespec="seconds")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with state_lock:
            state["stage"] = "error"
            state["error"] = str(exc)
            state["message"] = f"Hata oluştu: {exc}"
            state["running"] = False
            state["finished_at"] = datetime.now().isoformat(timespec="seconds")


@app.route("/")
def index():
    return render_template("index.html", timeframes=TIMEFRAMES, app_version=APP_VERSION)


@app.route("/api/scan", methods=["POST"])
def api_scan():
    payload = request.get_json(silent=True) or {}
    top_n = int(payload.get("top_n", DEFAULT_TOP_N))
    timeframes = payload.get("timeframes") or TIMEFRAMES

    with state_lock:
        if state["running"]:
            return jsonify({"ok": False, "message": "Zaten bir tarama çalışıyor."}), 409
        state.update({
            "running": True,
            "stage": "starting",
            "current": 0,
            "total": 0,
            "message": "Tarama başlatılıyor...",
            "results": [],
            "top_long": [],
            "top_short": [],
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "error": None,
        })

    thread = threading.Thread(target=_run_scan_background, args=(top_n, timeframes), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Tarama başlatıldı."})


@app.route("/api/status")
def api_status():
    with state_lock:
        return jsonify({
            "running": state["running"],
            "stage": state["stage"],
            "current": state["current"],
            "total": state["total"],
            "message": state["message"],
            "started_at": state["started_at"],
            "finished_at": state["finished_at"],
            "error": state["error"],
            "result_count": len(state["results"]),
        })


@app.route("/api/results")
def api_results():
    with state_lock:
        return jsonify({
            "results": state["results"],
            "top_long": state["top_long"],
            "top_short": state["top_short"],
        })


@app.route("/charts/<path:filename>")
def serve_chart(filename):
    return send_from_directory(CHARTS_DIR, filename)


@app.route("/api/export")
def api_export():
    with state_lock:
        all_results = list(state["results"])
        top_long = list(state["top_long"])
        top_short = list(state["top_short"])

    if not all_results:
        return jsonify({"ok": False, "message": "İndirilecek sonuç yok. Önce bir tarama çalıştırın."}), 400

    buffer = build_excel(all_results, top_long, top_short)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=export_filename(APP_VERSION),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# --------------------------------------------------------------------------
# Backtest API
# --------------------------------------------------------------------------
def _validate_symbol(raw_symbol: str):
    """
    Kullanıcının girdiği metni bir Binance sembolüne dönüştürüp doğrular.
    Geçerliyse (True, "BTCUSDT") döndürür; geçersizse (False, hata_mesajı).
    """
    symbol = (raw_symbol or "").strip().upper()
    if not symbol:
        return False, "Coin sembolü boş olamaz (ör. BTCUSDT)."
    symbol_candidate = symbol if symbol.endswith("USDT") else symbol + "USDT"
    if not re.fullmatch(r"[A-Z0-9]+", symbol_candidate):
        return False, (
            f"'{raw_symbol}' geçerli bir Binance coin sembolü değil. "
            "Lütfen bir formasyon adı değil, coin sembolü girin (ör. BTCUSDT, ETHUSDT, SOLUSDT)."
        )
    return True, symbol_candidate


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    payload = request.get_json(silent=True) or {}
    ok, symbol_or_err = _validate_symbol(payload.get("symbol"))
    if not ok:
        return jsonify({"ok": False, "message": symbol_or_err}), 400
    symbol = symbol_or_err
    timeframe = payload.get("timeframe", "4h")
    if timeframe not in TIMEFRAMES:
        return jsonify({"ok": False, "message": f"Geçersiz zaman dilimi: {timeframe}"}), 400

    with backtest_lock:
        if backtest_state["running"]:
            return jsonify({"ok": False, "message": "Zaten bir backtest çalışıyor."}), 409
        backtest_state.update({
            "running": True, "current": 0, "total": 0,
            "message": f"{symbol} ({timeframe}) için backtest başlatılıyor...",
            "result": None, "error": None,
        })

    thread = threading.Thread(target=_run_backtest_background, args=(symbol, timeframe), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Backtest başlatıldı."})


@app.route("/api/backtest/status")
def api_backtest_status():
    with backtest_lock:
        return jsonify({
            "running": backtest_state["running"],
            "current": backtest_state["current"],
            "total": backtest_state["total"],
            "message": backtest_state["message"],
            "error": backtest_state["error"],
            "has_result": backtest_state["result"] is not None,
        })


@app.route("/api/backtest/results")
def api_backtest_results():
    with backtest_lock:
        return jsonify(backtest_state["result"] or {})


# --------------------------------------------------------------------------
# Coin Ara API (tek coin için anlık, ayrıntılı tarama)
# --------------------------------------------------------------------------
@app.route("/api/coin-search", methods=["POST"])
def api_coin_search():
    payload = request.get_json(silent=True) or {}
    ok, symbol_or_err = _validate_symbol(payload.get("symbol"))
    if not ok:
        return jsonify({"ok": False, "message": symbol_or_err}), 400
    symbol = symbol_or_err

    with coin_search_lock:
        if coin_search_state["running"]:
            return jsonify({"ok": False, "message": "Zaten bir coin araması çalışıyor."}), 409
        coin_search_state.update({
            "running": True, "current": 0, "total": 0,
            "message": f"{symbol} aranıyor...",
            "result": None, "error": None,
        })

    thread = threading.Thread(target=_run_coin_search_background, args=(symbol,), daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Arama başlatıldı."})


@app.route("/api/coin-search/status")
def api_coin_search_status():
    with coin_search_lock:
        return jsonify({
            "running": coin_search_state["running"],
            "current": coin_search_state["current"],
            "total": coin_search_state["total"],
            "message": coin_search_state["message"],
            "error": coin_search_state["error"],
            "has_result": coin_search_state["result"] is not None,
        })


@app.route("/api/coin-search/results")
def api_coin_search_results():
    with coin_search_lock:
        return jsonify(coin_search_state["result"] or {})


# --------------------------------------------------------------------------
# BTC Piyasa Yönü API
# --------------------------------------------------------------------------
@app.route("/api/market-direction")
def api_market_direction():
    try:
        snapshot = compute_market_snapshot()
        return jsonify({"ok": True, **snapshot})
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({
            "ok": False,
            "message": f"Piyasa verisi alınamadı: {exc}",
        }), 502


# --------------------------------------------------------------------------
# Sinyal Günlüğü API
# --------------------------------------------------------------------------
@app.errorhandler(journal.DatabaseError)
def handle_db_error(exc):
    return jsonify({"ok": False, "message": f"Sinyal veritabanına erişilemedi: {exc}"}), 502


@app.route("/api/journal")
def api_journal():
    # İsteğe bağlı filtreler: ?symbol=BTCUSDT&status=HEDEF&timeframe=4h
    #                         &since=2026-09-01&until=2026-09-30&limit=1000
    args = request.args
    try:
        limit = max(1, min(int(args.get("limit", 500)), 100000))
    except ValueError:
        limit = 500
    signals = journal.get_signals(
        limit=limit,
        symbol=args.get("symbol") or None,
        status=args.get("status") or None,
        timeframe=args.get("timeframe") or None,
        since=args.get("since") or None,
        until=args.get("until") or None,
    )
    summary = journal.get_summary()
    return jsonify({"signals": signals, "summary": summary})


@app.route("/api/journal/refresh", methods=["POST"])
def api_journal_refresh():
    open_signals = journal.get_open_signals()
    open_symbols = sorted({s["symbol"] for s in open_signals})
    if not open_symbols:
        return jsonify({"ok": True, "message": "Güncellenecek açık sinyal yok.", "stats": {}})
    try:
        prices = get_latest_prices(open_symbols)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"Fiyatlar çekilemedi: {exc}"}), 502

    stats = journal.refresh_open_signals(prices)
    return jsonify({"ok": True, "message": "Sinyal günlüğü güncellendi.", "stats": stats})


@app.route("/api/journal/clear", methods=["POST"])
def api_journal_clear():
    journal.clear_journal()
    return jsonify({"ok": True, "message": "Sinyal günlüğü temizlendi."})


@app.route("/api/journal/export")
def api_journal_export():
    signals = journal.get_all_signals(limit=100000)
    summary = journal.get_summary()
    if not signals:
        return jsonify({"ok": False, "message": "Dışa aktarılacak sinyal yok."}), 400

    buffer = build_journal_excel(signals, summary, APP_VERSION)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=journal_export_filename(APP_VERSION),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


if __name__ == "__main__":
    # Öncelik ortam değişkenindedir; yoksa ayarlar.py değerleri kullanılır.
    # Böylece kalıcı değişiklik için ayarlar.py düzenlenebilir, geçici olarak
    # farklı portta çalıştırmak için SCANNER_PORT verilebilir.
    host = os.environ.get("SCANNER_HOST", WEB_HOST)
    port = int(os.environ.get("SCANNER_PORT", WEB_PORT))

    print("=" * 70)
    print(f"Kripto Formasyon Tarayıcı - Web Paneli  [{APP_VERSION}]")
    browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Tarayıcınızda şu adresi açın: http://{browser_host}:{port}")
    print(f"Sinyal günlüğü: {get_db().describe()}")
    print("=" * 70)
    try:
        app.run(host=host, port=port, debug=DEBUG_MODE)
    except OSError as exc:
        print(f"\nHATA: Port {port} kullanımda olabilir ({exc}).")
        print("Başka bir versiyon zaten bu portta çalışıyor olabilir.")
        print("Portu ayarlar.py içindeki WEB_PORT satırından değiştirebilirsiniz.")
