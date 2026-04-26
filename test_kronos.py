#!/usr/bin/env python3
"""
Kronos-mini backtest on BTC/USDT 4h candles.
Usage: python3 test_kronos.py
Outputs: directional accuracy % over last 30 days, chart saved to kronos_backtest.png
"""

import sys, os, json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import numpy as np
import urllib.request

# ── Kronos model source (cloned locally) ─────────────────────────────────────
KRONOS_DIR = Path(__file__).parent / "_kronos_src"
sys.path.insert(0, str(KRONOS_DIR))

# ── Yahoo Finance helper (same as scanner.py) ─────────────────────────────────
YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

def fetch_btc_4h(lookback_candles=600):
    """Fetch BTC-USD 4h candles via Yahoo Finance v8 (Railway-safe)."""
    url = f"{YAHOO_BASE}/BTC-USD?interval=1h&range=90d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = json.loads(urllib.request.urlopen(req, timeout=10).read())
    result = raw["chart"]["result"][0]
    ts     = result["timestamp"]
    q      = result["indicators"]["quote"][0]

    df = pd.DataFrame({
        "timestamps": pd.to_datetime(ts, unit="s", utc=True).tz_convert("UTC"),
        "open":   q["open"],
        "high":   q["high"],
        "low":    q["low"],
        "close":  q["close"],
        "volume": q["volume"],
    }).dropna()

    # Resample 1h → 4h
    df = df.set_index("timestamps")
    df4 = df.resample("4h").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna().reset_index()
    df4 = df4.rename(columns={"timestamps": "timestamps"})
    return df4.tail(lookback_candles).reset_index(drop=True)


def run_backtest(df, lookback=100, pred_len=5, n_windows=30):
    """
    Walk-forward backtest:
    - For each of the last n_windows steps, use `lookback` candles to predict next `pred_len` candles
    - Record: did predicted_close[-1] direction match actual_close[-1] direction?
    Returns: list of {"window": i, "predicted_close": float, "actual_close": float, "correct": bool}
    """
    from model import Kronos, KronosTokenizer, KronosPredictor

    print("Loading Kronos-mini from HuggingFace (first run downloads ~20MB)...")
    tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-2k")
    model     = Kronos.from_pretrained("NeoQuasar/Kronos-mini")
    predictor = KronosPredictor(model, tokenizer, device=None, max_context=2048)
    print("Model loaded.\n")

    results = []
    total   = len(df)

    # need: lookback + pred_len + n_windows candles minimum
    start   = total - n_windows - pred_len - lookback
    if start < 0:
        print(f"Not enough candles. Need {n_windows + pred_len + lookback}, got {total}")
        sys.exit(1)

    for i in range(n_windows):
        idx_start = start + i
        idx_end   = idx_start + lookback        # exclusive
        idx_pred_end = idx_end + pred_len       # exclusive

        x_df    = df.loc[idx_start:idx_end-1, ["open","high","low","close","volume"]].copy()
        x_df["amount"] = 0.0                    # not available from Yahoo, set to 0
        x_ts    = df.loc[idx_start:idx_end-1, "timestamps"]

        # future timestamps: step by 4h
        last_ts = df.loc[idx_end-1, "timestamps"]
        y_ts    = pd.Series([last_ts + pd.Timedelta(hours=4*(j+1)) for j in range(pred_len)])

        pred_df = predictor.predict(
            df          = x_df.reset_index(drop=True),
            x_timestamp = x_ts.reset_index(drop=True),
            y_timestamp = y_ts,
            pred_len    = pred_len,
            T           = 1.0,
            top_p       = 0.9,
            sample_count= 1,
            verbose     = False,
        )

        actual_df = df.loc[idx_end:idx_pred_end-1]
        if len(actual_df) < pred_len:
            continue  # skip incomplete windows at end

        last_input_close   = float(x_df["close"].iloc[-1])
        predicted_close    = float(pred_df["close"].iloc[-1])
        actual_close       = float(actual_df["close"].iloc[-1])

        pred_direction     = "UP" if predicted_close > last_input_close else "DOWN"
        actual_direction   = "UP" if actual_close    > last_input_close else "DOWN"
        correct            = (pred_direction == actual_direction)

        results.append({
            "window":           i + 1,
            "last_input_close": round(last_input_close, 2),
            "predicted_close":  round(predicted_close, 2),
            "actual_close":     round(actual_close, 2),
            "pred_direction":   pred_direction,
            "actual_direction": actual_direction,
            "correct":          correct,
        })

        icon = "✅" if correct else "❌"
        print(f"[{i+1:2d}/{n_windows}] Input={last_input_close:,.0f} "
              f"Pred={predicted_close:,.0f}({pred_direction}) "
              f"Actual={actual_close:,.0f}({actual_direction}) {icon}")

    return results


def print_summary(results):
    if not results:
        print("No results.")
        return

    correct = sum(1 for r in results if r["correct"])
    total   = len(results)
    acc     = correct / total * 100

    print("\n" + "="*50)
    print(f"  KRONOS-MINI BACKTEST RESULTS — BTC/USDT 4h")
    print("="*50)
    print(f"  Windows tested : {total}")
    print(f"  Correct        : {correct}")
    print(f"  Wrong          : {total - correct}")
    print(f"  Accuracy       : {acc:.1f}%")
    print("="*50)

    if acc >= 60:
        verdict = "✅ STRONG — Integrate into bot (Layer 4 ML confluence)"
    elif acc >= 55:
        verdict = "✅ PASS — Integrate as lightweight signal booster"
    elif acc >= 50:
        verdict = "⚠️  WEAK EDGE — Use cautiously, monitor live performance"
    else:
        verdict = "❌ FAIL — Do not integrate, below random chance"

    print(f"\n  Verdict: {verdict}")
    print(f"\n  Threshold for integration: >55%")
    print("="*50 + "\n")


def save_chart(df, results):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=False)
        fig.suptitle("Kronos-mini BTC/USDT 4h Backtest", fontsize=14, fontweight="bold")

        # Chart 1: actual close price
        close_plot = df.tail(200)
        ax1.plot(close_plot["timestamps"], close_plot["close"], color="steelblue", linewidth=1.2)
        ax1.set_title("BTC/USDT 4h Close (last 200 candles)")
        ax1.set_ylabel("Price (USD)")
        ax1.grid(True, alpha=0.3)

        # Chart 2: accuracy per window
        windows   = [r["window"] for r in results]
        correct   = [1 if r["correct"] else 0 for r in results]
        rolling   = pd.Series(correct).rolling(5, min_periods=1).mean() * 100
        colors    = ["green" if c else "red" for c in correct]

        ax2.bar(windows, [100 if c else 0 for c in correct], color=colors, alpha=0.5, label="Correct/Wrong")
        ax2.plot(windows, rolling.tolist(), color="navy", linewidth=2, label="5-window rolling accuracy")
        ax2.axhline(55, color="orange", linestyle="--", linewidth=1.5, label="Integration threshold (55%)")
        ax2.axhline(50, color="red",    linestyle=":",  linewidth=1.2, label="Random baseline (50%)")
        ax2.set_ylim(0, 110)
        ax2.set_xlabel("Window")
        ax2.set_ylabel("Accuracy %")
        ax2.set_title("Directional Accuracy per Window")
        ax2.legend(loc="upper left")
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        out = Path(__file__).parent / "kronos_backtest.png"
        plt.savefig(out, dpi=120, bbox_inches="tight")
        print(f"Chart saved → {out}")
    except Exception as e:
        print(f"Chart skipped: {e}")


if __name__ == "__main__":
    print("Fetching BTC/USDT 4h candles from Yahoo Finance...")
    df = fetch_btc_4h(lookback_candles=600)
    print(f"Got {len(df)} candles. Last close: ${df['close'].iloc[-1]:,.2f}\n")

    results = run_backtest(df, lookback=100, pred_len=5, n_windows=30)
    print_summary(results)
    save_chart(df, results)
