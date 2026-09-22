"""
risk_manager.py
----------------
Pozisyon boyutlandırma, stop-loss/take-profit takibi, maksimum açık
pozisyon sayısı ve günlük zarar limiti gibi kuralları uygular.

Bot ne kadar iyi bir stratejiye sahip olursa olsun, risk yönetimi
olmadan hesabı tamamen kaybetmesi an meselesidir. Bu modül botun
"fren sistemi"dir.
"""

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone

import config

logger = logging.getLogger("binance_bot")

# Kâr koruması (trailing stop) için kademeli geri-verme oranları. Zirvedeki
# kâr yüzdesi arttıkça geri verilecek pay küçülür - küçük bir kârı gürültüyle
# kaybetmemek için sıkı korurken, büyük bir yükselişte pozisyona nefes payı
# bırakır (kullanıcı isteğiyle: %10'a kadar -> %34 [zirvenin %66'sı korunur;
# %50 ve %75 denendi, ikisi de küçük kârlarda komisyona yenik düşme riskini
# fazla büyütüyordu], %10-30 -> %25, %30-80 -> %20, %80+ -> %15). Alt sınır
# MIN_PROFIT_TO_ARM_TRAIL_PCT ile belirlenir.
PROFIT_GIVEBACK_TIERS = [
    (0.10, 0.34),
    (0.30, 0.25),
    (0.80, 0.20),
    (float("inf"), 0.15),
]


def _giveback_pct_for(peak_profit_pct: float) -> float:
    for upper_bound, giveback in PROFIT_GIVEBACK_TIERS:
        if peak_profit_pct < upper_bound:
            return giveback
    return PROFIT_GIVEBACK_TIERS[-1][1]


class RiskManager:
    def __init__(self, state_file: str = config.STATE_FILE):
        self.state_file = state_file
        self.state = self._load_state()
        # Pozisyon kontrolü artık ayrı bir thread'de (main.py) çok sık
        # çalışıyor; tam tarama thread'iyle aynı anda state.json'a yazmaya
        # çalışırlarsa yarış durumu (race condition) oluşabilir. Bu kilit
        # bunu önler.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _load_state(self) -> dict:
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                state = json.load(f)
            state.setdefault("last_trade_opened", None)
            state.setdefault("peak_pnl_today", 0.0)
            state.setdefault("halt_reason_today", None)
            state.setdefault("last_sold", {})  # symbol -> ISO zaman, aynı-parite bekleme süresi için
            state.setdefault("loss_streak", {})  # symbol -> art arda stop_loss sayısı
            for pos in state.get("open_positions", {}).values():
                pos.setdefault("trailing_active", False)
                pos.setdefault("peak_price", None)
                pos.setdefault("opened_at", datetime.now(timezone.utc).isoformat())
            return state
        return {
            "date": str(date.today()),
            "starting_balance_today": None,
            "realized_pnl_today": 0.0,
            "peak_pnl_today": 0.0,  # günün en yüksek ulaşılan kârı - kâr geri verme koruması için
            "halt_reason_today": None,  # "daily_loss_limit" / "profit_giveback" / None
            "last_trade_opened": None,  # son işlem açılış zamanı (ISO, UTC) - komisyon aşımını önleyen bekleme için
            "last_sold": {},  # symbol -> ISO zaman, aynı-parite bekleme süresi için
            "loss_streak": {},  # symbol -> art arda stop_loss sayısı
            "open_positions": {},  # symbol -> {entry_price, quantity, stop_loss, take_profit}
        }

    def _save_state(self):
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def _log_trade(self, record: dict):
        """Panelin okuyacağı yapılandırılmış işlem geçmişini dosyaya ekler."""
        record["time"] = datetime.now(timezone.utc).isoformat()
        with open(config.TRADE_HISTORY_FILE, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _reset_if_new_day(self, current_balance: float):
        if self.state["date"] != str(date.today()):
            logger.info("Yeni gün başladı, günlük istatistikler sıfırlanıyor.")
            self.state["date"] = str(date.today())
            self.state["starting_balance_today"] = current_balance
            self.state["realized_pnl_today"] = 0.0
            self.state["peak_pnl_today"] = 0.0
            self.state["halt_reason_today"] = None
            self.state["loss_streak"] = {}
            self._save_state()
        elif self.state["starting_balance_today"] is None:
            self.state["starting_balance_today"] = current_balance
            self._save_state()

    # ------------------------------------------------------------------
    def can_trade_today(self, current_balance: float) -> bool:
        """Sabit günlük zarar limiti (%5) tetiklendiyse False döner. Bir kez
        tetiklenince gün boyunca (PnL toparlansa bile) yeniden açılmaz - amaç
        panik/aşırı işlem döngüsüne girmemek.

        Not: Eskiden ayrıca "günün en yüksek kârının X%'i geri verilirse dur"
        kuralı da vardı; kullanıcı isteğiyle kaldırıldı (2026-09-17) - bir
        coin satılıp kâr kısmen geri verildiğinde botun BAŞKA bir coinde yeni
        fırsat değerlendirmeye devam etmesi isteniyor, güne tamamen kilitlenmek
        değil. Sabit %5 zarar limiti (aşağıda) tek güvenlik ağı olarak kalıyor.
        """
        with self._lock:
            self._reset_if_new_day(current_balance)

            if self.state["halt_reason_today"]:
                return False

            start = self.state["starting_balance_today"] or current_balance
            pnl = self.state["realized_pnl_today"]

            if start > 0 and (-pnl / start) >= config.DAILY_MAX_LOSS_PCT:
                logger.warning(
                    "Günlük zarar limiti (%.1f%%) aşıldı (PnL: %.2f USDT). Bugün yeni işlem açılmayacak.",
                    config.DAILY_MAX_LOSS_PCT * 100, pnl,
                )
                self.state["halt_reason_today"] = "daily_loss_limit"
                self._save_state()
                return False

            return True

    def can_open_new_position(self) -> bool:
        with self._lock:
            if len(self.state["open_positions"]) >= config.MAX_OPEN_POSITIONS:
                logger.info(
                    "Maksimum açık pozisyon sayısına ulaşıldı (%d/%d).",
                    len(self.state["open_positions"]), config.MAX_OPEN_POSITIONS,
                )
                return False
            return self._cooldown_elapsed()

    def _cooldown_elapsed(self) -> bool:
        """Komisyonların kârı eritmemesi için art arda hızlı işlem açılmasını
        engeller: son işlemden bu yana MIN_MINUTES_BETWEEN_TRADES geçmeden
        yeni işlem açılmaz (mevcut pozisyonların stop-loss/take-profit'i etkilenmez)."""
        last = self.state.get("last_trade_opened")
        if not last:
            return True
        elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last)
        if elapsed < timedelta(minutes=config.MIN_MINUTES_BETWEEN_TRADES):
            logger.info(
                "Yeni işlem için bekleme süresi dolmadı (%.0f/%d dk) - komisyon aşımını önlemek için atlanıyor.",
                elapsed.total_seconds() / 60, config.MIN_MINUTES_BETWEEN_TRADES,
            )
            return False
        return True

    def has_position(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self.state["open_positions"]

    def symbol_on_cooldown(self, symbol: str) -> bool:
        """Bu parite yakın zamanda satıldıysa True döner - SAME_SYMBOL_COOLDOWN_MINUTES
        dolmadan aynı coin tekrar alınmaz (art arda aynı hataya düşmemek için)."""
        with self._lock:
            last_sold = self.state.get("last_sold", {}).get(symbol)
            if not last_sold:
                return False
            elapsed = datetime.now(timezone.utc) - datetime.fromisoformat(last_sold)
            if elapsed < timedelta(minutes=config.SAME_SYMBOL_COOLDOWN_MINUTES):
                logger.info(
                    "%s atlanıyor: %.0f/%d dk önce satılmıştı, aynı coin için bekleme sürüyor.",
                    symbol, elapsed.total_seconds() / 60, config.SAME_SYMBOL_COOLDOWN_MINUTES,
                )
                return True
            return False

    def symbol_blocked_by_losses(self, symbol: str) -> bool:
        """Bir parite AYNI GÜN art arda 2+ kez stop-loss'a çarptıysa True
        döner - o coin o gün için tamamen elenir. Amaç: RSI'nin "toparlanma
        bölgesi" sinyalinin, gerçekte düşüşe devam eden bir coini defalarca
        "fırsat" göstermesiyle art arda zarar etmeyi önlemek (kullanıcı
        isteğiyle eklendi - F_TRY üst üste 2 kez stop-loss yiyip 3. kez daha
        alınmıştı)."""
        with self._lock:
            streak = self.state.get("loss_streak", {}).get(symbol, 0)
            if streak >= 2:
                logger.info(
                    "%s atlanıyor: bugün art arda %d kez stop-loss'a çarptı, bugün için tamamen elendi.",
                    symbol, streak,
                )
                return True
            return False

    def position_size(self, balance: float) -> float:
        """Bir işlemde kullanılacak USDT miktarı."""
        return balance * config.POSITION_SIZE_PCT

    # ------------------------------------------------------------------
    def register_buy(self, symbol: str, entry_price: float, quantity: float, reason: str = ""):
        with self._lock:
            self.state["open_positions"][symbol] = {
                "entry_price": entry_price,
                "quantity": quantity,
                "stop_loss": entry_price * (1 - config.STOP_LOSS_PCT),
                "take_profit": entry_price * (1 + config.TAKE_PROFIT_PCT),
                "trailing_active": False,  # take_profit'e ulaşılınca True olur
                "peak_price": None,  # trailing modundayken görülen en yüksek fiyat
                "reason": reason,  # neden alındı (skor bileşenleri) - panelde gösterilir
                "opened_at": datetime.now(timezone.utc).isoformat(),  # durgunluk kontrolü için
            }
            self.state["last_trade_opened"] = datetime.now(timezone.utc).isoformat()
            self._save_state()
            self._log_trade({
                "side": "BUY", "symbol": symbol, "price": entry_price, "quantity": quantity, "reason": reason,
            })

    def register_sell(self, symbol: str, exit_price: float, reason: str = "manual"):
        with self._lock:
            pos = self.state["open_positions"].pop(symbol, None)
            self.state.setdefault("last_sold", {})[symbol] = datetime.now(timezone.utc).isoformat()
            # Art arda stop-loss sayacı: stop_loss'ta artar, başka bir
            # sebeple (kâr, erken vazgeçme, manuel vb.) satılırsa sıfırlanır -
            # "gerçekten toparlandı" ile "hep aynı yanlış sinyali veriyor"
            # ayrımını yapmak için.
            streak = self.state.setdefault("loss_streak", {})
            if reason == "stop_loss":
                streak[symbol] = streak.get(symbol, 0) + 1
            else:
                streak[symbol] = 0
            self._save_state()
            if not pos:
                return 0.0
            gross_pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
            # Komisyon her iki bacakta da (alım + satım) alınır; testnet'te bile
            # gerçekçi net sonucu görebilmek için modellenmiş oranla düşülür.
            buy_fee = pos["entry_price"] * pos["quantity"] * config.TRADING_FEE_PCT
            sell_fee = exit_price * pos["quantity"] * config.TRADING_FEE_PCT
            pnl = gross_pnl - buy_fee - sell_fee
            self.state["realized_pnl_today"] += pnl
            if self.state["realized_pnl_today"] > self.state["peak_pnl_today"]:
                self.state["peak_pnl_today"] = self.state["realized_pnl_today"]
            self._save_state()
            self._log_trade({
                "side": "SELL", "symbol": symbol, "price": exit_price, "quantity": pos["quantity"],
                "entry_price": pos["entry_price"], "pnl": pnl, "gross_pnl": gross_pnl,
                "fee_paid": buy_fee + sell_fee, "reason": reason,
            })
            return pnl

    def check_exit_conditions(self, symbol: str, current_price: float) -> str | None:
        """Zirve fiyatı (dolayısıyla zirvedeki kâr yüzdesini) pozisyon
        açıldığı andan itibaren SÜREKLİ takip eder - TAKE_PROFIT_PCT'ye
        ulaşılmasını beklemez. Zirve kâr, gürültüyü (komisyon dahil) saymamak
        için MIN_PROFIT_TO_ARM_TRAIL_PCT eşiğini geçer geçmez koruma devreye
        girer: o kârın PROFIT_GIVEBACK_TRAIL_PCT kadarı geri verilirse
        'trailing_stop' ile satılır. Sabit stop-loss her zaman bir güvenlik
        ağı olarak kalır. TAKE_PROFIT_PCT artık sadece panelde "hedef" olarak
        gösterilir, koruma başlangıcını belirlemez.
        """
        with self._lock:
            pos = self.state["open_positions"].get(symbol)
            if not pos:
                return None

            if pos.get("peak_price") is None or current_price > pos["peak_price"]:
                pos["peak_price"] = current_price
                self._save_state()

            peak_profit_pct = pos["peak_price"] / pos["entry_price"] - 1

            if peak_profit_pct >= config.MIN_PROFIT_TO_ARM_TRAIL_PCT:
                giveback_pct = _giveback_pct_for(peak_profit_pct)
                if not pos.get("trailing_active"):
                    pos["trailing_active"] = True
                    self._save_state()
                    logger.info(
                        "%s kâr koruması devreye girdi (zirve %%%.2f) - şu an zirveden %%%.0f geri çekilince satılacak (kademe zirveyle birlikte değişir).",
                        symbol, peak_profit_pct * 100, giveback_pct * 100,
                    )
                giveback_price = pos["entry_price"] * (1 + peak_profit_pct * (1 - giveback_pct))
                if current_price <= giveback_price:
                    # Geri-verme eşiği tetiklendi ama şu anki kâr komisyonu
                    # (alım+satım, ~%0.2) karşılamıyorsa satma - "kâr kilitleme"
                    # amacıyla net zarara dönüşen satışı önlemek için (ör.
                    # SKL_TRY: zirve %1.38, geri düşünce fiyat girişe denk
                    # geldi, brüt fark ~0 ama komisyon net -3.93 TL zarar
                    # yazdırdı). Fiyat daha da düşerse sabit stop-loss zaten
                    # devrede kalır.
                    current_profit_pct = current_price / pos["entry_price"] - 1
                    round_trip_fee_pct = 2 * config.TRADING_FEE_PCT
                    if current_profit_pct > round_trip_fee_pct:
                        return "trailing_stop"
                    logger.info(
                        "%s geri-verme eşiği tetiklendi ama şu anki kâr (%%%.3f) komisyonu (%%%.3f) karşılamıyor - satılmıyor, bekleniyor.",
                        symbol, current_profit_pct * 100, round_trip_fee_pct * 100,
                    )

            if current_price <= pos["stop_loss"]:
                return "stop_loss"

            if not pos.get("trailing_active"):
                opened_at = pos.get("opened_at")
                held_minutes = (
                    (datetime.now(timezone.utc) - datetime.fromisoformat(opened_at)).total_seconds() / 60
                    if opened_at else 0
                )
                profit_pct = current_price / pos["entry_price"] - 1

                # Erken vazgeçme: belirlenen süre içinde hiç anlamlı kâra (%1)
                # ulaşamadıysa, büyük hareketi beklemeyi bırak - komisyonu
                # karşılayan İLK fırsatta (küçük de olsa gerçek bir kâr) çık.
                if held_minutes >= config.EARLY_EXIT_AFTER_MINUTES and profit_pct >= config.EARLY_EXIT_MIN_PROFIT_PCT:
                    logger.info(
                        "%s %d dakikadır %%1 kâra ulaşamadı, komisyonu karşılayan ilk fırsatta (PnL %%%.3f) çıkılıyor.",
                        symbol, held_minutes, profit_pct * 100,
                    )
                    return "early_breakeven_exit"

                # Durgun pozisyon koruması: uzun süredir açık, hiç anlamlı kâra
                # geçmemiş VE komisyonu bile karşılamayacak kadar küçük bir
                # hareket varsa (kâr da zarar da olsa), sermayeyi orada kilitli
                # tutmak yerine çık.
                pnl_pct_abs = abs(profit_pct)
                if held_minutes >= config.STAGNANT_EXIT_MINUTES and pnl_pct_abs < config.STAGNANT_PNL_THRESHOLD_PCT:
                    logger.info(
                        "%s %d dakikadır durgun (PnL %%%.3f, komisyonu karşılamıyor) - çıkılıyor.",
                        symbol, held_minutes, pnl_pct_abs * 100,
                    )
                    return "stagnant_exit"

            return None

    def open_positions(self) -> dict:
        with self._lock:
            return dict(self.state["open_positions"])
