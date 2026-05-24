import os,logging,requests,re,time,json,sqlite3
from bs4 import BeautifulSoup
from datetime import datetime,timedelta
import pytz
from telegram import Update
from telegram.ext import Application,CommandHandler,ContextTypes,MessageHandler,filters
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic

logging.basicConfig(format='%(asctime)s-%(levelname)s-%(message)s',level=logging.INFO)
log=logging.getLogger(__name__)

TELEGRAM_TOKEN=os.environ['TELEGRAM_TOKEN']
ANTHROPIC_API_KEY=os.environ['ANTHROPIC_API_KEY']
CHAT_ID=os.environ['CHAT_ID']
BD_TZ=pytz.timezone('Asia/Dhaka')
HEADERS={'User-Agent':'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120','Accept':'text/html'}

MIN_PRICE=1.0
PENNY_THRESHOLD=10.0
MIN_VOLUME=20000
MAX_CHANGE=15.0
TP1_MIN=0.08   # minimum 8%
TP2_MIN=0.20   # minimum 20%
DB_PATH='/tmp/dse_v3.db'

# ══════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════
def init_db():
    conn=sqlite3.connect(DB_PATH);c=conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,symbol TEXT,signal_type TEXT,
        entry REAL,sl REAL,tp1 REAL,tp2 REAL,score REAL,
        indicators TEXT,outcome TEXT DEFAULT 'pending',
        outcome_pct REAL DEFAULT 0,check_date TEXT,created_at TEXT)''')
    conn.commit();conn.close()

def save_signal(sym,sig,entry,sl,tp1,tp2,score,inds):
    try:
        conn=sqlite3.connect(DB_PATH);c=conn.cursor()
        now=datetime.now(BD_TZ)
        chk=(now+timedelta(days=5)).strftime('%Y-%m-%d')
        c.execute('''INSERT INTO signals(date,symbol,signal_type,entry,sl,tp1,tp2,score,indicators,check_date,created_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                  (now.strftime('%Y-%m-%d'),sym,sig,entry,sl,tp1,tp2,score,json.dumps(inds),chk,now.isoformat()))
        conn.commit();conn.close()
    except:pass

def get_stats():
    try:
        conn=sqlite3.connect(DB_PATH);c=conn.cursor()
        total=c.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        wins=c.execute('SELECT COUNT(*) FROM signals WHERE outcome="win"').fetchone()[0]
        losses=c.execute('SELECT COUNT(*) FROM signals WHERE outcome="loss"').fetchone()[0]
        pending=c.execute('SELECT COUNT(*) FROM signals WHERE outcome="pending"').fetchone()[0]
        avg=c.execute('SELECT AVG(outcome_pct) FROM signals WHERE outcome!="pending"').fetchone()[0] or 0
        recent=c.execute('SELECT symbol,entry,tp1,outcome,outcome_pct,date FROM signals ORDER BY id DESC LIMIT 8').fetchall()
        conn.close()
        wr=round(wins/max(wins+losses,1)*100,1)
        return{'total':total,'wins':wins,'losses':losses,'pending':pending,'wr':wr,'avg':round(avg,2),'recent':recent}
    except:
        return{'total':0,'wins':0,'losses':0,'pending':0,'wr':0,'avg':0,'recent':[]}

# ══════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════
def sf(txt):
    try:return float(str(txt).strip().replace(',','').replace('%','').replace('৳',''))
    except:return 0.0

def ema(data,period):
    if len(data)<period:return data[-1] if data else 0
    k=2/(period+1);v=sum(data[:period])/period
    for p in data[period:]:v=p*k+v*(1-k)
    return v

def rsi(closes,period=14):
    if len(closes)<period+1:return 50.0
    g,l=[],[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1];g.append(max(d,0));l.append(max(-d,0))
    ag=sum(g[-period:])/period;al=sum(l[-period:])/period
    if al==0:return 100.0
    return round(100-(100/(1+ag/al)),1)

def macd(closes):
    if len(closes)<26:return 0,0,0
    e12=ema(closes,12);e26=ema(closes,26)
    ml=e12-e26;sl_=ml*0.9;hist=ml-sl_
    return round(ml,3),round(sl_,3),round(hist,3)

def bollinger(closes,period=20):
    if len(closes)<period:return 0,0,0
    r=closes[-period:];mid=sum(r)/period
    std=(sum((x-mid)**2 for x in r)/period)**0.5
    return round(mid+2*std,2),round(mid,2),round(mid-2*std,2)

def sma(closes,period):
    if len(closes)<period:return closes[-1] if closes else 0
    return sum(closes[-period:])/period

# ══════════════════════════════════════
#  HISTORICAL DATA (Yahoo Finance)
# ══════════════════════════════════════
def get_hist(symbol,days=90):
    try:
        sym=symbol+'.BD'
        end=int(time.time());start=end-(days*86400)
        url=f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={start}&period2={end}&interval=1d"
        r=requests.get(url,headers={'User-Agent':'Mozilla/5.0'},timeout=15)
        d=r.json()['chart']['result'][0]
        quotes=d['indicators']['quote'][0]
        closes=[c for c in quotes.get('close',[]) if c]
        highs=[h for h in quotes.get('high',[]) if h]
        lows=[l for l in quotes.get('low',[]) if l]
        vols=[v for v in quotes.get('volume',[]) if v]
        return closes,highs,lows,vols
    except:
        return[],[],[],[]

def get_all_indicators(symbol):
    closes,highs,lows,vols=get_hist(symbol,90)
    if len(closes)<20:
        return{'ok':False,'rsi':50,'macd_h':0,'bb_pos':'mid',
               'ma20':0,'ma50':0,'ema9':0,'ema21':0,'trend':'neutral',
               'avg_vol':0,'vol_spike':False,'swing_high':0,'swing_low':0}

    r=rsi(closes)
    ml,sl_,mh=macd(closes)
    bbu,bbm,bbl=bollinger(closes)
    ma20=sma(closes,20)
    ma50=sma(closes,min(50,len(closes)))
    e9=ema(closes,min(9,len(closes)))
    e21=ema(closes,min(21,len(closes)))
    last=closes[-1]

    # BB position
    bp='upper' if last>bbu else 'lower' if last<bbl else 'mid'

    # Trend (MA alignment)
    if e9>e21>ma20>ma50:trend='strong_up'
    elif e9>e21 and ma20>ma50:trend='up'
    elif e9<e21<ma20<ma50:trend='strong_down'
    elif e9<e21 and ma20<ma50:trend='down'
    else:trend='neutral'

    # Volume
    avg_vol=sum(vols[-20:])/max(len(vols[-20:]),1) if vols else 0
    cur_vol=vols[-1] if vols else 0
    vol_spike=cur_vol>avg_vol*1.5 if avg_vol>0 else False

    # Swing High/Low (last 20 candles)
    swing_high=max(highs[-20:]) if len(highs)>=20 else 0
    swing_low=min(lows[-20:]) if len(lows)>=20 else 0

    # EW rough (last 5 candles trend vs previous 5)
    ew_phase='unknown'
    if len(closes)>=10:
        rec=sum(closes[-5:])/5;old=sum(closes[-10:-5])/5
        if last>rec>old:ew_phase='wave3_5'  # impulse up
        elif last<rec<old:ew_phase='wave_down'
        elif old>rec and last>rec:ew_phase='wave2_4_end'  # correction ending
        else:ew_phase='neutral'

    # Fake breakout detection
    # Price above swing high but RSI not confirming
    fake_break=False
    if len(highs)>=5:
        prev_high=max(highs[-6:-1]) if len(highs)>=6 else 0
        if last>prev_high and r>75:fake_break=True  # overbought breakout

    return{
        'ok':True,'rsi':r,'macd':ml,'macd_sig':sl_,'macd_h':mh,
        'bb_upper':bbu,'bb_mid':bbm,'bb_lower':bbl,'bb_pos':bp,
        'ma20':round(ma20,2),'ma50':round(ma50,2),
        'ema9':round(e9,2),'ema21':round(e21,2),
        'trend':trend,'avg_vol':int(avg_vol),'vol_spike':vol_spike,
        'swing_high':round(swing_high,2),'swing_low':round(swing_low,2),
        'ew_phase':ew_phase,'fake_break':fake_break,'closes':closes,
    }

# ══════════════════════════════════════
#  DSE LIVE DATA
# ══════════════════════════════════════
def fetch_stocks():
    log.info("DSE data fetch শুরু...")
    url="https://www.dsebd.org/latest_share_price_scroll_by_value.php"
    try:
        r=requests.get(url,headers=HEADERS,timeout=30);r.raise_for_status()
        soup=BeautifulSoup(r.text,'html.parser')
        stocks=[]
        for row in soup.find_all('tr'):
            cols=row.find_all('td')
            if len(cols)<9:continue
            cells=[c.get_text(strip=True) for c in cols]
            sym=None;si=0
            for i,cell in enumerate(cells[:4]):
                cl=cell.replace('-','').replace('_','')
                if cl.isalpha() and 2<=len(cell)<=12 and cell.upper() not in('SL','NO','SYMBOL','NAME','CODE','TRADE'):
                    sym=cell.upper();si=i;break
            if not sym:continue
            nums=[sf(c) for c in cells[si+1:]]
            if len(nums)<6:continue
            ltp=nums[0];hi=nums[2] if len(nums)>2 else 0
            lo=nums[3] if len(nums)>3 else 0
            yd=nums[4] if len(nums)>4 else 0
            chg=nums[5] if len(nums)>5 else 0
            vol=0
            for n in nums[6:]:
                if 1000<=n<=999999999 and n>vol:vol=n
            vol=int(vol)
            if ltp<MIN_PRICE or vol<MIN_VOLUME or abs(chg)>MAX_CHANGE:continue
            if hi<=0:hi=ltp
            if lo<=0:lo=ltp
            if hi<lo:hi,lo=lo,hi
            stocks.append({'symbol':sym,'ltp':round(ltp,2),'high':round(hi,2),
                           'low':round(lo,2),'yday':round(yd,2),'change':round(chg,2),'volume':vol})
        seen=set();unique=[]
        for s in stocks:
            if s['symbol'] not in seen:seen.add(s['symbol']);unique.append(s)
        log.info(f"✅ {len(unique)} stocks fetched")
        return unique
    except Exception as e:
        log.error(f"Fetch error:{e}");return[]

def get_dsex():
    try:
        r=requests.get("https://www.dsebd.org",headers=HEADERS,timeout=12)
        for pat in[r'DSEX[^\d]*(\d{4,6}\.?\d{0,2})',r'>(\d{4,6}\.\d{2})<']:
            for m in re.findall(pat,r.text):
                try:
                    v=float(m.replace(',',''))
                    if 3000<v<10000:return f"{v:,.2f}"
                except:continue
        return "N/A"
    except:return "N/A"

# ══════════════════════════════════════
#  COMPREHENSIVE ANALYSIS ENGINE
# ══════════════════════════════════════
def analyze(stocks,use_hist=False):
    scored=[]
    for s in stocks:
        ltp=s['ltp'];hi=s['high'];lo=s['low']
        chg=s['change'];vol=s['volume'];yd=s['yday']
        rng=hi-lo if hi>lo else ltp*0.01
        cp=(ltp-lo)/rng   # close position 0-1
        uw=(hi-ltp)/rng   # upper wick
        lw=(ltp-lo)/rng   # lower wick

        score=0.0;tags=[];reasons=[];inds=[]
        warnings=[]

        # ══ 1. CANDLESTICK PATTERNS ══
        # Hammer (bullish reversal)
        if lw>0.5 and uw<0.1 and cp>0.6:
            score+=4;tags.append("Hammer 🔨");reasons.append("Hammer: বুলিশ রিভার্সাল")
            inds.append('hammer')
        # Bullish Engulfing (need prev candle info — approximate)
        elif lw>0.35 and cp>0.7 and chg>1:
            score+=3;tags.append("Bullish Engulf 🕯️");reasons.append("Bullish candle: strong close")
            inds.append('bull_engulf')
        # Inverted Hammer
        elif uw>0.4 and lw<0.1 and cp>0.5 and chg>0:
            score+=2;tags.append("Inv Hammer");inds.append('inv_hammer')
        # Shooting Star (bearish)
        elif uw>0.5 and lw<0.1 and cp<0.4:
            score-=4;tags.append("Shooting Star 💫");warnings.append("Shooting Star: বেয়ারিশ")
            inds.append('shooting_star')
        # Bearish Engulfing
        elif uw>0.35 and cp<0.3 and chg<-1:
            score-=3;tags.append("Bearish Candle 📉");inds.append('bear_engulf')
        # Doji (indecision)
        elif abs(cp-0.5)<0.08 and lw>0.2 and uw>0.2:
            tags.append("Doji ⚖️");warnings.append("Doji: মার্কেট অনিশ্চিত")
            inds.append('doji')
        # Morning Star approximation
        elif lw>0.3 and cp>0.55 and chg>2:
            score+=2;tags.append("Morning Star ⭐");inds.append('morning_star')

        # ══ 2. PRICE CHANGE ══
        if 2<chg<=5:
            score+=2;tags.append(f"+{chg:.1f}% 📈")
            reasons.append(f"দাম {chg:.1f}% বেড়েছে")
        elif 5<chg<=MAX_CHANGE:
            score+=1;tags.append(f"+{chg:.1f}%")
            warnings.append("বেশি change — circuit সন্দেহ")
        elif -5<=chg<-2:
            score-=2;tags.append(f"{chg:.1f}% 📉")
        elif chg<-5:
            score-=3;tags.append(f"{chg:.1f}% 💥")

        # ══ 3. VOLUME ANALYSIS ══
        if vol>3000000:
            score+=4;tags.append(f"Vol:{vol//1000}K 🔥🔥🔥")
            reasons.append(f"অসাধারণ volume {vol:,} — institutional interest")
        elif vol>1000000:
            score+=3;tags.append(f"Vol:{vol//1000}K 🔥🔥")
            reasons.append(f"High volume {vol:,}")
        elif vol>300000:
            score+=2;tags.append(f"Vol:{vol//1000}K 🔥")
        elif vol>100000:
            score+=1;tags.append(f"Vol:{vol//1000}K")
        else:
            tags.append(f"Vol:{vol//1000}K")
            warnings.append("Volume কম — সতর্ক থাকুন")

        # ══ 4. FIBONACCI (Daily Range) ══
        f786=lo+rng*0.786;f618=lo+rng*0.618
        f500=lo+rng*0.500;f382=lo+rng*0.382;f236=lo+rng*0.236
        fib_hit=False
        for fval,flbl in[(f618,'0.618'),(f382,'0.382'),(f786,'0.786'),(f500,'0.500'),(f236,'0.236')]:
            if ltp>0 and abs(ltp-fval)/ltp<0.015:
                sc=2 if flbl in('0.618','0.382') else 1
                score+=sc;tags.append(f"Fib {flbl} ✨")
                reasons.append(f"Fibonacci {flbl} সাপোর্টে")
                fib_hit=True;break

        # ══ 5. GAP ANALYSIS ══
        if yd>0:
            gap=(ltp-yd)/yd*100
            if gap>2:score+=1;tags.append("Gap Up ⬆️");reasons.append("Gap Up")
            elif gap>1:score+=0.5;tags.append("Gap Up ⬆️")
            elif gap<-2:score-=1;tags.append("Gap Down ⬇️")

        # ══ 6. SMC — Structure ══
        # Basic: close near high = bullish structure
        if cp>0.8 and vol>200000:
            score+=2;tags.append("SMC: Strong 💪")
            reasons.append("SMC: Price closed near high with volume — institutional buying")
            inds.append('smc_strong')
        elif cp<0.2 and vol>200000:
            score-=2;tags.append("SMC: Weak ⚠️")
            warnings.append("SMC: Price closed near low — institutional selling")

        # ══ 7. HISTORICAL INDICATORS ══
        ind={'ok':False,'rsi':50,'trend':'neutral','ew_phase':'unknown','fake_break':False}
        if use_hist:
            ind=get_all_indicators(s['symbol'])
            if ind['ok']:
                r_val=ind['rsi']

                # RSI
                if r_val<30:
                    score+=4;tags.append(f"RSI:{r_val} Oversold 🟢")
                    reasons.append(f"RSI {r_val} — heavily oversold, bounce likely")
                    inds.append('rsi_oversold')
                elif 30<=r_val<45:
                    score+=3;tags.append(f"RSI:{r_val} 🟢")
                    reasons.append(f"RSI {r_val} — good buy zone")
                    inds.append('rsi_good')
                elif 45<=r_val<60:
                    score+=1;tags.append(f"RSI:{r_val}")
                    inds.append('rsi_neutral')
                elif 60<=r_val<75:
                    tags.append(f"RSI:{r_val}")
                elif r_val>=75:
                    score-=3;tags.append(f"RSI:{r_val} OB ⚠️")
                    warnings.append(f"RSI {r_val} — overbought, exit risk")
                    inds.append('rsi_overbought')

                # MACD
                mh=ind['macd_h']
                if mh>0 and ind['macd']>ind['macd_sig']:
                    score+=3;tags.append("MACD ↑ 🟢")
                    reasons.append("MACD bullish crossover — momentum up")
                    inds.append('macd_bull')
                elif mh>0:
                    score+=1;tags.append("MACD ↑")
                elif mh<0 and ind['macd']<ind['macd_sig']:
                    score-=3;tags.append("MACD ↓ 🔴")
                    warnings.append("MACD bearish — momentum down")
                    inds.append('macd_bear')
                elif mh<0:
                    score-=1;tags.append("MACD ↓")

                # Bollinger Bands
                bp=ind['bb_pos']
                if bp=='lower':
                    score+=3;tags.append("BB Lower 🟢")
                    reasons.append("BB Lower band — oversold territory, bounce zone")
                    inds.append('bb_lower')
                elif bp=='upper':
                    score-=2;tags.append("BB Upper ⚠️")
                    warnings.append("BB Upper band — overbought")
                    inds.append('bb_upper')
                else:
                    tags.append("BB Mid")

                # MA/EMA Trend
                tr=ind['trend']
                if tr=='strong_up':
                    score+=4;tags.append("Trend: ↑↑ 🚀")
                    reasons.append("EMA9>EMA21>MA20>MA50 — perfect bullish alignment")
                    inds.append('trend_strong_up')
                elif tr=='up':
                    score+=2;tags.append("Trend: ↑")
                    reasons.append("Bullish trend — MA aligned")
                    inds.append('trend_up')
                elif tr=='strong_down':
                    score-=4;tags.append("Trend: ↓↓")
                    warnings.append("Strong downtrend — avoid")
                elif tr=='down':
                    score-=2;tags.append("Trend: ↓")

                # EW Phase
                ep=ind['ew_phase']
                if ep=='wave2_4_end':
                    score+=4;tags.append("EW: Wave 3/5 শুরু 🌊")
                    reasons.append("Elliott Wave 2/4 শেষ — সবচেয়ে শক্তিশালী impulse আসছে")
                    inds.append('ew_wave345')
                elif ep=='wave3_5':
                    score+=2;tags.append("EW: Impulse ↑")
                    reasons.append("EW impulse wave চলছে")
                elif ep=='wave_down':
                    score-=2;tags.append("EW: Down ↓")

                # Volume Spike (historical)
                if ind['vol_spike']:
                    score+=2;tags.append("Vol Spike 🔥")
                    reasons.append("Volume spike — smart money entry")
                    inds.append('vol_spike')

                # Fake Breakout Detection
                if ind['fake_break']:
                    score-=3
                    warnings.append("⚠️ FAKE BREAKOUT সম্ভাবনা — RSI overbought + near swing high")
                    tags.append("FakeBreak ⚠️")
                    inds.append('fake_break')

                # Near Swing Low (SMC Order Block)
                if ind['swing_low']>0 and abs(ltp-ind['swing_low'])/ltp<0.03:
                    score+=2;tags.append("Near OB 📦")
                    reasons.append(f"SMC Order Block zone কাছে (৳{ind['swing_low']})")
                    inds.append('order_block')

        s['ind']=ind;s['inds']=inds
        s['reasons']=reasons;s['warnings']=warnings

        # ══ SIGNAL DECISION ══
        if score>=12:   signal="STRONG BUY 🟢🟢"
        elif score>=7:  signal="BUY 🟢"
        elif score<=-12:signal="STRONG SELL 🔴🔴"
        elif score<=-7: signal="SELL 🔴"
        else:           signal="HOLD 🟡"

        # ══ TP/SL WITH MINIMUM TARGETS ══
        sl=round(lo*0.993,2)  # SL below day low
        risk=ltp-sl
        if risk<=0:risk=ltp*0.04

        # Minimum TP1=8%, TP2=20%
        tp1=round(max(ltp*(1+TP1_MIN), ltp+risk*2.5),2)
        tp2=round(max(ltp*(1+TP2_MIN), ltp+risk*4.5),2)

        s.update({'score':round(score,1),'signal':signal,'tags':tags,
                  'reasons':reasons,'warnings':warnings,
                  'entry':ltp,'sl':sl,'tp1':tp1,'tp2':tp2})
        scored.append(s)

    scored.sort(key=lambda x:x['score'],reverse=True)
    return scored

# ══════════════════════════════════════
#  EW DEEP SCANNER
# ══════════════════════════════════════
def find_ew(stocks):
    out=[]
    for s in stocks:
        ind=s.get('ind',{})
        if not ind.get('ok'):continue
        ep=ind.get('ew_phase','')
        if ep not in('wave2_4_end','wave3_5'):continue
        if s['score']<5:continue
        hi,lo,ltp=s['high'],s['low'],s['ltp']
        if hi<=lo:continue
        rng=hi-lo
        f618=lo+rng*0.618;f382=lo+rng*0.382
        n618=abs(ltp-f618)/ltp<0.03;n382=abs(ltp-f382)/ltp<0.03
        fib_txt=f"Fib {'0.618' if n618 else '0.382' if n382 else 'zone'}"
        tr=ind.get('trend','neutral')
        rsi_v=ind.get('rsi',50)
        desc=(f"EW {ep.replace('_',' ').title()} | {fib_txt} | "
              f"RSI:{rsi_v} | Trend:{tr} | MACD:{'↑' if ind.get('macd_h',0)>0 else '↓'}")
        s['ew_note']=desc
        out.append(s)
    return out[:6]

# ══════════════════════════════════════
#  AI ANALYSIS
# ══════════════════════════════════════
def ai_analysis(buys,ew_list,sells,dsex,stats):
    today=datetime.now(BD_TZ).strftime("%d %B %Y")
    buy_txt=""
    for s in buys[:5]:
        ind=s.get('ind',{})
        buy_txt+=(f"{s['symbol']}: ৳{s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,} "
                  f"Score:{s['score']} RSI:{ind.get('rsi','-')} Trend:{ind.get('trend','-')} "
                  f"EW:{ind.get('ew_phase','-')}\n"
                  f"  Reasons: {', '.join(s.get('reasons',[])[:3])}\n"
                  f"  Warnings: {', '.join(s.get('warnings',[])[:2])}\n")
    ew_txt="\n".join([f"• {s['symbol']}: {s.get('ew_note','')}" for s in ew_list[:4]])
    sell_txt="\n".join([f"• {s['symbol']}: ৳{s['ltp']} ({s['change']:+.1f}%) Score:{s['score']}" for s in sells[:3]])

    prompt=(
        f"তুমি DSE Bangladesh এর একজন professional Technical Analyst। "
        f"তারিখ: {today} | DSEX: {dsex} | Bot Win Rate: {stats['wr']}%\n\n"
        f"=== BUY সিগনাল ===\n{buy_txt}\n"
        f"=== EW Wave Candidates ===\n{ew_txt}\n"
        f"=== SELL সিগনাল ===\n{sell_txt}\n\n"
        f"নিচের format এ বাংলায় বিশ্লেষণ দাও:\n"
        f"1. আজকের মার্কেট সামগ্রিক অবস্থা (২ লাইন)\n"
        f"2. প্রতিটি BUY stock এর জন্য: কেন কিনবেন, কী risk, কখন exit (২-৩ লাইন)\n"
        f"3. আজকের কৌশল ও পরামর্শ (২ লাইন)\n"
        f"সহজ বাংলায়, professional tone।"
    )
    try:
        client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp=client.messages.create(model="claude-sonnet-4-20250514",max_tokens=700,
                                    messages=[{"role":"user","content":prompt}])
        return resp.content[0].text
    except Exception as e:
        return f"⚠️ AI বিশ্লেষণ পাওয়া যায়নি: {str(e)[:100]}"

# ══════════════════════════════════════
#  MESSAGE FORMAT
# ══════════════════════════════════════
def fmt_stock(s,show_detail=True):
    msg=""
    ind=s.get('ind',{})
    rsi_v=ind.get('rsi','-') if ind.get('ok') else '-'
    tr=ind.get('trend','-') if ind.get('ok') else '-'
    ep=ind.get('ew_phase','-') if ind.get('ok') else '-'
    tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
    tp2_pct=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
    fb=" ⚠️FakeBreak" if ind.get('fake_break') else ""
    msg+=f"*{s['symbol']}*{fb} — {s['signal']} (Score:{s['score']})\n"
    msg+=f"💰 `৳{s['ltp']}` ({s['change']:+.1f}%) | Vol:{s['volume']:,}\n"
    msg+=f"H:`{s['high']}` L:`{s['low']}`\n"
    if rsi_v!='-':msg+=f"📊 RSI:`{rsi_v}` | Trend:`{tr}` | EW:`{ep}`\n"
    msg+=f"📥 Entry:`৳{s['entry']}` SL:`৳{s['sl']}`\n"
    msg+=f"🎯 TP1:`৳{s['tp1']}` _(+{tp1_pct}%)_ TP2:`৳{s['tp2']}` _(+{tp2_pct}%)_\n"
    msg+=f"🏷 {' · '.join(s['tags'][:5])}\n"
    if show_detail and s.get('warnings'):
        msg+=f"⚠️ {' | '.join(s['warnings'][:2])}\n"
    msg+="\n"
    return msg

def build_msg(scored,dsex):
    now=datetime.now(BD_TZ).strftime("%d %b %Y %I:%M %p")
    stats=get_stats()
    buys=[s for s in scored if 'BUY' in s['signal']]
    sells=[s for s in scored if 'SELL' in s['signal']][:5]
    ew_list=find_ew([s for s in scored if 'BUY' in s['signal']])
    ai=ai_analysis(buys[:5],ew_list,sells,dsex,stats)

    # Save signals
    for s in buys[:8]:
        save_signal(s['symbol'],s['signal'],s['entry'],s['sl'],
                    s['tp1'],s['tp2'],s['score'],s.get('inds',[]))

    msg=f"🏦 *DSE Signal Bot v3*\n"
    msg+=f"📅 {now} | 📊 DSEX: `{dsex}`\n"
    msg+=f"🧠 Win Rate: `{stats['wr']}%` ({stats['wins']}W/{stats['losses']}L/{stats['pending']}P)\n"
    msg+=f"{'━'*22}\n\n"

    if buys:
        normal=[s for s in buys if s['ltp']>=PENNY_THRESHOLD][:6]
        penny=[s for s in buys if s['ltp']<PENNY_THRESHOLD][:4]
        if normal:
            msg+=f"🟢 *BUY সিগনাল — {len(normal)} টি*\n\n"
            for s in normal:msg+=fmt_stock(s)
        if penny:
            msg+=f"💎 *Penny BUY — {len(penny)} টি* _(বেশি ঝুঁকি)_\n\n"
            for s in penny:msg+=fmt_stock(s)
    else:
        msg+="🟡 আজ BUY সিগনাল নেই\n\n"

    if sells:
        msg+=f"🔴 *SELL — {len(sells)} টি*\n"
        for s in sells:
            msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Score:{s['score']}\n"
        msg+="\n"

    if ew_list:
        msg+="🌊 *EW Wave 2/4 — Strongest BUY*\n\n"
        for s in ew_list:
            pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
            msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%)\n"
            msg+=f"TP1:`৳{s['tp1']}` _(+{pct}%)_ | _{s.get('ew_note','')}_\n\n"

    msg+=f"{'━'*22}\n🤖 *AI বিশ্লেষণ*\n{ai}\n\n"
    msg+=f"💬 _যেকোনো স্টক সম্পর্কে message করুন_\n"
    msg+="⚠️ _Stop Loss সবসময় ব্যবহার করুন। বিনিয়োগে ঝুঁকি আছে।_"
    return msg

# ══════════════════════════════════════
#  OUTCOME CHECKER
# ══════════════════════════════════════
async def check_outcomes(bot):
    log.info("Outcome check...")
    try:
        conn=sqlite3.connect(DB_PATH);c=conn.cursor()
        today=datetime.now(BD_TZ).strftime('%Y-%m-%d')
        pending=c.execute('SELECT id,symbol,entry,tp1,sl FROM signals WHERE outcome="pending" AND check_date<=?',(today,)).fetchall()
        conn.close()
        if not pending:return
        stocks=fetch_stocks()
        pm={s['symbol']:s['ltp'] for s in stocks}
        report="📊 *Signal Outcome*\n\n";w=l=0
        for sid,sym,entry,tp1,sl in pending:
            cur=pm.get(sym)
            if not cur:continue
            pct=round((cur-entry)/entry*100,2)
            if cur>=tp1:
                conn=sqlite3.connect(DB_PATH);c=conn.cursor()
                c.execute('UPDATE signals SET outcome="win",outcome_pct=? WHERE id=?',(pct,sid))
                conn.commit();conn.close()
                report+=f"✅ *{sym}* TP1 Hit! +{pct}%\n";w+=1
            elif cur<=sl:
                conn=sqlite3.connect(DB_PATH);c=conn.cursor()
                c.execute('UPDATE signals SET outcome="loss",outcome_pct=? WHERE id=?',(pct,sid))
                conn.commit();conn.close()
                report+=f"❌ *{sym}* SL Hit! {pct}%\n";l+=1
        if w+l>0:
            report+=f"\n✅{w} ❌{l} | 🧠 Bot learning updated!"
            await bot.send_message(chat_id=CHAT_ID,text=report,parse_mode='Markdown')
    except Exception as e:
        log.error(f"Outcome check error:{e}")

# ══════════════════════════════════════
#  AI CHAT HANDLER
# ══════════════════════════════════════
async def handle_message(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    text=update.message.text
    if text.startswith('/'):return
    stats=get_stats()
    stocks=fetch_stocks()
    ctx_txt=""
    for s in stocks:
        if s['symbol'].upper() in text.upper():
            ind=get_all_indicators(s['symbol'])
            ctx_txt=(f"\n{s['symbol']} Live Data:\n"
                    f"Price:৳{s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
                    f"H:{s['high']} L:{s['low']} Yesterday:৳{s['yday']}\n")
            if ind['ok']:
                ctx_txt+=(f"RSI:{ind['rsi']} MACD:{'↑' if ind['macd_h']>0 else '↓'} "
                         f"BB:{ind['bb_pos']} Trend:{ind['trend']} EW:{ind['ew_phase']}\n"
                         f"MA20:{ind['ma20']} MA50:{ind['ma50']} EMA9:{ind['ema9']}\n"
                         f"SwingHigh:{ind['swing_high']} SwingLow:{ind['swing_low']}\n"
                         f"FakeBreak:{ind['fake_break']}\n")
            break
    prompt=(
        f"তুমি DSE Bangladesh expert analyst। বাংলায় সংক্ষিপ্ত ও সহজ উত্তর দাও।\n"
        f"Bot stats: WinRate:{stats['wr']}% Total:{stats['total']}\n"
        f"{ctx_txt}\nUser: {text}\n\n"
        f"৬-৮ লাইনে উত্তর দাও। Technical analysis ও practical পরামর্শ দাও।"
    )
    try:
        client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp=client.messages.create(model="claude-sonnet-4-20250514",max_tokens=500,
                                    messages=[{"role":"user","content":prompt}])
        await update.message.reply_text(resp.content[0].text)
    except Exception as e:
        await update.message.reply_text(f"সমস্যা: {str(e)[:100]}")

# ══════════════════════════════════════
#  SEND SIGNALS
# ══════════════════════════════════════
async def send_signals(bot):
    log.info("Signal job শুরু...")
    await bot.send_message(chat_id=CHAT_ID,text="⏳ SMC+EW+RSI+MACD+BB+MA+EMA+Candle+FakeBreak বিশ্লেষণ চলছে...")
    try:
        stocks=fetch_stocks()
        if not stocks:
            await bot.send_message(chat_id=CHAT_ID,text="❌ ডেটা নেই।\n• DSE বন্ধ (শুক্র/শনি)?\n• Trading hour শেষ?")
            return
        dsex=get_dsex()
        scored=analyze(stocks,use_hist=True)
        msg=build_msg(scored,dsex)
        for i in range(0,len(msg),4000):
            await bot.send_message(chat_id=CHAT_ID,text=msg[i:i+4000],parse_mode='Markdown')
        log.info(f"✅ {len(stocks)} stocks analyzed")
    except Exception as e:
        log.error(f"Error:{e}")
        await bot.send_message(chat_id=CHAT_ID,text=f"❌ সমস্যা:\n{e}")

# ══════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════
async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "🏦 *DSE Signal Bot v3 — Pro*\n\n"
        "🔬 *Analysis Engine:*\n"
        "✅ SMC (Order Block, Liquidity)\n"
        "✅ Elliott Wave (Wave 2/4 detection)\n"
        "✅ RSI + MACD + Bollinger Bands\n"
        "✅ MA20, MA50, EMA9, EMA21\n"
        "✅ Candlestick Patterns (10+ types)\n"
        "✅ Fibonacci (6 levels)\n"
        "✅ Fake Breakout Detection\n"
        "✅ Volume Spike Analysis\n"
        "✅ Trend Alignment Score\n"
        "✅ TP1 min 8%, TP2 min 20%\n\n"
        "📌 *Commands:*\n"
        "/signal — পূর্ণ বিশ্লেষণ\n"
        "/stats — Performance দেখুন\n"
        "/top — Top Gainers\n"
        "/sell — Sell সিগনাল\n"
        "/ew — EW Wave 2/4\n"
        "/penny — Penny stocks\n\n"
        "💬 *যেকোনো স্টক সম্পর্কে message করুন!*\n"
        "_যেমন: GP কি কিনব? BRACBANK analysis দাও_\n\n"
        "🕕 প্রতিদিন সন্ধ্যা ৬টায় auto signal\n"
        "⚠️ _বিনিয়োগে ঝুঁকি আছে_",parse_mode='Markdown')

async def cmd_signal(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("⏳ সম্পূর্ণ বিশ্লেষণ চলছে (২-৩ মিনিট লাগতে পারে)...")
    await send_signals(ctx.bot)

async def cmd_stats(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    stats=get_stats()
    msg=f"📊 *Bot Performance*\n\n"
    msg+=f"মোট Signal: `{stats['total']}`\n"
    msg+=f"✅ Win: `{stats['wins']}` | ❌ Loss: `{stats['losses']}`\n"
    msg+=f"⏳ Pending: `{stats['pending']}`\n"
    msg+=f"🎯 Win Rate: `{stats['wr']}%`\n"
    msg+=f"📈 Avg Return: `{stats['avg']}%`\n\n"
    if stats['recent']:
        msg+="🕐 *সাম্প্রতিক:*\n"
        for sym,entry,tp1,outcome,pct,date in stats['recent'][:6]:
            ic="✅" if outcome=='win' else "❌" if outcome=='loss' else "⏳"
            msg+=f"{ic} {sym} ({date}) ৳{entry} {outcome} {pct:+.1f}%\n"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_top(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔥 লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    top=sorted(stocks,key=lambda x:x['change'],reverse=True)[:12]
    msg="🔥 *Top 12 Gainers*\n_(Circuit ও কম volume বাদ)_\n\n"
    for i,s in enumerate(top,1):
        p="💎" if s['ltp']<PENNY_THRESHOLD else "  "
        msg+=f"{i}.{p}*{s['symbol']}* `৳{s['ltp']}` (+{s['change']:.1f}%) Vol:{s['volume']:,}\n"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_sell(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔴 লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    scored=analyze(stocks,use_hist=False)
    sells=[s for s in scored if 'SELL' in s['signal']]
    if not sells:await u.message.reply_text("আজ SELL সিগনাল নেই।");return
    msg="🔴 *SELL সিগনাল*\n\n"
    for s in sells[:8]:
        msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Score:{s['score']}\n"
        msg+=f"🏷 {' · '.join(s['tags'][:3])}\n"
        if s.get('warnings'):msg+=f"⚠️ {s['warnings'][0]}\n"
        msg+="\n"
    msg+="⚠️ _বিনিয়োগে ঝুঁকি আছে।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_ew(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🌊 EW স্ক্যান চলছে (historical data আনছি)...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    scored=analyze(stocks,use_hist=True)
    ew_list=find_ew([s for s in scored if 'BUY' in s['signal']])
    if not ew_list:await u.message.reply_text("আজ EW Wave 2/4 candidate নেই।");return
    msg="🌊 *EW Wave 2/4 — সবচেয়ে শক্তিশালী BUY*\n\n"
    for s in ew_list:
        tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
        tp2_pct=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
        msg+=fmt_stock(s)
        msg+=f"🌊 _{s.get('ew_note','')}_\n\n"
    msg+="⚠️ _Stop Loss সবসময় ব্যবহার করুন।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_penny(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("💎 Penny Stock বিশ্লেষণ চলছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    penny=[s for s in stocks if s['ltp']<PENNY_THRESHOLD]
    if not penny:await u.message.reply_text("আজ penny stock নেই।");return
    scored=analyze(penny,use_hist=False)
    buys=[s for s in scored if 'BUY' in s['signal']]
    if not buys:await u.message.reply_text("আজ penny BUY নেই।");return
    msg="💎 *Penny Stock BUY*\n_(৳১০ এর নিচে — বেশি ঝুঁকি, বেশি সুযোগ)_\n\n"
    for s in buys[:8]:msg+=fmt_stock(s,show_detail=True)
    msg+="⚠️ _Penny stock এ ছোট amount ব্যবহার করুন।_"
    await u.message.reply_text(msg,parse_mode='Markdown')

# ══════════════════════════════════════
#  SCHEDULER + MAIN
# ══════════════════════════════════════
async def post_init(app):
    init_db()
    sched=AsyncIOScheduler(timezone='UTC')
    sched.add_job(send_signals,'cron',hour=12,minute=0,args=[app.bot])
    sched.add_job(check_outcomes,'cron',hour=4,minute=0,args=[app.bot])
    sched.start()
    log.info("✅ Scheduler: Signal UTC 12:00 | Check UTC 04:00")

def main():
    init_db()
    log.info("🚀 DSE Signal Bot v3 Pro চালু হচ্ছে...")
    app=Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CommandHandler("top",    cmd_top))
    app.add_handler(CommandHandler("sell",   cmd_sell))
    app.add_handler(CommandHandler("ew",     cmd_ew))
    app.add_handler(CommandHandler("penny",  cmd_penny))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_message))
    log.info("✅ Bot v3 Pro polling শুরু")
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__':
    main()
