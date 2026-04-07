#!/usr/bin/env python3
"""
Trade Card Generator
Generates complete, executable trade cards for Binance Futures.
Output: entry zone, no-trade zone, TP1/TP2/TP3, SL, hold time,
        position size with leverage, skip conditions, big player validation.
"""

import json, sys, time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from typing import Optional

HDR = {"User-Agent": "Mozilla/5.0"}

try:
    import pandas as pd
    import numpy as np
    HAS_PANDAS = True
except:
    HAS_PANDAS = False

# ── LEVERAGE CONFIG ───────────────────────────────────────────────────────────
LEVERAGE_MAP = {
    "BTC": (20, 1.40), "ETH": (20, 1.40),
    "BNB": (10, 2.30), "SOL": (10, 2.30), "XRP": (10, 2.30),
    "ADA": (5, 4.5),   "DOT": (5, 4.5),   "AVAX": (5, 4.5),
    "SUI": (5, 4.5),   "TAO": (5, 4.5),   "LINK": (5, 4.5),
    "DOGE": (5, 4.5),  "PEPE": (3, 6.0),  "TRU": (3, 6.0),
    "_DEFAULT": (5, 4.5),
}

def get_lev(symbol: str):
    base = symbol.replace("USDT","").replace("USDC","")
    for k, v in LEVERAGE_MAP.items():
        if k != "_DEFAULT" and base.startswith(k):
            return v
    return LEVERAGE_MAP["_DEFAULT"]

# ── HOLD TIME BY SIGNAL STRENGTH ─────────────────────────────────────────────
def hold_time(score: int, primary_tf: str = "4h") -> str:
    if score >= 7:
        return "Up to 72h (strong signal — let it run)"
    elif score >= 5:
        return "Up to 48h (good signal)"
    elif score >= 3:
        return "Up to 24h (moderate signal — watch closely)"
    else:
        return "Up to 8h (weak signal — scalp only)"

# ── NO-TRADE ZONE CALCULATOR ─────────────────────────────────────────────────
def no_trade_zones(entry: float, atr: float, direction: str,
                   whale_entry: Optional[float], max_pain: Optional[float]) -> list:
    """
    Defines price ranges where opening a new position is dangerous.
    """
    zones = []
    sign = 1 if direction == "LONG" else -1

    # Zone 1: Already past TP1 (chasing)
    tp1 = entry + sign * atr * 2.0
    zones.append({
        "zone":   f"Above ${tp1:,.2f}" if direction == "LONG" else f"Below ${tp1:,.2f}",
        "reason": "Past TP1 — you're chasing, risk/reward destroyed"
    })

    # Zone 2: Near liquidation buffer (within 2x ATR of liq price)
    lev, max_stop_pct = get_lev("XXXX")
    liq_price = entry * (1 - sign * (1/lev) * 0.95)
    danger_zone = liq_price + sign * atr * 2
    zones.append({
        "zone":   f"Below ${danger_zone:,.4f}" if direction == "LONG" else f"Above ${danger_zone:,.4f}",
        "reason": f"Too close to liquidation at ${liq_price:,.2f}"
    })

    # Zone 3: If whale entry is known and price is far above it
    if whale_entry:
        dist_pct = abs(entry - whale_entry) / whale_entry * 100
        if direction == "LONG" and entry > whale_entry * 1.05:
            zones.append({
                "zone":   f"Above ${whale_entry * 1.05:,.2f}",
                "reason": f"Price 5%+ above whale avg entry ${whale_entry:,.2f} — late entry"
            })

    # Zone 4: Options max pain gravity
    if max_pain:
        mp_low  = max_pain * 0.98
        mp_high = max_pain * 1.02
        zones.append({
            "zone":   f"${mp_low:,.0f} – ${mp_high:,.0f}",
            "reason": f"Options max pain zone — price tends to stall here"
        })

    return zones

# ── SKIP CONDITIONS ───────────────────────────────────────────────────────────
SKIP_CONDITIONS = [
    ("VIX > 35",                    "Market panic — reduce leverage or skip"),
    ("Funding > +0.10%",            "Longs paying too much — flush risk"),
    ("Macro event < 2h",            "Forced close — never hold into FOMC/CPI/PCE"),
    ("BTC -5% in past 1h",          "Flash crash — close all, wait for dust to settle"),
    ("Top traders > 65% opposite",  "Smart money strongly against you — skip"),
    ("Volume < 80% of 20d avg",     "Low participation — false signal risk"),
    ("Spread > 0.05%",              "Low liquidity — slippage risk"),
    ("24h volume < $50M",           "Too illiquid for futures — skip pair"),
    ("Already at TP1 level",        "Chasing — no trade, wait for pullback"),
    ("OI falling + price rising",   "Distribution — smart money exiting into your buys"),
]

# ── POSITION SIZING ───────────────────────────────────────────────────────────
def size_position(account: float, risk_pct: float,
                  entry: float, stop: float, leverage: int) -> dict:
    risk_usd  = account * (risk_pct / 100)
    stop_dist = abs(entry - stop)
    if stop_dist == 0:
        return {}
    units     = risk_usd / stop_dist
    notional  = units * entry
    margin    = notional / leverage
    liq_price = entry * (1 - (1/leverage) * 0.95)  # simplified for long
    return {
        "units":        round(units, 6),
        "notional_usd": round(notional, 2),
        "margin_usd":   round(margin, 2),
        "margin_pct":   round(margin / account * 100, 1),
        "risk_usd":     round(risk_usd, 2),
        "liq_price":    round(liq_price, 4),
    }

# ── TRADE CARD GENERATOR ──────────────────────────────────────────────────────

def generate(
    symbol: str,
    direction: str,
    entry: float,
    atr: float,
    score: int,
    rsi: float,
    macd_cross: Optional[str],
    funding: float,
    oi_trend: str,
    account: float = 1000.0,
    risk_pct: float = 1.0,
    whale_entry: Optional[float] = None,
    max_pain: Optional[float] = None,
    big_player_summary: str = "",
    macro_risk: str = "LOW",
    macro_warning: str = "",
) -> dict:

    lev, max_stop_pct = get_lev(symbol)
    sign = 1 if direction == "LONG" else -1

    # Levels (ATR-based)
    stop_dist = min(atr * 1.5, entry * (max_stop_pct / 100))
    stop      = round(entry - sign * stop_dist, 6)
    tp1       = round(entry + sign * atr * 2.0,  6)
    tp2       = round(entry + sign * atr * 3.5,  6)
    tp3       = round(entry + sign * atr * 5.5,  6)

    # Entry zone (slightly better than current — wait for small pullback)
    entry_low  = round(entry - sign * atr * 0.3, 6)  # slight pullback entry
    entry_high = round(entry + sign * atr * 0.2, 6)  # max chase entry

    rr_tp1 = round(abs(tp1 - entry) / stop_dist, 2)
    rr_tp2 = round(abs(tp2 - entry) / stop_dist, 2)

    # Position sizing
    pos = size_position(account, risk_pct, entry, stop, lev)

    # Hold time
    hold = hold_time(score)

    # No-trade zones
    no_zones = no_trade_zones(entry, atr, direction, whale_entry, max_pain)

    # Active skip conditions
    active_skips = []
    if macro_risk in ("HIGH", "EXTREME"):
        active_skips.append(f"🚨 {macro_warning}")
    if funding > 0.001 and direction == "LONG":
        active_skips.append(f"⚠️ Funding {funding*100:.4f}% — longs paying too much")
    if rsi > 68 and direction == "LONG":
        active_skips.append(f"⚠️ RSI {rsi:.0f} — near overbought")
    if oi_trend in ("FALLING 🔴", "STRONGLY FALLING 🔴"):
        active_skips.append(f"⚠️ OI falling — smart money exiting")

    # Signal confidence
    if score >= 7:     conf = "HIGH ⭐⭐⭐"
    elif score >= 5:   conf = "MEDIUM ⭐⭐"
    elif score >= 3:   conf = "LOW ⭐"
    else:              conf = "WEAK — SKIP"

    return {
        "symbol":      symbol,
        "direction":   direction,
        "leverage":    lev,
        "confidence":  conf,
        "score":       score,
        "entry_zone":  (entry_low, entry_high),
        "stop":        stop,
        "tp1":         tp1,
        "tp2":         tp2,
        "tp3":         tp3,
        "rr_tp1":      rr_tp1,
        "rr_tp2":      rr_tp2,
        "hold_time":   hold,
        "position":    pos,
        "no_zones":    no_zones[:3],
        "skip_active": active_skips,
        "big_player":  big_player_summary,
        "macro_risk":  macro_risk,
        "signals": {
            "rsi":       rsi,
            "macd_cross": macd_cross,
            "funding":   round(funding * 100, 5),
            "oi_trend":  oi_trend,
        }
    }

def format_telegram(card: dict) -> str:
    """Format trade card for Telegram — mobile-optimized."""
    d   = card["direction"]
    sym = card["symbol"]
    lev = card["leverage"]
    pos = card["position"]
    ez  = card["entry_zone"]
    sig = card["signals"]

    dir_icon  = "📈" if d == "LONG" else "📉"
    conf_icon = "🟢" if "HIGH" in card["confidence"] else "🟡" if "MEDIUM" in card["confidence"] else "🔴"

    lines = [
        f"{'━'*32}",
        f"{dir_icon} *{sym}* — {d} [{lev}x]  {conf_icon} {card['confidence']}",
        f"{'━'*32}",
        f"",
        f"📍 *ENTRY ZONE*",
        f"   ${ez[0]:,.4f} – ${ez[1]:,.4f}",
        f"",
        f"🎯 *TAKE PROFIT*",
        f"   TP1: `${card['tp1']:,.4f}`  (+{abs(card['tp1']-ez[0])/ez[0]*100:.1f}%)  → close 50%",
        f"   TP2: `${card['tp2']:,.4f}`  (+{abs(card['tp2']-ez[0])/ez[0]*100:.1f}%)  → close 30%",
        f"   TP3: `${card['tp3']:,.4f}`  (+{abs(card['tp3']-ez[0])/ez[0]*100:.1f}%)  → close 20%",
        f"",
        f"🛑 *STOP LOSS*",
        f"   `${card['stop']:,.4f}`  (-{abs(card['stop']-ez[0])/ez[0]*100:.1f}%)",
        f"   R:R → 1:{card['rr_tp1']} (TP1)  |  1:{card['rr_tp2']} (TP2)",
        f"",
        f"💰 *POSITION SIZE* (${pos.get('notional_usd',0):,.0f} notional)",
        f"   Units:  {pos.get('units',0):.4f}",
        f"   Margin: ${pos.get('margin_usd',0):.2f} ({pos.get('margin_pct',0):.1f}% of acct)",
        f"   Liq:    ~${pos.get('liq_price',0):,.2f}",
        f"   Risk:   ${pos.get('risk_usd',0):.2f} max loss",
        f"",
        f"⏱️ *HOLD TIME*",
        f"   {card['hold_time']}",
        f"",
        f"🚫 *NO-TRADE ZONES*",
    ]

    for z in card["no_zones"]:
        lines.append(f"   • {z['zone']}")
        lines.append(f"     ↳ {z['reason']}")

    lines += [
        f"",
        f"⚡ *SIGNALS*",
        f"   RSI: {sig['rsi']:.0f}  |  Funding: {sig['funding']:.4f}%",
        f"   MACD: {sig['macd_cross'] or 'bullish'}  |  OI: {sig['oi_trend']}",
    ]

    if card["big_player"]:
        lines += [f"", card["big_player"]]

    if card["skip_active"]:
        lines += [f"", f"⛔ *ACTIVE WARNINGS*"]
        for s in card["skip_active"]:
            lines.append(f"   {s}")
    else:
        lines += [f"", f"✅ *No active skip conditions*"]

    lines += [
        f"",
        f"📏 *SKIP IF:*",
        f"   VIX>35 | Funding>0.1% | Event<2h",
        f"   BTC flash crash | Volume<80% avg",
        f"{'━'*32}",
    ]

    return "\n".join(lines)

if __name__ == "__main__":
    # Test
    card = generate(
        symbol="BTCUSDT", direction="LONG",
        entry=69350, atr=825, score=7,
        rsi=43, macd_cross="BULL_CROSS",
        funding=0.00005, oi_trend="RISING 🟢",
        account=1000, risk_pct=1.0,
        whale_entry=67500, max_pain=71000,
    )
    print(format_telegram(card))
