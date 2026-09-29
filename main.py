# -*- coding: utf-8 -*-
"""
main.py
-------
Terminalden çalıştırılabilen CLI giriş noktası.
Kullanım:
    python main.py
    python main.py --top 50 --timeframes 1h 4h 1d
"""

import argparse
from typing import List, Dict

from scanner import run_scan, top_opportunities


def format_table(results: List[Dict]) -> str:
    header = f"{'Coin':<12}{'TF':<5}{'Formasyon':<26}{'Yön':<7}{'Skor':<6}{'Başarı %':<10}{'Breakout':<10}{'Hacim':<10}{'Güven':<12}"
    sep = "-" * len(header)
    lines = [sep, header, sep]
    for r in results:
        lines.append(
            f"{r['symbol']:<12}{r['timeframe']:<5}{r['pattern']:<26}{r['direction']:<7}"
            f"{r['score']:<6}{r['success_probability']:<10}{r['breakout']:<10}"
            f"{r['volume_desc']:<10}{r['label']:<12}"
        )
    lines.append(sep)
    return "\n".join(lines)


def format_top_table(results: List[Dict], title: str) -> str:
    lines = [f"\n{title}\n" + "=" * len(title)]
    lines.append(format_table(results))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Binance Yükseliş/Düşüş Formasyon Tarayıcı")
    parser.add_argument("--top", type=int, default=50, help="Hacme göre taranacak coin sayısı")
    parser.add_argument("--timeframes", nargs="+", default=["1h", "4h", "1d"],
                         help="Taranacak zaman dilimleri")
    args = parser.parse_args()

    print("Tarama başlatılıyor, bu işlem birkaç dakika sürebilir...\n")
    results = run_scan(top_n=args.top, timeframes=args.timeframes)

    if not results:
        print("60 puan üzerinde herhangi bir formasyon bulunamadı.")
        return

    print("\nTÜM SONUÇLAR (Başarı % sırasına göre)")
    print(format_table(results))

    top_long = top_opportunities(results, "LONG", 10)
    top_short = top_opportunities(results, "SHORT", 10)

    if top_long:
        print(format_top_table(top_long, "EN GÜÇLÜ LONG FIRSATLARI (İlk 10)"))
    if top_short:
        print(format_top_table(top_short, "EN GÜÇLÜ SHORT FIRSATLARI (İlk 10)"))

    print(f"\nGrafikler 'charts/' klasörüne kaydedildi. Toplam {len(results)} formasyon bulundu.")


if __name__ == "__main__":
    main()
