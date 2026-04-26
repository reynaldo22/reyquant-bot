#!/usr/bin/env python3
"""
Kronos-mini signal layer for reyquant bot.
Layer 4: ML candle forecast confluence — adds +1 to score when direction matches TA.

Usage:
    from kronos_signal import get_kronos_signal, format_kronos_card
    sig = get_kronos_signal("BTCUSDT", ohlcv_df)
    # sig = {"direction": "LONG", "target": 85000, "stop": 78000,
    #        "confidence": 0.72, "pred_close": 84500, "current_close": 83000}
"""

import sys, os, json, time
from pathlib import Path
from functools import lru_cache
from datetime import datetime, timezone

import pandas as pd
import numpy as np
import urllib.request

# ── Kronos model source ───────────────────────────────────────────────────────
KRONOS_DIR = Path(__file__).parent / "_kronos_src"
sys.path.insert(0, str(KRONOS_DIR))

# ── Yahoo Finance OHLCV (same geo-safe source as scanner.py) ─────────────────
YAHOO_MAP = {
    "BTCUSDT":  "BTC-USD",  "ETHUSDT":  "ETH-USD",  "SOLUSDT":  "SOL-USD",
    "BNBUSDT":  "BNB-USD",  "XRPUSDT":  "XRP-USD",  "ADAUSDT":  "ADA-USD",
    "AVAXUSDT": "AVAX-USD", "DOTUSDT":  "DOT-USD",  "LINKUSDT": "LINK-USD",
    "MATICUSDT":"MATIC-USD","DOGEUSDT": "DOGE-USD", "LTCUSDT":  "LTC-USD",
    "TAOUSDT":  "TAO-USD",  "SUIUSDT":  "SUI-USD",  "FETUSDT":  "FET-USD",
    "RENDERUSDT":"RNDR-USD","INJUSDT":  "INJ-USD",  "APTUSDT":  "APT-USD",
}

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

# ── Predictor singleton (load once, reuse) ────────────────────────────────────
_predictor = None
_predictor_loaded_at = None
_PREDICTOR_TTL = 3600  # reload every hour max

def _load_predictor():
    """Load Kronos-mini once. Returns KronosPredictor or None on failure."""
    global _predictor, _predictor_loaded_at
    now = time.time()
    if _predictor is not None and (now - (_predictor_loaded_at or 0)) < _PREDICTOR_TTL:
        return _predictor
    try:
        from model import Kronos, KronosTokenizer, KronosPredictor
        tokenizer  = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-2k")
        model      = Kronos.from_pretrained("NeoQuasar/Kronos-mini")
        _predictor = KronosPredictor(model, tokenizer, device=None, max_context=2048)
        _predictor_loaded_at = now
        return _predictor
    except Exception as e:
        print(f"[kronos_signal] Model load failed: {e}")
        return None


def _fetch_ohlcv_4h(symbol: str, lookback: int = 120):
    """Fetch 4h candles for a Binance symbol via Yahoo Finance."""
    yahoo_sym = YAHOO_MAP.get(symbol)
    if not yahoo_sym:
        return None
    try:
        url = f"{YAHOO_BASE}/{yahoo_sym}?interval=1h&range=30d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = json.loads(urllib.request.urlopen(req, timeout=10).read())
        result = raw["chart"]["result"][0]
        ts = result["timestamp"]
        q  = result["indicators"]["quote"][0]

        df = pd.DataFrame({
            "timestamps": pd.to_datetime(ts, unit="s", utc=True),
            "open":   q["open"],
            "high":   q["high"],
            "low":    q["low"],
            "close":  q["close"],
            "volume": q["volume"],
        }).dropna().set_index("timestamps")

        # Resample 1h → 4h
        df4 = df.resample("4h").agg({
            "open":   "first",
            "high":   "max",
            "low":    "min",
            "close":  "last",
            "volume": "sum",
        }).dropna().reset_index()

        return df4.tail(lookback).reset_index(drop=True)
    except Exception as e:
        print(f"[kronos_signal] OHLCV fetch failed for {symbol}: {e}")
        return None


def get_kronos_signal(symbol: str, ohlcv_df=None,
                      pred_len: int = 5) -> dict:
    """
    Get Kronos-mini directional signal for a symbol.

    Args:
        symbol: Binance symbol e.g. "BTCUSDT"
        ohlcv_df: Optional pre-fetched DataFrame with columns
                  [timestamps, open, high, low, close, volume]
                  If None, fetches from Yahoo Finance automatically.
        pred_len: Number of candles to predict (default 5 = 20h ahead on 4h)

    Returns:
        {
          "symbol":        str,
          "direction":     "LONG" | "SHORT" | "NEUTRAL",
          "current_close": float,
          "pred_close":    float,       # predicted close at pred_len candles
          "pred_high":     float,       # predicted high (natural TP)
          "pred_low":      float,       # predicted low (natural SL)
          "change_pct":    float,       # (pred_close - current) / current * 100
          "confidence":    float,       # 0.0–1.0 (based on magnitude of move)
          "model":         str,
          "error":         str | None,
        }
    """
    result_base = {
        "symbol":        symbol,
        "direction":     "NEUTRAL",
        "current_close": 0.0,
        "pred_close":    0.0,
        "pred_high":     0.0,
        "pred_low":      0.0,
        "change_pct":    0.0,
        "confidence":    0.0,
        "model":         "Kronos-mini",
        "error":         None,
    }

    # Load model
    predictor = _load_predictor()
    if predictor is None:
        result_base["error"] = "Model unavailable"
        return result_base

    # Fetch data if not provided
    if ohlcv_df is None:
        ohlcv_df = _fetch_ohlcv_4h(symbol, lookback=120)
    if ohlcv_df is None or len(ohlcv_df) < 50:
        result_base["error"] = f"Insufficient data for {symbol}"
        return result_base

    try:
        lookback = min(100, len(ohlcv_df))
        x_df = ohlcv_df.tail(lookback).copy()

        # Ensure required columns
        if "timestamps" not in x_df.columns:
            result_base["error"] = "Missing timestamps column"
            return result_base
        x_df["amount"] = 0.0  # Yahoo doesn't provide amount

        x_ts   = x_df["timestamps"].reset_index(drop=True)
        x_data = x_df[["open","high","low","close","volume","amount"]].reset_index(drop=True)

        last_ts = x_ts.iloc[-1]
        y_ts = pd.Series([last_ts + pd.Timedelta(hours=4*(j+1)) for j in range(pred_len)])

        pred_df = predictor.predict(
            df          = x_data,
            x_timestamp = x_ts,
            y_timestamp = y_ts,
            pred_len    = pred_len,
            T           = 1.0,
            top_p       = 0.9,
            sample_count= 1,
            verbose     = False,
        )

        current_close = float(x_data["close"].iloc[-1])
        pred_close    = float(pred_df["close"].iloc[-1])
        pred_high     = float(pred_df["high"].max())
        pred_low      = float(pred_df["low"].min())
        change_pct    = (pred_close - current_close) / current_close * 100

        # Direction
        if change_pct > 0.3:
            direction = "LONG"
        elif change_pct < -0.3:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        # Confidence: scaled by magnitude (0.3%=0.3 → 2%+=1.0)
        confidence = min(1.0, abs(change_pct) / 2.0)

        return {
            **result_base,
            "direction":     direction,
            "current_close": round(current_close, 4),
            "pred_close":    round(pred_close, 4),
            "pred_high":     round(pred_high, 4),
            "pred_low":      round(pred_low, 4),
            "change_pct":    round(change_pct, 3),
            "confidence":    round(confidence, 3),
        }

    except Exception as e:
        result_base["error"] = str(e)
        return result_base


def score_delta(kronos_sig: dict, ta_direction: str) -> float:
    """
    Returns score adjustment for trade_card generator.
    +1.0 if Kronos confirms TA direction
    -0.5 if Kronos contradicts
     0.0 if Kronos is NEUTRAL or errored
    """
    if kronos_sig.get("error") or kronos_sig["direction"] == "NEUTRAL":
        return 0.0
    if kronos_sig["direction"] == ta_direction:
        return 1.0 * kronos_sig["confidence"]
    return -0.5


def format_kronos_card(sig: dict) -> str:
    """Format Kronos signal for Telegram."""
    if sig.get("error"):
        return f"🤖 Kronos: unavailable ({sig['error']})"

    arrow = "📈" if sig["direction"] == "LONG" else ("📉" if sig["direction"] == "SHORT" else "➡️")
    conf  = int(sig["confidence"] * 100)

    return (
        f"🤖 *KRONOS-MINI ML FORECAST*\n"
        f"  {arrow} Direction: *{sig['direction']}*\n"
        f"  Current:  ${sig['current_close']:,.2f}\n"
        f"  Predicted: ${sig['pred_close']:,.2f} ({sig['change_pct']:+.2f}%)\n"
        f"  ML Target: ${sig['pred_high']:,.2f}\n"
        f"  ML Stop:   ${sig['pred_low']:,.2f}\n"
        f"  Confidence: {conf}%\n"
        f"  Model: Kronos-mini | AAAI 2026"
    )


if __name__ == "__main__":
    # Quick test
    sym = sys.argv[1].upper() if len(sys.argv) > 1 else "BTCUSDT"
    print(f"Testing Kronos signal for {sym}...")
    sig = get_kronos_signal(sym)
    print(json.dumps(sig, indent=2))
    print("\n" + format_kronos_card(sig))
