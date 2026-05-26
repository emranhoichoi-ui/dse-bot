import os,logging,requests,re,time,json,sqlite3,csv,io
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
GITHUB_TOKEN=os.environ.get('GITHUB_TOKEN','')
BD_TZ=pytz.timezone('Asia/Dhaka')
HEADERS={'User-Agent':'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120'}
GITHUB_RAW='https://raw.githubusercontent.com/emranhoichoi-ui/dse-bot/main/data'
GITHUB_API='https://api.github.com/repos/emranhoichoi-ui/dse-bot/contents/data'

MIN_PRICE=1.0
PENNY_THRESHOLD=10.0
MIN_VOLUME=20000
MAX_CHANGE=15.0
TP1_MIN=0.08
TP2_MIN=0.20
DB_PATH='/tmp/dse_v4.db'
SEP='='*26
_cache={}

# ══════════════════════
#  DATABASE
# ══════════════════════
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
        c.execute('''INSERT INTO signals(date,symbol,signal_type,entry,sl,tp1,tp2,score,
                     indicators,check_date,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                  (now.strftime('%Y-%m-%d'),sym,sig,entry,sl,tp1,tp2,
                   score,json.dumps(inds),chk,now.isoformat()))
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

# ══════════════════════
#  GITHUB DATA
# ══════════════════════
def get_hist(symbol):
    global _cache
    key=f"{symbol}_{datetime.now(BD_TZ).strftime('%Y%m%d')}"
    if key in _cache:return _cache[key]
    try:
        r=requests.get(f"{GITHUB_RAW}/{symbol}.csv",headers={'User-Agent':'Mozilla/5.0'},timeout=15)
        if r.status_code!=200:return None
        rows=list(csv.DictReader(io.StringIO(r.text)))
        if not rows:return None
        def fv(col):return[float(row[col]) for row in rows if row.get(col) and row[col].strip()]
        data={
            'closes':fv('Close'),'highs':fv('High'),'lows':fv('Low'),
            'opens':fv('Open'),'vols':fv('Volume'),
            'dates':[row['Date'] for row in rows if row.get('Date')]
        }
        _cache[key]=data
        return data
    except Exception as e:
        log.error(f"GitHub {symbol}: {e}");return None

# ══════════════════════
#  MATH HELPERS
# ══════════════════════
def sf(txt):
    try:return float(str(txt).strip().replace(',','').replace('%',''))
    except:return 0.0

def ema(data,p):
    if not data or len(data)<p:return data[-1] if data else 0
    k=2/(p+1);v=sum(data[:p])/p
    for x in data[p:]:v=x*k+v*(1-k)
    return round(v,2)

def rsi(closes,p=14):
    if len(closes)<p+1:return 50.0
    g,l=[],[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1];g.append(max(d,0));l.append(max(-d,0))
    ag=sum(g[-p:])/p;al=sum(l[-p:])/p
    if al==0:return 100.0
    return round(100-(100/(1+ag/al)),1)

def macd(closes):
    if len(closes)<26:return 0,0,0
    ml=ema(closes,12)-ema(closes,26);sl=ml*0.9
    return round(ml,3),round(sl,3),round(ml-sl,3)

def bb(closes,p=20):
    if len(closes)<p:return 0,0,0
    r=closes[-p:];mid=sum(r)/p
    std=(sum((x-mid)**2 for x in r)/p)**0.5
    return round(mid+2*std,2),round(mid,2),round(mid-2*std,2)

def sma(closes,p):
    if len(closes)<p:return closes[-1] if closes else 0
    return round(sum(closes[-p:])/p,2)

# ══════════════════════
#  ELLIOTT WAVE (Improved)
# ══════════════════════
def detect_ew(closes,highs,lows):
    """
    Proper EW detection using:
    1. Multi-timeframe trend comparison
    2. Fibonacci retracement levels
    3. Wave structure analysis
    """
    if len(closes)<20:return 'unknown','N/A'

    last=closes[-1]

    # Short/Medium/Long term averages
    avg5=sum(closes[-5:])/5
    avg10=sum(closes[-10:-5])/5
    avg20=sum(closes[-20:-10])/10

    # 50-day swing for Fibonacci
    lookback=min(50,len(closes))
    sh=max(highs[-lookback:])
    sl=min(lows[-lookback:])
    rng=sh-sl if sh>sl else 1

    # Fibonacci levels
    f236=sl+rng*0.236
    f382=sl+rng*0.382
    f500=sl+rng*0.500
    f618=sl+rng*0.618
    f786=sl+rng*0.786

    # Near Fibonacci check
    near_fib='none'
    for fval,flbl in[(f618,'0.618'),(f382,'0.382'),(f500,'0.500'),(f786,'0.786'),(f236,'0.236')]:
        if last>0 and abs(last-fval)/last<0.025:
            near_fib=flbl;break

    # Wave detection logic
    # Wave 3/5: Strong impulse up - price above all averages
    if last>avg5>avg10>avg20:
        phase='Wave 3/5 (Impulse Up)'
        desc='Strong uptrend - Wave 3 ba 5 cholche'
        if near_fib!='none':desc+=f' | Fib {near_fib} te support'
        return phase,desc

    # Wave 2/4 End: Correction ending - price bouncing from lows
    elif avg10>avg5 and last>avg5 and last>avg10*0.95:
        phase='Wave 2/4 Shesh'
        desc=f'Correction shesh hocche - Bounce shuru'
        if near_fib!='none':desc+=f' | Fib {near_fib} te support niyeche'
        return phase,desc

    # Wave 2/4: Active correction
    elif last<avg5<avg10 and avg20>avg10:
        phase='Wave 2/4 (Correction)'
        desc='Correction cholche - abhi entry kora thik na'
        if near_fib!='none':desc+=f' | Fib {near_fib} te support pache'
        return phase,desc

    # Wave 5 End / Possible reversal
    elif last>avg5>avg10 and rsi(closes)>75:
        phase='Wave 5 Shesh?'
        desc='Uptrend shesh hote pare - RSI overbought'
        return phase,desc

    # Downtrend
    elif last<avg5<avg10<avg20:
        phase='Downtrend'
        desc='Strong downtrend - avoid korun'
        return phase,desc

    else:
        phase='Neutral'
        desc='Kono clear wave pattern nei'
        if near_fib!='none':desc+=f' | Fib {near_fib} te ache'
        return phase,desc

# ══════════════════════
#  FULL INDICATORS
# ══════════════════════
def get_ind(symbol):
    data=get_hist(symbol)
    if not data or len(data['closes'])<20:
        return{'ok':False,'rsi':50,'macd_h':0,'bb_pos':'mid',
               'ma20':0,'ma50':0,'ema9':0,'ema21':0,'trend':'neutral',
               'vol_ratio':0,'swing_high':0,'swing_low':0,
               'ew_phase':'unknown','ew_desc':'N/A',
               'fake_break':False,'candle':'N/A','candle_score':0,
               'base_days':0,'fib_level':'none',
               'trend_ok':True}  # trend_ok = allow signal

    closes=data['closes'];highs=data['highs']
    lows=data['lows'];opens=data['opens'];vols=data['vols']

    r=rsi(closes)
    ml,sl_,mh=macd(closes)
    bbu,bbm,bbl=bb(closes)
    ma20=sma(closes,20);ma50=sma(closes,min(50,len(closes)))
    e9=ema(closes,min(9,len(closes)));e21=ema(closes,min(21,len(closes)))
    last=closes[-1]
    bp='upper' if last>bbu else 'lower' if last<bbl else 'mid'

    # Trend
    if e9>e21>ma20>ma50:trend='strong_up'
    elif e9>e21 and ma20>ma50:trend='up'
    elif e9<e21<ma20<ma50:trend='strong_down'
    elif e9<e21 and ma20<ma50:trend='down'
    else:trend='neutral'

    # ══ DOWNTREND FILTER (new!) ══
    # MA50 niche thakle ba strong downtrend e BUY signal debo na
    trend_ok=trend not in('strong_down','down')

    # Volume
    avg_vol=sum(vols[-20:])/max(len(vols[-20:]),1) if vols else 0
    vol_ratio=round((vols[-1] if vols else 0)/avg_vol,2) if avg_vol>0 else 0

    # Swing
    lb=min(50,len(closes))
    sh=max(highs[-lb:]);sl=min(lows[-lb:])

    # EW Detection (improved)
    ew_phase,ew_desc=detect_ew(closes,highs,lows)

    # Fake breakout
    fake_break=False
    if len(highs)>=6:
        prev_high=max(highs[-6:-1])
        if last>prev_high and r>78:fake_break=True

    # Range-bound detection (new!)
    range_bound=False
    if len(highs)>=120:
        rng_120=max(highs[-120:])-min(lows[-120:])
        avg_120=sum(closes[-120:])/120
        if avg_120>0 and rng_120/avg_120<0.30:range_bound=True

    # Multi-candle pattern
    candle='none';cs=0
    if len(closes)>=5 and len(opens)>=5:
        c0,c1,c2=closes[-3],closes[-2],closes[-1]
        o0,o1,o2=opens[-3],opens[-2],opens[-1]
        h0,h1,h2=highs[-3],highs[-2],highs[-1]
        l0,l1,l2=lows[-3],lows[-2],lows[-1]
        b0=abs(c0-o0);b1=abs(c1-o1);b2=abs(c2-o2)
        rng2=h2-l2 if h2>l2 else 0.01
        bull2=c2>o2;bull1=c1>o1;bull0=c0>o0
        uw2=(h2-max(c2,o2))/rng2;lw2=(min(c2,o2)-l2)/rng2
        pd=closes[-5]>closes[-3] if len(closes)>=5 else False

        if lw2>0.5 and uw2<0.1 and bull2 and pd:candle='Real Hammer';cs=6
        elif lw2>0.5 and uw2<0.1 and bull2:candle='Hammer';cs=3
        elif not bull1 and bull2 and c2>o1 and o2<c1 and b2>b1:candle='Bullish Engulfing';cs=6
        elif not bull0 and b1<b0*0.3 and bull2 and c2>((c0+o0)/2):candle='Morning Star';cs=7
        elif bull0 and bull1 and bull2 and c2>c1>c0:candle='3 White Soldiers';cs=8
        elif uw2>0.5 and lw2<0.1 and not bull2:candle='Shooting Star';cs=-6
        elif bull0 and b1<b0*0.3 and not bull2 and c2<((c0+o0)/2):candle='Evening Star';cs=-7
        elif bull1 and not bull2 and o2>c1 and c2<o1 and b2>b1:candle='Bearish Engulfing';cs=-6
        elif not bull0 and not bull1 and not bull2 and c2<c1<c0:candle='3 Black Crows';cs=-8
        elif b2<rng2*0.1:candle='Doji';cs=0
        elif bull2 and b2>rng2*0.6:candle='Strong Bull';cs=2

    # Base
    base_days=0
    if len(highs)>=30:
        rng30=max(highs[-30:])-min(lows[-30:])
        avg30=sum(closes[-30:])/30
        if avg30>0 and rng30/avg30<0.12:base_days=30

    # Fibonacci near current price
    rng_fib=sh-sl
    fib_level='none'
    for fv_,fl in[(sl+rng_fib*0.618,'0.618'),(sl+rng_fib*0.382,'0.382'),
                  (sl+rng_fib*0.500,'0.500'),(sl+rng_fib*0.786,'0.786'),(sl+rng_fib*0.236,'0.236')]:
        if last>0 and abs(last-fv_)/last<0.025:fib_level=fl;break

    return{
        'ok':True,'rsi':r,'macd':ml,'macd_sig':sl_,'macd_h':mh,
        'bb_upper':bbu,'bb_mid':bbm,'bb_lower':bbl,'bb_pos':bp,
        'ma20':ma20,'ma50':ma50,'ema9':e9,'ema21':e21,
        'trend':trend,'trend_ok':trend_ok,
        'vol_ratio':vol_ratio,'swing_high':sh,'swing_low':sl,
        'ew_phase':ew_phase,'ew_desc':ew_desc,
        'fake_break':fake_break,'range_bound':range_bound,
        'candle':candle,'candle_score':cs,
        'base_days':base_days,'fib_level':fib_level,
    }

# ══════════════════════
#  BREAKOUT SCANNER (Fixed)
# ══════════════════════
def scan_breakouts(stocks):
    candidates=[]
    for s in stocks:
        if s['ltp']<MIN_PRICE or s['volume']<MIN_VOLUME:continue
        ind=get_ind(s['symbol'])
        if not ind['ok']:continue

        # ══ DOWNTREND FILTER ══
        if not ind['trend_ok']:continue  # downtrend e skip
        if ind['range_bound'] and ind['vol_ratio']<4:continue  # range-bound, low vol skip

        score=0;sigs=[];reasons=[]
        ltp=s['ltp'];vr=ind['vol_ratio']

        # 1. Volume
        if vr>=5:score+=8;sigs.append(f"Vol {vr}x")
        elif vr>=3:score+=6;sigs.append(f"Vol {vr}x")
        elif vr>=2:score+=4;sigs.append(f"Vol {vr}x")
        elif vr>=1.5:score+=2;sigs.append(f"Vol {vr}x")
        else:continue

        # 2. Base breakout
        if ind['base_days']>0:
            score+=5;sigs.append(f"Base {ind['base_days']}d")
            reasons.append(f"Consolidation shesh - parabolic move shomvob")

        # 3. Swing high break
        sh=ind['swing_high']
        if sh>0 and ltp>=sh*0.98:
            score+=4;sigs.append("Swing Break")
            reasons.append(f"50-diner high {sh} break hoyeche")

        # 4. MA/EMA Trend
        tr=ind['trend']
        if tr=='strong_up':
            score+=5;sigs.append("EMA>MA Perfect")
            reasons.append("Perfect MA alignment - strong uptrend")
        elif tr=='up':score+=3;sigs.append("Trend Up")

        # 5. RSI (not overbought for breakout)
        r=ind['rsi']
        if 50<=r<=68:
            score+=4;sigs.append(f"RSI:{r} OK")
            reasons.append(f"RSI {r} - breakout zone, aro upore jawar space ache")
        elif 45<=r<50:score+=2;sigs.append(f"RSI:{r}")
        elif r>75:score-=4;sigs.append(f"RSI:{r} Overbought!")  # penalize more

        # 6. MACD
        if ind['macd_h']>0 and ind['macd']>ind['macd_sig']:
            score+=3;sigs.append("MACD Bull")
        elif ind['macd_h']>0:score+=1;sigs.append("MACD Up")
        elif ind['macd_h']<0:score-=2;sigs.append("MACD Bear")

        # 7. EW Phase
        ep=ind['ew_phase']
        if 'Wave 3/5' in ep:
            score+=4;sigs.append("EW Impulse Up")
            reasons.append(ind['ew_desc'])
        elif 'Wave 2/4 Shesh' in ep:
            score+=5;sigs.append("EW W2/4 End")
            reasons.append(ind['ew_desc'])
        elif 'Downtrend' in ep:
            score-=5;sigs.append("EW Downtrend!")
        elif 'Correction' in ep:
            score-=2;sigs.append("EW Correction")

        # 8. Candle
        cs=ind['candle_score']
        if cs>=4:score+=cs;sigs.append(ind['candle'])
        elif cs>0:score+=cs;sigs.append(ind['candle'])
        elif cs<=-4:score+=cs;sigs.append(ind['candle'])

        # 9. Fibonacci
        if ind['fib_level']!='none':
            score+=2;sigs.append(f"Fib {ind['fib_level']}")
            reasons.append(f"Fibonacci {ind['fib_level']} te support")

        # 10. BB
        if ind['bb_pos']=='lower':score+=2;sigs.append("BB Lower")
        elif ind['bb_pos']=='upper' and vr>3:score+=1;sigs.append("BB Upper+Vol")

        # Fake breakout penalty
        if ind['fake_break']:score-=5;sigs.append("FAKE BREAK!")
        if ind['range_bound']:sigs.append("Range-bound")

        if score<10:continue

        sl=round(ind['swing_low']*0.99 if ind['swing_low']>0 else ltp*0.93,2)
        risk=ltp-sl;risk=risk if risk>0 else ltp*0.05
        tp1=round(max(ltp*(1+TP1_MIN),ltp+risk*2),2)
        tp2=round(max(ltp*(1+TP2_MIN),ltp+risk*4),2)
        tp3=round(max(ltp*1.50,ltp+risk*6),2)

        candidates.append({
            **s,'score':score,'sigs':sigs,'reasons':reasons,'ind':ind,
            'entry':ltp,'sl':sl,'tp1':tp1,'tp2':tp2,'tp3':tp3,
            'vr':vr,'candle':ind['candle'],'rsi':r,'trend':tr,
            'ew_phase':ind['ew_phase'],'ew_desc':ind['ew_desc'],
        })

    candidates.sort(key=lambda x:x['score'],reverse=True)
    return candidates[:8]

# ══════════════════════
#  DSE LIVE
# ══════════════════════
def fetch_stocks():
    log.info("DSE fetch...")
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
        log.info(f"{len(unique)} stocks");return unique
    except Exception as e:
        log.error(f"Fetch: {e}");return[]

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

# ══════════════════════
#  STANDARD ANALYSIS (Fixed)
# ══════════════════════
def analyze(stocks,use_hist=False):
    scored=[]
    for s in stocks:
        ltp=s['ltp'];hi=s['high'];lo=s['low']
        chg=s['change'];vol=s['volume'];yd=s['yday']
        rng=hi-lo if hi>lo else ltp*0.01
        cp_pos=(ltp-lo)/rng;uw=(hi-ltp)/rng;lw=(ltp-lo)/rng
        score=0.0;tags=[];inds=[];warnings=[]

        ind={'ok':False,'rsi':50,'trend':'neutral','ew_phase':'unknown','ew_desc':'N/A',
             'fake_break':False,'candle':'N/A','candle_score':0,'macd_h':0,
             'bb_pos':'mid','vol_ratio':0,'macd':0,'macd_sig':0,'trend_ok':True,
             'fib_level':'none','range_bound':False}

        if use_hist:
            ind=get_ind(s['symbol'])

        # ══ DOWNTREND FILTER ══
        if use_hist and not ind['trend_ok']:
            # downtrend e HOLD force kori
            s.update({'score':-5,'signal':'HOLD (Downtrend)','tags':['Downtrend - avoid'],
                      'entry':ltp,'sl':round(lo*0.99,2),
                      'tp1':round(ltp*1.08,2),'tp2':round(ltp*1.20,2),'ind':ind,'inds':[],'warnings':['Downtrend - avoid']})
            scored.append(s);continue

        if ind['ok']:
            # Candle pattern (multi-day)
            cs=ind['candle_score']
            if cs>=6:score+=cs;tags.append(ind['candle'])
            elif cs>=3:score+=cs;tags.append(ind['candle'])
            elif cs>0:score+=cs;tags.append(ind['candle'])
            elif cs<=-6:score+=cs;tags.append(ind['candle'])
            elif cs<0:score+=cs;tags.append(ind['candle'])

            # RSI
            r=ind['rsi']
            if r<30:score+=4;tags.append(f"RSI:{r} Oversold");inds.append('rsi_os')
            elif 30<=r<45:score+=3;tags.append(f"RSI:{r}");inds.append('rsi_good')
            elif 45<=r<65:score+=1;tags.append(f"RSI:{r}")
            elif r>=75:score-=3;tags.append(f"RSI:{r} OB");warnings.append(f"RSI {r} overbought")
            else:tags.append(f"RSI:{r}")

            # MACD
            if ind['macd_h']>0 and ind['macd']>ind['macd_sig']:
                score+=3;tags.append("MACD Bull");inds.append('macd_bull')
            elif ind['macd_h']<0 and ind['macd']<ind['macd_sig']:
                score-=3;tags.append("MACD Bear")

            # BB
            if ind['bb_pos']=='lower':score+=3;tags.append("BB Lower");inds.append('bb_low')
            elif ind['bb_pos']=='upper':score-=2;tags.append("BB Upper")
            else:tags.append("BB Mid")

            # Trend
            tr=ind['trend']
            if tr=='strong_up':score+=5;tags.append("Trend StrongUp");inds.append('t_sup')
            elif tr=='up':score+=2;tags.append("Trend Up")
            elif tr=='strong_down':score-=5;tags.append("Trend StrongDown")
            elif tr=='down':score-=2;tags.append("Trend Down")

            # EW Phase
            ep=ind['ew_phase']
            if 'Wave 2/4 Shesh' in ep:score+=5;tags.append("EW Bounce!");inds.append('ew_b')
            elif 'Wave 3/5' in ep:score+=3;tags.append("EW Impulse");inds.append('ew_i')
            elif 'Downtrend' in ep:score-=4;tags.append("EW Down")
            elif 'Correction' in ep:score-=2;tags.append("EW Corr")

            # Volume ratio
            vr=ind['vol_ratio']
            if vr>=3:score+=3;tags.append(f"Vol {vr}x")
            elif vr>=2:score+=2;tags.append(f"Vol {vr}x")
            elif vr>=1.5:score+=1;tags.append(f"Vol {vr}x")

            # Fibonacci
            if ind['fib_level']!='none':
                score+=2;tags.append(f"Fib {ind['fib_level']}")

            # Range-bound penalty
            if ind['range_bound']:
                score-=2;warnings.append("Range-bound stock - sideway cholche")

            # Fake break
            if ind['fake_break']:score-=5;tags.append("FakeBreak!");warnings.append("Fake breakout risk")

        else:
            # No historical data - basic analysis only
            if lw>0.4 and uw<0.15 and cp_pos>0.6:score+=2;tags.append("Bull Shape")
            elif uw>0.4 and lw<0.15:score-=2;tags.append("Bear Shape")
            if vol>3000000:score+=3;tags.append(f"Vol:{vol//1000}K")
            elif vol>1000000:score+=2;tags.append(f"Vol:{vol//1000}K")
            elif vol>300000:score+=1;tags.append(f"Vol:{vol//1000}K")
            else:tags.append(f"Vol:{vol//1000}K")

        # Price change
        if 1.5<chg<=5:score+=2;tags.append(f"+{chg:.1f}%")
        elif chg>5:score+=1;tags.append(f"+{chg:.1f}%")
        elif chg<-3:score-=2;tags.append(f"{chg:.1f}%")
        elif chg<-1:score-=1;tags.append(f"{chg:.1f}%")

        # Gap
        if yd>0:
            gap=(ltp-yd)/yd*100
            if gap>1.5:score+=1;tags.append("Gap Up")
            elif gap<-1.5:score-=1;tags.append("Gap Down")

        s['ind']=ind;s['inds']=inds;s['warnings']=warnings

        if score>=12:signal="STRONG BUY"
        elif score>=7:signal="BUY"
        elif score<=-12:signal="STRONG SELL"
        elif score<=-7:signal="SELL"
        else:signal="HOLD"

        sl=round(lo*0.993,2);risk=ltp-sl
        if risk<=0:risk=ltp*0.04
        tp1=round(max(ltp*(1+TP1_MIN),ltp+risk*2.5),2)
        tp2=round(max(ltp*(1+TP2_MIN),ltp+risk*4.5),2)
        s.update({'score':round(score,1),'signal':signal,'tags':tags,'entry':ltp,'sl':sl,'tp1':tp1,'tp2':tp2})
        scored.append(s)

    scored.sort(key=lambda x:x['score'],reverse=True)
    return scored

# ══════════════════════
#  AI
# ══════════════════════
def ai_call(prompt):
    try:
        client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp=client.messages.create(model="claude-sonnet-4-20250514",max_tokens=500,
                                    messages=[{"role":"user","content":prompt}])
        return resp.content[0].text
    except Exception as e:
        return f"AI credit shesh. Notun key lagbe."

def ai_summary(buys,breakouts,dsex,stats):
    today=datetime.now(BD_TZ).strftime("%d %B %Y")
    buy_txt=""
    for s in buys[:4]:
        ind=s.get('ind',{})
        buy_txt+=(f"{s['symbol']}: {s['ltp']} ({s['change']:+.1f}%) "
                 f"RSI:{ind.get('rsi','-')} Trend:{ind.get('trend','-')} "
                 f"EW:{ind.get('ew_phase','-')} Candle:{ind.get('candle','-')}\n")
    brk_txt=""
    for s in breakouts[:3]:
        brk_txt+=f"{s['symbol']}: Vol {s['vr']}x EW:{s['ew_phase']}\n"
    prompt=(
        f"Tumi DSE Bangladesh expert analyst. Banglay sohoj vashay uttor dao.\n"
        f"Tarikh:{today} DSEX:{dsex} WinRate:{stats['wr']}%\n\n"
        f"BUY signal:\n{buy_txt}\n"
        f"Breakout:\n{brk_txt}\n\n"
        f"Protita BUY stock er jonno 2 laine: keno kinben, ki risk, kothay exit. "
        f"Sheshe 2 laine aj DSE market er obostha o koushol."
    )
    return ai_call(prompt)

# ══════════════════════
#  MESSAGE (Clear Layout)
# ══════════════════════
def fmt_brk(s):
    tp1p=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
    tp2p=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
    tp3p=round((s['tp3']-s['ltp'])/s['ltp']*100,1)
    lines=[
        f">>> {s['symbol']} | Score:{s['score']} <<<",
        f"Daam: {s['ltp']} ({s['change']:+.1f}%) | Vol:{s['volume']:,} ({s['vr']}x avg)",
        f"RSI:{s['rsi']} | Trend:{s['trend']} | Candle:{s['candle']}",
        f"EW: {s['ew_phase']}",
        f"   {s['ew_desc']}" if s['ew_desc']!='N/A' else "",
    ]
    if s.get('ind',{}).get('fib_level','none')!='none':
        lines.append(f"Fibonacci {s['ind']['fib_level']} te support")
    if s['ind'].get('base_days',0)>0:
        lines.append(f"Base: {s['ind']['base_days']} din consolidation")
    lines+=[
        f"",
        f"Entry : {s['entry']}",
        f"SL    : {s['sl']}",
        f"TP1   : {s['tp1']} (+{tp1p}%)",
        f"TP2   : {s['tp2']} (+{tp2p}%)",
        f"TP3   : {s['tp3']} (+{tp3p}%) [Parabolic]",
        f"Signals: {' | '.join(s['sigs'][:5])}",
    ]
    if s.get('reasons'):lines.append(f"Karon: {s['reasons'][0]}")
    if s['ind'].get('range_bound'):lines.append("!! Range-bound stock - shotorko thakun")
    lines.append("")
    return "\n".join([l for l in lines if l is not None])

def fmt_sig(s):
    ind=s.get('ind',{})
    r=ind.get('rsi','-') if ind.get('ok') else '-'
    tr=ind.get('trend','-') if ind.get('ok') else '-'
    cp=ind.get('candle','') if ind.get('ok') else ''
    ep=ind.get('ew_phase','') if ind.get('ok') else ''
    fib=ind.get('fib_level','none') if ind.get('ok') else 'none'
    tp1p=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
    tp2p=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
    lines=[
        f">> {s['symbol']} | {s['signal']} | Score:{s['score']}",
        f"   Daam: {s['ltp']} ({s['change']:+.1f}%) | Vol:{s['volume']:,}",
    ]
    if r!='-':lines.append(f"   RSI:{r} | Trend:{tr}")
    if cp and cp not in('none','N/A'):lines.append(f"   Candle: {cp}")
    if ep and ep not in('unknown','neutral','N/A'):lines.append(f"   EW: {ep}")
    if fib!='none':lines.append(f"   Fibonacci {fib} te support")
    lines+=[
        f"   Entry:{s['entry']} | SL:{s['sl']}",
        f"   TP1:{s['tp1']} (+{tp1p}%) | TP2:{s['tp2']} (+{tp2p}%)",
    ]
    if s.get('warnings'):lines.append(f"   !! {s['warnings'][0]}")
    lines.append("")
    return "\n".join(lines)

def build_msg(scored,breakouts,dsex):
    now=datetime.now(BD_TZ).strftime("%d %b %Y %I:%M %p")
    stats=get_stats()
    buys=[s for s in scored if 'BUY' in s['signal']]
    sells=[s for s in scored if 'SELL' in s['signal']][:4]
    ai=ai_summary(buys[:4],breakouts[:3],dsex,stats)

    for s in buys[:8]:
        save_signal(s['symbol'],s['signal'],s['entry'],s['sl'],
                    s['tp1'],s['tp2'],s['score'],s.get('inds',[]))

    parts=[]
    parts.append("DSE Signal Bot v4.1")
    parts.append(f"Tarikh : {now}")
    parts.append(f"DSEX   : {dsex}")
    parts.append(f"Win Rate: {stats['wr']}% ({stats['wins']}W/{stats['losses']}L/{stats['pending']}P)")
    parts.append(SEP)
    parts.append("Kemon kaj korche:")
    parts.append("- Downtrend e signal dei na")
    parts.append("- Elliott Wave analysis kore")
    parts.append("- Fibonacci support check kore")
    parts.append("- Fake breakout filter ache")
    parts.append(SEP)

    if breakouts:
        parts.append(f"BREAKOUT SCANNER -- {len(breakouts)} ti")
        parts.append("(Volume spike + EW + Fib diye khuje peyeche)")
        parts.append("")
        for s in breakouts:parts.append(fmt_brk(s))
        parts.append(SEP)

    if buys:
        normal=[s for s in buys if s['ltp']>=PENNY_THRESHOLD][:5]
        penny=[s for s in buys if s['ltp']<PENNY_THRESHOLD][:4]
        if normal:
            parts.append(f"BUY SIGNAL -- {len(normal)} ti")
            parts.append("")
            for s in normal:parts.append(fmt_sig(s))
        if penny:
            parts.append(f"PENNY BUY -- {len(penny)} ti [beshi jhuki]")
            parts.append("")
            for s in penny:parts.append(fmt_sig(s))
    else:
        parts.append("Aj kono strong BUY signal nei")
        parts.append("")

    if sells:
        parts.append(f"SELL -- {len(sells)} ti")
        for s in sells:parts.append(f"  {s['symbol']} {s['ltp']} ({s['change']:+.1f}%) Score:{s['score']}")
        parts.append("")

    parts.append(SEP)
    parts.append("AI Bishleshan:")
    parts.append(ai)
    parts.append("")
    parts.append("Jekono stock er naam pathiye din - full analysis paben!")
    parts.append("STOP LOSS shobshomai byabohar korun.")

    return "\n".join(parts)

# ══════════════════════
#  AUTO UPDATE
# ══════════════════════
async def auto_update_data(bot):
    log.info("Auto update...")
    stocks=fetch_stocks()
    if not stocks:return
    today=datetime.now(BD_TZ).strftime('%Y-%m-%d')
    updated=0
    for s in stocks:
        try:
            url=f"{GITHUB_API}/{s['symbol']}.csv"
            gh={'Authorization':f'token {GITHUB_TOKEN}','Accept':'application/vnd.github.v3+json'}
            r=requests.get(url,headers=gh,timeout=15)
            if r.status_code!=200:continue
            info=r.json()
            import base64
            old=base64.b64decode(info['content']).decode('utf-8')
            if today in old:continue
            nl=f"\n{today},{s['high']},{s['high']},{s['low']},{s['ltp']},{s['volume']}"
            enc=base64.b64encode((old.rstrip()+nl).encode()).decode()
            requests.put(url,headers=gh,json={'message':f"Update {s['symbol']} {today}",'content':enc,'sha':info['sha']},timeout=20)
            updated+=1
        except:pass
    global _cache;_cache={}
    log.info(f"Updated {updated}")

# ══════════════════════
#  SEND SIGNALS
# ══════════════════════
async def send_signals(bot):
    log.info("Signal job...")
    await bot.send_message(chat_id=CHAT_ID,text="Bishleshan cholche... (3-5 minit)")
    try:
        stocks=fetch_stocks()
        if not stocks:
            await bot.send_message(chat_id=CHAT_ID,text="Data nei. DSE bondho (Fri/Sat) ba trading hour shesh.")
            return
        dsex=get_dsex()
        global _cache;_cache={}
        breakouts=scan_breakouts(stocks)
        scored=analyze(stocks,use_hist=True)
        msg=build_msg(scored,breakouts,dsex)
        for i in range(0,len(msg),4000):
            await bot.send_message(chat_id=CHAT_ID,text=msg[i:i+4000])
    except Exception as e:
        log.error(f"Error:{e}")
        await bot.send_message(chat_id=CHAT_ID,text=f"Shomshsha: {str(e)[:200]}")

# ══════════════════════
#  OUTCOME CHECK
# ══════════════════════
async def check_outcomes(bot):
    try:
        conn=sqlite3.connect(DB_PATH);c=conn.cursor()
        today=datetime.now(BD_TZ).strftime('%Y-%m-%d')
        pending=c.execute('SELECT id,symbol,entry,tp1,sl FROM signals WHERE outcome="pending" AND check_date<=?',(today,)).fetchall()
        conn.close()
        if not pending:return
        stocks=fetch_stocks()
        pm={s['symbol']:s['ltp'] for s in stocks}
        report="Signal Outcome:\n\n";w=l=0
        for sid,sym,entry,tp1,sl in pending:
            cur=pm.get(sym)
            if not cur:continue
            pct=round((cur-entry)/entry*100,2)
            conn=sqlite3.connect(DB_PATH);c=conn.cursor()
            if cur>=tp1:
                c.execute('UPDATE signals SET outcome="win",outcome_pct=? WHERE id=?',(pct,sid))
                report+=f"WIN: {sym} +{pct}%\n";w+=1
            elif cur<=sl:
                c.execute('UPDATE signals SET outcome="loss",outcome_pct=? WHERE id=?',(pct,sid))
                report+=f"LOSS: {sym} {pct}%\n";l+=1
            conn.commit();conn.close()
        if w+l>0:
            report+=f"\nWin:{w} Loss:{l}"
            await bot.send_message(chat_id=CHAT_ID,text=report)
    except Exception as e:
        log.error(f"Outcome: {e}")

# ══════════════════════
#  CHAT HANDLER
# ══════════════════════
async def handle_message(update:Update,ctx:ContextTypes.DEFAULT_TYPE):
    text=update.message.text
    if text.startswith('/'):return
    stats=get_stats()
    stocks=fetch_stocks()
    ctx_txt=""
    for s in stocks:
        if s['symbol'].upper() in text.upper():
            ind=get_ind(s['symbol'])
            ctx_txt=f"\n{s['symbol']}: {s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
            if ind['ok']:
                ctx_txt+=(f"RSI:{ind['rsi']} MACD:{'up' if ind['macd_h']>0 else 'down'} "
                         f"BB:{ind['bb_pos']} Trend:{ind['trend']}\n"
                         f"EW Phase: {ind['ew_phase']}\n"
                         f"EW Detail: {ind['ew_desc']}\n"
                         f"Candle: {ind['candle']} (score:{ind['candle_score']})\n"
                         f"Vol Ratio: {ind['vol_ratio']}x\n"
                         f"MA20:{ind['ma20']} MA50:{ind['ma50']} EMA9:{ind['ema9']} EMA21:{ind['ema21']}\n"
                         f"Fib Level: {ind['fib_level']}\n"
                         f"Range-bound: {ind['range_bound']}\n"
                         f"Trend OK: {ind['trend_ok']}\n")
            break
    prompt=(
        f"Tumi DSE expert analyst. Banglay sohoj o clear vashay uttor dao.\n"
        f"WinRate:{stats['wr']}% Total:{stats['total']}\n{ctx_txt}\n"
        f"User: {text}\n\n"
        f"6-8 laine: technical analysis, EW wave obostha, kena/na kena koushol, risk management."
    )
    reply=ai_call(prompt)
    await update.message.reply_text(reply)

# ══════════════════════
#  COMMANDS
# ══════════════════════
async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    msg=(
        "DSE Signal Bot v4.1\n"
        f"Chat ID: {u.effective_chat.id}\n\n"
        "Notun Features:\n"
        "- Downtrend e signal dei na\n"
        "- EW (Elliott Wave) proper analysis\n"
        "- Fibonacci 6 level check\n"
        "- Range-bound stock filter\n"
        "- Fake breakout detection\n"
        "- Multi-candle pattern (10+ types)\n"
        "- RSI + MACD + BB + MA + EMA\n"
        "- TP1:8% TP2:20% TP3:50%+\n\n"
        "Commands:\n"
        "/signal - Full analysis\n"
        "/breakout - Breakout scanner\n"
        "/ew - EW analysis\n"
        "/stats - Performance\n"
        "/top - Top Gainers\n"
        "/sell - Sell signal\n"
        "/penny - Penny stocks\n\n"
        "Jekono stock er naam pathiye din!\n"
        "Sondha 6tar auto signal ashbe.\n\n"
        "BIJONOGE JHUKI ACHE. STOP LOSS SHOBSHOMAI."
    )
    await u.message.reply_text(msg)

async def cmd_signal(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Bishleshan cholche (3-5 minit)...")
    await send_signals(ctx.bot)

async def cmd_breakout(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Breakout scanner cholche...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    candidates=scan_breakouts(stocks)
    if not candidates:
        await u.message.reply_text("Aj kono breakout candidate nei.\nKal check korun.");return
    msg=f"BREAKOUT SCANNER -- {len(candidates)} ti Stock\n\n"
    for s in candidates:msg+=fmt_brk(s)
    msg+="STOP LOSS shobshomai din."
    for i in range(0,len(msg),4000):
        await u.message.reply_text(msg[i:i+4000])

async def cmd_ew(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Elliott Wave analysis cholche...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    results=[]
    for s in stocks[:50]:  # top 50 by volume
        ind=get_ind(s['symbol'])
        if not ind['ok']:continue
        ep=ind['ew_phase']
        if 'Wave 2/4 Shesh' in ep or 'Wave 3/5' in ep:
            results.append((s,ind))
    if not results:
        await u.message.reply_text("Aj kono strong EW candidate nei.");return
    msg="Elliott Wave Analysis:\n\n"
    for s,ind in results[:8]:
        tp1p=round((s['ltp']*1.08-s['ltp'])/s['ltp']*100,1)
        msg+=f">> {s['symbol']} | {ind['ew_phase']}\n"
        msg+=f"   Daam: {s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
        msg+=f"   {ind['ew_desc']}\n"
        msg+=f"   RSI:{ind['rsi']} Trend:{ind['trend']}\n"
        if ind['fib_level']!='none':msg+=f"   Fib {ind['fib_level']} te support\n"
        msg+=f"   Entry:{s['ltp']} SL:{round(ind['swing_low']*0.99,2)}\n\n"
    msg+="STOP LOSS shobshomai byabohar korun."
    for i in range(0,len(msg),4000):
        await u.message.reply_text(msg[i:i+4000])

async def cmd_stats(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    stats=get_stats()
    msg=f"Bot Performance:\n\n"
    msg+=f"Moto Signal : {stats['total']}\n"
    msg+=f"Win         : {stats['wins']}\n"
    msg+=f"Loss        : {stats['losses']}\n"
    msg+=f"Pending     : {stats['pending']}\n"
    msg+=f"Win Rate    : {stats['wr']}%\n"
    msg+=f"Avg Return  : {stats['avg']}%\n\n"
    if stats['recent']:
        msg+="Shampratik Signal:\n"
        for sym,entry,tp1,outcome,pct,date in stats['recent'][:6]:
            ic="WIN" if outcome=='win' else "LOSS" if outcome=='loss' else "..."
            msg+=f"  {ic} {sym} ({date}) {entry} -> {pct:+.1f}%\n"
    await u.message.reply_text(msg)

async def cmd_top(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Lod holche...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    top=sorted(stocks,key=lambda x:x['change'],reverse=True)[:12]
    msg="Top 12 Gainers:\n\n"
    for i,s in enumerate(top,1):
        p="[P] " if s['ltp']<PENNY_THRESHOLD else "    "
        msg+=f"{i:2}. {p}{s['symbol']:12} {s['ltp']:8.1f} (+{s['change']:.1f}%) Vol:{s['volume']:,}\n"
    await u.message.reply_text(msg)

async def cmd_sell(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Lod holche...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    scored=analyze(stocks,use_hist=True)
    sells=[s for s in scored if 'SELL' in s['signal']]
    if not sells:await u.message.reply_text("Aj SELL nei.");return
    msg="SELL Signal:\n\n"
    for s in sells[:8]:msg+=fmt_sig(s)
    await u.message.reply_text(msg)

async def cmd_penny(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Penny scan...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    penny=[s for s in stocks if s['ltp']<PENNY_THRESHOLD]
    if not penny:await u.message.reply_text("Penny stock nei.");return
    scored=analyze(penny,use_hist=True)
    buys=[s for s in scored if 'BUY' in s['signal']]
    if not buys:await u.message.reply_text("Penny BUY nei.");return
    msg="Penny BUY [beshi jhuki]:\n\n"
    for s in buys[:8]:msg+=fmt_sig(s)
    await u.message.reply_text(msg)

# ══════════════════════
#  SCHEDULER + MAIN
# ══════════════════════
async def post_init(app):
    init_db()
    sched=AsyncIOScheduler(timezone='UTC')
    sched.add_job(send_signals,'cron',hour=12,minute=0,args=[app.bot])
    sched.add_job(auto_update_data,'cron',hour=9,minute=30,args=[app.bot])
    sched.add_job(check_outcomes,'cron',hour=4,minute=0,args=[app.bot])
    sched.start()
    log.info("Scheduler ready: Signal UTC12 | Update UTC09:30 | Check UTC04")

def main():
    init_db()
    log.info("DSE Bot v4.1 shuru...")
    app=Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("breakout",cmd_breakout))
    app.add_handler(CommandHandler("ew",      cmd_ew))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("top",     cmd_top))
    app.add_handler(CommandHandler("sell",    cmd_sell))
    app.add_handler(CommandHandler("penny",   cmd_penny))
    app.add_handler(CommandHandler("giant",   cmd_giant))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_message))
    log.info("Polling shuru")
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__':
    main()

# ══════════════════════════════════════
#  SLEEPING GIANT SCANNER
#  Based on 15 real DSE winning stocks pattern
# ══════════════════════════════════════
def scan_sleeping_giants(stocks):
    """
    DSE Sleeping Giant Strategy:
    - দীর্ঘ downtrend বা sideways এর পর
    - হঠাৎ Volume explosion
    - Fresh EMA crossover
    - RSI oversold থেকে বের হচ্ছে
    - MACD turning positive
    """
    giants=[]
    for s in stocks:
        if s['ltp']<MIN_PRICE or s['volume']<10000:continue
        data=get_hist(s['symbol'])
        if not data or len(data['closes'])<60:continue

        closes=data['closes'];highs=data['highs']
        lows=data['lows'];vols=data['vols']
        cur=closes[-1]

        # 52-week high/low
        lookback=min(252,len(closes))
        w52_high=max(closes[-lookback:])
        w52_low=min(closes[-lookback:])
        if w52_low<=0:continue
        from_low=round((cur-w52_low)/w52_low*100,1)
        from_high=round((cur-w52_high)/w52_high*100,1)

        # Volume
        avg_vol=sum(vols[-20:])/max(len(vols[-20:]),1) if vols else 0
        cur_vol=vols[-1] if vols else 0
        vol_ratio=round(cur_vol/max(avg_vol,1),2)

        # Indicators
        r=rsi(closes)
        ml,sl_,hist=macd(closes)
        e9=ema(closes,min(9,len(closes)))
        e21=ema(closes,min(21,len(closes)))
        e9_prev=ema(closes[:-1],min(9,len(closes)-1)) if len(closes)>1 else e9
        e21_prev=ema(closes[:-1],min(21,len(closes)-1)) if len(closes)>1 else e21
        ma50=sma(closes,min(50,len(closes)))
        bbu,bbm,bbl=bb(closes)

        # Fresh EMA crossover (last 3 days)
        fresh_cross=e9>e21 and e9_prev<=e21_prev
        ema_bull=e9>e21

        # BB lower bounce
        bb_bounce=cur>bbl and closes[-2]<=bbl if len(closes)>=2 else False

        # MA50 breakout
        ma50_break=cur>ma50 and closes[-2]<=ma50 if len(closes)>=2 else False

        # Accumulation check: price was low for a while
        # Last 30 days below current MA
        long_base=False
        if len(closes)>=30:
            avg_30=sum(closes[-30:])/30
            min_30=min(closes[-30:])
            max_30=max(closes[-30:])
            rng_30=max_30-min_30
            # Base = tight range below MA50
            if rng_30/max(avg_30,1)<0.25 and avg_30<ma50*1.1:
                long_base=True

        # Score calculation
        score=0;reasons=[];signals=[]

        # 1. Near 52-week low (most important)
        if from_low<30:
            score+=4;signals.append(f"Near 52w Low (+{from_low:.0f}%)")
            reasons.append(f"52 saptaher low theke matro +{from_low:.0f}% upore - smart money kinche na")
        elif from_low<60:
            score+=2;signals.append(f"Low zone (+{from_low:.0f}%)")
        elif from_low<100:
            score+=1;signals.append(f"Below high (+{from_low:.0f}%)")

        # 2. Volume explosion (critical)
        if vol_ratio>=7:
            score+=6;signals.append(f"Vol {vol_ratio}x EXPLOSION!")
            reasons.append(f"Volume gorore {vol_ratio}x - institutional accumulation shuru")
        elif vol_ratio>=5:
            score+=5;signals.append(f"Vol {vol_ratio}x")
            reasons.append(f"Volume gorore {vol_ratio}x - strong buying interest")
        elif vol_ratio>=3:
            score+=4;signals.append(f"Vol {vol_ratio}x")
            reasons.append(f"Volume gorore {vol_ratio}x - above average buying")
        elif vol_ratio>=2:
            score+=2;signals.append(f"Vol {vol_ratio}x")
        else:
            continue  # Volume must spike

        # 3. RSI in sweet zone
        if r<30:
            score+=4;signals.append(f"RSI:{r} Oversold!")
            reasons.append(f"RSI {r} - heavily oversold, bounce imminent")
        elif 30<=r<45:
            score+=3;signals.append(f"RSI:{r} Recovery")
            reasons.append(f"RSI {r} - oversold theke recovery shuru")
        elif 45<=r<60:
            score+=2;signals.append(f"RSI:{r} OK")
            reasons.append(f"RSI {r} - momentum building")
        elif r>=75:
            score-=2;signals.append(f"RSI:{r} OB")

        # 4. Fresh EMA crossover (strongest signal)
        if fresh_cross:
            score+=5;signals.append("Fresh EMA Cross!")
            reasons.append("EMA9 eikhuni EMA21 cross korche - strongest buy signal")
        elif ema_bull:
            score+=2;signals.append("EMA Bull")

        # 5. MACD turning positive
        if hist>0 and ml>sl_:
            score+=3;signals.append("MACD Bull Cross")
            reasons.append("MACD bullish crossover - momentum confirm")
        elif hist>0:
            score+=2;signals.append("MACD Positive")
        elif hist<0 and hist>-0.1:
            score+=1;signals.append("MACD Near Zero")

        # 6. BB lower bounce
        if bb_bounce:
            score+=3;signals.append("BB Bounce!")
            reasons.append("BB lower band theke bounce - oversold reversal")
        elif cur<bbm:
            score+=1;signals.append("Below BB Mid")

        # 7. MA50 breakout
        if ma50_break:
            score+=3;signals.append("MA50 Break!")
            reasons.append("MA50 er upore uthche - medium term trend change")

        # 8. Long base (accumulation)
        if long_base:
            score+=2;signals.append("Base Pattern")
            reasons.append("Dorghodin base baniyeche - parabolic move er aga")

        # 9. Price change today
        chg=s['change']
        if 2<chg<=8:score+=2;signals.append(f"+{chg:.1f}% today")
        elif chg>8:score+=1;signals.append(f"+{chg:.1f}% (high)")
        elif chg<0:score-=1

        if score<10:continue

        # TP/SL based on strategy
        sl=round(w52_low*1.02,2)  # SL just above 52w low
        risk=cur-sl
        if risk<=0:risk=cur*0.10
        tp1=round(max(cur*(1+TP1_MIN),cur+risk*1.5),2)
        tp2=round(max(cur*(1+TP2_MIN),cur+risk*3),2)
        tp3=round(max(cur*1.50,cur+risk*5),2)
        tp4=round(max(cur*2.00,cur+risk*8),2)  # Parabolic target

        giants.append({
            **s,'score':score,'signals':signals,'reasons':reasons,
            'entry':cur,'sl':sl,'tp1':tp1,'tp2':tp2,'tp3':tp3,'tp4':tp4,
            'w52_high':w52_high,'w52_low':w52_low,
            'from_low':from_low,'from_high':from_high,
            'vol_ratio':vol_ratio,'rsi':r,'ema9':e9,'ema21':e21,
            'fresh_cross':fresh_cross,'long_base':long_base,
            'ma50':ma50,'hist':hist,
        })

    giants.sort(key=lambda x:x['score'],reverse=True)
    return giants[:10]

def fmt_giant(s):
    tp1p=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
    tp2p=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
    tp3p=round((s['tp3']-s['ltp'])/s['ltp']*100,1)
    tp4p=round((s['tp4']-s['ltp'])/s['ltp']*100,1)
    lines=[
        f"GIANT: {s['symbol']} | Score:{s['score']}",
        f"Daam: {s['ltp']} ({s['change']:+.1f}%) | Vol:{s['volume']:,} ({s['vol_ratio']}x avg)",
        f"52w: High={s['w52_high']} Low={s['w52_low']}",
        f"Low theke: +{s['from_low']}% | High theke: {s['from_high']}%",
        f"RSI:{s['rsi']} | EMA9:{s['ema9']} | EMA21:{s['ema21']}",
        f"Fresh EMA Cross: {'YES!' if s['fresh_cross'] else 'No'} | Base: {'YES' if s['long_base'] else 'No'}",
        f"",
        f"Entry : {s['entry']}",
        f"SL    : {s['sl']} (52w low er kache)",
        f"TP1   : {s['tp1']} (+{tp1p}%)",
        f"TP2   : {s['tp2']} (+{tp2p}%)",
        f"TP3   : {s['tp3']} (+{tp3p}%) [Parabolic]",
        f"TP4   : {s['tp4']} (+{tp4p}%) [Max Target]",
        f"",
        f"Signals: {' | '.join(s['signals'][:5])}",
    ]
    if s.get('reasons'):
        lines.append(f"Karon 1: {s['reasons'][0]}")
        if len(s['reasons'])>1:lines.append(f"Karon 2: {s['reasons'][1]}")
    lines.append("")
    return "\n".join(lines)


async def cmd_giant(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "Sleeping Giant Scanner cholche...\n"
        "15ti winning stock er pattern diye khujche...\n"
        "(2-3 minit lagbe)"
    )
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    giants=scan_sleeping_giants(stocks)
    if not giants:
        await u.message.reply_text(
            "Aj kono Sleeping Giant nei.\n\n"
            "Karon: DSE te ekhon emon kono stock nei jeta:\n"
            "- 52w low er kache AND\n"
            "- Volume spike hochhe AND\n"
            "- EMA cross korche\n\n"
            "Kal abar try korun."
        )
        return

    msg=f"SLEEPING GIANT SCANNER -- {len(giants)} ti Stock\n"
    msg+="="*26+"\n"
    msg+="15ti real winning stock er pattern:\n"
    msg+="MEGHNAPET, BDTHAIFOOD, RDFOOD,\n"
    msg+="NAHEEACP, ASIATICLAB er moto\n\n"

    for s in giants:msg+=fmt_giant(s)

    msg+="="*26+"\n"
    msg+="STRATEGY:\n"
    msg+="- Entry: EMA cross er din ba tarporer din\n"
    msg+="- SL: 52w low er just upore\n"
    msg+="- TP1: 8% e half sell\n"
    msg+="- TP2: 20% e quarter sell\n"
    msg+="- TP3/4: baki hold korun parabolic er jonno\n\n"
    msg+="SHOBCHEYE MUHURTPORTO: Stop Loss!!!"

    for i in range(0,len(msg),4000):
        await u.message.reply_text(msg[i:i+4000])
