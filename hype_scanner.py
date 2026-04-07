#!/usr/bin/env python3
"""
Hype & Trend Scanner — finds momentum pairs before they move.
Combines: CoinGecko trending/gainers + Binance volume spikes +
Long/Short ratios (Bybit) + OI history trend (Binance) +
CoinGecko community sentiment.
"""

import json, time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

try:
    import pandas as pd
    import numpy as np
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

HEADERS = {"User-Agent": "Mozilla/5.0"}
BINANCE_FAPI  = "https://fapi.binance.com"
BINANCE_DATA  = "https://fapi.binance.com/futures/data"
BYBIT_API     = "https://api.bybit.com"
COINGECKO_API = "https://api.coingecko.com/api/v3"

def fetch(url, timeout=10):
    try:
        r = urlopen(Request(url, headers=HEADERS), timeout=timeout)
        return json.loads(r.read())
    except:
        return None

# ── CoinGecko ──────────────────────────────────────────────────────────────

def cg_trending():
    """Coins trending on CoinGecko right now — early hype signal."""
    data = fetch(f"{COINGECKO_API}/search/trending")
    if not data:
        return []
    results = []
    for c in data.get("coins", []):
        item = c["item"]
        results.append({
            "symbol": item["symbol"].upper(),
            "name":   item["name"],
            "rank":   item.get("market_cap_rank"),
            "score":  item.get("score", 0),
            "source": "cg_trending"
        })
    return results

def cg_top_gainers(limit=20):
    """Top 24h gainers by price change — chasing momentum."""
    url = (f"{COINGECKO_API}/coins/markets?vs_currency=usd"
           f"&order=volume_desc&per_page={limit}&page=1"
           f"&sparkline=false&price_change_percentage=24h")
    data = fetch(url)
    if not data:
        return []
    results = []
    for c in data:
        chg = c.get("price_change_percentage_24h") or 0
        vol = c.get("total_volume") or 0
        if vol < 50_000_000:  # skip low volume coins
            continue
        results.append({
            "symbol":     c["symbol"].upper(),
            "name":       c["name"],
            "price":      c["current_price"],
            "change_24h": round(chg, 2),
            "volume":     vol,
            "market_cap_rank": c.get("market_cap_rank"),
            "source": "cg_gainers"
        })
    # Sort by 24h change descending
    return sorted(results, key=lambda x: x["change_24h"], reverse=True)

def cg_sentiment(coin_id="bitcoin"):
    """Community sentiment votes from CoinGecko."""
    url = (f"{COINGECKO_API}/coins/{coin_id}"
           f"?localization=false&tickers=false"
           f"&market_data=false&community_data=true&developer_data=false")
    data = fetch(url)
    if not data:
        return {}
    return {
        "up_pct":   data.get("sentiment_votes_up_percentage", 0),
        "down_pct": data.get("sentiment_votes_down_percentage", 0),
        "watchlist": data.get("watchlist_portfolio_users", 0)
    }

# ── Binance Volume Spike Detection ────────────────────────────────────────

def binance_volume_spikes(min_ratio=2.0):
    """
    Finds pairs where today's volume is unusually high vs recent average.
    Volume ratio > 2x = potential hype/breakout in progress.
    """
    # Get 24h stats
    stats_24h = fetch(f"{BINANCE_FAPI}/fapi/v1/ticker/24hr")
    if not stats_24h:
        return []

    spikes = []
    skip   = {"USDCUSDT","BUSDUSDT","USDTBUSD","TUSDUSDT","FDUSDUSDT"}

    for t in stats_24h:
        sym = t["symbol"]
        if not sym.endswith("USDT") or sym in skip:
            continue
        vol_24h = float(t.get("quoteVolume", 0))
        chg_pct = float(t.get("priceChangePercent", 0))
        price   = float(t.get("lastPrice", 0))
        if vol_24h < 20_000_000:
            continue

        # Get 7-day average volume via daily klines
        klines = fetch(f"{BINANCE_FAPI}/fapi/v1/klines?symbol={sym}&interval=1d&limit=8")
        if not klines or len(klines) < 2:
            continue

        # Exclude the current (incomplete) candle, use previous 7
        past_vols = [float(k[7]) for k in klines[:-1]]  # quote volume
        avg_vol   = sum(past_vols) / len(past_vols) if past_vols else 0
        ratio     = vol_24h / avg_vol if avg_vol > 0 else 0

        if ratio >= min_ratio:
            spikes.append({
                "symbol":     sym,
                "price":      price,
                "change_24h": chg_pct,
                "volume_24h": vol_24h,
                "avg_vol_7d": round(avg_vol, 0),
                "vol_ratio":  round(ratio, 2),
                "source":     "volume_spike"
            })
        time.sleep(0.05)

    return sorted(spikes, key=lambda x: x["vol_ratio"], reverse=True)[:10]

# ── Bybit Long/Short Ratios ────────────────────────────────────────────────

def bybit_ls_ratio(symbol="BTCUSDT", period="1h", limit=5):
    """Long/short ratio from Bybit (not geo-blocked)."""
    url = (f"{BYBIT_API}/v5/market/account-ratio"
           f"?category=linear&symbol={symbol}&period={period}&limit={limit}")
    data = fetch(url)
    if not data or data.get("retCode") != 0:
        return []
    return [
        {"symbol": d["symbol"],
         "long_pct": round(float(d["buyRatio"]) * 100, 1),
         "short_pct": round(float(d["sellRatio"]) * 100, 1),
         "ratio": round(float(d["buyRatio"]) / float(d["sellRatio"]), 3)
                  if float(d["sellRatio"]) > 0 else 0,
         "ts": d["timestamp"]}
        for d in data["result"]["list"]
    ]

def ls_signal(ls_data):
    """
    Interpret L/S ratio for trading bias.
    > 60% long  → crowded longs → flush risk → prefer short or wait
    > 55% long  → bullish bias
    < 45% long  → bearish bias / short squeeze risk
    < 40% long  → crowded shorts → squeeze risk → prefer long
    """
    if not ls_data:
        return "UNKNOWN", 0
    latest = ls_data[0]
    long_pct = latest["long_pct"]
    if long_pct >= 60:
        return "CROWDED_LONG ⚠️ flush risk", long_pct
    elif long_pct >= 55:
        return "BULLISH LEAN", long_pct
    elif long_pct >= 45:
        return "NEUTRAL", long_pct
    elif long_pct >= 40:
        return "BEARISH LEAN", long_pct
    else:
        return "CROWDED_SHORT 🔥 squeeze setup", long_pct

# ── Binance OI History (Trend) ─────────────────────────────────────────────

def oi_trend(symbol="BTCUSDT", periods=10):
    """
    OI history from Binance futures/data endpoint.
    Rising OI + rising price = trend continuation (strong)
    Rising OI + falling price = trend exhaustion (weak)
    Falling OI = position unwinding
    """
    url = f"{BINANCE_DATA}/openInterestHist?symbol={symbol}&period=1h&limit={periods}"
    data = fetch(url)
    if not data:
        return {}
    oi_vals = [float(d["sumOpenInterest"]) for d in data]
    oi_usd  = [float(d["sumOpenInterestValue"]) for d in data]
    change  = (oi_vals[-1] - oi_vals[0]) / oi_vals[0] * 100 if oi_vals[0] > 0 else 0
    trend   = "RISING" if change > 1 else "FALLING" if change < -1 else "FLAT"
    return {
        "symbol":     symbol,
        "oi_current": round(oi_vals[-1], 0),
        "oi_usd":     round(oi_usd[-1], 0),
        "change_pct": round(change, 2),
        "trend":      trend,
        "signal":     "BULLISH" if trend == "RISING" else "BEARISH" if trend == "FALLING" else "NEUTRAL"
    }

# ── Global L/S via Binance ─────────────────────────────────────────────────

def binance_global_ls(symbol="BTCUSDT", limit=3):
    """Global L/S account ratio from Binance (alternative endpoint)."""
    url = (f"{BINANCE_DATA}/globalLongShortAccountRatio"
           f"?symbol={symbol}&period=1h&limit={limit}")
    data = fetch(url)
    if not data:
        return []
    return [{"long": round(float(d["longAccount"])*100,1),
             "short": round(float(d["shortAccount"])*100,1),
             "ratio": round(float(d["longShortRatio"]),3)}
            for d in data]

# ── Full Hype Scan ─────────────────────────────────────────────────────────

def run_hype_scan(account_usd=1000):
    print("[ ] Scanning CoinGecko trending...", flush=True)
    trending   = cg_trending()

    print("[ ] Scanning CoinGecko top gainers (>$50M vol)...", flush=True)
    gainers    = cg_top_gainers(30)

    print("[ ] Detecting Binance volume spikes (>2x avg)...", flush=True)
    vol_spikes = binance_volume_spikes(min_ratio=2.0)

    print("[ ] Fetching BTC/ETH/SOL long-short ratios...", flush=True)
    btc_ls  = bybit_ls_ratio("BTCUSDT")
    eth_ls  = bybit_ls_ratio("ETHUSDT")
    sol_ls  = bybit_ls_ratio("SOLUSDT")
    bnb_ls  = bybit_ls_ratio("BNBUSDT")

    print("[ ] Fetching OI history trends...", flush=True)
    btc_oi  = oi_trend("BTCUSDT")
    eth_oi  = oi_trend("ETHUSDT")

    print("[ ] Fetching community sentiment...", flush=True)
    btc_sent = cg_sentiment("bitcoin")
    eth_sent = cg_sentiment("ethereum")

    # Cross-reference: coins appearing in BOTH trending AND gainers = HIGH CONVICTION hype
    trending_syms = {t["symbol"] for t in trending}
    gainers_syms  = {g["symbol"] for g in gainers[:10]}  # top 10 gainers
    vol_spike_syms= {v["symbol"].replace("USDT","") for v in vol_spikes}

    # Coins in multiple signals = hype confirmed
    multi_signal = []
    all_candidates = trending_syms | gainers_syms | vol_spike_syms
    for sym in all_candidates:
        signals = []
        if sym in trending_syms:  signals.append("CG_TRENDING")
        if sym in gainers_syms:   signals.append("TOP_GAINER")
        if sym in vol_spike_syms: signals.append("VOL_SPIKE")
        if len(signals) >= 2:
            multi_signal.append({"symbol": sym, "signals": signals, "count": len(signals)})
    multi_signal.sort(key=lambda x: x["count"], reverse=True)

    return {
        "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "cg_trending":   trending,
        "top_gainers":   gainers[:10],
        "volume_spikes": vol_spikes[:8],
        "multi_signal":  multi_signal,  # highest conviction hype
        "long_short": {
            "BTC": {"bybit": btc_ls[:1], "signal": ls_signal(btc_ls),
                    "binance": binance_global_ls("BTCUSDT")[:1],
                    "oi": btc_oi},
            "ETH": {"bybit": eth_ls[:1], "signal": ls_signal(eth_ls),
                    "binance": binance_global_ls("ETHUSDT")[:1],
                    "oi": eth_oi},
            "SOL": {"bybit": sol_ls[:1], "signal": ls_signal(sol_ls)},
            "BNB": {"bybit": bnb_ls[:1], "signal": ls_signal(bnb_ls)},
        },
        "community_sentiment": {
            "BTC": btc_sent,
            "ETH": eth_sent,
        }
    }

def print_hype_report(r):
    print(f"\n{'='*60}")
    print(f"  HYPE & TREND SCANNER — {r['timestamp']}")
    print(f"{'='*60}\n")

    # Multi-signal (highest conviction)
    if r["multi_signal"]:
        print("🔥 HIGH CONVICTION HYPE (multiple signals):")
        for m in r["multi_signal"]:
            print(f"   {m['symbol']:12} → {' + '.join(m['signals'])}")
        print()

    # CoinGecko trending
    print("📈 COINGECKO TRENDING NOW:")
    for t in r["cg_trending"][:7]:
        print(f"   #{t['score']+1} {t['symbol']:10} {t['name'][:25]}")
    print()

    # Top gainers
    print("🚀 TOP GAINERS (>$50M vol):")
    for g in r["top_gainers"][:8]:
        print(f"   {g['symbol']:12} {g['change_24h']:+.1f}%  ${g['volume']/1e6:.0f}M vol  rank#{g['market_cap_rank']}")
    print()

    # Volume spikes
    if r["volume_spikes"]:
        print("📊 VOLUME SPIKES (vs 7d avg):")
        for v in r["volume_spikes"][:5]:
            print(f"   {v['symbol']:14} {v['vol_ratio']:.1f}x  {v['change_24h']:+.1f}%  ${v['volume_24h']/1e6:.0f}M")
        print()

    # Long/Short ratios
    print("⚖️  LONG / SHORT RATIOS:")
    for asset, d in r["long_short"].items():
        if d["bybit"]:
            ls = d["bybit"][0]
            sig, pct = d["signal"]
            oi_str = ""
            if "oi" in d and d["oi"]:
                oi = d["oi"]
                oi_str = f"  OI {oi['trend']} ({oi['change_pct']:+.1f}%)"
            print(f"   {asset:6} Long {ls['long_pct']:.1f}% / Short {ls['short_pct']:.1f}%  → {sig}{oi_str}")
    print()

    # Community sentiment
    print("💬 COMMUNITY SENTIMENT:")
    for coin, s in r["community_sentiment"].items():
        if s:
            bar_up = "█" * int(s["up_pct"] / 5)
            print(f"   {coin:6} {s['up_pct']:.0f}% bullish  {bar_up}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Hype & Trend Scanner")
    p.add_argument("--json",    action="store_true")
    p.add_argument("--account", type=float, default=1000)
    args = p.parse_args()

    import sys
    result = run_hype_scan(args.account)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_hype_report(result)
