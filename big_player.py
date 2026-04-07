#!/usr/bin/env python3
"""
Big Player Validator
Estimates where large accounts / smart money are positioned for any pair.
Uses: Binance top trader L/S, OKX L/S, HyperLiquid funding+OI,
      OI history (to estimate whale avg entry price), liquidation levels.
"""

import json, time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError
from typing import Optional

HDR = {"User-Agent": "Mozilla/5.0"}

def fetch(url, method="GET", body=None, timeout=10):
    try:
        data = json.dumps(body).encode() if body else None
        if body:
            HDR["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=HDR, method=method)
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except:
        return None

# ── Binance Top Trader L/S (smart money) ─────────────────────────────────────

def binance_top_trader_ls(symbol: str) -> dict:
    """Top 20% largest accounts on Binance — direction they're positioned."""
    url = f"https://fapi.binance.com/futures/data/topLongShortPositionRatio?symbol={symbol}&period=1h&limit=3"
    data = fetch(url)
    if not data or len(data) == 0:
        return {}
    latest = data[-1]
    long_pct  = round(float(latest["longAccount"]) * 100, 1)
    short_pct = round(float(latest["shortAccount"]) * 100, 1)
    ratio     = float(latest["longShortRatio"])

    # Trend: are top traders adding longs or shorts?
    if len(data) >= 2:
        prev_long = float(data[-2]["longAccount"]) * 100
        trend = "ADDING LONGS" if long_pct > prev_long else "ADDING SHORTS"
    else:
        trend = "UNKNOWN"

    if long_pct >= 58:   bias = "STRONGLY LONG 🟢"
    elif long_pct >= 52: bias = "LONG BIAS 🟢"
    elif long_pct >= 48: bias = "NEUTRAL ⚪"
    elif long_pct >= 42: bias = "SHORT BIAS 🔴"
    else:                bias = "STRONGLY SHORT 🔴"

    return {
        "source":    "Binance Top Traders",
        "long_pct":  long_pct,
        "short_pct": short_pct,
        "ratio":     round(ratio, 3),
        "bias":      bias,
        "trend":     trend,
    }

def binance_global_ls(symbol: str) -> dict:
    """All accounts on Binance (retail + smart money combined)."""
    url = f"https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol={symbol}&period=1h&limit=1"
    data = fetch(url)
    if not data or len(data) == 0:
        return {}
    d = data[0]
    long_pct = round(float(d["longAccount"]) * 100, 1)
    return {
        "source":    "Binance Global",
        "long_pct":  long_pct,
        "short_pct": round(float(d["shortAccount"]) * 100, 1),
    }

def bybit_ls(symbol: str) -> dict:
    """Bybit retail long/short ratio."""
    url = f"https://api.bybit.com/v5/market/account-ratio?category=linear&symbol={symbol}&period=1h&limit=1"
    data = fetch(url)
    if not data or data.get("retCode") != 0:
        return {}
    try:
        d = data["result"]["list"][0]
        long_pct = round(float(d["buyRatio"]) * 100, 1)
        if long_pct >= 65:   bias = "CROWDED LONG ⚠️"
        elif long_pct >= 55: bias = "LONG LEANING"
        elif long_pct >= 45: bias = "NEUTRAL"
        elif long_pct >= 35: bias = "SHORT LEANING"
        else:                bias = "CROWDED SHORT 🔥"
        return {
            "source":   "Bybit Retail",
            "long_pct": long_pct,
            "short_pct": round(float(d["sellRatio"]) * 100, 1),
            "bias":     bias,
        }
    except:
        return {}

# ── Whale Avg Entry Estimation ────────────────────────────────────────────────

def estimate_whale_entry(symbol: str) -> dict:
    """
    Estimates where large players opened their positions.
    Method: find when OI started rising significantly → get price at that time.
    Rising OI = new positions being opened = whale entry zone.
    """
    # Get OI history (last 24h in 1h intervals)
    oi_url = f"https://fapi.binance.com/futures/data/openInterestHist?symbol={symbol}&period=1h&limit=24"
    oi_data = fetch(oi_url)

    # Get price history (1h klines)
    kline_url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1h&limit=25"
    kline_data = fetch(kline_url)

    if not oi_data or not kline_data or len(oi_data) < 5:
        return {"error": "insufficient data"}

    # Build OI + price time series
    oi_series   = [float(d["sumOpenInterest"]) for d in oi_data]
    price_series = [float(k[4]) for k in kline_data[-len(oi_data):]]  # close prices

    # Find OI inflection point (where it started rising most)
    # Compare first 6h avg vs last 6h avg
    oi_early = sum(oi_series[:6]) / 6
    oi_late  = sum(oi_series[-6:]) / 6
    oi_change_pct = (oi_late - oi_early) / oi_early * 100 if oi_early > 0 else 0

    # Find the specific hour when OI started rising significantly (> 1% in one period)
    entry_price_estimate = None
    entry_time_ago_h = None
    for i in range(1, len(oi_series)):
        period_change = (oi_series[i] - oi_series[i-1]) / oi_series[i-1] * 100 if oi_series[i-1] > 0 else 0
        if period_change > 1.0:  # OI jumped > 1% in 1h = whales entering
            if i < len(price_series):
                entry_price_estimate = price_series[i]
                entry_time_ago_h = len(oi_series) - i
            break

    current_price = price_series[-1] if price_series else 0

    # OI trend
    if oi_change_pct > 5:    oi_trend = "STRONGLY RISING 🟢"
    elif oi_change_pct > 1:  oi_trend = "RISING 🟢"
    elif oi_change_pct > -1: oi_trend = "FLAT ⚪"
    elif oi_change_pct > -5: oi_trend = "FALLING 🔴"
    else:                    oi_trend = "STRONGLY FALLING 🔴"

    # Are whales in profit or underwater?
    profit_status = None
    if entry_price_estimate:
        pnl_pct = (current_price - entry_price_estimate) / entry_price_estimate * 100
        if abs(pnl_pct) < 1:   profit_status = "AT BREAKEVEN"
        elif pnl_pct > 0:      profit_status = f"IN PROFIT +{pnl_pct:.1f}% (unlikely to dump)"
        else:                  profit_status = f"UNDERWATER {pnl_pct:.1f}% (may add or cut)"

    return {
        "oi_current":    round(oi_series[-1], 2),
        "oi_change_24h": round(oi_change_pct, 2),
        "oi_trend":      oi_trend,
        "estimated_entry_price": round(entry_price_estimate, 4) if entry_price_estimate else None,
        "entry_time_ago_h":      entry_time_ago_h,
        "whale_profit_status":   profit_status,
    }

# ── OKX L/S Ratio ─────────────────────────────────────────────────────────────

def okx_ls(base_ccy: str) -> dict:
    """OKX long/short ratio for cross-exchange validation."""
    url = f"https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={base_ccy}&period=1H&limit=2"
    data = fetch(url)
    if not data or data.get("code") != "0" or not data.get("data"):
        return {}
    try:
        latest = float(data["data"][0][1])
        prev   = float(data["data"][1][1]) if len(data["data"]) > 1 else latest
        trend  = "rising" if latest > prev else "falling"
        bias   = "LONG DOMINANT" if latest > 1.3 else "SHORT DOMINANT" if latest < 0.7 else "NEUTRAL"
        return {"source": "OKX", "ls_ratio": round(latest, 3), "bias": bias, "trend": trend}
    except:
        return {}

# ── HyperLiquid Per-Asset ─────────────────────────────────────────────────────

def hyperliquid_asset(asset_name: str) -> dict:
    """Funding rate + OI for a specific asset on HyperLiquid DEX."""
    data = fetch("https://api.hyperliquid.xyz/info",
                 method="POST", body={"type": "metaAndAssetCtxs"})
    if not data or len(data) < 2:
        return {}
    assets   = data[0].get("universe", [])
    contexts = data[1]
    mids     = fetch("https://api.hyperliquid.xyz/info",
                     method="POST", body={"type": "allMids"}) or {}

    for i, asset in enumerate(assets):
        if asset.get("name", "").upper() == asset_name.upper():
            if i >= len(contexts):
                break
            ctx = contexts[i]
            try:
                funding  = float(ctx.get("funding", 0)) * 100
                mark_px  = float(ctx.get("markPx", 0))
                oi_usd   = float(ctx.get("openInterest", 0)) * float(mids.get(asset_name, 0) or 0)

                if funding > 0.05:   funding_signal = "⚠️ LONGS PAYING (squeeze risk)"
                elif funding < -0.05: funding_signal = "🔥 SHORTS PAYING (squeeze setup)"
                else:                funding_signal = "✅ NEUTRAL"

                return {
                    "source":         "HyperLiquid DEX",
                    "funding_pct":    round(funding, 5),
                    "funding_signal": funding_signal,
                    "oi_usd_m":       round(oi_usd / 1e6, 1),
                    "mark_price":     mark_px,
                }
            except:
                pass
    return {}

# ── OKX Liquidations ──────────────────────────────────────────────────────────

def okx_liq_levels(symbol: str) -> dict:
    """Recent liquidation prices — where the market is flushing."""
    url = f"https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&instId={symbol}&state=filled&limit=15"
    data = fetch(url)
    if not data or data.get("code") != "0":
        return {}

    long_liqs  = []
    short_liqs = []
    for item in data.get("data", []):
        for d in item.get("details", []):
            price  = float(d.get("bkPx", 0))
            side   = d.get("posSide", "")
            if side == "long"  and price: long_liqs.append(price)
            if side == "short" and price: short_liqs.append(price)

    verdict = "BALANCED"
    if len(short_liqs) > len(long_liqs) * 1.5:
        verdict = "🟢 SHORTS GETTING SQUEEZED"
    elif len(long_liqs) > len(short_liqs) * 1.5:
        verdict = "🔴 LONGS GETTING FLUSHED"

    return {
        "long_liqs":   len(long_liqs),
        "short_liqs":  len(short_liqs),
        "verdict":     verdict,
        "long_liq_prices":  sorted(set(round(p) for p in long_liqs))[-3:],
        "short_liq_prices": sorted(set(round(p) for p in short_liqs))[:3],
    }

# ── FULL BIG PLAYER REPORT ────────────────────────────────────────────────────

def validate(symbol: str) -> dict:
    """
    Full big player validation for a symbol.
    Returns consensus direction + whale avg entry estimate.
    """
    base = symbol.replace("USDT","").replace("USDC","")

    top_trader = binance_top_trader_ls(symbol)
    time.sleep(0.1)
    global_ls  = binance_global_ls(symbol)
    time.sleep(0.1)
    bybit      = bybit_ls(symbol)
    time.sleep(0.1)
    whale_oi   = estimate_whale_entry(symbol)
    time.sleep(0.1)
    okx        = okx_ls(base)
    time.sleep(0.1)
    hl         = hyperliquid_asset(base)
    time.sleep(0.1)
    liqs       = okx_liq_levels(f"{base}-USDT")

    # Consensus scoring
    score = 0
    if top_trader.get("long_pct", 50) >= 52: score += 2
    elif top_trader.get("long_pct", 50) < 48: score -= 2
    if bybit.get("long_pct", 50) < 60: score += 1   # not crowded = safer long
    elif bybit.get("long_pct", 50) >= 65: score -= 1  # too crowded
    if whale_oi.get("oi_trend", "") in ("RISING 🟢", "STRONGLY RISING 🟢"): score += 2
    if liqs.get("verdict", "").startswith("🟢"): score += 2
    elif liqs.get("verdict", "").startswith("🔴"): score -= 2
    if hl.get("funding_pct", 0) < -0.03: score += 1  # negative funding = squeeze

    if score >= 3:   consensus = "🟢 BIG PLAYERS LONG — confirms buy signal"
    elif score <= -3: consensus = "🔴 BIG PLAYERS SHORT — confirms sell signal"
    else:            consensus = "⚪ MIXED — proceed with caution"

    return {
        "symbol":      symbol,
        "consensus":   consensus,
        "score":       score,
        "top_trader":  top_trader,
        "bybit_retail": bybit,
        "whale_oi":    whale_oi,
        "okx":         okx,
        "hyperliquid": hl,
        "liquidations": liqs,
    }

def format_summary(v: dict) -> str:
    """Short summary for Telegram."""
    tt  = v.get("top_trader", {})
    ret = v.get("bybit_retail", {})
    hl  = v.get("hyperliquid", {})
    oi  = v.get("whale_oi", {})
    liq = v.get("liquidations", {})

    entry_str = ""
    if oi.get("estimated_entry_price"):
        entry_str = f"\n  Est. whale entry: ~${oi['estimated_entry_price']:,.2f} ({oi.get('entry_time_ago_h','?')}h ago)"
        if oi.get("whale_profit_status"):
            entry_str += f"\n  Status: {oi['whale_profit_status']}"

    return f"""🧠 BIG PLAYER VALIDATION
  Binance smart$: {tt.get('long_pct','?')}% long → {tt.get('bias','')} ({tt.get('trend','')})
  Bybit retail:   {ret.get('long_pct','?')}% long → {ret.get('bias','')}
  HL funding:     {hl.get('funding_pct','?')}% → {hl.get('funding_signal','')}
  OI trend:       {oi.get('oi_trend','')} ({oi.get('oi_change_24h','?')}% 24h){entry_str}
  Liquidations:   {liq.get('verdict','')}
  ──────────────────────────────
  CONSENSUS: {v.get('consensus','')}"""

if __name__ == "__main__":
    import sys
    sym = sys.argv[1].upper() if len(sys.argv) > 1 else "BTCUSDT"
    print(f"Validating {sym}...")
    result = validate(sym)
    print(format_summary(result))
