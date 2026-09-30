# -*- coding: utf-8 -*-
"""
market_direction.py
--------------------
BTC PİYASA YÖNÜ ANALİZ MODÜLÜ

Dört göstergeyi birlikte değerlendirerek kripto piyasasının genel yönünü
(rejimini) belirler:

1. BTC Fiyatı / BTCUSDT          -> Binance'ten
2. TOTAL  (toplam piyasa değeri)  -> CoinGecko /global
3. TOTAL2 (BTC hariç toplam)      -> TOTAL ve BTC dominance'tan türetilir
4. BTC Dominance (BTC.D)          -> CoinGecko /global

Amaç yalnızca "yükseldi mi düştü mü" göstermek değil; bu dört değeri
birlikte okuyarak piyasa rejimini sınıflandırmaktır: Genel Yükseliş,
Genel Düşüş, Altcoin Sezonu, BTC Öncülüğünde Yükseliş, Alt Kanaması /
BTC'ye Kaçış, Yatay/Kararsız, ya da Karışık Sinyaller.

Not: CoinGecko'nun /global uç noktası yalnızca TOTAL'in kendi 24 saatlik
değişimini verir; TOTAL2'nin ve BTC.D'nin 24s önceki değerleri doğrudan
sunulmaz. Bu yüzden BTC'nin kendi 24s fiyat değişimi (Binance'ten) ile
BTC'nin piyasa değerini 24 saat öncesine geri hesaplayıp, TOTAL2 ve
BTC.D'nin 24s önceki değerlerini matematiksel olarak türetiyoruz. Bu,
BTC arzının 24 saat içinde ihmal edilebilir düzeyde değiştiği (fiyat
değişimi ~ piyasa değeri değişimi) varsayımına dayanır.
"""

import os
import time
import logging
from typing import Dict, Tuple

import requests

from utils import get_ticker_24hr

logger = logging.getLogger("scanner.market_direction")

COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "crypto-pattern-scanner/1.0"})

# Basit bellek-içi önbellek: CoinGecko'nun ücretsiz katmanını sık sık
# yormamak için sonucu kısa süre (60 saniye) saklarız.
_CACHE: Dict = {"data": None, "ts": 0.0}
_CACHE_TTL_SECONDS = 60.0

FLAT_THRESHOLD_PCT = 1.0      # %1 altı hareket "yatay" sayılır
STRONG_DOM_THRESHOLD_PP = 0.3  # 0.3 puan üstü dominance hareketi "anlamlı" sayılır
DIVERGENCE_THRESHOLD_PCT = 1.5  # TOTAL2 ile BTC arasındaki fark bu kadar açılırsa "ayrışma" sayılır


def _fetch_coingecko_global(retries: int = 3, timeout: int = 10) -> Dict:
    """CoinGecko /global uç noktasından TOTAL market cap ve BTC dominance çeker."""
    last_exc = None
    for attempt in range(retries):
        try:
            key = os.environ.get("APP_COINGECKO_API_KEY", "").strip()
            headers = {"x-cg-demo-api-key": key} if key else {}
            resp = _SESSION.get(COINGECKO_GLOBAL_URL, timeout=timeout, headers=headers)
            resp.raise_for_status()
            return resp.json()["data"]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("CoinGecko isteği başarısız (deneme %d/%d): %s", attempt + 1, retries, exc)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"CoinGecko /global isteği başarısız oldu: {last_exc}")


def classify_regime(btc_chg: float, total_chg: float, total2_chg: float,
                     dom_chg_pp: float) -> Tuple[str, str, str]:
    """
    4 göstergenin 24 saatlik değişimine bakarak piyasa rejimini sınıflandırır.
    Döndürür: (rejim_adı, açıklama, sentiment["bull"|"bear"|"neutral"|"mixed"])
    """
    all_flat = (abs(btc_chg) < FLAT_THRESHOLD_PCT and abs(total_chg) < FLAT_THRESHOLD_PCT
                and abs(total2_chg) < FLAT_THRESHOLD_PCT)
    if all_flat:
        return (
            "YATAY / KARARSIZ PİYASA",
            f"BTC %{btc_chg:+.2f}, TOTAL %{total_chg:+.2f}, TOTAL2 %{total2_chg:+.2f} — "
            "hepsi sınırlı hareket ediyor, net bir yön yok.",
            "neutral",
        )

    rising_together = (btc_chg > FLAT_THRESHOLD_PCT and total_chg > FLAT_THRESHOLD_PCT
                        and total2_chg > FLAT_THRESHOLD_PCT)
    falling_together = (btc_chg < -FLAT_THRESHOLD_PCT and total_chg < -FLAT_THRESHOLD_PCT
                         and total2_chg < -FLAT_THRESHOLD_PCT)

    if rising_together:
        if total2_chg > btc_chg + DIVERGENCE_THRESHOLD_PCT and dom_chg_pp < -STRONG_DOM_THRESHOLD_PP:
            return (
                "ALTCOIN SEZONU",
                f"TOTAL2 (%{total2_chg:+.2f}) BTC'den (%{btc_chg:+.2f}) belirgin şekilde daha güçlü "
                f"yükseliyor ve BTC dominansı {dom_chg_pp:+.2f} puan düşüyor — sermaye BTC'den "
                "altcoinlere kayıyor.",
                "bull",
            )
        return (
            "GENEL YÜKSELİŞ (RISK-ON)",
            f"BTC (%{btc_chg:+.2f}), TOTAL (%{total_chg:+.2f}) ve TOTAL2 (%{total2_chg:+.2f}) "
            "birlikte yükseliyor — piyasa genelinde risk iştahı güçlü.",
            "bull",
        )

    if falling_together:
        if total2_chg < btc_chg - DIVERGENCE_THRESHOLD_PCT and dom_chg_pp > STRONG_DOM_THRESHOLD_PP:
            return (
                "ALT KANAMASI / BTC'YE KAÇIŞ",
                f"TOTAL2 (%{total2_chg:+.2f}) BTC'den (%{btc_chg:+.2f}) çok daha sert düşüyor ve "
                f"BTC dominansı {dom_chg_pp:+.2f} puan yükseliyor — altcoinlerden BTC'ye/güvenli "
                "varlıklara kaçış var.",
                "bear",
            )
        return (
            "GENEL DÜŞÜŞ (RISK-OFF)",
            f"BTC (%{btc_chg:+.2f}), TOTAL (%{total_chg:+.2f}) ve TOTAL2 (%{total2_chg:+.2f}) "
            "birlikte düşüyor — piyasa genelinde risk iştahı zayıf.",
            "bear",
        )

    if btc_chg > FLAT_THRESHOLD_PCT and dom_chg_pp > STRONG_DOM_THRESHOLD_PP and total2_chg < btc_chg:
        return (
            "BTC ÖNCÜLÜĞÜNDE YÜKSELİŞ",
            f"BTC yükseliyor (%{btc_chg:+.2f}) ve dominansı artıyor ({dom_chg_pp:+.2f} puan) ama "
            f"TOTAL2 geride kalıyor (%{total2_chg:+.2f}) — sermaye BTC'de yoğunlaşıyor, "
            "altcoinler zayıf performans gösteriyor.",
            "mixed",
        )

    if btc_chg < -FLAT_THRESHOLD_PCT and dom_chg_pp < -STRONG_DOM_THRESHOLD_PP and total2_chg > btc_chg:
        return (
            "BTC ZAYIF, ALTLAR DİRENÇLİ",
            f"BTC düşerken (%{btc_chg:+.2f}) TOTAL2 nispeten daha dirençli (%{total2_chg:+.2f}) ve "
            f"BTC dominansı düşüyor ({dom_chg_pp:+.2f} puan) — altcoin tarafı sınırlı da olsa "
            "daha güçlü.",
            "mixed",
        )

    return (
        "KARIŞIK SİNYALLER",
        f"BTC %{btc_chg:+.2f}, TOTAL %{total_chg:+.2f}, TOTAL2 %{total2_chg:+.2f}, "
        f"BTC.D {dom_chg_pp:+.2f} puan — göstergeler net bir ortak yön vermiyor, temkinli olun.",
        "mixed",
    )


def compute_market_snapshot(use_cache: bool = True) -> Dict:
    """
    4 göstergeyi çeker, TOTAL2 ve BTC.D'nin 24s önceki değerlerini türetir
    ve piyasa rejimini sınıflandırır. Sonucu kısa süreliğine önbelleğe alır.
    """
    now = time.time()
    if use_cache and _CACHE["data"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL_SECONDS:
        return _CACHE["data"]

    g = _fetch_coingecko_global()
    btc_ticker = get_ticker_24hr("BTCUSDT")

    total_now = float(g["total_market_cap"]["usd"])
    btc_dom_now = float(g["market_cap_percentage"]["btc"])
    total_chg = float(g.get("market_cap_change_percentage_24h_usd", 0.0))

    btc_price = float(btc_ticker["lastPrice"])
    btc_chg = float(btc_ticker["priceChangePercent"])

    btc_mcap_now = total_now * btc_dom_now / 100.0
    total2_now = total_now - btc_mcap_now

    # 24 saat öncesine geri hesapla (BTC arzı 24s içinde ~sabit varsayımıyla
    # fiyat değişimi ~ piyasa değeri değişimi kabul edilir)
    btc_ratio_24h = 1 + btc_chg / 100.0
    total_ratio_24h = 1 + total_chg / 100.0

    btc_mcap_24h_ago = btc_mcap_now / btc_ratio_24h if btc_ratio_24h != 0 else btc_mcap_now
    total_24h_ago = total_now / total_ratio_24h if total_ratio_24h != 0 else total_now
    total2_24h_ago = total_24h_ago - btc_mcap_24h_ago

    total2_chg = ((total2_now - total2_24h_ago) / total2_24h_ago * 100.0) if total2_24h_ago else 0.0
    btc_dom_24h_ago = (btc_mcap_24h_ago / total_24h_ago * 100.0) if total_24h_ago else btc_dom_now
    dom_chg_pp = btc_dom_now - btc_dom_24h_ago

    regime, explanation, sentiment = classify_regime(btc_chg, total_chg, total2_chg, dom_chg_pp)

    result = {
        "btc_price": btc_price,
        "btc_chg_24h_pct": round(btc_chg, 2),
        "total_mcap": total_now,
        "total_chg_24h_pct": round(total_chg, 2),
        "total2_mcap": total2_now,
        "total2_chg_24h_pct": round(total2_chg, 2),
        "btc_dominance": round(btc_dom_now, 2),
        "btc_dominance_chg_24h_pp": round(dom_chg_pp, 2),
        "regime": regime,
        "explanation": explanation,
        "sentiment": sentiment,
        "updated_at": int(now),
    }

    _CACHE["data"] = result
    _CACHE["ts"] = now
    return result
