"""
binance_tr_client.py
---------------------
Binance TR (binance.tr) için özel API istemcisi.

ÖNEMLİ: Binance TR, binance.com ile AYNI imzalama yöntemini (HMAC SHA256,
X-MBX-APIKEY header) kullanır ama TAMAMEN FARKLI uç noktalara sahiptir
(ör. emir verme: /open/v1/orders, binance.com'daki /api/v3/order değil).
Bu yüzden `python-binance` kütüphanesi burada çalışmaz; bu modül doğrudan
HTTP istekleriyle (requests) yazılmıştır.

DAHA DA ÖNEMLİSİ: Binance TR'nin bir test/sandbox ortamı YOKTUR. Buradaki
her çağrı GERÇEK hesaba karşı yapılır. Bu yüzden:
  1. Önce sadece OKUMA çağrılarıyla (bakiye, parite listesi) doğrulama yap
  2. Yanıt şeklini (JSON alan adları) gerçek bir çağrıyla teyit et - resmi
     dokümantasyon özet bilgi veriyor, birebir alan adlarını garanti etmez
  3. İlk gerçek emri (market_buy/market_sell) küçük bir tutarla ve elle
     onaylayarak dene

Resmi dokümantasyon: https://www.binance.tr/apidocs/
"""

import hashlib
import hmac
import logging
import time
from urllib.parse import urlencode

import pandas as pd
import requests

import config

logger = logging.getLogger("binance_bot")

BASE_URL = "https://www.binance.tr"
# Bazı genel piyasa verisi uçları (ör. klines) ayrı bir alan adından sunuluyor.
MARKET_DATA_URL = "https://api.binance.me"


class BinanceTRClient:
    def __init__(self):
        if not config.API_KEY or not config.API_SECRET:
            raise RuntimeError(
                "API anahtarları bulunamadı. .env dosyasını kontrol et "
                "(BINANCE_API_KEY / BINANCE_API_SECRET)."
            )
        self.api_key = config.API_KEY
        self.api_secret = config.API_SECRET
        self.session = requests.Session()
        self.session.headers.update({"X-MBX-APIKEY": self.api_key})
        logger.warning(
            "Binance TR istemcisi başlatıldı - GERÇEK HESAP (sandbox yok, her çağrı gerçektir)."
        )

    # ------------------------------------------------------------------
    # Düşük seviye HTTP + imzalama
    # ------------------------------------------------------------------
    def _signed_params(self, params: dict) -> dict:
        params = dict(params)
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _request(self, method: str, base: str, path: str, params: dict = None, signed: bool = False):
        params = params or {}
        if signed:
            params = self._signed_params(params)
        url = f"{base}{path}"
        try:
            resp = self.session.request(method, url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            logger.error("Binance TR API hatası [%s %s]: %s -> %s", method, path, e, resp.text[:500])
            raise
        except Exception as e:
            logger.error("Binance TR isteği başarısız [%s %s]: %s", method, path, e)
            raise

    def diagnostic_raw_call(self, method: str, path: str, params: dict = None, signed: bool = False, base: str = BASE_URL):
        """Yanıt şeklini (JSON alan adları) doğrulamak için ham yanıtı döndürür.
        Gerçek API anahtarı geldiğinde ilk olarak bununla test edilir."""
        return self._request(method, base, path, params, signed)

    # ------------------------------------------------------------------
    # Piyasa verisi
    # ------------------------------------------------------------------
    def get_try_pairs(self, include_majors: bool = False) -> list:
        """Taranabilir TRY paritelerinin listesini döndürür (ör. 'BTC_TRY').
        Binance.com'dan farklı olarak semboller alt çizgiyle ayrılır.
        `include_majors` parametresi BinanceClient.get_usdt_pairs ile aynı
        arayüzü sağlamak için var (bkz. o fonksiyonun docstring'i)."""
        data = self._request("GET", BASE_URL, "/open/v1/common/symbols")
        symbols = data.get("data", {}).get("list", [])
        pairs = []
        for s in symbols:
            symbol = s.get("symbol", "")
            base = s.get("baseAsset", "")
            quote = s.get("quoteAsset", "")
            if quote != "TRY":
                continue
            if any(kw in base for kw in config.EXCLUDED_KEYWORDS):
                continue
            if base in config.STABLECOINS or base in config.DEAD_COINS or base in config.COMMODITY_COINS:
                continue
            if base in config.BLOCKED_COINS:
                continue
            if not include_majors and config.ALTCOINS_ONLY and base in config.MAJOR_COINS:
                continue
            pairs.append(symbol)
        return pairs

    def _get_symbol_filters(self, symbol: str) -> dict:
        data = self._request("GET", BASE_URL, "/open/v1/common/symbols")
        symbols = data.get("data", {}).get("list", [])
        s = next((x for x in symbols if x.get("symbol") == symbol), None)
        if not s:
            return {}
        return {f["filterType"]: f for f in s.get("filters", [])}

    def get_min_notional(self, symbol: str, default: float = 10.0) -> float:
        """Bu paritedeki minimum işlem tutarını (TRY) döndürür."""
        try:
            notional = self._get_symbol_filters(symbol).get("NOTIONAL")
            if notional and notional.get("minNotional"):
                return float(notional["minNotional"])
        except Exception as e:
            logger.debug("Min notional alınamadı %s: %s", symbol, e)
        return default

    @staticmethod
    def _round_step(quantity: float, step_size: float) -> float:
        if step_size <= 0:
            return quantity
        precision = len(str(step_size).split(".")[1].rstrip("0")) if "." in str(step_size) else 0
        factor = 10 ** precision
        return int(quantity * factor) / factor

    def get_asset_balance(self, asset: str) -> float:
        """Belirli bir varlığın (TRY dışında, ör. 'ONE') gerçek serbest
        bakiyesini döndürür - satış öncesi gerçek miktarı doğrulamak için."""
        data = self._request("GET", BASE_URL, "/open/v1/account/spot", signed=True)
        assets = data.get("data", {}).get("accountAssets", [])
        for a in assets:
            if a.get("asset") == asset:
                return float(a.get("free", 0))
        return 0.0

    @staticmethod
    def _market_data_symbol(symbol: str) -> str:
        """Ticker/klines uçları (api.binance.me) bitişik sembol formatı
        bekler (ör. 'BTCTRY'); hesap/emir uçları (binance.tr) ise alt
        çizgili format kullanır (ör. 'BTC_TRY'). Bu dönüşümü burada
        yapıyoruz ki geri kalan kod tek tip (alt çizgili) formatla çalışsın."""
        return symbol.replace("_", "")

    def get_24h_ticker(self, symbol: str) -> dict:
        return self._request(
            "GET", MARKET_DATA_URL, "/api/v1/ticker/24hr",
            {"symbol": self._market_data_symbol(symbol)},
        )

    def get_klines_df(self, symbol: str, interval: str, limit: int = 200) -> pd.DataFrame:
        raw = self._request(
            "GET", MARKET_DATA_URL, "/api/v1/klines",
            {"symbol": self._market_data_symbol(symbol), "interval": interval, "limit": limit},
        )
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

    # ------------------------------------------------------------------
    # Hesap
    # ------------------------------------------------------------------
    def get_try_balance(self) -> float:
        data = self._request("GET", BASE_URL, "/open/v1/account/spot", signed=True)
        assets = data.get("data", {}).get("accountAssets", [])
        for a in assets:
            if a.get("asset") == "TRY":
                return float(a.get("free", 0))
        return 0.0

    # trader.py/risk_manager.py/dashboard.py "USDT" isimli genel metodları
    # çağırır (Binance.com istemcisiyle ortak arayüz); burada TRY karşılığına
    # yönlendiriyoruz ki bu dosyaların geri kalanı borsadan bağımsız kalsın.
    get_usdt_pairs = get_try_pairs
    get_usdt_balance = get_try_balance

    # ------------------------------------------------------------------
    # Emirler
    # ------------------------------------------------------------------
    @staticmethod
    def _order_succeeded(raw: dict) -> bool:
        """Binance TR, bakiye yetersizliği gibi İŞ MANTIĞI hatalarında bile
        HTTP 200 döner - hata sadece gövdedeki 'code' alanında görünür (ör.
        insufficient balance -> code=2202). resp.raise_for_status() bunu
        YAKALAMAZ. Gerçek bir emirle doğrulandı (2026-09-17): başarılı emir
        code=0 döner. Bu kontrol olmadan başarısız bir emir "başarılı"
        sanılıp pozisyon yanlışlıkla kapanmış işaretlenebilir - coin gerçekte
        hesapta kalırken bot onu "satıldı" sanır."""
        return isinstance(raw, dict) and raw.get("code") == 0

    def _normalize_order(self, raw: dict, symbol: str, fallback_price: float) -> dict:
        """Binance TR'nin emir yanıtı alan adları resmi dokümanla tam
        doğrulanmadı (sandbox olmadığı için ilk gerçek emirde teyit edilecek).
        Bu fonksiyon yanıtı BinanceClient (binance.com) ile aynı şekle
        (executedQty, fills) dönüştürmeye çalışır; başarısız olursa ham
        yanıtı olduğu gibi loglayıp güvenli bir varsayılana düşer, böylece
        alan adı farkı sessizce yanlış bir miktar/fiyatla devam etmez."""
        data = raw.get("data", raw) if isinstance(raw, dict) else raw
        try:
            executed_qty = float(
                data.get("executedQty") or data.get("origQty") or data.get("quantity") or 0
            )
            # Binance TR "fills" listesi değil, tek bir "executedPrice"
            # (ortalama gerçekleşme fiyatı) döndürüyor - gerçek bir işlemle
            # doğrulandı (2026-09-17). Bunu BinanceClient (binance.com) ile
            # aynı "fills" şekline çeviriyoruz ki trader.py değişmeden çalışsın.
            fills = data.get("fills")
            if not fills and data.get("executedPrice"):
                fills = [{"price": data["executedPrice"], "qty": str(executed_qty)}]
            if executed_qty > 0:
                return {"executedQty": executed_qty, "fills": fills or [], "raw": raw}
        except Exception:
            pass
        logger.warning(
            "%s emrinin yanıt şekli beklenenden farklı, ham yanıt: %s", symbol, raw
        )
        return {"executedQty": 0.0, "fills": [], "raw": raw}

    def market_buy(self, symbol: str, quote_amount: float):
        """quote_amount kadar TRY ile piyasa fiyatından alım yapar.

        UYARI: Binance TR'nin sandbox'ı yok - bu fonksiyon GERÇEK parayla
        GERÇEK emir verir. Yanıt şekli ilk canlı çağrıda teyit edilmelidir."""
        try:
            params = {
                "symbol": symbol,
                "side": 0,  # 0 = BUY (Binance TR sayısal enum kullanıyor)
                "type": 2,  # 2 = MARKET
                "quoteOrderQty": round(quote_amount, 2),
            }
            raw = self._request("POST", BASE_URL, "/open/v1/orders", params, signed=True)
            if not self._order_succeeded(raw):
                logger.error("Alım BAŞARISIZ (%s): %s", symbol, raw)
                return None
            logger.info("ALIM emri gönderildi (Binance TR): %s -> %s", symbol, raw)
            return self._normalize_order(raw, symbol, fallback_price=0.0)
        except Exception as e:
            logger.error("Alım hatası (%s): %s", symbol, e)
            return None

    def market_sell(self, symbol: str, quantity: float):
        """UYARI: Binance TR'nin sandbox'ı yok - bu fonksiyon GERÇEK parayla
        GERÇEK emir verir.

        `quantity` (bizim kayıtlı miktarımız) her zaman GERÇEK bakiyeyle
        sınırlanır: bir ALIM'da komisyon TRY değil, alınan coin'den kesilir
        (gerçek bir işlemle doğrulandı, 2026-09-17), yani kayıtlı miktar
        gerçekte elde bulunandan her zaman biraz FAZLA olur. Bunu hesaba
        katmazsak Binance "yetersiz bakiye" hatasıyla emri reddeder ve bot
        pozisyonu asla kapatamaz."""
        try:
            base_asset = symbol.split("_")[0]
            real_balance = self.get_asset_balance(base_asset)
            sell_qty = min(quantity, real_balance)

            lot = self._get_symbol_filters(symbol).get("LOT_SIZE")
            if lot and lot.get("stepSize"):
                sell_qty = self._round_step(sell_qty, float(lot["stepSize"]))

            if sell_qty <= 0:
                logger.error(
                    "%s satılamadı: gerçek bakiye yetersiz/sıfır (kayıtlı: %s, gerçek: %s).",
                    symbol, quantity, real_balance,
                )
                return None
            if sell_qty < quantity:
                logger.info(
                    "%s satış miktarı gerçek bakiyeye göre ayarlandı: %s -> %s (komisyon coin'den kesilmiş olabilir).",
                    symbol, quantity, sell_qty,
                )

            params = {
                "symbol": symbol,
                "side": 1,  # 1 = SELL
                "type": 2,  # 2 = MARKET
                "quantity": sell_qty,
            }
            raw = self._request("POST", BASE_URL, "/open/v1/orders", params, signed=True)
            if not self._order_succeeded(raw):
                logger.error("Satım BAŞARISIZ (%s): %s - pozisyon AÇIK kalmaya devam ediyor.", symbol, raw)
                return None
            logger.info("SATIM emri gönderildi (Binance TR): %s -> %s", symbol, raw)
            return raw
        except Exception as e:
            logger.error("Satım hatası (%s): %s", symbol, e)
            return None
