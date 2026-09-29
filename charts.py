# -*- coding: utf-8 -*-
"""
charts.py
---------
Tespit edilen her formasyon için mumlar, trend çizgileri, kırılım
(breakout) seviyesi, hedef (target), stop-loss çizgileri VE formasyonun
kendi geometrik şeklini (omuzlar, boyun çizgisi, çift tepe/dip, fincan
eğrisi, bayrak direği vb.) grafiğin üzerine çizer.

Grafik penceresi, formasyonun tüm noktalarını (ör. bayrağın direği,
fincanın başlangıcı) kapsayacak şekilde otomatik ayarlanır; böylece
sabit bir "son 120 mum" penceresi formasyonun bir kısmını kesip atmaz.
"""

import os
import re
import logging

import matplotlib
matplotlib.use("Agg")  # Sunucu / terminal ortamında ekran gerektirmez
import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd

logger = logging.getLogger("scanner.charts")

CHARTS_DIR = "charts"

# Formasyon şekli çiziminde kullanılan sabit renkler
COLOR_SHAPE = "#f5a623"     # formasyon ana hatları (turuncu/amber)
COLOR_NECK = "#b967ff"      # boyun çizgisi (mor)
COLOR_BREAKOUT = "#4f8ef7"  # breakout seviyesi (mavi)
COLOR_TARGET = "#2ca02c"    # hedef (yeşil)
COLOR_STOP = "#ef5350"      # stop loss (kırmızı)


def _safe_filename(symbol: str, tf: str, pattern_name: str) -> str:
    clean_pattern = re.sub(r"[^A-Za-z0-9]", "", pattern_name)
    return f"{symbol}_{tf}_{clean_pattern}.png"


def _collect_pattern_indices(pattern: dict) -> list:
    """Formasyona ait tüm global df indekslerini toplar (pencere hesabı için)."""
    idxs = []
    for tl in pattern.get("trendlines", []):
        try:
            _, start_idx, _slope, _intercept, length = tl
            idxs.append(start_idx)
            idxs.append(start_idx + max(length - 1, 0))
        except Exception:  # noqa: BLE001
            continue
    for v in pattern.get("points", {}).values():
        if isinstance(v, (tuple, list)) and len(v) == 2:
            idxs.append(v[0])
    for pt in pattern.get("curve", []):
        idxs.append(pt[0])
    return [i for i in idxs if isinstance(i, (int, np.integer))]


def _compute_plot_window(df: pd.DataFrame, pattern: dict,
                          min_len: int = 60, max_len: int = 220,
                          padding: int = 8) -> tuple:
    """
    Formasyonun tüm noktalarını içerecek şekilde çizim penceresinin
    (start, end) global indekslerini hesaplar.
    """
    n = len(df)
    idxs = _collect_pattern_indices(pattern)
    if idxs:
        start = max(0, min(idxs) - padding)
    else:
        start = max(0, n - min_len)

    end = n  # her zaman en güncel muma kadar göster

    if end - start < min_len:
        start = max(0, end - min_len)

    if end - start > max_len:
        start = end - max_len

    return start, end


def _local_x(global_idx: int, offset: int) -> int:
    return global_idx - offset


def _add_point_marker(ax, offset: int, idx: int, price: float, color: str,
                       label: str = None, marker: str = "o", size: int = 55,
                       text_offset=(6, 8)):
    lx = _local_x(idx, offset)
    ax.scatter([lx], [price], color=color, edgecolors="black", linewidths=0.6,
               zorder=6, s=size, marker=marker)
    if label:
        ax.annotate(label, xy=(lx, price), xytext=text_offset,
                    textcoords="offset points", fontsize=8, color=color,
                    fontweight="bold", zorder=7)


def _add_connecting_line(ax, offset: int, points: list, color: str,
                          style: str = "-", width: float = 1.6, alpha: float = 0.9):
    xs = [_local_x(p[0], offset) for p in points]
    ys = [p[1] for p in points]
    ax.plot(xs, ys, color=color, linestyle=style, linewidth=width,
             alpha=alpha, zorder=5)


def _draw_pattern_shape(ax, offset: int, pattern: dict):
    """Formasyon tipine göre özel geometrik şekli grafiğe çizer."""
    name = pattern.get("name", "")
    pts = pattern.get("points", {})

    try:
        if name == "Double Bottom":
            low1 = pts.get("low1")
            low2 = pts.get("low2")
            neck = pts.get("neck")
            if low1 and low2 and neck:
                _add_connecting_line(ax, offset, [low1, neck, low2], COLOR_SHAPE)
                _add_point_marker(ax, offset, *low1, COLOR_SHAPE, "Dip 1")
                _add_point_marker(ax, offset, *low2, COLOR_SHAPE, "Dip 2")
                _add_point_marker(ax, offset, *neck, COLOR_NECK, "Boyun")
                ax.axhline(neck[1], color=COLOR_NECK, linestyle=":", linewidth=1.0, alpha=0.6)

        elif name == "Double Top":
            high1 = pts.get("high1")
            high2 = pts.get("high2")
            neck = pts.get("neck")
            if high1 and high2 and neck:
                _add_connecting_line(ax, offset, [high1, neck, high2], COLOR_SHAPE)
                _add_point_marker(ax, offset, *high1, COLOR_SHAPE, "Tepe 1")
                _add_point_marker(ax, offset, *high2, COLOR_SHAPE, "Tepe 2")
                _add_point_marker(ax, offset, *neck, COLOR_NECK, "Boyun")
                ax.axhline(neck[1], color=COLOR_NECK, linestyle=":", linewidth=1.0, alpha=0.6)

        elif name in ("Inverse Head and Shoulders", "Head and Shoulders"):
            ls = pts.get("l_shoulder")
            head = pts.get("head")
            rs = pts.get("r_shoulder")
            neck_l = pts.get("neck_left")
            neck_r = pts.get("neck_right")
            neck = pts.get("neck")
            if ls and head and rs:
                chain = [ls]
                if neck_l:
                    chain.append(neck_l)
                chain.append(head)
                if neck_r:
                    chain.append(neck_r)
                chain.append(rs)
                _add_connecting_line(ax, offset, chain, COLOR_SHAPE)
                _add_point_marker(ax, offset, *ls, COLOR_SHAPE, "Sol Omuz")
                _add_point_marker(ax, offset, *head, COLOR_SHAPE, "Baş")
                _add_point_marker(ax, offset, *rs, COLOR_SHAPE, "Sağ Omuz")
            if neck:
                _add_point_marker(ax, offset, *neck, COLOR_NECK, "Boyun Çizgisi")
                ax.axhline(neck[1], color=COLOR_NECK, linestyle=":", linewidth=1.0, alpha=0.6)

        elif name == "Cup and Handle":
            curve = pattern.get("curve", [])
            if curve:
                xs = [_local_x(p[0], offset) for p in curve]
                ys = [p[1] for p in curve]
                ax.plot(xs, ys, color=COLOR_SHAPE, linewidth=2.0, alpha=0.9, zorder=5)
            left_rim = pts.get("left_rim")
            right_rim = pts.get("right_rim")
            bottom = pts.get("bottom")
            handle_high = pts.get("handle_high")
            handle_low = pts.get("handle_low")
            if left_rim:
                _add_point_marker(ax, offset, *left_rim, COLOR_SHAPE, "Sol Kenar")
            if right_rim:
                _add_point_marker(ax, offset, *right_rim, COLOR_SHAPE, "Sağ Kenar")
            if bottom:
                _add_point_marker(ax, offset, *bottom, COLOR_SHAPE, "Dip")
            if handle_high and handle_low:
                _add_connecting_line(ax, offset, [handle_high, handle_low], COLOR_NECK, style="--")
                _add_point_marker(ax, offset, *handle_high, COLOR_NECK, "Kulp")

        elif name in ("Bull Flag", "Bear Flag"):
            pole_start = pts.get("pole_start")
            pole_end = pts.get("pole_end")
            if pole_start and pole_end:
                _add_connecting_line(ax, offset, [pole_start, pole_end], COLOR_SHAPE, width=2.2)
                _add_point_marker(ax, offset, *pole_start, COLOR_SHAPE, "Direk Başı")
                _add_point_marker(ax, offset, *pole_end, COLOR_SHAPE, "Direk Sonu")

        elif name in ("Falling Wedge", "Rising Wedge", "Descending Triangle"):
            for key, val in pts.items():
                if key.startswith("high_") or key.startswith("low_"):
                    _add_point_marker(ax, offset, *val, COLOR_SHAPE, marker="D", size=35)
            if name == "Descending Triangle":
                s_start = pts.get("support_start")
                s_end = pts.get("support_end")
                if s_start and s_end:
                    _add_connecting_line(ax, offset, [s_start, s_end], COLOR_NECK, style=":")
    except Exception:  # noqa: BLE001
        logger.warning("Formasyon şekli çizilirken hata oluştu (%s)", name, exc_info=True)


def plot_pattern(df: pd.DataFrame, symbol: str, tf: str, pattern: dict) -> str:
    """
    Formasyon grafiğini oluşturur ve dosya yolunu döndürür.
    df: 'open_time' sütunlu tam OHLCV DataFrame (RangeIndex 0..n-1).
    """
    os.makedirs(CHARTS_DIR, exist_ok=True)

    start, end = _compute_plot_window(df, pattern)
    plot_df = df.iloc[start:end].copy()
    plot_df = plot_df.set_index("open_time")
    plot_df = plot_df[["open", "high", "low", "close", "volume"]]
    offset = start

    addplots = []

    # --- Trend çizgileri (bayrak, kama, üçgen) ---
    for tl in pattern.get("trendlines", []):
        try:
            _, start_idx, slope, intercept, length = tl
            full_xs = [start_idx + i for i in range(length)]
            full_ys = [slope * i + intercept for i in range(length)]
            line_series = pd.Series(index=plot_df.index, dtype=float)
            for gx, gy in zip(full_xs, full_ys):
                local_idx = gx - offset
                if 0 <= local_idx < len(plot_df):
                    line_series.iloc[local_idx] = gy
            line_series = line_series.interpolate(limit_area="inside")
            addplots.append(mpf.make_addplot(line_series, color="orange", width=1.4))
        except Exception:  # noqa: BLE001
            continue

    # --- Breakout / target / stop yatay çizgileri ---
    hlines = []
    hcolors = []
    if pattern.get("breakout_level") is not None:
        hlines.append(pattern["breakout_level"])
        hcolors.append(COLOR_BREAKOUT)
    if pattern.get("target") is not None:
        hlines.append(pattern["target"])
        hcolors.append(COLOR_TARGET)
    if pattern.get("stop_loss") is not None:
        hlines.append(pattern["stop_loss"])
        hcolors.append(COLOR_STOP)

    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", inherit=True)
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle="--", gridcolor="#e0e0e0",
                                facecolor="white", figcolor="white")

    direction_tr = "YÜKSELİŞ (LONG)" if pattern["direction"] == "LONG" else "DÜŞÜŞ (SHORT)"
    title = f"{symbol} - {tf} - {pattern['name']} - {direction_tr}"

    filename = _safe_filename(symbol, tf, pattern["name"])
    filepath = os.path.join(CHARTS_DIR, filename)

    plot_kwargs = dict(
        type="candle",
        style=style,
        volume=True,
        title=title,
        returnfig=True,
        figsize=(13, 7.5),
        tight_layout=True,
    )
    if addplots:
        plot_kwargs["addplot"] = addplots
    if hlines:
        plot_kwargs["hlines"] = dict(hlines=hlines, colors=hcolors, linestyle="-.", linewidths=1.2)

    fig, axlist = mpf.plot(plot_df, **plot_kwargs)
    ax = axlist[0]

    # --- Formasyonun kendi geometrik şeklini (omuz/tepe/dip/eğri) çiz ---
    _draw_pattern_shape(ax, offset, pattern)

    # --- Lejant ---
    legend_handles = []
    legend_labels = []
    if pattern.get("breakout_level") is not None:
        legend_handles.append(plt.Line2D([0], [0], color=COLOR_BREAKOUT, lw=2))
        legend_labels.append("Breakout")
    if pattern.get("target") is not None:
        legend_handles.append(plt.Line2D([0], [0], color=COLOR_TARGET, lw=2))
        legend_labels.append("Hedef")
    if pattern.get("stop_loss") is not None:
        legend_handles.append(plt.Line2D([0], [0], color=COLOR_STOP, lw=2))
        legend_labels.append("Stop Loss")
    if pattern.get("points") or pattern.get("curve"):
        legend_handles.append(plt.Line2D([0], [0], color=COLOR_SHAPE, lw=2))
        legend_labels.append("Formasyon Şekli")
    if legend_handles:
        ax.legend(legend_handles, legend_labels, loc="upper left", fontsize=8)

    fig.savefig(filepath, dpi=130)
    plt.close(fig)
    logger.info("Grafik kaydedildi: %s", filepath)
    return filename
