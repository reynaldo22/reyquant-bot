#!/usr/bin/env python3
"""
Confidence Engine — unified signal fusion for reyquant bot.

Runs 4 signal layers in parallel and synthesises ONE confidence score (0-100).
I decide. You execute.

Architecture:
  Layer 1: TA (scanner.py)       → max 40 pts  — backbone
  Layer 2: Big Player            → max 25 pts  — smart money
  Layer 3: Kronos ML             → max 20 pts  — forward-looking
  Layer 4: Macro multiplier      → 0.4x–1.0x  — risk gate

Confidence thresholds:
  ≥75 → STRONG TRADE  — full size, tiered leverage
  60–75 → TRADE       — standard size
  50–60 → WEAK        — half size, min leverage
  <50  → SKIP         — no trade, reason given
"""

import time
import threading
from dataclasses import dataclass, field


# ─── RESULT CONTAINERS ───────────────────────────────────────────────────────

@dataclass
class TALayer:
    score: float = 0.0          # raw scanner score
    direction: str = "NEUTRAL"
    pts: float = 0.0            # 0–40
    reasons: list = field(default_factory=list)
    price: float = 0.0
    atr: float = 0.0
    rsi: float = 50.0
    macd: dict = field(default_factory=dict)
    funding: float = 0.0
    leverage: int = 5
    error: str = ""

@dataclass
class BigPlayerLayer:
    direction: str = "NEUTRAL"
    pts: float = 0.0            # 0–25
    consensus: float = 0.0
    whale_entry: float = 0.0
    oi_trend: str = "FLAT ⚪"
    summary: str = ""
    error: str = ""

@dataclass
class KronosLayer:
    direction: str = "NEUTRAL"
    pts: float = 0.0            # 0–20 (negative if contra)
    pred_close: float = 0.0
    pred_high: float = 0.0
    pred_low: float = 0.0
    change_pct: float = 0.0
    confidence: float = 0.0
    error: str = ""

@dataclass
class MacroLayer:
    level: str = "LOW"
    multiplier: float = 1.0
    hard_block: bool = False
    warnings: list = field(default_factory=list)

@dataclass
class FusionResult:
    symbol: str
    direction: str              # LONG / SHORT / SKIP
    confidence: float           # 0–100
    verdict: str                # STRONG TRADE / TRADE / WEAK / SKIP
    risk_pct: float             # position risk %
    leverage: int
    # price levels
    entry: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    tp3: float = 0.0
    stop: float = 0.0
    hold_time: str = "8–24h"
    # layers
    ta: TALayer = field(default_factory=TALayer)
    bp: BigPlayerLayer = field(default_factory=BigPlayerLayer)
    kr: KronosLayer = field(default_factory=KronosLayer)
    macro: MacroLayer = field(default_factory=MacroLayer)
    # meta
    elapsed_sec: float = 0.0
    skip_reason: str = ""


# ─── LAYER RUNNERS (each in its own thread) ──────────────────────────────────

def _run_ta(symbol: str, out: dict, account_usd: float, risk_pct: float):
    try:
        from scanner import get_klines, get_funding, score_pair, rsi, macd, bollinger, ema_trend, atr, volume_signal
        from algo_rules import get_leverage

        # Build tf_data the same way run() does
        tf_data = {}
        for interval in ["4h", "1h", "15m"]:
            df = get_klines(symbol, interval, limit=100)
            if df is None or len(df) < 30:
                continue
            c = df["close"]
            tf_data[interval] = {
                "price": float(c.iloc[-1]),
                "rsi":   rsi(c),
                "macd":  macd(c),
                "bb":    bollinger(c),
                "ema":   ema_trend(c),
                "atr":   atr(df),
                "vol":   volume_signal(df),
            }

        if "4h" not in tf_data:
            out["ta"] = TALayer(error="No 4h data")
            return

        funding_list = get_funding(symbol)
        funding      = funding_list[0] if funding_list else 0.0

        result    = score_pair(symbol, tf_data, [funding], None)
        raw_score = result.get("score", 0)
        pts       = max(0.0, min(40.0, (abs(raw_score) / 15.0) * 40.0))
        lev, _    = get_leverage(symbol)

        out["ta"] = TALayer(
            score     = raw_score,
            direction = "LONG" if raw_score > 0 else ("SHORT" if raw_score < 0 else "NEUTRAL"),
            pts       = pts,
            reasons   = result.get("reasons", []),
            price     = result.get("price", tf_data["4h"]["price"]),
            atr       = result.get("atr", tf_data["4h"]["atr"]),
            rsi       = tf_data["4h"]["rsi"],
            macd      = tf_data["4h"]["macd"],
            funding   = funding,
            leverage  = lev,
        )
    except Exception as e:
        out["ta"] = TALayer(error=str(e))


def _run_bp(symbol: str, out: dict):
    try:
        from big_player import validate, format_summary
        result = validate(symbol)
        summary = format_summary(result)
        consensus = result.get("score", 0)

        # Normalise to 0–25 pts
        pts = max(0.0, min(25.0, (abs(consensus) / 5.0) * 25.0))

        # Direction from consensus score
        direction = "LONG" if consensus > 0 else ("SHORT" if consensus < 0 else "NEUTRAL")

        out["bp"] = BigPlayerLayer(
            direction   = direction,
            pts         = pts,
            consensus   = consensus,
            whale_entry = result.get("whale_oi", {}).get("estimated_entry_price"),
            oi_trend    = result.get("whale_oi", {}).get("oi_trend", "FLAT ⚪"),
            summary     = summary,
        )
    except Exception as e:
        out["bp"] = BigPlayerLayer(error=str(e))


def _run_kronos(symbol: str, out: dict):
    try:
        from kronos_signal import get_kronos_signal
        sig = get_kronos_signal(symbol)

        if sig.get("error"):
            out["kr"] = KronosLayer(error=sig["error"])
            return

        # Pts: direction confirmed → up to +20 scaled by confidence
        #      direction contra    → negative pts (veto signal)
        base_pts = sig["confidence"] * 20.0  # max 20

        out["kr"] = KronosLayer(
            direction  = sig["direction"],
            pts        = base_pts,
            pred_close = sig["pred_close"],
            pred_high  = sig["pred_high"],
            pred_low   = sig["pred_low"],
            change_pct = sig["change_pct"],
            confidence = sig["confidence"],
        )
    except Exception as e:
        out["kr"] = KronosLayer(error=str(e))


def _run_macro(out: dict):
    try:
        from scanner import macro_risk
        macro = macro_risk()
        out["macro"] = MacroLayer(
            level      = macro.get("level", "LOW"),
            multiplier = macro.get("multiplier", 1.0),
            hard_block = macro.get("hard_block", False),
            warnings   = macro.get("warnings", []),
        )
    except Exception as e:
        out["macro"] = MacroLayer()


# ─── FUSION ──────────────────────────────────────────────────────────────────

def _calc_levels(ta, kr, direction: str):
    """Calculate entry, TP1/2/3, SL from TA + Kronos blend."""
    entry = ta.price
    atr   = ta.atr if ta.atr > 0 else entry * 0.015
    sign  = 1 if direction == "LONG" else -1

    # Primary levels from ATR
    stop = entry - sign * 1.5 * atr
    tp1  = entry + sign * 2.0 * atr
    tp2  = entry + sign * 3.5 * atr
    tp3  = entry + sign * 5.0 * atr

    # If Kronos has valid pred levels, blend them in for TP2/SL
    if not kr.error and kr.pred_close > 0:
        if direction == "LONG":
            # use Kronos predicted high as TP2 reference if higher than ATR TP2
            if kr.pred_high > tp2:
                tp2 = (tp2 + kr.pred_high) / 2
            # tighten stop using Kronos predicted low
            if kr.pred_low > stop and kr.pred_low < entry:
                stop = (stop + kr.pred_low) / 2
        else:
            if kr.pred_low < tp2:
                tp2 = (tp2 + kr.pred_low) / 2
            if kr.pred_high < stop and kr.pred_high > entry:
                stop = (stop + kr.pred_high) / 2

    return round(entry, 4), round(tp1, 4), round(tp2, 4), round(tp3, 4), round(stop, 4)


def _hold_time(confidence: float) -> str:
    if confidence >= 75: return "12–36h"
    if confidence >= 60: return "8–24h"
    return "4–12h"


def fuse(symbol: str, account_usd: float = 1000, risk_pct: float = 1.0,
         timeout: float = 45.0) -> FusionResult:
    """
    Run all 4 signal layers in parallel and return a single FusionResult.
    This is the single entry point — bot.py calls this per pair.
    """
    t0  = time.time()
    out = {}

    # Spawn 4 threads simultaneously
    threads = [
        threading.Thread(target=_run_ta,     args=(symbol, out, account_usd, risk_pct), daemon=True),
        threading.Thread(target=_run_bp,     args=(symbol, out),                         daemon=True),
        threading.Thread(target=_run_kronos, args=(symbol, out),                         daemon=True),
        threading.Thread(target=_run_macro,  args=(out,),                                daemon=True),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=timeout)

    ta    = out.get("ta",    TALayer(error="timeout"))
    bp    = out.get("bp",    BigPlayerLayer(error="timeout"))
    kr    = out.get("kr",    KronosLayer(error="timeout"))
    macro = out.get("macro", MacroLayer())

    # ── Hard block: event < 30min ────────────────────────────────────────────
    if macro.hard_block:
        return FusionResult(
            symbol="", direction="SKIP", confidence=0, verdict="SKIP",
            risk_pct=0, leverage=1,
            ta=ta, bp=bp, kr=kr, macro=macro,
            skip_reason="HARD BLOCK: " + "; ".join(macro.warnings[:2]),
            elapsed_sec=round(time.time()-t0, 1),
        )

    # ── Determine master direction from TA (backbone) ───────────────────────
    if ta.error or ta.direction == "NEUTRAL":
        return FusionResult(
            symbol=symbol, direction="SKIP", confidence=0, verdict="SKIP",
            risk_pct=0, leverage=1,
            ta=ta, bp=bp, kr=kr, macro=macro,
            skip_reason=f"No TA signal: {ta.error or 'neutral'}",
            elapsed_sec=round(time.time()-t0, 1),
        )

    direction = ta.direction  # TA sets master direction

    # ── Layer 1: TA points (0–40) ────────────────────────────────────────────
    l1_pts = ta.pts  # already 0–40

    # ── Layer 2: Big player pts (0–25 if confirms, negative if contra) ───────
    if bp.error:
        l2_pts = 0.0  # unavailable → neutral, don't penalise
    elif bp.direction == direction:
        l2_pts = bp.pts          # confirms → full credit
    elif bp.direction == "NEUTRAL":
        l2_pts = 0.0
    else:
        l2_pts = -bp.pts * 0.5  # contradicts → half penalty

    # ── Layer 3: Kronos pts (0–20 if confirms, negative if contra) ──────────
    if kr.error:
        l3_pts = 0.0  # unavailable → neutral
    elif kr.direction == direction:
        l3_pts = kr.pts          # confirms → full credit
    elif kr.direction == "NEUTRAL":
        l3_pts = 0.0
    else:
        l3_pts = -kr.pts * 0.5  # contradicts → half penalty

    # ── Raw score (0–85 max) → normalise to 0–100 ───────────────────────────
    raw = l1_pts + l2_pts + l3_pts
    raw_capped = max(0.0, min(85.0, raw))
    score_100 = raw_capped / 85.0 * 100.0

    # ── Layer 4: Macro multiplier ────────────────────────────────────────────
    confidence = round(score_100 * macro.multiplier, 1)

    # ── Verdict + sizing ────────────────────────────────────────────────────
    if confidence >= 75:
        verdict   = "STRONG TRADE"
        adj_risk  = round(risk_pct * 1.5, 2)   # 1.5% risk
        adj_lev   = ta.leverage                  # full leverage
    elif confidence >= 60:
        verdict   = "TRADE"
        adj_risk  = round(risk_pct * 1.0, 2)
        adj_lev   = ta.leverage
    elif confidence >= 50:
        verdict   = "WEAK"
        adj_risk  = round(risk_pct * 0.5, 2)
        adj_lev   = max(2, ta.leverage // 2)
    else:
        verdict   = "SKIP"
        adj_risk  = 0.0
        adj_lev   = 1

    # ── Price levels ─────────────────────────────────────────────────────────
    entry, tp1, tp2, tp3, stop = _calc_levels(ta, kr, direction)

    return FusionResult(
        symbol     = symbol,
        direction  = direction if verdict != "SKIP" else "SKIP",
        confidence = confidence,
        verdict    = verdict,
        risk_pct   = adj_risk,
        leverage   = adj_lev,
        entry      = entry,
        tp1        = tp1,
        tp2        = tp2,
        tp3        = tp3,
        stop       = stop,
        hold_time  = _hold_time(confidence),
        ta         = ta,
        bp         = bp,
        kr         = kr,
        macro      = macro,
        elapsed_sec= round(time.time()-t0, 1),
    )


# ─── FORMATTER ────────────────────────────────────────────────────────────────

def format_fusion_card(r, account_usd: float = 1000) -> str:
    """Format FusionResult as a Telegram-ready card."""
    if r.verdict == "SKIP":
        return (
            f"⏭️ *{r.symbol}* — SKIP\n"
            f"_{r.skip_reason}_"
        )

    sym   = r.symbol.replace("USDT", "")
    d_icon = "📈" if r.direction == "LONG" else "📉"
    v_icon = {"STRONG TRADE": "🟢", "TRADE": "🟡", "WEAK": "🟠"}.get(r.verdict, "🔴")

    # Confidence bar (10 chars)
    filled = round(r.confidence / 10)
    bar    = "█" * filled + "░" * (10 - filled)

    # Position size
    if r.entry > 0 and r.stop > 0 and r.risk_pct > 0:
        risk_usd     = account_usd * r.risk_pct / 100
        stop_dist    = abs(r.entry - r.stop)
        units        = risk_usd / stop_dist if stop_dist > 0 else 0
        notional     = units * r.entry
        margin       = notional / r.leverage if r.leverage > 0 else 0
        size_line    = f"${margin:,.0f} margin | ${notional:,.0f} notional"
    else:
        size_line    = "—"

    # TA reasons (top 3)
    ta_reasons = " | ".join(r.ta.reasons[:3]) if r.ta.reasons else "—"

    # Layer agreement icons
    ta_icon = "✅" if not r.ta.error else "❓"
    bp_icon = ("✅" if r.bp.direction == r.direction else
               ("❌" if r.bp.direction not in ("NEUTRAL", "") and not r.bp.error else "➖"))
    kr_icon = ("✅" if r.kr.direction == r.direction else
               ("❌" if r.kr.direction not in ("NEUTRAL", "") and not r.kr.error else "➖"))

    macro_icon = {"LOW":"✅","MEDIUM":"🟡","HIGH":"⚠️","EXTREME":"🚨"}.get(r.macro.level, "➖")

    lines = [
        f"{d_icon} *{sym}/USDT* — {r.direction} [{r.leverage}x]  {v_icon} *{r.verdict}*",
        f"`{bar}` *{r.confidence:.0f}%*",
        f"",
        f"📍 Entry:  `${r.entry:,.2f}`",
        f"🎯 TP1:    `${r.tp1:,.2f}`",
        f"🎯 TP2:    `${r.tp2:,.2f}`",
        f"🎯 TP3:    `${r.tp3:,.2f}`",
        f"🛑 SL:     `${r.stop:,.2f}`",
        f"⏰ Hold:   {r.hold_time}",
        f"",
        f"*SIGNAL LAYERS:*",
        f"  {ta_icon} TA:     `{r.ta.pts:.0f}/40 pts`  — {ta_reasons[:60]}",
        f"  {bp_icon} Whales: `{max(0,r.bp.pts):.0f}/25 pts`  — OI {r.bp.oi_trend}",
        f"  {kr_icon} Kronos: `{max(0,r.kr.pts):.0f}/20 pts`  — {r.kr.change_pct:+.1f}% forecast",
        f"  {macro_icon} Macro:  `×{r.macro.multiplier}` — {r.macro.level}",
        f"",
        f"📐 Size: {size_line}",
    ]

    if r.macro.warnings:
        lines.append(f"⚠️ _{r.macro.warnings[0]}_")

    return "\n".join(lines)


# ─── QUICK TEST ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sym = sys.argv[1].upper() if len(sys.argv) > 1 else "BTCUSDT"
    print(f"Running confidence engine for {sym}...")
    result = fuse(sym, account_usd=1000, risk_pct=1.0)
    print(f"\nRaw result:")
    print(f"  Confidence: {result.confidence}%")
    print(f"  Verdict:    {result.verdict}")
    print(f"  Direction:  {result.direction}")
    print(f"  TA pts:     {result.ta.pts:.1f}/40")
    print(f"  BP pts:     {result.bp.pts:.1f}/25")
    print(f"  Kronos pts: {result.kr.pts:.1f}/20")
    print(f"  Macro:      {result.macro.level} ×{result.macro.multiplier}")
    print(f"  Time:       {result.elapsed_sec}s")
    print(f"\n{'='*50}")
    print(format_fusion_card(result))
