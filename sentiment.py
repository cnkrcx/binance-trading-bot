"""
sentiment.py
------------
Haber/duyuru metinlerinin piyasa etkisini analiz eder. İki mod var:

1. ANTHROPIC_API_KEY tanımlıysa: Claude ile gerçek dil anlayışı (ironi,
   belirsizlik, kaynağın güvenilirliği gibi nüansları da hesaba katarak)
   -1.0 (çok olumsuz) ile +1.0 (çok olumlu) arasında bir skor üretir.
2. Tanımlı değilse: tamamen ÜCRETSİZ, anahtar kelime tabanlı basit bir
   analize otomatik geçilir (aşağıdaki POSITIVE/NEGATIVE_KEYWORDS listeleri).
   Bu mod nüansı (ironi, "aslında önemsiz haber" gibi durumları) kaçırabilir
   ama hiçbir API anahtarı gerektirmez ve bot her zaman bir şeyler üretir.
"""

import json
import logging

import config

logger = logging.getLogger("binance_bot")

_client = None

# Ücretsiz mod için basit anahtar kelime listeleri (RSS kaynakları İngilizce
# olduğundan İngilizce). Kapsamlı değildir, gerçek dil anlayışı yerine geçmez.
POSITIVE_KEYWORDS = [
    "partnership", "listing", "listed", "approval", "approved", "adoption",
    "surge", "rally", "bullish", "upgrade", "integration", "launch",
    "funding", "invest", "record high", "all-time high", "breakthrough",
    "gains", "growth", "expands", "expansion", "institutional", "milestone",
    "outperform", "inflow", "inflows", "buyback", "mainnet", "airdrop",
    "collaboration", "backing", "soars", "jumps", "breakout", "accumulation",
    "whale buy", "staking rewards", "tvl grows", "upgrade complete",
]
NEGATIVE_KEYWORDS = [
    "hack", "hacked", "exploit", "lawsuit", "sued", "charges", "fraud",
    "scam", "ban", "banned", "crash", "plunge", "bearish", "delist",
    "delisting", "bankruptcy", "insolvent", "investigation", "fine",
    "penalty", "breach", "vulnerability", "outage", "halt", "warning",
    "decline", "sell-off", "selloff", "collapse", "outflow", "outflows",
    "dump", "dumps", "rug pull", "rugpull", "exploited", "drained",
    "liquidated", "liquidation", "unlock", "token unlock", "slump",
    "downtrend", "correction", "capitulation", "whale sell",
]


def _keyword_sentiment(text: str) -> dict | None:
    """Ücretsiz, anahtar kelime tabanlı basit skor. Hiç eşleşme yoksa None
    döner (haber akışını alakasız/nötr başlıklarla doldurmamak için)."""
    lower = text.lower()
    pos_hits = [kw for kw in POSITIVE_KEYWORDS if kw in lower]
    neg_hits = [kw for kw in NEGATIVE_KEYWORDS if kw in lower]
    total = len(pos_hits) + len(neg_hits)
    if total == 0:
        return None
    score = round((len(pos_hits) - len(neg_hits)) / total, 2)
    parts = []
    if pos_hits:
        parts.append("olumlu: " + ", ".join(pos_hits[:3]))
    if neg_hits:
        parts.append("olumsuz: " + ", ".join(neg_hits[:3]))
    return {
        "score": score,
        "reason": "; ".join(parts) + " (ücretsiz anahtar-kelime modu)",
        "method": "keyword",
    }


def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


PROMPT_TEMPLATE = """Aşağıda kripto para piyasasını ilgilendirebilecek haber/duyuru \
başlıkları var. Her biri için, o haberin ilgili kripto varlığın (veya genel \
piyasanın) fiyatına KISA VADEDE (saatler-günler) etkisini -1.0 ile 1.0 \
arasında bir skorla değerlendir:
  -1.0 = çok olumsuz (hack, dava, yasak, iflas, kötü bilanço, güven kaybı)
   0.0 = nötr / belirsiz / piyasayı etkilemez
  +1.0 = çok olumlu (resmi ortaklık, borsa listelemesi, düzenleyici onay, \
benimseme haberi)

Spekülatif, kaynağı belirsiz veya çoktan fiyatlanmış "eski" haberlere düşük \
mutlak değerli skor ver. Sadece resmi/güvenilir kaynaklardan gelen somut \
gelişmelere yüksek mutlak değerli skor ver. Şüpheli durumda 0'a yakın kal.

Haberler (JSON dizisi, her öğede "id" ve "text" var):
{items_json}

Yanıtını SADECE şu JSON formatında ver, başka hiçbir açıklama ekleme:
[{{"id": <id>, "score": <-1.0..1.0>, "reason": "<kısa Türkçe gerekçe>"}}, ...]
"""


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first_line, rest = text.split("\n", 1)
            text = rest if first_line.strip().lower() in ("json", "") else text
    return text.strip()


def analyze_batch(items: list) -> dict:
    """items: [{"id": int, "text": str}, ...] -> {id: {"score", "reason", "method"}}

    ANTHROPIC_API_KEY yoksa otomatik olarak ücretsiz anahtar-kelime moduna
    geçer (bkz. modül docstring'i) - bot hiçbir zaman tamamen "duygu verisi
    yok" durumuna düşmez, sadece daha basit bir analizle devam eder.
    """
    if not items:
        return {}

    if not config.ANTHROPIC_API_KEY:
        result = {}
        for item in items:
            r = _keyword_sentiment(item["text"])
            if r:
                result[item["id"]] = r
        return result

    try:
        client = _get_client()
        prompt = PROMPT_TEMPLATE.format(items_json=json.dumps(items, ensure_ascii=False))
        response = client.messages.create(
            model=config.SENTIMENT_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        parsed = json.loads(_strip_code_fence(raw))

        result = {}
        for entry in parsed:
            try:
                score = max(-1.0, min(1.0, float(entry.get("score", 0.0))))
                result[entry["id"]] = {"score": score, "reason": entry.get("reason", ""), "method": "llm"}
            except (KeyError, TypeError, ValueError):
                continue
        return result
    except Exception as e:
        logger.warning("Claude ile duygu analizi başarısız, bu tur için ücretsiz moda geçiliyor: %s", e)
        result = {}
        for item in items:
            r = _keyword_sentiment(item["text"])
            if r:
                result[item["id"]] = r
        return result
