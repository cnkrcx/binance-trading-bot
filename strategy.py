"""
strategy.py
-----------
"Her şeyi hesaba kat" isteğine karşılık gelen kompozit skorlama motoru.
Tek bir indikatöre güvenmek yerine, birden fazla sinyali ağırlıklandırarak
birleştirir ve her parite için 0-100 arası bir "fırsat skoru" üretir.

Bileşenler:
  1. Trend (EMA20 vs EMA50)        -> yön belirler
  2. RSI                            -> aşırı alım/satım bölgeleri; RSI 60-78
                                        arası toplam skor kademeli düşürülür,
                                        78 üstü tamamen elenir (aşırı-aşırı alım)
  3. MACD histogram momentumu       -> momentum artıyor mu azalıyor mu
  4. Hacim artışı                   -> ilgi/likidite teyidi
  5. Volatilite filtresi (ATR%)     -> çok durgun ya da çok riskli coinleri eler
  6. Haber/duygu analizi (news_sentiment.py + sentiment.py) -> resmi haber
     kaynaklarından Claude ile çıkarılan duygu skoru; güçlü olumsuz haberde
     (hack, dava, yasak) parite teknik skordan bağımsız tamamen elenir.
  7. "Zaten pompalanmış" vetosu     -> son PUMP_LOOKBACK_CANDLES mumda fiyat
                                        MAX_RECENT_PUMP_PCT'ten fazla yükseldiyse
                                        tamamen elenir - devam eden bir hareketin
                                        tepesini kovalamamak için.
  8. Volatilite bonusu (0-30 puan)  -> izin verilen aralıkta bile daha
                                        "hareketli" coinler tercih edilir -
                                        neredeyse hiç kıpırdamayan coinlerde
                                        sermaye saatlerce beklemeye alınmasın.

Bu bir "kutsal kâse" strateji değildir; kripto piyasaları tahmin edilemez
şekilde hareket edebilir. Skor yüksek olsa bile kayıp riski her zaman vardır.
Hiçbir kombinasyon her işlemde kâr garanti etmez.
"""

import logging
from dataclasses import dataclass

import pandas as pd

import config
import indicators

logger = logging.getLogger("binance_bot")

# Minimum ve maksimum kabul edilebilir volatilite (ATR / fiyat) - SERT FİLTRE.
# Çok düşükse (durgun/likit değil), çok yüksekse (aşırı riskli) eleriz.
MIN_ATR_PCT = 0.003   # %0.3
MAX_ATR_PCT = 0.12    # %12

# Volatilite BONUSU için ayrı, daha DAR bir referans aralığı - gerçek
# coinlerin çoğu ATR %1-3 bandında kümelenir, bonusu MIN/MAX_ATR_PCT'e göre
# normalize etmek (0.3%-12%) farkı neredeyse görünmez kılıyordu (gerçek bir
# gözlemden sonra düzeltildi: ATR %1.12 olan bir coin ~1.4/20 puan almıştı).
VOLATILITY_BONUS_MIN = 0.005  # %0.5 - bu ve altı bonusa katkı vermez
VOLATILITY_BONUS_MAX = 0.04   # %4 - bu ve üstü tam bonus alır


@dataclass
class Opportunity:
    symbol: str
    score: float
    price: float
    reason: str


def score_symbol(symbol: str, df, sentiment_score: float = 0.0, day_range_position: float = 0.5) -> Opportunity | None:
    if len(df) < 55:
        return None  # yeterli geçmiş veri yok

    # Güçlü olumsuz haber/duyuru varsa (hack, dava, yasak vb.) teknik skor ne
    # olursa olsun bu pariteyi tamamen ele - "ucuzladı" diye kötü habere karşı
    # pozisyon açmayı engelle.
    if sentiment_score <= config.NEGATIVE_SENTIMENT_VETO:
        logger.info("%s: olumsuz haber nedeniyle elendi (duygu skoru %+.2f)", symbol, sentiment_score)
        return None

    d = indicators.compute_all(df)
    last = d.iloc[-1]
    prev = d.iloc[-2]

    price = last["close"]
    atr_pct = last["atr_pct"]

    # Volatilite filtresi: aralık dışındaysa hemen ele
    if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT or pd.isna(atr_pct):
        return None

    # RSI aşırı-aşırı alım vetosu: diğer göstergeler ne kadar güçlü olursa
    # olsun, fiyat bu kadar hızlı yükselmişse ani bir geri çekilme riski çok
    # yüksektir - tamamen ele. (Gerçek bir kayıptan sonra eklendi: RSI 84'te
    # alım yapılmış, saniyeler içinde stop-loss'a çarpılmıştı.)
    if last["rsi14"] > config.RSI_OVERBOUGHT_VETO:
        return None

    # "Zaten pompalanmış coini kovalama" vetosu: son N mumda fiyat zaten çok
    # yükselmişse (ör. son 24 saatte +%15+), bu YENİ bir fırsat değil, devam
    # eden bir hareketin en tepesine bahis oynamaktır - tamamen ele.
    if len(d) > config.PUMP_LOOKBACK_CANDLES:
        past_price = d.iloc[-(config.PUMP_LOOKBACK_CANDLES + 1)]["close"]
        if past_price > 0:
            recent_pump_pct = (last["close"] / past_price) - 1
            if recent_pump_pct > config.MAX_RECENT_PUMP_PCT:
                logger.info(
                    "%s: son %d mumda zaten %%%.1f yükselmiş - kovalamıyoruz.",
                    symbol, config.PUMP_LOOKBACK_CANDLES, recent_pump_pct * 100,
                )
                return None

    score = 0.0
    reasons = []

    # 1) Trend skoru (0-30 puan)
    if last["ema20"] > last["ema50"]:
        trend_strength = min((last["ema20"] / last["ema50"] - 1) * 100, 3.0)
        trend_score = 20 + trend_strength * 3.3  # ~20-30 arası
        score += trend_score
        reasons.append("yükseliş trendi (EMA20>EMA50)")
    else:
        score += 0
        reasons.append("düşüş trendi - zayıf sinyal")

    # 2) RSI skoru (0-25 puan) - aşırı satımdan dönüş aranır
    rsi_val = last["rsi14"]
    if 30 <= rsi_val <= 45:
        score += 25  # toparlanma bölgesi, ideal alım
        reasons.append(f"RSI toparlanma bölgesinde ({rsi_val:.1f})")
    elif 45 < rsi_val <= 60:
        score += 15
        reasons.append(f"RSI nötr-pozitif ({rsi_val:.1f})")
    elif rsi_val < 30:
        score += 10  # çok aşırı satım, bıçak tutma riski
        reasons.append(f"RSI aşırı satım, riskli ({rsi_val:.1f})")
    else:
        score += 0  # RSI > 60, aşırı alım - yeni pozisyon için uygun değil
        reasons.append(f"RSI aşırı alım bölgesinde ({rsi_val:.1f})")

    # 3) MACD momentum skoru (0-25 puan)
    macd_hist_rising = last["macd_hist"] > prev["macd_hist"]
    if last["macd_hist"] > 0 and macd_hist_rising:
        score += 25
        reasons.append("MACD momentumu güçleniyor")
    elif last["macd_hist"] > 0:
        score += 15
        reasons.append("MACD pozitif ama momentum yavaşlıyor")
    elif macd_hist_rising:
        score += 10
        reasons.append("MACD negatif ama toparlanıyor")
    else:
        reasons.append("MACD momentumu zayıf")

    # 4) Hacim skoru (0-20 puan)
    if last["vol_avg20"] and last["vol_avg20"] > 0:
        vol_ratio = last["volume"] / last["vol_avg20"]
        if vol_ratio >= 1.5:
            score += 20
            reasons.append(f"hacim ortalamanın {vol_ratio:.1f}x üzerinde")
        elif vol_ratio >= 1.0:
            score += 10
            reasons.append("hacim normal/artan")
        else:
            reasons.append("hacim zayıf")

    # 5) Haber/duygu analizi skoru (-20 ile +20 arası)
    if sentiment_score:
        score += sentiment_score * 20
        if sentiment_score >= 0.4:
            reasons.append(f"haberler olumlu (duygu skoru {sentiment_score:+.2f})")
        elif sentiment_score <= -0.4:
            reasons.append(f"haberler olumsuz ama veto eşiğinin üstünde (duygu skoru {sentiment_score:+.2f})")

    # 6) Volatilite bonusu (0-30 puan) - VOLATILITY_BONUS_MIN/MAX gibi DAR ve
    # gerçekçi bir aralığa göre normalize edilir (bkz. yukarıdaki not), bu
    # yüzden gerçekten "hareketli" coinler artık belirgin şekilde öne çıkar.
    # Kullanıcı isteğiyle ağırlık artırıldı (10 -> 20 -> 30) ve normalizasyon
    # aralığı düzeltildi - amaç saatlerce kıpırdamayan coinlerde beklememek,
    # gerçek hareket potansiyeli olan coinlere yönelmek.
    volatility_range = VOLATILITY_BONUS_MAX - VOLATILITY_BONUS_MIN
    volatility_position = min(max(atr_pct - VOLATILITY_BONUS_MIN, 0), volatility_range) / volatility_range
    volatility_bonus = volatility_position * 30
    score += volatility_bonus
    reasons.append(f"volatilite bonusu (ATR %{atr_pct * 100:.2f})")

    # 7) Günlük dip yakınlığı bonusu (0-20 puan) - kullanıcı isteğiyle: doğru
    # fiyattan almak için, günün en yüksek/en düşük aralığında fiyat dibe ne
    # kadar yakınsa o kadar fazla puan verilir (0 = tam dipte, 1 = tam tepede).
    # Amaç: günün zirvesine yakın alıp aşağı kalan bir hareketi kovalamak
    # yerine, ucuz kalmış bir noktadan alıp yükselişi yakalamak.
    day_low_bonus = (1 - day_range_position) * 20
    score += day_low_bonus
    reasons.append(f"günlük dip yakınlığı bonusu (gün aralığında %{day_range_position * 100:.0f} seviyede)")

    # RSI 60 - RSI_OVERBOUGHT_VETO arası "ısınmış ama henüz vetolanmamış"
    # bölge: trend/MACD/hacim ne kadar güçlü olursa olsun, geç kalınmış bir
    # harekete tam puan verilmesin diye TOPLAM skor kademeli olarak düşürülür
    # (RSI 60'ta çarpan ~1.0, veto eşiğine yaklaştıkça ~0.4'e iner).
    if rsi_val > 60:
        overheated_range = config.RSI_OVERBOUGHT_VETO - 60
        overheated_position = min(rsi_val - 60, overheated_range) / overheated_range
        dampening = 1 - overheated_position * 0.6
        score *= dampening
        reasons.append(f"RSI ısınmış bölgede - skor %{dampening * 100:.0f} ile çarpıldı")

    return Opportunity(
        symbol=symbol,
        score=round(score, 1),
        price=price,
        reason="; ".join(reasons),
    )


def rank_opportunities(scored: list) -> list:
    """Skora göre büyükten küçüğe sıralar."""
    valid = [o for o in scored if o is not None]
    return sorted(valid, key=lambda o: o.score, reverse=True)
