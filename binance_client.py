"""
binance_client.py
------------------
Binance API ile tüm iletişimi tek bir yerden yönetir.
Not: Bot hesabına ASLA kullanıcı adı/şifre ile "giriş" yapmaz.
Binance API anahtarları (API key + secret) kullanılır. Bu anahtarları
oluştururken "Withdraw" (para çekme) iznini KAPALI bırakmalısın; bot
sadece "Enable Spot Trading" iznine ihtiyaç duyar.
"""

import logging
import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException

import config

logger = logging.getLogger("binance_bot")


class BinanceClient:
    def __init__(self):
        if not config.API_KEY or not config.API_SECRET:
            raise RuntimeError(
                "API anahtarları bulunamadı. .env dosyasını kontrol et "
                "(BINANCE_API_KEY / BINANCE_API_SECRET)."
            )

        self.client = Client(
            config.API_KEY,
            config.API_SECRET,
            testnet=config.USE_TESTNET,
        )
        logger.info(
            "Binance istemcisi başlatıldı (mod: %s)",
            "TESTNET" if config.USE_TESTNET else "GERÇEK HESAP",
        )

    # ------------------------------------------------------------------
    # Piyasa verisi
    # ------------------------------------------------------------------
    def get_usdt_pairs(self, include_majors: bool = False):
        """Taranabilir USDT paritelerinin listesini döndürür (kaldıraçlı
        token'lar ve stablecoin-stablecoin çiftleri hariç).

        ALTCOINS_ONLY açıksa MAJOR_COINS listesindeki bilinen/büyük coinler
        normalde dışarıda bırakılır (alım-satım kararları için kullanılan liste
        budur). `include_majors=True` verilirse bu filtre uygulanmaz - haber
        eşleştirmesi gibi sadece "bilgi amaçlı, işlem açmayan" kullanımlar için:
        genel kripto haberleri ağırlıklı olarak BTC/ETH/SOL gibi büyük
        coinlerden bahsettiğinden, haber akışının boş kalmaması için bu
        coinlerin de eşleştirmeye dahil edilmesi gerekir."""
        info = self.client.get_exchange_info()
        pairs = []
        for s in info["symbols"]:
            symbol = s["symbol"]
            base = s["baseAsset"]
            quote = s["quoteAsset"]

            if quote != "USDT" or s["status"] != "TRADING":
                continue
            if any(kw in base for kw in config.EXCLUDED_KEYWORDS):
                continue
            if base in config.STABLECOINS:
                continue
            if base in config.DEAD_COINS:
                continue  # çökmüş projelerin kalıntısı - gerçek kazanç potansiyeli yok
            if base in config.COMMODITY_COINS:
                continue  # altın/emtia takipçisi - kripto momentum stratejisiyle alakasız
            if not include_majors and config.ALTCOINS_ONLY and base in config.MAJOR_COINS:
                continue

            pairs.append(symbol)
        return pairs

    def get_24h_ticker(self, symbol: str):
        return self.client.get_ticker(symbol=symbol)

    def get_klines_df(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        """Mum verilerini pandas DataFrame olarak döndürür."""
        raw = self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(
            raw,
            columns=[
                "open_time", "open", "high", "low", "close", "volume",
                "close_time", "quote_asset_volume", "num_trades",
                "taker_buy_base", "taker_buy_quote", "ignore",
            ],
        )
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        return df

    def get_symbol_filters(self, symbol: str):
        """LOT_SIZE ve MIN_NOTIONAL gibi işlem kurallarını döndürür."""
        info = self.client.get_symbol_info(symbol)
        filters = {f["filterType"]: f for f in info["filters"]}
        return filters

    def get_min_notional(self, symbol: str, default: float = 5.0) -> float:
        """Bu paritede geçerli minimum işlem tutarını (USDT) döndürür.
        Binance parite bazında farklı minimumlar kullanır (genelde 5-10 USDT
        arası); sabit bir sayı varsaymak yerine gerçek değeri sorgulamak,
        küçük sermayeyle de mümkün olan pariteleri (genelde küçük coinlerde
        minimum daha düşüktür) gözden kaçırmamak için önemlidir."""
        try:
            filters = self.get_symbol_filters(symbol)
            f = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
            if f and f.get("minNotional"):
                return float(f["minNotional"])
        except Exception as e:
            logger.debug("Min notional alınamadı %s: %s", symbol, e)
        return default

    # ------------------------------------------------------------------
    # Hesap
    # ------------------------------------------------------------------
    def get_usdt_balance(self) -> float:
        balance = self.client.get_asset_balance(asset="USDT")
        return float(balance["free"]) if balance else 0.0

    def get_open_positions(self):
        """Sıfırdan büyük bakiyesi olan varlıkları (USDT hariç) döndürür."""
        account = self.client.get_account()
        positions = {}
        for b in account["balances"]:
            free = float(b["free"])
            locked = float(b["locked"])
            if b["asset"] != "USDT" and (free + locked) > 0:
                positions[b["asset"]] = free + locked
        return positions

    # ------------------------------------------------------------------
    # Emirler
    # ------------------------------------------------------------------
    def round_step_size(self, quantity: float, step_size: float) -> float:
        precision = len(str(step_size).split(".")[1].rstrip("0")) if "." in str(step_size) else 0
        factor = 10 ** precision
        return int(quantity * factor) / factor

    def market_buy(self, symbol: str, quote_amount: float):
        """quote_amount kadar USDT ile piyasa fiyatından alım yapar."""
        try:
            order = self.client.order_market_buy(
                symbol=symbol,
                quoteOrderQty=round(quote_amount, 2),
            )
            logger.info("ALIM emri gönderildi: %s -> %s", symbol, order)
            return order
        except BinanceAPIException as e:
            logger.error("Alım hatası (%s): %s", symbol, e)
            return None

    def market_sell(self, symbol: str, quantity: float):
        try:
            order = self.client.order_market_sell(symbol=symbol, quantity=quantity)
            logger.info("SATIM emri gönderildi: %s -> %s", symbol, order)
            return order
        except BinanceAPIException as e:
            logger.error("Satım hatası (%s): %s", symbol, e)
            return None
