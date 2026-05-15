import os,logging,requests,re
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
from telegram import Update
from telegram.ext import Application,CommandHandler,ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',level=logging.INFO)
log=logging.getLogger(__name__)

TELEGRAM_TOKEN=os.environ['TELEGRAM_TOKEN']
ANTHROPIC_API_KEY=os.environ['ANTHROPIC_API_KEY']
CHAT_ID=os.environ['CHAT_ID']
BD_TZ=pytz.timezone('Asia/Dhaka')
HEADERS={'User-Agent':'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120'}

MIN_VOLUME=10000      # কমপক্ষে ১০,০০০ লট
MIN_PRICE=5.0         # কমপক্ষে ৳৫
MAX_CHANGE=30.0       # ৩০% এর বেশি change = circuit breaker, বাদ

def fetch_stocks():
    """dsebd.org থেকে সব স্টকের ডেটা আনে এবং filter করে"""
    urls=[
        "https://www.dsebd.org/latest_share_price_scroll_by_value.php",
        "https://www.dsebd.org/latest_share_price_scroll_l.php",
    ]
    for url in urls:
        try:
            r=requests.get(url,headers=HEADERS,timeout=25)
            r.raise_for_status()
            soup=BeautifulSoup(r.text,'html.parser')
            stocks=[]
            rows=soup.select('tr.alt,tr')
            for row in rows:
                cols=row.find_all('td')
                if len(cols)<10:continue
                try:
                    sym=cols[1].get_text(strip=True)
                    if not sym or len(sym)<2 or sym in('SYMBOL','Symbol','CODE','Name'):continue
                    ltp  =float(cols[2].get_text(strip=True).replace(',','') or 0)
                    hi   =float(cols[4].get_text(strip=True).replace(',','') or 0)
                    lo   =float(cols[5].get_text(strip=True).replace(',','') or 0)
                    yday =float(cols[6].get_text(strip=True).replace(',','') or 0)
                    chg  =float(cols[7].get_text(strip=True).replace(',','').replace('%','') or 0)
                    trade=float(cols[8].get_text(strip=True).replace(',','') or 0)
                    vol  =float(cols[9].get_text(strip=True).replace(',','') or 0)
                    val  =float(cols[10].get_text(strip=True).replace(',','') or 0) if len(cols)>10 else 0

                    # ── Filters ──
                    if ltp < MIN_PRICE: continue           # দাম খুব কম
                    if vol < MIN_VOLUME: continue          # volume খুব কম
                    if abs(chg) > MAX_CHANGE: continue     # circuit breaker
                    if hi<=0 or lo<=0: continue
                    if hi<lo: continue

                    stocks.append({
                        'symbol':sym.upper(),
                        'ltp':round(ltp,2),
                        'high':round(hi,2),
                        'low':round(lo,2),
                        'yday':round(yday,2),
                        'change':round(chg,2),
                        'volume':int(vol),
                        'value':round(val,2),
                        'trades':int(trade),
                    })
                except:continue

            if len(stocks)>10:
                log.info(f"✅ {len(stocks)} valid stocks fetched from {url}")
                return stocks
        except Exception as e:
            log.error(f"Fetch error {url}: {e}")
    return[]

def get_dsex():
    """DSEX সূচক সঠিকভাবে আনে"""
    try:
        r=requests.get("https://www.dsebd.org",headers=HEADERS,timeout=10)
        soup=BeautifulSoup(r.text,'html.parser')
        # DSEX value বিভিন্ন জায়গায় থাকতে পারে
        for pattern in[
            r'DSEX\D{0,20}?(\d{3,6}\.?\d{0,2})',
            r'(\d{4,6}\.\d{2})',
        ]:
            m=re.search(pattern,r.text)
            if m:
                val=float(m.group(1).replace(',',''))
                if 3000<val<10000:  # reasonable DSEX range
                    return f"{val:,.2f}"
        return "N/A"
    except Exception as e:
        log.error(f"DSEX error:{e}")
        return "N/A"

def analyze(stocks):
    """Technical analysis — SMC, Candle, Volume, Fibonacci"""
    scored=[]
    for s in stocks:
        ltp,hi,lo=s['ltp'],s['high'],s['low']
        rng=hi-lo if hi>lo else 1
        bp=(ltp-lo)/rng      # body position (0=low end, 1=high end)
        uw=(hi-ltp)/rng      # upper wick ratio
        lw=(ltp-lo)/rng      # lower wick ratio
        score,tags=0,[]

        # ── Candle Pattern ──
        if bp>0.7:
            score+=2;tags.append("Strong Close ✅")
        elif bp<0.3:
            score-=2;tags.append("Weak Close ❌")

        if lw>0.45 and uw<0.15:
            score+=3;tags.append("Hammer 🔨")
        elif uw>0.45 and lw<0.15:
            score-=3;tags.append("Shooting Star 💫")
        elif lw>0.3 and uw>0.3 and bp>0.4 and bp<0.6:
            score+=1;tags.append("Doji")

        # ── Price Change ──
        chg=s['change']
        if chg>5:   score+=3;tags.append(f"+{chg:.1f}% 🚀")
        elif chg>3: score+=2;tags.append(f"+{chg:.1f}%")
        elif chg>1: score+=1;tags.append(f"+{chg:.1f}%")
        elif chg<-5:score-=3;tags.append(f"{chg:.1f}% 💥")
        elif chg<-3:score-=2;tags.append(f"{chg:.1f}%")
        elif chg<-1:score-=1;tags.append(f"{chg:.1f}%")

        # ── Volume Score ──
        vol=s['volume']
        if vol>500000:  score+=3;tags.append("Vol 🔥🔥🔥")
        elif vol>200000:score+=2;tags.append("Vol 🔥🔥")
        elif vol>50000: score+=1;tags.append("Vol 🔥")

        # ── Fibonacci Levels ──
        f618=lo+rng*0.618
        f382=lo+rng*0.382
        f500=lo+rng*0.500
        if abs(ltp-f618)/ltp<0.015:
            score+=2;tags.append("Fib 0.618 ✨")
        elif abs(ltp-f382)/ltp<0.015:
            score+=1;tags.append("Fib 0.382")
        elif abs(ltp-f500)/ltp<0.015:
            score+=1;tags.append("Fib 0.500")

        # ── Near Yesterday Close (Gap) ──
        if s['yday']>0:
            gap=(ltp-s['yday'])/s['yday']*100
            if gap>2 and chg>0:
                score+=1;tags.append("Gap Up ⬆️")
            elif gap<-2 and chg<0:
                score-=1;tags.append("Gap Down ⬇️")

        # ── Signal Decision ──
        if score>=7:   signal="STRONG BUY 🟢🟢"
        elif score>=4: signal="BUY 🟢"
        elif score<=-7:signal="STRONG SELL 🔴🔴"
        elif score<=-4:signal="SELL 🔴"
        else:          signal="HOLD 🟡"

        # ── Risk Management ──
        risk_pct=0.05 if 'STRONG' in signal else 0.04
        s.update({
            'score':score,
            'signal':signal,
            'tags':tags,
            'entry':round(ltp,2),
            'sl':round(lo*0.995,2),
            'tp1':round(ltp*(1+risk_pct*2),2),
            'tp2':round(ltp*(1+risk_pct*3.5),2),
            'rr':'1:2 / 1:3.5',
        })
        scored.append(s)

    scored.sort(key=lambda x:x['score'],reverse=True)
    return scored

def find_ew(stocks):
    """Elliott Wave 2/4 bounce candidates"""
    out=[]
    for s in stocks:
        ltp,hi,lo=s['ltp'],s['high'],s['low']
        if hi<=lo:continue
        rng=hi-lo
        f618=lo+rng*0.618
        f382=lo+rng*0.382
        bp=(ltp-lo)/rng
        n618=abs(ltp-f618)/ltp<0.02
        n382=abs(ltp-f382)/ltp<0.02
        # Bounce condition: near fib, closing upper half, positive change, good volume
        if(n618 or n382) and bp>0.6 and s['change']>0 and s['volume']>20000:
            fib='0.618' if n618 else '0.382'
            s['ew_note']=f"Fib {fib} এ সাপোর্ট নিয়েছে — Wave 3/5 শুরু হতে পারে 🌊"
            out.append(s)
    return out[:8]

def ai_summary(buys,ew_stocks,dsex):
    """Claude AI দিয়ে সংক্ষিপ্ত বিশ্লেষণ"""
    today=datetime.now(BD_TZ).strftime("%d %B %Y")
    lines="".join([
        f"{s['symbol']}: ৳{s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,} Score:{s['score']}\n"
        for s in buys[:6]
    ])
    ew_lines="\n".join([f"• {s['symbol']}: {s.get('ew_note','')}" for s in ew_stocks[:4]])
    try:
        client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp=client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role":"user","content":
                f"তারিখ: {today} | DSEX: {dsex}\n\n"
                f"আজকের BUY সিগনাল:\n{lines}\n"
                f"EW Bounce candidates:\n{ew_lines}\n\n"
                f"প্রতিটি BUY স্টকের জন্য ১ লাইনে কেন কিনবেন বা সাবধান থাকবেন — বাংলায় সহজ ভাষায়। "
                f"শেষে ২ লাইনে আজকের DSE মার্কেটের সামগ্রিক অবস্থা।"
            }]
        )
        return resp.content[0].text
    except Exception as e:
        log.error(f"AI error:{e}")
        return f"⚠️ AI বিশ্লেষণ পাওয়া যায়নি।"

def build_msg(scored,dsex):
    """Telegram message তৈরি করে"""
    today=datetime.now(BD_TZ).strftime("%d %b %Y %I:%M %p")
    buys =[s for s in scored if 'BUY'  in s['signal']][:8]
    sells=[s for s in scored if 'SELL' in s['signal']][:5]
    holds=[s for s in scored if 'HOLD' in s['signal'] and s['volume']>100000][:3]
    ew_list=find_ew(scored)
    ai=ai_summary(buys,ew_list,dsex)

    msg =f"🏦 *DSE Signal Bot*\n"
    msg+=f"📅 {today}\n"
    msg+=f"📊 DSEX: `{dsex}`\n"
    msg+=f"{'━'*22}\n\n"

    # BUY signals
    if buys:
        msg+=f"🟢 *BUY সিগনাল — {len(buys)} টি স্টক*\n\n"
        for s in buys:
            msg+=f"*{s['symbol']}* — {s['signal']}\n"
            msg+=f"💰 `৳{s['ltp']}` ({s['change']:+.1f}%) Vol: {s['volume']:,}\n"
            msg+=f"📥 Entry: `৳{s['entry']}` | SL: `৳{s['sl']}`\n"
            msg+=f"🎯 TP1: `৳{s['tp1']}` | TP2: `৳{s['tp2']}`\n"
            msg+=f"🏷 {' · '.join(s['tags'][:3])}\n\n"
    else:
        msg+="🟡 আজ কোনো strong BUY সিগনাল নেই\n\n"

    # SELL signals
    if sells:
        msg+=f"🔴 *SELL সতর্কতা — {len(sells)} টি*\n"
        for s in sells:
            msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+="\n"

    # EW candidates
    if ew_list:
        msg+="🌊 *Elliott Wave 2/4 Bounce — Strong BUY সুযোগ*\n"
        for s in ew_list:
            msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%)\n"
            msg+=f"_{s.get('ew_note','')}_\n"
            msg+=f"Entry:`৳{s['entry']}` SL:`৳{s['sl']}` TP:`৳{s['tp1']}`\n\n"

    msg+=f"{'━'*22}\n"
    msg+=f"🤖 *AI বিশ্লেষণ*\n{ai}\n\n"
    msg+="⚠️ _বিনিয়োগে ঝুঁকি আছে। Stop Loss সবসময় ব্যবহার করুন।_"
    return msg

async def send_signals(bot):
    log.info("📊 Signal job শুরু...")
    await bot.send_message(chat_id=CHAT_ID,text="⏳ DSE ডেটা সংগ্রহ ও বিশ্লেষণ চলছে...")
    try:
        stocks=fetch_stocks()
        if not stocks:
            await bot.send_message(chat_id=CHAT_ID,text="❌ ডেটা পাওয়া যায়নি। DSE বন্ধ থাকতে পারে।")
            return
        dsex=get_dsex()
        scored=analyze(stocks)
        msg=build_msg(scored,dsex)
        # Message ৪০০০ char এ ভাগ করে পাঠাও
        for i in range(0,len(msg),4000):
            await bot.send_message(chat_id=CHAT_ID,text=msg[i:i+4000],parse_mode='Markdown')
        log.info(f"✅ Signal পাঠানো হয়েছে — {len(stocks)} stocks analyzed")
    except Exception as e:
        log.error(f"Signal error:{e}")
        await bot.send_message(chat_id=CHAT_ID,text=f"❌ সমস্যা হয়েছে:\n{e}")

async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        f"🏦 *DSE Signal Bot চালু আছে!*\n\n"
        f"Chat ID: `{u.effective_chat.id}`\n\n"
        f"📌 *Commands:*\n"
        f"/signal — এখনই সম্পূর্ণ সিগনাল\n"
        f"/ew — Elliott Wave 2/4 স্টক\n"
        f"/top — আজকের Top Gainers\n"
        f"/sell — আজকের Sell সিগনাল\n\n"
        f"🕕 প্রতিদিন সন্ধ্যা ৬:০০ টায় স্বয়ংক্রিয় সিগনাল আসবে।\n\n"
        f"⚠️ _বিনিয়োগে ঝুঁকি আছে।_",
        parse_mode='Markdown')

async def cmd_signal(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("⏳ বিশ্লেষণ চলছে, একটু অপেক্ষা করুন...")
    await send_signals(ctx.bot)

async def cmd_ew(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🌊 Elliott Wave স্ক্যান চলছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("ডেটা পাওয়া যায়নি।");return
    scored=analyze(stocks)
    ew_list=find_ew(scored)
    if not ew_list:
        await u.message.reply_text("আজ কোনো EW 2/4 candidate পাওয়া যায়নি।\nকাল আবার চেষ্টা করুন।")
        return
    msg="🌊 *Elliott Wave 2/4 Bounce — BUY সুযোগ*\n\n"
    for s in ew_list:
        msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+=f"Entry:`৳{s['entry']}` | SL:`৳{s['sl']}` | TP:`৳{s['tp1']}`\n"
        msg+=f"_{s.get('ew_note','')}_\n\n"
    msg+="⚠️ _Stop Loss সবসময় ব্যবহার করুন।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_top(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔥 Top Gainers লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("ডেটা পাওয়া যায়নি।");return
    top=sorted(stocks,key=lambda x:x['change'],reverse=True)[:10]
    msg="🔥 *আজকের Top 10 Gainers*\n_(min volume: ১০,০০০ lot)_\n\n"
    for i,s in enumerate(top,1):
        msg+=f"{i}. *{s['symbol']}* `৳{s['ltp']}` (+{s['change']:.1f}%) Vol:{s['volume']:,}\n"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_sell(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔴 Sell সিগনাল লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("ডেটা পাওয়া যায়নি।");return
    scored=analyze(stocks)
    sells=[s for s in scored if 'SELL' in s['signal']]
    if not sells:
        await u.message.reply_text("আজ কোনো SELL সিগনাল নেই।");return
    msg="🔴 *আজকের SELL সিগনাল*\n\n"
    for s in sells[:8]:
        msg+=f"*{s['symbol']}* — {s['signal']}\n"
        msg+=f"💰 `৳{s['ltp']}` ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+=f"🏷 {' · '.join(s['tags'][:3])}\n\n"
    msg+="⚠️ _বিনিয়োগে ঝুঁকি আছে।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def post_init(app):
    sched=AsyncIOScheduler(timezone=BD_TZ)
    sched.add_job(send_signals,'cron',hour=18,minute=0,args=[app.bot])
    sched.start()
    log.info("✅ Scheduler চালু — প্রতিদিন ৬:০০ PM (BST)")

def main():
    log.info("🚀 DSE Signal Bot চালু হচ্ছে...")
    app=Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("signal",cmd_signal))
    app.add_handler(CommandHandler("ew",    cmd_ew))
    app.add_handler(CommandHandler("top",   cmd_top))
    app.add_handler(CommandHandler("sell",  cmd_sell))
    log.info("✅ Bot polling শুরু হয়েছে")
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__':
    main()
