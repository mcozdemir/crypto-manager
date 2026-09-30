# Proje çalışma notları (geliştiriciler ve Claude için)

Bu depoda her değişiklikte uyulacak kurallar:

1. **Kural belgesi:** Sinyal tespiti, puanlama, eleme, kayıt, tahmin veya erişim mantığını
   değiştiren her değişiklikte [`docs/KURALLAR.md`](docs/KURALLAR.md) aynı commit içinde
   güncellenir (ilgili bölüm + en üstteki "Son güncelleme" satırı + sürüm geçmişi tablosu).
2. **Altyapı belgesi:** Dosya, veri akışı, veritabanı şeması, dış servis, ortam değişkeni,
   yayın veya güvenlik değişikliğinde [`docs/ALTYAPI.md`](docs/ALTYAPI.md) güncellenir.
3. **Sürüm:** Ürün mantığı değişiyorsa `version.py` → `VERSION` artırılır ve `CHANGELOG`'a
   satır eklenir (büyük değişiklik 1.3 → 1.4, küçük düzeltme 1.3.0 → 1.3.1). Yalnızca
   görünüm değişikliği sürüm artırmaz.
4. **Test:** Mantık değişiklikleri gerçek Binance/CoinGecko verisiyle, `.env` olmadan
   (yerel SQLite) test edilir; canlı Turso veritabanına test verisi yazılmaz.
5. **Sırlar:** `.env` asla commit edilmez; yeni ortam değişkeni `.env.example`,
   `render.yaml` ve `docs/ALTYAPI.md`'ye eklenir.
6. **Dil:** Kod yorumları, arayüz metinleri ve belgeler Türkçe.
