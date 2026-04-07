#!/usr/bin/env python3
"""
Whale Intelligence Scanner — 100% FREE, no API keys.
Sources:
  - OKX: real liquidation orders (long/short flushes happening right now)
  - Bybit: insurance fund, OI history, top trader L/S
  - Binance: top trader position ratios for BTC/ETH/SOL
  - HyperLiquid: DEX perp funding + OI (cross-exchange check)
  - Blockchair: on-chain whale transactions (ETH/BTC large moves)
  - Mempool.space: BTC mempool state (whale urgency indicator)
  - DefiLlama: TVL trend (capital flow into/out of DeFi)
  - OKX: cross-exchange L/S ratio
"""

import json, time, sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

HDR = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

def fetch(url, method="GET", body=None, timeout=10):
    try:
        data = json.dumps(body).encode() if body else None
        if body:
            HDR["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=HDR, method=method)
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return None

# ── OKX Liquidations ──────────────────────────────────────────────────────────

def okx_liquidations(symbol="BTC-USDT", limit=20):
    """Real liquidation orders from OKX — shows where the market is flushing."""
    url = (f"https://www.okx.com/api/v5/public/liquidation-orders"
           f"?instType=SWAP&instId={symbol}&state=filled&limit={limit}")
    data = fetch(url)
    if not data or data.get("code") != "0":
        return []
    results = []
    for item in data.get("data", []):
        for d in item.get("details", []):
            results.append({
                "side":       d.get("side"),      # buy = liquidated short, sell = liquidated long
                "pos_side":   d.get("posSide"),   # long/short = which side got liquidated
                "bk_price":   float(d.get("bkPx", 0)),
                "size":       float(d.get("sz", 0)),
                "time":       int(d.get("time", 0)),
            })
    return sorted(results, key=lambda x: x["time"], reverse=True)[:limit]

def analyze_liquidations(liqs):
    """
    Tells us: are longs or shorts being flushed?
    If shorts liquidating (side=buy) → price going UP, shorts squeezed
    If longs liquidating (side=sell) → price going DOWN, longs flushed
    """
    if not liqs:
        return {"verdict": "NO DATA", "long_liqs": 0, "short_liqs": 0}
    long_liqs  = sum(1 for l in liqs if l["pos_side"] == "long")
    short_liqs = sum(1 for l in liqs if l["pos_side"] == "short")
    long_usd   = sum(l["size"] for l in liqs if l["pos_side"] == "long")
    short_usd  = sum(l["size"] for l in liqs if l["pos_side"] == "short")

    if short_liqs > long_liqs * 1.5:
        verdict = "🟢 SHORTS BEING SQUEEZED — bullish"
    elif long_liqs > short_liqs * 1.5:
        verdict = "🔴 LONGS BEING FLUSHED — bearish"
    else:
        verdict = "⚪ BALANCED — no clear direction"

    # Price levels being liquidated
    liq_prices = sorted(set(round(l["bk_price"]) for l in liqs))
    return {
        "verdict":    verdict,
        "long_liqs":  long_liqs,
        "short_liqs": short_liqs,
        "long_usd":   round(long_usd),
        "short_usd":  round(short_usd),
        "liq_prices": liq_prices[-5:],  # recent liq price levels
    }

# ── Multi-Exchange Top Trader L/S ─────────────────────────────────────────────

def top_trader_ls(symbols=["BTCUSDT","ETHUSDT","SOLUSDT"]):
    """
    Binance top trader position ratio — smart money positioning.
    These are the large accounts (top 20% by position size).
    """
    BASE = "https://fapi.binance.com/futures/data"
    results = {}
    for sym in symbols:
        url = f"{BASE}/topLongShortPositionRatio?symbol={sym}&period=1h&limit=1"
        data = fetch(url)
        if data and len(data) > 0:
            d = data[0]
            long_pct  = round(float(d["longAccount"]) * 100, 1)
            short_pct = round(float(d["shortAccount"]) * 100, 1)
            ratio     = float(d["longShortRatio"])
            if long_pct >= 55:   signal = "🟢 TOP TRADERS LONG (bullish)"
            elif long_pct <= 45: signal = "🔴 TOP TRADERS SHORT (bearish)"
            else:                signal = "⚪ NEUTRAL"
            results[sym] = {"long": long_pct, "short": short_pct,
                            "ratio": round(ratio, 3), "signal": signal}
        time.sleep(0.1)
    return results

def okx_ls_ratio(ccy="BTC"):
    """OKX long/short account ratio — third exchange cross-check."""
    url = (f"https://www.okx.com/api/v5/rubik/stat/contracts"
           f"/long-short-account-ratio?ccy={ccy}&period=1H&limit=3")
    data = fetch(url)
    if not data or data.get("code") != "0":
        return []
    return [{"ratio": round(float(d[1]), 3),
             "ts": int(d[0])}
            for d in data.get("data", [])]

# ── HyperLiquid DEX Intelligence ──────────────────────────────────────────────

def hyperliquid_markets(top_n=10):
    """
    HyperLiquid is the largest crypto DEX for perps.
    Funding rates + OI here signal smart DeFi money positioning.
    """
    data = fetch("https://api.hyperliquid.xyz/info",
                 method="POST",
                 body={"type": "metaAndAssetCtxs"})
    if not data or len(data) < 2:
        return []

    assets   = data[0].get("universe", [])
    contexts = data[1]

    # Get live mid prices for comparison
    mids_data = fetch("https://api.hyperliquid.xyz/info",
                      method="POST", body={"type": "allMids"})
    mids = mids_data if isinstance(mids_data, dict) else {}

    results = []
    for i, asset in enumerate(assets[:50]):
        if i >= len(contexts):
            break
        ctx = contexts[i]
        name = asset.get("name", "?")
        try:
            funding = float(ctx.get("funding", 0)) * 100
            oi_usd  = float(ctx.get("openInterest", 0)) * float(mids.get(name, 0) or 0)
            mark    = float(ctx.get("markPx", 0))
            results.append({
                "symbol":  name,
                "funding": round(funding, 5),
                "oi_usd":  round(oi_usd),
                "mark":    mark,
            })
        except:
            pass

    # Sort by OI descending
    results.sort(key=lambda x: x["oi_usd"], reverse=True)
    return results[:top_n]

def hl_funding_signal(markets):
    """
    Extreme funding on HyperLiquid = over-leveraged in that direction.
    Negative = shorts overpaying = long squeeze likely.
    Positive > 0.01% = longs overpaying = long flush likely.
    """
    signals = []
    for m in markets:
        f = m["funding"]
        if f > 0.05:
            signals.append(f"⚠️ {m['symbol']}: +{f:.3f}% funding (longs overpaying → flush risk)")
        elif f < -0.05:
            signals.append(f"🔥 {m['symbol']}: {f:.3f}% funding (shorts overpaying → squeeze)")
    return signals

# ── Blockchair Whale Transactions ─────────────────────────────────────────────

def whale_transactions(chain="ethereum", min_usd=5_000_000, limit=5):
    """Large on-chain transactions — where is big money moving?"""
    # Rate limit gently — Blockchair allows 1440 free req/day
    url = (f"https://api.blockchair.com/{chain}/transactions"
           f"?q=value_usd({min_usd}..)&limit={limit}&s=time(desc)")
    data = fetch(url)
    if not data or "data" not in data:
        return []
    txns = []
    for tx in data["data"]:
        txns.append({
            "time":      tx.get("time", ""),
            "value_usd": tx.get("value_usd", 0),
            "sender":    (tx.get("sender", "") or "")[:12] + "...",
            "recipient": (tx.get("recipient", "") or "")[:12] + "...",
            "hash":      tx.get("hash", "")[:12] + "...",
        })
    return txns

# ── Mempool State (BTC urgency) ────────────────────────────────────────────────

def btc_mempool():
    """BTC mempool state — if fees spike, whales are moving urgently."""
    mempool = fetch("https://mempool.space/api/mempool")
    fees    = fetch("https://mempool.space/api/v1/fees/recommended")
    height  = fetch("https://mempool.space/api/blocks/tip/height")
    if not mempool:
        return {}
    fastest = fees.get("fastestFee", 0) if fees else 0
    urgency = "HIGH 🔴" if fastest > 20 else "MEDIUM 🟡" if fastest > 5 else "LOW 🟢"
    return {
        "pending_txns": mempool.get("count", 0),
        "fastest_fee":  fastest,
        "urgency":      urgency,
        "block_height": height,
        "interpretation": ("Whales moving urgently" if fastest > 20
                           else "Calm — no rush to move BTC" if fastest <= 5
                           else "Moderate activity"),
    }

# ── Bybit Insurance Fund ───────────────────────────────────────────────────────

def bybit_insurance():
    """Insurance fund health — if it drops fast, systemic risk rising."""
    data = fetch("https://api.bybit.com/v5/market/insurance?coin=BTC")
    if not data or data.get("retCode") != 0:
        return {}
    for item in data.get("result", {}).get("list", []):
        if item.get("symbols") == "BTCUSD":
            bal = float(item.get("balance", 0))
            val = float(item.get("value", 0))
            health = "HEALTHY 🟢" if val > 200_000_000 else "MODERATE 🟡" if val > 100_000_000 else "LOW 🔴"
            return {"btc_balance": round(bal, 2), "usd_value": round(val),
                    "health": health}
    return {}

# ── DefiLlama TVL Trend ────────────────────────────────────────────────────────

def defi_tvl_trend(days=7):
    """Total DeFi TVL trend — rising TVL = capital flowing in = bullish."""
    data = fetch("https://api.llama.fi/charts")
    if not data:
        return {}
    recent = data[-days:]
    if len(recent) < 2:
        return {}
    start = recent[0]["totalLiquidityUSD"]
    end   = recent[-1]["totalLiquidityUSD"]
    chg   = round((end - start) / start * 100, 2)
    trend = "📈 RISING" if chg > 1 else "📉 FALLING" if chg < -1 else "➡️ FLAT"
    return {"current_tvl_b": round(end / 1e9, 1),
            "change_7d_pct": chg, "trend": trend}

# ── FULL SCAN ──────────────────────────────────────────────────────────────────

def run_whale_scan():
    print("[ ] Scanning OKX liquidations (BTC)...", file=sys.stderr)
    btc_liqs = okx_liquidations("BTC-USDT", limit=20)
    btc_liq_analysis = analyze_liquidations(btc_liqs)
    time.sleep(0.3)

    print("[ ] Scanning ETH liquidations...", file=sys.stderr)
    eth_liqs = okx_liquidations("ETH-USDT", limit=20)
    eth_liq_analysis = analyze_liquidations(eth_liqs)
    time.sleep(0.3)

    print("[ ] Fetching top trader L/S ratios...", file=sys.stderr)
    top_ls = top_trader_ls(["BTCUSDT", "ETHUSDT", "SOLUSDT"])

    print("[ ] Fetching OKX L/S ratio...", file=sys.stderr)
    okx_ls = okx_ls_ratio("BTC")
    time.sleep(0.2)

    print("[ ] Scanning HyperLiquid DEX...", file=sys.stderr)
    hl_markets = hyperliquid_markets(10)
    hl_signals = hl_funding_signal(hl_markets)
    time.sleep(0.3)

    print("[ ] Fetching ETH whale transactions...", file=sys.stderr)
    eth_whales = whale_transactions("ethereum", min_usd=5_000_000, limit=5)

    print("[ ] Checking BTC mempool...", file=sys.stderr)
    mempool = btc_mempool()

    print("[ ] Checking Bybit insurance fund...", file=sys.stderr)
    insurance = bybit_insurance()

    print("[ ] Fetching DeFi TVL trend...", file=sys.stderr)
    tvl = defi_tvl_trend(7)

    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "liquidations": {
            "BTC": btc_liq_analysis,
            "ETH": eth_liq_analysis,
        },
        "top_trader_ls": top_ls,
        "okx_btc_ls": okx_ls,
        "hyperliquid": {
            "top_markets": hl_markets,
            "signals": hl_signals,
        },
        "eth_whale_txns": eth_whales,
        "btc_mempool": mempool,
        "insurance_fund": insurance,
        "defi_tvl": tvl,
    }

def print_whale_report(r):
    print(f"\n{'='*60}")
    print(f"  WHALE INTELLIGENCE — {r['timestamp']}")
    print(f"{'='*60}\n")

    # Liquidations — most important
    print("💥 LIQUIDATIONS (OKX — real flushes right now)")
    for sym, la in r["liquidations"].items():
        print(f"   {sym}: {la['verdict']}")
        print(f"      Longs liquidated: {la['long_liqs']}  |  Shorts liquidated: {la['short_liqs']}")
        if la.get("liq_prices"):
            print(f"      Liq price levels: {la['liq_prices']}")
    print()

    # Smart money L/S
    print("🧠 TOP TRADER POSITIONING (Binance — large accounts only)")
    for sym, ls in r["top_trader_ls"].items():
        print(f"   {sym:12} Long {ls['long']:.1f}% / Short {ls['short']:.1f}%  →  {ls['signal']}")

    if r["okx_btc_ls"]:
        latest_r = r["okx_btc_ls"][0]["ratio"]
        print(f"\n   OKX BTC L/S ratio: {latest_r}  " +
              ("🟢 Longs dominant" if latest_r > 1.2 else
               "🔴 Shorts dominant" if latest_r < 0.8 else "⚪ Balanced"))
    print()

    # HyperLiquid
    print("⚡ HYPERLIQUID DEX (smart DeFi money):")
    for m in r["hyperliquid"]["top_markets"][:5]:
        f = m["funding"]
        fi = "⚠️" if abs(f) > 0.05 else ""
        print(f"   {m['symbol']:8} funding: {f:+.4f}%  OI: ${m['oi_usd']/1e6:.0f}M  {fi}")
    if r["hyperliquid"]["signals"]:
        print()
        for s in r["hyperliquid"]["signals"]:
            print(f"   {s}")
    print()

    # ETH whale movements
    if r["eth_whale_txns"]:
        print("🐋 ETH ON-CHAIN WHALE MOVES (>$5M, last hour):")
        for tx in r["eth_whale_txns"][:4]:
            print(f"   ${tx['value_usd']/1e6:.1f}M  {tx['time']}  {tx['sender']} → {tx['recipient']}")
        print()

    # Mempool
    m = r["btc_mempool"]
    if m:
        print(f"⛓️  BTC MEMPOOL: {m['pending_txns']:,} txns pending  "
              f"| Fee: {m['fastest_fee']} sat/vbyte  "
              f"| Urgency: {m['urgency']}")
        print(f"   → {m['interpretation']}")
        print()

    # Insurance
    ins = r["insurance_fund"]
    if ins:
        print(f"🛡️  BYBIT INSURANCE FUND: {ins['btc_balance']:.0f} BTC "
              f"(${ins['usd_value']/1e6:.0f}M)  {ins['health']}")
        print()

    # DeFi TVL
    tvl = r["defi_tvl"]
    if tvl:
        print(f"📊 DEFI TVL: ${tvl['current_tvl_b']:.0f}B  "
              f"7d change: {tvl['change_7d_pct']:+.1f}%  {tvl['trend']}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Whale Intelligence Scanner")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    result = run_whale_scan()
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_whale_report(result)
