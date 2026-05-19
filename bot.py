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
HEADERS={
    'User-Agent':'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120',
    'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language':'en-US,en;q=0.5',
    'Connection':'keep-alive',
}

def safe_float(txt):
    try:
        return float(str(txt).strip().replace(',','').replace('%','').replace('৳',''))
    except:return 0.0

def safe_int(txt):
    try:
        return int(float(str(txt).strip().replace(',','')))
    except:return 0

def fetch_stocks():
    log.info("DSE থেকে ডেটা আনা শুরু...")
    url="https://www.dsebd.org/latest_share_price_scroll_by_value.php"
    try:
        r=requests.get(url,headers=HEADERS,timeout=30)
        r.raise_for_status()
        soup=BeautifulSoup(r.text,'html.parser')

        # সব table row খুঁজি
        all_rows=soup.find_all('tr')
        log.info(f"মোট row পাওয়া গেছে: {len(all_rows)}")

        stocks=[]
        for row in all_rows:
            cols=row.find_all('td')
            if len(cols)<9:continue

            # সব column এর text নিই
            cells=[c.get_text(strip=True) for c in cols]

            # Symbol খুঁজি — শুধু letters, 2-12 char
            sym=None
            sym_idx=0
            for i,cell in enumerate(cells[:4]):
                cleaned=cell.replace('-','').replace('_','')
                if cleaned.isalpha() and 2<=len(cell)<=12 and cell.upper() not in('SL','NO','SYMBOL','NAME','CODE','TRADE'):
                    sym=cell.upper()
                    sym_idx=i
                    break

            if not sym:continue

            # Symbol এর পরের সব numeric value নিই
            nums=[]
            for cell in cells[sym_idx+1:]:
                nums.append(safe_float(cell))

            if len(nums)<6:continue

            # dsebd column order: LTP, OPEN, HIGH, LOW, YDAY, CHANGE%, TRADE, VALUE, VOLUME
            ltp  =nums[0] if len(nums)>0 else 0
            hi   =nums[2] if len(nums)>2 else 0
            lo   =nums[3] if len(nums)>3 else 0
            yday =nums[4] if len(nums)>4 else 0
            chg  =nums[5] if len(nums)>5 else 0

            # Volume — সবচেয়ে বড় number যেটা reasonable
            vol=0
            for n in nums[6:]:
                if 100<=n<=100000000 and n>vol:
                    vol=n
            vol=int(vol)

            # Validation
            if ltp<5 or ltp>200000:continue
            if hi<=0:hi=ltp
            if lo<=0:lo=ltp
            if hi<lo:hi,lo=lo,hi
            if abs(chg)>25:continue   # circuit breaker বাদ
            if vol<5000:continue      # কম volume বাদ

            stocks.append({
                'symbol':sym,
                'ltp':round(ltp,2),
                'high':round(hi,2),
                'low':round(lo,2),
                'yday':round(yday,2),
                'change':round(chg,2),
                'volume':vol,
            })

        # Duplicate বাদ
        seen=set()
        unique=[]
        for s in stocks:
            if s['symbol'] not in seen:
                seen.add(s['symbol'])
                unique.append(s)

        log.info(f"✅ {len(unique)} valid stocks পাওয়া গেছে")

        # যদি কম পাই তাহলে log করি
        if len(unique)<10:
            log.warning(f"মাত্র {len(unique)} stocks — DSE বন্ধ থাকতে পারে")

        return unique

    except Exception as e:
        log.error(f"Fetch error: {e}")
        return[]

def get_dsex():
    try:
        r=requests.get("https://www.dsebd.org",headers=HEADERS,timeout=12)
        text=r.text
        # DSEX সাধারণত 4000-8000 range এ থাকে
        patterns=[
            r'DSEX[^\d]*(\d{4,6}\.?\d{0,2})',
            r'>(\d{4,6}\.\d{2})<',
            r'(\d{4,6}\.\d{2})',
        ]
        for pat in patterns:
            matches=re.findall(pat,text)
            for m in matches:
                try:
                    val=float(m.replace(',',''))
                    if 3000<val<10000:
                        return f"{val:,.2f}"
                except:continue
        return "N/A"
    except Exception as e:
        log.error(f"DSEX error: {e}")
        return "N/A"

def analyze(stocks):
    scored=[]
    for s in stocks:
        ltp=s['ltp']
        hi=s['high']
        lo=s['low']
        chg=s['change']
        vol=s['volume']

        rng=hi-lo if hi>lo else ltp*0.01
        close_pos=(ltp-lo)/rng   # 0=bottom, 1=top
        upper_wick=(hi-ltp)/rng
        lower_wick=(ltp-lo)/rng

        score=0
        tags=[]

        # ── Candlestick Pattern ──
        if close_pos>0.7 and lower_wick>0.3:
            score+=3;tags.append("Hammer 🔨")
        elif close_pos>0.65:
            score+=2;tags.append("Strong Close ✅")
        elif close_pos<0.3 and upper_wick>0.3:
            score-=3;tags.append("Shooting Star 💫")
        elif close_pos<0.35:
            score-=2;tags.append("Weak Close ❌")

        # ── Price Change ──
        if 1<chg<=3:    score+=1;tags.append(f"+{chg:.1f}%")
        elif 3<chg<=7:  score+=2;tags.append(f"+{chg:.1f}% 📈")
        elif chg>7:     score+=1;tags.append(f"+{chg:.1f}% ⚠️")  # circuit সন্দেহ
        elif -3<=chg<-1:score-=1;tags.append(f"{chg:.1f}%")
        elif -7<=chg<-3:score-=2;tags.append(f"{chg:.1f}% 📉")
        elif chg<-7:    score-=1;tags.append(f"{chg:.1f}% ⚠️")

        # ── Volume ──
        if vol>1000000:   score+=3;tags.append(f"Vol:{vol//1000}K 🔥🔥🔥")
        elif vol>500000:  score+=2;tags.append(f"Vol:{vol//1000}K 🔥🔥")
        elif vol>100000:  score+=1;tags.append(f"Vol:{vol//1000}K 🔥")
        elif vol>30000:   tags.append(f"Vol:{vol//1000}K")

        # ── Fibonacci (daily range based) ──
        f618=lo+rng*0.618
        f382=lo+rng*0.382
        if ltp>0:
            if abs(ltp-f618)/ltp<0.012:
                score+=2;tags.append("Fib 0.618 ✨")
            elif abs(ltp-f382)/ltp<0.012:
                score+=1;tags.append("Fib 0.382")

        # ── Gap from yesterday ──
        if s['yday']>0:
            gap=(ltp-s['yday'])/s['yday']*100
            if gap>1.5:score+=1;tags.append("Gap Up ⬆️")
            elif gap<-1.5:score-=1;tags.append("Gap Down ⬇️")

        # ── Signal ──
        if score>=7:   signal="STRONG BUY 🟢🟢"
        elif score>=4: signal="BUY 🟢"
        elif score<=-7:signal="STRONG SELL 🔴🔴"
        elif score<=-4:signal="SELL 🔴"
        else:          signal="HOLD 🟡"

        # ── Entry / SL / TP ──
        sl=round(lo*0.995,2)
        risk=max(ltp-sl, ltp*0.02)
        tp1=round(ltp+risk*2,2)
        tp2=round(ltp+risk*3.5,2)

        s.update({
            'score':score,'signal':signal,'tags':tags,
            'entry':ltp,'sl':sl,'tp1':tp1,'tp2':tp2,
        })
        scored.append(s)

    scored.sort(key=lambda x:x['score'],reverse=True)
    return scored

def find_ew(stocks):
    out=[]
    for s in stocks:
        hi,lo,ltp=s['high'],s['low'],s['ltp']
        if hi<=lo or ltp<=0:continue
        rng=hi-lo
        f618=lo+rng*0.618
        f382=lo+rng*0.382
        close_pos=(ltp-lo)/rng
        n618=abs(ltp-f618)/ltp<0.02
        n382=abs(ltp-f382)/ltp<0.02
        if(n618 or n382) and close_pos>0.55 and s['change']>0 and s['volume']>20000:
            fib='0.618' if n618 else '0.382'
            s['ew_note']=f"Fib {fib} এ সাপোর্ট — Wave 3/5 শুরু হতে পারে 🌊"
            out.append(s)
    return out[:6]

def ai_summary(buys,ew_list,dsex):
    today=datetime.now(BD_TZ).strftime("%d %B %Y")
    lines="".join([
        f"{s['symbol']}: ৳{s['ltp']} H:{s['high']} L:{s['low']} ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        for s in buys[:5]
    ])
    ew_txt="\n".join([f"• {s['symbol']}: {s.get('ew_note','')}" for s in ew_list[:3]])
    try:
        client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp=client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role":"user","content":
                f"তারিখ: {today} | DSEX: {dsex}\n\n"
                f"BUY সিগনাল:\n{lines}\n"
                f"EW candidates:\n{ew_txt}\n\n"
                f"প্রতিটি স্টকের জন্য ১ লাইনে কেন কিনবেন বা সাবধান — বাংলায়। "
                f"শেষে ২ লাইনে আজকের DSE মার্কেট অবস্থা।"
            }]
        )
        return resp.content[0].text
    except Exception as e:
        log.error(f"AI error: {e}")
        return "⚠️ AI বিশ্লেষণ পাওয়া যায়নি।"

def build_msg(scored,dsex):
    now=datetime.now(BD_TZ).strftime("%d %b %Y %I:%M %p")
    buys =[s for s in scored if 'BUY'  in s['signal']][:8]
    sells=[s for s in scored if 'SELL' in s['signal']][:5]
    ew_list=find_ew(scored)
    ai=ai_summary(buys,ew_list,dsex)

    msg =f"🏦 *DSE Signal Bot*\n"
    msg+=f"📅 {now}\n"
    msg+=f"📊 DSEX: `{dsex}`\n"
    msg+=f"{'━'*22}\n\n"

    if buys:
        msg+=f"🟢 *BUY সিগনাল — {len(buys)} টি*\n\n"
        for s in buys:
            msg+=f"*{s['symbol']}* — {s['signal']}\n"
            msg+=f"💰 `৳{s['ltp']}` ({s['change']:+.1f}%) | Vol: {s['volume']:,}\n"
            msg+=f"H:`{s['high']}` L:`{s['low']}`\n"
            msg+=f"📥 Entry:`৳{s['entry']}` SL:`৳{s['sl']}`\n"
            msg+=f"🎯 TP1:`৳{s['tp1']}` TP2:`৳{s['tp2']}`\n"
            msg+=f"🏷 {' · '.join(s['tags'][:3])}\n\n"
    else:
        msg+="🟡 আজ strong BUY সিগনাল নেই\n\n"

    if sells:
        msg+=f"🔴 *SELL সতর্কতা — {len(sells)} টি*\n"
        for s in sells:
            msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+="\n"

    if ew_list:
        msg+="🌊 *EW Wave 2/4 Bounce*\n"
        for s in ew_list:
            msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%)\n"
            msg+=f"Entry:`৳{s['entry']}` SL:`৳{s['sl']}` TP:`৳{s['tp1']}`\n"
            msg+=f"_{s.get('ew_note','')}_\n\n"

    msg+=f"{'━'*22}\n"
    msg+=f"🤖 *AI বিশ্লেষণ*\n{ai}\n\n"
    msg+="⚠️ _Stop Loss সবসময় ব্যবহার করুন।_"
    return msg

async def send_signals(bot):
    log.info("📊 Signal job শুরু...")
    await bot.send_message(chat_id=CHAT_ID,text="⏳ DSE ডেটা বিশ্লেষণ চলছে...")
    try:
        stocks=fetch_stocks()
        if not stocks:
            await bot.send_message(chat_id=CHAT_ID,
                text="❌ ডেটা পাওয়া যায়নি।\n"
                     "কারণ হতে পারে:\n"
                     "• DSE আজ বন্ধ (শুক্র/শনি)\n"
                     "• Trading hour শেষ (২:৩০ PM এর পর)\n"
                     "• dsebd.org সাময়িক বন্ধ")
            return
        dsex=get_dsex()
        scored=analyze(stocks)
        msg=build_msg(scored,dsex)
        for i in range(0,len(msg),4000):
            await bot.send_message(chat_id=CHAT_ID,
                text=msg[i:i+4000],parse_mode='Markdown')
        log.info(f"✅ Signal পাঠানো হয়েছে — {len(stocks)} stocks")
    except Exception as e:
        log.error(f"Signal error: {e}")
        await bot.send_message(chat_id=CHAT_ID,text=f"❌ সমস্যা:\n{e}")

async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        f"🏦 *DSE Signal Bot*\n\n"
        f"Chat ID: `{u.effective_chat.id}`\n\n"
        f"📌 Commands:\n"
        f"/signal — এখনই সিগনাল\n"
        f"/top — Top Gainers\n"
        f"/sell — Sell সিগনাল\n"
        f"/ew — EW Wave 2/4\n\n"
        f"🕕 প্রতিদিন সন্ধ্যা ৬টায় সিগনাল আসবে\n"
        f"⚠️ _বিনিয়োগে ঝুঁকি আছে_",
        parse_mode='Markdown')

async def cmd_signal(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("⏳ বিশ্লেষণ চলছে...")
    await send_signals(ctx.bot)

async def cmd_top(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔥 লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:
        await u.message.reply_text("❌ ডেটা নেই। DSE বন্ধ থাকতে পারে।")
        return
    top=sorted(stocks,key=lambda x:x['change'],reverse=True)[:10]
    msg="🔥 *Top 10 Gainers* _(Vol≥5K filtered)_\n\n"
    for i,s in enumerate(top,1):
        msg+=f"{i}. *{s['symbol']}* `৳{s['ltp']}` (+{s['change']:.1f}%) Vol:{s['volume']:,}\n"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_sell(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔴 লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:
        await u.message.reply_text("❌ ডেটা নেই।")
        return
    scored=analyze(stocks)
    sells=[s for s in scored if 'SELL' in s['signal']]
    if not sells:
        await u.message.reply_text("আজ SELL সিগনাল নেই।")
        return
    msg="🔴 *SELL সিগনাল*\n\n"
    for s in sells[:8]:
        msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+=f"🏷 {' · '.join(s['tags'][:3])}\n\n"
    msg+="⚠️ _বিনিয়োগে ঝুঁকি আছে।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_ew(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🌊 EW স্ক্যান চলছে...")
    stocks=fetch_stocks()
    if not stocks:
        await u.message.reply_text("❌ ডেটা নেই।")
        return
    scored=analyze(stocks)
    ew_list=find_ew(scored)
    if not ew_list:
        await u.message.reply_text("আজ EW candidate নেই।")
        return
    msg="🌊 *EW Wave 2/4 Bounce*\n\n"
    for s in ew_list:
        msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+=f"Entry:`৳{s['entry']}` SL:`৳{s['sl']}` TP:`৳{s['tp1']}`\n"
        msg+=f"_{s.get('ew_note','')}_\n\n"
    msg+="⚠️ _Stop Loss সবসময় ব্যবহার করুন।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def post_init(app):
    # UTC 12:00 = Bangladesh 18:00 (সন্ধ্যা ৬টা)
    sched=AsyncIOScheduler(timezone='UTC')
    sched.add_job(send_signals,'cron',hour=12,minute=0,args=[app.bot])
    sched.start()
    log.info("✅ Scheduler চালু — UTC 12:00 = BD সন্ধ্যা ৬:০০")

def main():
    log.info("🚀 DSE Signal Bot চালু হচ্ছে...")
    app=Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("signal",cmd_signal))
    app.add_handler(CommandHandler("top",   cmd_top))
    app.add_handler(CommandHandler("sell",  cmd_sell))
    app.add_handler(CommandHandler("ew",    cmd_ew))
    log.info("✅ Bot polling শুরু হয়েছে")
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__':
    main()
