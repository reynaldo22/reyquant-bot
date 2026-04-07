#!/usr/bin/env python3
"""
ALGO TRADING RULES ENGINE
Full algorithmic decision system — no ego, 100% rule-based.
Defines: entry conditions, position sizing, exit rules, time filters.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
import math

# ─── ACCOUNT CONFIG ───────────────────────────────────────────────────────────

@dataclass
class AccountConfig:
    balance:        float = 1000.0   # USDT
    risk_pct:       float = 1.0      # % of account per trade
    max_positions:  int   = 3        # simultaneous open
    max_corr_same:  int   = 2        # max same-direction positions
    max_drawdown_pct: float = 10.0   # % from peak → STOP trading

# ─── LEVERAGE TIER RULES ──────────────────────────────────────────────────────

LEVERAGE_TIERS = {
    # pair prefix → (leverage, max_stop_pct)
    "BTC":  (20, 1.5),   # 20x, stop max 1.5% (liquidation at -5%)
    "ETH":  (20, 1.5),   # 20x, stop max 1.5%
    "BNB":  (10, 2.5),   # 10x, stop max 2.5% (liquidation at -10%)
    "SOL":  (10, 2.5),
    "XRP":  (10, 2.5),
    "ADA":  (5,  5.0),   # 5x, stop max 5% (liquidation at -20%)
    "DOT":  (5,  5.0),
    "AVAX": (5,  5.0),
    "SUI":  (5,  5.0),
    "TAO":  (5,  5.0),
    "DOGE": (5,  5.0),
    "PEPE": (3,  7.0),   # 3x for meme coins
    "_DEFAULT": (5, 5.0),
}

def get_leverage(symbol: str) -> Tuple[int, float]:
    """Returns (leverage, max_stop_pct) for a symbol."""
    base = symbol.replace("USDT","").replace("USDC","")
    for key in LEVERAGE_TIERS:
        if base.startswith(key) and key != "_DEFAULT":
            return LEVERAGE_TIERS[key]
    return LEVERAGE_TIERS["_DEFAULT"]

# ─── ENTRY CONDITIONS ─────────────────────────────────────────────────────────

@dataclass
class EntryConditions:
    """ALL conditions must be True to enter a trade."""

    # Technical (from scanner)
    macd_4h_bullish:     bool = False   # 4h MACD line > signal line
    macd_4h_crossover:   bool = False   # fresh 4h MACD bullish crossover
    macd_1h_bullish:     bool = False   # 1h MACD aligned
    rsi_4h_range:        bool = False   # RSI between 25-65 (not overbought)
    ema_4h_up:           bool = False   # EMA9 > EMA21 on 4h
    volume_confirmed:    bool = False   # Volume > 80% of 20-period avg

    # Market structure
    funding_safe:        bool = False   # < +0.05% for longs | > -0.05% for shorts
    not_crowded:         bool = False   # Top trader L/S not extreme opposite
    oi_rising:          bool = False    # OI trend = RISING

    # Macro gates
    no_imminent_event:  bool = False   # No HIGH impact event in next 6h
    not_dead_zone:      bool = False   # NOT 22:00-01:00 UTC
    vix_below_35:       bool = False   # VIX < 35

    def is_valid_long(self) -> Tuple[bool, list]:
        """Returns (can_trade, list_of_failed_checks)."""
        failed = []
        checks = {
            "4h MACD bullish":    self.macd_4h_bullish,
            "1h MACD aligned":    self.macd_1h_bullish,
            "RSI not overbought": self.rsi_4h_range,
            "Funding safe":       self.funding_safe,
            "Not crowded":        self.not_crowded,
            "No macro event":     self.no_imminent_event,
            "Not dead zone":      self.not_dead_zone,
            "VIX < 35":           self.vix_below_35,
        }
        # Must have: MACD crossover OR (4h bullish + 1h bullish) + RSI
        has_signal = (self.macd_4h_crossover or
                      (self.macd_4h_bullish and self.macd_1h_bullish))
        if not has_signal:
            failed.append("No MACD signal (need 4h crossover or 4h+1h aligned)")
        for name, cond in checks.items():
            if not cond:
                failed.append(name)
        return len(failed) == 0, failed

    def score(self) -> int:
        """Signal strength score 0-10."""
        pts = [
            self.macd_4h_crossover * 3,
            self.macd_4h_bullish * 1,
            self.macd_1h_bullish * 1,
            self.rsi_4h_range * 1,
            self.ema_4h_up * 1,
            self.volume_confirmed * 1,
            self.funding_safe * 1,
            self.not_crowded * 1,
        ]
        return sum(pts)

# ─── POSITION SIZING ─────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol:       str
    direction:    str        # LONG or SHORT
    entry:        float
    atr_4h:       float
    account:      AccountConfig

    leverage:     int   = field(init=False)
    max_stop_pct: float = field(init=False)
    stop_loss:    float = field(init=False)
    tp1:          float = field(init=False)
    tp2:          float = field(init=False)
    units:        float = field(init=False)
    notional:     float = field(init=False)
    margin:       float = field(init=False)
    risk_usd:     float = field(init=False)
    liq_price:    float = field(init=False)
    rr_tp1:       float = field(init=False)
    rr_tp2:       float = field(init=False)

    def __post_init__(self):
        self.leverage, self.max_stop_pct = get_leverage(self.symbol)
        self._calculate()

    def _calculate(self):
        sign = 1 if self.direction == "LONG" else -1

        # Stop: 1.5x ATR but capped at max_stop_pct for safety
        atr_stop   = self.atr_4h * 1.5
        pct_stop   = self.entry * (self.max_stop_pct / 100)
        stop_dist  = min(atr_stop, pct_stop)  # use tighter of the two

        self.stop_loss = round(self.entry - sign * stop_dist, 6)
        self.tp1       = round(self.entry + sign * self.atr_4h * 2.0, 6)
        self.tp2       = round(self.entry + sign * self.atr_4h * 3.5, 6)

        # Position sizing: risk = 1% of account
        risk_usd   = self.account.balance * (self.account.risk_pct / 100)
        self.units = risk_usd / stop_dist
        self.notional = self.units * self.entry
        self.margin   = self.notional / self.leverage
        self.risk_usd = risk_usd

        # Liquidation price (Binance formula simplified)
        # Liq ≈ Entry - (Entry / leverage) for isolated margin long
        self.liq_price = round(
            self.entry * (1 - sign * (1 / self.leverage) * 0.95), 4
        )

        # R:R
        self.rr_tp1 = round(abs(self.tp1 - self.entry) / stop_dist, 2)
        self.rr_tp2 = round(abs(self.tp2 - self.entry) / stop_dist, 2)

    def safety_check(self) -> Tuple[bool, list]:
        """Validates position is safe to enter."""
        warnings = []

        # Stop must fire BEFORE liquidation
        sign = 1 if self.direction == "LONG" else -1
        if self.direction == "LONG" and self.stop_loss < self.liq_price:
            warnings.append(f"CRITICAL: Stop ${self.stop_loss:.2f} is BELOW liquidation ${self.liq_price:.2f}!")
        if self.direction == "SHORT" and self.stop_loss > self.liq_price:
            warnings.append(f"CRITICAL: Stop ${self.stop_loss:.2f} is ABOVE liquidation ${self.liq_price:.2f}!")

        # Margin shouldn't exceed 20% of account
        if self.margin > self.account.balance * 0.20:
            warnings.append(f"Margin ${self.margin:.2f} > 20% of account — reduce risk %")

        # R:R check
        if self.rr_tp1 < 1.3:
            warnings.append(f"R:R {self.rr_tp1} < 1.3 minimum — skip this trade")

        return len(warnings) == 0, warnings

    def summary(self) -> str:
        safe, warns = self.safety_check()
        status = "✅ SAFE" if safe else "⚠️ WARNINGS"
        liq_buffer = abs(self.entry - self.liq_price) / self.entry * 100

        return f"""
╔══ {self.symbol} {self.direction} [{self.leverage}x] ══╗
  Entry:        {self.entry:>12,.4f}
  Stop Loss:    {self.stop_loss:>12,.4f}  ({abs(self.entry-self.stop_loss)/self.entry*100:.2f}% away)
  Take Profit 1:{self.tp1:>12,.4f}  R:R 1:{self.rr_tp1}
  Take Profit 2:{self.tp2:>12,.4f}  R:R 1:{self.rr_tp2}
  Liq Price:    {self.liq_price:>12,.4f}  ({liq_buffer:.2f}% buffer to liq)
  ─────────────────────────────
  Units:        {self.units:.6f}
  Notional:     ${self.notional:>10,.2f}
  Margin:       ${self.margin:>10,.2f}  ({self.margin/self.account.balance*100:.1f}% of account)
  Max Risk:     ${self.risk_usd:>10,.2f}  ({self.account.risk_pct:.1f}% of account)
  Status:       {status}
{chr(10).join('  ⚠️ ' + w for w in warns)}
╚{'═'*30}╝"""

# ─── EXIT ALGORITHM ───────────────────────────────────────────────────────────

class ExitAlgo:
    """All exit conditions — when to close a trade."""

    @staticmethod
    def check_exit(
        position_dir: str,
        entry: float,
        current_price: float,
        stop_loss: float,
        tp1: float,
        tp2: float,
        atr: float,
        open_minutes: int,
        funding_rate: float,
        vix: float,
        macro_event_in_minutes: Optional[int],
        btc_hourly_drop_pct: float = 0.0,
        trailing_stop: Optional[float] = None,
    ) -> Tuple[str, str]:
        """
        Returns: (action, reason)
        action = "HOLD" | "CLOSE_ALL" | "CLOSE_HALF" | "MOVE_STOP_BE"
        """
        sign = 1 if position_dir == "LONG" else -1
        pnl_pct = sign * (current_price - entry) / entry * 100

        # ── FORCED EXITS (highest priority) ────────────────────────────────
        # 1. Stop Loss Hit
        if position_dir == "LONG" and current_price <= stop_loss:
            return "CLOSE_ALL", f"🔴 STOP LOSS hit at ${current_price:,.4f}"
        if position_dir == "SHORT" and current_price >= stop_loss:
            return "CLOSE_ALL", f"🔴 STOP LOSS hit at ${current_price:,.4f}"

        # 2. Macro event approaching
        if macro_event_in_minutes is not None and macro_event_in_minutes <= 120:
            return "CLOSE_ALL", f"🚨 HIGH IMPACT EVENT in {macro_event_in_minutes}min — mandatory close"

        # 3. Flash crash protection (BTC drops > 5% in 1h)
        if btc_hourly_drop_pct < -5.0:
            return "CLOSE_ALL", f"💥 Flash crash detected ({btc_hourly_drop_pct:.1f}% in 1h)"

        # 4. Funding rate spike
        if position_dir == "LONG" and funding_rate > 0.001:
            return "CLOSE_ALL", f"⚠️ Funding rate {funding_rate*100:.4f}% too high — paying too much"

        # 5. VIX spike
        if vix > 35:
            return "CLOSE_ALL", f"🔴 VIX {vix:.1f} > 35 — extreme market fear"

        # ── TAKE PROFITS ────────────────────────────────────────────────────
        # 6. TP2 hit — close everything
        if position_dir == "LONG" and current_price >= tp2:
            return "CLOSE_ALL", f"🎯 TP2 HIT at ${current_price:,.4f} (+{pnl_pct:.2f}%) 🏆"
        if position_dir == "SHORT" and current_price <= tp2:
            return "CLOSE_ALL", f"🎯 TP2 HIT at ${current_price:,.4f} (+{pnl_pct:.2f}%) 🏆"

        # 7. TP1 hit — close half, move stop to breakeven
        if position_dir == "LONG" and current_price >= tp1:
            be_stop = round(entry + 0.3 * atr, 4)
            return "CLOSE_HALF", f"🎯 TP1 HIT at ${current_price:,.4f} — close 50%, move stop to ${be_stop:,.4f}"
        if position_dir == "SHORT" and current_price <= tp1:
            be_stop = round(entry - 0.3 * atr, 4)
            return "CLOSE_HALF", f"🎯 TP1 HIT at ${current_price:,.4f} — close 50%, move stop to ${be_stop:,.4f}"

        # 8. Trailing stop (after TP1)
        if trailing_stop is not None:
            if position_dir == "LONG" and current_price <= trailing_stop:
                return "CLOSE_ALL", f"📉 Trailing stop triggered at ${trailing_stop:,.4f}"
            if position_dir == "SHORT" and current_price >= trailing_stop:
                return "CLOSE_ALL", f"📉 Trailing stop triggered at ${trailing_stop:,.4f}"

        # ── TIME EXITS ──────────────────────────────────────────────────────
        # 9. Stale trade: < 1% movement in 8 hours
        if open_minutes >= 480 and abs(pnl_pct) < 1.0:
            return "CLOSE_ALL", f"⏱️ Stale trade — {open_minutes//60}h open, only {pnl_pct:.2f}% movement"

        # 10. Max hold time: 72 hours
        if open_minutes >= 4320:
            return "CLOSE_ALL", f"⏰ Max hold time (72h) reached — ${pnl_pct:+.2f}%"

        return "HOLD", f"👁️ Monitoring — {pnl_pct:+.2f}% | {open_minutes//60}h open"

# ─── TIME FILTER ─────────────────────────────────────────────────────────────

def is_valid_trading_time() -> Tuple[bool, str]:
    """
    Returns (can_trade, reason).
    Dead zones: 22:00-01:00 UTC (low liquidity).
    Best windows: 09:00-11:00 UTC (London), 14:00-16:00 UTC (NY).
    """
    now = datetime.now(timezone.utc)
    hour = now.hour

    if 22 <= hour or hour < 1:
        return False, f"🕐 Dead zone ({now.strftime('%H:%M')} UTC) — low liquidity, high spread"

    windows = {
        (6, 9):   "Asia close / London pre-open",
        (9, 12):  "🔥 LONDON OPEN — peak liquidity",
        (13, 16): "🔥 NEW YORK OPEN — peak liquidity",
        (19, 22): "NY close / Asia pre-open — moderate",
    }
    for (start, end), label in windows.items():
        if start <= hour < end:
            return True, f"✅ Good time: {label} ({now.strftime('%H:%M')} UTC)"

    return True, f"✅ Normal hours ({now.strftime('%H:%M')} UTC)"

# ─── ALGO SCHEDULE ────────────────────────────────────────────────────────────

SCAN_SCHEDULE = [
    (6,  30, "Pre-London scan"),
    (9,   0, "🔥 London open scan"),
    (12,  0, "Midday check"),
    (14,  0, "🔥 New York open scan"),
    (18,  0, "Evening scan"),
    (21, 30, "Final scan before dead zone"),
]

def next_scan_time() -> str:
    """Returns time until next scheduled scan."""
    now = datetime.now(timezone.utc)
    for hour, minute, label in SCAN_SCHEDULE:
        t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if t <= now:
            t += timedelta(days=1)
        diff = t - now
        hours = int(diff.total_seconds() // 3600)
        mins  = int((diff.total_seconds() % 3600) // 60)
        if diff.total_seconds() < 86400:
            return f"Next: {label} in {hours}h {mins}m ({t.strftime('%H:%M')} UTC)"
    return "Unknown"

# ─── DAILY ALGO CHECKLIST ─────────────────────────────────────────────────────

DAILY_CHECKLIST = """
╔══════════════════════════════════════════╗
║     ALGO TRADING DAILY CHECKLIST         ║
╠══════════════════════════════════════════╣
║ BEFORE ANY TRADE:                        ║
║ □ Check macro calendar (6h lookout)      ║
║ □ Funding rate < 0.05%                   ║
║ □ Not in dead zone (22:00-01:00 UTC)     ║
║ □ 4h candle CLOSED (not mid-candle)      ║
║ □ Max 3 open positions                   ║
║                                          ║
║ ENTRY RULES:                             ║
║ □ 4h MACD crossover OR 4h+1h aligned    ║
║ □ RSI 25-65 range                        ║
║ □ Volume > 80% of 20-period avg          ║
║                                          ║
║ POSITION SIZING:                         ║
║ □ Risk = 1% of account ($10)             ║
║ □ BTC/ETH: 20x, stop < 1.5%             ║
║ □ SOL/BNB/XRP: 10x, stop < 2.5%        ║
║ □ Alts: 5x, stop < 5%                   ║
║ □ Margin < 20% of account per trade     ║
║                                          ║
║ EXIT RULES (non-negotiable):             ║
║ □ Stop hit → CLOSE immediately           ║
║ □ TP1 hit → Close 50%, move stop to BE  ║
║ □ TP2 hit → Close everything             ║
║ □ 8h no movement < 1% → EXIT            ║
║ □ 72h max hold → EXIT                   ║
║ □ Macro event 2h away → CLOSE ALL       ║
╚══════════════════════════════════════════╝
"""

if __name__ == "__main__":
    # Test the system
    acc = AccountConfig(balance=1000, risk_pct=1.0)

    print("=== BTC/USDT 20x LONG ===")
    btc = Position("BTCUSDT", "LONG", entry=69350, atr_4h=825, account=acc)
    print(btc.summary())

    print("\n=== TAO/USDT 5x LONG ===")
    tao = Position("TAOUSDT", "LONG", entry=320, atr_4h=25, account=acc)
    print(tao.summary())

    print("\n=== ETH/USDT 20x SHORT ===")
    eth = Position("ETHUSDT", "SHORT", entry=2143, atr_4h=32, account=acc)
    print(eth.summary())

    print("\n=== VALID TRADING TIME? ===")
    can_trade, reason = is_valid_trading_time()
    print(f"{'✅' if can_trade else '❌'} {reason}")

    print(f"\n{next_scan_time()}")
    print(DAILY_CHECKLIST)
