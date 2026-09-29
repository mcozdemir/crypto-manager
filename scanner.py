# -*- coding: utf-8 -*-
"""
scanner.py
----------
Tüm sürecin orkestrasyonu:
1. İlk 50 hacimli USDT paritesini belirle
2. Her paritede 1H / 4H / 1D zaman dilimlerini tara
3. İndikatörleri hesapla
4. 10 formasyonu tespit et
5. Puanla, 60 altını ele
6. Grafik üret
7. Sonuç listesini döndür
"""

import logging
from datetime import datetime
from typing import List, Dict, Callable, Optional

from utils import get_top_usdt_symbols, get_klines, safe_request_sleep
from indicators import add_indicators
from patterns import detect_all_patterns
from scoring import score_pattern
from charts import plot_pattern
from ayarlar import DEFAULT_TOP_N, TIMEFRAMES, MIN_SCORE, KLINE_LIMIT
import confidence

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scanner")

def run_scan(top_n: int = DEFAULT_TOP_N, timeframes: Optional[List[str]] = None,
             progress_cb: Optional[Callable[[dict], None]] = None,
             log_to_journal: bool = True) -> List[Dict]:
    """
    Tüm tarama sürecini çalıştırır.
    progress_cb: opsiyonel; her adımda {"stage":..., "current":..., "total":..., "message":...}
                 sözlüğü ile çağrılan callback (web panelinde ilerleme çubuğu için kullanılır).
    log_to_journal: True ise, bulunan sinyaller otomatik olarak signal_journal.db'ye
                    kaydedilir (Sinyal Günlüğü özelliği için).
    """
    timeframes = timeframes or TIMEFRAMES

    def _progress(stage, current, total, message):
        if progress_cb:
            try:
                progress_cb({"stage": stage, "current": current, "total": total, "message": message})
            except Exception:  # noqa: BLE001
                pass
        logger.info("[%s] %s (%s/%s)", stage, message, current, total)

    _progress("symbols", 0, 1, "Hacme göre ilk %d USDT paritesi belirleniyor..." % top_n)
    symbols = get_top_usdt_symbols(limit=top_n)
    _progress("symbols", 1, 1, f"{len(symbols)} parite bulundu.")

    results: List[Dict] = []
    total_tasks = len(symbols) * len(timeframes)
    task_idx = 0

    features_by_symbol: Dict[str, Dict] = {}

    for symbol in symbols:
        symbol_dfs: Dict = {}
        results_before = len(results)
        for tf in timeframes:
            task_idx += 1
            _progress("scanning", task_idx, total_tasks, f"{symbol} ({tf}) analiz ediliyor...")
            try:
                df = get_klines(symbol, tf, limit=KLINE_LIMIT)
                safe_request_sleep()
                if len(df) < 60:
                    continue
                df = add_indicators(df)
                symbol_dfs[tf] = df

                found_patterns = detect_all_patterns(df)
                candidates = []
                for pattern in found_patterns:
                    score_info = score_pattern(df, pattern)
                    if score_info["total_score"] < MIN_SCORE:
                        continue
                    candidates.append((pattern, score_info))

                # BUG FİX: Aynı coin+zaman diliminde hem LONG hem SHORT formasyon
                # eşiği geçtiğinde ikisi de "aktif fırsat" gibi gösteriliyordu
                # (ör. DOGEUSDT 1H'de aynı anda Bull Flag LONG + Bear Flag SHORT +
                # Rising Wedge SHORT). Bu çelişkili/kafa karıştırıcı bir sinyal
                # seti oluşturuyordu. Çelişki varsa yalnızca en yüksek skorlu
                # yönün formasyonları tutulur, diğer yöndekiler elenir.
                directions_present = {p["direction"] for p, _ in candidates}
                if len(directions_present) > 1:
                    best_direction = max(
                        directions_present,
                        key=lambda d: max(s["total_score"] for p, s in candidates if p["direction"] == d)
                    )
                    candidates = [(p, s) for p, s in candidates if p["direction"] == best_direction]

                for pattern, score_info in candidates:
                    chart_path = None
                    try:
                        chart_path = plot_pattern(df, symbol, tf, pattern)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Grafik oluşturulamadı %s %s %s: %s",
                                        symbol, tf, pattern["name"], exc)

                    results.append({
                        "symbol": symbol,
                        "timeframe": tf,
                        "pattern": pattern["name"],
                        "direction": pattern["direction"],
                        "score": score_info["total_score"],
                        "label": score_info["label"],
                        "success_probability": score_info["success_probability"],
                        "breakout": "VAR" if pattern.get("breakout_confirmed") else "YOK",
                        "volume_desc": score_info["volume_desc"],
                        "fake_breakout_risk": pattern.get("fake_breakout_risk"),
                        "target": pattern.get("target"),
                        "stop_loss": pattern.get("stop_loss"),
                        "breakout_level": pattern.get("breakout_level"),
                        "last_price": float(df["close"].iloc[-1]),
                        "chart_path": chart_path,
                        "score_breakdown": score_info["scores"],
                        "detected_at": datetime.now().isoformat(timespec="seconds"),
                    })
            except Exception as exc:  # noqa: BLE001
                logger.warning("Hata (%s / %s): %s", symbol, tf, exc)
                continue
        # Güven endeksleri için coinin tüm zaman dilimlerindeki trend özeti
        # (yalnızca sinyal çıkan coinler için; bellek ve istek tasarrufu)
        if len(results) > results_before:
            features_by_symbol[symbol] = confidence.ensure_daily(
                symbol, confidence.symbol_features(symbol_dfs))

    if results:
        _progress("confidence", total_tasks, total_tasks,
                  "Güven endeksleri hesaplanıyor (piyasa yönü, türev piyasa, likidite)...")
        try:
            ctx = confidence.ScanContext(timeframes)
            confidence.enrich(results, ctx, features_by_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Güven endeksleri hesaplanamadı: %s", exc)

    results.sort(key=lambda r: r["success_probability"], reverse=True)
    _progress("done", total_tasks, total_tasks, f"Tarama tamamlandı. {len(results)} formasyon bulundu.")

    if log_to_journal and results:
        try:
            import journal
            new_count = journal.record_signals(results)
            logger.info("Sinyal günlüğüne %d yeni kayıt eklendi.", new_count)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sinyal günlüğüne kaydedilemedi: %s", exc)

    return results


def top_opportunities(results: List[Dict], direction: str, n: int = 10) -> List[Dict]:
    filtered = [r for r in results if r["direction"] == direction]
    filtered.sort(key=lambda r: r["success_probability"], reverse=True)
    return filtered[:n]


def scan_single_symbol(symbol: str, timeframes: Optional[List[str]] = None,
                        progress_cb: Optional[Callable[[dict], None]] = None,
                        log_to_journal: bool = True) -> Dict:
    """
    Belirli TEK bir coin için (ana taramadaki ilk 50 hacimli listede olsun
    ya da olmasın) tüm zaman dilimlerini tarar. Ana taramadan farkı: yalnızca
    60 puan üzerindeki (aktif) sinyalleri değil, eşiği GEÇEMEYEN formasyonları
    da (skorlarıyla birlikte) döndürür — böylece "bu coin neden ana taramada
    çıkmadı" sorusuna somut bir cevap verir.
    """
    timeframes = timeframes or TIMEFRAMES
    symbol = symbol.strip().upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"

    def _progress(current, total, message):
        if progress_cb:
            try:
                progress_cb({"current": current, "total": total, "message": message})
            except Exception:  # noqa: BLE001
                pass
        logger.info("[coin_search] %s (%s/%s)", message, current, total)

    qualified: List[Dict] = []
    below_threshold: List[Dict] = []
    total_tasks = len(timeframes)
    symbol_dfs: Dict = {}

    for task_idx, tf in enumerate(timeframes, start=1):
        _progress(task_idx - 1, total_tasks, f"{symbol} ({tf}) analiz ediliyor...")
        df = get_klines(symbol, tf, limit=KLINE_LIMIT)
        safe_request_sleep()
        if len(df) < 60:
            continue
        df = add_indicators(df)
        symbol_dfs[tf] = df

        found_patterns = detect_all_patterns(df)
        tf_qualified = []
        for pattern in found_patterns:
            score_info = score_pattern(df, pattern)

            chart_path = None
            try:
                chart_path = plot_pattern(df, symbol, tf, pattern)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Grafik oluşturulamadı %s %s %s: %s",
                                symbol, tf, pattern["name"], exc)

            row = {
                "symbol": symbol,
                "timeframe": tf,
                "pattern": pattern["name"],
                "direction": pattern["direction"],
                "score": score_info["total_score"],
                "label": score_info["label"],
                "success_probability": score_info["success_probability"],
                "breakout": "VAR" if pattern.get("breakout_confirmed") else "YOK",
                "volume_desc": score_info["volume_desc"],
                "fake_breakout_risk": pattern.get("fake_breakout_risk"),
                "target": pattern.get("target"),
                "stop_loss": pattern.get("stop_loss"),
                "breakout_level": pattern.get("breakout_level"),
                "last_price": float(df["close"].iloc[-1]),
                "chart_path": chart_path,
                "score_breakdown": score_info["scores"],
                "detected_at": datetime.now().isoformat(timespec="seconds"),
            }
            if score_info["total_score"] >= MIN_SCORE:
                tf_qualified.append(row)
            else:
                below_threshold.append(row)

        # Ana taramadaki gibi: aynı coin+TF'de çelişkili yön varsa yalnızca
        # en yüksek skorlu yön "aktif sinyal" sayılır (diğeri below_threshold'a
        # düşmez, sadece qualified listesine girmez; below_threshold zaten
        # tüm ham formasyonları gösterdiği için kullanıcı yine görebilir).
        directions_present = {r["direction"] for r in tf_qualified}
        if len(directions_present) > 1:
            best_direction = max(
                directions_present,
                key=lambda d: max(r["score"] for r in tf_qualified if r["direction"] == d)
            )
            tf_qualified = [r for r in tf_qualified if r["direction"] == best_direction]

        qualified.extend(tf_qualified)

    if qualified or below_threshold:
        try:
            ctx = confidence.ScanContext(timeframes)
            feats = confidence.ensure_daily(symbol, confidence.symbol_features(symbol_dfs))
            confidence.enrich(qualified + below_threshold, ctx, {symbol: feats})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Güven endeksleri hesaplanamadı: %s", exc)

    qualified.sort(key=lambda r: r["success_probability"], reverse=True)
    below_threshold.sort(key=lambda r: r["score"], reverse=True)

    _progress(total_tasks, total_tasks,
              f"Tarama tamamlandı. {len(qualified)} aktif sinyal, "
              f"{len(below_threshold)} eşiği geçemeyen formasyon bulundu.")

    if log_to_journal and qualified:
        try:
            import journal
            journal.record_signals(qualified)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Sinyal günlüğüne kaydedilemedi: %s", exc)

    return {
        "symbol": symbol,
        "qualified": qualified,
        "below_threshold": below_threshold,
    }
