import os
import logging
import requests
import re
from bs4 import BeautifulSoup
from datetime import datetime
import pytz

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from anthropic import Anthropic

# ======================
# INIT
# ======================
print("bot running")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

log = logging.getLogger(__name__)

# ======================
# ENV SAFE LOAD
# ======================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
CHAT_ID = os.environ.get("CHAT_ID")

BD_TZ = pytz.timezone("Asia/Dhaka")

claude = None
if ANTHROPIC_API_KEY:
    claude = Anthropic(api_key=ANTHROPIC_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36"
}

# ======================
# FETCH STOCKS (YOUR ORIGINAL LOGIC)
# ======================
def fetch_stocks():
    try:
        r = requests.get(
            "https://www.dsebd.org/latest_share_price_scroll_by_value.php",
            headers=HEADERS,
            timeout=20
        )

        soup = BeautifulSoup(r.text, "html.parser")
        stocks = []

        for row in soup.select("table tr")[1:]:
            cols = row.find_all("td")

            if len(cols) >= 8:
                try:
                    sym = cols[1].get_text(strip=True)
                    ltp = cols[2].get_text(strip=True).replace(",", "")
                    hi = cols[4].get_text(strip=True).replace(",", "")
                    lo = cols[5].get_text(strip=True).replace(",", "")
                    chg = cols[7].get_text(strip=True).replace(",", "").replace("%", "")
                    vol = cols[9].get_text(strip=True).replace(",", "") if len(cols) > 9 else "0"

                    if sym and len(sym) > 1 and sym not in ("SYMBOL", "Symbol", "CODE"):
                        stocks.append({
                            "symbol": sym.upper(),
                            "ltp": float(ltp) if ltp else 0,
                            "high": float(hi) if hi else 0,
                            "low": float(lo) if lo else 0,
                            "change": float(chg) if chg else 0,
                            "volume": int(float(vol)) if vol else 0
                        })

                except:
                    continue

        log.info(f"{len(stocks)} stocks fetched")
        return stocks

    except Exception as e:
        log.error(f"Fetch error: {e}")
        return []

# ======================
# ANALYSIS (UNCHANGED LOGIC)
# ======================
def analyze(stocks):
    scored = []

    for s in stocks:
        if s["ltp"] <= 0:
            continue

        ltp, hi, lo = s["ltp"], s["high"], s["low"]
        rng = hi - lo if hi > lo else 1

        bp = (ltp - lo) / rng
        uw = (hi - ltp) / rng
        lw = (ltp - lo) / rng

        score = 0
        tags = []

        if bp > 0.65:
            score += 2
            tags.append("Strong Close ✅")
        elif bp < 0.35:
            score -= 2
            tags.append("Weak Close ❌")

        if lw > 0.4 and uw < 0.2:
            score += 3
            tags.append("Hammer 🔨")
        elif uw > 0.4 and lw < 0.2:
            score -= 3
            tags.append("Shooting Star ⭐")

        if s["change"] > 4:
            score += 3
            tags.append(f"+{s['change']:.1f}% 🚀")
        elif s["change"] > 2:
            score += 2
            tags.append(f"+{s['change']:.1f}%")
        elif s["change"] > 0:
            score += 1
            tags.append(f"+{s['change']:.1f}%")
        elif s["change"] < -4:
            score -= 3
            tags.append(f"{s['change']:.1f}% 💥")
        elif s["change"] < -2:
            score -= 2
            tags.append(f"{s['change']:.1f}%")
        elif s["change"] < 0:
            score -= 1
            tags.append(f"{s['change']:.1f}%")

        if s["volume"] > 1000000:
            score += 3
            tags.append("Vol 🔥🔥")
        elif s["volume"] > 300000:
            score += 2
            tags.append("Vol High 🔥")
        elif s["volume"] > 100000:
            score += 1
            tags.append("Vol OK")

        f618 = lo + rng * 0.618
        f382 = lo + rng * 0.382

        if abs(ltp - f618) / ltp < 0.02:
            score += 2
            tags.append("Fib 0.618 ✨")
        elif abs(ltp - f382) / ltp < 0.02:
            score += 1
            tags.append("Fib 0.382")

        if score >= 6:
            signal = "STRONG BUY 🟢🟢"
        elif score >= 3:
            signal = "BUY 🟢"
        elif score <= -6:
            signal = "STRONG SELL 🔴🔴"
        elif score <= -3:
            signal = "SELL 🔴"
        else:
            signal = "HOLD 🟡"

        s.update({
            "score": score,
            "signal": signal,
            "tags": tags,
            "entry": round(ltp, 2),
            "sl": round(lo * 0.99, 2),
            "tp1": round(ltp * 1.08, 2),
            "tp2": round(ltp * 1.15, 2)
        })

        scored.append(s)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored

# ======================
# EW (UNCHANGED)
# ======================
def find_ew(stocks):
    out = []

    for s in stocks:
        if s["ltp"] <= 0 or s["high"] <= s["low"]:
            continue

        rng = s["high"] - s["low"]
        f618 = s["low"] + rng * 0.618
        f382 = s["low"] + rng * 0.382

        bp = (s["ltp"] - s["low"]) / rng
        n618 = abs(s["ltp"] - f618) / s["ltp"] < 0.025
        n382 = abs(s["ltp"] - f382) / s["ltp"] < 0.025

        if (n618 or n382) and bp > 0.55 and s["change"] > 0 and s["volume"] > 50000:
            s["ew_note"] = f"Fib {'0.618' if n618 else '0.382'} bounce — Wave 3/5 শুরু হতে পারে 🌊"
            out.append(s)

    return out[:8]

# ======================
# DSEX
# ======================
def get_dsex():
    try:
        r = requests.get("https://www.dsebd.org", headers=HEADERS, timeout=10)
        m = re.search(r"DSEX[^0-9]*([0-9,]+\.?[0-9]*)", r.text)
        return m.group(1) if m else "N/A"
    except:
        return "N/A"

# ======================
# AI (SAFE)
# ======================
def ai_summary(buys, ew_stocks, dsex):
    if not claude:
        return "AI off (no API key)"

    try:
        today = datetime.now(BD_TZ).strftime("%d %B %Y")

        lines = ""
        for s in buys[:5]:
            lines += f"{s['symbol']}:৳{s['ltp']}({s['change']:+.1f}%) Score:{s['score']}\n"

        ew_lines = "\n".join([f"{s['symbol']}:{s.get('ew_note','')}" for s in ew_stocks[:4]])

        resp = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{
                "role": "user",
                "content": f"তারিখ:{today} DSEX:{dsex}\nTop BUY:\n{lines}\nEW:\n{ew_lines}\n\nবাংলায় বিশ্লেষণ দাও।"
            }]
        )

        return resp.content[0].text

    except Exception as e:
        return f"AI error: {e}"

# ======================
# MESSAGE BUILDER
# ======================
def build_msg(scored, dsex):
    today = datetime.now(BD_TZ).strftime("%d %b %Y %I:%M %p")

    buys = [s for s in scored if "BUY" in s["signal"]][:8]
    sells = [s for s in scored if "SELL" in s["signal"]][:4]
    ew_list = find_ew(scored)

    ai = ai_summary(buys, ew_list, dsex)

    msg = f"🏦 *DSE Signal Bot*\n📅 {today}\n📊 DSEX: `{dsex}`\n{'━'*22}\n\n"

    if buys:
        msg += f"🟢 BUY ({len(buys)})\n\n"
        for s in buys:
            msg += f"*{s['symbol']}* — {s['signal']}\n💰 {s['ltp']} ({s['change']:+.1f}%)\n🏷 {' · '.join(s['tags'][:3])}\n\n"

    if sells:
        msg += "🔴 SELL\n"
        for s in sells:
            msg += f"*{s['symbol']}* {s['ltp']} ({s['change']:+.1f}%)\n"

    if ew_list:
        msg += "\n🌊 EW\n"
        for s in ew_list:
            msg += f"*{s['symbol']}* — {s.get('ew_note','')}\n"

    msg += f"\n{'━'*22}\n🤖 AI:\n{ai}\n\n⚠️ Risk warning"

    return msg

# ======================
# TELEGRAM COMMANDS
# ======================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🏦 DSE Bot Active\n/signal /ew")

async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Analyzing...")
    stocks = fetch_stocks()
    scored = analyze(stocks)
    dsex = get_dsex()
    msg = build_msg(scored, dsex)

    for i in range(0, len(msg), 3500):
        await update.message.reply_text(msg[i:i+3500], parse_mode="Markdown")

async def cmd_ew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌊 EW scanning...")

    stocks = fetch_stocks()
    scored = analyze(stocks)
    ew_list = find_ew(scored)

    if not ew_list:
        await update.message.reply_text("No EW setup today")
        return

    msg = "🌊 EW Candidates\n\n"
    for s in ew_list:
        msg += f"*{s['symbol']}* {s['ltp']} ({s['change']:+.1f}%)\n{s.get('ew_note','')}\n\n"

    await update.message.reply_text(msg, parse_mode="Markdown")

# ======================
# MAIN
# ======================
def main():
    if not TELEGRAM_TOKEN:
        print("Missing TELEGRAM_TOKEN")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("ew", cmd_ew))

    print("Bot started...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
