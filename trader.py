"""
trader.py
---------
Botun ana döngüsü: piyasayı tarar, fırsatları puanlar, açık pozisyonları
stop-loss/take-profit için kontrol eder, kurallara uygunsa yeni işlem açar.
"""

import logging
import threading
import time

import config
import news_sentiment
import strategy
from risk_manager import RiskManager

logger = logging.getLogger("binance_bot")


class Trader:
    def __init__(self):
        if config.EXCHANGE == "binance_tr":
            from binance_tr_client import BinanceTRClient
            client_cls = BinanceTRClient
            self.min_volume = config.MIN_24H_VOLUME_TRY
            self.quote_label = "TRY"
            self.min_balance_floor = 20  # TRY - Binance TR'de tipik minNotional 10 TRY
        else:
            from binance_client import BinanceClient
            client_cls = BinanceClient
            self.min_volume = config.MIN_24H_VOLUME_USDT
            self.quote_label = "USDT"
            self.min_balance_floor = 5  # USDT

        self.client = client_cls()
        # Pozisyon kontrolü artık ayrı bir thread'de (main.py) çalışıyor.
        # HTTP oturumları (requests.Session) thread-güvenli olmadığından,
        # tam tarama thread'iyle aynı istemciyi paylaşmak yerine ayrı bir
        # istemci kullanıyoruz - iki thread aynı anda ağ çağrısı yapabilir.
        self.position_client = client_cls()
        self.risk = RiskManager()

    # ------------------------------------------------------------------
    def scan_market(self):
        """Tüm paritelerini tarar, resmi haber kaynaklarını duygu analizinden
        geçirir ve puanlanmış fırsat listesini döndürür. Dönen tuple:
        (sıralı fırsat listesi, genel piyasa duygu skoru)."""
        symbols = self.client.get_usdt_pairs()
        logger.info("%d %s paritesi taranacak...", len(symbols), self.quote_label)

        # Haber eşleştirmesi büyük coinler dahil TÜM paritelere karşı yapılır
        # (genel kripto haberleri ağırlıklı BTC/ETH/SOL'dan bahseder) - ama
        # işlem kararları yine sadece `symbols` (altcoin) listesiyle sınırlıdır.
        news_symbols = self.client.get_usdt_pairs(include_majors=True)
        symbol_sentiment, market_sentiment = news_sentiment.refresh(news_symbols, self.client)
        if market_sentiment:
            logger.info("Genel piyasa haber duyarlılığı: %+.2f", market_sentiment)

        opportunities = []
        for symbol in symbols:
            try:
                ticker = self.client.get_24h_ticker(symbol)
                volume = float(ticker["quoteVolume"])
                if volume < self.min_volume:
                    continue  # likiditesi düşük, atla

                price_now = float(ticker["lastPrice"])
                if config.MAX_COIN_PRICE and price_now > config.MAX_COIN_PRICE:
                    continue  # birim fiyatı çok yüksek, atla

                day_high = float(ticker.get("highPrice", 0))
                day_low = float(ticker.get("lowPrice", 0))
                day_range_position = (
                    (price_now - day_low) / (day_high - day_low) if day_high > day_low else 0.5
                )

                df = self.client.get_klines_df(symbol, config.KLINE_INTERVAL, limit=200)
                opp = strategy.score_symbol(
                    symbol, df,
                    sentiment_score=symbol_sentiment.get(symbol, 0.0),
                    day_range_position=day_range_position,
                )
                if opp:
                    opportunities.append(opp)
            except Exception as e:
                logger.debug("Atlanıyor %s: %s", symbol, e)
                continue

            time.sleep(0.05)  # API rate limitine takılmamak için küçük bekleme

        ranked = strategy.rank_opportunities(opportunities)
        logger.info("Tarama tamamlandı. %d geçerli fırsat bulundu.", len(ranked))
        return ranked, market_sentiment

    # ------------------------------------------------------------------
    def refresh_news(self):
        """Haberleri tam piyasa taramasından bağımsız, çok daha sık yeniler
        (bkz. NEWS_REFRESH_INTERVAL_MINUTES). Sadece parite listesini ve RSS
        beslemelerini okur - ticker/mum verisi çekmediği için hızlıdır."""
        try:
            symbols = self.client.get_usdt_pairs(include_majors=True)
            symbol_sentiment, market_sentiment = news_sentiment.refresh(symbols, self.client)
            logger.info(
                "Haber yenileme tamamlandı. Takip edilen %d parite, genel piyasa duygu skoru: %+.2f",
                len(symbol_sentiment), market_sentiment,
            )
        except Exception as e:
            logger.warning("Haber yenileme başarısız: %s", e)

    # ------------------------------------------------------------------
    def manage_open_positions(self):
        """Açık pozisyonları stop-loss/take-profit için kontrol eder.
        `position_client` kullanır (bkz. __init__) - tam tarama thread'iyle
        aynı anda çalışabildiği için ayrı bir HTTP istemcisi gerekir."""
        for symbol in list(self.risk.open_positions().keys()):
            try:
                ticker = self.position_client.get_24h_ticker(symbol)
                price = float(ticker["lastPrice"])
            except Exception as e:
                logger.error("Fiyat alınamadı %s: %s", symbol, e)
                continue

            exit_reason = self.risk.check_exit_conditions(symbol, price)
            if exit_reason:
                pos = self.risk.open_positions().get(symbol)
                if not pos:
                    continue
                order = self.position_client.market_sell(symbol, pos["quantity"])
                if order:
                    pnl = self.risk.register_sell(symbol, price, reason=exit_reason)
                    logger.info(
                        "%s pozisyonu kapatıldı (%s). PnL: %.2f %s",
                        symbol, exit_reason, pnl, self.quote_label,
                    )
                    # Pozisyon kapandı - 30 dakikalık zamanlanmış taramayı
                    # beklemeden yeni fırsatlara HEMEN bak. Tam tarama
                    # dakikalarca sürebileceği için ayrı bir thread'de
                    # çalıştırılır ki bu (hızlı) pozisyon kontrol döngüsü
                    # bloklanmasın.
                    logger.info("Sermaye serbest kaldı, yeni fırsatlar için hemen tarama başlatılıyor...")
                    threading.Thread(target=self.run_once, daemon=True, name="post-sell-scan").start()

    # ------------------------------------------------------------------
    def open_new_positions(self, opportunities, min_score: float = None):
        """Skoru eşiğin üzerinde olan ve henüz pozisyon açılmamış fırsatlar için alım yapar."""
        if min_score is None:
            min_score = config.MIN_OPPORTUNITY_SCORE
        balance = self.client.get_usdt_balance()
        if balance < self.min_balance_floor:
            logger.warning("Bakiye çok düşük (%.2f %s), yeni pozisyon açılamıyor.", balance, self.quote_label)
            return

        if not self.risk.can_trade_today(balance):
            return

        for opp in opportunities:
            if not self.risk.can_open_new_position():
                break  # gerçek sebep (limit/bekleme süresi) RiskManager içinde zaten loglandı

            if opp.score < min_score:
                break  # liste zaten skora göre sıralı, devamı daha düşük

            if self.risk.has_position(opp.symbol):
                continue

            if self.risk.symbol_on_cooldown(opp.symbol):
                continue  # bu coin yakın zamanda satıldı - başka bir fırsata bak

            if self.risk.symbol_blocked_by_losses(opp.symbol):
                continue  # bugün art arda 2+ kez stop-loss yedi - bugün için tamamen elendi

            quote_amount = self.risk.position_size(balance)
            # Sabit bir "10 USDT" varsayımı yerine paritenin gerçek minimum
            # işlem tutarını sorgula: küçük coinlerde bu genelde daha düşüktür,
            # bu yüzden küçük sermayeyle de birçok altcoin işleme uygun kalır.
            min_notional = self.client.get_min_notional(opp.symbol)
            if quote_amount < min_notional:
                logger.debug(
                    "%s atlanıyor: işlem tutarı (%.2f %s) minimumun (%.2f %s) altında.",
                    opp.symbol, quote_amount, self.quote_label, min_notional, self.quote_label,
                )
                continue  # bu parite için yetmedi ama minimumu daha düşük başka bir fırsat olabilir

            logger.info(
                "AÇILIYOR: %s | skor=%.1f | fiyat=%.6f | gerekçe: %s",
                opp.symbol, opp.score, opp.price, opp.reason,
            )
            order = self.client.market_buy(opp.symbol, quote_amount)
            if order:
                executed_qty = float(order["executedQty"])
                fills = order.get("fills", [])
                avg_price = (
                    sum(float(f["price"]) * float(f["qty"]) for f in fills) / executed_qty
                    if fills and executed_qty > 0
                    else opp.price
                )
                self.risk.register_buy(opp.symbol, avg_price, executed_qty, reason=opp.reason)
                balance -= quote_amount

    # ------------------------------------------------------------------
    def run_once(self):
        logger.info("=" * 60)
        logger.info("Yeni tarama döngüsü başlıyor (piyasa + haber analizi)...")
        self.manage_open_positions()
        opportunities, market_sentiment = self.scan_market()

        if opportunities:
            top = opportunities[:10]
            logger.info("En iyi 10 fırsat:")
            for o in top:
                logger.info("  %-12s skor=%5.1f  fiyat=%.6f", o.symbol, o.score, o.price)

        if market_sentiment <= config.NEGATIVE_SENTIMENT_VETO:
            logger.warning(
                "Genel piyasa haber duyarlılığı çok olumsuz (%.2f) - bu turda yeni pozisyon açılmayacak.",
                market_sentiment,
            )
        else:
            self.open_new_positions(opportunities)
        logger.info("Döngü tamamlandı.")
