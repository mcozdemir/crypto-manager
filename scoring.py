# -*- coding: utf-8 -*-
"""
scoring.py
----------
Bulunan her formasyon için 100 üzerinden puan ve başarı olasılığı (%)
hesaplar.

Puan dağılımı:
    Geometri      30
    Hacim         20
    Breakout      20
    Trend uyumu   15
    ATR uygunluğu 10
    Momentum       5
    ------------------
    Toplam       100
"""

from typing import Dict
import numpy as np
import pandas as pd


def _volume_score(df: pd.DataFrame, pattern: Dict) -> float:
    breakout_row = pattern.get("breakout_row")
    avg_vol = df["volume"].rolling(20, min_periods=5).mean().iloc[-1]
    if breakout_row is None or pd.isna(avg_vol) or avg_vol <= 0:
        return 5.0
    ratio = breakout_row["volume"] / avg_vol
    # 1.0x -> 5 puan, 1.3x -> 12 puan, 2x+ -> 20 puan (doğrusal ölçek + tavan)
    score = np.interp(ratio, [0.8, 1.0, 1.3, 2.0, 3.0], [2, 5, 12, 18, 20])
    return float(np.clip(score, 0, 20))


def _breakout_score(pattern: Dict) -> float:
    score = 0.0
    if pattern.get("breakout_confirmed"):
        score += 10
    if pattern.get("close_confirmed"):
        score += 6
    risk = pattern.get("fake_breakout_risk", "Yüksek")
    if risk == "Düşük":
        score += 4
    elif risk == "Orta":
        score += 2
    return float(np.clip(score, 0, 20))


def _trend_score(row: pd.Series, direction: str) -> float:
    """EMA20/50/200 dizilimi yön ile uyumluysa yüksek puan."""
    ema20, ema50, ema200 = row.get("ema20"), row.get("ema50"), row.get("ema200")
    if any(pd.isna(v) for v in [ema20, ema50, ema200]):
        return 7.5
    if direction == "LONG":
        aligned = ema20 > ema50 > ema200
        partial = ema20 > ema50 or ema50 > ema200
    else:
        aligned = ema20 < ema50 < ema200
        partial = ema20 < ema50 or ema50 < ema200

    adx = row.get("adx14", 0) or 0
    trend_strength_bonus = min(adx / 50 * 5, 5)  # güçlü trend ekstra puan

    if aligned:
        base = 10
    elif partial:
        base = 6
    else:
        base = 2
    return float(np.clip(base + trend_strength_bonus, 0, 15))


def _atr_score(df: pd.DataFrame, row: pd.Series) -> float:
    """Aşırı düşük (yatay/işlemsiz) veya aşırı yüksek (kaotik) volatilite cezalandırılır."""
    atr = row.get("atr14")
    close = row.get("close")
    if pd.isna(atr) or pd.isna(close) or close == 0:
        return 5.0
    atr_pct = atr / close * 100
    # Uygun aralık yaklaşık %0.5 - %6 arası kabul edilir
    if 0.5 <= atr_pct <= 6:
        return 10.0
    if atr_pct < 0.5:
        return float(np.interp(atr_pct, [0, 0.5], [2, 10]))
    return float(np.clip(np.interp(atr_pct, [6, 15], [10, 2]), 2, 10))


def _momentum_score(row: pd.Series, direction: str) -> float:
    rsi = row.get("rsi14", 50)
    macd_hist = row.get("macd_hist", 0)
    score = 0.0
    if direction == "LONG":
        if rsi > 50:
            score += 2.5
        if macd_hist > 0:
            score += 2.5
    else:
        if rsi < 50:
            score += 2.5
        if macd_hist < 0:
            score += 2.5
    return float(np.clip(score, 0, 5))


def _geometry_score(pattern: Dict) -> float:
    quality = pattern.get("geometry_quality", 0.5)
    return float(np.clip(quality * 30, 0, 30))


def score_label(total: float) -> str:
    if total > 85:
        return "Çok Güçlü"
    if total >= 70:
        return "Güçlü"
    if total >= 60:
        return "Orta"
    return "Zayıf"


def compute_success_probability(scores: Dict, pattern: Dict, row: pd.Series) -> float:
    """
    Başarı Olasılığı (%): formasyon kalitesi, breakout kalitesi, hacim,
    trend, momentum, ATR uygunluğu ve volatiliteye dayalı ağırlıklı skor.
    """
    geometry_ratio = scores["geometry"] / 30
    breakout_ratio = scores["breakout"] / 20
    volume_ratio = scores["volume"] / 20
    trend_ratio = scores["trend"] / 15
    atr_ratio = scores["atr"] / 10
    momentum_ratio = scores["momentum"] / 5

    weights = {
        "geometry": 0.25,
        "breakout": 0.22,
        "volume": 0.18,
        "trend": 0.15,
        "atr": 0.10,
        "momentum": 0.10,
    }
    weighted = (
        geometry_ratio * weights["geometry"]
        + breakout_ratio * weights["breakout"]
        + volume_ratio * weights["volume"]
        + trend_ratio * weights["trend"]
        + atr_ratio * weights["atr"]
        + momentum_ratio * weights["momentum"]
    )
    # 40-95 aralığına ölçekle (100% kesinlik iddia etmemek için tavan 95)
    probability = 40 + weighted * 55
    return float(np.clip(probability, 5, 95))


def score_pattern(df: pd.DataFrame, pattern: Dict) -> Dict:
    """Bir formasyon için tüm puan bileşenlerini, toplamı ve güven skorunu hesaplar."""
    row = df.iloc[-1]
    direction = pattern["direction"]

    scores = {
        "geometry": _geometry_score(pattern),
        "volume": _volume_score(df, pattern),
        "breakout": _breakout_score(pattern),
        "trend": _trend_score(row, direction),
        "atr": _atr_score(df, row),
        "momentum": _momentum_score(row, direction),
    }
    total = float(sum(scores.values()))
    label = score_label(total)
    success_prob = compute_success_probability(scores, pattern, row)

    # BUG FİX: "Fake Breakout Riski" yalnızca breakout bileşeninin %20'sini
    # etkiliyordu (toplam skorun ~4 puanı), bu da geometri/trend/hacim iyi
    # olduğunda riskli bir kırılımın hâlâ "Çok Güçlü" etiketiyle ve yüksek
    # başarı olasılığıyla gösterilmesine yol açıyordu (ör. NEARUSDT 1H
    # Double Top: Fake Breakout Riski=Yüksek ama skor=85, "Çok Güçlü").
    # Burada risk seviyesine göre açık bir tavan/ceza uygulanıyor.
    fake_risk = pattern.get("fake_breakout_risk")
    if fake_risk == "Yüksek":
        if label == "Çok Güçlü":
            label = "Güçlü"  # sahte kırılım riski yüksekken en üst etiket verilmez
        success_prob = min(success_prob, 65.0)
    elif fake_risk == "Orta":
        success_prob = min(success_prob, 80.0)

    volume_desc = "Yüksek" if scores["volume"] >= 12 else ("Normal" if scores["volume"] >= 6 else "Düşük")
    if scores["volume"] >= 17:
        volume_desc = "Çok Yüksek"

    return {
        "scores": scores,
        "total_score": round(total, 1),
        "label": label,
        "success_probability": round(success_prob, 1),
        "volume_desc": volume_desc,
    }
