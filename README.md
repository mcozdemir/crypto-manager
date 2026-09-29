# Kripto Formasyon Tarayıcı (Binance Spot)

Binance Spot'taki en yüksek hacimli ilk 50 USDT paritesini 1H / 4H / 1D
zaman dilimlerinde tarayıp 10 klasik grafik formasyonunu (5 yükseliş,
5 düşüş) tespit eden, puanlayan ve grafikleyen tam bir Python projesi.

## Kurulum

```bash
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

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
sinyalleri otomatik olarak `signal_journal.db` (SQLite) dosyasına
kaydeder — **Skor, Başarı Olasılığı % ve Güven (etiket) bilgileriyle
birlikte**. Üst menüden **"Sinyal Günlüğü"** sekmesine geçip **"🔄
Fiyatları Güncelle"**ye tıklayarak açık sinyallerin anlık fiyatlarla
hedefe mi stop'a mı ulaştığını kontrol edebilirsiniz. Sayfa üstünde
toplam sinyal, açık, hedef vuran, stop vuran ve **gerçek başarı oranı**
özetlenir. **"⬇ Excel'e Aktar"** ile tüm günlüğü dışa aktarabilir,
**"🗑 Günlüğü Temizle"** ile sıfırlayabilirsiniz.

## Birden fazla versiyonu aynı anda çalıştırma (A/B/C karşılaştırması)

Projeyi `crypto_pattern_scanner`, `crypto_pattern_scannerv2`,
`crypto_pattern_scannerv3` gibi ayrı klasörlere kopyalayıp aynı anda
çalıştırmak istiyorsanız:

- **Panel başlığı otomatik olarak klasör adını gösterir** (ör.
  "crypto_pattern_scannerv2"), Excel dosya adlarına da otomatik eklenir
  — hangi dosyanın hangi versiyondan geldiğini karıştırmazsınız.
- **Her klasörün kendi `signal_journal.db`'si vardır**, sinyaller
  birbirine karışmaz.
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
| `journal.py` | Sinyalleri SQLite'a kaydeden ve sonucunu takip eden günlük modülü |
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
