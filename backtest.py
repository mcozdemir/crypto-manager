# -*- coding: utf-8 -*-
"""
backtest.py
-----------
Bir sembol + zaman dilimi için formasyon tespit motorunu GEÇMİŞ veri
üzerinde "walk-forward" (ileriye doğru yürüyerek) çalıştırır: her adımda
yalnızca o ana kadarki mumları kullanarak formasyon arar (geleceğe bakmaz),
formasyon bulunduğunda GERÇEK gelecekteki fiyat hareketiyle hedefe mi
yoksa stop'a mı önce ulaştığını ölçer.

Bu, scanner.py'daki canlı tarama motorundan TAMAMEN AYRI çalışır;
canlı taramada asla gelecek veri kullanılmaz. Backtest sadece geçmişte
"bu formasyon türü gerçekten ne kadar başarılı olmuş" sorusuna cevap
aramak için tasarlanmıştır.
"""

import logging
from typing import List, Dict, Optional, Callable

import pandas as pd

from utils import get_klines
from indicators import add_indicators
from patterns import detect_all_patterns
from scoring import score_pattern
from ayarlar import BACKTEST_MIN_SCORE, BACKTEST_MIN_GAP

logger = logging.getLogger("scanner.backtest")

# Canlı tarayıcı (scanner.py) ile AYNI eşik kullanılmalı. Burada yerel bir
# sabit olarak tutuluyor (scanner.py'yi import etmek charts/matplotlib gibi
# gereksiz ağır bağımlılıkları da tetikler); iki dosyadaki değer senkron
# tutulmalıdır.
MIN_SCORE = BACKTEST_MIN_SCORE

# Aynı formasyon türü art arda birçok adımda tekrar tespit edilebilir
# (pencere kaydıkça aynı yapı hâlâ görünür kalabilir). Bunu tekrar tekrar
# "yeni sinyal" saymamak için, aynı (pattern, direction) için yeni bir
# işlem açmadan önce en az bu kadar mum geçmesini bekleriz.
MIN_GAP_BETWEEN_SIGNALS = BACKTEST_MIN_GAP


def _simulate_outcome(future: pd.DataFrame, direction: str, target: float, stop: float):
    """
    Formasyon tespit edildikten SONRAKİ mumları tarayarak hedefin mi
    stop'un mu önce vurulduğunu bulur. Aynı mumda ikisi de mümkünse
    (yüksek oynaklık) ihtiyatlı davranılır ve STOP önce vurulmuş kabul
    edilir (gerçekçi/muhafazakâr varsayım).
    """
    for idx, row in future.iterrows():
        if direction == "LONG":
            if row["low"] <= stop:
                return "STOP", idx, stop
            if row["high"] >= target:
                return "HEDEF", idx, target
        else:
            if row["high"] >= stop:
                return "STOP", idx, stop
            if row["low"] <= target:
                return "HEDEF", idx, target
    return "AÇIK", None, None


def run_backtest(symbol: str, timeframe: str, total_candles: int = 1000,
                  window: int = 500, step: int = 4, max_hold_candles: int = 80,
                  progress_cb: Optional[Callable[[dict], None]] = None) -> Dict:
    """
    Belirtilen sembol/zaman dilimi için walk-forward backtest çalıştırır.

    total_candles: Binance'ten çekilecek toplam mum sayısı (Binance limiti 1000).
    window: her adımda formasyon aramak için kullanılan geçmiş pencere genişliği
            (canlı tarayıcıdaki KLINE_LIMIT=500 ile aynı, tutarlılık için).
    step: pencere her adımda kaç mum kaydırılarak ilerlenecek (performans için >1).
    max_hold_candles: bir sinyalin hedef/stop için en fazla kaç mum beklenecek
                       (bu süre içinde hiçbiri vurulmazsa 'AÇIK/belirsiz' sayılır).
    """
    def _progress(current, total, message):
        if progress_cb:
            try:
                progress_cb({"current": current, "total": total, "message": message})
            except Exception:  # noqa: BLE001
                pass

    _progress(0, 1, f"{symbol} ({timeframe}) için geçmiş veri indiriliyor...")
    df_full = get_klines(symbol, timeframe, limit=total_candles)
    if len(df_full) < window + 50:
        raise ValueError("Backtest için yeterli geçmiş veri yok (sembol çok yeni olabilir).")

    df_full = add_indicators(df_full)

    start_i = window
    end_i = len(df_full) - 1
    total_steps = max(1, (end_i - start_i) // step)

    trades: List[Dict] = []
    last_signal_idx: Dict[str, int] = {}  # "pattern|direction" -> son sinyal indeksi

    step_count = 0
    for i in range(start_i, end_i, step):
        step_count += 1
        if step_count % 10 == 0:
            _progress(step_count, total_steps, f"{symbol} taranıyor... ({step_count}/{total_steps})")

        sub = df_full.iloc[i - window:i + 1].reset_index(drop=True)
        try:
            found = detect_all_patterns(sub)
        except Exception:  # noqa: BLE001
            continue

        # BUG FİX: Canlı tarayıcı (scanner.py) yalnızca skoru >= MIN_SCORE (60)
        # olan formasyonları kullanıcıya gösterir. Backtest bu filtreyi
        # uygulamıyordu ve detect_all_patterns()'ın bulduğu TÜM formasyonları
        # (zayıf/düşük kaliteli olanlar dahil) "sinyal" sayıyordu. Bu yüzden
        # backtest sonuçları, aracın gerçekte gösterdiği sinyallerin başarı
        # oranını değil, ham/filtrelenmemiş geometrinin başarı oranını
        # yansıtıyordu — genelde olduğundan daha kötü görünüyordu.
        candidates = []
        for p in found:
            score_info = score_pattern(sub, p)
            if score_info["total_score"] < MIN_SCORE:
                continue
            candidates.append((p, score_info))

        # BUG FİX: Canlı tarayıcıdaki çelişkili-yön koruması (aynı coin+TF'de
        # hem LONG hem SHORT formasyon eşiği geçerse yalnızca en yüksek
        # skorlu yön tutulur) backtest'e de uygulanmalı, aksi hâlde aynı
        # anlık pencerede birbirine zıt iki "sinyal" ayrı ayrı sayılır.
        directions_present = {p["direction"] for p, _ in candidates}
        if len(directions_present) > 1:
            best_direction = max(
                directions_present,
                key=lambda d: max(s["total_score"] for p, s in candidates if p["direction"] == d)
            )
            candidates = [(p, s) for p, s in candidates if p["direction"] == best_direction]

        for p, score_info in candidates:
            key = f"{p['name']}|{p['direction']}"
            last_idx = last_signal_idx.get(key, -10_000)
            if i - last_idx < MIN_GAP_BETWEEN_SIGNALS:
                continue  # aynı formasyon çok yakın zamanda zaten sayıldı
            last_signal_idx[key] = i

            entry_price = float(sub["close"].iloc[-1])
            future = df_full.iloc[i + 1: i + 1 + max_hold_candles]
            outcome, exit_idx, exit_price = _simulate_outcome(
                future, p["direction"], p["target"], p["stop_loss"])

            bars_to_resolve = int(exit_idx - i) if exit_idx is not None else None
            if outcome == "HEDEF":
                pnl_pct = (exit_price - entry_price) / entry_price * 100
            elif outcome == "STOP":
                pnl_pct = (exit_price - entry_price) / entry_price * 100
            else:
                pnl_pct = None
            if p["direction"] == "SHORT" and pnl_pct is not None:
                pnl_pct = -pnl_pct

            trades.append({
                "entry_time": str(sub["open_time"].iloc[-1]),
                "pattern": p["name"],
                "direction": p["direction"],
                "entry_price": entry_price,
                "target": float(p["target"]),
                "stop_loss": float(p["stop_loss"]),
                "score": score_info["total_score"],
                "outcome": outcome,
                "bars_to_resolve": bars_to_resolve,
                "pnl_pct": round(float(pnl_pct), 2) if pnl_pct is not None else None,
            })

    _progress(total_steps, total_steps, "Backtest tamamlandı, özet hesaplanıyor...")
    summary = summarize_trades(trades)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "total_candles_used": len(df_full),
        "trades": trades,
        "summary": summary,
    }


def summarize_trades(trades: List[Dict]) -> List[Dict]:
    """Her formasyon türü + yön kombinasyonu için özet istatistik üretir."""
    groups: Dict[str, List[Dict]] = {}
    for t in trades:
        key = f"{t['pattern']}|{t['direction']}"
        groups.setdefault(key, []).append(t)

    summary = []
    for key, items in groups.items():
        pattern, direction = key.split("|")
        resolved = [t for t in items if t["outcome"] in ("HEDEF", "STOP")]
        wins = [t for t in resolved if t["outcome"] == "HEDEF"]
        open_trades = [t for t in items if t["outcome"] == "AÇIK"]
        avg_pnl = (sum(t["pnl_pct"] for t in resolved) / len(resolved)) if resolved else None
        avg_bars = (sum(t["bars_to_resolve"] for t in resolved) / len(resolved)) if resolved else None

        summary.append({
            "pattern": pattern,
            "direction": direction,
            "toplam_sinyal": len(items),
            "çözümlenen": len(resolved),
            "hedef": len(wins),
            "stop": len(resolved) - len(wins),
            "açık_belirsiz": len(open_trades),
            "başarı_oranı": round(len(wins) / len(resolved) * 100, 1) if resolved else None,
            "ortalama_getiri_pct": round(avg_pnl, 2) if avg_pnl is not None else None,
            "ortalama_mum_sayısı": round(avg_bars, 1) if avg_bars is not None else None,
        })

    summary.sort(key=lambda s: (s["başarı_oranı"] is None, -(s["başarı_oranı"] or 0)))
    return summary
