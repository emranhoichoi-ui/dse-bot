import os,logging,requests,re,time
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
    'Accept':'text/html,application/xhtml+xml',
}

# ── Config ──
MIN_PRICE=1.0
PENNY_THRESHOLD=10.0
MIN_VOLUME=30000
MAX_CHANGE=15.0
TP1_MIN=0.07
TP2_MIN=0.15

def safe_float(txt):
    try:return float(str(txt).strip().replace(',','').replace('%','').replace('৳',''))
    except:return 0.0

def safe_int(txt):
    try:return int(float(str(txt).strip().replace(',','')))
    except:return 0

# ════════════════════════════════
#  HISTORICAL DATA + INDICATORS
# ════════════════════════════════

def get_historical(symbol,days=60):
    try:
        sym=symbol+'.BD'
        end=int(time.time())
        start=end-(days*24*3600)
        url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={start}&period2={end}&interval=1d"
        r=requests.get(url,headers={'User-Agent':'Mozilla/5.0'},timeout=15)
        data=r.json()
        closes=data['chart']['result'][0]['indicators']['quote'][0]['close']
        closes=[c for c in closes if c is not None]
        return closes
    except:
        return[]

def calc_rsi(closes,period=14):
    if len(closes)<period+1:return 50.0
    gains,losses=[],[]
    for i in range(1,len(closes)):
        diff=closes[i]-closes[i-1]
        gains.append(max(diff,0))
        losses.append(max(-diff,0))
    avg_gain=sum(gains[-period:])/period
    avg_loss=sum(losses[-period:])/period
    if avg_loss==0:return 100.0
    rs=avg_gain/avg_loss
    return round(100-(100/(1+rs)),1)

def calc_ema(data,period):
    if len(data)<period:return data[-1] if data else 0
    k=2/(period+1)
    ema_val=sum(data[:period])/period
    for price in data[period:]:
        ema_val=price*k+ema_val*(1-k)
    return ema_val

def calc_macd(closes):
    if len(closes)<26:return 0,0,0
    ema12=calc_ema(closes,12)
    ema26=calc_ema(closes,26)
    macd_line=ema12-ema26
    signal=macd_line*0.9
    histogram=macd_line-signal
    return round(macd_line,3),round(signal,3),round(histogram,3)

def calc_bb(closes,period=20):
    if len(closes)<period:return 0,0,0
    recent=closes[-period:]
    middle=sum(recent)/period
    std=(sum((x-middle)**2 for x in recent)/period)**0.5
    upper=middle+2*std
    lower=middle-2*std
    return round(upper,2),round(middle,2),round(lower,2)

def get_indicators(symbol):
    closes=get_historical(symbol,60)
    if len(closes)<20:
        return{'rsi':50,'macd':0,'macd_sig':0,'macd_hist':0,
               'bb_upper':0,'bb_mid':0,'bb_lower':0,'data_ok':False}
    rsi=calc_rsi(closes)
    macd,sig,hist=calc_macd(closes)
    bbu,bbm,bbl=calc_bb(closes)
    last=closes[-1]
    bb_pos='upper' if last>bbu else 'lower' if last<bbl else 'middle'
    return{
        'rsi':rsi,'macd':macd,'macd_sig':sig,'macd_hist':hist,
        'bb_upper':bbu,'bb_mid':bbm,'bb_lower':bbl,
        'bb_pos':bb_pos,'data_ok':True,'closes':closes
    }

# ════════════════════════════════
#  DSE DATA FETCH
# ════════════════════════════════

def fetch_stocks():
    log.info("DSE ডেটা আনা শুরু...")
    url="https://www.dsebd.org/latest_share_price_scroll_by_value.php"
    try:
        r=requests.get(url,headers=HEADERS,timeout=30)
        r.raise_for_status()
        soup=BeautifulSoup(r.text,'html.parser')
        all_rows=soup.find_all('tr')
        log.info(f"Row: {len(all_rows)}")
        stocks=[]
        for row in all_rows:
            cols=row.find_all('td')
            if len(cols)<9:continue
            cells=[c.get_text(strip=True) for c in cols]
            sym=None;sym_idx=0
            for i,cell in enumerate(cells[:4]):
                cl=cell.replace('-','').replace('_','')
                if cl.isalpha() and 2<=len(cell)<=12 and cell.upper() not in('SL','NO','SYMBOL','NAME','CODE'):
                    sym=cell.upper();sym_idx=i;break
            if not sym:continue
            nums=[safe_float(c) for c in cells[sym_idx+1:]]
            if len(nums)<6:continue
            ltp=nums[0]
            hi =nums[2] if len(nums)>2 else 0
            lo =nums[3] if len(nums)>3 else 0
            yd =nums[4] if len(nums)>4 else 0
            chg=nums[5] if len(nums)>5 else 0
            vol=0
            for n in nums[6:]:
                if 1000<=n<=500000000 and n>vol:vol=n
            vol=int(vol)
            if ltp<MIN_PRICE:continue
            if vol<MIN_VOLUME:continue
            if abs(chg)>MAX_CHANGE:continue
            if hi<=0:hi=ltp
            if lo<=0:lo=ltp
            if hi<lo:hi,lo=lo,hi
            stocks.append({
                'symbol':sym,'ltp':round(ltp,2),
                'high':round(hi,2),'low':round(lo,2),
                'yday':round(yd,2),'change':round(chg,2),'volume':vol,
            })
        seen=set();unique=[]
        for s in stocks:
            if s['symbol'] not in seen:seen.add(s['symbol']);unique.append(s)
        log.info(f"✅ {len(unique)} stocks")
        return unique
    except Exception as e:
        log.error(f"Fetch error: {e}");return[]

def get_dsex():
    try:
        r=requests.get("https://www.dsebd.org",headers=HEADERS,timeout=12)
        patterns=[r'DSEX[^\d]*(\d{4,6}\.?\d{0,2})',r'>(\d{4,6}\.\d{2})<',r'(\d{4,6}\.\d{2})']
        for pat in patterns:
            for m in re.findall(pat,r.text):
                try:
                    val=float(m.replace(',',''))
                    if 3000<val<10000:return f"{val:,.2f}"
                except:continue
        return "N/A"
    except:return "N/A"

# ════════════════════════════════
#  TECHNICAL ANALYSIS
# ════════════════════════════════

def analyze(stocks,use_historical=False):
    scored=[]
    for s in stocks:
        ltp=s['ltp'];hi=s['high'];lo=s['low']
        chg=s['change'];vol=s['volume'];yd=s['yday']

        rng=hi-lo if hi>lo else ltp*0.01
        close_pos=(ltp-lo)/rng
        upper_wick=(hi-ltp)/rng
        lower_wick=(ltp-lo)/rng

        score=0;tags=[]

        # ── 1. Candlestick ──
        if lower_wick>0.45 and upper_wick<0.15 and close_pos>0.55:
            score+=3;tags.append("Hammer 🔨")
        elif lower_wick>0.35 and close_pos>0.65:
            score+=2;tags.append("Bullish 🕯️")
        elif upper_wick>0.45 and lower_wick<0.15:
            score-=3;tags.append("Shooting Star 💫")
        elif upper_wick>0.35 and close_pos<0.35:
            score-=2;tags.append("Bearish 🕯️")
        elif abs(close_pos-0.5)<0.1 and lower_wick>0.25 and upper_wick>0.25:
            tags.append("Doji")

        # ── 2. Change % ──
        if 1.5<chg<=5:   score+=2;tags.append(f"+{chg:.1f}% 📈")
        elif 5<chg<=MAX_CHANGE:score+=1;tags.append(f"+{chg:.1f}%")
        elif chg<-3:     score-=2;tags.append(f"{chg:.1f}% 📉")
        elif chg<-1:     score-=1;tags.append(f"{chg:.1f}%")

        # ── 3. Volume ──
        if vol>2000000:   score+=3;tags.append(f"Vol:{vol//1000}K 🔥🔥🔥")
        elif vol>500000:  score+=2;tags.append(f"Vol:{vol//1000}K 🔥🔥")
        elif vol>100000:  score+=1;tags.append(f"Vol:{vol//1000}K 🔥")
        else:             tags.append(f"Vol:{vol//1000}K")

        # ── 4. Fibonacci (daily range) ──
        f618=lo+rng*0.618
        f382=lo+rng*0.382
        f500=lo+rng*0.500
        if ltp>0:
            if abs(ltp-f618)/ltp<0.015:score+=2;tags.append("Fib 0.618 ✨")
            elif abs(ltp-f382)/ltp<0.015:score+=1;tags.append("Fib 0.382")
            elif abs(ltp-f500)/ltp<0.015:score+=1;tags.append("Fib 0.500")

        # ── 5. Gap ──
        if yd>0:
            gap=(ltp-yd)/yd*100
            if gap>1.5:score+=1;tags.append("Gap Up ⬆️")
            elif gap<-1.5:score-=1;tags.append("Gap Down ⬇️")

        # ── 6. RSI / MACD / BB ──
        ind={'rsi':50,'data_ok':False}
        if use_historical:
            ind=get_indicators(s['symbol'])
            if ind['data_ok']:
                rsi=ind['rsi']
                # RSI
                if rsi<35:
                    score+=3;tags.append(f"RSI:{rsi} Oversold 🟢")
                elif 35<=rsi<50:
                    score+=2;tags.append(f"RSI:{rsi} ✅")
                elif 50<=rsi<65:
                    score+=1;tags.append(f"RSI:{rsi}")
                elif rsi>=75:
                    score-=2;tags.append(f"RSI:{rsi} OB ⚠️")
                else:
                    tags.append(f"RSI:{rsi}")

                # MACD
                if ind['macd_hist']>0 and ind['macd']>ind['macd_sig']:
                    score+=2;tags.append("MACD ↑ 🟢")
                elif ind['macd_hist']>0:
                    score+=1;tags.append("MACD ↑")
                elif ind['macd_hist']<0 and ind['macd']<ind['macd_sig']:
                    score-=2;tags.append("MACD ↓ 🔴")
                elif ind['macd_hist']<0:
                    score-=1;tags.append("MACD ↓")

                # Bollinger Bands
                if ind['bb_pos']=='lower':
                    score+=2;tags.append("BB Lower 🟢")
                elif ind['bb_pos']=='upper':
                    score-=1;tags.append("BB Upper ⚠️")
                else:
                    tags.append("BB Mid")

        s['ind']=ind

        # ── Signal ──
        if score>=8:   signal="STRONG BUY 🟢🟢"
        elif score>=5: signal="BUY 🟢"
        elif score<=-8:signal="STRONG SELL 🔴🔴"
        elif score<=-5:signal="SELL 🔴"
        else:          signal="HOLD 🟡"

        # ── TP/SL — min 7% TP1, 15% TP2 ──
        sl=round(lo*0.995,2)
        risk=ltp-sl
        if risk<=0:risk=ltp*0.03
        tp1=round(max(ltp*(1+TP1_MIN),ltp+risk*2),2)
        tp2=round(max(ltp*(1+TP2_MIN),ltp+risk*3.5),2)

        s.update({
            'score':score,'signal':signal,'tags':tags,
            'entry':ltp,'sl':sl,'tp1':tp1,'tp2':tp2,
        })
        scored.append(s)

    scored.sort(key=lambda x:x['score'],reverse=True)
    return scored

# ════════════════════════════════
#  ELLIOTT WAVE SCANNER
# ════════════════════════════════

def find_ew(stocks):
    out=[]
    for s in stocks:
        hi,lo,ltp=s['high'],s['low'],s['ltp']
        if hi<=lo or ltp<=0:continue
        rng=hi-lo
        close_pos=(ltp-lo)/rng
        f618=lo+rng*0.618
        f382=lo+rng*0.382
        n618=abs(ltp-f618)/ltp<0.025
        n382=abs(ltp-f382)/ltp<0.025
        if not(n618 or n382):continue
        if close_pos<0.5:continue
        if s['change']<=0:continue
        if s['volume']<50000:continue

        closes=get_historical(s['symbol'],30)
        fib='0.618' if n618 else '0.382'

        if len(closes)>=10:
            recent_avg=sum(closes[-5:])/5
            older_avg=sum(closes[-10:-5])/5
            rsi=calc_rsi(closes) if len(closes)>14 else 50
            if recent_avg<older_avg:
                wave_desc=f"Wave 2/4 শেষে bounce | Fib {fib} সাপোর্ট | RSI:{rsi}"
                if rsi<50:wave_desc+=" Oversold ✨"
            else:
                wave_desc=f"Uptrend continuation | Fib {fib} | RSI:{rsi}"
        else:
            wave_desc=f"Fib {fib} সাপোর্টে bounce 🌊"

        s['ew_note']=wave_desc
        out.append(s)
    return out[:6]

# ════════════════════════════════
#  MESSAGE BUILDER
# ════════════════════════════════

def format_stock(s,show_penny_label=False):
    msg=""
    rsi=s['ind'].get('rsi','-') if s['ind'].get('data_ok') else '-'
    tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
    tp2_pct=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
    penny=" 💎_penny_" if show_penny_label else ""
    msg+=f"*{s['symbol']}*{penny} — {s['signal']}\n"
    msg+=f"💰 `৳{s['ltp']}` ({s['change']:+.1f}%) | Vol:{s['volume']:,}\n"
    msg+=f"H:`{s['high']}` L:`{s['low']}`\n"
    if rsi!='-':msg+=f"📊 RSI:`{rsi}`\n"
    msg+=f"📥 Entry:`৳{s['entry']}` SL:`৳{s['sl']}`\n"
    msg+=f"🎯 TP1:`৳{s['tp1']}` _(+{tp1_pct}%)_ TP2:`৳{s['tp2']}` _(+{tp2_pct}%)_\n"
    msg+=f"🏷 {' · '.join(s['tags'][:4])}\n\n"
    return msg

def ai_summary(buys,ew_list,dsex):
    today=datetime.now(BD_TZ).strftime("%d %B %Y")
    lines="".join([
        f"{s['symbol']}: ৳{s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,} "
        f"RSI:{s['ind'].get('rsi','-') if s['ind'].get('data_ok') else '-'}\n"
        for s in buys[:6]
    ])
    ew_txt="\n".join([f"• {s['symbol']}: {s.get('ew_note','')}" for s in ew_list[:3]])
    try:
        client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp=client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role":"user","content":
                f"তারিখ: {today} | DSEX: {dsex}\n\n"
                f"BUY সিগনাল (RSI/MACD/BB সহ):\n{lines}\n"
                f"EW candidates:\n{ew_txt}\n\n"
                f"প্রতিটি স্টকের জন্য ২ লাইনে বাংলায়: কেন BUY সুযোগ, কী risk। "
                f"শেষে ৩ লাইনে DSE মার্কেট অবস্থা ও আজকের কৌশল।"
            }]
        )
        return resp.content[0].text
    except Exception as e:
        return f"⚠️ AI: {str(e)[:80]}"

def build_msg(scored,dsex):
    now=datetime.now(BD_TZ).strftime("%d %b %Y %I:%M %p")
    buys =[s for s in scored if 'BUY'  in s['signal']]
    sells=[s for s in scored if 'SELL' in s['signal']][:5]
    ew_list=find_ew([s for s in scored if 'BUY' in s['signal']])
    ai=ai_summary(buys[:7],ew_list,dsex)

    msg=f"🏦 *DSE Signal Bot v2*\n📅 {now}\n📊 DSEX: `{dsex}`\n{'━'*22}\n\n"

    if buys:
        normal=[s for s in buys if s['ltp']>=PENNY_THRESHOLD][:7]
        penny =[s for s in buys if s['ltp']< PENNY_THRESHOLD][:5]

        if normal:
            msg+=f"🟢 *BUY সিগনাল — {len(normal)} টি*\n\n"
            for s in normal:msg+=format_stock(s)

        if penny:
            msg+=f"💎 *Penny Stock BUY — {len(penny)} টি*\n"
            msg+="_(৳১০ এর নিচে — বেশি ঝুঁকি, বেশি সুযোগ)_\n\n"
            for s in penny:msg+=format_stock(s,show_penny_label=False)
    else:
        msg+="🟡 আজ BUY সিগনাল নেই\n\n"

    if sells:
        msg+=f"🔴 *SELL সতর্কতা — {len(sells)} টি*\n"
        for s in sells:
            msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+="\n"

    if ew_list:
        msg+="🌊 *EW Wave 2/4 — Strong BUY সুযোগ*\n\n"
        for s in ew_list:
            tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
            msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%)\n"
            msg+=f"Entry:`৳{s['entry']}` SL:`৳{s['sl']}` TP1:`৳{s['tp1']}` _(+{tp1_pct}%)_\n"
            msg+=f"_{s.get('ew_note','')}_\n\n"

    msg+=f"{'━'*22}\n🤖 *AI বিশ্লেষণ*\n{ai}\n\n"
    msg+="⚠️ _Stop Loss সবসময় ব্যবহার করুন। বিনিয়োগে ঝুঁকি আছে।_"
    return msg

# ════════════════════════════════
#  BOT COMMANDS
# ════════════════════════════════

async def send_signals(bot):
    log.info("📊 Signal job শুরু...")
    await bot.send_message(chat_id=CHAT_ID,text="⏳ DSE ডেটা + RSI/MACD/BB বিশ্লেষণ চলছে...")
    try:
        stocks=fetch_stocks()
        if not stocks:
            await bot.send_message(chat_id=CHAT_ID,
                text="❌ ডেটা পাওয়া যায়নি।\n"
                     "• DSE আজ বন্ধ (শুক্র/শনি)?\n"
                     "• Trading hour শেষ (২:৩০ PM এর পর)?")
            return
        dsex=get_dsex()
        scored=analyze(stocks,use_historical=True)
        msg=build_msg(scored,dsex)
        for i in range(0,len(msg),4000):
            await bot.send_message(chat_id=CHAT_ID,text=msg[i:i+4000],parse_mode='Markdown')
        log.info(f"✅ {len(stocks)} stocks analyzed")
    except Exception as e:
        log.error(f"Error: {e}")
        await bot.send_message(chat_id=CHAT_ID,text=f"❌ সমস্যা:\n{e}")

async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "🏦 *DSE Signal Bot v2*\n\n"
        "✅ RSI + MACD + BB analysis\n"
        "✅ Penny stock আলাদা section\n"
        "✅ TP1 min 7%, TP2 min 15%\n"
        "✅ EW Wave 2/4 detection\n"
        "✅ Circuit breaker filtered\n\n"
        "📌 *Commands:*\n"
        "/signal — সম্পূর্ণ বিশ্লেষণ\n"
        "/top — Top Gainers\n"
        "/sell — Sell সিগনাল\n"
        "/ew — EW Wave 2/4\n"
        "/penny — শুধু Penny stocks\n\n"
        "🕕 প্রতিদিন সন্ধ্যা ৬টায় সিগনাল\n"
        "⚠️ _বিনিয়োগে ঝুঁকি আছে_",
        parse_mode='Markdown')

async def cmd_signal(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("⏳ RSI/MACD/BB সহ বিশ্লেষণ চলছে, ১-২ মিনিট অপেক্ষা করুন...")
    await send_signals(ctx.bot)

async def cmd_top(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔥 লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    top=sorted(stocks,key=lambda x:x['change'],reverse=True)[:10]
    msg="🔥 *Top 10 Gainers*\n_(Vol≥30K, Circuit বাদ)_\n\n"
    for i,s in enumerate(top,1):
        penny="💎" if s['ltp']<PENNY_THRESHOLD else "  "
        msg+=f"{i}. {penny}*{s['symbol']}* `৳{s['ltp']}` (+{s['change']:.1f}%) Vol:{s['volume']:,}\n"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_sell(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔴 লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    scored=analyze(stocks,use_historical=False)
    sells=[s for s in scored if 'SELL' in s['signal']]
    if not sells:await u.message.reply_text("আজ SELL সিগনাল নেই।");return
    msg="🔴 *SELL সিগনাল*\n\n"
    for s in sells[:8]:
        msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+=f"🏷 {' · '.join(s['tags'][:3])}\n\n"
    msg+="⚠️ _বিনিয়োগে ঝুঁকি আছে।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_ew(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🌊 EW স্ক্যান চলছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    scored=analyze(stocks,use_historical=False)
    buys=[s for s in scored if 'BUY' in s['signal']]
    ew_list=find_ew(buys)
    if not ew_list:await u.message.reply_text("আজ EW candidate নেই।");return
    msg="🌊 *EW Wave 2/4 — Strong BUY*\n\n"
    for s in ew_list:
        tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
        msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%)\n"
        msg+=f"Entry:`৳{s['entry']}` SL:`৳{s['sl']}` TP1:`৳{s['tp1']}` _(+{tp1_pct}%)_\n"
        msg+=f"_{s.get('ew_note','')}_\n\n"
    msg+="⚠️ _Stop Loss সবসময় ব্যবহার করুন।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_penny(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("💎 Penny Stock বিশ্লেষণ চলছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    penny_stocks=[s for s in stocks if s['ltp']<PENNY_THRESHOLD]
    if not penny_stocks:
        await u.message.reply_text("আজ কোনো penny stock নেই।");return
    scored=analyze(penny_stocks,use_historical=False)
    buys=[s for s in scored if 'BUY' in s['signal']]
    if not buys:
        await u.message.reply_text("আজ penny stock BUY সিগনাল নেই।");return
    msg="💎 *Penny Stock BUY সিগনাল*\n"
    msg+="_(৳১০ এর নিচে — বেশি ঝুঁকি, বেশি সুযোগ)_\n\n"
    for s in buys[:8]:
        tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
        tp2_pct=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
        msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+=f"📥 Entry:`৳{s['entry']}` SL:`৳{s['sl']}`\n"
        msg+=f"🎯 TP1:`৳{s['tp1']}` _(+{tp1_pct}%)_ TP2:`৳{s['tp2']}` _(+{tp2_pct}%)_\n"
        msg+=f"🏷 {' · '.join(s['tags'][:3])}\n\n"
    msg+="⚠️ _Penny stock এ ঝুঁকি বেশি। ছোট amount দিয়ে শুরু করুন।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def post_init(app):
    sched=AsyncIOScheduler(timezone='UTC')
    sched.add_job(send_signals,'cron',hour=12,minute=0,args=[app.bot])
    sched.start()
    log.info("✅ Scheduler — UTC 12:00 = BD সন্ধ্যা ৬:০০")

def main():
    log.info("🚀 DSE Signal Bot v2 চালু হচ্ছে...")
    app=Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("signal",cmd_signal))
    app.add_handler(CommandHandler("top",   cmd_top))
    app.add_handler(CommandHandler("sell",  cmd_sell))
    app.add_handler(CommandHandler("ew",    cmd_ew))
    app.add_handler(CommandHandler("penny", cmd_penny))
    log.info("✅ Bot v2 polling শুরু")
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__':
    main()
