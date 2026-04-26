#!/usr/bin/env python3
"""
TRADING PIPELINE TELEGRAM BOT
Text from your phone → triggers full quant pipeline → get trading calls back.

SETUP:
1. Message @BotFather on Telegram → /newbot → get TOKEN
2. Set TELEGRAM_BOT_TOKEN in ~/.claude/settings.json OR as env var
3. Get your chat ID: message the bot first, then run:
   python3 -c "import requests; print(requests.get('https://api.telegram.org/bot{TOKEN}/getUpdates').json())"
4. Set TELEGRAM_CHAT_ID (your personal chat ID, so only you can use it)
5. Run: python3 bot.py &  (runs in background)
"""

import os, sys, json, subprocess, asyncio, logging, time
from datetime import datetime, timezone
from pathlib import Path

# Add parent scripts to path — works on both Mac and Oracle Cloud
SCRIPT_DIR  = Path(__file__).parent.resolve()
SKILLS_DIR  = SCRIPT_DIR  # on cloud, all scripts are in same folder
sys.path.insert(0, str(SCRIPT_DIR))

try:
    from telegram import Update, Bot
    from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
    from telegram.constants import ParseMode
except ImportError:
    print("Run: pip3 install python-telegram-bot")
    sys.exit(1)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.WARNING
)

# ─── CONFIG ───────────────────────────────────────────────────────────────────

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

if not TOKEN:
    print("ERROR: Set TELEGRAM_BOT_TOKEN environment variable")
    print("Get token from @BotFather on Telegram")
    sys.exit(1)

# ─── PIPELINE RUNNER ─────────────────────────────────────────────────────────

def run_script(script_path: str, args: list = [], timeout: int = 90) -> str:
    """Run a Python script and return its stdout."""
    try:
        result = subprocess.run(
            ["python3", script_path] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip() or result.stderr.strip() or "No output"
    except subprocess.TimeoutExpired:
        return "⏱️ Scan timed out (>90s). Try again."
    except Exception as e:
        return f"Error: {e}"

def format_for_telegram(text: str) -> str:
    """Convert plain text report to Telegram-friendly markdown."""
    lines = text.split("\n")
    out = []
    for line in lines:
        # Convert section headers to bold
        if line.startswith("===") or line.startswith("───"):
            out.append(f"\n*{'─'*30}*")
        elif "STRONG BUY" in line or "STRONG LONG" in line:
            out.append(f"🟢 `{line.strip()}`")
        elif "LONG" in line and "►" in line:
            out.append(f"📈 `{line.strip()}`")
        elif "STRONG SELL" in line or "STRONG SHORT" in line:
            out.append(f"🔴 `{line.strip()}`")
        elif "SHORT" in line and "►" in line:
            out.append(f"📉 `{line.strip()}`")
        elif "STOP" in line and "$" in line:
            out.append(f"🛑 `{line.strip()}`")
        elif "TARGET" in line and "$" in line:
            out.append(f"🎯 `{line.strip()}`")
        elif "ENTRY" in line and "$" in line:
            out.append(f"📍 `{line.strip()}`")
        elif "⚠️" in line or "🚨" in line or "DANGER" in line:
            out.append(f"⚠️ *{line.strip()}*")
        elif line.strip():
            out.append(line)
    return "\n".join(out)[:4096]  # Telegram message limit

# ─── COMMAND HANDLERS ─────────────────────────────────────────────────────────

async def auth_check(update: Update) -> bool:
    """Only allow messages from your chat ID."""
    if CHAT_ID and str(update.effective_chat.id) != str(CHAT_ID):
        await update.message.reply_text("⛔ Unauthorized.")
        return False
    return True

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    msg = f"""
⚡ *TRADING BOT ONLINE* — {now}

*Available commands:*

🃏 `/daily` — *FULL TRADE CARDS* ← main command
  _All pairs + big player validation + entry/TP/SL/hold/skip_

📊 `/plan` — Quick daily pipeline
🔍 `/scan` — Futures scanner (top pairs)
🐋 `/whale` — Whale intelligence
📐 `/options` — Deribit max pain + IV
📰 `/news` — Latest crypto + macro news
🔥 `/hype` — Trending + volume spikes

📋 `/pairs` — Best pairs to trade now
⚖️ `/btc` — BTC deep analysis
💹 `/eth` — ETH deep analysis
⏰ `/time` — Is it a good time to trade?
📏 `/size PAIR ENTRY STOP` — Position calculator
🗓️ `/macro` — Economic calendar risks

🤖 `/kronos PAIR` — Kronos-mini ML candle forecast
❓ `/help` — Show this menu

_Or just type naturally:_
"what pairs today" → `/plan`
"scan market" → `/scan`
"any whales" → `/whale`
"""
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, context)

async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full pipeline — the main command."""
    if not await auth_check(update): return
    msg = await update.message.reply_text("⏳ Running full pipeline... (30-60s)")

    # Run all scans in sequence
    scanner_path = str(SCRIPT_DIR / "scanner.py")
    whale_path   = str(SCRIPT_DIR / "whale_scan.py")
    deribit_path = str(SCRIPT_DIR / "options_analysis.py")

    await msg.edit_text("⏳ Scanning Binance futures...")
    scan_out   = run_script(scanner_path, ["--pairs", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,TAOUSDT,SUIUSDT,ADAUSDT,XRPUSDT", "--account", "1000"])

    await msg.edit_text("⏳ Running whale intelligence...")
    whale_out  = run_script(whale_path, timeout=60)

    await msg.edit_text("⏳ Fetching options data...")
    opt_out    = run_script(deribit_path, timeout=30)

    # Also check algo rules
    try:
        from algo_rules import is_valid_trading_time, next_scan_time, AccountConfig, get_leverage
        can_trade, time_reason = is_valid_trading_time()
        next_scan = next_scan_time()
        time_block = f"\n⏰ *TIMING*\n{time_reason}\n{next_scan}"
    except:
        time_block = ""

    # Combine into one message
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header = f"⚡ *FULL PIPELINE — {now_str}*\n{'─'*35}\n"

    # Send whale intel first (most important)
    whale_formatted = format_for_telegram(whale_out)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"🐋 *WHALE INTEL*\n```\n{whale_out[:1800]}\n```",
        parse_mode=ParseMode.MARKDOWN
    )

    # Send options
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📐 *OPTIONS (DERIBIT)*\n```\n{opt_out[:1500]}\n```",
        parse_mode=ParseMode.MARKDOWN
    )

    # Send scanner
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📊 *FUTURES SCAN*\n```\n{scan_out[:2000]}\n```",
        parse_mode=ParseMode.MARKDOWN
    )

    if time_block:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=time_block,
            parse_mode=ParseMode.MARKDOWN
        )

    await msg.delete()

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    msg = await update.message.reply_text("🔍 Scanning markets...")
    script = str(SCRIPT_DIR / "scanner.py")
    out = run_script(script, ["--pairs", "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,TAOUSDT,SUIUSDT,ADAUSDT,XRPUSDT,DOTUSDT,AVAXUSDT", "--account", "1000"])
    await msg.edit_text(f"📊 *MARKET SCAN*\n```\n{out[:3500]}\n```", parse_mode=ParseMode.MARKDOWN)

async def cmd_whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    msg = await update.message.reply_text("🐋 Running whale scan...")
    script = str(SCRIPT_DIR / "whale_scan.py")
    out = run_script(script, timeout=60)
    await msg.edit_text(f"🐋 *WHALE INTELLIGENCE*\n```\n{out[:3500]}\n```", parse_mode=ParseMode.MARKDOWN)

async def cmd_options(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    msg = await update.message.reply_text("📐 Fetching Deribit options...")
    script = str(SCRIPT_DIR / "options_analysis.py")
    out = run_script(script, timeout=30)
    await msg.edit_text(f"📐 *BTC OPTIONS (DERIBIT)*\n```\n{out[:3500]}\n```", parse_mode=ParseMode.MARKDOWN)

async def cmd_hype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    msg = await update.message.reply_text("🔥 Scanning for hype...")
    script = str(SCRIPT_DIR / "hype_scanner.py")
    out = run_script(script, timeout=90)
    await msg.edit_text(f"🔥 *HYPE & TREND SCAN*\n```\n{out[:3500]}\n```", parse_mode=ParseMode.MARKDOWN)

async def cmd_btc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    msg = await update.message.reply_text("₿ Analyzing BTC...")
    script = str(SCRIPT_DIR / "scanner.py")
    out = run_script(script, ["--pairs", "BTCUSDT", "--account", "1000"])
    whale_script = str(SCRIPT_DIR / "whale_scan.py")
    opt_script   = str(SCRIPT_DIR / "options_analysis.py")
    whale_out = run_script(whale_script, timeout=60)
    opt_out   = run_script(opt_script, timeout=30)
    combined = f"₿ *BTC DEEP ANALYSIS*\n\n*TA:*\n```{out[:1200]}```\n*WHALE:*\n```{whale_out[:800]}```\n*OPTIONS:*\n```{opt_out[:800]}```"
    await msg.edit_text(combined[:4096], parse_mode=ParseMode.MARKDOWN)

async def cmd_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    try:
        from algo_rules import is_valid_trading_time, next_scan_time
        can, reason = is_valid_trading_time()
        nxt = next_scan_time()
        icon = "✅" if can else "❌"
        await update.message.reply_text(
            f"⏰ *TRADING TIME CHECK*\n\n{icon} {reason}\n\n📅 {nxt}",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def cmd_size(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /size BTCUSDT 69000 68000"""
    if not await auth_check(update): return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Usage: `/size BTCUSDT 69000 68000`\n(pair, entry, stop)", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        from algo_rules import Position, AccountConfig, get_leverage
        pair  = args[0].upper()
        entry = float(args[1])
        stop  = float(args[2])
        # Estimate ATR as distance between entry and stop / 1.5
        atr_est = abs(entry - stop) / 1.5
        direction = "LONG" if entry > stop else "SHORT"
        acc = AccountConfig(balance=1000, risk_pct=1.0)
        pos = Position(pair, direction, entry=entry, atr_4h=atr_est, account=acc)
        lev, _ = get_leverage(pair)
        await update.message.reply_text(
            f"📏 *POSITION CALCULATOR*\n```{pos.summary()}```",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await update.message.reply_text(f"Error: {e}\nUsage: `/size BTCUSDT 69000 68000`", parse_mode=ParseMode.MARKDOWN)

async def cmd_macro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    import urllib.request
    try:
        req = urllib.request.Request("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                                      headers={"User-Agent":"Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        high_usd = [e for e in data if e.get("impact","").upper() in ("HIGH","MEDIUM")
                    and e.get("country","").upper() in ("USD","US")][:6]
        lines = ["🗓️ *MACRO CALENDAR — HIGH IMPACT USD*\n"]
        for e in high_usd:
            dt = e.get("date","?")[:16]
            title = e.get("title","?")[:40]
            impact = "🔴" if e.get("impact","").upper() == "HIGH" else "🟡"
            lines.append(f"{impact} `{dt}` — {title}")
        lines.append("\n⚠️ Close all positions 2h before each event")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"Error fetching calendar: {e}")

async def cmd_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick pairs recommendation."""
    if not await auth_check(update): return
    msg = await update.message.reply_text("🔍 Finding best pairs...")
    script = str(SCRIPT_DIR / "hype_scanner.py")
    hype_out = run_script(script, timeout=90)

    try:
        from algo_rules import is_valid_trading_time
        can_trade, time_reason = is_valid_trading_time()
        time_note = f"\n⏰ {time_reason}"
    except:
        time_note = ""

    await msg.edit_text(
        f"💹 *BEST PAIRS NOW*{time_note}\n\n```\n{hype_out[:2500]}\n```",
        parse_mode=ParseMode.MARKDOWN
    )

# ─── NATURAL LANGUAGE HANDLER ─────────────────────────────────────────────────
# INTENT_MAP is built lazily inside handle_text so all cmd_ functions are defined first

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    text = update.message.text.lower().strip()

    intent_map = {
        ("daily", "trade card", "full card", "all pairs", "give me pairs",
         "what can i trade", "show me trades", "trade list"): cmd_daily,
        ("plan", "what pairs", "what trade", "daily call", "daily plan",
         "give me call", "what should i", "good morning", "morning", "today"): cmd_daily,
        ("scan", "market scan", "scan market", "check market"): cmd_scan,
        ("whale", "whales", "smart money", "on chain"): cmd_whale,
        ("option", "max pain", "deribit", "iv ", "options"): cmd_options,
        ("hype", "trending", "trend", "hot coin", "what pumping"): cmd_hype,
        ("btc", "bitcoin", "₿"): cmd_btc,
        ("time", "good time", "should i trade", "trading time"): cmd_time,
        ("macro", "calendar", "event", "cpi", "fomc", "news"): cmd_macro,
        ("pairs", "best pair", "which pair"): cmd_pairs,
    }

    # Match intent
    for keywords, handler in intent_map.items():
        if any(kw in text for kw in keywords):
            await handler(update, context)
            return

    # Fallback
    await update.message.reply_text(
        "🤖 Not sure what you want. Try:\n"
        "• `/plan` — full daily call\n"
        "• `/scan` — market scan\n"
        "• `/whale` — whale intel\n"
        "• `/help` — all commands",
        parse_mode=ParseMode.MARKDOWN
    )

# ─── NEWS HANDLER ─────────────────────────────────────────────────────────────

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Unified confidence engine — all 4 layers fused into one score per pair.
    I decide. You execute.
    """
    if not await auth_check(update): return
    msg = await update.message.reply_text("⚡ Confidence Engine starting...\n(TA + Whales + Kronos + Macro in parallel)")

    try:
        import urllib.request as _ur2, json as _json2
        from confidence_engine import fuse, format_fusion_card
        from scanner import get_fear_greed

        # ── Pair discovery: 3 layers ───────────────────────────────────────
        await msg.edit_text("📡 Discovering pairs (Core + Trending + Radar)...")

        core_pairs = ["BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT",
                      "ADAUSDT","LINKUSDT","AVAXUSDT"]
        hype_syms  = []
        radar_header_text = ""

        try:
            cg_data = _json2.loads(_ur2.urlopen(_ur2.Request(
                "https://api.coingecko.com/api/v3/search/trending",
                headers={"User-Agent":"Mozilla/5.0"}), timeout=6).read())
            hype_syms = [
                c["item"]["symbol"].upper() + "USDT"
                for c in cg_data.get("coins", [])[:6]
                if c["item"]["symbol"].upper() + "USDT" not in core_pairs
            ]
        except:
            pass

        try:
            from market_radar import scan as radar_scan, format_radar_header
            radar = radar_scan(max_pairs=8)
            radar_syms = [o["symbol"] for o in radar["opportunities"]
                          if o.get("symbol") and o["symbol"] not in core_pairs + hype_syms]
            radar_header_text = format_radar_header(radar)
        except:
            radar_syms = []

        scan_list = list(dict.fromkeys(
            core_pairs + hype_syms[:4] + radar_syms[:5]
        ))[:12]

        # ── Fear & Greed ───────────────────────────────────────────────────
        try:
            fg = get_fear_greed()
        except:
            fg = {}

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        fg_val  = fg.get("value", "?")
        fg_lbl  = fg.get("label", "")

        header = (
            f"⚡ *CONFIDENCE ENGINE — {now_str}*\n"
            f"{'━'*32}\n"
            f"📊 F&G: *{fg_val}* {fg_lbl}\n"
            f"🔎 Scanning {len(scan_list)} pairs: "
            f"{', '.join(s.replace('USDT','') for s in scan_list[:8])}"
        )
        if radar_header_text:
            header += f"\n\n{radar_header_text}"

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=header, parse_mode=ParseMode.MARKDOWN
        )

        # ── Run confidence engine for each pair ────────────────────────────
        results = []
        for i, sym in enumerate(scan_list):
            await msg.edit_text(
                f"🧠 Fusing signals [{i+1}/{len(scan_list)}]: {sym}\n"
                f"TA + Whales + Kronos + Macro in parallel..."
            )
            fusion = fuse(sym, account_usd=1000, risk_pct=1.0, timeout=40.0)
            results.append(fusion)

        # ── Sort by confidence, filter: only TRADE and above ───────────────
        tradeable = sorted(
            [r for r in results if r.verdict in ("STRONG TRADE","TRADE")],
            key=lambda x: x.confidence, reverse=True
        )
        weak = sorted(
            [r for r in results if r.verdict == "WEAK"],
            key=lambda x: x.confidence, reverse=True
        )
        skipped = [r for r in results if r.verdict == "SKIP"]

        # Send top 5 tradeable cards
        sent = 0
        for fusion in tradeable[:5]:
            card_text = format_fusion_card(fusion, account_usd=1000)
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=card_text, parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=card_text.replace("*","").replace("`","").replace("_","")
                )
            sent += 1
            time.sleep(0.4)

        # If no strong signals, show best weak ones
        if sent == 0 and weak:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ *No high-confidence setups. Showing weak signals only:*",
                parse_mode=ParseMode.MARKDOWN
            )
            for fusion in weak[:3]:
                card_text = format_fusion_card(fusion, account_usd=1000)
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=card_text, parse_mode=ParseMode.MARKDOWN
                    )
                except Exception:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=card_text.replace("*","").replace("`","").replace("_","")
                    )
                time.sleep(0.4)

        # Summary footer
        skip_syms = ", ".join(r.symbol.replace("USDT","") for r in skipped[:6])
        footer = (
            f"{'━'*32}\n"
            f"📊 *SUMMARY:* {len(tradeable)} trade  |  {len(weak)} weak  |  {len(skipped)} skip\n"
            f"⏭️ Skipped: {skip_syms or '—'}\n\n"
            f"*RULES:*\n"
            f"  ≥75% → Full size  |  60–75% → Standard\n"
            f"  50–60% → Half size  |  <50% → Skip\n"
            f"  After TP1 → move SL to breakeven\n"
            f"  Max 3 positions open at once\n"
            f"{'━'*32}"
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=footer, parse_mode=ParseMode.MARKDOWN
        )
        await msg.delete()

    except Exception as e:
        await msg.edit_text(f"Confidence engine error: {e}\n\nTry /scan or /plan instead.")

async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step-by-step diagnostic — tells us EXACTLY where it fails on Railway."""
    if not await auth_check(update): return
    lines = []
    def log(msg):
        lines.append(msg)
        print(msg)

    await update.message.reply_text("🔬 Running diagnostics...")

    # 1. Python + pandas
    try:
        import sys
        log(f"✅ Python {sys.version[:6]}")
        import pandas as pd
        log(f"✅ pandas {pd.__version__}")
        import numpy as np
        log(f"✅ numpy {np.__version__}")
    except Exception as e:
        log(f"❌ pandas/numpy: {e}")

    # 2. Yahoo Finance reachable
    try:
        import urllib.request, json
        r = urllib.request.urlopen(
            urllib.request.Request(
                "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD?interval=1h&range=1d",
                headers={"User-Agent":"Mozilla/5.0"}), timeout=8)
        d = json.loads(r.read())
        last = d["chart"]["result"][0]["indicators"]["quote"][0]["close"][-1]
        log(f"✅ Yahoo Finance reachable — BTC=${last:,.0f}")
    except Exception as e:
        log(f"❌ Yahoo Finance: {e}")

    # 2b. CoinGecko reachable
    try:
        r2 = urllib.request.urlopen(
            urllib.request.Request(
                "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
                headers={"User-Agent":"Mozilla/5.0"}), timeout=8)
        d2 = json.loads(r2.read())
        log(f"✅ CoinGecko reachable — BTC=${d2['bitcoin']['usd']:,}")
    except Exception as e:
        log(f"❌ CoinGecko: {e}")

    # 3. get_top_pairs
    try:
        from scanner import get_top_pairs
        top = get_top_pairs(3)
        log(f"✅ get_top_pairs: {[t['symbol'] for t in top]}")
    except Exception as e:
        log(f"❌ get_top_pairs: {e}")

    # 4. get_klines
    try:
        from scanner import get_klines
        df = get_klines("BTCUSDT", "4h", limit=30)
        if df is not None:
            log(f"✅ get_klines: {len(df)} candles, last close={df['close'].iloc[-1]:.2f}")
        else:
            log("❌ get_klines returned None")
    except Exception as e:
        log(f"❌ get_klines: {e}")

    # 5. Full run() with 2 pairs
    try:
        from scanner import run as run_scanner
        result = run_scanner(account_usd=1000, risk_pct=1.0, scan_pairs=["BTCUSDT","ETHUSDT"])
        sigs = result.get("all_signals", [])
        ov   = result.get("market_overview", [])
        log(f"✅ run(): all_signals={len(sigs)}, market_overview={len(ov)}")
        for s in sigs:
            log(f"   {s['symbol']}: score={s['score']}")
    except Exception as e:
        log(f"❌ run(): {e}")

    # 6. trade_card import
    try:
        from trade_card import generate, format_telegram
        log("✅ trade_card imported")
    except Exception as e:
        log(f"❌ trade_card: {e}")

    report = "🔬 *DIAGNOSTIC REPORT*\n\n" + "\n".join(lines)
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)

async def cmd_validate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Validate a specific pair against big players: /validate SOLUSDT"""
    if not await auth_check(update): return
    args = context.args
    sym  = args[0].upper() if args else "BTCUSDT"
    if not sym.endswith("USDT"):
        sym += "USDT"
    msg = await update.message.reply_text(f"🧠 Validating {sym} with big players...")
    try:
        from big_player import validate, format_summary
        result  = validate(sym)
        summary = format_summary(result)
        await msg.edit_text(f"```\n{summary}\n```", parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"Error: {e}")

async def cmd_kronos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ML candle forecast: /kronos BTCUSDT"""
    if not await auth_check(update): return
    args = context.args
    sym  = args[0].upper() if args else "BTCUSDT"
    if not sym.endswith("USDT"):
        sym += "USDT"
    msg = await update.message.reply_text(f"🤖 Running Kronos-mini ML forecast for {sym}... (~10s)")
    try:
        from kronos_signal import get_kronos_signal, format_kronos_card
        sig  = get_kronos_signal(sym)
        card = format_kronos_card(sig)
        await msg.edit_text(card, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await msg.edit_text(f"Kronos error: {e}")

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await auth_check(update): return
    from xml.etree import ElementTree
    import urllib.request
    headlines = []
    for url in ["https://cointelegraph.com/rss", "https://feeds.bbci.co.uk/news/business/rss.xml"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            txt = urllib.request.urlopen(req, timeout=8).read().decode("utf-8","ignore")
            root = ElementTree.fromstring(txt)
            for item in root.findall(".//item")[:4]:
                t = item.find("title")
                if t is not None and t.text:
                    headlines.append(f"• {t.text.strip()[:80]}")
        except:
            pass
    msg = "📰 *LATEST CRYPTO & MACRO NEWS*\n\n" + "\n".join(headlines[:8])
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    import time

    print(f"⚡ Trading Bot starting...")
    print(f"Token: {'SET ✅' if TOKEN else 'MISSING ❌'}")
    print(f"Chat ID: {'SET ✅' if CHAT_ID else 'Not set (all users accepted)'}")

    # Clear any existing webhook/polling sessions to avoid conflict
    import urllib.request, urllib.parse
    try:
        data = urllib.parse.urlencode({"drop_pending_updates": "true"}).encode()
        req  = urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/deleteWebhook",
            data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        urllib.request.urlopen(req, timeout=10)
        print("Webhook cleared ✅")
        time.sleep(3)  # Wait for any other instance to die
    except Exception as e:
        print(f"Webhook clear skipped: {e}")

    print("Bot is running. Send /start from Telegram to test.")
    print("Press Ctrl+C to stop.\n")

    app = Application.builder().token(TOKEN).build()

    # Register commands
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("daily",    cmd_daily))
    app.add_handler(CommandHandler("plan",     cmd_plan))
    app.add_handler(CommandHandler("scan",    cmd_scan))
    app.add_handler(CommandHandler("whale",   cmd_whale))
    app.add_handler(CommandHandler("options", cmd_options))
    app.add_handler(CommandHandler("hype",    cmd_hype))
    app.add_handler(CommandHandler("btc",     cmd_btc))
    app.add_handler(CommandHandler("eth",     cmd_btc))  # same handler
    app.add_handler(CommandHandler("time",    cmd_time))
    app.add_handler(CommandHandler("size",    cmd_size))
    app.add_handler(CommandHandler("macro",   cmd_macro))
    app.add_handler(CommandHandler("pairs",   cmd_pairs))
    app.add_handler(CommandHandler("news",     cmd_news))
    app.add_handler(CommandHandler("validate", cmd_validate))
    app.add_handler(CommandHandler("kronos",   cmd_kronos))
    app.add_handler(CommandHandler("debug",    cmd_debug))

    # Natural language
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
