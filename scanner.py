#!/usr/bin/env python3
"""
Binance Futures Scanner & Multi-Timeframe Analysis Engine
- Scans top pairs by volume + volatility
- Computes RSI, MACD, Bollinger Bands, ATR on 15m / 1h / 4h
- Checks funding rates, open interest
- Fetches macro events (ForexFactory) and crypto news (CoinTelegraph, CoinDesk)
- Generates ranked trading opportunities with entry/target/stop/leverage
"""

import json, sys, time, math
from datetime import datetime, timezone, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError
from xml.etree import ElementTree

try:
    import pandas as pd
    import numpy as np
except ImportError:
    print("ERROR: Run: pip3 install pandas numpy", file=sys.stderr)
    sys.exit(1)

BASE        = "https://fapi.binance.com"   # kept for funding/OI only
BYBIT_BASE  = "https://api.bybit.com"
YAHOO_BASE  = "https://query1.finance.yahoo.com/v8/finance/chart"
COINGECKO   = "https://api.coingecko.com/api/v3"
HEADERS     = {"User-Agent": "Mozilla/5.0"}

# Binance futures symbol → Yahoo Finance symbol
YAHOO_MAP = {
    "BTCUSDT":"BTC-USD",  "ETHUSDT":"ETH-USD",  "SOLUSDT":"SOL-USD",
    "BNBUSDT":"BNB-USD",  "XRPUSDT":"XRP-USD",  "ADAUSDT":"ADA-USD",
    "AVAXUSDT":"AVAX-USD","LINKUSDT":"LINK-USD", "DOTUSDT":"DOT-USD",
    "DOGEUSDT":"DOGE-USD","LTCUSDT":"LTC-USD",  "BCHUSDT":"BCH-USD",
    "ATOMUSDT":"ATOM-USD","UNIUSDT":"UNI-USD",  "AAVEUSDT":"AAVE-USD",
    "SUIUSDT":"SUI-USD",  "TAOUSDT":"TAO-USD",  "PEPEUSDT":"PEPE-USD",
    "HYPEUSDT":"HYPE-USD","INJUSDT":"INJ-USD",  "APTUSDT":"APT-USD",
    "ARBUSDT":"ARB-USD",  "OPUSDT":"OP-USD",    "XAUUSDT":"GC=F",
}

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def fetch(url, timeout=10):
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return None

def fetch_text(url, timeout=10):
    try:
        req = Request(url, headers=HEADERS)
        with urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except:
        return ""

# ─── DATA LAYER — Yahoo Finance + CoinGecko (works from any cloud server) ─────

def get_top_pairs(n=20):
    """Top N pairs using CoinGecko volume — works from any server."""
    data = fetch(f"{COINGECKO}/coins/markets?vs_currency=usd&order=volume_desc&per_page=30&page=1&sparkline=false&price_change_percentage=24h")
    if data:
        rows = []
        skip_syms = {"usdt","usdc","busd","tusd","dai","fdusd","usdp"}
        for coin in data:
            sym_base = coin.get("symbol","").upper()
            sym = sym_base + "USDT"
            if sym_base.lower() in skip_syms:
                continue
            if sym not in YAHOO_MAP:
                continue
            try:
                rows.append({
                    "symbol": sym,
                    "price":  float(coin.get("current_price") or 0),
                    "change": float(coin.get("price_change_percentage_24h") or 0),
                    "volume": float(coin.get("total_volume") or 0),
                    "high":   float(coin.get("high_24h") or coin.get("current_price") or 0),
                    "low":    float(coin.get("low_24h")  or coin.get("current_price") or 0),
                })
            except:
                continue
        if rows:
            return sorted(rows, key=lambda x: x["volume"], reverse=True)[:n]

    # Hardcoded fallback — always works
    return [{"symbol": s, "price": 0, "change": 0, "volume": 1e9, "high": 0, "low": 0}
            for s in ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
                       "ADAUSDT","AVAXUSDT","LINKUSDT","DOGEUSDT","DOTUSDT"]]

def get_klines(symbol, interval="4h", limit=100):
    """Fetch OHLCV via Yahoo Finance — works from any cloud server."""
    yahoo_sym = YAHOO_MAP.get(symbol)
    if not yahoo_sym:
        return None

    # Map interval → Yahoo Finance params
    yf_params = {
        "15m": ("15m", "5d"),
        "1h":  ("1h",  "7d"),
        "4h":  ("1h",  "15d"),  # fetch 1h then resample to 4h
        "1d":  ("1d",  "60d"),
    }
    yf_interval, yf_range = yf_params.get(interval, ("1h", "7d"))
    resample_4h = (interval == "4h")

    url  = f"{YAHOO_BASE}/{yahoo_sym}?interval={yf_interval}&range={yf_range}"
    data = fetch(url)
    if not data:
        return None
    try:
        result = data["chart"]["result"][0]
        ts     = result["timestamp"]
        q      = result["indicators"]["quote"][0]

        df = pd.DataFrame({
            "ts":     pd.to_datetime(ts, unit="s"),
            "open":   q.get("open",   [None]*len(ts)),
            "high":   q.get("high",   [None]*len(ts)),
            "low":    q.get("low",    [None]*len(ts)),
            "close":  q.get("close",  [None]*len(ts)),
            "volume": q.get("volume", [0]*len(ts)),
        }).dropna(subset=["close"]).set_index("ts")

        # Approximate futures-style columns
        df["quote_vol"]       = df["volume"] * df["close"]
        df["taker_buy_base"]  = df["volume"]  * 0.5
        df["taker_buy_quote"] = df["quote_vol"] * 0.5

        if resample_4h:
            df = df.resample("4h").agg({
                "open":  "first", "high": "max", "low": "min",
                "close": "last",  "volume": "sum",
                "quote_vol": "sum", "taker_buy_base": "sum",
                "taker_buy_quote": "sum"
            }).dropna()

        return df.tail(limit)
    except Exception as e:
        return None

def get_funding(symbol):
    """Funding rate from HyperLiquid DEX (works globally)."""
    base = symbol.replace("USDT","").replace("USDC","")
    try:
        import json as _j
        from urllib.request import urlopen, Request
        req  = Request("https://api.hyperliquid.xyz/info",
                       data=_j.dumps({"type":"metaAndAssetCtxs"}).encode(),
                       headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"},
                       method="POST")
        resp = _j.loads(urlopen(req, timeout=8).read())
        if resp and len(resp) >= 2:
            for i, asset in enumerate(resp[0].get("universe",[])):
                if asset.get("name","").upper() == base.upper():
                    if i < len(resp[1]):
                        rate = float(resp[1][i].get("funding", 0))
                        return [rate, rate, rate]
    except:
        pass
    return [0.0]

def get_open_interest(symbol):
    """OI from HyperLiquid (works globally)."""
    base = symbol.replace("USDT","").replace("USDC","")
    try:
        import json as _j
        from urllib.request import urlopen, Request
        req  = Request("https://api.hyperliquid.xyz/info",
                       data=_j.dumps({"type":"allMids"}).encode(),
                       headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"},
                       method="POST")
        mids = _j.loads(urlopen(req, timeout=6).read())
        req2 = Request("https://api.hyperliquid.xyz/info",
                       data=_j.dumps({"type":"metaAndAssetCtxs"}).encode(),
                       headers={"Content-Type":"application/json","User-Agent":"Mozilla/5.0"},
                       method="POST")
        resp = _j.loads(urlopen(req2, timeout=8).read())
        if resp and len(resp) >= 2:
            for i, asset in enumerate(resp[0].get("universe",[])):
                if asset.get("name","").upper() == base.upper():
                    if i < len(resp[1]):
                        oi    = float(resp[1][i].get("openInterest", 0))
                        price = float(mids.get(base, 0))
                        return oi * price
    except:
        pass
    return None

# Keep signature compatible — old Binance OI check
def _get_open_interest_binance(symbol):
    data = fetch(f"{BASE}/fapi/v1/openInterest?symbol={symbol}")
    if data:
        return float(data["openInterest"])
    return None

# ─── TECHNICAL INDICATORS ─────────────────────────────────────────────────────

def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return (100 - 100 / (1 + g / l.replace(0, np.nan))).iloc[-1]

def macd(s, fast=12, slow=26, sig=9):
    ef = s.ewm(span=fast, adjust=False).mean()
    es = s.ewm(span=slow, adjust=False).mean()
    line = ef - es
    signal = line.ewm(span=sig, adjust=False).mean()
    hist = line - signal
    cross = None
    if line.iloc[-2] < signal.iloc[-2] and line.iloc[-1] > signal.iloc[-1]:
        cross = "BULL_CROSS"
    elif line.iloc[-2] > signal.iloc[-2] and line.iloc[-1] < signal.iloc[-1]:
        cross = "BEAR_CROSS"
    return {"line": line.iloc[-1], "signal": signal.iloc[-1],
            "hist": hist.iloc[-1], "bullish": line.iloc[-1] > signal.iloc[-1],
            "cross": cross}

def bollinger(s, p=20, std=2):
    mid = s.rolling(p).mean()
    sd  = s.rolling(p).std()
    up, lo = mid + std * sd, mid - std * sd
    price = s.iloc[-1]
    width = up.iloc[-1] - lo.iloc[-1]
    pct   = (price - lo.iloc[-1]) / width * 100 if width > 0 else 50
    return {"upper": up.iloc[-1], "mid": mid.iloc[-1], "lower": lo.iloc[-1],
            "pct": pct, "squeeze": width / mid.iloc[-1] < 0.03}

def atr(df, p=14):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"]  - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(p).mean().iloc[-1]

def volume_signal(df):
    """Is current candle volume above 20-period average?"""
    avg = df["volume"].rolling(20).mean().iloc[-1]
    cur = df["volume"].iloc[-1]
    return {"ratio": cur / avg if avg > 0 else 1, "above_avg": cur > avg}

def ema_trend(s):
    e9  = s.ewm(span=9,  adjust=False).mean().iloc[-1]
    e21 = s.ewm(span=21, adjust=False).mean().iloc[-1]
    e50 = s.ewm(span=50, adjust=False).mean().iloc[-1] if len(s) >= 50 else None
    return {"e9": e9, "e21": e21, "e50": e50,
            "up": e9 > e21, "strong": e50 and e21 > e50}

# ─── SIGNAL SCORING ───────────────────────────────────────────────────────────

def score_pair(sym, tf_data, funding_rates, oi):
    """
    Multi-timeframe scoring. Returns signal dict.
    tf_data: {"4h": indicators, "1h": indicators, "15m": indicators}
    """
    score = 0
    reasons = []
    price = tf_data["4h"]["price"]
    atr_v = tf_data["4h"]["atr"]

    # --- 4h signals (weight 3) ---
    d4 = tf_data["4h"]
    if d4["ema"]["up"]:       score += 2; reasons.append("4h EMA uptrend")
    else:                     score -= 2; reasons.append("4h EMA downtrend")
    if d4["macd"]["bullish"]: score += 1; reasons.append("4h MACD bullish")
    else:                     score -= 1
    if d4["macd"]["cross"] == "BULL_CROSS":
        score += 3; reasons.append("🔥 4h MACD bullish crossover")
    elif d4["macd"]["cross"] == "BEAR_CROSS":
        score -= 3; reasons.append("💀 4h MACD bearish crossover")
    if d4["rsi"] < 35:    score += 2; reasons.append(f"4h RSI oversold ({d4['rsi']:.0f})")
    elif d4["rsi"] > 68:  score -= 2; reasons.append(f"4h RSI overbought ({d4['rsi']:.0f})")
    if d4["bb"]["pct"] < 20:  score += 2; reasons.append("4h BB lower band (oversold)")
    elif d4["bb"]["pct"] > 80: score -= 2; reasons.append("4h BB upper band (overbought)")

    # --- 1h signals (weight 2) ---
    if "1h" in tf_data:
        d1 = tf_data["1h"]
        if d1["ema"]["up"]:       score += 1; reasons.append("1h EMA uptrend")
        else:                     score -= 1
        if d1["macd"]["bullish"]: score += 1; reasons.append("1h MACD bullish")
        else:                     score -= 1
        if d1["macd"]["cross"] == "BULL_CROSS":
            score += 2; reasons.append("1h MACD bullish crossover")
        elif d1["macd"]["cross"] == "BEAR_CROSS":
            score -= 2; reasons.append("1h MACD bearish crossover")
        if d1["rsi"] < 35: score += 1; reasons.append(f"1h RSI oversold ({d1['rsi']:.0f})")
        elif d1["rsi"] > 68: score -= 1

    # --- 15m signals (weight 1, confirmation only) ---
    if "15m" in tf_data:
        d15 = tf_data["15m"]
        if d15["macd"]["cross"] == "BULL_CROSS":
            score += 1; reasons.append("15m MACD bullish crossover (entry timing)")
        elif d15["macd"]["cross"] == "BEAR_CROSS":
            score -= 1

    # --- Funding rate check ---
    fr = funding_rates[0] if funding_rates else 0
    fr_pct = fr * 100
    if fr > 0.001:    score -= 3; reasons.append(f"⚠️ DANGER: High funding {fr_pct:.4f}% (longs paying too much)")
    elif fr > 0.0005: score -= 1; reasons.append(f"Funding elevated {fr_pct:.4f}%")
    elif fr < -0.001: score += 2; reasons.append(f"🔥 Negative funding {fr_pct:.4f}% (squeeze setup)")
    elif fr < 0:      score += 1; reasons.append(f"Negative funding {fr_pct:.4f}% (slight squeeze bias)")
    else:             reasons.append(f"Funding neutral {fr_pct:.4f}%")

    # Determine action
    if score >= 7:    action, lev = "STRONG LONG", 5
    elif score >= 4:  action, lev = "LONG", 3
    elif score >= 2:  action, lev = "WEAK LONG", 2
    elif score <= -7: action, lev = "STRONG SHORT", 5
    elif score <= -4: action, lev = "SHORT", 3
    elif score <= -2: action, lev = "WEAK SHORT", 2
    else:             action, lev = "NEUTRAL / SKIP", 1

    # Levels
    entry   = price
    stop    = round(entry - 1.5 * atr_v, 6)  if "LONG" in action else round(entry + 1.5 * atr_v, 6)
    target1 = round(entry + 2.0 * atr_v, 6)  if "LONG" in action else round(entry - 2.0 * atr_v, 6)
    target2 = round(entry + 3.5 * atr_v, 6)  if "LONG" in action else round(entry - 3.5 * atr_v, 6)
    rr      = round(abs(target1 - entry) / abs(entry - stop), 2) if entry != stop else 0

    return {
        "symbol":   sym,
        "action":   action,
        "score":    score,
        "leverage": lev,
        "confidence": min(100, abs(score) * 8),
        "price":    price,
        "entry":    entry,
        "target1":  target1,
        "target2":  target2,
        "stop":     stop,
        "rr":       rr,
        "funding":  round(fr * 100, 6),
        "oi":       oi,
        "atr":      atr_v,
        "reasons":  reasons,
        "rsi_4h":   d4["rsi"],
        "macd_4h":  d4["macd"],
    }

# ─── MACRO & NEWS ─────────────────────────────────────────────────────────────

def get_economic_calendar():
    """ForexFactory this-week calendar — HIGH impact USD events."""
    data = fetch("https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    if not data:
        return []
    high_usd = [
        e for e in data
        if e.get("impact", "").upper() in ("HIGH", "MEDIUM")
        and e.get("country", "").upper() in ("USD", "US")
    ]
    return high_usd[:10]

def get_crypto_news(limit=8):
    """CoinTelegraph + CoinDesk RSS headlines."""
    headlines = []
    for feed_url in [
        "https://cointelegraph.com/rss",
        "https://coindesk.com/arc/outboundfeeds/rss/"
    ]:
        txt = fetch_text(feed_url)
        if not txt:
            continue
        try:
            root = ElementTree.fromstring(txt)
            for item in root.findall(".//item")[:limit // 2]:
                t = item.find("title")
                d = item.find("pubDate")
                headlines.append({
                    "title": t.text.strip() if t is not None else "",
                    "date":  d.text.strip() if d is not None else "",
                })
        except:
            pass
    return headlines[:limit]

def get_fear_greed():
    data = fetch("https://api.alternative.me/fng/?limit=7")
    if data and "data" in data:
        today = int(data["data"][0]["value"])
        label = data["data"][0]["value_classification"]
        hist  = [int(d["value"]) for d in data["data"]]
        trend = "RISING" if hist[0] > hist[-1] else "FALLING"
        return {"value": today, "label": label, "trend": trend, "history_7d": hist}
    return {"value": None, "label": "Unknown", "trend": "?", "history_7d": []}

def get_trending_coins():
    data = fetch("https://api.coingecko.com/api/v3/search/trending")
    if data and "coins" in data:
        return [c["item"]["symbol"].upper() for c in data["coins"][:6]]
    return []

# ─── MACRO RISK SCORE ─────────────────────────────────────────────────────────

def macro_risk(calendar_events, fg):
    """
    Returns macro risk level with RISK MULTIPLIER — never a hard block.
    Only true hard blocks: event < 30min away, or flash crash active.

    Risk multiplier table:
      LOW     → 1.00x (full size, full leverage)
      MEDIUM  → 0.75x (75% size, full leverage)
      HIGH    → 0.50x (50% size, half leverage)
      EXTREME → 0.25x (25% size, quarter leverage)
      BLOCKED → 0.00x (hard block: < 30min to event OR flash crash)
    """
    score     = 0
    warnings  = []
    hard_block = False
    now_utc   = datetime.now(timezone.utc)

    for ev in calendar_events:
        try:
            ev_time    = datetime.fromisoformat(ev.get("date", "").replace("Z", "+00:00"))
            hours_away = (ev_time - now_utc).total_seconds() / 3600
            mins_away  = hours_away * 60

            if 0 < mins_away < 30:
                # TRUE hard block — event is about to print, market will spike
                hard_block = True
                warnings.append(f"🚫 HARD BLOCK: {ev['title']} in {mins_away:.0f}min — wait for print")
            elif 0 < hours_away < 2:
                score += 4
                warnings.append(f"🚨 {ev['title']} in {hours_away*60:.0f}min → SIZE 25% | half leverage")
            elif 0 < hours_away < 6:
                score += 3
                warnings.append(f"⚠️ {ev['title']} in {hours_away:.1f}h → SIZE 50% | half leverage")
            elif 0 < hours_away < 24:
                score += 2
                warnings.append(f"📅 {ev['title']} today in {hours_away:.0f}h → SIZE 75%")
            elif -1 < hours_away <= 0:
                # Just released — wait 15min for dust to settle
                score += 2
                warnings.append(f"📍 JUST RELEASED: {ev['title']} — wait 15min then trade normally")
        except:
            pass

    if fg.get("value") and fg["value"] < 10:
        score += 1
        warnings.append(f"F&G={fg['value']} (Extreme Fear) — size down, volatility high")

    if hard_block:
        level      = "BLOCKED"
        multiplier = 0.0
    elif score >= 4:
        level      = "EXTREME"
        multiplier = 0.25
    elif score >= 3:
        level      = "HIGH"
        multiplier = 0.50
    elif score >= 2:
        level      = "MEDIUM"
        multiplier = 0.75
    else:
        level      = "LOW"
        multiplier = 1.00

    return {
        "level":       level,
        "score":       score,
        "multiplier":  multiplier,
        "hard_block":  hard_block,
        "warnings":    warnings,
    }

# ─── POSITION SIZING ──────────────────────────────────────────────────────────

def calc_position(account_usd, risk_pct, entry, stop, leverage):
    """
    account_usd: total account balance
    risk_pct: max % of account to risk (e.g. 1.0 = 1%)
    Returns: units, notional, margin required
    """
    risk_usd     = account_usd * (risk_pct / 100)
    risk_per_unit = abs(entry - stop)
    if risk_per_unit == 0:
        return {}
    units         = risk_usd / risk_per_unit
    notional      = units * entry
    margin        = notional / leverage
    return {
        "units":   round(units, 6),
        "notional": round(notional, 2),
        "margin":   round(margin, 2),
        "pct_account": round(margin / account_usd * 100, 1)
    }

# ─── MAIN PIPELINE ────────────────────────────────────────────────────────────

def run(account_usd=1000, risk_pct=1.0, top_n=15, scan_pairs=None):
    print("[ ] Fetching market overview...", file=sys.stderr)
    top = get_top_pairs(top_n)
    pairs = scan_pairs or [t["symbol"] for t in top]

    print("[ ] Fetching Fear & Greed...", file=sys.stderr)
    fg = get_fear_greed()

    print("[ ] Fetching economic calendar...", file=sys.stderr)
    calendar = get_economic_calendar()

    print("[ ] Fetching crypto news...", file=sys.stderr)
    news = get_crypto_news(8)
    trending = get_trending_coins()

    macro = macro_risk(calendar, fg)

    print(f"[ ] Scanning {len(pairs)} pairs (15m/1h/4h)...", file=sys.stderr)
    signals = []

    for sym in pairs:
        try:
            # Multi-timeframe klines
            tf_data = {}
            for tf in ["4h", "1h", "15m"]:
                df = get_klines(sym, tf, limit=100)
                if df is None or len(df) < 30:
                    continue
                c = df["close"]
                tf_data[tf] = {
                    "price": c.iloc[-1],
                    "rsi":   rsi(c),
                    "macd":  macd(c),
                    "bb":    bollinger(c),
                    "ema":   ema_trend(c),
                    "atr":   atr(df),
                    "vol":   volume_signal(df),
                }
                time.sleep(0.05)  # gentle rate limiting

            if "4h" not in tf_data:
                continue

            funding = get_funding(sym)
            oi      = get_open_interest(sym)

            sig = score_pair(sym, tf_data, funding, oi)
            pos = calc_position(account_usd, risk_pct, sig["entry"], sig["stop"], sig["leverage"])
            sig["position"] = pos
            signals.append(sig)

        except Exception as e:
            print(f"  [!] {sym}: {e}", file=sys.stderr)

        time.sleep(0.1)

    # Sort by abs(score) descending, filter actionable
    signals.sort(key=lambda x: abs(x["score"]), reverse=True)
    top_longs  = [s for s in signals if s["score"] >= 4][:3]
    top_shorts = [s for s in signals if s["score"] <= -4][:3]
    neutral    = [s for s in signals if abs(s["score"]) < 4][:3]

    return {
        "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "account_usd":  account_usd,
        "risk_pct":     risk_pct,
        "fear_greed":   fg,
        "macro_risk":   macro,
        "calendar":     calendar[:5],
        "news":         news,
        "trending":     trending,
        "top_longs":    top_longs,
        "top_shorts":   top_shorts,
        "neutral":      neutral,
        "all_signals":  signals,
        "market_overview": top[:10],
    }

def print_report(result):
    fg   = result["fear_greed"]
    mac  = result["macro_risk"]
    ts   = result["timestamp"]

    print(f"\n{'='*60}")
    print(f"  BINANCE FUTURES DAILY CALL — {ts}")
    print(f"{'='*60}\n")

    print(f"📊 FEAR & GREED: {fg['value']} — {fg['label']} ({fg['trend']})")
    print(f"   7-day history: {fg['history_7d']}\n")

    print(f"🌍 MACRO RISK: {mac['level']}")
    for w in mac["warnings"]:
        print(f"   {w}")
    print()

    print("📰 LATEST NEWS:")
    for n in result["news"][:5]:
        print(f"   • {n['title'][:85]}")
    print()

    print(f"🔥 TRENDING COINS: {', '.join(result['trending'])}\n")

    print("─"*60)
    print("📈 TOP LONG SETUPS")
    print("─"*60)
    for s in result["top_longs"]:
        _print_signal(s, result["account_usd"])

    print("─"*60)
    print("📉 TOP SHORT SETUPS")
    print("─"*60)
    for s in result["top_shorts"]:
        _print_signal(s, result["account_usd"])

    if not result["top_longs"] and not result["top_shorts"]:
        print("\n  No strong setups today. Wait for better conditions.\n")

def _print_signal(s, account_usd):
    pos = s.get("position", {})
    print(f"\n  {s['symbol']} — {s['action']} (score={s['score']}, conf={s['confidence']}%)")
    print(f"  Price: ${s['price']:,.4f} | ATR: ${s['atr']:,.4f} | Funding: {s['funding']:.4f}%")
    print(f"  RSI(4h): {s['rsi_4h']:.1f} | MACD: {'BULL' if s['macd_4h']['bullish'] else 'BEAR'}" +
          (f" [{s['macd_4h']['cross']}]" if s['macd_4h']['cross'] else ""))
    print(f"  ► ENTRY:    ${s['entry']:,.4f}")
    print(f"  ► TARGET 1: ${s['target1']:,.4f}")
    print(f"  ► TARGET 2: ${s['target2']:,.4f}")
    print(f"  ► STOP:     ${s['stop']:,.4f}")
    print(f"  ► R/R:      1:{s['rr']}  |  Leverage: {s['leverage']}x")
    if pos:
        print(f"  ► POSITION: {pos.get('units',0):.4f} units = ${pos.get('notional',0):,.0f} notional")
        print(f"             Margin: ${pos.get('margin',0):,.0f} ({pos.get('pct_account',0):.1f}% of ${account_usd:,.0f})")
    reasons_str = " | ".join(s["reasons"][:4])
    print(f"  Signals: {reasons_str}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Binance Futures Scanner")
    p.add_argument("--account", type=float, default=1000, help="Account size in USDT")
    p.add_argument("--risk",    type=float, default=1.0,  help="Risk per trade %%")
    p.add_argument("--pairs",   type=str,   default=None, help="Comma-separated symbols, e.g. BTCUSDT,ETHUSDT")
    p.add_argument("--json",    action="store_true")
    args = p.parse_args()

    pairs = args.pairs.split(",") if args.pairs else None
    result = run(account_usd=args.account, risk_pct=args.risk, scan_pairs=pairs)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_report(result)
