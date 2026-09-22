# Binance Trading Bot

Binance'daki tüm USDT paritelerini tarayan, çoklu teknik indikatör
(RSI, MACD, EMA trend, hacim, volatilite) **ve** resmi haber kaynaklarından
Claude ile çıkarılan duygu analizini birleştirerek fırsatları puanlayan,
otomatik alım-satım yapan bir bot.

> ⚠️ **Hiçbir strateji kâr garantisi vermez.** Bu bot, kazanma olasılığını
> artırmaya çalışan filtreler (teknik + haber teyidi, sıkı stop-loss, günlük
> zarar limiti, işlemler arası bekleme) uygular; ama kayıplı işlemler
> **olacaktır**, bu normaldir. "%1 hedef, her işlemde kazanç" gibi bir
> garanti veren kod yoktur ve olamaz. Varsayılan olarak `USE_TESTNET=true`
> (sahte para) ile gelir - `.env` dosyasında bunu bilerek `false` yapmadan
> bot gerçek parayla işlem yapmaz. Gerçek hesaba geçmeden önce botu
> haftalarca testnet'te izlemeni şiddetle tavsiye ederim.

## 1. Kurulum

```bash
cd binance_bot
pip install -r requirements.txt
cp .env.example .env
```

## 2. Testnet API Anahtarı Alma

1. https://testnet.binance.vision/ adresine git
2. "Log in with GitHub" ile giriş yap
3. "Generate HMAC_SHA256 Key" butonuna tıkla
4. Oluşan `API Key` ve `Secret Key` değerlerini `.env` dosyasına yapıştır

`.env` dosyasında `USE_TESTNET=true` olarak kalmalı.

## 3. Çalıştırma

```bash
python main.py
```

Bot:
1. Anında ilk taramasını yapar
2. `.env` dosyasındaki `SCAN_INTERVAL_MINUTES` ayarına göre periyodik olarak tekrar tarar
3. Tüm işlemleri hem terminale hem `trading_bot.log` dosyasına yazar
4. Açık pozisyonları ve günlük kâr/zarar bilgisini `state.json` dosyasında saklar

Durdurmak için `Ctrl+C`.

## 4. Nasıl Çalışıyor?

### Tarama ve Puanlama (`strategy.py`)
Her USDT paritesi için bir "fırsat skoru" hesaplanır:

| Bileşen | Ne ölçer | Ağırlık |
|---|---|---|
| Trend (EMA20 vs EMA50) | Fiyat yükseliş trendinde mi | 0-30 |
| RSI(14) | Aşırı alım/satım, toparlanma bölgesi | 0-25 |
| MACD histogram | Momentum güçleniyor mu | 0-25 |
| Hacim | Ortalamaya göre ilgi artışı | 0-20 |
| Volatilite (ATR%) | Çok durgun/çok riskli coinleri eler | filtre |
| Haber/duygu analizi | Resmi kaynaklardan çıkarılan duygu | ±20 (veya tam veto) |

Skoru en yüksek pariteler, eşik değerin (varsayılan 60) üzerindeyse ve
uygunsa (bakiye, açık pozisyon limiti, bekleme süresi vb.) satın alınır.

### Haber / Duygu Analizi (`news_sentiment.py`, `sentiment.py`)
X (Twitter) API'si arama/okuma için ücretli olduğundan (aylık min. ~$200)
kapsam dışı bırakıldı. Bunun yerine bot, resmi haber ajansları ve düzenleyici
kurumların **RSS beslemelerini** (CoinDesk, Cointelegraph, SEC, Fed - `.env`
içindeki `NEWS_RSS_FEEDS` ile genişletilebilir) periyodik olarak tarar,
daha önce görülmemiş her başlığı **Claude'a** gönderip -1 (çok olumsuz) ile
+1 (çok olumlu) arası bir skor aldırır. Bu skor:
- İlgili paritenin fırsat skoruna ±20 puan olarak yansır,
- Çok olumsuzsa (`NEGATIVE_SENTIMENT_VETO`, varsayılan -0.5) o parite teknik
  skoru ne olursa olsun **tamamen elenir** (hack/dava/yasak haberinde "ucuzladı"
  diye almayı engeller),
- SEC/Fed gibi genel piyasayı ilgilendiren haberlerde tüm yeni pozisyon
  açma işlemleri o tur için durur.

`ANTHROPIC_API_KEY` boş bırakılırsa bu katman sessizce atlanır, bot sadece
teknik indikatörlerle çalışmaya devam eder.

### Komisyona Yenik Düşmemek: Az ama Seçici İşlem
Binance spot komisyonu işlem başına ~%0.1 (BNB ile ~%0.075), yani bir
alım+satım ~%0.15-0.2 demektir. Hedef kâr `TAKE_PROFIT_PCT` (varsayılan %1)
gibi küçük olduğunda sık işlem yapmak komisyonla kârı eritir. Bu yüzden:
- Tam piyasa+haber taraması seyrek çalışır (`SCAN_INTERVAL_MINUTES`, varsayılan 180 dk),
- Yeni bir işlem açıldıktan sonra en az `MIN_MINUTES_BETWEEN_TRADES` (varsayılan
  120 dk) geçmeden başka yeni işlem açılmaz,
- Açık pozisyonların stop-loss/take-profit kontrolü ise bundan bağımsız,
  sık yapılır (`POSITION_CHECK_INTERVAL_MINUTES`, varsayılan 10 dk) - zarar
  durdurma bu bekleme sürelerinden etkilenmez.

Sabit bir "günde X işlem" tavanı **yok**; sistem doğal olarak seyrek ve
seçici işlem yapacak şekilde tasarlandı.

### Risk Yönetimi (`risk_manager.py`)
- Her işlemde bakiyenin sadece `POSITION_SIZE_PCT` kadarı kullanılır (varsayılan %10)
- Her pozisyon için otomatik **stop-loss** (varsayılan %1.5) ve **take-profit** (varsayılan %3, ~2:1 kâr/risk oranı)
- Aynı anda en fazla `MAX_OPEN_POSITIONS` pozisyon (varsayılan 5)
- Günlük zarar `DAILY_MAX_LOSS_PCT`'i (varsayılan %5) aşarsa bot o gün yeni işlem açmaz
- **Kâr geri-verme koruması:** gün içinde ulaşılan en yüksek kârın `PROFIT_GIVEBACK_LIMIT_PCT`
  kadarı (varsayılan %40) geri verilirse (ör. gün +100 USDT'ye çıktı, sonra +60'a
  geriledi) bot kalan kârı korumak için o gün yeni işlem açmayı durdurur
- Günlük zarar limiti veya kâr geri-verme koruması bir kez tetiklenince, PnL o gün
  içinde toparlansa bile tekrar açılmaz - panik/aşırı işlem döngüsüne girilmez
- İşlemler arası minimum bekleme (`MIN_MINUTES_BETWEEN_TRADES`) - yukarı bakın

Tüm bu değerleri `.env` dosyasından değiştirebilirsin. **Not:** %3 hedef / %1.5
stop, "her işlem kazandırır" garantisi değildir - sadece kazanılan işlemlerde
kaybedilenlerin ~2 katı kadar kazanmayı hedefler; kayıplı işlemler yine olur.

## 5. Dosya Yapısı

```
binance_bot/
├── main.py            # Giriş noktası, iki ayrı zamanlayıcı döngü (pozisyon kontrolü / tam tarama)
├── trader.py           # Ana iş mantığı: tara -> puanla -> işlem yap
├── strategy.py          # Kompozit skorlama motoru (teknik + duygu)
├── indicators.py        # RSI, EMA, MACD, ATR hesaplamaları
├── news_sentiment.py     # Resmi RSS kaynaklarını tarar, sembol eşleştirir, decay uygular
├── sentiment.py          # Claude API ile haber metni -> duygu skoru
├── risk_manager.py       # Pozisyon boyutu, SL/TP, günlük limit, işlemler arası bekleme
├── binance_client.py      # Binance API sarmalayıcısı
├── config.py            # .env dosyasından ayarları okur
├── .env.example          # Örnek ayar dosyası
├── requirements.txt
├── state.json            # (otomatik oluşur) açık pozisyonlar, günlük PnL, son işlem zamanı
└── news_state.json        # (otomatik oluşur) görülen haberler, sembol/piyasa duygu skorları
```

## 6. ÖNEMLİ Güvenlik Notları

- **API anahtarlarında "Withdrawal" (para çekme) iznini asla açma.** Bot
  sadece "Spot Trading" iznine ihtiyaç duyar. Bu sayede anahtarların ele
  geçirilse bile paran çekilemez.
- `.env` dosyasını asla GitHub'a veya başka bir yere yükleme.
- Gerçek hesaba geçmeden önce (`USE_TESTNET=false`) küçük bir tutarla
  başla ve botu yakından izle.
- Bu kod bir yatırım tavsiyesi değildir; kendi araştırmanı yap ve
  kaybetmeyi göze alamayacağın parayla işlem yapma.

## 7. Sınırlamalar / Geliştirme Fikirleri

- Şu anki strateji orta vadeli (1 saatlik mumlar) trend/momentum takibine
  dayanır; gün içi (scalping) veya çok uzun vadeli stratejiler için ayrı
  ayarlama gerekir.
- Backtesting (geçmiş veri üzerinde test) modülü eklenmemiştir; eklemek
  istersen `strategy.py`'deki `score_symbol` fonksiyonu geçmiş veri
  üzerinde döngüyle çalıştırılabilir.
- Şu an piyasa emirleri (market order) kullanılıyor; limit emir ve OCO
  (One-Cancels-Other) emirleri eklenerek kayma (slippage) azaltılabilir.
  %1'lik bir take-profit hedefinde slippage'ın kendisi bile önemli bir pay
  yiyebilir - küçük hedefli stratejilerde bu risklidir.
- X (Twitter) kapsam dışı; sadece resmi RSS kaynakları kullanılıyor (bkz.
  bölüm 4). İleride X API'sine (ücretli) geçmek istersen `news_sentiment.py`
  içine ayrı bir toplayıcı eklemek yeterli, geri kalan katmanlar (skorlama,
  veto, decay) aynı kalır.
- Duygu analizi Claude API çağrısı yaptığından, çok fazla yeni haber çıkan
  bir dönemde küçük bir API maliyeti oluşur (haiku modeliyle düşük).
