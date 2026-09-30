# Crypto Manager — Kural Seti

> **Bu belge ürünün çalışma mantığındaki tüm kuralların tek kaynağıdır.**
> Sinyal tespiti, puanlama, eleme, kayıt veya tahmin mantığını değiştiren her
> güncellemede bu belge de aynı commit içinde güncellenir (bkz. [§16](#16-değişiklik-ve-sürüm-kuralları)).
>
> Son güncelleme: **v1.4.0** · 2026-09-30 · Kodla karşılaştırma: `patterns.py`, `scoring.py`,
> `scanner.py`, `confidence.py`, `fundamentals.py`, `market_direction.py`, `journal.py`,
> `backtest.py`, `auth.py`, `ayarlar.py`, `binance_client.py`, `trading.py`, `profiles.py`

## İçindekiler

1. [Temel ilkeler](#1-temel-ilkeler)
2. [Taranan evren](#2-taranan-evren)
3. [İndikatörler](#3-indikatörler)
4. [Pivot (tepe/dip) tespiti](#4-pivot-tepedip-tespiti)
5. [Formasyonlar](#5-formasyonlar)
6. [Kırılım ve geçerlilik kuralları](#6-kırılım-ve-geçerlilik-kuralları)
7. [Formasyon skoru ve tahmini başarı](#7-formasyon-skoru-ve-tahmini-başarı)
8. [Eleme ve çakışma kuralları](#8-eleme-ve-çakışma-kuralları)
9. [Güven endeksleri](#9-güven-endeksleri)
10. [Piyasa rejimi sınıflandırması](#10-piyasa-rejimi-sınıflandırması)
11. [Sinyal günlüğü](#11-sinyal-günlüğü)
12. [Backtest](#12-backtest)
13. [Coin Ara](#13-coin-ara)
14. [Binance ile emir açma](#14-binance-ile-emir-açma)
15. [Erişim ve profil kuralları](#15-erişim-ve-profil-kuralları)
16. [Değişiklik ve sürüm kuralları](#16-değişiklik-ve-sürüm-kuralları)
17. [Bilinen sınırlamalar](#17-bilinen-sınırlamalar)

---

## 1. Temel ilkeler

- **Amaç yön tahminidir, yükseliş beklentisi değil.** Sistem hem LONG (yükseliş) hem SHORT
  (düşüş) sinyali üretir. 10 formasyonun 5'i LONG, 5'i SHORT'tur.
- **Tüm güven puanları sinyalin yönüne göredir (0-100).** Yüksek puan = veri bu sinyalin
  yönünü destekliyor. Aynı veri LONG için düşük, SHORT için yüksek puan üretebilir
  (ör. düşen piyasa). Tek istisna **likidite**dir; yönden bağımsız bir kalite ölçüsüdür.
- **Gösterim:** Uyum puanları `71/100` biçiminde, gerçek oranlar `%64 (22 sinyal)` biçiminde
  gösterilir. Renk/işaret eşikleri: **≥ 65 ✓ destekliyor (yeşil)**, **45-64 ~ nötr (sarı)**,
  **< 45 ✗ ters (kırmızı)**.
- **Geleceğe bakılmaz.** Canlı taramada yalnızca son kapanmış/devam eden mumlara kadar veri
  kullanılır. Backtest de yürüyen pencereyle (walk-forward) çalışır.
- **Veri alınamayan bileşen puanı bozmaz.** Eksik bileşen hesaptan çıkarılır, kalan
  bileşenlerin ağırlıkları yeniden dağıtılır; arayüzde "veri yok" olarak gösterilir.
- Sistem yatırım tavsiyesi vermez; karar destek aracıdır. Emir yalnızca kullanıcı özeti
  görüp onayladığında, kullanıcının kendi Binance anahtarıyla açılır; otomatik işlem yoktur.

## 2. Taranan evren

| Kural | Değer | Nerede |
|---|---|---|
| Borsa / piyasa | Binance **Spot**, USDT pariteleri | `utils.py` |
| Coin seçimi | 24 saatlik USDT işlem hacmine göre ilk **N** parite (varsayılan 50, panelden değiştirilebilir) | `utils.get_top_usdt_symbols` |
| Durum | Yalnızca `TRADING` durumundaki ve spot işleme açık pariteler | |
| Hariç: stablecoin tabanlılar | USDC, FDUSD, TUSD, USDP, DAI, USDS, BUSD, EUR, EURI, GBP, TRY, AEUR, USDe, PYUSD, RLUSD, USD1, U | `STABLECOINS` |
| Hariç: kaldıraçlı tokenlar | Taban varlığı `UP`, `DOWN`, `BULL`, `BEAR` ile bitenler | |
| Zaman dilimleri | **1H, 4H, 1D** | `ayarlar.TIMEFRAMES` |
| Mum sayısı | Her zaman dilimi için son **500** mum | `ayarlar.KLINE_LIMIT` |
| Yetersiz veri | 60'tan az mum varsa o coin/zaman dilimi atlanır | `scanner.py` |
| İstek aralığı | Her mum isteğinden sonra 0,15 sn bekleme (Binance limiti) | |

## 3. İndikatörler

`indicators.py` — her zaman dilimi için hesaplanır:

| İndikatör | Parametre |
|---|---|
| RSI | 14 (Wilder yumuşatma) |
| MACD | 12 / 26 / 9 (histogram kullanılır) |
| EMA | 20, 50, 200 |
| ADX | 14 |
| ATR | 14 (Wilder) |
| OBV | — |
| Hacim ortalaması | 20 mum |

## 4. Pivot (tepe/dip) tespiti

- Pivot tepe/dip: bir mumun sağında ve solunda **5'er mum** içinde daha yüksek (tepe) / daha
  düşük (dip) mum olmaması (`scipy.argrelextrema`, `order=5`).
- Birbirine **3 mumdan yakın** pivotlardan yalnızca ilki tutulur (plato temizleme).
- Trend çizgileri pivotlardan **doğrusal regresyonla** üretilir.

## 5. Formasyonlar

Tümünde kırılım seviyesi aşağıdaki [§6](#6-kırılım-ve-geçerlilik-kuralları) kurallarıyla teyit edilmek zorundadır.

### LONG formasyonları

| Formasyon | Tespit kuralı | Kırılım seviyesi | Hedef | Stop |
|---|---|---|---|---|
| **Bull Flag** | Son 40 mumda 12 mumluk "direk": yükseliş > **2,5 ATR** ve direk hacmi > 20 mumluk ortalama. Ardından ≥ 6 mumluk bayrak kanalı: üst çizgi eğimi ≤ direk eğiminin **%30**'u, kanal çizgileri paralel (eğim farkı / direk eğimi ≤ 1,5) | Bayrak üst çizgisinin son değeri | Kırılım + direk yüksekliği | Bayrağın en düşük dibi |
| **Double Bottom** | Son iki dip pivot arası ≥ 5 mum, dipler arası fark ≤ **%3**, aralarında bir tepe pivotu (boyun) | Aradaki en yüksek tepe (boyun çizgisi) | Boyun + (boyun − en düşük dip) | En düşük dip |
| **Inverse Head and Shoulders** | Son 3 dip: baş iki omuzdan da derin, omuzlar arası fark ≤ **%5**, omuzlar arasında tepe pivotları | Boyun tepelerinin ortalaması | Boyun + (boyun − baş) | Baş (en düşük dip) |
| **Falling Wedge** | Son 2-3 tepe ve dipten iki düşen çizgi; üst çizgi daha dik düşer (üst eğim ≤ alt eğim × 0,9 → yakınsama) | Üst çizginin son mumdaki değeri | Kırılım + max(ilk genişlik, 2 ATR) | Son dip − **0,5 ATR** |
| **Cup and Handle** | Son 60 mumun ilk 45'i fincan, son 15'i kulp. Fincan kenarları arası fark ≤ %5, dip kenarların en az %3 altında, kapanışlara 2. derece polinom konkav yukarı (U). Kulp tepesi sağ kenarın ≤ %1 üstünde | İki kenarın yükseği | Kırılım + fincan derinliği | Kulp dibi |

### SHORT formasyonları

| Formasyon | Tespit kuralı | Kırılım seviyesi | Hedef | Stop |
|---|---|---|---|---|
| **Bear Flag** | Bull Flag'in tersi: 12 mumluk düşüş > 2,5 ATR + yüksek hacim; alt çizgi eğimi direk eğiminin %30'undan dik olamaz, kanal paralel | Bayrak alt çizgisinin son değeri | Kırılım − direk yüksekliği | Bayrağın en yüksek tepesi |
| **Head and Shoulders** | Son 3 tepe: baş iki omuzdan da yüksek, omuzlar arası fark ≤ %5, aralarında dip pivotları | Boyun diplerinin ortalaması | Boyun − (baş − boyun) | Baş |
| **Double Top** | Son iki tepe arası ≥ 5 mum, tepeler arası fark ≤ %3, aralarında dip pivotu | Aradaki en düşük dip | Boyun − (en yüksek tepe − boyun) | En yüksek tepe |
| **Rising Wedge** | İki yükselen çizgi; alt çizgi daha dik yükselir (alt eğim ≥ üst eğim × 0,9) | Alt çizginin son mumdaki değeri | Kırılım − max(ilk genişlik, 2 ATR) | Son tepe + **0,5 ATR** |
| **Descending Triangle** | Son 2-3 dip yatay (ilk-son fark ≤ **%2**), tepelerden geçen çizgi alçalan | Destek (diplerin ortalaması) | Destek − (ilk tepe − destek) | Son tepe + **0,5 ATR** |

## 6. Kırılım ve geçerlilik kuralları

Bir formasyonun sinyal sayılabilmesi için **hepsi** sağlanmalıdır (`patterns.py`):

1. **Kırılım teyidi:** Son **3 mum** içinde en az birinin **kapanışı** kırılım seviyesinin
   doğru tarafında olmalı (LONG: üstünde, SHORT: altında).
2. **Son mum bozmamalı:** Son kapanış seviyenin tekrar yanlış tarafına geçtiyse formasyon
   geçersizdir (LONG: seviyenin **%0,5** altı, SHORT: **%0,5** üstü).
3. **Geometri sırası:** LONG'da `stop < kırılım < hedef`, SHORT'ta `stop > kırılım > hedef`.
4. **Bayat sinyal koruması:** Güncel fiyat hedefe zaten ulaştıysa veya stop'u zaten
   geçtiyse formasyon reddedilir.
5. **Minimum stop mesafesi:** Stop, kırılım seviyesine **0,5 ATR**'den yakın olamaz
   (sıradan bir fitille tetiklenecek kırılgan kurulumlar elenir).
6. **Hacim teyidi (skoru etkiler, eleme yapmaz):** Kırılım mumunun hacmi 20 mumluk
   ortalamanın **1,3 katından** fazlaysa "hacim teyitli".
7. **Sahte kırılım riski:** Kırılım mumunun gövdesi < **0,4 ATR** veya hacim teyidi yoksa
   **Yüksek**; gövde < **0,8 ATR** ise **Orta**; aksi halde **Düşük**.

## 7. Formasyon skoru ve tahmini başarı

`scoring.py` — 100 üzerinden:

| Bileşen | Maks. | Kural |
|---|---|---|
| Geometri | 30 | Formasyona özgü uyum kalitesi (0-1) × 30. Ör. ikili dipte dip farkı / %3 toleransı |
| Hacim | 20 | Kırılım hacmi / 20 mumluk ortalama: 0,8x → 2, 1,0x → 5, 1,3x → 12, 2x → 18, 3x+ → 20 (doğrusal ara değer) |
| Kırılım | 20 | Kırılım teyitli +10, kapanış teyitli +6, sahte kırılım riski Düşük +4 / Orta +2 |
| Trend uyumu | 15 | EMA20/50/200 dizilimi sinyal yönünde: tam 10, kısmi 6, yok 2; + ADX/50 × 5 (en çok 5) |
| Volatilite (ATR) | 10 | ATR/fiyat %0,5-%6 arası 10 puan; altı ve üstü kademeli düşer (en az 2) |
| Momentum | 5 | Yön lehine RSI (LONG > 50, SHORT < 50) +2,5; MACD histogramı yön lehine +2,5 |

**Etiket:** > 85 Çok Güçlü · ≥ 70 Güçlü · ≥ 60 Orta · < 60 Zayıf.

**Tahmini başarı olasılığı (%):** bileşen oranlarının ağırlıklı ortalaması
(geometri 0,25 · kırılım 0,22 · hacim 0,18 · trend 0,15 · ATR 0,10 · momentum 0,10),
`40 + ağırlıklı × 55` ile **%40-95** aralığına ölçeklenir.

**Sahte kırılım tavanı:** Risk **Yüksek** ise tahmini başarı en çok **%65** ve etiket en çok
"Güçlü"; risk **Orta** ise en çok **%80**.

## 8. Eleme ve çakışma kuralları

- **Eşik:** Formasyon skoru **< 60** ise sinyal gösterilmez ve günlüğe yazılmaz
  (`ayarlar.MIN_SCORE`). Coin Ara'da bu formasyonlar ayrı bölümde gösterilir.
- **Yön çakışması:** Aynı coin + zaman diliminde hem LONG hem SHORT formasyon eşiği
  geçerse yalnızca **en yüksek skorlu yönün** formasyonları tutulur.
- **Sıralama:** Tarama sonuçları tahmini başarı olasılığına göre büyükten küçüğe sıralanır.
  "En güçlü LONG/SHORT" sekmeleri her yönün ilk 10'udur.

## 9. Güven endeksleri

Formasyon eşiği geçen her sinyal için hesaplanır (`confidence.py`, `fundamentals.py`).

### 9.1 Geçmiş başarı (Formasyon kolonunda, %)

- Kaynak: sinyal günlüğündeki **sonuçlanmış** sinyaller (`HEDEF` veya `STOP`); açıklar sayılmaz.
- Oran = hedef / (hedef + stop).
- Kapsam sırası: önce aynı **formasyon + zaman dilimi + yön**; bu kapsamda **10'dan az**
  sonuçlanmış sinyal varsa **formasyon + yön** (tüm zaman dilimleri); o da yetersizse
  "veri az" (örnek sayısıyla).

### 9.2 Piyasa Yönü — şimdi (0-100)

LONG açısından hesaplanır, SHORT için `100 − puan`:

| Parça | Ağırlık | Kural |
|---|---|---|
| 24 saatlik piyasa değişimi | 0,6 | BTC sinyallerinde BTC'nin 24s değişimi; diğerlerinde `0,6 × TOTAL2 + 0,4 × TOTAL` 24s değişimi. Puan = `50 + 50 × tanh(değişim / 4)` |
| BTC günlük trendi | 0,4 | BTC 1D: kapanış > EMA50 ve EMA20 > EMA50 → yukarı (90); tersi → aşağı (10); diğer → kararsız (50) |

Piyasa rejimi metni ayrıca gösterilir ([§10](#10-piyasa-rejimi-sınıflandırması)).
**1 haftalık tahmin: Faz 2** (yapay zekâ + haber).

### 9.3 Temel Analiz (0-100)

Önce yönden bağımsız "proje sağlamlığı" (0-100) hesaplanır; LONG'da aynen, SHORT'ta
`100 − puan` olarak kullanılır (sağlam temel LONG'u, zayıf temel SHORT'u destekler).

| Bileşen | Ağırlık | Kural |
|---|---|---|
| Şeffaflık | 0,15 | Taban 20 + whitepaper 40 + GitHub deposu 30 + web sitesi 10 |
| Geliştirme aktivitesi | 0,20 | GitHub son güncelleme: ≤ 7 gün 95 · 30 g 80 · 90 g 55 · 1 yıl 30 · 2 yıl+ 10 (ara değer); depo arşivlenmişse 5. Depo yoksa hesaplanmaz |
| Token ekonomisi | 0,30 | Dolaşımdaki arz / (maks. veya toplam arz): %10 → 10 · %30 → 35 · %50 → 60 · %80 → 85 · %95+ → 95 (ağırlık 0,5). FDV / piyasa değeri: 1,05x → 95 · 1,5x → 75 · 2x → 60 · 4x → 30 · 10x → 5 (ağırlık 0,5) |
| Olgunluk ve büyüklük | 0,20 | Piyasa değeri sırası: #10 → 95 · #50 → 85 · #100 → 70 · #300 → 45 · #1000 → 20 (ağırlık 0,7). Yaş: 0,5 yıl → 15 · 1 → 35 · 3 → 70 · 6+ → 95 (ağırlık 0,3) |
| DeFi metrikleri | 0,15 | **Yalnızca DeFi protokolleri** (Katman-1 / akıllı sözleşme platformu kategorileri ve TVL'si olmayanlar hariç). TVL 7 günlük değişim: `50 + 45 × tanh(değişim / 15)` (ağırlık 0,6). Yıllık ücret / piyasa değeri: %0,1 → 20 · %1 → 45 · %5 → 75 · %15+ → 95 (ağırlık 0,4) |

- Coin eşlemesi: Binance taban varlığı (ör. `ETH`) → CoinGecko'da aynı sembollü **en yüksek
  piyasa değerli** coin; ilk 1000'de yoksa CoinGecko araması.
- Veriler **24 saat** önbelleklenir. Tarama başına ayrıntı (whitepaper/GitHub/DeFi) bütçesi:
  anahtarsız **12**, CoinGecko Demo anahtarıyla **40** coin; kalanlar "kısmi veri" olarak
  gösterilir ve sonraki taramalarda tamamlanır.
- Kilit açılımı (token unlock) takvimi **henüz dahil değil** (ücretsiz güvenilir kaynak yok).

### 9.4 Crypto Manager (0-100)

Dört bileşenin ağırlıklı ortalaması; ağırlıklar sinyalin zaman dilimine göre değişir:

| Bileşen | 1H | 4H | 1D | Kural (LONG açısından; SHORT için `100 − puan`) |
|---|---|---|---|---|
| Zaman dilimi uyumu | 0,25 | 0,30 | 0,30 | Coinin 1H/4H/1D trendleri (kapanış ve EMA20'nin EMA50'ye göre konumu: +1 / −1 / 0), ağırlıklar 1H 0,2 · 4H 0,35 · 1D 0,45. Puan = `50 + 50 × Σ(ağırlık × trend × yön) / Σ ağırlık` |
| Göreli güç (BTC'ye karşı) | 0,15 | 0,25 | 0,35 | 7 ve 30 günlük getiri farkı (coin − BTC): `0,6 × fark7 + 0,4 × fark30`; puan = `50 + 50 × tanh(fark / 15)`. BTC'nin kendisi için hesaplanmaz |
| Likidite (**yönden bağımsız**) | 0,25 | 0,15 | 0,10 | 24s hacim (log ölçek): 100 bin $ → 0 · 1 M → 20 · 10 M → 50 · 100 M → 80 · 1 milyar $ → 100 (ağırlık 0,6). Alış-satış farkı: 1 bps → 100 · 5 → 85 · 20 → 40 · 50+ → 0 (ağırlık 0,4). Etiket: ≥ 65 iyi, ≥ 45 orta, altı zayıf |
| Türev piyasa | 0,35 | 0,30 | 0,25 | Binance USDT-M vadeli. **Fonlama** (ağırlık 0,40): `50 − 50 × tanh((fonlama_bps − 1) / 5)` — aşırı pozitif fonlama kalabalık long'dur, SHORT lehine. **Long/short hesap oranı** (0,25): `50 − 50 × tanh((oran − 1,2) / 1)`. **Açık pozisyon 24s değişimi** (0,35): fiyatla aynı yönde artan açık pozisyon hareketi teyit eder: `50 + 50 × tanh(yön(fiyat) × değişim / 10) × güç` (fiyat değişimi < %0,5 ise güç 0,3). Vadeli piyasası olmayan coinlerde hesaplanmaz |

### 9.5 Risk / Ödül

- Giriş fiyatı = sinyal anındaki son fiyat.
- Risk/ödül = |hedef − giriş| / |giriş − stop|. Gösterim `1 : 2,0`.
- Renk: ≥ 1,5 yeşil · ≥ 1,0 sarı · < 1,0 kırmızı.
- Hedef ve stop yanında girişe yüzde uzaklık gösterilir.

### 9.6 Henüz olmayanlar

- Toplam (birleşik) güven puanı: sinyal günlüğünde yeterli sonuç biriktikten sonra, endeks
  isabetlerine göre ağırlıklandırılacak.
- Olay riski (kilit açılımları, makro takvim), 1 haftalık piyasa tahmini, satır bazında AI
  analizi: **Faz 2**.

## 10. Piyasa rejimi sınıflandırması

`market_direction.py` — BTC fiyatı (Binance), TOTAL ve BTC dominansı (CoinGecko `/global`),
TOTAL2 = TOTAL − BTC piyasa değeri. 24s değişimlere göre, ilk eşleşen kural:

| Rejim | Koşul | Eğilim |
|---|---|---|
| Yatay / kararsız | BTC, TOTAL, TOTAL2 hepsi \|değişim\| < %1 | nötr |
| Altcoin sezonu | Hepsi > %1 yükselişte **ve** TOTAL2 > BTC + 1,5 puan **ve** BTC.D < −0,3 puan | yükseliş |
| Genel yükseliş (risk-on) | Hepsi > %1 yükselişte | yükseliş |
| Alt kanaması / BTC'ye kaçış | Hepsi > %1 düşüşte **ve** TOTAL2 < BTC − 1,5 puan **ve** BTC.D > +0,3 puan | düşüş |
| Genel düşüş (risk-off) | Hepsi > %1 düşüşte | düşüş |
| BTC öncülüğünde yükseliş | BTC > %1, BTC.D > +0,3 puan, TOTAL2 < BTC | karışık |
| BTC zayıf, altlar dirençli | BTC < −%1, BTC.D < −0,3 puan, TOTAL2 > BTC | karışık |
| Karışık sinyaller | Diğer tüm durumlar | karışık |

Sonuç 60 saniye önbelleklenir.

## 11. Sinyal günlüğü

`journal.py` — Turso bulut veritabanı (`.env` yoksa yerel `signal_journal.db`).

- **Kayıt:** Eşiği geçen her sinyal, tüm güven endeksi puanları ve ayrıntılarıyla
  (`confidence_json`) birlikte **otomatik** kaydedilir (tarama ve Coin Ara).
- **Tekrar önleme:** Aynı coin + zaman dilimi + formasyon + yön için **açık** bir sinyal son
  **6 saat** içinde kaydedildiyse yenisi eklenmez. Veritabanında ayrıca (coin, TF, formasyon,
  yön, zaman) benzersiz indeksi vardır; ağ hatası sonrası tekrar deneme çift kayıt üretmez.
- **Kayıpsız yazma:** Tüm yeni kayıtlar tek işlemde (transaction) yazılır. Veritabanına
  ulaşılamazsa sinyaller `pending_signals.jsonl` dosyasında bekletilir ve bir sonraki başarılı
  kayıtta gönderilir.
- **Durum güncelleme ("Fiyatları Güncelle"):** Açık sinyaller **anlık** fiyatla kontrol edilir.
  LONG: fiyat ≥ hedef → HEDEF, fiyat ≤ stop → STOP. SHORT: tersi. Başka bir kullanıcının
  kapattığı sinyalin sonucu üzerine yazılmaz.
- **Sürüm:** Her kayıt `app_version` (ör. 1.3.1) ve `app_commit` ile yazılır. Sürüm bilgisi
  eklenmeden önceki kayıtlar geriye dönük etiketlidir (`app_commit` boş): güven endeksi
  yoksa 1.0.0, temel analizi varsa 1.2.0, diğerleri 1.1.0.
- **Kaydeden:** Sinyali yazan bilgisayar/sunucu adı `source` alanında tutulur.
- **Başarı oranı:** hedef / (hedef + stop); açık sinyaller hariç.
- **Endeks isabeti (sürüm karşılaştırması):** Bir endeks sinyali desteklerken (**≥ 65**)
  sonuçlanan sinyallerin hedef oranı. Genel başarı oranından belirgin (> 2 puan) yüksekse
  yeşil, düşükse kırmızı gösterilir.
- **Temizleme:** "Günlüğü Temizle" ortak veritabanındaki tüm sinyalleri **herkes için** siler;
  onay penceresi bunu açıkça belirtir.

## 12. Backtest

`backtest.py` — seçilen coin + zaman dilimi için:

- Son **1000** mum çekilir; **500** mumluk pencere **4'er** mum ilerletilerek her adımda
  canlı taramayla **aynı** tespit ve puanlama kuralları çalıştırılır (skor ≥ 60).
- Aynı formasyon + yön için yeni işlem açmadan önce en az **15 mum** beklenir.
- Sonuç, sonraki en çok **80 mum** içinde aranır: hedef mi stop mu önce. **Aynı mumda ikisi de
  mümkünse STOP** sayılır (muhafazakâr varsayım). Süre dolarsa "açık/belirsiz".
- Özet: formasyon + yön bazında sinyal sayısı, hedef/stop, başarı oranı, ortalama getiri,
  ortalama mum sayısı.
- Backtest sonuçları sinyal günlüğüne **yazılmaz**.

## 13. Coin Ara

- Ana taramadaki ilk N listesinde olmayan coinler de aranabilir (`USDT` eki otomatik eklenir).
- Tüm zaman dilimleri taranır; eşiği geçenler "Aktif sinyaller", geçemeyenler skorlarıyla
  ayrı bölümde gösterilir. Güven endeksleri her ikisi için de hesaplanır.
- Yalnızca eşiği geçen (aktif) sinyaller günlüğe yazılır. Yön çakışması kuralı aynen uygulanır.

## 14. Binance ile emir açma

Tarama sonuçları, Coin Ara ve Sinyal Günlüğü tablolarındaki **⇅ İşlem** butonu emir penceresini
açar. Durumu HEDEF/STOP olan (kapanmış) sinyallerde buton gösterilmez.

**Hesap ve piyasa**

- Her kullanıcı kendi Binance anahtarını **Profil → Binance API** bölümünden girer; başka
  kullanıcının anahtarı/emri görülemez.
- İki hesap türü: **Demo** (Binance Demo Trading, sahte bakiye — varsayılan) ve **Canlı**
  (gerçek para). Canlıya geçiş profilde ayrıca onay ister.
- Piyasalar: **Spot** (yalnızca LONG) ve **USDT-M Vadeli** (LONG + SHORT, süresiz sözleşme).
  SHORT sinyaller her zaman Vadeli'de açılır.
- Varsayılanlar (profilde değiştirilebilir, her emirde ayrıca seçilebilir): Demo, Vadeli,
  3x, İzole marj, 50 USDT, piyasa emri.

**Emir girdileri ve doğrulama** (sunucu her seferinde yeniden kontrol eder)

- Girdi: piyasa, giriş türü (**piyasa** veya **limit** + limit fiyatı), yatırım tutarı (USDT;
  vadelide marj), kaldıraç (**1–20x**), marj tipi (İzole/Çapraz), hedef, stop. Hedef ve stop
  sinyalden gelir, değiştirilebilir.
- LONG: `stop < giriş < hedef`; SHORT: `hedef < giriş < stop`. Piyasa emrinde güncel fiyat
  hedef/stop aralığının dışındaysa emir reddedilir ("sinyal bayatlamış olabilir").
- Fiyatlar paritenin fiyat adımına yuvarlanır; miktar = `tutar × kaldıraç / giriş`, miktar
  adımına **aşağı** yuvarlanır. Binance'in minimum miktar ve minimum emir tutarı (notional)
  kuralları uygulanır.
- Vadelide tahmini tasfiye fiyatı (bakım marjı %0,5 varsayımıyla) hesaplanır; **stop tasfiye
  fiyatının ötesindeyse emir reddedilir**.
- Kullanılabilir bakiye marj + ücretlerden azsa emir reddedilir.
- Uyarılar (engellemez): risk/ödül < 1, kaldıraç ≥ 10x, çapraz marj, limit fiyatın güncel
  fiyattan uzaklığı.

**Emir özeti ve onay**

- Emirden önce özet gösterilir: hesap, parite/yön/piyasa, giriş, miktar, pozisyon büyüklüğü,
  marj ve kaldıraç, tahmini tasfiye, hedef/stop, hedefte kâr ve stopta zarar (USDT ve marja
  göre %), risk/ödül, tahmini ücret (spot %0,1, vadeli %0,05 × giriş+çıkış), bakiye ve
  Binance'e gönderilecek emirlerin listesi.
- Gönderim için "Özeti okudum" onayı zorunludur; **Canlı** hesapta ayrıca `ONAYLA` yazılır.
- Gönderim anında özet sunucuda güncel fiyatla **yeniden hesaplanır**.
- Aynı kullanıcı aynı anda tek emir gönderebilir; aynı parite/yön/piyasa/tutar 15 sn içinde
  tekrar gönderilemez (çift tıklama koruması).

**Binance'e gönderilen emirler**

| Durum | Emirler |
|---|---|
| Spot, piyasa | `MARKET` alım (USDT tutarıyla) → dolan miktar (komisyon düşülerek) için **OCO** satış: hedef `LIMIT_MAKER`, stop `STOP_LOSS_LIMIT` (limit fiyatı stop'un %0,3 altı) |
| Spot, limit | **OTOCO**: `LIMIT` alım dolunca otomatik OCO satış (hedef + stop) |
| Vadeli | Marj tipi → kaldıraç → giriş (`MARKET` veya `LIMIT GTC`) → **koşullu emirler** (Algo Order API): `STOP_MARKET` ve `TAKE_PROFIT_MARKET`, `closePosition=true`, işaret fiyatıyla (`MARK_PRICE`) tetiklenir. Hedge modunda `positionSide` otomatik eklenir |

- Hedef/stop emirlerinden biri kurulamazsa emir **KORUMASIZ** olarak işaretlenir ve kullanıcıya
  Binance'ten elle girmesi söylenir.
- Emir durumları: `GÖNDERİLDİ` (giriş + koruma emirleri kuruldu), `KORUMASIZ`, `HATA`
  (hiçbir emir açılmadı). Tüm denemeler **Profil → Emirlerim**'de listelenir.
- Uygulama pozisyonu sonradan takip etmez/kapatmaz; limit giriş dolmazsa emir Binance'te
  bekler, iptal Binance'ten yapılır.

**API anahtarı kuralları**

- Anahtar kaydedilmeden önce Binance'e bağlanılarak doğrulanır (spot ve/veya vadeli).
- Canlı anahtarda **çekim (withdraw) izni açıksa anahtar reddedilir**.
- Anahtar ve secret veritabanında şifreli saklanır; tarayıcıya yalnızca ilk/son 4 karakter
  gösterilir.

## 15. Erişim ve profil kuralları

- Tüm sayfalar ve API'ler giriş gerektirir (yalnızca `/login` ve `/healthz` açık).
- Giriş: kullanıcı adı + Authenticator uygulamasının ürettiği **6 haneli, 30 saniyelik** kod
  (TOTP, RFC 6238). Saat kaymasına ±30 sn tolerans.
- Aynı kod **ikinci kez** kullanılamaz.
- Bir kullanıcı adıyla **5 hatalı** deneme → **15 dakika** kilit; bir IP'den 20 hatalı deneme →
  15 dakika kilit. Hata mesajı kullanıcının var olup olmadığını belli etmez.
- Oturum süresi **12 saat**.
- Dışarıdan kayıt yoktur; kullanıcılar yalnızca `manage_users.py` ile eklenir/kapatılır.
- **Profil** (`/profile`): kullanıcı kendi adını değiştirebilir (3–32 karakter; küçük harf,
  rakam, `.`, `_`, `-`; benzersiz). Authenticator kodu değişmez, sonraki girişte yeni ad
  kullanılır. Profil fotoğrafı: JPG/PNG/WEBP, en fazla 5 MB, 256×256 kare JPEG'e kırpılır;
  fotoğraf yoksa baş harf gösterilir.
- Profil ve emir API'lerinde yazma istekleri `X-CM-Request` başlığı ister (CSRF koruması).

## 16. Değişiklik ve sürüm kuralları

- Sinyal tespiti, puanlama, eleme, kayıt veya tahmin mantığını değiştiren her değişiklikte:
  1. `version.py` → `VERSION` artırılır (büyük değişiklik: 1.3 → 1.4; küçük düzeltme:
     1.3.0 → 1.3.1) ve `CHANGELOG`'a satır eklenir,
  2. **bu belge** aynı commit içinde güncellenir (üstteki "Son güncelleme" satırı dahil),
  3. altyapı etkileniyorsa [`ALTYAPI.md`](ALTYAPI.md) de güncellenir.
- Yalnızca görünümü değiştiren değişiklikler sürüm artırmaz.
- Sürümler arası karşılaştırma Sinyal Günlüğü → **Sürüm karşılaştırması** bölümünden yapılır.

### Sürüm geçmişi (özet)

| Sürüm | Tarih | Değişiklik |
|---|---|---|
| 1.4.0 | 2026-09-30 | Binance ile emir açma (Spot + Vadeli, Demo + Canlı), emir özeti, otomatik hedef/stop; profil sayfası |
| 1.3.1 | 2026-09-30 | CoinGecko Demo anahtarı: piyasa yönü anahtarla; temel analiz ayrıntı bütçesi 12 → 40 |
| 1.3.0 | 2026-09-29 | Sinyallere sürüm bilgisi; DeFi metrikleri yalnızca DeFi protokollerinde |
| 1.2.0 | 2026-09-29 | Temel Analiz endeksi |
| 1.1.0 | 2026-09-29 | Güven endeksleri: geçmiş başarı, piyasa yönü (şimdi), Crypto Manager, risk/ödül |
| 1.0.0 | 2026-09-21 | Formasyon skoru ve tahmini başarı olasılığı |

## 17. Bilinen sınırlamalar

- Durum güncellemesi yalnızca **anlık** fiyata bakar; iki kontrol arasında hedefe/stop'a değip
  dönen hareketler kaçabilir (backtest mum içi yüksek/düşük değerleri kullanır).
- Tahmini başarı olasılığı formül tabanlıdır; gerçek başarıyı **Geçmiş başarı** ve
  **sürüm karşılaştırması** ölçer.
- Temel analiz kısa vadeli (1H) hareketlerde zayıf bir göstergedir.
- Binance ABD IP'lerini engeller; sunucu Frankfurt'ta çalışır.
- Ücretsiz API limitleri nedeniyle temel analiz ilk taramalarda "kısmi" olabilir.
- Render'ın çıkış IP'leri sabit olmadığından Binance anahtarında IP kısıtlaması
  kullanılamaz; bu tür anahtarlar Binance tarafından **90 gün** sonra (ya da 30 gün
  kullanılmazsa) silinir ve yeniden girilmesi gerekir.
- Tasfiye fiyatı yaklaşıktır (kademeli bakım marjı ve fonlama dikkate alınmaz).
