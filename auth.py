# -*- coding: utf-8 -*-
"""
auth.py
-------
Kullanıcı adı + tek kullanımlık kod (TOTP / Authenticator) ile giriş.

- Kodlar Google Authenticator, Microsoft Authenticator, 1Password vb.
  uygulamaların ürettiği 30 saniyelik 6 haneli kodlardır (RFC 6238).
  SMS / e-posta servisi gerekmez, tamamen ücretsizdir.
- Kullanıcılar sinyallerle aynı veritabanında (Turso) "users" tablosunda
  tutulur. Dışarıdan kayıt yoktur; kullanıcılar manage_users.py ile eklenir.
- Aynı kod ikinci kez kullanılamaz; art arda hatalı denemeler geçici
  olarak engellenir.
"""

import base64
import hashlib
import hmac
import logging
import os
import secrets
import struct
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from db import get_db

logger = logging.getLogger("scanner.auth")

ISSUER = "Crypto Manager"
TOTP_STEP = 30          # saniye
TOTP_DIGITS = 6
TOTP_WINDOW = 1         # saat kaymasına tolerans: ±1 adım (±30 sn)
SESSION_LIFETIME = timedelta(hours=12)

# Kaba kuvvet koruması
MAX_FAILURES = 5                    # bu kadar hatalı denemeden sonra...
LOCKOUT_SECONDS = 15 * 60           # ...bu süre boyunca giriş engellenir
MAX_IP_FAILURES = 20

bp = Blueprint("auth", __name__)

# Giriş gerektirmeyen yollar
PUBLIC_ENDPOINTS = {"auth.login", "auth.healthz", "static"}


# --------------------------------------------------------------------------
# TOTP (RFC 6238) — ek paket gerektirmez
# --------------------------------------------------------------------------
def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return str(code).zfill(TOTP_DIGITS)


def current_code(secret: str, at: Optional[float] = None) -> str:
    return _hotp(secret, int((at or time.time()) // TOTP_STEP))


def verify_code(secret: str, code: str, last_step: int = -1, at: Optional[float] = None) -> Optional[int]:
    """
    Kod doğruysa kullanılan zaman adımını döndürür, değilse None.
    last_step'ten eski/eşit adımlar reddedilir (aynı kod tekrar kullanılamaz).
    """
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return None
    now_step = int((at or time.time()) // TOTP_STEP)
    for step in range(now_step - TOTP_WINDOW, now_step + TOTP_WINDOW + 1):
        if step > last_step and hmac.compare_digest(_hotp(secret, step), code):
            return step
    return None


def provisioning_uri(username: str, secret: str) -> str:
    label = urllib.parse.quote(f"{ISSUER}:{username}")
    params = urllib.parse.urlencode({
        "secret": secret, "issuer": ISSUER, "algorithm": "SHA1",
        "digits": TOTP_DIGITS, "period": TOTP_STEP,
    })
    return f"otpauth://totp/{label}?{params}"


# --------------------------------------------------------------------------
# Kullanıcı tablosu
# --------------------------------------------------------------------------
_users_ready = False


def init_users_table():
    global _users_ready
    if _users_ready:
        return
    get_db().execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            totp_secret TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_totp_step INTEGER NOT NULL DEFAULT -1,
            created_at TEXT NOT NULL,
            last_login_at TEXT
        )
    """)
    _users_ready = True


def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def get_user(username: str) -> Optional[Dict]:
    init_users_table()
    rows = get_db().execute("SELECT * FROM users WHERE username = ?", (normalize_username(username),))
    return rows[0] if rows else None


def list_users() -> List[Dict]:
    init_users_table()
    return get_db().execute(
        "SELECT id, username, is_active, created_at, last_login_at FROM users ORDER BY username")


def create_user(username: str) -> str:
    """Yeni kullanıcı oluşturur, TOTP gizli anahtarını döndürür."""
    init_users_table()
    username = normalize_username(username)
    if not username or not username.replace(".", "").replace("_", "").replace("-", "").isalnum():
        raise ValueError("Kullanıcı adı yalnızca harf, rakam, nokta, alt çizgi ve tire içerebilir.")
    if get_user(username):
        raise ValueError(f"'{username}' kullanıcısı zaten var.")
    secret = new_secret()
    get_db().execute(
        "INSERT INTO users (username, totp_secret, created_at) VALUES (?, ?, ?)",
        (username, secret, datetime.now().isoformat(timespec="seconds")))
    return secret


def reset_user_secret(username: str) -> str:
    """Kullanıcının Authenticator kaydını yeniler (telefon kaybı vb.)."""
    if not get_user(username):
        raise ValueError(f"'{username}' kullanıcısı bulunamadı.")
    secret = new_secret()
    get_db().execute("UPDATE users SET totp_secret = ?, last_totp_step = -1 WHERE username = ?",
                     (secret, normalize_username(username)))
    return secret


def set_user_active(username: str, active: bool):
    if not get_user(username):
        raise ValueError(f"'{username}' kullanıcısı bulunamadı.")
    get_db().execute("UPDATE users SET is_active = ? WHERE username = ?",
                     (1 if active else 0, normalize_username(username)))


def delete_user(username: str):
    if not get_user(username):
        raise ValueError(f"'{username}' kullanıcısı bulunamadı.")
    get_db().execute("DELETE FROM users WHERE username = ?", (normalize_username(username),))


# --------------------------------------------------------------------------
# Kaba kuvvet koruması (bellek içi; tek sunucu süreci için yeterli)
# --------------------------------------------------------------------------
_fail_lock = threading.Lock()
_failures: Dict[str, List[float]] = {}


def _recent(key: str) -> List[float]:
    cutoff = time.time() - LOCKOUT_SECONDS
    items = [t for t in _failures.get(key, []) if t > cutoff]
    _failures[key] = items
    return items


def _is_locked(username: str, ip: str) -> bool:
    with _fail_lock:
        return (len(_recent("u:" + username)) >= MAX_FAILURES
                or len(_recent("ip:" + ip)) >= MAX_IP_FAILURES)


def _record_failure(username: str, ip: str):
    with _fail_lock:
        now = time.time()
        _failures.setdefault("u:" + username, []).append(now)
        _failures.setdefault("ip:" + ip, []).append(now)


def _clear_failures(username: str):
    with _fail_lock:
        _failures.pop("u:" + username, None)


def authenticate(username: str, code: str, ip: str) -> (bool, str):
    username = normalize_username(username)
    if _is_locked(username, ip):
        return False, "Çok fazla hatalı deneme. Lütfen 15 dakika sonra tekrar deneyin."
    user = get_user(username) if username else None
    step = None
    if user and user["is_active"]:
        step = verify_code(user["totp_secret"], code, int(user["last_totp_step"]))
    if step is None:
        _record_failure(username, ip)
        # Kullanıcının var olup olmadığını belli etmeyen genel mesaj
        return False, "Kullanıcı adı veya kod hatalı."
    # Kullanılan kod adımı kaydedilir; aynı kod bir daha kabul edilmez.
    get_db().execute(
        "UPDATE users SET last_totp_step = ?, last_login_at = ? WHERE id = ? AND last_totp_step < ?",
        (step, datetime.now().isoformat(timespec="seconds"), user["id"], step))
    _clear_failures(username)
    return True, user["username"]


# --------------------------------------------------------------------------
# Flask entegrasyonu
# --------------------------------------------------------------------------
def init_app(app):
    secret_key = os.environ.get("APP_SECRET_KEY", "").strip()
    if not secret_key:
        secret_key = secrets.token_hex(32)
        logger.warning("APP_SECRET_KEY tanımlı değil; geçici anahtar üretildi "
                       "(uygulama yeniden başlayınca oturumlar kapanır).")
    on_server = bool(os.environ.get("RENDER")) or os.environ.get("APP_HTTPS", "") == "1"
    app.config.update(
        SECRET_KEY=secret_key,
        SESSION_COOKIE_NAME="cm_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=on_server,
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
    )
    if on_server:
        # Render gibi ters vekil (proxy) arkasında doğru https/IP bilgisi için
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.register_blueprint(bp)

    @app.before_request
    def _require_login():
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if session.get("user"):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "message": "Oturum süresi doldu, lütfen tekrar giriş yapın.",
                            "login_required": True}), 401
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if on_server:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @app.context_processor
    def _inject_user():
        return {"current_user": session.get("user")}


def _safe_next(target: Optional[str]) -> str:
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("index")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user"):
        return redirect(_safe_next(request.args.get("next")))
    error = None
    username = ""
    if request.method == "POST":
        username = request.form.get("username", "")
        code = request.form.get("code", "")
        ip = request.remote_addr or "?"
        try:
            ok, result = authenticate(username, code, ip)
        except Exception as exc:  # noqa: BLE001  (ör. veritabanına ulaşılamadı)
            logger.exception("Giriş sırasında hata")
            ok, result = False, f"Giriş şu anda yapılamıyor: {exc}"
        if ok:
            session.clear()
            session.permanent = True
            session["user"] = result
            logger.info("Giriş yapıldı: %s (%s)", result, ip)
            return redirect(_safe_next(request.args.get("next")))
        logger.warning("Başarısız giriş denemesi: %r (%s)", normalize_username(username), ip)
        error = result
    return render_template("login.html", error=error, username=username), (401 if error else 200)


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/healthz")
def healthz():
    return jsonify({"ok": True})
