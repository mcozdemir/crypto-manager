# -*- coding: utf-8 -*-
"""
trading.py
----------
Tarama sonuçlarından / sinyal günlüğünden Binance emri açma API'si.

Akış:
  1. GET  /api/trade/context   -> kullanıcının varsayılanları, anahtar durumu
  2. GET  /api/trade/quote     -> güncel fiyat (anahtar gerekmez, 3 sn'de bir yenilenir)
  3. POST /api/trade/preview   -> emir özeti (doğrulama, yuvarlama, bakiye, kâr/zarar)
  4. POST /api/trade/place     -> özet sunucuda güncel fiyatla YENİDEN hesaplanır ve
                                  gönderilir; canlı modda "ONAYLA" yazılması zorunlu
  5. GET  /api/trade/orders    -> kullanıcının emir geçmişi

Her emir denemesi (başarılı / hatalı) trade_orders tablosuna kaydedilir.
"""

import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict

from flask import Blueprint, jsonify, request

import binance_client
import profiles
import secretbox
from db import get_db
from version import VERSION

logger = logging.getLogger("scanner.trading")

bp = Blueprint("trading", __name__)

_ready = False
_user_locks: Dict[int, threading.Lock] = {}
_last_submit: Dict[int, tuple] = {}
DUPLICATE_WINDOW_SEC = 15


def init_tables():
    global _ready
    if _ready:
        return
    profiles.init_tables()
    get_db().execute_many([
        ("""CREATE TABLE IF NOT EXISTS trade_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                env TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_type TEXT,
                amount_usdt REAL,
                leverage INTEGER,
                margin_type TEXT,
                quantity TEXT,
                entry_price TEXT,
                avg_price TEXT,
                target TEXT,
                stop TEXT,
                status TEXT NOT NULL,
                protected INTEGER,
                signal_ref TEXT,
                result_json TEXT,
                error TEXT,
                app_version TEXT
            )""", ()),
        ("CREATE INDEX IF NOT EXISTS idx_trade_orders_user ON trade_orders (user_id, created_at)", ()),
    ])
    _ready = True


def _guard(view):
    wrapped = profiles.api_guard(view)

    def inner(*a, **k):
        init_tables()
        return wrapped(*a, **k)
    inner.__name__ = view.__name__
    return inner


def _strip_raw(plan: Dict) -> Dict:
    return {k: v for k, v in plan.items() if not k.startswith("_")}


@bp.route("/api/trade/context")
@_guard
def api_context(user):
    s = profiles.get_settings(user["id"])
    keys = profiles.key_status(user["id"])
    return jsonify({"ok": True, "settings": s, "keys": {e: {"hint": v["hint"]} for e, v in keys.items()},
                    "env": s["trade_env"], "env_label": binance_client.ENV_LABEL[s["trade_env"]],
                    "has_key": s["trade_env"] in keys, "encryption_ready": secretbox.is_configured(),
                    "max_leverage": binance_client.MAX_LEVERAGE})


@bp.route("/api/trade/quote")
@_guard
def api_quote(user):
    symbol = (request.args.get("symbol") or "").upper()
    market = request.args.get("market", "futures")
    env = profiles.get_settings(user["id"])["trade_env"]
    try:
        q = binance_client.current_price(env, market, symbol)
        rules = binance_client.symbol_rules(env, market, symbol)
    except (binance_client.BinanceError, binance_client.PlanError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "message": f"Fiyat alınamadı: {exc}"}), 502
    return jsonify({"ok": True, "symbol": symbol, "market": market,
                    "price": binance_client.fmt(q["price"]),
                    "mark": binance_client.fmt(q["mark"]) if q.get("mark") is not None else None,
                    "tick": binance_client.fmt(rules["tick"]),
                    "min_notional": binance_client.fmt(rules["min_notional"]),
                    "ts": int(time.time())})


def _plan_for(user, body):
    env = profiles.get_settings(user["id"])["trade_env"]
    client = profiles.get_client(user["id"], env)
    if client is None:
        raise binance_client.PlanError(
            f"{binance_client.ENV_LABEL[env]} için Binance API anahtarınız yok. "
            "Profil → Binance API bölümünden ekleyin.")
    return env, client, binance_client.build_plan(client, env, body)


@bp.route("/api/trade/preview", methods=["POST"])
@_guard
def api_preview(user):
    body = request.get_json(silent=True) or {}
    try:
        env, _, plan = _plan_for(user, body)
    except (binance_client.PlanError, binance_client.BinanceError, secretbox.SecretBoxError) as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        logger.exception("Önizleme hatası")
        return jsonify({"ok": False, "message": f"Özet hazırlanamadı: {exc}"}), 502
    return jsonify({"ok": True, "plan": _strip_raw(plan)})


@bp.route("/api/trade/place", methods=["POST"])
@_guard
def api_place(user):
    body = request.get_json(silent=True) or {}
    lock = _user_locks.setdefault(user["id"], threading.Lock())
    if not lock.acquire(blocking=False):
        return jsonify({"ok": False, "message": "Önceki emriniz hâlâ işleniyor."}), 409
    try:
        sig = (body.get("symbol"), body.get("direction"), body.get("market"), str(body.get("amount_usdt")))
        last = _last_submit.get(user["id"])
        if last and last[0] == sig and time.time() - last[1] < DUPLICATE_WINDOW_SEC:
            return jsonify({"ok": False, "message": "Aynı emir birkaç saniye önce gönderildi; "
                                                   "çift emir engellendi."}), 409
        try:
            env, client, plan = _plan_for(user, body)
        except (binance_client.PlanError, binance_client.BinanceError, secretbox.SecretBoxError) as exc:
            return jsonify({"ok": False, "message": str(exc)}), 400
        if env == "live" and (body.get("confirm_text") or "").strip().upper() != "ONAYLA":
            return jsonify({"ok": False, "message": "Canlı emir için onay kutusuna ONAYLA yazın."}), 400
        if body.get("confirmed") is not True:
            return jsonify({"ok": False, "message": "Emir özeti onaylanmadı."}), 400
        result, error = None, None
        try:
            result = binance_client.place(client, plan)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            logger.warning("Emir hatası (%s %s %s): %s", user["username"], env, plan["symbol"], exc)
        if not error:
            _last_submit[user["id"]] = (sig, time.time())  # yalnızca gönderilen emir çift sayılır
        status = "HATA" if error else ("GÖNDERİLDİ" if (result or {}).get("protected") else "KORUMASIZ")
        _record(user, env, plan, body, result, error, status)
        if error:
            return jsonify({"ok": False, "message": f"Emir gönderilemedi: {error}",
                            "plan": _strip_raw(plan)}), 400
        clean = {**result, "steps": [{k: v for k, v in s.items() if k != "response"}
                                     for s in result.get("steps", [])]}
        return jsonify({"ok": True, "status": status, "result": clean, "plan": _strip_raw(plan)})
    finally:
        lock.release()


def _record(user, env, plan, body, result, error, status):
    try:
        get_db().execute("""
            INSERT INTO trade_orders (created_at, user_id, username, env, market, symbol, direction,
                entry_type, amount_usdt, leverage, margin_type, quantity, entry_price, avg_price,
                target, stop, status, protected, signal_ref, result_json, error, app_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(timespec="seconds"), user["id"], user["username"], env,
              plan["market"], plan["symbol"], plan["direction"], plan["entry_type"],
              float(plan["amount_usdt"]), plan["leverage"], plan.get("margin_type"),
              (result or {}).get("quantity") or plan["quantity"], plan["entry_price"],
              (result or {}).get("avg_price"), plan["target"], plan["stop"], status,
              1 if (result or {}).get("protected") else 0, str(body.get("signal_ref") or "")[:80] or None,
              json.dumps(result, default=str)[:20000] if result else None, error, VERSION))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Emir kaydı yazılamadı: %s", exc)


@bp.route("/api/trade/orders")
@_guard
def api_orders(user):
    rows = get_db().execute("""
        SELECT id, created_at, env, market, symbol, direction, entry_type, amount_usdt, leverage,
               margin_type, quantity, entry_price, avg_price, target, stop, status, protected, error,
               app_version
        FROM trade_orders WHERE user_id = ? ORDER BY id DESC LIMIT 100
    """, (user["id"],))
    return jsonify({"ok": True, "orders": rows})


# --------------------------------------------------------------------------
# İşlemlerim paneli: canlı pozisyonlar, emirler, müdahale
# --------------------------------------------------------------------------
import binance_account  # noqa: E402
from decimal import Decimal  # noqa: E402
from flask import render_template  # noqa: E402

_snap_cache: Dict[tuple, tuple] = {}
SNAPSHOT_TTL_SEC = 4
_actions_ready = False


def _init_actions():
    global _actions_ready
    if _actions_ready:
        return
    get_db().execute_many([
        ("""CREATE TABLE IF NOT EXISTS trade_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                env TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                action TEXT NOT NULL,
                detail_json TEXT,
                ok INTEGER,
                error TEXT,
                app_version TEXT
            )""", ()),
        ("CREATE INDEX IF NOT EXISTS idx_trade_actions_user ON trade_actions (user_id, created_at)", ()),
    ])
    _actions_ready = True


def _record_action(user, env, market, symbol, action, detail, ok, error=None):
    try:
        _init_actions()
        get_db().execute("""
            INSERT INTO trade_actions (created_at, user_id, username, env, market, symbol, action,
                                       detail_json, ok, error, app_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(timespec="seconds"), user["id"], user["username"], env, market,
              symbol, action, json.dumps(detail, default=str)[:20000], 1 if ok else 0, error, VERSION))
    except Exception as exc:  # noqa: BLE001
        logger.warning("İşlem kaydı yazılamadı: %s", exc)


def _env_for(user, requested):
    s = profiles.get_settings(user["id"])
    env = requested if requested in ("demo", "live") else s["trade_env"]
    return env


def _client_or_error(user, env):
    client = profiles.get_client(user["id"], env)
    if client is None:
        raise binance_client.PlanError(
            f"{binance_client.ENV_LABEL[env]} için Binance API anahtarınız yok. Profil → Binance API "
            "bölümünden ekleyin.")
    return client


def _app_spot_entries(user_id: int, env: str) -> Dict[str, Decimal]:
    rows = get_db().execute("""
        SELECT symbol, avg_price, entry_price FROM trade_orders
        WHERE user_id = ? AND env = ? AND market = 'spot' AND status != 'HATA'
        ORDER BY id ASC
    """, (user_id, env))
    out = {}
    for r in rows:
        price = r["avg_price"] or r["entry_price"]
        try:
            out[r["symbol"]] = binance_client.D(price)
        except Exception:  # noqa: BLE001
            continue
    return out


@bp.route("/trades")
def trades_page():
    return render_template("trades.html")


@bp.route("/api/trades/snapshot")
@_guard
def api_snapshot(user):
    env = _env_for(user, request.args.get("env"))
    keys = profiles.key_status(user["id"])
    base = {"env": env, "env_label": binance_client.ENV_LABEL[env],
            "envs_with_key": sorted(keys.keys()), "ts": int(time.time())}
    if env not in keys:
        return jsonify({**base, "ok": True, "has_key": False})
    force = request.args.get("force") == "1"
    ck = (user["id"], env)
    hit = _snap_cache.get(ck)
    if hit and not force and time.time() - hit[0] < SNAPSHOT_TTL_SEC:
        return jsonify({**hit[1], "ts": int(hit[0]), "cached": True})
    try:
        client = _client_or_error(user, env)
    except (binance_client.PlanError, secretbox.SecretBoxError) as exc:
        return jsonify({**base, "ok": False, "message": str(exc)}), 400
    out = {**base, "ok": True, "has_key": True}
    try:
        out["futures"] = binance_account.futures_snapshot(client, user["id"])
    except Exception as exc:  # noqa: BLE001
        out["futures"] = {"ok": False, "message": str(exc)}
    try:
        out["spot"] = binance_account.spot_snapshot(client, _app_spot_entries(user["id"], env))
    except Exception as exc:  # noqa: BLE001
        out["spot"] = {"ok": False, "message": str(exc)}
    out["app_orders"] = get_db().execute("""
        SELECT id, created_at, market, symbol, direction, entry_type, amount_usdt, leverage,
               avg_price, entry_price, target, stop, status
        FROM trade_orders WHERE user_id = ? AND env = ? ORDER BY id DESC LIMIT 20
    """, (user["id"], env))
    _init_actions()
    out["actions"] = get_db().execute("""
        SELECT created_at, market, symbol, action, ok, error FROM trade_actions
        WHERE user_id = ? AND env = ? ORDER BY id DESC LIMIT 20
    """, (user["id"], env))
    _snap_cache[ck] = (time.time(), out)
    return jsonify(out)


def _action(user, body, name, fn):
    """Ortak müdahale akışı: ortam, anahtar, canlı onayı, kilit, kayıt."""
    env = _env_for(user, body.get("env"))
    market = body.get("market")
    symbol = str(body.get("symbol") or "").upper()
    if market not in ("spot", "futures") or not symbol:
        return jsonify({"ok": False, "message": "Eksik bilgi."}), 400
    if body.get("confirmed") is not True:
        return jsonify({"ok": False, "message": "İşlem onaylanmadı."}), 400
    if env == "live" and (body.get("confirm_text") or "").strip().upper() != "ONAYLA":
        return jsonify({"ok": False, "message": "Canlı hesapta işlem için ONAYLA yazın."}), 400
    lock = _user_locks.setdefault(user["id"], threading.Lock())
    if not lock.acquire(blocking=False):
        return jsonify({"ok": False, "message": "Önceki işleminiz hâlâ sürüyor."}), 409
    try:
        try:
            client = _client_or_error(user, env)
            result = fn(client, env, market, symbol)
        except (binance_client.PlanError, binance_client.BinanceError, secretbox.SecretBoxError) as exc:
            _record_action(user, env, market, symbol, name, body, False, str(exc))
            return jsonify({"ok": False, "message": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            logger.exception("Panel işlemi hatası")
            _record_action(user, env, market, symbol, name, body, False, str(exc))
            return jsonify({"ok": False, "message": f"İşlem yapılamadı: {exc}"}), 502
        _record_action(user, env, market, symbol, name, {"request": body, "result": result},
                       result.get("ok", True), result.get("critical"))
        _snap_cache.pop((user["id"], env), None)
        return jsonify({"ok": bool(result.get("ok", True)), "result": result,
                        "message": result.get("critical")})
    finally:
        lock.release()


@bp.route("/api/trades/close", methods=["POST"])
@_guard
def api_trades_close(user):
    body = request.get_json(silent=True) or {}

    def fn(client, env, market, symbol):
        if market == "futures":
            return binance_account.close_futures(client, symbol, str(body.get("position_side") or "BOTH").upper())
        return binance_account.close_spot(client, symbol)
    return _action(user, body, "KAPAT", fn)


@bp.route("/api/trades/cancel", methods=["POST"])
@_guard
def api_trades_cancel(user):
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    if kind not in ("order", "algo", "list") or body.get("id") in (None, ""):
        return jsonify({"ok": False, "message": "Geçersiz emir."}), 400

    def fn(client, env, market, symbol):
        binance_account.cancel(client, market, kind, symbol, body["id"])
        return {"ok": True, "steps": [{"name": "Emir iptal edildi", "ok": True}]}
    return _action(user, body, "İPTAL", fn)


@bp.route("/api/trades/tpsl", methods=["POST"])
@_guard
def api_trades_tpsl(user):
    body = request.get_json(silent=True) or {}

    def fn(client, env, market, symbol):
        if market == "futures":
            return binance_account.edit_futures_tpsl(client, symbol, str(body.get("position_side") or "BOTH").upper(),
                                                     body.get("target"), body.get("stop"))
        return binance_account.edit_spot_tpsl(client, symbol, body.get("target"), body.get("stop"))
    return _action(user, body, "HEDEF-STOP", fn)
