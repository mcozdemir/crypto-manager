# -*- coding: utf-8 -*-
"""
profiles.py
-----------
Kullanıcı profili: kullanıcı adı değiştirme, profil fotoğrafı, Binance API
anahtarları (demo / canlı, şifreli) ve işlem varsayılanları.

Her kullanıcı kendi Binance anahtarını kendisi girer; anahtar sunucuda
`secretbox` ile şifrelenerek saklanır ve tarayıcıya asla geri gönderilmez
(yalnızca ilk/son 4 karakteri gösterilir).
"""

import base64
import io
import json
import logging
import re
import time
from datetime import datetime
from functools import wraps
from typing import Dict, Optional

from flask import Blueprint, Response, jsonify, render_template, request, session

import auth
import secretbox
from db import get_db

logger = logging.getLogger("scanner.profiles")

bp = Blueprint("profiles", __name__)

AVATAR_SIZE = 256
AVATAR_MAX_UPLOAD = 5 * 1024 * 1024
USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
ENVS = ("demo", "live")

DEFAULT_SETTINGS = {
    "trade_env": "demo",          # demo | live
    "default_market": "futures",  # spot | futures
    "default_leverage": 3,
    "margin_type": "ISOLATED",    # ISOLATED | CROSSED
    "default_amount": 50.0,       # USDT
    "default_entry": "MARKET",    # MARKET | LIMIT
}

_ready = False


def init_tables():
    global _ready
    if _ready:
        return
    auth.init_users_table()
    get_db().execute_many([
        ("""CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                avatar_b64 TEXT,
                avatar_updated INTEGER,
                settings_json TEXT,
                updated_at TEXT
            )""", ()),
        ("""CREATE TABLE IF NOT EXISTS exchange_keys (
                user_id INTEGER NOT NULL,
                env TEXT NOT NULL,
                api_key_enc TEXT NOT NULL,
                secret_enc TEXT NOT NULL,
                key_hint TEXT,
                status_json TEXT,
                verified_at TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (user_id, env)
            )""", ()),
    ])
    _ready = True


# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------
def current_user() -> Optional[Dict]:
    name = session.get("user")
    return auth.get_user(name) if name else None


def api_guard(view):
    """
    Profil/işlem API'leri için: oturum + CSRF koruması. Tarayıcılar başka
    sitelerden özel başlıkla istek gönderemez (CORS ön kontrolü), bu yüzden
    X-CM-Request başlığı zorunludur.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if request.method != "GET" and request.headers.get("X-CM-Request") != "1":
            return jsonify({"ok": False, "message": "Geçersiz istek."}), 400
        user = current_user()
        if not user or not user["is_active"]:
            return jsonify({"ok": False, "message": "Oturum bulunamadı.", "login_required": True}), 401
        init_tables()
        return view(user, *args, **kwargs)
    return wrapper


def _profile_row(user_id: int) -> Dict:
    rows = get_db().execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
    return rows[0] if rows else {}


def get_settings(user_id: int) -> Dict:
    row = _profile_row(user_id)
    settings = dict(DEFAULT_SETTINGS)
    if row.get("settings_json"):
        try:
            settings.update(json.loads(row["settings_json"]))
        except ValueError:
            pass
    return settings


def _save_settings(user_id: int, settings: Dict):
    now = datetime.now().isoformat(timespec="seconds")
    get_db().execute("""
        INSERT INTO user_profiles (user_id, settings_json, updated_at) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET settings_json=excluded.settings_json,
            updated_at=excluded.updated_at
    """, (user_id, json.dumps(settings), now))


def key_status(user_id: int) -> Dict:
    rows = get_db().execute(
        "SELECT env, key_hint, status_json, verified_at FROM exchange_keys WHERE user_id = ?", (user_id,))
    out = {}
    for r in rows:
        try:
            status = json.loads(r["status_json"] or "{}")
        except ValueError:
            status = {}
        out[r["env"]] = {"hint": r["key_hint"], "verified_at": r["verified_at"], "status": status}
    return out


def get_client(user_id: int, env: str):
    """Kullanıcının ilgili ortam için imzalı Binance istemcisi (anahtar yoksa None)."""
    import binance_client
    rows = get_db().execute(
        "SELECT api_key_enc, secret_enc FROM exchange_keys WHERE user_id = ? AND env = ?", (user_id, env))
    if not rows:
        return None
    return binance_client.Client(env, secretbox.decrypt(rows[0]["api_key_enc"]),
                                 secretbox.decrypt(rows[0]["secret_enc"]))


def avatar_url(user: Dict) -> str:
    row = _profile_row(user["id"])
    v = row.get("avatar_updated") or 0
    return f"/avatar/{user['id']}?v={v}"


# --------------------------------------------------------------------------
# Sayfa ve profil API'si
# --------------------------------------------------------------------------
@bp.route("/profile")
def profile_page():
    init_tables()
    return render_template("profile.html")


@bp.route("/api/profile")
@api_guard
def api_profile(user):
    return jsonify({
        "ok": True,
        "username": user["username"],
        "created_at": user["created_at"],
        "last_login_at": user["last_login_at"],
        "avatar_url": avatar_url(user),
        "settings": get_settings(user["id"]),
        "keys": key_status(user["id"]),
        "encryption_ready": secretbox.is_configured(),
    })


@bp.route("/api/profile/username", methods=["POST"])
@api_guard
def api_username(user):
    new = auth.normalize_username((request.get_json(silent=True) or {}).get("username", ""))
    if not USERNAME_RE.match(new):
        return jsonify({"ok": False, "message": "Kullanıcı adı 3-32 karakter olmalı; yalnızca küçük harf, "
                                               "rakam, nokta, alt çizgi ve tire içerebilir."}), 400
    if new == user["username"]:
        return jsonify({"ok": True, "username": new})
    if auth.get_user(new):
        return jsonify({"ok": False, "message": "Bu kullanıcı adı kullanılıyor."}), 409
    get_db().execute("UPDATE users SET username = ? WHERE id = ?", (new, user["id"]))
    session["user"] = new
    logger.info("Kullanıcı adı değişti: %s -> %s", user["username"], new)
    return jsonify({"ok": True, "username": new,
                    "message": "Kullanıcı adı güncellendi. Bir sonraki girişte yeni adı kullanın; "
                               "Authenticator kodunuz değişmedi."})


@bp.route("/api/profile/avatar", methods=["POST", "DELETE"])
@api_guard
def api_avatar(user):
    now = int(time.time())
    if request.method == "DELETE":
        get_db().execute("""
            INSERT INTO user_profiles (user_id, avatar_b64, avatar_updated) VALUES (?, NULL, ?)
            ON CONFLICT(user_id) DO UPDATE SET avatar_b64=NULL, avatar_updated=excluded.avatar_updated
        """, (user["id"], now))
        return jsonify({"ok": True, "avatar_url": f"/avatar/{user['id']}?v={now}"})

    f = request.files.get("photo")
    if not f:
        return jsonify({"ok": False, "message": "Fotoğraf seçilmedi."}), 400
    data = f.read(AVATAR_MAX_UPLOAD + 1)
    if len(data) > AVATAR_MAX_UPLOAD:
        return jsonify({"ok": False, "message": "Fotoğraf en fazla 5 MB olabilir."}), 400
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img = ImageOps.fit(img, (AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85, optimize=True)
    except Exception:  # noqa: BLE001
        return jsonify({"ok": False, "message": "Dosya okunamadı; JPG, PNG veya WEBP yükleyin."}), 400
    b64 = base64.b64encode(out.getvalue()).decode("ascii")
    get_db().execute("""
        INSERT INTO user_profiles (user_id, avatar_b64, avatar_updated) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET avatar_b64=excluded.avatar_b64,
            avatar_updated=excluded.avatar_updated
    """, (user["id"], b64, now))
    return jsonify({"ok": True, "avatar_url": f"/avatar/{user['id']}?v={now}"})


@bp.route("/avatar/<int:user_id>")
def avatar(user_id: int):
    init_tables()
    row = _profile_row(user_id)
    if row.get("avatar_b64"):
        resp = Response(base64.b64decode(row["avatar_b64"]), mimetype="image/jpeg")
    else:
        users = get_db().execute("SELECT username FROM users WHERE id = ?", (user_id,))
        letter = (users[0]["username"][:1] if users else "?").upper()
        hue = (user_id * 67) % 360
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{AVATAR_SIZE}" height="{AVATAR_SIZE}" '
               f'viewBox="0 0 100 100"><rect width="100" height="100" fill="hsl({hue},45%,38%)"/>'
               f'<text x="50" y="50" dy=".35em" text-anchor="middle" font-family="Arial,sans-serif" '
               f'font-size="46" font-weight="700" fill="#fff">{letter}</text></svg>')
        resp = Response(svg, mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@bp.route("/api/profile/settings", methods=["POST"])
@api_guard
def api_settings(user):
    body = request.get_json(silent=True) or {}
    s = get_settings(user["id"])
    if "trade_env" in body:
        env = body["trade_env"]
        if env not in ENVS:
            return jsonify({"ok": False, "message": "Geçersiz ortam."}), 400
        if env == "live" and s.get("trade_env") != "live" and body.get("confirm_live") is not True:
            return jsonify({"ok": False, "message": "Canlı moda geçmek için onay gerekli."}), 400
        s["trade_env"] = env
    if "default_market" in body and body["default_market"] in ("spot", "futures"):
        s["default_market"] = body["default_market"]
    if "margin_type" in body and body["margin_type"] in ("ISOLATED", "CROSSED"):
        s["margin_type"] = body["margin_type"]
    if "default_entry" in body and body["default_entry"] in ("MARKET", "LIMIT"):
        s["default_entry"] = body["default_entry"]
    try:
        if "default_leverage" in body:
            s["default_leverage"] = max(1, min(20, int(body["default_leverage"])))
        if "default_amount" in body:
            s["default_amount"] = max(1.0, float(body["default_amount"]))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Kaldıraç ve tutar sayı olmalı."}), 400
    _save_settings(user["id"], s)
    return jsonify({"ok": True, "settings": s})


# --------------------------------------------------------------------------
# Binance API anahtarları
# --------------------------------------------------------------------------
def _hint(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 10 else "…"


@bp.route("/api/profile/keys", methods=["POST"])
@api_guard
def api_save_keys(user):
    import binance_client
    body = request.get_json(silent=True) or {}
    env = body.get("env")
    api_key = (body.get("api_key") or "").strip()
    secret = (body.get("api_secret") or "").strip()
    if env not in ENVS:
        return jsonify({"ok": False, "message": "Geçersiz ortam."}), 400
    if len(api_key) < 20 or len(secret) < 20:
        return jsonify({"ok": False, "message": "API Key ve Secret Key'i eksiksiz girin."}), 400
    if not secretbox.is_configured():
        return jsonify({"ok": False, "message": "Sunucuda şifreleme anahtarı (APP_ENCRYPTION_KEY) "
                                               "tanımlı değil; yöneticiye bildirin."}), 500
    status = binance_client.Client(env, api_key, secret).verify()
    spot_ok = status.get("spot", {}).get("ok")
    fut_ok = status.get("futures", {}).get("ok")
    if not spot_ok and not fut_ok:
        err = status.get("spot", {}).get("error") or status.get("futures", {}).get("error")
        return jsonify({"ok": False, "status": status,
                        "message": f"Anahtar doğrulanamadı: {err}"}), 400
    restr = status.get("restrictions") or {}
    if env == "live" and restr.get("withdrawals"):
        return jsonify({"ok": False, "status": status,
                        "message": "Bu anahtarda ÇEKİM (withdraw) izni açık. Güvenliğiniz için kabul "
                                   "edilmiyor: Binance'te anahtarın çekim iznini kapatıp tekrar deneyin."}), 400
    now = datetime.now().isoformat(timespec="seconds")
    get_db().execute("""
        INSERT INTO exchange_keys (user_id, env, api_key_enc, secret_enc, key_hint, status_json,
                                   verified_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, env) DO UPDATE SET api_key_enc=excluded.api_key_enc,
            secret_enc=excluded.secret_enc, key_hint=excluded.key_hint,
            status_json=excluded.status_json, verified_at=excluded.verified_at
    """, (user["id"], env, secretbox.encrypt(api_key), secretbox.encrypt(secret), _hint(api_key),
          json.dumps(status), now, now))
    logger.info("Binance anahtarı kaydedildi: kullanıcı=%s ortam=%s", user["username"], env)
    return jsonify({"ok": True, "status": status, "keys": key_status(user["id"])})


@bp.route("/api/profile/keys/<env>/test", methods=["POST"])
@api_guard
def api_test_keys(user, env):
    if env not in ENVS:
        return jsonify({"ok": False, "message": "Geçersiz ortam."}), 400
    try:
        client = get_client(user["id"], env)
    except secretbox.SecretBoxError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400
    if not client:
        return jsonify({"ok": False, "message": "Bu ortam için kayıtlı anahtar yok."}), 404
    status = client.verify()
    get_db().execute("UPDATE exchange_keys SET status_json = ?, verified_at = ? WHERE user_id = ? AND env = ?",
                     (json.dumps(status), datetime.now().isoformat(timespec="seconds"), user["id"], env))
    return jsonify({"ok": True, "status": status, "keys": key_status(user["id"])})


@bp.route("/api/profile/keys/<env>", methods=["DELETE"])
@api_guard
def api_delete_keys(user, env):
    get_db().execute("DELETE FROM exchange_keys WHERE user_id = ? AND env = ?", (user["id"], env))
    return jsonify({"ok": True, "keys": key_status(user["id"])})
