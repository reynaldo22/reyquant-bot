#!/usr/bin/env python3
"""
Deribit Options Analysis — 100% free, no API key.
Computes: max pain, put/call ratio, IV percentile, skew, biggest OI strikes.
"""

import json, sys
from datetime import datetime, timezone
from urllib.request import urlopen, Request
from collections import defaultdict

try:
    import pandas as pd
    import numpy as np
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

BASE = "https://www.deribit.com/api/v2/public"
HDR  = {"User-Agent": "Mozilla/5.0"}

def fetch(url):
    try:
        r = urlopen(Request(url, headers=HDR), timeout=10)
        return json.loads(r.read())["result"]
    except Exception as e:
        return None

def get_index(currency="BTC"):
    d = fetch(f"{BASE}/get_index_price?index_name={currency.lower()}_usd")
    return d["index_price"] if d else None

def get_book_summary(currency="BTC"):
    d = fetch(f"{BASE}/get_book_summary_by_currency?currency={currency}&kind=option")
    return d if d else []

def parse_instrument(name):
    """BTC-7APR26-70000-C → {expiry, strike, type}"""
    try:
        parts = name.split("-")
        return {
            "expiry":  parts[1],
            "strike":  float(parts[2]),
            "type":    parts[3],  # C or P
        }
    except:
        return None

def max_pain(summaries, current_price):
    """
    Max pain = strike where total option value destroyed is maximized.
    i.e. where most traders lose money → market gravitates here near expiry.
    """
    strikes = defaultdict(lambda: {"call_oi": 0, "put_oi": 0})
    for s in summaries:
        parsed = parse_instrument(s.get("instrument_name", ""))
        if not parsed:
            continue
        oi = s.get("open_interest", 0) or 0
        if parsed["type"] == "C":
            strikes[parsed["strike"]]["call_oi"] += oi
        else:
            strikes[parsed["strike"]]["put_oi"] += oi

    if not strikes:
        return None, {}

    all_strikes = sorted(strikes.keys())
    pain = {}
    for target in all_strikes:
        total_pain = 0
        # Call pain: all calls below target expire worthless
        for k, v in strikes.items():
            if k < target:
                total_pain += v["call_oi"] * (target - k)  # ITM calls lose
            elif k > target:
                total_pain += v["put_oi"] * (k - target)   # ITM puts lose
        pain[target] = total_pain

    max_pain_strike = min(pain, key=pain.get)
    return max_pain_strike, strikes

def put_call_analysis(summaries):
    """Total put OI vs call OI and ratio."""
    call_oi = put_oi = call_vol = put_vol = 0
    iv_list = []
    for s in summaries:
        parsed = parse_instrument(s.get("instrument_name", ""))
        if not parsed:
            continue
        oi  = s.get("open_interest", 0) or 0
        vol = s.get("volume", 0) or 0
        iv  = s.get("mark_iv", 0) or 0
        if iv > 0:
            iv_list.append(iv)
        if parsed["type"] == "C":
            call_oi  += oi
            call_vol += vol
        else:
            put_oi   += oi
            put_vol  += vol

    ratio_oi  = round(put_oi  / call_oi  if call_oi  > 0 else 0, 3)
    ratio_vol = round(put_vol / call_vol if call_vol > 0 else 0, 3)
    avg_iv    = round(sum(iv_list) / len(iv_list), 1) if iv_list else 0

    if ratio_oi > 1.5:   pc_signal = "EXTREME FEAR 🔴 (contrarian LONG)"
    elif ratio_oi > 1.0: pc_signal = "BEARISH 🟡"
    elif ratio_oi > 0.7: pc_signal = "NEUTRAL ⚪"
    elif ratio_oi > 0.5: pc_signal = "BULLISH 🟢"
    else:                pc_signal = "EXTREME GREED 🟢 (contrarian SHORT)"

    return {
        "call_oi": round(call_oi, 1),
        "put_oi":  round(put_oi, 1),
        "ratio_oi": ratio_oi,
        "ratio_vol": ratio_vol,
        "signal": pc_signal,
        "avg_iv": avg_iv,
    }

def biggest_strikes(strikes_data, top_n=8):
    """Strikes with most total OI — price magnets."""
    totals = [
        {"strike": k, "total_oi": v["call_oi"] + v["put_oi"],
         "call_oi": round(v["call_oi"], 1), "put_oi": round(v["put_oi"], 1)}
        for k, v in strikes_data.items()
        if v["call_oi"] + v["put_oi"] > 0
    ]
    return sorted(totals, key=lambda x: x["total_oi"], reverse=True)[:top_n]

def nearest_expiry_iv(summaries, current_price):
    """IV for nearest expiry options near ATM."""
    by_expiry = defaultdict(list)
    for s in summaries:
        parsed = parse_instrument(s.get("instrument_name", ""))
        if not parsed:
            continue
        iv = s.get("mark_iv") or 0
        if iv <= 0:
            continue
        # Near ATM = within 10% of current price
        if abs(parsed["strike"] - current_price) / current_price < 0.10:
            by_expiry[parsed["expiry"]].append(iv)

    result = {}
    for exp, ivs in by_expiry.items():
        result[exp] = round(sum(ivs) / len(ivs), 1)
    # Sort by expiry date
    return dict(sorted(result.items())[:5])

def skew(summaries, current_price, expiry_filter=None):
    """
    Options skew: 25-delta put IV vs 25-delta call IV.
    Positive skew = puts more expensive = fear = bearish
    Negative skew = calls more expensive = greed = bullish
    """
    otm_calls, otm_puts = [], []
    for s in summaries:
        parsed = parse_instrument(s.get("instrument_name", ""))
        if not parsed:
            continue
        if expiry_filter and parsed["expiry"] != expiry_filter:
            continue
        iv = s.get("mark_iv") or 0
        strike = parsed["strike"]
        if iv <= 0:
            continue
        pct_from_atm = (strike - current_price) / current_price
        # OTM calls: 5-25% above spot
        if parsed["type"] == "C" and 0.05 < pct_from_atm < 0.25:
            otm_calls.append(iv)
        # OTM puts: 5-25% below spot
        elif parsed["type"] == "P" and -0.25 < pct_from_atm < -0.05:
            otm_puts.append(iv)

    if not otm_calls or not otm_puts:
        return None
    call_iv = sum(otm_calls) / len(otm_calls)
    put_iv  = sum(otm_puts)  / len(otm_puts)
    skew_val = round(put_iv - call_iv, 1)

    if skew_val > 20:   label = "EXTREME FEAR (puts way pricier)"
    elif skew_val > 10: label = "FEARFUL (puts pricier)"
    elif skew_val > 0:  label = "SLIGHT FEAR"
    elif skew_val > -10: label = "SLIGHT GREED"
    else:               label = "GREEDY (calls pricier)"

    return {"put_iv": round(put_iv,1), "call_iv": round(call_iv,1),
            "skew": skew_val, "label": label}

def run(currency="BTC"):
    print(f"[ ] Fetching {currency} index price...", file=sys.stderr)
    spot = get_index(currency)
    if not spot:
        print("ERROR: Could not get index price", file=sys.stderr)
        return {}

    print(f"[ ] Fetching {currency} options book ({currency}= ${spot:,.0f})...", file=sys.stderr)
    summaries = get_book_summary(currency)
    if not summaries:
        print("ERROR: No options data", file=sys.stderr)
        return {}

    print(f"[ ] Analyzing {len(summaries)} options contracts...", file=sys.stderr)

    mp_strike, strikes_data = max_pain(summaries, spot)
    pc                      = put_call_analysis(summaries)
    big_strikes             = biggest_strikes(strikes_data)
    iv_by_expiry            = nearest_expiry_iv(summaries, spot)
    nearest_exp             = list(iv_by_expiry.keys())[0] if iv_by_expiry else None
    skew_data               = skew(summaries, spot, nearest_exp)

    mp_delta = round(((mp_strike or spot) - spot) / spot * 100, 1) if mp_strike else 0
    mp_bias  = "BULLISH (max pain above spot)" if mp_delta > 1 else \
               "BEARISH (max pain below spot)" if mp_delta < -1 else "NEUTRAL"

    return {
        "currency":      currency,
        "spot":          spot,
        "timestamp":     datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "max_pain":      {"strike": mp_strike, "delta_pct": mp_delta, "bias": mp_bias},
        "put_call":      pc,
        "iv_by_expiry":  iv_by_expiry,
        "skew":          skew_data,
        "big_strikes":   big_strikes,
        "total_options": len(summaries),
    }

def print_report(r):
    spot = r["spot"]
    mp   = r["max_pain"]
    pc   = r["put_call"]
    sk   = r["skew"]

    print(f"\n{'='*55}")
    print(f"  {r['currency']} OPTIONS — {r['timestamp']}")
    print(f"  Spot: ${spot:,.0f}  |  {r['total_options']} active contracts")
    print(f"{'='*55}\n")

    print(f"🎯 MAX PAIN:    ${mp['strike']:,.0f}  ({mp['delta_pct']:+.1f}% from spot)")
    print(f"   Bias:        {mp['bias']}\n")

    print(f"📊 PUT/CALL OI: {pc['ratio_oi']:.2f}  ({pc['signal']})")
    print(f"   Put OI:      {pc['put_oi']:,.0f}  |  Call OI: {pc['call_oi']:,.0f}")
    print(f"   Avg IV:      {pc['avg_iv']}%\n")

    if sk:
        print(f"📐 OPTIONS SKEW: {sk['skew']:+.1f}%  ({sk['label']})")
        print(f"   OTM Put IV:  {sk['put_iv']}%  |  OTM Call IV: {sk['call_iv']}%\n")

    if r["iv_by_expiry"]:
        print("⚡ ATM IV BY EXPIRY (nearest 5):")
        for exp, iv in r["iv_by_expiry"].items():
            bar = "█" * int(iv / 10)
            print(f"   {exp:12}  {iv:5.1f}%  {bar}")
        print()

    print("💰 BIGGEST OI STRIKES (price magnets):")
    for s in r["big_strikes"][:6]:
        pct  = round((s["strike"] - spot) / spot * 100, 1)
        tag  = f"ATM" if abs(pct) < 2 else f"{pct:+.0f}%"
        bar  = "█" * min(20, int(s["total_oi"] / (r["big_strikes"][0]["total_oi"] / 20)))
        print(f"   ${s['strike']:>8,.0f} ({tag:6})  C:{s['call_oi']:6.0f} P:{s['put_oi']:6.0f}  {bar}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--currency", default="BTC", choices=["BTC","ETH"])
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    r = run(args.currency)
    if args.json:
        print(json.dumps(r, indent=2, default=str))
    else:
        print_report(r)
