# Kripto Formasyon Tarayıcı (Binance Spot)

Binance Spot'taki en yüksek hacimli ilk 50 USDT paritesini 1H / 4H / 1D
zaman dilimlerinde tarayıp 10 klasik grafik formasyonunu (5 yükseliş,
5 düşüş) tespit eden, puanlayan ve grafikleyen tam bir Python projesi.

> 📘 **Belgeler:** Ürünün tüm kural seti → [`docs/KURALLAR.md`](docs/KURALLAR.md) ·
> Teknik altyapı ve pipeline → [`docs/ALTYAPI.md`](docs/ALTYAPI.md) ·
> Geliştirme kuralları → [`CLAUDE.md`](CLAUDE.md)

## Kurulum

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env           # ardından .env içindeki Turso bilgilerini doldurun
```

Sanal ortamın adı `.venv` olmalıdır; macOS'taki çift tıklamalı başlatıcı
(`Kripto Tarayıcıyı Başlat.command`) bu klasörü arar.

### Sinyal veritabanı (Turso)

Sinyal geçmişi ortak bir bulut veritabanında (Turso / libSQL) tutulur;
projeyi kullanan herkes aynı geçmişi görür ve günceller. Bağlantı
bilgileri proje klasöründeki `.env` dosyasından okunur (bu dosya Git'e
**gönderilmez**, her geliştirici kendi kopyasını oluşturur):

```dotenv
APP_TURSO_TECH_DB_URL=libsql://<veritabani>-<kullanici>.turso.io
APP_TURSO_TECH_TOKEN=<turso erişim anahtarı>
```

- `.env` yoksa veya değerler boşsa program yerel `signal_journal.db`
  (SQLite) dosyasıyla çalışır.
- Ek paket gerekmez; Turso'ya HTTP API üzerinden bağlanılır (`db.py`).
- Tarama sırasında Turso'ya ulaşılamazsa sinyaller kaybolmaz:
  `pending_signals.jsonl` dosyasında bekletilir ve bir sonraki başarılı
  kayıtta otomatik gönderilir.
- Eski yerel `signal_journal.db` içeriğini Turso'ya aktarmak için:
  `.venv/bin/python migrate_to_turso.py` (tekrar çalıştırmak güvenlidir,
  aynı sinyal iki kez eklenmez).
- Uygulama açılırken terminalde hangi veritabanının kullanıldığı yazar
  (`Sinyal günlüğü: Turso bulut veritabanı (...)`).

## Giriş (kullanıcı adı + Authenticator kodu)

Panel giriş gerektirir: kullanıcı adı + telefondaki Authenticator
uygulamasının (Google / Microsoft Authenticator, 1Password vb.) ürettiği
6 haneli kod. SMS / e-posta servisi kullanılmaz, ücretsizdir. Kullanıcılar
Turso veritabanında tutulur; dışarıdan kayıt yoktur.

```bash
.venv/bin/python manage_users.py add mehmet      # kullanıcı ekler, terminalde QR kod gösterir
.venv/bin/python manage_users.py list
.venv/bin/python manage_users.py reset mehmet    # telefon değişti: yeni QR
.venv/bin/python manage_users.py disable mehmet  # girişi kapat (enable ile açılır)
.venv/bin/python manage_users.py delete mehmet
```

- QR kodu Authenticator uygulamasıyla okutun; kayıt "Crypto Manager" adıyla görünür.
- Oturum 12 saat açık kalır. 5 hatalı denemeden sonra o kullanıcı 15 dakika engellenir.
- Aynı kod iki kez kullanılamaz; telefon saati otomatik ayarlı olmalıdır.
- `.env` içine `APP_SECRET_KEY=<uzun rastgele değer>` eklerseniz yerelde de
  uygulama yeniden başlayınca oturum kapanmaz (isteğe bağlı).

## Yayına alma (Render, Frankfurt)

Repo kökündeki `render.yaml` ücretsiz bir Render web servisi tanımlar
(Frankfurt bölgesi — Binance ABD IP'lerini engellediği için Avrupa seçildi).

1. https://render.com adresinde GitHub hesabınızla oturum açın.
2. **New → Blueprint** → `mcozdemir/crypto-manager` reposunu seçin.
3. İstenen `APP_TURSO_TECH_DB_URL` ve `APP_TURSO_TECH_TOKEN` değerlerini
   `.env` dosyanızdaki gibi girin → **Apply**.
4. Birkaç dakika sonra site `https://crypto-manager-xxxx.onrender.com`
   gibi bir adreste açılır. `main` dalına her push otomatik yeniden yayınlanır.

Notlar:
- Ücretsiz planda servis 15 dakika kullanılmazsa uyur; ilk açılış ~1 dakika sürer.
- Grafik dosyaları (charts/) sunucu yeniden başlayınca silinir; sinyaller
  ve kullanıcılar Turso'da olduğu için kaybolmaz.
- Kendi domaininizi sonradan Render panelinde **Settings → Custom Domains**
  bölümünden bağlayabilirsiniz.

## Güven endeksleri

Tarama, Coin Ara ve Sinyal Günlüğü tabloları aynı kolonları gösterir; her
kolon başlığındaki (i) ikonunun üzerine gelince açıklaması görünür.
**Tüm puanlar sinyalin yönüne göredir (0-100):** yüksek puan, verinin bu
LONG veya SHORT sinyalini desteklediği anlamına gelir. Aynı piyasa verisi
LONG için düşük, SHORT için yüksek puan üretebilir.

| Kolon | Ne gösterir |
|---|---|
| Formasyon | Formasyon skoru (/100) + **geçmiş başarı**: aynı formasyon/zaman dilimi/yöndeki sonuçlanmış sinyallerin hedef oranı (en az 10 örnek; yoksa "veri az") |
| Piyasa Yönü | Şimdi: TOTAL/TOTAL2 24s değişimi + BTC günlük trendi (/100). 1 haftalık tahmin: yakında |
| Temel Analiz | Şeffaflık (whitepaper, GitHub, web sitesi) + geliştirme aktivitesi (son kod güncellemesi) + token ekonomisi (dolaşımdaki arz, FDV/piyasa değeri) + olgunluk (sıralama, yaş) + DeFi (TVL trendi, ücret geliri). Sağlam temel LONG'u, zayıf temel SHORT'u destekler |
| Crypto Manager | Zaman dilimi uyumu + BTC'ye karşı göreli güç + likidite + türev piyasa (fonlama, açık pozisyon, long/short) — ağırlıklar zaman dilimine göre değişir |
| Risk/Ödül | Hedef mesafesi / stop mesafesi |

Satıra tıklayınca grafik ve tüm puanların alt kırılımı açılır. Puanlar
sinyal günlüğüne de kaydedilir (`market_score`, `cm_score`, `hist_rate`,
`rr`, `confidence_json` sütunları); böylece ileride hangi endeksin tutan
sinyalleri gerçekten öngördüğü ölçülüp ağırlıklar ayarlanabilir. Hesaplama
`confidence.py` dosyasındadır. Vadeli işlem verisine ulaşılamazsa veya
coinin vadeli piyasası yoksa o bileşen hesaba katılmaz.

Temel analiz verileri (`fundamentals.py`) CoinGecko, GitHub ve DefiLlama'nın
ücretsiz API'lerinden alınır ve Turso'daki `coin_fundamentals` tablosunda
24 saat önbelleklenir. Ücretsiz limitleri aşmamak için her taramada en fazla
12 coinin (CoinGecko Demo anahtarıyla 40) ayrıntısı (whitepaper, GitHub, DeFi) çekilir; kalanlar sonraki
taramalarda tamamlanır ("kısmi veri"). Daha hızlı ve güvenilir veri için
CoinGecko'dan ücretsiz bir **Demo API anahtarı** alıp `.env` dosyasına ve
Render → Environment bölümüne `APP_COINGECKO_API_KEY` olarak ekleyin.
Kilit açılımı (token unlock) takvimi için ücretsiz bir kaynak bulunmadığından
henüz dahil edilmedi.

## Sürümler ve karşılaştırma

Her sinyal, üretildiği uygulama sürümüyle (`version.py` → `VERSION`) ve git
commit kısaltmasıyla birlikte sinyal günlüğüne kaydedilir. Sürüm, tablolarda
tarihin altında (ör. `v1.3.0`) ve sayfa başlığında görünür.

Sinyal Günlüğü'ndeki **Sürüm karşılaştırması** bölümü her sürüm için
sinyal sayısını, başarı oranını ve endeks isabetini (Crypto Manager / Piyasa
Yönü / Temel Analiz sinyali desteklerken, yani puan ≥ 65 iken, sonuçlanan
sinyallerin hedef oranı) gösterir. Sürüm seçici ile günlük ve Excel dışa
aktarımı tek bir sürüme filtrelenebilir (`/api/journal?version=1.3.0`,
`/api/journal/versions`).

**Kural:** Sinyal tespiti, puanlama veya tahmin mantığını değiştiren her
değişiklikte `version.py` içindeki `VERSION` artırılır ve `CHANGELOG`'a satır
eklenir (büyük değişiklik: 1.3 → 1.4, küçük düzeltme: 1.3.0 → 1.3.1).

Sürüm bilgisi eklenmeden önceki kayıtlar içeriklerine göre geriye dönük
etiketlendi (gri etiket): güven endeksi olmayanlar `1.0.0`, temel analizi
olanlar `1.2.0`, diğerleri `1.1.0`.

## Kullanım

### 1) Terminal (CLI) modu
```bash
python main.py
python main.py --top 30 --timeframes 1h 4h
```
Sonuç tabloları terminale yazdırılır, grafikler `charts/` klasörüne kaydedilir.

### 2) Web Paneli (localhost)
```bash
python app.py
```
Ardından tarayıcıda **http://127.0.0.1:5050** adresini açın.

## Ayarları ve mantığı düzenleme

- Sunucu portu, taranan coin sayısı, zaman dilimleri, minimum skor ve mum
  sayısı gibi temel değerler `ayarlar.py` dosyasındadır.
- Tarama akışı `scanner.py`, formasyon kuralları `patterns.py`, puanlama
  mantığı `scoring.py`, indikatörler `indicators.py` içinden değiştirilebilir.
- Dosyalar düz metin Python kodudur; VS Code, Cursor veya herhangi bir metin
  düzenleyiciyle açılabilir. Değişiklikten sonra uygulamayı yeniden başlatın.

- "Taramayı Başlat" butonuna basınca tarama arka planda çalışır.
- İlerleme çubuğu canlı olarak güncellenir.
- Sonuç tablosuna tıklayarak her formasyonun grafiğini, hedef/stop
  seviyelerini ve güven skorunu genişletebilirsiniz.
- Sekmelerden "Tüm Sonuçlar", "En Güçlü LONG" ve "En Güçlü SHORT"
  arasında geçiş yapabilirsiniz.
- Tarama tamamlandıktan sonra sağ üstteki **"⬇ Excel'e İndir"** butonuyla
  tüm sonuçları `.xlsx` dosyası olarak indirebilirsiniz. Dosyada 3 ayrı
  sekme bulunur: "Tüm Sonuçlar", "En Güçlü LONG", "En Güçlü SHORT".

### 3) BTC Piyasa Yönü
Üst menüden **"📈 BTC Piyasa Yönü"** sekmesine geçin. Bu modül 4
göstergeyi birlikte değerlendirir:
- **BTC Fiyatı (BTCUSDT)** — Binance'ten
- **TOTAL** — kripto piyasasının toplam değeri — CoinGecko'dan
- **TOTAL2** — BTC hariç toplam piyasa değeri — TOTAL ve BTC
  dominance'tan türetilir
- **BTC Dominance (BTC.D)** — CoinGecko'dan

Amaç yalnızca bu değerlerin yükselip düştüğünü göstermek değil,
aralarındaki ilişkiyi yorumlayarak piyasanın genel rejimini
belirlemektir. Örneğin BTC ve TOTAL2 birlikte güçlü yükselirken BTC
dominance düşüyorsa bu **"Altcoin Sezonu"** anlamına gelir; hepsi
birlikte düşüyorsa **"Genel Düşüş (Risk-Off)"**; TOTAL2 BTC'den çok
daha sert düşerken BTC dominance yükseliyorsa **"Alt Kanaması / BTC'ye
Kaçış"** olarak sınıflandırılır. Sistem şu rejimlerden birini seçip
nedenini açık şekilde anlatır: Genel Yükseliş, Genel Düşüş, Altcoin
Sezonu, Alt Kanaması/BTC'ye Kaçış, BTC Öncülüğünde Yükseliş, BTC Zayıf
Altlar Dirençli, Yatay/Kararsız, ya da Karışık Sinyaller. Veriler ~60
saniye önbelleklenir; "🔄 Yenile" ile anlık güncelleyebilirsiniz.

### 4) Coin Ara
Ana tarama yalnızca hacme göre ilk 50 coini tarar; merak ettiğiniz bir
coin o listede yoksa (ör. ADAUSDT o gün ilk 50'ye girmediyse) üst
menüden **"Coin Ara"** sekmesine geçip sembolü yazıp **"Ara"**ya
tıklayın. Sistem yalnızca o coin için 1H/4H/1D'yi tarar ve iki tablo
gösterir:
- **✅ Aktif Sinyaller (Skor ≥ 60):** ana taramada da görünecek kalitedeki
  formasyonlar.
- **⚠️ Eşiği Geçemeyen Formasyonlar (Skor < 60):** geometrik olarak
  tespit edilen ama kalite eşiğini geçemediği için ana taramada
  gösterilmeyen formasyonlar — skorlarıyla birlikte. Böylece "bu coin
  neden ana taramada çıkmadı" sorusuna somut bir cevap bulabilirsiniz.

### 5) Backtest
Üst menüden **"Backtest"** sekmesine geçin, bir coin (ör. `BTCUSDT`) ve
zaman dilimi seçip **"Backtest Çalıştır"**a tıklayın. Sistem, seçilen
coin için geçmiş ~1000 mumu indirir ve formasyon tespit motorunu
"ileriye doğru yürüyerek" (walk-forward, geleceğe bakmadan) çalıştırır.
**Yalnızca canlı tarayıcıda da gösterilecek kalitede (skor ≥ 60) olan
formasyonlar sayılır** — böylece backtest sonuçları, aracın gerçekte
önereceği sinyalleri yansıtır (önceden bu eşik uygulanmıyordu ve
zayıf/düşük kaliteli formasyonlar da istatistiğe dahil oluyordu, bu da
başarı oranlarını olduğundan düşük gösteriyordu). Aynı coin+TF'de
çelişkili yön (LONG+SHORT birlikte) oluşursa yalnızca en yüksek skorlu
yön sayılır — canlı tarayıcıyla aynı mantık. Her formasyon türü için
gerçekte kaç kez hedefe, kaç kez stop'a ulaşıldığını, ortalama getiriyi
ve ortalama çözülme süresini gösteren bir özet tablo üretir.

### 6) Sinyal Günlüğü
Her canlı tarama (ve Coin Ara ile bulunan aktif sinyaller), bulduğu
sinyalleri otomatik olarak sinyal veritabanına (Turso; `.env` yoksa
yerel `signal_journal.db`) kaydeder — **Skor, Başarı Olasılığı % ve Güven (etiket) bilgileriyle
birlikte**. Üst menüden **"Sinyal Günlüğü"** sekmesine geçip **"🔄
Fiyatları Güncelle"**ye tıklayarak açık sinyallerin anlık fiyatlarla
hedefe mi stop'a mı ulaştığını kontrol edebilirsiniz. Sayfa üstünde
toplam sinyal, açık, hedef vuran, stop vuran ve **gerçek başarı oranı**
özetlenir. **"⬇ Excel'e Aktar"** ile tüm günlüğü dışa aktarabilir,
**"🗑 Günlüğü Temizle"** ile sıfırlayabilirsiniz (dikkat: ortak bulut
veritabanında bu işlem herkes için geçmişi siler).

Kayıtlı sinyaller API üzerinden filtrelenerek de okunabilir:

```
/api/journal?symbol=BTCUSDT&status=HEDEF&timeframe=4h&since=2026-09-01&until=2026-09-30&limit=1000
```

Python'dan: `journal.get_signals(symbol="BTCUSDT", status="HEDEF", since="2026-09-01")`.
Her kayıtta sinyali hangi bilgisayarın kaydettiği `source` sütununda tutulur.

## Birden fazla versiyonu aynı anda çalıştırma (A/B/C karşılaştırması)

Projeyi `crypto_pattern_scanner`, `crypto_pattern_scannerv2`,
`crypto_pattern_scannerv3` gibi ayrı klasörlere kopyalayıp aynı anda
çalıştırmak istiyorsanız:

- **Panel başlığı otomatik olarak klasör adını gösterir** (ör.
  "crypto_pattern_scannerv2"), Excel dosya adlarına da otomatik eklenir
  — hangi dosyanın hangi versiyondan geldiğini karıştırmazsınız.
- **Sinyal veritabanı:** Aynı `.env` (Turso) bilgilerini kullanan tüm
  kopyalar aynı sinyal geçmişine yazar. Kopyaların sinyallerini ayrı
  tutmak istiyorsanız her birine ayrı bir Turso veritabanı tanımlayın
  veya `.env` olmadan yerel `signal_journal.db` ile çalıştırın.
- **Aynı anda çalıştırmak için farklı portlar kullanın** (varsayılan
  5000, ikinci ve üçüncü kopya için port çakışması olur). PowerShell'de:

```powershell
# 1. terminal (crypto_pattern_scanner klasöründe)
python app.py                                    # http://127.0.0.1:5000

# 2. terminal (crypto_pattern_scannerv2 klasöründe)
$env:SCANNER_PORT="5001"; python app.py           # http://127.0.0.1:5001

# 3. terminal (crypto_pattern_scannerv3 klasöründe)
$env:SCANNER_PORT="5002"; python app.py           # http://127.0.0.1:5002
```

Her terminali ayrı bırakıp üç sekmede de tarayıcıdan ilgili adrese
girerek üçünü paralel kullanabilirsiniz.

## Dosya Yapısı

| Dosya | Görev |
|---|---|
| `utils.py` | Binance REST API'den sembol/kline/anlık fiyat verisi çekme |
| `indicators.py` | RSI, MACD, EMA20/50/200, ADX, ATR, OBV |
| `patterns.py` | Pivot tespiti, trend çizgisi (lin. regresyon), 10 formasyon algoritması |
| `scoring.py` | 100 üzerinden puanlama + başarı olasılığı (%) |
| `charts.py` | mplfinance ile mum + trend + breakout + hedef/stop grafiği |
| `scanner.py` | Tüm süreci orkestre eden tarama motoru |
| `backtest.py` | Geçmiş veride walk-forward formasyon başarı oranı testi |
| `version.py` | Uygulama sürümü ve değişiklik günlüğü (CHANGELOG) |
| `fundamentals.py` | Temel analiz: CoinGecko / GitHub / DefiLlama verisi ve puanlaması |
| `confidence.py` | Güven endeksleri: geçmiş başarı, piyasa yönü, Crypto Manager, risk/ödül |
| `journal.py` | Sinyalleri veritabanına kaydeden ve sonucunu takip eden günlük modülü |
| `db.py` | Veritabanı katmanı: Turso (bulut) veya yerel SQLite, `.env` okuma |
| `auth.py` | Giriş sistemi: kullanıcı adı + Authenticator (TOTP) kodu, oturum yönetimi |
| `manage_users.py` | Kullanıcı ekleme / listeleme / sıfırlama komutları |
| `render.yaml` | Render'da yayına alma ayarları |
| `migrate_to_turso.py` | Yerel `signal_journal.db` geçmişini Turso'ya aktaran tek seferlik betik |
| `market_direction.py` | BTC/TOTAL/TOTAL2/BTC.D'yi birlikte değerlendirip piyasa rejimini belirleyen modül |
| `excel_export.py` | Tarama sonuçlarını biçimlendirilmiş .xlsx'e aktarma |
| `main.py` | CLI giriş noktası |
| `app.py` | Flask tabanlı localhost web paneli |
| `templates/index.html` | Web panel arayüzü (Tarama / Backtest / Sinyal Günlüğü) |

## Notlar

- Sadece **60 puan ve üzeri** formasyonlar sonuç tablosunda gösterilir.
- Formasyonun son mum ile bozulup bozulmadığı (`invalidated`) kontrol
  edilir; bozulmuş formasyonlar sonuçlara dahil edilmez.
- **Bayat/geçersiz sinyal koruması:** Her formasyon, listelenmeden önce
  bir "sanity check"ten geçer — stop/breakout/hedef sıralamasının mantıklı
  olduğu ve güncel fiyatın hedefi ya da stopu **henüz** geçmediği
  doğrulanır. Bu, ör. aylar önce oluşmuş ama fiyatın çoktan hedefi
  geride bıraktığı "bayat" formasyonların aktif fırsat gibi
  gösterilmesini engeller.
- **Çelişkili sinyal koruması:** Aynı coin + aynı zaman diliminde hem
  LONG hem SHORT formasyon eşiği geçerse, yalnızca en yüksek skorlu yön
  gösterilir; diğer yön elenir (ör. aynı anda "Bull Flag LONG" ve
  "Bear Flag SHORT" birlikte gösterilmez).
- **Stablecoin filtresi:** USDC/FDUSD/DAI gibi bilinen stablecoinlerin
  yanı sıra EURI (Euro'ya sabit), RLUSD, USD1 ve U (hepsi $1'e sabit)
  de artık taranmıyor — bunlar gerçek fiyat hareketi göstermediği için
  üzerlerinde anlamlı bir formasyon oluşamaz.
- **Sahte kırılım riski ağırlığı:** "Fake Breakout Riski" Yüksek olan
  bir formasyon artık "Çok Güçlü" etiketi alamaz ve başarı olasılığı
  otomatik olarak sınırlanır (Yüksek risk → en fazla %65, Orta risk →
  en fazla %80) — önceden bu risk faktörü toplam skorun yalnızca küçük
  bir kısmını etkilediği için geometri/hacim iyi olduğunda riskli bir
  kırılım bile "Çok Güçlü" görünebiliyordu.
- **Aşırı dar stop koruması (v6):** Yakınsayan kama (Falling/Rising
  Wedge) ve üçgen (Descending Triangle) formasyonlarında, son pivot
  noktası tamponsuz stop olarak kullanıldığında stop breakout
  seviyesine neredeyse yapışık çıkabiliyordu (ör. gerçek bir üründe
  stop, breakout'un yalnızca %0.26 altındaydı — sıradan bir fitil bile
  anında tetikleyebilirdi). Artık bu formasyonların stop'una 0.5 ATR'lik
  bir tampon ekleniyor; ayrıca TÜM formasyon türleri için, stop-breakout
  mesafesi 0.5 ATR'nin altındaysa formasyon tamamen reddediliyor.
- Fake breakout riski; kırılım mumunun gövde büyüklüğü (ATR'ye oranla)
  ve hacim teyidine göre "Düşük / Orta / Yüksek" olarak etiketlenir.
- Binance API oran sınırlarına takılmamak için istekler arasına küçük
  bekleme (`safe_request_sleep`) eklenmiştir; 50 coin x 3 zaman dilimi
  taraması birkaç dakika sürebilir.
- Bu proje **yatırım tavsiyesi değildir**; puanlama ve olasılık
  değerleri sezgisel/istatistiksel kurallara dayanır, kesinlik iddia
  etmez.
