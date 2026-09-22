"""
main.py
-------
Botu başlatan dosya. Çalıştırmak için:

    python main.py

Bot, config.py / .env içindeki SCAN_INTERVAL_MINUTES ayarına göre
periyodik olarak piyasayı tarar. Durdurmak için Ctrl+C kullan.
"""

import logging
import threading
import time

import schedule

import config
from trader import Trader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("binance_bot")


def position_check_loop(trader: Trader):
    """Açık pozisyonları (stop-loss/take-profit/kâr koruması) SÜREKLİ,
    kendi ayrı thread'inde kontrol eder.

    KRİTİK: Bu döngü tam piyasa taramasından (dakikalarca sürebilir) TAMAMEN
    BAĞIMSIZDIR. Daha önce pozisyon kontrolü ana thread'deki zamanlayıcıyla
    aynı sıraya giriyordu; tam tarama çalışırken pozisyon kontrolü tamamen
    donuyordu (gerçek parada bir kâr koruması tetiklenmesi gereken an
    tamamen kaçırılmıştı). Artık tarama ne kadar sürerse sürsün, bu thread
    yoluna devam eder.
    """
    while True:
        try:
            trader.manage_open_positions()
        except Exception as e:
            logger.error("Pozisyon kontrol thread'inde hata: %s", e)
        time.sleep(config.POSITION_CHECK_INTERVAL_SECONDS)


def main():
    logger.info(
        "Bot başlatılıyor... (borsa: %s, mod: %s)",
        config.EXCHANGE, "GERÇEK HESAP" if config.IS_REAL_MONEY else "TESTNET",
    )
    trader = Trader()

    # Pozisyon kontrolünü İLK TARAMADAN ÖNCE, ayrı bir thread'de başlat -
    # böylece tam tarama sürerken bile açık pozisyonlar kesintisiz izlenir.
    position_thread = threading.Thread(
        target=position_check_loop, args=(trader,), daemon=True, name="position-check",
    )
    position_thread.start()
    logger.info("Pozisyon kontrol thread'i başlatıldı (her %d sn).", config.POSITION_CHECK_INTERVAL_SECONDS)

    # İlk turu hemen yap: tam piyasa/haber taraması (pozisyon kontrolü zaten
    # yukarıdaki thread'de ayrıca ve sürekli çalışıyor). Geçici bir ağ hatası
    # (ör. "connection reset") burada yakalanmazsa TÜM SÜRECİ (pozisyon
    # kontrol thread'i dahil) çökertir - gerçek bir olayla doğrulandı
    # (2026-09-17). Asla process'i öldürmesin.
    try:
        trader.run_once()
    except Exception as e:
        logger.error("İlk tarama sırasında hata (devam ediliyor): %s", e)

    # İki bağımsız zamanlanmış döngü: haber yenileme (orta sıklıkta, panel
    # güncel kalsın) ve tam piyasa taraması + yeni işlem açma (bilerek
    # seyrek - amaç komisyonları eritmeden az ama seçici işlem yapmak).
    schedule.every(config.NEWS_REFRESH_INTERVAL_MINUTES).minutes.do(trader.refresh_news)
    schedule.every(config.SCAN_INTERVAL_MINUTES).minutes.do(trader.run_once)

    logger.info(
        "Bot çalışıyor. Pozisyon kontrolü: %d sn (ayrı thread), haber yenileme: %d dk, "
        "tam piyasa taraması: %d dk. Durdurmak için Ctrl+C.",
        config.POSITION_CHECK_INTERVAL_SECONDS, config.NEWS_REFRESH_INTERVAL_MINUTES, config.SCAN_INTERVAL_MINUTES,
    )

    while True:
        try:
            schedule.run_pending()
        except Exception as e:
            # Zamanlanmış işlerden (tam tarama, haber yenileme) sızan HERHANGİ
            # bir hata burada durdurulur - bot tek bir geçici ağ hatasıyla
            # tamamen ölmesin (pozisyon kontrol thread'i canlı kalsın).
            logger.error("Zamanlanmış işte beklenmeyen hata (bot çalışmaya devam ediyor): %s", e)
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot kullanıcı tarafından durduruldu.")
