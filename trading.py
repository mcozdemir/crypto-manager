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
