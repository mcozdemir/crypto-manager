# -*- coding: utf-8 -*-
"""
patterns.py
-----------
Pivot High/Low tespiti, otomatik trend çizgisi (linear regression) üretimi
ve 10 formasyonun (5 yükseliş + 5 düşüş) geometrik kurallara göre tespiti.

Her pattern fonksiyonu şu yapıda bir sözlük döndürür (bulunamazsa None):
{
    "name": "Bull Flag",
    "direction": "LONG" | "SHORT",
    "points": {...}              -> grafik çizimi için pivot/nokta bilgisi
    "trendlines": [(x1,y1,x2,y2), ...],
    "breakout_level": float,
    "breakout_confirmed": bool,
    "close_confirmed": bool,
    "volume_confirmed": bool,
    "fake_breakout_risk": "Düşük" | "Orta" | "Yüksek",
    "invalidated": bool,         -> son mum formasyonu bozuyor mu
    "target": float,
    "stop_loss": float,
    "geometry_quality": float,   -> 0-1 arası, simetri/uyum kalitesi
}
"""

from typing import List, Tuple, Optional, Dict
import numpy as np
import pandas as pd
from scipy.signal import argrelextrema


# --------------------------------------------------------------------------
# Ortak yardımcı fonksiyonlar
# --------------------------------------------------------------------------

def find_pivots(df: pd.DataFrame, order: int = 5) -> Tuple[List[int], List[int]]:
    """
    Pivot High ve Pivot Low indekslerini scipy.argrelextrema ile bulur.
    `order`: bir pivotun geçerli sayılması için sağında/solunda kaç mum
    daha yüksek/düşük olmaması gerektiği (gürültü filtresi).
    """
    highs = df["high"].values
    lows = df["low"].values

    high_idx = argrelextrema(highs, np.greater_equal, order=order)[0]
    low_idx = argrelextrema(lows, np.less_equal, order=order)[0]

    # Ardışık aynı-değerli platoları teke indir
    high_idx = _dedup_consecutive(high_idx)
    low_idx = _dedup_consecutive(low_idx)
    return list(high_idx), list(low_idx)


def _dedup_consecutive(idx_array, min_gap: int = 3):
    if len(idx_array) == 0:
        return idx_array
    result = [idx_array[0]]
    for i in idx_array[1:]:
        if i - result[-1] >= min_gap:
            result.append(i)
    return np.array(result)


def linreg(x: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
    """Basit doğrusal regresyon: y = slope*x + intercept."""
    if len(x) < 2:
        return 0.0, float(y[-1]) if len(y) else 0.0
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def pct_diff(a: float, b: float) -> float:
    """İki değer arasındaki yüzdesel fark (mutlak)."""
    if b == 0:
        return float("inf")
    return abs(a - b) / abs(b)


def check_breakout(df: pd.DataFrame, level: float, direction: str,
                    lookback: int = 3) -> Dict:
    """
    Son `lookback` mum içinde breakout gerçekleşmiş mi, kapanışla teyit
    edilmiş mi ve hacim artışı var mı kontrol eder.
    """
    recent = df.iloc[-lookback:]
    avg_vol = df["volume"].rolling(20, min_periods=5).mean().iloc[-1]

    breakout_confirmed = False
    close_confirmed = False
    volume_confirmed = False
    breakout_row = None

    for _, row in recent.iterrows():
        if direction == "LONG" and row["close"] > level:
            breakout_confirmed = True
            close_confirmed = True
            breakout_row = row
        elif direction == "SHORT" and row["close"] < level:
            breakout_confirmed = True
            close_confirmed = True
            breakout_row = row

    if breakout_row is not None and not np.isnan(avg_vol) and avg_vol > 0:
        volume_confirmed = breakout_row["volume"] > 1.3 * avg_vol

    return {
        "breakout_confirmed": breakout_confirmed,
        "close_confirmed": close_confirmed,
        "volume_confirmed": volume_confirmed,
        "breakout_row": breakout_row,
    }


def last_candle_invalidates(df: pd.DataFrame, level: float, direction: str) -> bool:
    """Son mum kapanışı formasyon seviyesinin tekrar yanlış tarafına geçtiyse True."""
    last_close = df["close"].iloc[-1]
    if direction == "LONG":
        return last_close < level * 0.995
    else:
        return last_close > level * 1.005


MIN_STOP_DISTANCE_ATR_MULT = 0.5  # stop, breakout'a ATR'nin bu katından daha yakın olamaz


def passes_final_sanity_check(df: pd.DataFrame, pattern: Dict) -> bool:
    """
    BUG FİX: check_breakout() yalnızca son birkaç mumun kapanışına bakar;
    fiyatın seviyeyi ÇOK ÖNCE geçip geçmediğini kontrol etmez. Trend uzun
    süredir devam ediyorsa (ör. son pivot dip/tepe aylar önce oluşmuşsa)
    sistem eski/bayat bir kırılımı "yeni kırılım" sanabiliyordu (ör.
    KAITOUSDT 1D: neckline 0.50 iken güncel fiyat zaten 1.13'tü, yani
    hedef %86 aşılmıştı ama formasyon hâlâ "aktif fırsat" gösteriliyordu).

    Bu fonksiyon geometri sırasını (stop/breakout/hedef) ve güncel fiyatın
    hedefi/stopu ÇOKTAN geçip geçmediğini kontrol eder; geçmişse formasyon
    reddedilir. Bu, hem stale/bayat formasyonları hem de kama/üçgen
    ekstrapolasyonundan kaynaklanan tutarsız geometrileri (ör. LONG bir
    formasyonda stop'un breakout seviyesinin üstünde çıkması) yakalar.

    BUG FİX 2 (v6): Ayrıca stop'un breakout seviyesine göre ATR cinsinden
    yeterince UZAK olduğunu doğrular. Yakınsayan kama (wedge) ve üçgen
    (triangle) formasyonlarında üst ve alt trend çizgileri birbirine
    yaklaştığı için, ham pivot noktası tampon eklenmeden stop olarak
    kullanılınca stop breakout'a neredeyse yapışık çıkabiliyordu (ör.
    LTCUSDT 1H Falling Wedge: stop, breakout'un yalnızca %0.26 altındaydı
    — sıradan bir fitil bile anında stop'u tetikleyebilirdi). Bu tür aşırı
    dar/kırılgan kurulumlar artık reddediliyor.
    """
    direction = pattern.get("direction")
    bl = pattern.get("breakout_level")
    tgt = pattern.get("target")
    stop = pattern.get("stop_loss")
    if bl is None or tgt is None or stop is None:
        return False

    last_close = df["close"].iloc[-1]

    if direction == "LONG":
        if not (stop < bl < tgt):
            return False
        if last_close >= tgt:      # hedef zaten geçilmiş -> bayat sinyal
            return False
        if last_close <= stop:     # stop zaten kırılmış -> geçersiz
            return False
    else:
        if not (stop > bl > tgt):
            return False
        if last_close <= tgt:
            return False
        if last_close >= stop:
            return False

    atr = df["atr14"].iloc[-1] if "atr14" in df.columns else None
    if atr is not None and not pd.isna(atr) and atr > 0:
        stop_distance = abs(bl - stop)
        if stop_distance < MIN_STOP_DISTANCE_ATR_MULT * atr:
            return False  # stop aşırı dar, normal piyasa gürültüsüyle tetiklenebilir

    return True




def fake_breakout_risk(df: pd.DataFrame, breakout_info: Dict, atr: float) -> str:
    row = breakout_info.get("breakout_row")
    if row is None:
        return "Yüksek"
    body = abs(row["close"] - row["open"])
    if atr <= 0:
        return "Orta"
    if body < 0.4 * atr or not breakout_info.get("volume_confirmed"):
        return "Yüksek"
    if body < 0.8 * atr:
        return "Orta"
    return "Düşük"


def geometry_quality_from_diffs(diffs: List[float], tolerance: float) -> float:
    """Verilen yüzdesel farkların toleransa göre ortalama kalitesini 0-1 döndürür."""
    if not diffs:
        return 0.0
    scores = [max(0.0, 1 - (d / tolerance)) for d in diffs]
    return float(np.clip(np.mean(scores), 0.0, 1.0))


# --------------------------------------------------------------------------
# YÜKSELİŞ FORMASYONLARI
# --------------------------------------------------------------------------

def detect_bull_flag(df: pd.DataFrame, atr_series: pd.Series) -> Optional[Dict]:
    """
    Bull Flag: güçlü yükseliş dalgası (pole) + hafif aşağı/yatay kanal
    (flag) + üst trend çizgisi kırılımı.
    """
    n = len(df)
    if n < 40:
        return None

    window = df.iloc[-40:].reset_index(drop=True)
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    # Pole: son 25 mum içinde 15 mumluk bir bölümde güçlü yükseliş ara
    pole_len = 12
    best_pole = None
    for start in range(0, len(window) - pole_len - 8):
        seg = window.iloc[start:start + pole_len]
        move = seg["close"].iloc[-1] - seg["close"].iloc[0]
        if move > 2.5 * atr and seg["volume"].mean() > df["volume"].rolling(20).mean().iloc[-1]:
            best_pole = (start, start + pole_len)

    if best_pole is None:
        return None

    flag_start = best_pole[1]
    flag = window.iloc[flag_start:].reset_index(drop=True)
    if len(flag) < 6:
        return None

    x = np.arange(len(flag))
    upper_slope, upper_int = linreg(x, flag["high"].values)
    lower_slope, lower_int = linreg(x, flag["low"].values)
    pole_slope = (window["close"].iloc[best_pole[1]] - window["close"].iloc[best_pole[0]]) / pole_len

    # Flag kanalı düşük eğimli veya hafif aşağı olmalı, pole'den çok daha yatay
    if upper_slope > 0.3 * pole_slope:
        return None
    if abs(upper_slope - lower_slope) / (abs(pole_slope) + 1e-9) > 1.5:
        return None  # kanal paralel değil

    breakout_level = upper_slope * (len(flag) - 1) + upper_int
    info = check_breakout(df, breakout_level, "LONG")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, breakout_level, "LONG")
    pole_height = window["close"].iloc[best_pole[1]] - window["close"].iloc[best_pole[0]]
    target = breakout_level + pole_height
    stop = flag["low"].min()

    geom_q = geometry_quality_from_diffs(
        [abs(upper_slope - lower_slope) / (abs(pole_slope) + 1e-9)], 1.0)

    # window, df'in son 40 mumu olduğu için pencere-içi indeksleri global df
    # indekslerine çeviriyoruz (grafik çiziminde doğru konumlanma için).
    window_offset = n - len(window)
    g_pole_start = window_offset + best_pole[0]
    g_pole_end = window_offset + best_pole[1]
    g_flag_start = window_offset + flag_start

    return {
        "name": "Bull Flag",
        "direction": "LONG",
        "trendlines": [("upper", g_flag_start, upper_slope, upper_int, len(flag)),
                        ("lower", g_flag_start, lower_slope, lower_int, len(flag))],
        "points": {
            "pole_start": (g_pole_start, float(window["close"].iloc[best_pole[0]])),
            "pole_end": (g_pole_end, float(window["close"].iloc[best_pole[1]])),
        },
        "breakout_level": breakout_level,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


def detect_double_bottom(df: pd.DataFrame, pivots_low: List[int],
                          pivots_high: List[int], atr_series: pd.Series) -> Optional[Dict]:
    if len(pivots_low) < 2 or len(pivots_high) < 1:
        return None
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    p1, p2 = pivots_low[-2], pivots_low[-1]
    if p2 - p1 < 5:
        return None

    low1, low2 = df["low"].iloc[p1], df["low"].iloc[p2]
    diff = pct_diff(low1, low2)
    if diff > 0.03:  # iki dip %3'ten fazla farklıysa geçersiz
        return None

    neckline_candidates = [h for h in pivots_high if p1 < h < p2]
    if not neckline_candidates:
        return None
    neck_idx = max(neckline_candidates, key=lambda i: df["high"].iloc[i])
    neckline = df["high"].iloc[neck_idx]

    if neckline <= max(low1, low2):
        return None

    info = check_breakout(df, neckline, "LONG")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, neckline, "LONG")
    depth = neckline - min(low1, low2)
    target = neckline + depth
    stop = min(low1, low2)
    geom_q = geometry_quality_from_diffs([diff], 0.03)

    return {
        "name": "Double Bottom",
        "direction": "LONG",
        "trendlines": [],
        "points": {"low1": (p1, low1), "low2": (p2, low2), "neck": (neck_idx, neckline)},
        "breakout_level": neckline,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


def detect_inverse_head_shoulders(df: pd.DataFrame, pivots_low: List[int],
                                   pivots_high: List[int], atr_series: pd.Series) -> Optional[Dict]:
    if len(pivots_low) < 3 or len(pivots_high) < 2:
        return None
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    l_shoulder, head, r_shoulder = pivots_low[-3], pivots_low[-2], pivots_low[-1]
    if not (l_shoulder < head < r_shoulder):
        return None

    ls_price = df["low"].iloc[l_shoulder]
    head_price = df["low"].iloc[head]
    rs_price = df["low"].iloc[r_shoulder]

    if head_price >= ls_price or head_price >= rs_price:
        return None  # baş, omuzlardan daha derin olmalı
    shoulder_diff = pct_diff(ls_price, rs_price)
    if shoulder_diff > 0.05:
        return None

    neck_candidates = [h for h in pivots_high if l_shoulder < h < r_shoulder]
    if not neck_candidates:
        return None
    neck1 = neck_candidates[0]
    neck_price1 = df["high"].iloc[neck1]
    neck2_idx = neck_candidates[-1] if len(neck_candidates) > 1 else neck1
    neck_price2 = df["high"].iloc[neck2_idx]
    neckline = (neck_price1 + neck_price2) / 2
    neck_mid_idx = int(round((neck1 + neck2_idx) / 2))

    info = check_breakout(df, neckline, "LONG")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, neckline, "LONG")
    depth = neckline - head_price
    target = neckline + depth
    stop = head_price
    geom_q = geometry_quality_from_diffs([shoulder_diff], 0.05)

    return {
        "name": "Inverse Head and Shoulders",
        "direction": "LONG",
        "trendlines": [],
        "points": {"l_shoulder": (l_shoulder, ls_price), "head": (head, head_price),
                   "r_shoulder": (r_shoulder, rs_price),
                   "neck_left": (neck1, neck_price1), "neck_right": (neck2_idx, neck_price2),
                   "neck": (neck_mid_idx, neckline)},
        "breakout_level": neckline,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


def detect_falling_wedge(df: pd.DataFrame, pivots_high: List[int],
                          pivots_low: List[int], atr_series: pd.Series) -> Optional[Dict]:
    if len(pivots_high) < 2 or len(pivots_low) < 2:
        return None
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    highs = pivots_high[-3:]
    lows = pivots_low[-3:]
    if len(highs) < 2 or len(lows) < 2:
        return None

    xh = np.array(highs)
    yh = df["high"].iloc[highs].values
    xl = np.array(lows)
    yl = df["low"].iloc[lows].values

    slope_h, int_h = linreg(xh, yh)
    slope_l, int_l = linreg(xl, yl)

    # Falling wedge: her iki çizgi de düşüşte, üst çizgi alt çizgiden daha dik düşmeli (yakınsama)
    if not (slope_h < 0 and slope_l < 0):
        return None
    if slope_h > slope_l * 0.9:  # yakınsama yeterli değil
        return None

    last_idx = len(df) - 1
    breakout_level = slope_h * last_idx + int_h

    # BUG FİX (v6): Ham pivot noktası tamponsuz stop olarak kullanılıyordu;
    # yakınsayan kama geometrisinde bu, stop'u breakout'a neredeyse
    # yapıştırıyordu. Artık son dip pivotunun biraz altına (0.5 ATR),
    # normal fitil gürültüsüne dayanabilecek bir tampon ekleniyor.
    raw_stop = df["low"].iloc[lows[-1]]
    stop = raw_stop - 0.5 * atr
    if stop >= breakout_level:
        return None  # geometri tutarsız: extrapolasyon stop'u breakout'un üstüne taşımış

    info = check_breakout(df, breakout_level, "LONG")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, breakout_level, "LONG")
    wedge_height = (yh[0] - yl[0])
    target = breakout_level + max(wedge_height, 2 * atr)
    convergence = abs(slope_h - slope_l) / (abs(slope_l) + 1e-9)
    geom_q = float(np.clip(convergence / 2, 0, 1))

    points = {f"high_{i}": (int(idx), float(df["high"].iloc[idx])) for i, idx in enumerate(highs)}
    points.update({f"low_{i}": (int(idx), float(df["low"].iloc[idx])) for i, idx in enumerate(lows)})

    return {
        "name": "Falling Wedge",
        "direction": "LONG",
        "trendlines": [("upper", 0, slope_h, int_h, last_idx),
                        ("lower", 0, slope_l, int_l, last_idx)],
        "points": points,
        "breakout_level": breakout_level,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


def detect_cup_and_handle(df: pd.DataFrame, atr_series: pd.Series) -> Optional[Dict]:
    n = len(df)
    if n < 60:
        return None
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    # reset_index YAPMIYORUZ: df RangeIndex'e sahip olduğu için .iloc dilimi
    # sonrası index etiketleri global konumlarla birebir aynı kalır; bu da
    # grafik çiziminde formasyon noktalarını doğru yerde göstermemizi sağlar.
    cup_window = df.iloc[-60:-15]
    handle_window = df.iloc[-15:]
    if len(cup_window) < 20 or len(handle_window) < 5:
        return None

    left_slice = cup_window["high"].iloc[:5]
    right_slice = cup_window["high"].iloc[-5:]
    left_rim_idx = int(left_slice.idxmax())
    right_rim_idx = int(right_slice.idxmax())
    left_rim = float(left_slice.max())
    right_rim = float(right_slice.max())
    cup_bottom_idx = int(cup_window["low"].idxmin())
    cup_bottom = float(cup_window["low"].min())
    rim_diff = pct_diff(left_rim, right_rim)
    if rim_diff > 0.05:
        return None
    if cup_bottom >= min(left_rim, right_rim) * 0.97:
        return None  # yeterince derin U şekli yok

    # U-şekli (konkav yukarı) kontrolü: 2. derece polinom fit, katsayı > 0
    x = np.arange(len(cup_window))
    y = cup_window["close"].values
    coeffs = np.polyfit(x, y, 2)
    if coeffs[0] <= 0:
        return None  # konkav yukarı değil

    # Grafikte fincan eğrisini çizebilmek için polinomdan üretilen eğri
    # noktalarını (global indeks, fiyat) çiftleri olarak sakla.
    curve_y = np.polyval(coeffs, x)
    cup_window_start_global = int(cup_window.index[0])
    curve_points = [(cup_window_start_global + i, float(curve_y[i])) for i in range(len(x))]

    handle_high_idx = int(handle_window["high"].idxmax())
    handle_low_idx = int(handle_window["low"].idxmin())
    handle_high = float(handle_window["high"].max())
    handle_low = float(handle_window["low"].min())
    # Kulp, rim'in altında ve sığ bir düşüş olmalı
    if handle_high > right_rim * 1.01:
        return None
    if pct_diff(handle_low, handle_high) > 0.5:
        return None

    breakout_level = max(left_rim, right_rim)
    info = check_breakout(df, breakout_level, "LONG")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, breakout_level, "LONG")
    depth = breakout_level - cup_bottom
    target = breakout_level + depth
    stop = handle_low
    geom_q = geometry_quality_from_diffs([rim_diff], 0.05)

    return {
        "name": "Cup and Handle",
        "direction": "LONG",
        "trendlines": [],
        "points": {
            "left_rim": (left_rim_idx, left_rim),
            "right_rim": (right_rim_idx, right_rim),
            "bottom": (cup_bottom_idx, cup_bottom),
            "handle_high": (handle_high_idx, handle_high),
            "handle_low": (handle_low_idx, handle_low),
        },
        "curve": curve_points,
        "breakout_level": breakout_level,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


# --------------------------------------------------------------------------
# DÜŞÜŞ FORMASYONLARI
# --------------------------------------------------------------------------

def detect_bear_flag(df: pd.DataFrame, atr_series: pd.Series) -> Optional[Dict]:
    n = len(df)
    if n < 40:
        return None
    window = df.iloc[-40:].reset_index(drop=True)
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    pole_len = 12
    best_pole = None
    for start in range(0, len(window) - pole_len - 8):
        seg = window.iloc[start:start + pole_len]
        move = seg["close"].iloc[-1] - seg["close"].iloc[0]
        if move < -2.5 * atr and seg["volume"].mean() > df["volume"].rolling(20).mean().iloc[-1]:
            best_pole = (start, start + pole_len)

    if best_pole is None:
        return None

    flag_start = best_pole[1]
    flag = window.iloc[flag_start:].reset_index(drop=True)
    if len(flag) < 6:
        return None

    x = np.arange(len(flag))
    upper_slope, upper_int = linreg(x, flag["high"].values)
    lower_slope, lower_int = linreg(x, flag["low"].values)
    pole_slope = (window["close"].iloc[best_pole[1]] - window["close"].iloc[best_pole[0]]) / pole_len

    if lower_slope < 0.3 * pole_slope:
        return None
    if abs(upper_slope - lower_slope) / (abs(pole_slope) + 1e-9) > 1.5:
        return None

    breakout_level = lower_slope * (len(flag) - 1) + lower_int
    info = check_breakout(df, breakout_level, "SHORT")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, breakout_level, "SHORT")
    pole_height = abs(window["close"].iloc[best_pole[1]] - window["close"].iloc[best_pole[0]])
    target = breakout_level - pole_height
    stop = flag["high"].max()
    geom_q = geometry_quality_from_diffs(
        [abs(upper_slope - lower_slope) / (abs(pole_slope) + 1e-9)], 1.0)

    window_offset = n - len(window)
    g_pole_start = window_offset + best_pole[0]
    g_pole_end = window_offset + best_pole[1]
    g_flag_start = window_offset + flag_start

    return {
        "name": "Bear Flag",
        "direction": "SHORT",
        "trendlines": [("upper", g_flag_start, upper_slope, upper_int, len(flag)),
                        ("lower", g_flag_start, lower_slope, lower_int, len(flag))],
        "points": {
            "pole_start": (g_pole_start, float(window["close"].iloc[best_pole[0]])),
            "pole_end": (g_pole_end, float(window["close"].iloc[best_pole[1]])),
        },
        "breakout_level": breakout_level,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


def detect_head_shoulders(df: pd.DataFrame, pivots_high: List[int],
                           pivots_low: List[int], atr_series: pd.Series) -> Optional[Dict]:
    if len(pivots_high) < 3 or len(pivots_low) < 2:
        return None
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    l_shoulder, head, r_shoulder = pivots_high[-3], pivots_high[-2], pivots_high[-1]
    if not (l_shoulder < head < r_shoulder):
        return None

    ls_price = df["high"].iloc[l_shoulder]
    head_price = df["high"].iloc[head]
    rs_price = df["high"].iloc[r_shoulder]

    if head_price <= ls_price or head_price <= rs_price:
        return None
    shoulder_diff = pct_diff(ls_price, rs_price)
    if shoulder_diff > 0.05:
        return None

    neck_candidates = [l for l in pivots_low if l_shoulder < l < r_shoulder]
    if not neck_candidates:
        return None
    neck1 = neck_candidates[0]
    neck_price1 = df["low"].iloc[neck1]
    neck2_idx = neck_candidates[-1] if len(neck_candidates) > 1 else neck1
    neck_price2 = df["low"].iloc[neck2_idx]
    neckline = (neck_price1 + neck_price2) / 2
    neck_mid_idx = int(round((neck1 + neck2_idx) / 2))

    info = check_breakout(df, neckline, "SHORT")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, neckline, "SHORT")
    depth = head_price - neckline
    target = neckline - depth
    stop = head_price
    geom_q = geometry_quality_from_diffs([shoulder_diff], 0.05)

    return {
        "name": "Head and Shoulders",
        "direction": "SHORT",
        "trendlines": [],
        "points": {"l_shoulder": (l_shoulder, ls_price), "head": (head, head_price),
                   "r_shoulder": (r_shoulder, rs_price),
                   "neck_left": (neck1, neck_price1), "neck_right": (neck2_idx, neck_price2),
                   "neck": (neck_mid_idx, neckline)},
        "breakout_level": neckline,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


def detect_double_top(df: pd.DataFrame, pivots_high: List[int],
                       pivots_low: List[int], atr_series: pd.Series) -> Optional[Dict]:
    if len(pivots_high) < 2 or len(pivots_low) < 1:
        return None
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    p1, p2 = pivots_high[-2], pivots_high[-1]
    if p2 - p1 < 5:
        return None

    high1, high2 = df["high"].iloc[p1], df["high"].iloc[p2]
    diff = pct_diff(high1, high2)
    if diff > 0.03:
        return None

    neckline_candidates = [l for l in pivots_low if p1 < l < p2]
    if not neckline_candidates:
        return None
    neck_idx = min(neckline_candidates, key=lambda i: df["low"].iloc[i])
    neckline = df["low"].iloc[neck_idx]

    if neckline >= min(high1, high2):
        return None

    info = check_breakout(df, neckline, "SHORT")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, neckline, "SHORT")
    height = max(high1, high2) - neckline
    target = neckline - height
    stop = max(high1, high2)
    geom_q = geometry_quality_from_diffs([diff], 0.03)

    return {
        "name": "Double Top",
        "direction": "SHORT",
        "trendlines": [],
        "points": {"high1": (p1, high1), "high2": (p2, high2), "neck": (neck_idx, neckline)},
        "breakout_level": neckline,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


def detect_rising_wedge(df: pd.DataFrame, pivots_high: List[int],
                         pivots_low: List[int], atr_series: pd.Series) -> Optional[Dict]:
    if len(pivots_high) < 2 or len(pivots_low) < 2:
        return None
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    highs = pivots_high[-3:]
    lows = pivots_low[-3:]
    if len(highs) < 2 or len(lows) < 2:
        return None

    xh = np.array(highs)
    yh = df["high"].iloc[highs].values
    xl = np.array(lows)
    yl = df["low"].iloc[lows].values

    slope_h, int_h = linreg(xh, yh)
    slope_l, int_l = linreg(xl, yl)

    if not (slope_h > 0 and slope_l > 0):
        return None
    if slope_l < slope_h * 0.9:
        return None  # alt çizgi üst çizgiden daha dik yükselmeli (yakınsama)

    last_idx = len(df) - 1
    breakout_level = slope_l * last_idx + int_l

    # BUG FİX (v6): Falling Wedge ile aynı sebeple, son tepe pivotunun
    # biraz üstüne (0.5 ATR) tampon ekleniyor.
    raw_stop = df["high"].iloc[highs[-1]]
    stop = raw_stop + 0.5 * atr
    if stop <= breakout_level:
        return None  # geometri tutarsız: extrapolasyon stop'u breakout'un altına taşımış

    info = check_breakout(df, breakout_level, "SHORT")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, breakout_level, "SHORT")
    wedge_height = (yh[0] - yl[0])
    target = breakout_level - max(abs(wedge_height), 2 * atr)
    convergence = abs(slope_h - slope_l) / (abs(slope_h) + 1e-9)
    geom_q = float(np.clip(convergence / 2, 0, 1))

    points = {f"high_{i}": (int(idx), float(df["high"].iloc[idx])) for i, idx in enumerate(highs)}
    points.update({f"low_{i}": (int(idx), float(df["low"].iloc[idx])) for i, idx in enumerate(lows)})

    return {
        "name": "Rising Wedge",
        "direction": "SHORT",
        "trendlines": [("upper", 0, slope_h, int_h, last_idx),
                        ("lower", 0, slope_l, int_l, last_idx)],
        "points": points,
        "breakout_level": breakout_level,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


def detect_descending_triangle(df: pd.DataFrame, pivots_high: List[int],
                                pivots_low: List[int], atr_series: pd.Series) -> Optional[Dict]:
    if len(pivots_high) < 2 or len(pivots_low) < 2:
        return None
    atr = atr_series.iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None

    lows = pivots_low[-3:]
    highs = pivots_high[-3:]
    if len(lows) < 2 or len(highs) < 2:
        return None

    low_prices = df["low"].iloc[lows].values
    flat_diff = pct_diff(low_prices[0], low_prices[-1])
    if flat_diff > 0.02:
        return None  # destek yatay olmalı

    xh = np.array(highs)
    yh = df["high"].iloc[highs].values
    slope_h, int_h = linreg(xh, yh)
    if slope_h >= 0:
        return None  # direnç alçalan olmalı

    support = float(np.mean(low_prices))

    info = check_breakout(df, support, "SHORT")
    if not info["breakout_confirmed"]:
        return None

    invalidated = last_candle_invalidates(df, support, "SHORT")
    height = float(df["high"].iloc[highs[0]]) - support
    target = support - height
    # BUG FİX (v6): Aynı yakınsama sorunu üçgenin direnç tarafında da var;
    # son direnç pivotuna 0.5 ATR tampon ekleniyor.
    stop = float(df["high"].iloc[highs[-1]]) + 0.5 * atr
    geom_q = geometry_quality_from_diffs([flat_diff], 0.02)

    last_idx = len(df) - 1
    points = {f"high_{i}": (int(idx), float(df["high"].iloc[idx])) for i, idx in enumerate(highs)}
    points.update({f"low_{i}": (int(idx), float(df["low"].iloc[idx])) for i, idx in enumerate(lows)})
    points["support_start"] = (int(lows[0]), support)
    points["support_end"] = (int(lows[-1]), support)

    return {
        "name": "Descending Triangle",
        "direction": "SHORT",
        "trendlines": [("upper", 0, slope_h, int_h, last_idx)],
        "points": points,
        "breakout_level": support,
        **info,
        "fake_breakout_risk": fake_breakout_risk(df, info, atr),
        "invalidated": invalidated,
        "target": target,
        "stop_loss": stop,
        "geometry_quality": geom_q,
    }


# --------------------------------------------------------------------------
# Tüm formasyonları tek noktadan tarayan yardımcı fonksiyon
# --------------------------------------------------------------------------

def detect_all_patterns(df: pd.DataFrame) -> List[Dict]:
    """Bir DataFrame üzerinde 10 formasyonun tamamını dener, bulunanları döndürür."""
    pivots_high, pivots_low = find_pivots(df, order=5)
    atr_series = df["atr14"] if "atr14" in df.columns else None
    if atr_series is None:
        from indicators import compute_atr
        atr_series = compute_atr(df, 14)

    results = []
    detectors = [
        lambda: detect_bull_flag(df, atr_series),
        lambda: detect_double_bottom(df, pivots_low, pivots_high, atr_series),
        lambda: detect_inverse_head_shoulders(df, pivots_low, pivots_high, atr_series),
        lambda: detect_falling_wedge(df, pivots_high, pivots_low, atr_series),
        lambda: detect_cup_and_handle(df, atr_series),
        lambda: detect_bear_flag(df, atr_series),
        lambda: detect_head_shoulders(df, pivots_high, pivots_low, atr_series),
        lambda: detect_double_top(df, pivots_high, pivots_low, atr_series),
        lambda: detect_rising_wedge(df, pivots_high, pivots_low, atr_series),
        lambda: detect_descending_triangle(df, pivots_high, pivots_low, atr_series),
    ]

    for det in detectors:
        try:
            res = det()
        except Exception:  # noqa: BLE001
            res = None
        if res is None:
            continue
        if res.get("invalidated", False):
            continue
        if not passes_final_sanity_check(df, res):
            continue  # BUG FİX: hedef/stop zaten geçilmiş ya da geometri tutarsız
        results.append(res)

    return results
