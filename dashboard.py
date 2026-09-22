"""
dashboard.py
------------
Botun durumunu izlemek için yerel bir web paneli: hangi hesapta (testnet/gerçek)
çalıştığı, güncel bakiye, açık pozisyonlar, işlem geçmişi, günlük kâr/zarar,
kâr geri-verme/zarar limiti durumu ve haber duygu skorları.

Sadece kendi bilgisayarında (127.0.0.1) çalışır, dışarıya açılmaz. Botla (main.py)
aynı anda, ayrı bir terminalde çalıştırılır:

    python dashboard.py

Sonra tarayıcıda http://127.0.0.1:5050 adresini aç.
"""

import json
import logging
import os
import time

from flask import Flask, jsonify, render_template_string

import config
import news_sentiment
from risk_manager import RiskManager

logging.getLogger("werkzeug").setLevel(logging.WARNING)
logger = logging.getLogger("binance_bot")

app = Flask(__name__)

_price_cache = {"data": {}, "ts": 0}
_CACHE_SECONDS = 5
_universe_cache = {"data": [], "ts": 0}
_UNIVERSE_CACHE_SECONDS = 60  # taranabilir parite listesi sık değişmez
NEWS_FEED_LIMIT = 200  # "Tüm Haberler" ile açılan tam listenin üst sınırı
NEWS_FEED_PREVIEW = 8  # panelde varsayılan olarak gösterilen (öne çıkan) haber sayısı


def _mask(value: str) -> str:
    if not value or len(value) < 8:
        return "(tanımsız)"
    return f"{value[:4]}…{value[-4:]}"


def _get_client():
    try:
        if config.EXCHANGE == "binance_tr":
            from binance_tr_client import BinanceTRClient
            return BinanceTRClient()
        from binance_client import BinanceClient
        return BinanceClient()
    except Exception:
        return None


QUOTE_LABEL = "TL" if config.EXCHANGE == "binance_tr" else "USDT"


def _read_trade_history(limit: int = 300) -> list:
    if not os.path.exists(config.TRADE_HISTORY_FILE):
        return []
    records = []
    with open(config.TRADE_HISTORY_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[-limit:]


def _daily_fees(history: list, date_str: str) -> float:
    """Bugüne ait (state.json'daki 'date' ile aynı gün) tüm satışların
    komisyon toplamını döndürür - alım komisyonu da satış kaydına
    (fee_paid = buy_fee + sell_fee) dahil olduğu için sadece SELL satırları
    toplanır, çift saymaya gerek yok."""
    if not date_str:
        return 0.0
    total = 0.0
    for rec in history:
        if rec.get("side") == "SELL" and rec.get("time", "").startswith(date_str):
            total += rec.get("fee_paid", 0.0)
    return round(total, 4)


def _cumulative_pnl(history: list) -> list:
    points = []
    total = 0.0
    for rec in history:
        if rec.get("side") == "SELL" and "pnl" in rec:
            total += rec["pnl"]
            points.append({"time": rec["time"], "value": round(total, 4)})
    return points


def _get_universe(client) -> list:
    """Botun taradığı tüm parite evrenini döndürür (60sn önbellekli - liste
    sık değişmez, her 8sn'lik panel yenilemesinde tekrar sorgulamaya gerek yok)."""
    now = time.time()
    if now - _universe_cache["ts"] < _UNIVERSE_CACHE_SECONDS and _universe_cache["data"]:
        return _universe_cache["data"]
    if not client:
        return _universe_cache["data"]
    try:
        _universe_cache["data"] = client.get_usdt_pairs()
        _universe_cache["ts"] = now
    except Exception as e:
        logger.debug("Parite evreni alınamadı: %s", e)
    return _universe_cache["data"]


def _current_prices(symbols: list, client) -> dict:
    now = time.time()
    if now - _price_cache["ts"] < _CACHE_SECONDS:
        return {s: _price_cache["data"].get(s) for s in symbols}
    prices = {}
    if client:
        for s in symbols:
            try:
                prices[s] = float(client.get_24h_ticker(s)["lastPrice"])
            except Exception:
                prices[s] = None
    _price_cache["data"] = prices
    _price_cache["ts"] = now
    return prices


@app.route("/api/status")
def api_status():
    risk = RiskManager()
    state = risk.state
    client = _get_client()

    balance = None
    balance_error = None
    if client:
        try:
            balance = client.get_usdt_balance()
        except Exception as e:
            balance_error = str(e)
    else:
        balance_error = "Binance istemcisi başlatılamadı (API anahtarlarını kontrol et)."

    open_positions_raw = state.get("open_positions", {})

    try:
        # get_live_sentiment() zaman-sönümlemesini HER ÇAĞRIDA anlık yeniden
        # hesaplar (salt matematik, ağ çağrısı yok) - panel 8sn'de bir
        # sorduğunda skorlar gerçekten "şu ana göre" güncellenmiş olur.
        symbol_sentiment, market_sentiment = news_sentiment.get_live_sentiment()
        news_state = news_sentiment._load_state()
        news_feed = list(reversed(news_state.get("news_feed", [])))[:NEWS_FEED_LIMIT]
    except Exception:
        symbol_sentiment, market_sentiment, news_feed = {}, 0.0, []

    # Botun taradığı TÜM parite evrenini de tabloya dahil et (haberde hiç
    # geçmeyenler 0.0/nötr olarak görünür) - sadece rastlantısal eşleşenleri
    # değil, erişilen tüm coinleri göstermek için.
    universe = _get_universe(client)
    if universe:
        symbol_sentiment = {sym: symbol_sentiment.get(sym, 0.0) for sym in universe}

    # Açık pozisyonlar VE haber akışındaki coinler için tek seferde fiyat
    # sorgusu yap (aynı 5sn önbelleği paylaşır, gereksiz tekrar çağrı olmaz).
    feed_symbols_needed = {
        sym for item in news_feed for sym in item.get("price_at_news", {}).keys()
    }
    price_symbols = set(open_positions_raw.keys()) | feed_symbols_needed
    prices = _current_prices(list(price_symbols), client)

    open_positions = []
    for symbol, pos in open_positions_raw.items():
        current_price = prices.get(symbol)
        unrealized_pnl = None
        unrealized_pnl_pct = None
        if current_price is not None:
            unrealized_pnl = (current_price - pos["entry_price"]) * pos["quantity"]
            unrealized_pnl_pct = (current_price / pos["entry_price"] - 1) * 100
        open_positions.append({
            "symbol": symbol,
            "entry_price": pos["entry_price"],
            "quantity": pos["quantity"],
            "stop_loss": pos["stop_loss"],
            "take_profit": pos["take_profit"],
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "trailing_active": pos.get("trailing_active", False),
            "peak_price": pos.get("peak_price"),
            "reason": pos.get("reason", ""),
        })

    # Her haber öğesi için: "haber tespit edildiğinden beri fiyat ne kadar değişti".
    for item in news_feed:
        change = {}
        for sym, price_then in item.get("price_at_news", {}).items():
            now_price = prices.get(sym)
            if now_price and price_then:
                change[sym] = round((now_price / price_then - 1) * 100, 2)
        item["price_change"] = change

    history = _read_trade_history()

    return jsonify({
        "mode": "GERÇEK HESAP" if config.IS_REAL_MONEY else "TESTNET",
        "exchange": config.EXCHANGE,
        "quote_label": QUOTE_LABEL,
        "masked_api_key": _mask(config.API_KEY),
        "balance_usdt": balance,
        "balance_error": balance_error,
        # "Bakiye" sadece boşta duran (kullanılmamış) parayı gösterir; açık
        # pozisyonların değeri buna dahil değildir - toplam varlık ayrı
        # gösterilir ki "param nereye gitti" kafa karışıklığı olmasın.
        "total_equity": (
            (balance if balance is not None else 0.0)
            + sum((p["current_price"] or p["entry_price"]) * p["quantity"] for p in open_positions)
        ) if balance is not None else None,
        "daily": {
            "starting_balance_today": state.get("starting_balance_today"),
            "realized_pnl_today": state.get("realized_pnl_today", 0.0),
            "peak_pnl_today": state.get("peak_pnl_today", 0.0),
            "fees_today": _daily_fees(history, state.get("date")),
            "halt_reason_today": state.get("halt_reason_today"),
            "daily_max_loss_pct": config.DAILY_MAX_LOSS_PCT,
            "profit_giveback_limit_pct": config.PROFIT_GIVEBACK_LIMIT_PCT,
        },
        "open_positions": open_positions,
        "trade_history": list(reversed(history)),
        "cumulative_pnl": _cumulative_pnl(history),
        "sentiment": {
            "market": market_sentiment,
            "symbols": symbol_sentiment,
            "feed": news_feed,
        },
        "settings": {
            "take_profit_pct": config.TAKE_PROFIT_PCT,
            "stop_loss_pct": config.STOP_LOSS_PCT,
            "max_open_positions": config.MAX_OPEN_POSITIONS,
            "min_minutes_between_trades": config.MIN_MINUTES_BETWEEN_TRADES,
            "scan_interval_minutes": config.SCAN_INTERVAL_MINUTES,
        },
    })


PAGE = """
<!doctype html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Paneli</title>
<style>
  :root {
    --bg: #0b0f14; --panel: #131a22; --border: #223041; --text: #e6edf3;
    --muted: #8b98a5; --green: #3fb950; --red: #f85149; --amber: #d29922;
    --accent: #58a6ff;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    padding: 20px; padding-bottom: max(20px, env(safe-area-inset-bottom));
  }
  h1 { font-size: 1.3rem; margin: 0 0 4px; }
  .sub { color: var(--muted); font-size: 0.85rem; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
  .card .label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }
  .card .value { font-size: 1.4rem; font-weight: 600; margin-top: 4px; }
  .pos { color: var(--green); } .neg { color: var(--red); } .neutral { color: var(--muted); }
  .banner { padding: 12px 16px; border-radius: 10px; margin-bottom: 20px; font-size: 0.9rem; }
  .banner.halt { background: rgba(248,81,73,0.12); border: 1px solid var(--red); color: var(--red); }
  .banner.ok { background: rgba(63,185,80,0.10); border: 1px solid var(--green); color: var(--green); }
  section { margin-bottom: 28px; }
  h2 { font-size: 1rem; color: var(--muted); margin: 0 0 10px; font-weight: 600; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  th { color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; }
  .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 10px; }
  .empty { color: var(--muted); padding: 16px; font-size: 0.85rem; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; }
  .badge.buy { background: rgba(88,166,255,0.15); color: var(--accent); }
  .badge.sell { background: rgba(139,148,165,0.15); color: var(--muted); }
  svg { display: block; width: 100%; height: 120px; }
  footer { color: var(--muted); font-size: 0.75rem; margin-top: 30px; }
  @media (prefers-color-scheme: light) {
    :root:not([data-force-dark]) {
      --bg: #f6f8fa; --panel: #ffffff; --border: #d0d7de; --text: #1f2328; --muted: #59636e;
    }
  }
</style>
</head>
<body>
  <h1>🤖 Kripto Trading Bot Paneli</h1>
  <div class="sub" id="header-sub">Yükleniyor…</div>

  <div id="halt-banner"></div>

  <div class="grid" id="stat-cards"></div>

  <section>
    <h2>Kümülatif Kâr/Zarar (gerçekleşen)</h2>
    <div class="table-wrap" style="padding: 10px;"><svg id="pnl-chart" viewBox="0 0 600 120" preserveAspectRatio="none"></svg></div>
  </section>

  <section>
    <h2>Açık Pozisyonlar</h2>
    <div class="table-wrap"><table id="positions-table"><thead><tr>
      <th>Parite</th><th>Giriş</th><th>Miktar</th><th>Güncel</th><th>Stop-Loss</th><th>Hedef/Zirve</th><th>Durum</th><th>Anlık K/Z</th><th>Neden Alındı</th>
    </tr></thead><tbody></tbody></table></div>
  </section>

  <section>
    <h2>En Yüksek Duygu Skorlu 5 Parite <span style="color: var(--muted); font-weight: 400; font-size: 0.8em;">(3sn'de bir yeniden hesaplanır)</span></h2>
    <div class="table-wrap"><table id="sentiment-table"><thead><tr><th>Parite</th><th>Duygu Skoru</th></tr></thead><tbody></tbody></table></div>
  </section>

  <section>
    <h2>İşlem Geçmişi</h2>
    <div class="table-wrap"><table id="history-table"><thead><tr>
      <th>Zaman</th><th>Yön</th><th>Parite</th><th>Fiyat</th><th>Miktar</th><th>K/Z (net)</th><th>Komisyon</th><th>Gerekçe</th>
    </tr></thead><tbody></tbody></table></div>
  </section>

  <footer>Sadece bu bilgisayarda çalışır (127.0.0.1). 3 saniyede bir otomatik yenilenir.</footer>

<script>
function fmt(n, d=2) { return (n === null || n === undefined) ? '—' : Number(n).toFixed(d); }
function pnlClass(n) { return n > 0 ? 'pos' : (n < 0 ? 'neg' : 'neutral'); }
function esc(s) { return String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function safe(label, fn) { try { fn(); } catch (e) { console.error('Panel render hatası (' + label + '):', e); } }

async function refresh() {
  let data;
  try {
    const res = await fetch('/api/status');
    data = await res.json();
  } catch (e) {
    document.getElementById('header-sub').textContent = 'Sunucuya ulaşılamıyor: ' + e;
    return;
  }

  safe('header', () => {
    document.getElementById('header-sub').textContent =
      `Borsa: ${data.exchange}  •  Mod: ${data.mode}  •  API anahtarı: ${data.masked_api_key}  •  Son güncelleme: ${new Date().toLocaleTimeString('tr-TR')}`;
  });

  safe('banner', () => {
    const halt = document.getElementById('halt-banner');
    if (data.daily.halt_reason_today) {
      const reason = data.daily.halt_reason_today === 'daily_loss_limit'
        ? 'Günlük zarar limiti aşıldı' : 'Kâr geri-verme koruması tetiklendi';
      halt.innerHTML = `<div class="banner halt">⛔ ${reason} — bugün yeni işlem açılmayacak.</div>`;
    } else {
      halt.innerHTML = `<div class="banner ok">✅ Risk limitleri normal — bot yeni işlem açabilir.</div>`;
    }
  });

  safe('cards', () => {
    const q = data.quote_label || 'USDT';
    const balanceValue = data.balance_usdt !== null ? fmt(data.balance_usdt) + ' ' + q : (data.balance_error || 'bilinmiyor');
    const equityValue = data.total_equity !== null ? fmt(data.total_equity) + ' ' + q : 'bilinmiyor';
    const dailyPnl = data.daily.realized_pnl_today;
    const cards = [
      ['Toplam Varlık', equityValue, ''],
      ['Boştaki Bakiye', balanceValue, ''],
      ['Günlük K/Z', fmt(dailyPnl) + ' ' + q, pnlClass(dailyPnl)],
      ['Günün Zirve Kârı', fmt(data.daily.peak_pnl_today) + ' ' + q, 'neutral'],
      ['Bugünkü Toplam Komisyon', '-' + fmt(data.daily.fees_today) + ' ' + q, 'neutral'],
      ['Açık Pozisyon', data.open_positions.length + ' / ' + data.settings.max_open_positions, ''],
      ['Hedef / Stop', '+%' + (data.settings.take_profit_pct*100).toFixed(1) + ' / -%' + (data.settings.stop_loss_pct*100).toFixed(1), ''],
      ['Genel Piyasa Duyarlılığı', (data.sentiment.market >= 0 ? '+' : '') + data.sentiment.market.toFixed(2), pnlClass(data.sentiment.market)],
    ];
    document.getElementById('stat-cards').innerHTML = cards.map(([label, value, cls]) =>
      `<div class="card"><div class="label">${label}</div><div class="value ${cls}">${value}</div></div>`
    ).join('');
  });

  safe('positions', () => {
    const posBody = document.querySelector('#positions-table tbody');
    posBody.innerHTML = data.open_positions.length ? data.open_positions.map(p => `
      <tr>
        <td>${esc(p.symbol)}</td>
        <td>${fmt(p.entry_price, 6)}</td>
        <td>${fmt(p.quantity, 6)}</td>
        <td>${fmt(p.current_price, 6)}</td>
        <td>${fmt(p.stop_loss, 6)}</td>
        <td>${p.trailing_active ? fmt(p.peak_price, 6) + ' (zirve)' : fmt(p.take_profit, 6) + ' (hedef)'}</td>
        <td>${p.trailing_active ? '📈 iz sürüyor' : '⏳ hedefi bekliyor'}</td>
        <td class="${pnlClass(p.unrealized_pnl || 0)}">${fmt(p.unrealized_pnl)} ${data.quote_label || 'USDT'} (${fmt(p.unrealized_pnl_pct)}%)</td>
        <td style="white-space: normal; max-width: 320px;">${esc(p.reason || '—')}</td>
      </tr>`).join('') : '<tr><td colspan="9" class="empty">Açık pozisyon yok</td></tr>';
  });

  safe('sentiment', () => {
    const sentBody = document.querySelector('#sentiment-table tbody');
    // Sunucu tarafında TÜM coinler için skor hesaplanır; panelde sadece anlık
    // en yüksek 5'i gösteriyoruz - her 8sn'de skorlar değiştikçe sıralama/liste de değişir.
    const sentEntries = Object.entries(data.sentiment.symbols).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 5);
    sentBody.innerHTML = sentEntries.length ? sentEntries.map(([sym, score]) => `
      <tr><td>${esc(sym)}</td><td class="${pnlClass(score)}">${score >= 0 ? '+' : ''}${score.toFixed(2)}</td></tr>
    `).join('') : '<tr><td colspan="2" class="empty">Henüz ilgili haber bulunamadı</td></tr>';
  });

  safe('history', () => {
    const histBody = document.querySelector('#history-table tbody');
    histBody.innerHTML = data.trade_history.length ? data.trade_history.map(r => `
      <tr>
        <td>${new Date(r.time).toLocaleString('tr-TR')}</td>
        <td><span class="badge ${r.side === 'BUY' ? 'buy' : 'sell'}">${r.side === 'BUY' ? 'ALIM' : 'SATIM'}</span></td>
        <td>${esc(r.symbol)}</td>
        <td>${fmt(r.price, 6)}</td>
        <td>${fmt(r.quantity, 6)}</td>
        <td class="${r.pnl !== undefined ? pnlClass(r.pnl) : ''}">${r.pnl !== undefined ? fmt(r.pnl) + ' ' + (data.quote_label || 'USDT') : '—'}</td>
        <td>${r.fee_paid !== undefined ? '-' + fmt(r.fee_paid, 4) + ' ' + (data.quote_label || 'USDT') : '—'}</td>
        <td>${esc(r.reason || '—')}</td>
      </tr>`).join('') : '<tr><td colspan="8" class="empty">Henüz işlem yok</td></tr>';
  });

  safe('chart', () => {
    const svg = document.getElementById('pnl-chart');
    const pts = data.cumulative_pnl;
    if (pts.length < 2) {
      svg.innerHTML = '<text x="10" y="60" fill="#8b98a5" font-size="13">Grafik için en az 2 kapanmış işlem gerekiyor</text>';
      return;
    }
    const values = pts.map(p => p.value);
    const min = Math.min(0, ...values), max = Math.max(0, ...values);
    const range = (max - min) || 1;
    const w = 600, h = 120, pad = 6;
    const coords = pts.map((p, i) => {
      const x = pad + (i / (pts.length - 1)) * (w - 2*pad);
      const y = h - pad - ((p.value - min) / range) * (h - 2*pad);
      return [x, y];
    });
    const path = coords.map((c, i) => (i === 0 ? 'M' : 'L') + c[0].toFixed(1) + ',' + c[1].toFixed(1)).join(' ');
    const zeroY = h - pad - ((0 - min) / range) * (h - 2*pad);
    const lastVal = values[values.length - 1];
    const color = lastVal >= 0 ? '#3fb950' : '#f85149';
    svg.innerHTML = `
      <line x1="0" y1="${zeroY.toFixed(1)}" x2="${w}" y2="${zeroY.toFixed(1)}" stroke="#223041" stroke-dasharray="4 4"/>
      <path d="${path}" fill="none" stroke="${color}" stroke-width="2"/>
    `;
  });
}

refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


if __name__ == "__main__":
    print("Panel başlatılıyor: http://127.0.0.1:5050  (sadece bu bilgisayardan erişilebilir)")
    app.run(host="127.0.0.1", port=5050, debug=False)
