# -*- coding: utf-8 -*-
"""
confidence.py
-------------
Formasyon skoruna ek olarak her sinyal için "güven endeksleri" hesaplar.

TÜM PUANLAR SİNYALİN YÖNÜNE GÖREDİR (0-100):
  100'e yakın  -> veri bu sinyalin yönünü (LONG veya SHORT) DESTEKLİYOR
  50 civarı    -> nötr
  0'a yakın    -> veri sinyalin yönüne TERS
Aynı veri LONG sinyali için düşük, SHORT sinyali için yüksek puan üretir
(ör. piyasa düşüşteyse). Amaç coinin yükselmesini değil, hareketin yönünü
doğru tahmin etmektir.

Endeksler:
  - Geçmiş başarı       : Aynı formasyon/zaman dilimi/yönün sinyal
                          günlüğündeki gerçek hedef oranı (%, örnek sayısıyla)
  - Piyasa Yönü (şimdi) : TOTAL / TOTAL2 / BTC 24s değişimi + BTC günlük trendi
  - Crypto Manager      : Zaman dilimi uyumu + göreli güç (BTC'ye karşı) +
                          likidite + türev piyasa (fonlama, açık pozisyon,
                          long/short oranı) birleşimi
  - Temel Analiz        : fundamentals.py (şeffaflık, geliştirme, token
                          ekonomisi, olgunluk, DeFi metrikleri)
  - Risk/Ödül           : Hedef mesafesi / stop mesafesi

Veri alınamayan bileşen (ör. coinin vadeli işlem piyasası yoksa) hesaba
katılmaz; kalan bileşenlerin ağırlıkları yeniden dağıtılır.
"""

import logging
import math
import time
from typing import Dict, List, Optional

import numpy as np
import requests

logger = logging.getLogger("scanner.confidence")

FAPI_URL = "https://fapi.binance.com"
SPOT_URL = "https://api.binance.com"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "crypto-manager/1.0"})

MIN_HISTORY_SAMPLES = 10  # geçmiş başarı için gereken en az sonuçlanmış sinyal

# Crypto Manager bileşen ağırlıkları, sinyalin zaman dilimine göre.
# Kısa vadede (1H) likidite ve türev piyasa daha belirleyici; uzun vadede
# (1D) trend uyumu ve göreli güç ağır basar.
CM_WEIGHTS = {
    "1h": {"tf_align": 0.25, "rel_strength": 0.15, "liquidity": 0.25, "derivatives": 0.35},
    "4h": {"tf_align": 0.30, "rel_strength": 0.25, "liquidity": 0.15, "derivatives": 0.30},
    "1d": {"tf_align": 0.30, "rel_strength": 0.35, "liquidity": 0.10, "derivatives": 0.25},
}
TF_ALIGN_WEIGHTS = {"1h": 0.2, "4h": 0.35, "1d": 0.45}
TF_LABEL = {"1h": "1H", "4h": "4H", "1d": "1D"}


# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------
def tr_pct(v: float, digits: int = 1) -> str:
    """Türkçe yüzde gösterimi: +%1,2 / −%0,5"""
    sign = "+" if v > 0 else "−" if v < 0 else ""
    return f"{sign}%{abs(v):.{digits}f}".replace(".", ",")


def _clip(v: float) -> float:
    return float(max(0.0, min(100.0, v)))


def _for_direction(long_score: Optional[float], direction: str) -> Optional[float]:
    """LONG açısından hesaplanmış puanı sinyal yönüne çevirir."""
    if long_score is None:
        return None
    return _clip(long_score if direction == "LONG" else 100.0 - long_score)


def verdict(score: Optional[float]) -> str:
    if score is None:
        return "veri yok"
    if score >= 65:
        return "destekliyor"
    if score >= 45:
        return "nötr"
    return "ters"


def _get(url: str, params: dict = None, timeout: float = 8.0):
    resp = _SESSION.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _pct(a: float, b: float) -> Optional[float]:
    if not b:
        return None
    return (a / b - 1.0) * 100.0


def _trend_state(df) -> Optional[int]:
    """Son muma göre trend: +1 yukarı, -1 aşağı, 0 kararsız."""
    if df is None or len(df) == 0:
        return None
    row = df.iloc[-1]
    close, ema20, ema50 = row.get("close"), row.get("ema20"), row.get("ema50")
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in (close, ema20, ema50)):
        return None
    if close > ema50 and ema20 > ema50:
        return 1
    if close < ema50 and ema20 < ema50:
        return -1
    return 0


def _returns(df_1d) -> Dict[str, Optional[float]]:
    out = {"r7": None, "r30": None}
    if df_1d is None or len(df_1d) < 8:
        return out
    closes = df_1d["close"].astype(float).values
    out["r7"] = _pct(closes[-1], closes[-8])
    if len(closes) >= 31:
        out["r30"] = _pct(closes[-1], closes[-31])
    return out


# --------------------------------------------------------------------------
# Tarama bağlamı: tarama başına bir kez çekilen toplu veriler
# --------------------------------------------------------------------------
class ScanContext:
    """
    Bir tarama boyunca paylaşılan veriler (her coin için tekrar tekrar
    istek atmamak için toplu uç noktalardan bir kez çekilir).
    """

    def __init__(self, timeframes_scanned: Optional[List[str]] = None):
        self.timeframes_scanned = timeframes_scanned or []
        self.tickers: Dict[str, Dict] = {}
        self.books: Dict[str, Dict] = {}
        self.funding: Dict[str, float] = {}
        self.fapi_ok = True
        self.market: Optional[Dict] = None
        self.btc_1d = None
        self.btc_trend: Optional[int] = None
        self.history: Dict = {}
        self._deriv_cache: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        try:
            for t in _get(SPOT_URL + "/api/v3/ticker/24hr"):
                self.tickers[t["symbol"]] = t
        except Exception as exc:  # noqa: BLE001
            logger.warning("24s ticker verisi alınamadı: %s", exc)
        try:
            for b in _get(SPOT_URL + "/api/v3/ticker/bookTicker"):
                self.books[b["symbol"]] = b
        except Exception as exc:  # noqa: BLE001
            logger.warning("Emir defteri (bookTicker) alınamadı: %s", exc)
        try:
            for p in _get(FAPI_URL + "/fapi/v1/premiumIndex"):
                try:
                    self.funding[p["symbol"]] = float(p.get("lastFundingRate") or 0.0)
                except (TypeError, ValueError):
                    pass
        except Exception as exc:  # noqa: BLE001
            self.fapi_ok = False
            logger.warning("Vadeli işlem (futures) verisi alınamadı, türev bileşeni atlanacak: %s", exc)
        try:
            from market_direction import compute_market_snapshot
            self.market = compute_market_snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Piyasa yönü verisi alınamadı: %s", exc)
        try:
            from utils import get_klines
            from indicators import add_indicators
            self.btc_1d = add_indicators(get_klines("BTCUSDT", "1d", limit=250))
            self.btc_trend = _trend_state(self.btc_1d)
        except Exception as exc:  # noqa: BLE001
            logger.warning("BTC günlük verisi alınamadı: %s", exc)
        try:
            self.history = load_history_stats()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Geçmiş başarı istatistikleri alınamadı: %s", exc)

    def derivatives(self, symbol: str) -> Dict:
        """Coin başına açık pozisyon ve long/short oranı (önbellekli)."""
        if symbol in self._deriv_cache:
            return self._deriv_cache[symbol]
        data: Dict = {"funding": self.funding.get(symbol)}
        if self.fapi_ok and symbol in self.funding:
            try:
                oi = _get(FAPI_URL + "/futures/data/openInterestHist",
                          {"symbol": symbol, "period": "1h", "limit": 25}, timeout=6)
                if len(oi) >= 2:
                    first = float(oi[0]["sumOpenInterestValue"])
                    last = float(oi[-1]["sumOpenInterestValue"])
                    data["oi_chg_24h"] = _pct(last, first)
            except Exception as exc:  # noqa: BLE001
                logger.debug("OI alınamadı %s: %s", symbol, exc)
            try:
                ls = _get(FAPI_URL + "/futures/data/globalLongShortAccountRatio",
                          {"symbol": symbol, "period": "1h", "limit": 1}, timeout=6)
                if ls:
                    data["ls_ratio"] = float(ls[-1]["longShortRatio"])
            except Exception as exc:  # noqa: BLE001
                logger.debug("Long/short oranı alınamadı %s: %s", symbol, exc)
            time.sleep(0.05)
        self._deriv_cache[symbol] = data
        return data


# --------------------------------------------------------------------------
# Geçmiş başarı (sinyal günlüğünden)
# --------------------------------------------------------------------------
def load_history_stats() -> Dict:
    """Sonuçlanmış sinyallerin formasyon/zaman dilimi/yön bazında hedef-stop sayıları."""
    from db import get_db
    import journal
    journal.init_db()
    rows = get_db().execute("""
        SELECT pattern, timeframe, direction,
               SUM(CASE WHEN status='HEDEF' THEN 1 ELSE 0 END) AS hedef,
               SUM(CASE WHEN status='STOP'  THEN 1 ELSE 0 END) AS stop
        FROM signals WHERE status IN ('HEDEF', 'STOP')
        GROUP BY pattern, timeframe, direction
    """)
    exact: Dict = {}
    by_pattern: Dict = {}
    for r in rows:
        h, s = int(r["hedef"] or 0), int(r["stop"] or 0)
        exact[(r["pattern"], r["timeframe"], r["direction"])] = (h, s)
        ph, ps = by_pattern.get((r["pattern"], r["direction"]), (0, 0))
        by_pattern[(r["pattern"], r["direction"])] = (ph + h, ps + s)
    return {"exact": exact, "pattern": by_pattern}


def history_for(stats: Dict, pattern: str, timeframe: str, direction: str) -> Dict:
    tf = TF_LABEL.get(timeframe, timeframe)
    h, s = stats.get("exact", {}).get((pattern, timeframe, direction), (0, 0))
    if h + s >= MIN_HISTORY_SAMPLES:
        return {"rate": round(h / (h + s) * 100, 1), "n": h + s, "hedef": h, "stop": s,
                "scope": f"{pattern} · {tf} · {direction}"}
    ph, ps = stats.get("pattern", {}).get((pattern, direction), (0, 0))
    if ph + ps >= MIN_HISTORY_SAMPLES:
        return {"rate": round(ph / (ph + ps) * 100, 1), "n": ph + ps, "hedef": ph, "stop": ps,
                "scope": f"{pattern} · tüm zaman dilimleri · {direction}"}
    return {"rate": None, "n": max(h + s, ph + ps), "hedef": None, "stop": None,
            "scope": f"{pattern} · {direction}"}


# --------------------------------------------------------------------------
# Piyasa yönü (şimdi)
# --------------------------------------------------------------------------
def market_now(ctx: ScanContext, symbol: str, direction: str) -> Dict:
    m = ctx.market
    parts, notes = [], []
    if m:
        if symbol == "BTCUSDT":
            chg = m.get("btc_chg_24h_pct", 0.0)
            notes.append(f"BTC 24s {tr_pct(chg, 2)}")
        else:
            chg = 0.6 * m.get("total2_chg_24h_pct", 0.0) + 0.4 * m.get("total_chg_24h_pct", 0.0)
            notes.append(f"TOTAL2 24s {tr_pct(m.get('total2_chg_24h_pct', 0), 2)}, "
                         f"TOTAL 24s {tr_pct(m.get('total_chg_24h_pct', 0), 2)}")
        parts.append((0.6, 50 + 50 * math.tanh(chg / 4.0)))
    if ctx.btc_trend is not None:
        parts.append((0.4, 50 + 40 * ctx.btc_trend))
        notes.append("BTC günlük trend " + {1: "yukarı", -1: "aşağı", 0: "kararsız"}[ctx.btc_trend])
    if not parts:
        return {"score": None, "verdict": verdict(None), "regime": None, "notes": ["Piyasa verisi alınamadı"]}
    long_score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    score = _for_direction(long_score, direction)
    return {
        "score": round(score),
        "verdict": verdict(score),
        "regime": m.get("regime") if m else None,
        "notes": notes,
    }


# --------------------------------------------------------------------------
# Crypto Manager bileşenleri
# --------------------------------------------------------------------------
def _tf_alignment(trends: Dict[str, Optional[int]], direction: str) -> Dict:
    sign = 1 if direction == "LONG" else -1
    num = den = 0.0
    note_parts = []
    for tf in ("1d", "4h", "1h"):
        st = trends.get(tf)
        if st is None:
            continue
        w = TF_ALIGN_WEIGHTS[tf]
        num += w * st * sign
        den += w
        note_parts.append(f"{TF_LABEL[tf]} {'↑' if st > 0 else '↓' if st < 0 else '→'}")
    if den == 0:
        return {"score": None, "note": "Trend verisi yok"}
    score = _clip(50 + 50 * num / den)
    return {"score": round(score), "note": "Trend: " + ", ".join(note_parts)}


def _relative_strength(coin_ret: Dict, btc_ret: Dict, symbol: str, direction: str) -> Dict:
    if symbol == "BTCUSDT":
        return {"score": None, "note": "BTC referans alındığı için hesaplanmaz"}
    if coin_ret.get("r7") is None or btc_ret.get("r7") is None:
        return {"score": None, "note": "Günlük veri yetersiz"}
    d7 = coin_ret["r7"] - btc_ret["r7"]
    combined = d7
    note = f"7 gün BTC'ye göre {tr_pct(d7)}"
    if coin_ret.get("r30") is not None and btc_ret.get("r30") is not None:
        d30 = coin_ret["r30"] - btc_ret["r30"]
        combined = 0.6 * d7 + 0.4 * d30
        note += f", 30 gün {tr_pct(d30)}"
    long_score = 50 + 50 * math.tanh(combined / 15.0)
    return {"score": round(_for_direction(long_score, direction)), "note": note}


def _liquidity(ctx: ScanContext, symbol: str) -> Dict:
    """Yönden bağımsız kalite puanı: sığ piyasada formasyonlar kolay bozulur."""
    t = ctx.tickers.get(symbol)
    b = ctx.books.get(symbol)
    parts, notes = [], []
    if t:
        try:
            qv = float(t.get("quoteVolume") or 0)
            if qv > 0:
                parts.append((0.6, float(np.interp(math.log10(qv), [5, 6, 7, 8, 9], [0, 20, 50, 80, 100]))))
                notes.append(f"24s hacim {qv / 1e6:,.1f} milyon $".replace(",", "X").replace(".", ",").replace("X", "."))
        except (TypeError, ValueError):
            pass
    if b:
        try:
            bid, ask = float(b["bidPrice"]), float(b["askPrice"])
            if bid > 0 and ask > 0:
                bps = (ask - bid) / ((ask + bid) / 2) * 10000
                parts.append((0.4, float(np.interp(bps, [1, 5, 20, 50], [100, 85, 40, 0]))))
                notes.append(f"alış-satış farkı %{bps / 100:.3f}".replace(".", ","))
        except (TypeError, ValueError, KeyError):
            pass
    if not parts:
        return {"score": None, "note": "Likidite verisi yok"}
    score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    return {"score": round(score), "note": ", ".join(notes)}


def _derivatives(ctx: ScanContext, symbol: str, direction: str) -> Dict:
    if not ctx.fapi_ok:
        return {"score": None, "note": "Vadeli işlem verisine erişilemedi"}
    d = ctx.derivatives(symbol)
    if d.get("funding") is None:
        return {"score": None, "note": "Bu coinin vadeli işlem piyasası yok"}
    parts, notes = [], []
    # Fonlama: aşırı pozitif = kalabalık long (SHORT lehine), negatif = kalabalık short
    f_bps = d["funding"] * 10000
    parts.append((0.40, 50 - 50 * math.tanh((f_bps - 1.0) / 5.0)))
    notes.append(f"fonlama (8 saatlik) {tr_pct(d['funding'] * 100, 3)}")
    if d.get("ls_ratio") is not None:
        r = d["ls_ratio"]
        parts.append((0.25, 50 - 50 * math.tanh((r - 1.2) / 1.0)))
        notes.append(f"long/short hesap oranı {r:.2f}".replace(".", ","))
    if d.get("oi_chg_24h") is not None:
        t = ctx.tickers.get(symbol) or {}
        try:
            price_chg = float(t.get("priceChangePercent") or 0.0)
        except (TypeError, ValueError):
            price_chg = 0.0
        oi = d["oi_chg_24h"]
        strength = 1.0 if abs(price_chg) > 0.5 else 0.3
        sign = 1 if price_chg >= 0 else -1
        parts.append((0.35, 50 + 50 * math.tanh(sign * oi / 10.0) * strength))
        notes.append(f"açık pozisyon 24s {tr_pct(oi)}")
    long_score = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    return {"score": round(_for_direction(long_score, direction)), "note": ", ".join(notes)}


CM_NAMES = {
    "tf_align": "Zaman dilimi uyumu",
    "rel_strength": "Göreli güç (BTC'ye karşı)",
    "liquidity": "Likidite",
    "derivatives": "Türev piyasa",
}


def crypto_manager(ctx: ScanContext, symbol: str, timeframe: str, direction: str,
                   trends: Dict, coin_ret: Dict) -> Dict:
    btc_ret = _returns(ctx.btc_1d)
    comps = {
        "tf_align": _tf_alignment(trends, direction),
        "rel_strength": _relative_strength(coin_ret, btc_ret, symbol, direction),
        "liquidity": _liquidity(ctx, symbol),
        "derivatives": _derivatives(ctx, symbol, direction),
    }
    weights = CM_WEIGHTS.get(timeframe, CM_WEIGHTS["4h"])
    num = den = 0.0
    components = []
    for key, c in comps.items():
        w = weights[key]
        if c["score"] is not None:
            num += w * c["score"]
            den += w
        if key == "liquidity" and c["score"] is not None:
            # Likidite yönden bağımsızdır: "destekliyor/ters" yerine kalite etiketi
            v = "iyi" if c["score"] >= 65 else "orta" if c["score"] >= 45 else "zayıf"
        else:
            v = verdict(c["score"])
        components.append({"key": key, "name": CM_NAMES[key], "score": c["score"],
                           "verdict": v, "weight": w, "note": c["note"]})
    score = round(num / den) if den else None
    return {"score": score, "verdict": verdict(score), "components": components}


# --------------------------------------------------------------------------
# Risk / ödül
# --------------------------------------------------------------------------
def risk_reward(entry: Optional[float], target: Optional[float], stop: Optional[float]) -> Dict:
    if not entry or target is None or stop is None:
        return {"rr": None, "target_pct": None, "stop_pct": None}
    reward = abs(target - entry)
    risk = abs(entry - stop)
    return {
        "rr": round(reward / risk, 2) if risk > 0 else None,
        "target_pct": round(_pct(target, entry), 2),
        "stop_pct": round(_pct(stop, entry), 2),
    }


# --------------------------------------------------------------------------
# Ana giriş noktası
# --------------------------------------------------------------------------
def symbol_features(dfs_by_tf: Dict) -> Dict:
    """Bir coinin tarama sırasında elde edilen verilerinden özet çıkarır."""
    trends = {tf: _trend_state(df) for tf, df in dfs_by_tf.items()}
    df_1d = dfs_by_tf.get("1d")
    return {"trends": trends, "returns": _returns(df_1d)}


def ensure_daily(symbol: str, features: Dict) -> Dict:
    """1D taranmadıysa göreli güç ve trend için günlük veriyi ayrıca çeker."""
    if features.get("returns", {}).get("r7") is not None and "1d" in features.get("trends", {}):
        return features
    try:
        from utils import get_klines
        from indicators import add_indicators
        df = add_indicators(get_klines(symbol, "1d", limit=250))
        features.setdefault("trends", {})["1d"] = _trend_state(df)
        features["returns"] = _returns(df)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Günlük veri alınamadı %s: %s", symbol, exc)
    return features


def enrich(results: List[Dict], ctx: ScanContext, features_by_symbol: Dict[str, Dict]) -> List[Dict]:
    """Her sonuca güven endekslerini ekler (yerinde günceller)."""
    import fundamentals
    fund_data: Dict[str, Dict] = {}
    try:
        fund_data = fundamentals.collect(sorted({r["symbol"] for r in results}))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Temel analiz verisi alınamadı: %s", exc)
    for r in results:
        try:
            feats = features_by_symbol.get(r["symbol"], {"trends": {}, "returns": {}})
            hist = history_for(ctx.history, r["pattern"], r["timeframe"], r["direction"])
            market = market_now(ctx, r["symbol"], r["direction"])
            cm = crypto_manager(ctx, r["symbol"], r["timeframe"], r["direction"],
                                feats.get("trends", {}), feats.get("returns", {}))
            rr = risk_reward(r.get("last_price"), r.get("target"), r.get("stop_loss"))
            fund = fundamentals.score(fund_data.get(fundamentals.base_asset(r["symbol"])), r["direction"])
            r["confidence"] = {"history": hist, "market": market, "cm": cm, **rr,
                               "fundamental": fund, "market_week": None}
            r["fundamental_score"] = fund["score"]
            r["hist_rate"] = hist["rate"]
            r["hist_n"] = hist["n"]
            r["market_score"] = market["score"]
            r["cm_score"] = cm["score"]
            r["rr"] = rr["rr"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Güven endeksi hesaplanamadı %s %s: %s", r.get("symbol"), r.get("pattern"), exc)
    return results
