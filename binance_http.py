# -*- coding: utf-8 -*-
"""
binance_http.py
---------------
Binance'e yapılan TÜM anahtarsız (herkese açık) isteklerin ortak kapısı.

Neden var?
  Binance, dakikalık istek ağırlığı aşıldığında 429 döner. 429'dan sonra istek
  göndermeye devam eden IP'yi 418 ("I'm a teapot") ile 2 dakikadan 3 güne kadar
  YASAKLAR. Render'da çıkış IP'si başka uygulamalarla paylaşıldığı için bu sınıra
  başkalarının trafiği de etki eder. Bu modül:

  1. 429 / 418 alınca o adresi `Retry-After` süresince "soğumaya" alır ve o süre
     boyunca o adrese HİÇ istek göndermez (yasağın uzamasını engeller),
  2. Spot piyasa verisi için yedek adrese geçer: `data-api.binance.vision`
     (Binance'in yalnızca piyasa verisi sunan resmî uç noktası; aynı yanıt biçimi),
  3. Tüm adresler soğumadaysa anlaşılır bir Türkçe hata verir,
  4. Kullanılan ağırlığı (X-MBX-USED-WEIGHT-1M) izler; sınıra yaklaşınca yavaşlar.
"""

import logging
import threading
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger("scanner.binance_http")

SPOT_HOSTS = ["https://api.binance.com", "https://data-api.binance.vision"]
FUTURES_HOSTS = ["https://fapi.binance.com"]

WEIGHT_LIMIT = {"spot": 6000, "futures": 2400}   # dakikalık ağırlık sınırları
SLOW_DOWN_RATIO = 0.8                            # bu orandan sonra istekler yavaşlatılır
DEFAULT_BACKOFF = {429: 60, 418: 300, 451: 3600}  # Retry-After yoksa (saniye)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "crypto-manager/1.0"})

_lock = threading.Lock()
_cooldown: Dict[str, float] = {}        # host -> bu zamana kadar istek yok
_cooldown_code: Dict[str, int] = {}
_last_weight: Dict[str, int] = {}


class BinanceUnavailable(RuntimeError):
    """Binance geçici olarak erişilemez (hız sınırı / IP yasağı / bölge engeli)."""

    def __init__(self, msg: str, retry_after: int = 0, code: Optional[int] = None):
        super().__init__(msg)
        self.retry_after = retry_after
        self.code = code


def _retry_after(resp) -> int:
    try:
        return max(1, int(float(resp.headers.get("Retry-After", ""))))
    except ValueError:
        return DEFAULT_BACKOFF.get(resp.status_code, 60)


def _human(seconds: float) -> str:
    seconds = int(max(1, seconds))
    if seconds < 90:
        return f"{seconds} sn"
    if seconds < 5400:
        return f"{round(seconds / 60)} dk"
    return f"{round(seconds / 3600, 1)} saat".replace(".", ",")


def status() -> Dict:
    """Arayüz / sağlık kontrolü için soğumadaki adresler."""
    now = time.time()
    with _lock:
        return {h: {"remaining_sec": int(t - now), "code": _cooldown_code.get(h)}
                for h, t in _cooldown.items() if t > now}


def _market_of(host: str) -> str:
    return "futures" if "fapi" in host else "spot"


def get(hosts: List[str], path: str, params: dict = None, timeout: float = 10.0):
    """
    Sıradaki uygun adresten GET isteği yapar ve JSON döndürür.
    429/418/451'de adresi soğumaya alıp bir sonrakine geçer. Diğer HTTP
    hataları (400 geçersiz sembol vb.) olduğu gibi `requests.HTTPError` fırlatır.
    """
    last_err = None
    waits = []
    for host in hosts:
        now = time.time()
        with _lock:
            until = _cooldown.get(host, 0)
        if until > now:
            waits.append(until - now)
            continue
        market = _market_of(host)
        used = _last_weight.get(host, 0)
        if used > WEIGHT_LIMIT[market] * SLOW_DOWN_RATIO:
            time.sleep(1.0)  # sınıra yaklaşıldı: dakikanın dolmasını beklerken yavaşla
        try:
            resp = SESSION.get(host + path, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_err = exc
            continue
        w = resp.headers.get("X-MBX-USED-WEIGHT-1M") or resp.headers.get("x-mbx-used-weight-1m")
        if w and w.isdigit():
            _last_weight[host] = int(w)
        if resp.status_code in (418, 429, 451):
            secs = _retry_after(resp)
            with _lock:
                _cooldown[host] = time.time() + secs
                _cooldown_code[host] = resp.status_code
            logger.warning("Binance %s döndü (%s), %s boyunca bu adrese istek gönderilmeyecek.",
                           resp.status_code, host, _human(secs))
            waits.append(secs)
            last_err = resp.status_code
            continue
        resp.raise_for_status()
        return resp.json()

    if waits:
        wait = min(waits)
        codes = set(_cooldown_code.get(h) for h in hosts)
        if 418 in codes:
            why = "sunucunun IP adresini geçici olarak engelledi (HTTP 418, aşırı istek)"
        elif 451 in codes:
            why = "bu sunucu konumundan erişime izin vermiyor (HTTP 451)"
        else:
            why = "istek sınırına ulaşıldı (HTTP 429)"
        raise BinanceUnavailable(
            f"Binance {why}. Yaklaşık {_human(wait)} sonra tekrar deneyin.",
            retry_after=int(wait), code=next(iter(codes - {None}), None))
    raise BinanceUnavailable(f"Binance'e bağlanılamadı: {last_err}")
