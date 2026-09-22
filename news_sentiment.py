"""
news_sentiment.py
------------------
Resmi haber ajansı, borsa duyurusu ve düzenleyici kurum RSS beslemelerini
tarar, daha önce görülmemiş içerikleri sentiment.py ile Claude'a analiz
ettirir ve her parite için (ayrıca genel piyasa için) zamanla ağırlığı azalan
("decay") bir duygu skoru üretir.

Not: X (Twitter) API'si ücretli olduğu için kapsam dışı bırakıldı - bunun
yerine resmi haber ajansları ve düzenleyici kurumların RSS beslemeleri
kullanılıyor. .env dosyasındaki NEWS_RSS_FEEDS listesine virgülle ayırarak
kendi bulduğun ek/güncel kaynakları (ör. bir borsanın duyuru sayfası) ekleyebilirsin.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import feedparser

import config
import sentiment

logger = logging.getLogger("binance_bot")

NEWS_STATE_FILE = "news_state.json"

# Sembol -> tam isim eşlemesi. RSS metinlerinde hem "BTC" hem "Bitcoin" geçebilir.
# Sadece sembolün kendisiyle (büyük harf + kelime sınırı) eşleştirmek bile
# kısa sembollerde (ör. "ONE") yanlış eşleşme riski taşır; bilinen büyük
# coinler için tam isim de ayrıca kontrol edilir.
SYMBOL_ALIASES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "BNB": "Binance Coin", "SOL": "Solana",
    "XRP": "Ripple", "ADA": "Cardano", "DOGE": "Dogecoin", "AVAX": "Avalanche",
    "DOT": "Polkadot", "LINK": "Chainlink", "MATIC": "Polygon", "LTC": "Litecoin",
    "TRX": "Tron", "SHIB": "Shiba Inu", "ATOM": "Cosmos", "UNI": "Uniswap",
    "ETC": "Ethereum Classic", "XLM": "Stellar", "NEAR": "Near Protocol",
    "APT": "Aptos", "ARB": "Arbitrum", "OP": "Optimism", "FIL": "Filecoin",
    # Altcoin kapsamını genişletmek için ek eşlemeler (bu listedeki coinler
    # ALTCOINS_ONLY modunda MAJOR_COINS'te olmadıkları sürece taranabilir)
    "ZEC": "Zcash", "GRT": "The Graph", "SAND": "Sandbox", "MANA": "Decentraland",
    "AXS": "Axie Infinity", "CHZ": "Chiliz", "ENJ": "Enjin", "GALA": "Gala",
    "IMX": "Immutable", "APE": "ApeCoin", "LDO": "Lido", "CRV": "Curve",
    "SNX": "Synthetix", "COMP": "Compound", "MKR": "Maker", "AAVE": "Aave",
    "RUNE": "Thorchain", "FTM": "Fantom", "ALGO": "Algorand", "VET": "VeChain",
    "HBAR": "Hedera", "EGLD": "MultiversX", "FLOW": "Flow", "KAS": "Kaspa",
    "PEPE": "Pepe", "WIF": "dogwifhat", "BONK": "Bonk", "FLOKI": "Floki",
    "JUP": "Jupiter", "PYTH": "Pyth Network", "STRK": "Starknet", "SUI": "Sui",
    "SEI": "Sei", "TIA": "Celestia", "INJ": "Injective", "ORDI": "Ordinals",
    "WLD": "Worldcoin", "PENDLE": "Pendle", "ENS": "Ethereum Name Service",
    "1INCH": "1inch", "DYDX": "dYdX", "CFX": "Conflux", "MASK": "Mask Network",
}

# Kaynak başlığında bu ifadelerden biri geçiyorsa haber "genel piyasa"yı
# etkiler kabul edilir ve tüm paritelere uygulanır (ör. Fed faiz kararı, SEC).
MARKET_WIDE_SOURCE_HINTS = ("sec", "federal reserve", "securities and exchange")

# Genel İngilizce kelime/kısaltmalarla çakışan ticker'lar (ör. "ATM" hem bir
# coin hem "bankamatik" demek). Bunlar için çıplak sembol eşleşmesi kapatılır,
# sadece tam proje ismi (SYMBOL_ALIASES) geçiyorsa eşleşme kabul edilir.
AMBIGUOUS_TICKERS = {"ATM", "ONE", "FOR", "KEY", "WIN", "GAS", "ICE", "FUN", "HOT", "BAT", "TRY", "ANT", "JOE"}


def _load_state() -> dict:
    if os.path.exists(NEWS_STATE_FILE):
        with open(NEWS_STATE_FILE, "r") as f:
            state = json.load(f)
        state.setdefault("seen_links", [])
        state.setdefault("symbol_sentiment", {})
        state.setdefault("market_sentiment", {"score": 0.0, "updated": None})
        state.setdefault("news_feed", [])
        return state
    return {
        "seen_links": [], "symbol_sentiment": {},
        "market_sentiment": {"score": 0.0, "updated": None},
        "news_feed": [],  # panelde gösterilen "şu coin hakkında şu haber" akışı
    }


def _save_state(state: dict):
    with open(NEWS_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _fetch_entries() -> list:
    """Tüm RSS kaynaklarını okur, son birkaç tarama aralığı içindeki
    (veya tarihsiz) girdileri döndürür."""
    entries = []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max(config.SCAN_INTERVAL_MINUTES * 3, 180))

    for feed_url in config.NEWS_RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
            source = parsed.feed.get("title", feed_url)
            for e in parsed.entries[:30]:
                link = e.get("link") or e.get("id") or e.get("title")
                if not link:
                    continue
                if e.get("published_parsed"):
                    published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
                    if published < cutoff:
                        continue
                text = f"{e.get('title', '')}. {e.get('summary', '')}"[:600]
                entries.append({"link": link, "text": text, "source": source})
        except Exception as ex:
            logger.debug("RSS okunamadı (%s): %s", feed_url, ex)
    return entries


def _base_asset(symbol: str) -> str:
    """Bir parite sembolünden taban varlığı çıkarır. Binance.com bitişik
    (ör. 'BTCUSDT') Binance TR ise alt çizgili (ör. 'BTC_TRY') format kullanır."""
    if "_" in symbol:
        return symbol.split("_")[0]
    return symbol.replace("USDT", "")


def _match_symbols(text: str, symbols: list) -> list:
    matched = []
    for sym in symbols:
        base = _base_asset(sym)
        if len(base) < 3:
            continue  # çok kısa semboller yanlış eşleşmeye fazla açık
        alias = SYMBOL_ALIASES.get(base)

        if base in AMBIGUOUS_TICKERS:
            # Genel bir kelimeyle çakışıyor - sadece tam proje ismiyle eşleş.
            if alias and alias.lower() in text.lower():
                matched.append(sym)
            continue

        pattern = r"\b" + re.escape(base) + r"\b"
        if re.search(pattern, text) or (alias and alias.lower() in text.lower()):
            matched.append(sym)
    return matched


def refresh(symbols: list, client=None) -> tuple:
    """RSS kaynaklarını tarar, yeni içerikleri analiz eder, duygu durumunu günceller.

    `client` verilirse (BinanceClient), ilgili coin(ler)in o anki fiyatı da
    kaydedilir - panel daha sonra "haber çıktığından beri fiyat %X değişti"
    bilgisini gösterebilsin diye.

    Döndürülen: (symbol -> sentiment_score dict, market_wide_sentiment_score)
    """
    state = _load_state()
    seen = set(state["seen_links"])
    entries = _fetch_entries()
    new_entries = [e for e in entries if e["link"] not in seen]

    if new_entries:
        logger.info("%d yeni haber/duyuru bulundu, duygu analizi yapılıyor...", len(new_entries))
        batch = [{"id": i, "text": e["text"]} for i, e in enumerate(new_entries)]
        results = sentiment.analyze_batch(batch)

        now_iso = datetime.now(timezone.utc).isoformat()
        for i, e in enumerate(new_entries):
            seen.add(e["link"])
            res = results.get(i)
            if res is None:
                continue
            score = res["score"]
            logger.info("  [%s] %+.2f - %s (%s)", e["source"], score, e["text"][:80], res["reason"])

            is_market_wide = any(hint in e["source"].lower() for hint in MARKET_WIDE_SOURCE_HINTS)
            if is_market_wide:
                old = state["market_sentiment"]["score"]
                state["market_sentiment"] = {"score": round(old * 0.5 + score * 0.5, 3), "updated": now_iso}

            matched = _match_symbols(e["text"], symbols)
            for sym in matched:
                old = state["symbol_sentiment"].get(sym, {"score": 0.0, "updated": now_iso})["score"]
                state["symbol_sentiment"][sym] = {"score": round(old * 0.4 + score * 0.6, 3), "updated": now_iso}

            # Panelin "şu coin hakkında şu haber" şeklinde gösterebilmesi için
            # ham haberi de (kaynak, ilgili coin(ler), skor, gerekçe) sakla.
            feed_symbols = matched or (["GENEL PİYASA"] if is_market_wide else [])

            # Haber tespit edildiği andaki fiyatı da kaydet - panel sonradan
            # "haberden sonra fiyat %X değişti" diyebilsin diye.
            price_at_news = {}
            if client and matched:
                for sym in matched:
                    try:
                        price_at_news[sym] = float(client.get_24h_ticker(sym)["lastPrice"])
                    except Exception:
                        pass

            if feed_symbols:
                state["news_feed"].append({
                    "time": now_iso,
                    "source": e["source"],
                    "text": e["text"],
                    "symbols": feed_symbols,
                    "price_at_news": price_at_news,
                    "score": score,
                    "reason": res["reason"],
                    "method": res.get("method", "llm"),
                })
    else:
        logger.debug("Yeni haber/duyuru bulunamadı.")

    # Eski duygu verilerinin etkisini zamanla azalt ("decay"); 48 saatten
    # eskisi tamamen silinir, 6-48 saat arası doğrusal olarak sıfıra yaklaşır.
    for sym in list(state["symbol_sentiment"].keys()):
        data = state["symbol_sentiment"][sym]
        decayed = _decayed_score(data["score"], data["updated"])
        if decayed is None:
            del state["symbol_sentiment"][sym]
        else:
            state["symbol_sentiment"][sym]["score"] = decayed

    state["seen_links"] = list(seen)[-500:]  # dosya sınırsız büyümesin
    state["news_feed"] = state["news_feed"][-200:]  # akış da sınırsız büyümesin
    _save_state(state)

    symbol_scores = {sym: d["score"] for sym, d in state["symbol_sentiment"].items()}
    return symbol_scores, state["market_sentiment"]["score"]


def _decayed_score(score: float, updated_iso: str):
    """Bir skora, üretildiği andan bu yana geçen süreye göre sönümleme uygular.
    48 saatten eskiyse None (tamamen etkisiz/silinmeli) döner. Salt matematik -
    ağ çağrısı yapmaz, sık çağrılması güvenlidir."""
    age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(updated_iso)).total_seconds() / 3600
    if age_hours > 48:
        return None
    if age_hours > 6:
        decay = max(0.0, 1 - (age_hours - 6) / 42)
        return round(score * decay, 3)
    return score


def get_live_sentiment() -> tuple:
    """Depolanmış duygu verisini okuyup ANLIK zaman sönümlemesini uygulayarak
    döndürür - RSS/LLM çağrısı yapmaz, sadece matematik. Panelin sık sık
    (ör. 8 saniyede bir) çağırması için güvenlidir; refresh()'ten farklı olarak
    yeni haber ARAMAZ, sadece var olan skorları "şu ana göre" günceller.

    Döndürülen: (symbol -> canlı skor, genel piyasa skoru)
    """
    state = _load_state()
    live = {}
    for sym, data in state.get("symbol_sentiment", {}).items():
        score = _decayed_score(data["score"], data["updated"])
        if score is not None:
            live[sym] = score
    market = state.get("market_sentiment", {}).get("score", 0.0)
    return live, market
