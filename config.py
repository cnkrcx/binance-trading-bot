"""
config.py
---------
Tüm ayarları .env dosyasından okur. Kodun geri kalanı bu dosyadaki
değerleri kullanır; ayarları değiştirmek için .env dosyasını düzenlemen yeterli.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _get_bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "evet")


def _get_float(key: str, default: float) -> float:
    val = os.getenv(key)
    return float(val) if val else default


def _get_int(key: str, default: int) -> int:
    val = os.getenv(key)
    return int(val) if val else default


API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
USE_TESTNET = _get_bool("USE_TESTNET", True)

# "binance" = binance.com (USDT pariteleri, python-binance ile), "binance_tr"
# = binance.tr (TRY pariteleri, binance_tr_client.py ile - GERÇEK HESAP,
# sandbox yok). Binance TR'de USE_TESTNET'in hiçbir etkisi yoktur.
EXCHANGE = os.getenv("EXCHANGE", "binance").strip().lower()

MIN_24H_VOLUME_USDT = _get_float("MIN_24H_VOLUME_USDT", 5_000_000)
MIN_24H_VOLUME_TRY = _get_float("MIN_24H_VOLUME_TRY", 5_000_000)

# Bir coinin birim fiyatı bu değerin üzerindeyse hiç alınmaz (0 = sınırsız).
# Kullanıcı isteğiyle eklendi: yüksek birim fiyatlı coinlerde (ör. NEAR,
# GENIUS, KSM) tekrarlanan zararlardan sonra, sadece düşük birim fiyatlı
# coinlere yönelmek için.
MAX_COIN_PRICE = _get_float("MAX_COIN_PRICE", 0.0)
MAX_OPEN_POSITIONS = _get_int("MAX_OPEN_POSITIONS", 5)
POSITION_SIZE_PCT = _get_float("POSITION_SIZE_PCT", 0.10)
STOP_LOSS_PCT = _get_float("STOP_LOSS_PCT", 0.015)

# TAKE_PROFIT_PCT bir TAVAN değil, MİNİMUM kâr hedefidir: fiyat buraya
# ulaştığında bot hemen satmaz, "iz süren stop" (trailing stop) moduna geçer -
# fiyat yükseldikçe zirveyi (ve zirvedeki kâr yüzdesini) günceller. Çıkış
# artık sabit bir fiyat düşüşüne değil, ZİRVEDEKİ KÂRIN YÜZDESİNE göre
# tetiklenir: PROFIT_GIVEBACK_TRAIL_PCT kadarı geri verilirse satar. Böylece
# büyük bir yükselişte fiyata nefes alma payı tanır (küçük bir geri çekilmede
# hemen satmaz), ama ne kadar kâr edilmiş olursa olsun en az yarısı (varsayılan
# %50) her zaman kilitlenir.
TAKE_PROFIT_PCT = _get_float("TAKE_PROFIT_PCT", 0.03)
PROFIT_GIVEBACK_TRAIL_PCT = _get_float("PROFIT_GIVEBACK_TRAIL_PCT", 0.5)

# Kâr geri-verme koruması TAKE_PROFIT_PCT'yi beklemez - zirve kâr bu küçük
# eşiği (komisyonun üstünde, salt gürültüyü saymamak için) geçer geçmez
# devreye girer. TAKE_PROFIT_PCT panelde "hedef" olarak gösterilmeye devam
# eder ama artık korumanın ne zaman başlayacağını belirlemez.
MIN_PROFIT_TO_ARM_TRAIL_PCT = _get_float("MIN_PROFIT_TO_ARM_TRAIL_PCT", 0.01)

# Erken vazgeçme kuralı: bir pozisyon bu kadar dakika içinde hiç %1 kâra
# (MIN_PROFIT_TO_ARM_TRAIL_PCT) ulaşamazsa, bot artık büyük hareketi beklemeyi
# bırakır ve komisyonu karşılayan İLK fırsatta (en az EARLY_EXIT_MIN_PROFIT_PCT
# kâr) pozisyonu kapatır - sermayeyi daha iyi bir fırsata yönlendirmek için.
EARLY_EXIT_AFTER_MINUTES = _get_int("EARLY_EXIT_AFTER_MINUTES", 30)
EARLY_EXIT_MIN_PROFIT_PCT = _get_float("EARLY_EXIT_MIN_PROFIT_PCT", 0.003)

# "Durgun pozisyon" koruması: bir pozisyon henüz minimum kâr hedefine
# ulaşmadan (trailing moduna geçmeden) bu kadar dakika açık kalır VE kâr/zarar
# yüzdesi bu eşiğin altında kalırsa (ne net kazanıyor ne net kaybediyor),
# bot sermayeyi orada kilitli tutmak yerine pozisyonu kapatıp daha potansiyelli
# bir fırsata yönelir. Amaç: "hiçbir yere gitmeyen" işlemlerde sermayeyi
# saatlerce/günlerce durgunlaştırmamak. (2026-09-17: gerçek bir olayda 2 saat
# hiçbir yöne gitmeyen bir pozisyon fark edilince süre kısaltıldı.)
STAGNANT_EXIT_MINUTES = _get_int("STAGNANT_EXIT_MINUTES", 60)
STAGNANT_PNL_THRESHOLD_PCT = _get_float("STAGNANT_PNL_THRESHOLD_PCT", 0.01)

# Borsa komisyon oranı (işlem başına, alım ve satım ayrı ayrı alınır).
# Binance standart oranı %0.1'dir (BNB ile ödersen %0.075). Gerçekçi net
# kâr/zarar görebilmek için PnL hesaplarına bu oran her zaman uygulanır
# (testnet dahil - amaç gerçek hesapta beklenen sonucu önceden görmek).
TRADING_FEE_PCT = _get_float("TRADING_FEE_PCT", 0.001)

DAILY_MAX_LOSS_PCT = _get_float("DAILY_MAX_LOSS_PCT", 0.05)

# Gün içinde ulaşılan en yüksek kârın bu oranı kadarı geri verilirse
# (ör. gün +100 USDT'ye ulaştı, sonra +60 USDT'ye geriledi = %40 geri verildi)
# bot o gün için yeni işlem açmayı durdurur - kalan kârı korumak amacıyla.
PROFIT_GIVEBACK_LIMIT_PCT = _get_float("PROFIT_GIVEBACK_LIMIT_PCT", 0.40)

# Tam piyasa + haber taraması ne sıklıkla yapılsın (dakika). Sık taramak
# hem gereksiz API/LLM maliyeti hem de daha fazla işlem -> daha fazla komisyon
# demek; bu yüzden varsayılan değer bilerek düşük tutulmuyor.
SCAN_INTERVAL_MINUTES = _get_int("SCAN_INTERVAL_MINUTES", 180)

# Açık pozisyonların stop-loss/take-profit/kâr koruması kontrolü (fiyat
# izleme) bu tam taramadan bağımsız, çok daha sık çalışır - zarar durdurma
# gecikmemeli. Saniye cinsinden (dakika değil) - hızlı hareket eden gerçek
# parada 1 dakika bile uzun sayılabilir.
POSITION_CHECK_INTERVAL_SECONDS = _get_int("POSITION_CHECK_INTERVAL_SECONDS", 60)

# Haberler de tam piyasa taramasından bağımsız, çok daha sık yenilenir -
# panelin haber akışı güncel kalsın (fiyat/mum verisi çekmediği için hafiftir).
NEWS_REFRESH_INTERVAL_MINUTES = _get_int("NEWS_REFRESH_INTERVAL_MINUTES", 5)

# Yeni bir işlem açıldıktan sonra en az bu kadar dakika geçmeden başka yeni
# işlem AÇILMAZ (açık pozisyonların stop-loss/take-profit'i bundan etkilenmez).
# Amaç: sinyal eşiğini geçen her fırsata hemen atlayıp komisyonlarla küçük
# kârı eritmemek. Hedef kâr %1 gibi küçük olduğundan bu bekleme önemlidir.
MIN_MINUTES_BETWEEN_TRADES = _get_int("MIN_MINUTES_BETWEEN_TRADES", 120)

# Bir parite SATILDIKTAN sonra AYNI parite en az bu kadar dakika tekrar
# alınamaz - MIN_MINUTES_BETWEEN_TRADES'ten farklı olarak bu, son ALIMA değil
# son SATIŞA göre hesaplanır. Amaç: bir coin satılır satılmaz (ör. stop-loss
# ile) hemen aynı coine geri dönüp aynı hataya bir daha düşmemek - gerçek bir
# olaydan sonra eklendi (ONE_TRY art arda al-sat, ikincisinde zarar).
SAME_SYMBOL_COOLDOWN_MINUTES = _get_int("SAME_SYMBOL_COOLDOWN_MINUTES", 60)

# Bir paritenin satın alınabilmesi için fırsat skorunun (0-100) en az bu
# değere ulaşması gerekir. Düşürmek daha sık ama daha düşük kaliteli işlem,
# yükseltmek daha seyrek ama daha seçici işlem demektir.
MIN_OPPORTUNITY_SCORE = _get_float("MIN_OPPORTUNITY_SCORE", 60.0)

KLINE_INTERVAL = os.getenv("KLINE_INTERVAL", "1h")

# Duygu/haber analizi (bkz. sentiment.py, news_sentiment.py)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SENTIMENT_MODEL = os.getenv("SENTIMENT_MODEL", "claude-haiku-4-5-20251001")

_DEFAULT_NEWS_FEEDS = ",".join([
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.sec.gov/news/pressreleases.rss",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    # Altcoin haberlerinde kapsamı genişletmek için ek kaynaklar (hepsi ücretsiz, herkese açık RSS):
    "https://cryptoslate.com/feed/",
    "https://decrypt.co/feed",
    "https://u.today/rss",
    "https://cryptopotato.com/feed/",
    "https://ambcrypto.com/feed/",
    "https://beincrypto.com/feed/",
    "https://dailyhodl.com/feed/",
])
NEWS_RSS_FEEDS = [
    u.strip() for u in os.getenv("NEWS_RSS_FEEDS", _DEFAULT_NEWS_FEEDS).split(",") if u.strip()
]

# Bir haberin duygu skoru bu eşiğin altındaysa (çok olumsuz: hack, dava,
# yasak vb.) o parite teknik skoru ne olursa olsun o tur tamamen elenir.
NEGATIVE_SENTIMENT_VETO = _get_float("NEGATIVE_SENTIMENT_VETO", -0.5)

# RSI bu değerin üzerindeyse (aşırı-aşırı alım - fiyat çok hızlı yükselmiş,
# ani bir geri çekilme riski yüksek) parite diğer göstergeler ne kadar iyi
# olursa olsun tamamen elenir. Gerçek bir işlemde RSI 84 iken alım yapılıp
# saniyeler içinde stop-loss'a çarpılması sonrası eklendi (2026-09-17).
RSI_OVERBOUGHT_VETO = _get_float("RSI_OVERBOUGHT_VETO", 78.0)

# "Zaten pompalanmış coini kovalama" vetosu: fiyat son PUMP_LOOKBACK_CANDLES
# mumda (varsayılan KLINE_INTERVAL=1h ile 24 mum = 24 saat) zaten
# MAX_RECENT_PUMP_PCT'ten fazla yükselmişse, diğer göstergeler ne kadar
# güçlü olursa olsun parite tamamen elenir. Gerçek bir gözlemden sonra
# eklendi: bot "en çok artan coini" alıyordu - bu, kumar gibi zaten
# gerçekleşmiş bir hareketin devamına bahis oynamak demektir.
PUMP_LOOKBACK_CANDLES = _get_int("PUMP_LOOKBACK_CANDLES", 24)
MAX_RECENT_PUMP_PCT = _get_float("MAX_RECENT_PUMP_PCT", 0.15)

# Botun tarama dışında bırakacağı çift türleri (kaldıraçlı token'lar ve
# stablecoin-stablecoin çiftleri gibi "gürültü" oluşturan pariteler)
EXCLUDED_KEYWORDS = ["UP", "DOWN", "BULL", "BEAR"]
STABLECOINS = [
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "EUR", "GBP", "TRY",
    "PYUSD", "USDE", "FRAX", "USDD", "GUSD", "USDS", "USD1",
]

# Fiziksel emtia (altın, gümüş vb.) fiyatını takip eden "sentetik" tokenler.
# Bunlar kripto momentumuyla değil emtia piyasasıyla hareket eder - botun
# amacına (kripto piyasası analizi) uymaz. ("Altın istersen zaten altın alırsın.")
COMMODITY_COINS = [
    c.strip().upper() for c in os.getenv("COMMODITY_COINS", "PAXG,XAUT").split(",") if c.strip()
]

# Çökmüş/değer kaybetmiş projelerin "kalıntısı" olan, gerçek kazanç
# potansiyeli taşımayan ölü/zombi coinler (ör. USTC: Terra'nın çöken UST
# stablecoin'inin kalıntısı, LUNC: eski LUNA). Hacim/skor ne olursa olsun
# taranmaz - fiyatı görünüşte hareket etse bile gerçek bir toparlanma
# beklentisi yoktur.
DEAD_COINS = [
    c.strip().upper() for c in os.getenv("DEAD_COINS", "USTC,LUNC").split(",") if c.strip()
]

# Kullanıcının istediği belirli coinler - kaç puan alırsa alsın hiç
# taranmaz/alınmaz (ör. tekrarlanan zararlar sonrası elle eklenenler).
BLOCKED_COINS = [
    c.strip().upper() for c in os.getenv("BLOCKED_COINS", "").split(",") if c.strip()
]

# True ise, aşağıdaki MAJOR_COINS listesindeki "bilinen/büyük" coinler
# tamamen tarama dışı bırakılır - botun sadece altcoin'lerde işlem yapmasını
# gözlemlemek için kullanılır.
ALTCOINS_ONLY = _get_bool("ALTCOINS_ONLY", False)
MAJOR_COINS = [
    c.strip().upper() for c in os.getenv(
        "MAJOR_COINS", "BTC,ETH,BNB,SOL,XRP,ADA,DOGE,TRX,DOT,MATIC,LTC,AVAX,LINK,ATOM,SHIB,TON,BCH"
    ).split(",") if c.strip()
]

STATE_FILE = "state.json"
LOG_FILE = "trading_bot.log"
TRADE_HISTORY_FILE = "trade_history.jsonl"

# Binance TR'nin sandbox'ı yok - USE_TESTNET bayrağının orada hiçbir etkisi
# olmaz, her zaman gerçek hesap olarak kabul edilir. Panel/loglardaki "mod"
# göstergesi bu bayrağa bakar, USE_TESTNET'e değil - yoksa gerçek parayla
# çalışırken yanlışlıkla "TESTNET" gösterebilir.
IS_REAL_MONEY = (EXCHANGE == "binance_tr") or (not USE_TESTNET)

if IS_REAL_MONEY:
    print(
        "\n!!! UYARI: Bu bot GERÇEK PARA ile işlem yapacak. !!!\n"
        "Gerçek hesapta çalıştırmadan önce stratejiyi testnet'te uzun süre "
        "gözlemlemen şiddetle tavsiye edilir.\n"
    )
