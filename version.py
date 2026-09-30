# -*- coding: utf-8 -*-
"""
version.py
----------
Uygulamanın sürüm bilgisi. Her sinyal, kaydedildiği andaki sürümle birlikte
sinyal günlüğüne yazılır; böylece puanlama/tahmin mantığı değiştikçe
sürümler arasında başarı karşılaştırması yapılabilir.

KURAL: Sinyal tespiti, puanlama veya tahmin mantığını değiştiren her
değişiklikte VERSION artırılır ve CHANGELOG'a bir satır eklenir.
  - Büyük değişiklik (yeni endeks, ağırlık/formül değişimi): orta hane (1.3 -> 1.4)
  - Küçük düzeltme (hata düzeltme, sonucu az etkileyen ayar): son hane (1.3.0 -> 1.3.1)
Yalnızca görünümü değiştiren değişikliklerde sürüm artırmak gerekmez.
"""

import os
import subprocess
from functools import lru_cache

VERSION = "1.4.1"

# (sürüm, tarih, açıklama) — en yeni en üstte
CHANGELOG = [
    ("1.4.1", "2026-09-30", "Binance hız sınırı koruması: 429/418 alınınca istekler Retry-After süresince "
                            "durdurulur, spot veride data-api.binance.vision yedeğine geçilir."),
    ("1.4.0", "2026-09-30", "Binance ile emir açma (Spot + USDT-M Vadeli, Demo + Canlı), emir özeti ve "
                            "onayı, otomatik hedef/stop emirleri; profil sayfası ve kullanıcı bazlı şifreli API anahtarı."),
    ("1.3.1", "2026-09-30", "CoinGecko Demo anahtarı desteği: piyasa yönü (TOTAL/BTC.D) anahtarla "
                            "çekiliyor; temel analizde tarama başına ayrıntı bütçesi 12 → 40 coin."),
    ("1.3.0", "2026-09-29", "Sinyallere sürüm bilgisi eklendi; DeFi metrikleri yalnızca DeFi "
                            "protokollerinde hesaplanıyor (L1 zincirleri hariç)."),
    ("1.2.0", "2026-09-29", "Temel Analiz endeksi: şeffaflık, geliştirme, token ekonomisi, "
                            "olgunluk, DeFi metrikleri."),
    ("1.1.0", "2026-09-29", "Güven endeksleri: geçmiş başarı, piyasa yönü (şimdi), Crypto Manager "
                            "(zaman dilimi uyumu, göreli güç, likidite, türev piyasa), risk/ödül."),
    ("1.0.0", "2026-09-21", "İlk sürüm: yalnızca formasyon skoru ve tahmini başarı olasılığı."),
]


@lru_cache(maxsize=1)
def build_commit() -> str:
    """Çalışan kodun git commit kısaltması (Render'da ortam değişkeninden)."""
    commit = os.environ.get("RENDER_GIT_COMMIT", "").strip()
    if commit:
        return commit[:7]
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=os.path.dirname(os.path.abspath(__file__)),
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def version_label() -> str:
    commit = build_commit()
    return f"v{VERSION}" + (f" ({commit})" if commit else "")
