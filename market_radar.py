#!/usr/bin/env python3
"""
Market Radar — Dynamic opportunity discovery engine.
Finds WHERE the action is right now, not a fixed list.

Sources:
  1. CoinGecko 1h movers — what's moving in the last hour
  2. CoinGecko trending — community momentum / hype
  3. HyperLiquid funding extremes — squeeze setups (DEX smart money)
  4. Blockchair ETH whale transactions — large money moving on-chain
  5. Polymarket — prediction market probabilities for macro signals

Output: ranked list of opportunities with reason WHY they're interesting.
"""

import json, time
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

HDR = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# CG symbol → Binance futures symbol
CG_TO_BINANCE = {}  # built dynamically

YAHOO_SYMBOLS = {
    "BTC":"BTCUSDT","ETH":"ETHUSDT","SOL":"SOLUSDT","BNB":"BNBUSDT",
    "XRP":"XRPUSDT","ADA":"ADAUSDT","AVAX":"AVAXUSDT","LINK":"LINKUSDT",
    "DOT":"DOTUSDT","DOGE":"DOGEUSDT","LTC":"LTCUSDT","BCH":"BCHUSDT",
    "ATOM":"ATOMUSDT","UNI":"UNIUSDT","AAVE":"AAVEUSDT","SUI":"SUIUSDT",
    "TAO":"TAOUSDT","PEPE":"PEPEUSDT","HYPE":"HYPEUSDT","INJ":"INJUSDT",
    "APT":"APTUSDT","ARB":"ARBUSDT","OP":"OPUSDT","TRX":"TRXUSDT",
    "NEAR":"NEARUSDT","FIL":"FILUSDT","IMX":"IMXUSDT","GRT":"GRTUSDT",
    "FET":"FETUSDT","RENDER":"RENDERUSDT","ZEC":"ZECUSDT","ALGO":"ALGOUSDT",
}

def fetch(url, method="GET", body=None, timeout=8):
    try:
        data = json.dumps(body).encode() if body else None
        h = {**HDR}
        if body:
            h["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=h, method=method)
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except:
        return None

# ── 1. CoinGecko 1h Movers ────────────────────────────────────────────────────

def cg_1h_movers(min_vol_m=50, top_n=8):
    """
    Coins moving most in the LAST HOUR with decent volume.
    These are the freshest opportunities — something just happened.
    """
    url = ("https://api.coingecko.com/api/v3/coins/markets"
           "?vs_currency=usd&order=market_cap_desc&per_page=100&page=1"
           "&sparkline=false&price_change_percentage=1h,24h")
    data = fetch(url)
    if not data:
        return []

    movers = []
    for coin in data:
        sym_base = coin.get("symbol","").upper()
        binance_sym = YAHOO_SYMBOLS.get(sym_base)
        if not binance_sym:
            continue
        vol   = coin.get("total_volume", 0) or 0
        chg1h = coin.get("price_change_percentage_1h_in_currency", 0) or 0
        chg24 = coin.get("price_change_percentage_24h", 0) or 0
        if vol < min_vol_m * 1_000_000:
            continue
        if abs(chg1h) < 0.5:  # skip flat coins
            continue
        movers.append({
            "symbol":    binance_sym,
            "name":      coin.get("name",""),
            "price":     coin.get("current_price", 0),
            "chg_1h":    round(chg1h, 2),
            "chg_24h":   round(chg24, 2),
            "volume_m":  round(vol / 1e6, 0),
            "source":    "1h_mover",
            "reason":    f"{chg1h:+.1f}% in 1h | ${vol/1e6:.0f}M vol",
        })

    # Sort by absolute 1h change
    return sorted(movers, key=lambda x: abs(x["chg_1h"]), reverse=True)[:top_n]

# ── 2. CoinGecko Trending ─────────────────────────────────────────────────────

def cg_trending():
    """Real-time trending coins — community momentum, early hype signal."""
    data = fetch("https://api.coingecko.com/api/v3/search/trending")
    if not data:
        return []
    result = []
    for c in data.get("coins", [])[:7]:
        item = c["item"]
        sym_base = item.get("symbol","").upper()
        binance_sym = YAHOO_SYMBOLS.get(sym_base)
        if not binance_sym:
            continue
        result.append({
            "symbol":  binance_sym,
            "name":    item.get("name",""),
            "price":   item.get("data",{}).get("price", 0),
            "chg_24h": 0,
            "source":  "cg_trending",
            "reason":  f"Trending #{c['item']['score']+1} on CoinGecko",
        })
    return result

# ── 3. HyperLiquid Funding Extremes ──────────────────────────────────────────

def hl_funding_extremes(threshold=0.01):
    """
    Coins with extreme funding rates on HyperLiquid DEX.
    Very negative = shorts paying too much = SHORT SQUEEZE setup
    Very positive = longs paying too much = LONG FLUSH risk
    """
    data = fetch("https://api.hyperliquid.xyz/info",
                 method="POST", body={"type": "metaAndAssetCtxs"})
    mids = fetch("https://api.hyperliquid.xyz/info",
                 method="POST", body={"type": "allMids"}) or {}
    if not data or len(data) < 2:
        return []

    result = []
    for i, asset in enumerate(data[0].get("universe", [])):
        if i >= len(data[1]):
            break
        name     = asset.get("name","").upper()
        binance_sym = YAHOO_SYMBOLS.get(name)
        if not binance_sym:
            continue
        try:
            ctx      = data[1][i]
            funding  = float(ctx.get("funding", 0)) * 100  # as %
            oi       = float(ctx.get("openInterest", 0))
            price    = float(mids.get(name, 0))
            oi_usd   = oi * price

            if oi_usd < 10_000_000:  # skip tiny markets
                continue

            if funding < -threshold:
                result.append({
                    "symbol":  binance_sym,
                    "name":    name,
                    "price":   price,
                    "chg_24h": 0,
                    "funding": round(funding, 5),
                    "oi_usd_m": round(oi_usd/1e6, 1),
                    "source":  "hl_squeeze",
                    "reason":  f"🔥 SHORT SQUEEZE: funding {funding:.4f}% | OI ${oi_usd/1e6:.0f}M",
                    "bias":    "LONG",
                })
            elif funding > threshold:
                result.append({
                    "symbol":  binance_sym,
                    "name":    name,
                    "price":   price,
                    "chg_24h": 0,
                    "funding": round(funding, 5),
                    "oi_usd_m": round(oi_usd/1e6, 1),
                    "source":  "hl_flush",
                    "reason":  f"⚠️ LONG FLUSH RISK: funding +{funding:.4f}% | OI ${oi_usd/1e6:.0f}M",
                    "bias":    "SHORT",
                })
        except:
            continue

    return sorted(result, key=lambda x: abs(x.get("funding",0)), reverse=True)[:5]

# ── 4. Whale Transactions ─────────────────────────────────────────────────────

def eth_whale_moves(min_usd=5_000_000):
    """Large ETH transactions in last hour — where big money is moving."""
    url = (f"https://api.blockchair.com/ethereum/transactions"
           f"?q=value_usd({min_usd}..)&limit=5&s=time(desc)")
    data = fetch(url, timeout=10)
    if not data or "data" not in data:
        return []
    moves = []
    for tx in data["data"]:
        usd = tx.get("value_usd", 0)
        sender    = (tx.get("sender","") or "")[:16]
        recipient = (tx.get("recipient","") or "")[:16]
        t = tx.get("time","")[:16]
        moves.append({
            "usd_m":     round(usd/1e6, 1),
            "sender":    sender,
            "recipient": recipient,
            "time":      t,
        })
    return moves

# ── 5. Polymarket Signals ─────────────────────────────────────────────────────

POLYMARKET_KEYWORDS = [
    "bitcoin","btc","ethereum","eth","crypto","fed","rate cut","cpi",
    "inflation","iran","ceasefire","trump","tariff","recession","rate hike"
]

def polymarket_signals():
    """
    Fetch Polymarket macro signals relevant to crypto trading.
    Strategy: fetch 500 markets, filter aggressively for ACTIONABLE signals,
    skip sports/entertainment noise, focus on geopolitical + macro + crypto.
    Note: Polymarket is best used for DIRECTION and PROBABILITY CHANGES,
    not for high-volume signals (sports dominate volume currently).
    """
    skip_words = [
        "nba","nhl","nfl","fifa","world cup","gta vi","jesus","rihanna","carti",
        "mlb","nascar","ufc","golf","masters","stanley cup","playoffs","basketball",
        "hockey","football","tennis","baseball","wimbledon","super bowl","grammy",
        "oscar","emmy","harvey weinstein","kardashian","taylor swift","beyonce",
        "olympic","wimbledon","formula 1","f1","mma","boxing","wrestling"
    ]
    macro_keywords = [
        "bitcoin","btc","ethereum","eth","crypto","solana","xrp","fed","rate cut",
        "rate hike","cpi","inflation","iran","ceasefire","trump","tariff","recession",
        "war","oil","gold","dollar","yen","interest rate","gdp","jobs","nonfarm",
        "powell","stock","market","sanction","geopolit","debt","default","banking",
        "nuclear","missile","attack","escalat","peace","deal","agreement","china",
        "russia","ukraine","taiwan","korea","middle east","opec","sec","etf","halving",
        "trump","executive order","congress","senate"
    ]

    all_markets = {}
    # Fetch from multiple tag slugs + newest markets
    sources = [
        "https://gamma-api.polymarket.com/markets?limit=100&active=true&tag_slug=crypto",
        "https://gamma-api.polymarket.com/markets?limit=100&active=true&tag_slug=geopolitics",
        "https://gamma-api.polymarket.com/markets?limit=100&active=true&tag_slug=economics",
        "https://gamma-api.polymarket.com/markets?limit=100&active=true&tag_slug=politics",
        "https://gamma-api.polymarket.com/markets?limit=100&active=true&closed=false&_c=startDate&_d=desc",
    ]
    for url in sources:
        data = fetch(url, timeout=8)
        if isinstance(data, list):
            for m in data:
                cid = m.get("id")
                if cid:
                    all_markets[cid] = m

    signals = []
    for market in all_markets.values():
        q_lower = market.get("question","").lower()
        if any(s in q_lower for s in skip_words):
            continue
        if not any(k in q_lower for k in macro_keywords):
            continue
        try:
            prices_raw = market.get("outcomePrices","[]")
            prices     = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            yes_prob   = float(prices[0]) * 100 if prices else 50
            volume     = float(market.get("volume","0") or 0)
            if volume < 500:
                continue
            end_date = market.get("endDate","")[:10]
            question_full = market.get("question","")
            impact = _assess_impact(question_full, yes_prob)
            signals.append({
                "question":   question_full[:80],
                "yes_prob":   round(yes_prob, 1),
                "no_prob":    round(100 - yes_prob, 1),
                "volume_usd": round(volume),
                "end_date":   end_date,
                "impact":     impact,
            })
        except:
            continue

    # Sort by volume
    return sorted(signals, key=lambda x: x["volume_usd"], reverse=True)[:8]

def _assess_impact(question, yes_prob):
    """Assess what this market means for crypto prices."""
    q = question.lower()

    # Bullish signals for crypto (if YES)
    if any(x in q for x in ["ceasefire","peace","end war","rate cut","cut rates"]):
        return "🟢 BULLISH if YES" if yes_prob > 50 else "🔴 BEARISH if NO"
    if any(x in q for x in ["bitcoin above","btc above","crypto above"]):
        return f"🟢 {'HIGH' if yes_prob>60 else 'MODERATE'} chance — crypto bullish"
    # Bearish signals for crypto (if YES)
    if any(x in q for x in ["rate hike","cpi above","inflation above","recession"]):
        return "🔴 BEARISH if YES" if yes_prob > 50 else "🟢 BULLISH if NO"
    if any(x in q for x in ["escalat","attack","sanction","tariff"]):
        return "🔴 RISK-OFF if YES" if yes_prob > 50 else "🟢 RISK-ON if NO"
    return f"📊 Market says {yes_prob:.0f}% YES"

# ── FULL RADAR SCAN ───────────────────────────────────────────────────────────

def scan(max_pairs=12):
    """
    Full market radar scan.
    Returns: ranked opportunities + polymarket signals + whale moves.
    """
    print("[ ] Radar: 1h movers...", flush=True)
    movers = cg_1h_movers()
    time.sleep(0.5)

    print("[ ] Radar: trending coins...", flush=True)
    trending = cg_trending()
    time.sleep(0.3)

    print("[ ] Radar: HyperLiquid funding extremes...", flush=True)
    hl_signals = hl_funding_extremes(threshold=0.008)
    time.sleep(0.3)

    print("[ ] Radar: Polymarket signals...", flush=True)
    poly = polymarket_signals()
    time.sleep(0.3)

    print("[ ] Radar: ETH whale moves...", flush=True)
    whales = eth_whale_moves()

    # ── Deduplicate and rank opportunities ──────────────────────────────────
    seen     = {}
    ranked   = []

    def add(item, priority):
        sym = item.get("symbol")
        if not sym:
            return
        if sym not in seen or seen[sym]["priority"] > priority:
            seen[sym] = {**item, "priority": priority}

    # Priority 1: HyperLiquid funding extremes (smart DeFi money)
    for x in hl_signals:
        add(x, 1)

    # Priority 2: 1h movers (fresh momentum)
    for x in movers:
        add(x, 2)

    # Priority 3: CoinGecko trending (community momentum)
    for x in trending:
        add(x, 3)

    # Build final ranked list
    ranked = sorted(seen.values(), key=lambda x: x["priority"])[:max_pairs]

    # Always include BTC and ETH (most liquid, always relevant)
    core = ["BTCUSDT","ETHUSDT"]
    core_items = [{"symbol":s,"source":"core","reason":"Core liquid pair","priority":0,
                   "chg_24h":0,"price":0} for s in core if s not in seen]
    ranked = core_items + ranked
    ranked = ranked[:max_pairs]

    return {
        "timestamp":        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "opportunities":    ranked,
        "polymarket":       poly,
        "whale_moves":      whales,
        "hl_signals":       hl_signals,
        "trending_coins":   [t["symbol"].replace("USDT","") for t in trending[:5]],
        "top_movers":       [(m["symbol"].replace("USDT",""), m["chg_1h"]) for m in movers[:5]],
    }

def format_radar_header(radar: dict) -> str:
    """Format for Telegram header."""
    lines = []

    # Top movers
    if radar["top_movers"]:
        movers_str = "  ".join(f"{s} {c:+.1f}%" for s,c in radar["top_movers"][:4])
        lines.append(f"📈 *1h Movers:* {movers_str}")

    # Trending
    if radar["trending_coins"]:
        lines.append(f"🔥 *Trending:* {' · '.join(radar['trending_coins'][:5])}")

    # HL funding signals
    hl = radar.get("hl_signals",[])
    for sig in hl[:2]:
        lines.append(f"⚡ {sig['reason']}")

    # Polymarket top signals
    poly = radar.get("polymarket",[])
    if poly:
        lines.append("")
        lines.append("🎯 *Polymarket (real money signals):*")
        for p in poly[:4]:
            bar = "█" * int(p["yes_prob"] / 10)
            lines.append(f"  {p['yes_prob']:.0f}% YES: {p['question'][:55]}..")
            lines.append(f"  {bar} → {p['impact']}  (${p['volume_usd']/1e3:.0f}K vol)")

    # Whale moves
    if radar["whale_moves"]:
        lines.append("")
        lines.append("🐋 *ETH Whale moves (last hour):*")
        for w in radar["whale_moves"][:2]:
            lines.append(f"  ${w['usd_m']:.0f}M moved at {w['time']}")

    return "\n".join(lines)

if __name__ == "__main__":
    print("Running market radar...")
    result = scan()
    print(f"\nOpportunities found: {len(result['opportunities'])}")
    for o in result["opportunities"]:
        print(f"  {o['symbol']:14} [{o['source']}] {o.get('reason','')[:50]}")
    print(f"\nPolymarket signals: {len(result['polymarket'])}")
    for p in result["polymarket"]:
        print(f"  {p['yes_prob']:.0f}% YES: {p['question'][:60]}")
    print(f"\nWhale moves: {len(result['whale_moves'])}")
