# -*- coding: utf-8 -*-
"""
utils.py
--------
Binance Spot REST API ile iletişim kuran yardımcı fonksiyonlar.
- Hacme göre sıralanmış USDT paritelerini çeker
- Stablecoin paritelerini eler
- Belirtilen zaman diliminde mum (kline) verisi indirir

Not: ccxt yerine doğrudan Binance REST API kullanılmıştır çünkü
tek exchange (Binance) ile çalışıldığı için ekstra soyutlama katmanına
gerek yoktur; bu da daha az bağımlılık ve daha hızlı istek anlamına gelir.
İsteyen kullanıcı ccxt.binance() ile aynı fonksiyonları kolayca değiştirebilir.
"""

import time
import logging
from typing import List, Dict

import requests
import pandas as pd

logger = logging.getLogger("scanner.utils")

BASE_URL = "https://api.binance.com"

# Analiz dışında bırakılacak stablecoin bazlı paritelerin taban varlıkları
# BUG FİX: EURI (Eurite - Euro'ya sabit), RLUSD (Ripple USD), USD1 (World
# Liberty Financial USD) ve U (Binance'in USD-sabit birimi) eksikti; bu
# yüzden EURIUSDT gibi $1'e sabit paritelerde anlamsız formasyonlar
# (gerçekte sadece EUR/USD döviz gürültüsü) tespit ediliyordu.
STABLECOINS = {
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "USDS", "BUSD",
    "EUR", "EURI", "GBP", "TRY", "AEUR", "USDe", "PYUSD",
    "RLUSD", "USD1", "U",
}

# Binance interval string eşlemesi
INTERVAL_MAP = {
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "crypto-pattern-scanner/1.0"})


def _get(endpoint: str, params: dict = None, retries: int = 3, timeout: int = 10):
    """Basit retry mekanizmalı GET isteği."""
    url = f"{BASE_URL}{endpoint}"
    last_exc = None
    for attempt in range(retries):
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("İstek hatası (%s), tekrar deneniyor (%d/%d): %s",
                            endpoint, attempt + 1, retries, exc)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Binance API isteği başarısız oldu: {endpoint} -> {last_exc}")


def get_top_usdt_symbols(limit: int = 50) -> List[str]:
    """
    Binance Spot'ta işlem gören, stablecoin olmayan USDT paritelerini
    24 saatlik quote volume'e (USDT cinsinden işlem hacmi) göre sıralar
    ve ilk `limit` tanesini döndürür.
    """
    logger.info("Binance exchangeInfo çekiliyor...")
    exchange_info = _get("/api/v3/exchangeInfo")

    tradable_usdt = set()
    for s in exchange_info.get("symbols", []):
        if (
            s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("isSpotTradingAllowed", True)
        ):
            base = s.get("baseAsset", "")
            if base in STABLECOINS:
                continue
            # Leveraged token (UP/DOWN/BULL/BEAR) paritelerini de dışla
            if base.endswith(("UP", "DOWN", "BULL", "BEAR")):
                continue
            tradable_usdt.add(s["symbol"])

    logger.info("24 saatlik ticker verisi çekiliyor...")
    tickers = _get("/api/v3/ticker/24hr")

    rows = []
    for t in tickers:
        symbol = t.get("symbol")
        if symbol in tradable_usdt:
            try:
                quote_volume = float(t.get("quoteVolume", 0.0))
            except (TypeError, ValueError):
                quote_volume = 0.0
            rows.append((symbol, quote_volume))

    rows.sort(key=lambda x: x[1], reverse=True)
    top_symbols = [r[0] for r in rows[:limit]]
    logger.info("İlk %d hacimli USDT paritesi belirlendi.", len(top_symbols))
    return top_symbols


def get_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    """
    Belirtilen sembol ve zaman dilimi için OHLCV mum verisini
    pandas DataFrame olarak döndürür.
    """
    if interval not in INTERVAL_MAP:
        raise ValueError(f"Desteklenmeyen zaman dilimi: {interval}")

    params = {
        "symbol": symbol,
        "interval": INTERVAL_MAP[interval],
        "limit": limit,
    }
    raw = _get("/api/v3/klines", params=params)

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)

    numeric_cols = ["open", "high", "low", "close", "volume", "quote_volume"]
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    df = df.dropna(subset=numeric_cols).reset_index(drop=True)
    return df


def safe_request_sleep(seconds: float = 0.15):
    """Binance rate-limit'e takılmamak için istekler arasında küçük bekleme."""
    time.sleep(seconds)


def get_latest_price(symbol: str) -> float:
    """Belirtilen sembol için anlık (son işlem) fiyatı döndürür."""
    data = _get("/api/v3/ticker/price", params={"symbol": symbol})
    return float(data["price"])


def get_latest_prices(symbols: List[str]) -> Dict[str, float]:
    """
    Birden fazla sembol için anlık fiyatları TEK istekte döndürür
    (Binance'in toplu ticker endpoint'i kullanılır; sinyal günlüğünü
    güncellerken her coin için ayrı istek atmamak için).
    """
    data = _get("/api/v3/ticker/price")
    prices = {row["symbol"]: float(row["price"]) for row in data}
    return {s: prices[s] for s in symbols if s in prices}


def get_ticker_24hr(symbol: str) -> Dict:
    """
    Tek bir sembol için 24 saatlik istatistikleri döndürür (BTC Piyasa Yönü
    modülünde BTCUSDT'nin 24s fiyat değişimini almak için kullanılır).
    """
    return _get("/api/v3/ticker/24hr", params={"symbol": symbol})
