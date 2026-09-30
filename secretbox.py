# -*- coding: utf-8 -*-
"""
secretbox.py
------------
Kullanıcıların Binance API anahtarlarını veritabanında ŞİFRELİ saklamak için.

- Şifreleme: Fernet (AES-128-CBC + HMAC-SHA256), `cryptography` paketi.
- Ana anahtar: APP_ENCRYPTION_KEY ortam değişkeni (herhangi bir uzun rastgele
  metin; Fernet anahtarı ondan SHA-256 ile türetilir).
- ÖNEMLİ: Yerel uygulama ve Render aynı Turso veritabanını kullandığı için
  APP_ENCRYPTION_KEY her iki ortamda da AYNI olmalıdır. Değiştirilirse
  kayıtlı API anahtarları çözülemez; kullanıcıların yeniden girmesi gerekir.
- Çözülmüş anahtar hiçbir zaman tarayıcıya gönderilmez.
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

ENV_KEY = "APP_ENCRYPTION_KEY"


class SecretBoxError(Exception):
    pass


def is_configured() -> bool:
    from db import load_env
    load_env()
    return len(os.environ.get(ENV_KEY, "").strip()) >= 16


def _fernet() -> Fernet:
    if not is_configured():
        raise SecretBoxError(
            f"Sunucuda {ENV_KEY} tanımlı değil (en az 16 karakter). API anahtarları "
            "şifrelenemediği için kaydedilemez.")
    raw = os.environ[ENV_KEY].strip().encode("utf-8")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise SecretBoxError(
            "Kayıtlı API anahtarı çözülemedi (şifreleme anahtarı değişmiş olabilir). "
            "Lütfen Profil → Binance API bölümünden anahtarı yeniden girin.") from exc
