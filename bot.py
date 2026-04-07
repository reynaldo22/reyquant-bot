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
    Full daily trade cards — scans ALL pairs, validates with big player,
    returns complete executable trade cards with entry/TP/SL/hold/skip.
    """
    if not await auth_check(update): return
    msg = await update.message.reply_text("⚡ Running full daily scan + big player validation...\n(takes ~60s)")

    try:
        from scanner    import run as run_scanner, get_top_pairs
        from big_player import validate as bp_validate, format_summary as bp_fmt
        from trade_card import generate as gen_card, format_telegram as fmt_card
        from scanner    import get_fear_greed, get_economic_calendar

        await msg.edit_text("⏳ Step 1/4 — Scanning all Binance pairs...")

        # Get top 20 pairs by volume
        top_pairs = get_top_pairs(20)
        symbols   = [p["symbol"] for p in top_pairs]

        # Run full scanner
        result    = run_scanner(account_usd=1000, risk_pct=1.0, scan_pairs=symbols)
        fg        = result.get("fear_greed", {})
        macro     = result.get("macro_risk", {})
        calendar  = result.get("calendar", [])

        # Collect top longs and shorts (score >= 3)
        all_sigs  = result.get("all_signals", [])
        top_longs  = [s for s in all_sigs if s["score"] >= 3][:4]
        top_shorts = [s for s in all_sigs if s["score"] <= -3][:2]
        candidates = top_longs + top_shorts

        if not candidates:
            await msg.edit_text(
                f"⚠️ *NO SETUPS TODAY*\n\n"
                f"F\\&G: {fg.get('value','?')} — {fg.get('label','')}\n"
                f"Macro: {macro.get('level','?')}\n\n"
                f"{''.join(chr(10)+'⛔ '+w for w in macro.get('warnings',[]))}\n\n"
                f"Wait for macro events to pass, then `/daily` again.",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        # Header message
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        fg_val  = fg.get("value", "?")
        fg_lbl  = fg.get("label", "")
        macro_lvl = macro.get("level", "LOW")
        macro_icon = {"LOW":"✅","MEDIUM":"🟡","HIGH":"⚠️","EXTREME":"🚨"}.get(macro_lvl,"⚠️")

        header = (
            f"⚡ *DAILY TRADE CARDS — {now_str}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 F\\&G: *{fg_val}* — {fg_lbl}\n"
            f"{macro_icon} Macro: *{macro_lvl}*"
        )
        if macro.get("warnings"):
            for w in macro["warnings"][:2]:
                header += f"\n   {w}"
        header += f"\n\n*{len(candidates)} setup{'s' if len(candidates)>1 else ''} found — validating with big players...*"

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=header, parse_mode=ParseMode.MARKDOWN
        )

        # Generate trade card for each candidate
        for i, sig in enumerate(candidates):
            sym = sig["symbol"]
            await msg.edit_text(f"⏳ Step 2/4 — Validating {sym} with big players... ({i+1}/{len(candidates)})")

            # Big player validation
            try:
                bp_result  = bp_validate(sym)
                bp_summary = bp_fmt(bp_result)
                whale_entry = bp_result.get("whale_oi", {}).get("estimated_entry_price")
                oi_trend    = bp_result.get("whale_oi", {}).get("oi_trend", "FLAT ⚪")
                bp_consensus= bp_result.get("score", 0)
            except Exception as e:
                bp_summary = f"Big player data unavailable: {e}"
                whale_entry = None
                oi_trend    = sig.get("indicators", {}).get("trend", "FLAT")
                bp_consensus= 0

            # Generate full trade card
            ind = sig.get("indicators", {})
            card = gen_card(
                symbol    = sym,
                direction = "LONG" if sig["score"] > 0 else "SHORT",
                entry     = ind.get("price", sig["price"]),
                atr       = ind.get("atr",   sig["atr"]),
                score     = sig["score"] + bp_consensus,
                rsi       = ind.get("rsi", 50),
                macd_cross= ind.get("macd", {}).get("cross") if ind else sig.get("macd_4h", {}).get("cross"),
                funding   = sig.get("funding", 0) / 100,
                oi_trend  = oi_trend,
                account   = 1000,
                risk_pct  = 1.0,
                whale_entry = whale_entry,
                max_pain  = None,
                big_player_summary = bp_summary,
                macro_risk  = macro_lvl,
                macro_warning = macro.get("warnings", [""])[0] if macro.get("warnings") else "",
            )

            # Send trade card
            card_text = fmt_card(card)
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=card_text, parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                # Fallback: send without markdown if formatting fails
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=card_text.replace("*","").replace("`","").replace("_","")
                )
            time.sleep(0.5)

        # Footer — skip conditions reminder
        footer = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📏 *UNIVERSAL SKIP CONDITIONS*\n"
            f"   VIX \\> 35  \\|  Funding \\> 0\\.1%\n"
            f"   Event \\< 2h  \\|  BTC crash \\-5%\n"
            f"   Top traders \\> 65% opposite\n\n"
            f"After TP1: move stop to breakeven\n"
            f"Max 3 open positions simultaneously\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=footer, parse_mode=ParseMode.MARKDOWN
        )
        await msg.delete()

    except Exception as e:
        await msg.edit_text(f"Error in daily scan: {e}\n\nTry /scan or /plan instead.")

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

    # Natural language
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
