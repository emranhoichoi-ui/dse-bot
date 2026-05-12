import os, logging, requests, re
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from anthropic import Anthropic

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN    = os.environ['TELEGRAM_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
CHAT_ID           = os.environ['CHAT_ID']
BD_TZ             = pytz.timezone('Asia/Dhaka')
claude            = Anthropic(api_key=ANTHROPIC_API_KEY)

HEADERS = {'User-Agent': 'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36'}

def fetch_stocks():
    urls = [
        "https://www.dsebd.org/latest_share_price_scroll_by_value.php",
        "https://www.dsebd.org/latest_share_price_scroll_l.php"
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            soup = BeautifulSoup(r.text, 'html.parser')
            stocks = []
            for row in soup.select('table tr')[1:]:
                cols = row.find_all('td')
                if len(cols) >= 8:
                    try:
                        sym = cols[1].get_text(strip=True)
                        ltp = cols[2].get_text(strip=True).replace(',','')
                        hi  = cols[4].get_text(strip=True).replace(',','')
                        lo  = cols[5].get_text(strip=True).replace(',','')
                        chg = cols[7].get_text(strip=True).replace(',','').replace('%','')
                        vol = cols[9].get_text(strip=True).replace(',','') if len(cols)>9 else '0'
                        if sym and len(sym)>1 and sym not in ('SYMBOL','Symbol'):
                            stocks.append({
                                'symbol': sym.upper(),
                                'ltp':    float(ltp) if ltp else 0,
                                'high':   float(hi)  if hi  else 0,
                                'low':    float(lo)  if lo  else 0,
                                'change': float(chg) if chg else 0,
                                'volume': int(float(vol)) if vol else 0,
                            })
                    except: continue
            if stocks:
                log.info(f"{len(stocks)} স্টক পাওয়া গেছে")
                return stocks
        except Exception as e:
            log.error(f"Fetch error: {e}")
    return []

def analyze(stocks):
    scored = []
    for s in stocks:
        if s['ltp'] <= 0: continue
        ltp, hi, lo = s['ltp'], s['high'], s['low']
        rng = hi - lo if hi > lo else 1
        body_pos   = (ltp - lo) / rng
        upper_wick = (hi - ltp) / rng
        lower_wick = (ltp - lo) / rng
        score, tags = 0, []

        if body_pos > 0.65: score += 2; tags.append("Strong Close ✅")
        elif body_pos < 0.35: score -= 2; tags.append("Weak Close ❌")

        if lower_wick > 0.4 and upper_wick < 0.2: score += 3; tags.append("Hammer 🔨")
        elif upper_wick > 0.4 and lower_wick < 0.2: score -= 3; tags.append("Shooting Star ⭐")

        if s['change'] > 4:   score += 3; tags.append(f"+{s['change']:.1f}% 🚀")
        elif s['change'] > 2: score += 2; tags.append(f"+{s['change']:.1f}%")
        elif s['change'] > 0: score += 1; tags.append(f"+{s['change']:.1f}%")
        elif s['change'] < -4: score -= 3; tags.append(f"{s['change']:.1f}% 💥")
        elif s['change'] < -2: score -= 2; tags.append(f"{s['change']:.1f}%")
        elif s['change'] < 0:  score -= 1; tags.append(f"{s['change']:.1f}%")

        if s['volume'] > 1000000: score += 3; tags.append("Vol 🔥🔥")
        elif s['volume'] > 300000: score += 2; tags.append("Vol High 🔥")
        elif s['volume'] > 100000: score += 1; tags.append("Vol OK")

        fib618 = lo + rng * 0.618
        fib382 = lo + rng * 0.382
        if abs(ltp - fib618)/ltp < 0.02: score += 2; tags.append("Fib 0.618 ✨")
        elif abs(ltp - fib382)/ltp < 0.02: score += 1; tags.append("Fib 0.382")

        if score >= 6:   signal = "STRONG BUY 🟢🟢"
        elif score >= 3: signal = "BUY 🟢"
        elif score <= -6: signal = "STRONG SELL 🔴🔴"
        elif score <= -3: signal = "SELL 🔴"
        else:            signal = "HOLD 🟡"

        s.update({
            'score': score, 'signal': signal, 'tags': tags,
            'entry': round(ltp, 2),
            'sl':    round(lo * 0.99, 2),
            'tp1':   round(ltp * 1.08, 2),
            'tp2':   round(ltp * 1.15, 2),
        })
        scored.append(s)

    scored.sort(key=lambda x: x['score'], reverse=True)
    return scored

def find_ew(stocks):
    out = []
    for s in stocks:
        if s['ltp'] <= 0 or s['high'] <= s['low']: continue
        rng = s['high'] - s['low']
        fib618 = s['low'] + rng * 0.618
        fib382 = s['low'] + rng * 0.382
        bp = (s['ltp'] - s['low']) / rng
        near618 = abs(s['ltp'] - fib618) / s['ltp'] < 0.025
        near382 = abs(s['ltp'] - fib382) / s['ltp'] < 0.025
        if (near618 or near382) and bp > 0.55 and s['change'] > 0 and s['volume'] > 50000:
            s['ew_note'] = f"Fib {'0.618' if near618 else '0.382'} bounce — Wave 3/5 শুরু হতে পারে 🌊"
            out.append(s)
    return out[:8]

def ai_summary(buy_stocks, ew_stocks, dsex):
    today = datetime.now(BD_TZ).strftime("%d %B %Y")
    lines = ""
    for s in buy_stocks[:5]:
        lines += f"{s['symbol']}: ৳{s['ltp']} ({s['change']:+.1f}%) Score:{s['score']} Tags:{','.join(s['tags'][:2])}\n"
    ew_lines = "\n".join([f"{s['symbol']}: {s.get('ew_note','')}" for s in ew_stocks[:4]])
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=800,
            messages=[{"role":"user","content":
                f"তারিখ: {today}, DSEX: {dsex}\nTop BUY:\n{lines}\nEW Candidates:\n{ew_lines}\n\n"
                f"প্রতিটি স্টকের ১ লাইনে কেন BUY বা সাবধান — বাংলায় সহজ ভাষায়। শেষে ২ লাইনে মার্কেট সামগ্রিক অবস্থা।"
            }]
        )
        return resp.content[0].text
    except Exception as e:
        return f"AI বিশ্লেষণ পাওয়া যায়নি: {e}"

def get_dsex():
    try:
        r = requests.get("https://www.dsebd.org", headers=HEADERS, timeout=10)
        m = re.search(r'DSEX[^0-9]*([0-9,]+\.?[0-9]*)', r.text)
        return m.group(1) if m else "N/A"
    except: return "N/A"

def build_msg(scored, dsex):
    today   = datetime.now(BD_TZ).strftime("%d %b %Y %I:%M %p")
    buys    = [s for s in scored if 'BUY'  in s['signal']][:8]
    sells   = [s for s in scored if 'SELL' in s['signal']][:4]
    ew_list = find_ew(scored)
    ai      = ai_summary(buys, ew_list, dsex)

    msg = f"🏦 *DSE Signal Bot — দৈনিক রিপোর্ট*\n📅 {today}\n📊 DSEX: `{dsex}`\n{'━'*22}\n\n"

    if buys:
        msg += f"🟢 *BUY সিগনাল — {len(buys)} টি স্টক*\n\n"
        for s in buys:
            msg += (f"*{s['symbol']}* — {s['signal']}\n"
                    f"💰 `৳{s['ltp']}` ({s['change']:+.1f}%)\n"
                    f"📥 Entry: `৳{s['entry']}` | SL: `৳{s['sl']}`\n"
                    f"🎯 TP1: `৳{s['tp1']}` | TP2: `৳{s['tp2']}`\n"
                    f"🏷 {' · '.join(s['tags'][:3])}\n\n")

    if sells:
        msg += f"🔴 *SELL / সতর্কতা — {len(sells)} টি*\n"
        for s in sells:
            msg += f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) — {s['signal']}\n"
        msg += "\n"

    if ew_list:
        msg += "🌊 *Elliott Wave 2/4 Bounce — Strong BUY সুযোগ*\n"
        for s in ew_list:
            msg += f"*{s['symbol']}* `৳{s['ltp']}` — {s.get('ew_note','')}\n"
        msg += "\n"

    msg += f"{'━'*22}\n🤖 *AI বিশ্লেষণ*\n{ai}\n\n"
    msg += "⚠️ _বিনিয়োগে ঝুঁকি আছে। নিজের বিবেচনায় সিদ্ধান্ত নিন।_"
    return msg

async def send_signals(bot):
    log.info("Signal job শুরু...")
    await bot.send_message(chat_id=CHAT_ID, text="⏳ DSE ডেটা সংগ্রহ হচ্ছে...")
    try:
        stocks = fetch_stocks()
        if not stocks:
            await bot.send_message(chat_id=CHAT_ID, text="❌ ডেটা পাওয়া যায়নি।"); return
        dsex   = get_dsex()
        scored = analyze(stocks)
        msg    = build_msg(scored, dsex)
        for i in range(0, len(msg), 4000):
            await bot.send_message(chat_id=CHAT_ID, text=msg[i:i+4000], parse_mode='Markdown')
        log.info("✅ Signal পাঠানো হয়েছে")
    except Exception as e:
        await bot.send_message(chat_id=CHAT_ID, text=f"❌ সমস্যা: {e}")

async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        f"🏦 *DSE Signal Bot*\n\nআপনার Chat ID: `{u.effective_chat.id}`\n\n"
        "/signal — এখনই সিগনাল\n/ew — EW Wave 2/4 স্টক\n/top — Top Gainers\n"
        "\n🕕 প্রতিদিন সন্ধ্যা ৬টায় স্বয়ংক্রিয় সিগনাল আসবে।",
        parse_mode='Markdown')

async def cmd_signal(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("⏳ বিশ্লেষণ চলছে, একটু অপেক্ষা করুন...")
    await send_signals(ctx.bot)

async def cmd_ew(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🌊 EW স্ক্যান চলছে...")
    stocks  = fetch_stocks()
    scored  = analyze(stocks)
    ew_list = find_ew(scored)
    if not ew_list:
        await u.message.reply_text("আজ কোনো EW candidate পাওয়া যায়নি।"); return
    msg = "🌊 *Elliott Wave 2/4 Bounce স্টক*\n\n"
    for s in ew_list:
        msg += (f"*{s['symbol']}* — `৳{s['ltp']}` ({s['change']:+.1f}%)\n"
                f"Entry: `৳{s['entry']}` | SL: `৳{s['sl']}` | TP: `৳{s['tp1']}`\n"
                f"_{s.get('ew_note','')}_\n\n")
    msg += "⚠️ _Stop Loss সবসময় ব্যবহার করুন।_"
    await u.message.reply_text(msg, parse_mode='Markdown')

async def cmd_top(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("📊 লোড হচ্ছে...")
    stocks = fetch_stocks()
    if not stocks:
        await u.message.reply_text("ডেটা পাওয়া যায়নি।"); return
    top = sorted(stocks, key=lambda x: x.get('change',0), reverse=True)[:10]
    msg = "🔥 *আজকের Top 10 Gainers*\n\n"
    for i,s in enumerate(top,1):
        msg += f"{i}. *{s['symbol']}* `৳{s['ltp']}` (+{s['change']:.1f}%) Vol:{s['volume']:,}\n"
    await u.message.reply_text(msg, parse_mode='Markdown')

async def post_init(app):
    sched = AsyncIOScheduler(timezone=BD_TZ)
    sched.add_job(send_signals, 'cron', hour=18, minute=0, args=[app.bot])
    sched.start()
    log.info("✅ Scheduler চালু — প্রতিদিন ৬:০০ PM সিগনাল যাবে")

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("ew",     cmd_ew))
    app.add_handler(CommandHandler("top",    cmd_top))
    log.info("🚀 Bot চালু হচ্ছে...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
