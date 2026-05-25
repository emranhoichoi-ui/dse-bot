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
HEADERS={'User-Agent':'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120','Accept':'text/html'}

GITHUB_RAW='https://raw.githubusercontent.com/emranhoichoi-ui/dse-bot/main/data'
GITHUB_API='https://api.github.com/repos/emranhoichoi-ui/dse-bot/contents/data'

MIN_PRICE=1.0
PENNY_THRESHOLD=10.0
MIN_VOLUME=20000
MAX_CHANGE=15.0
TP1_MIN=0.08
TP2_MIN=0.20
DB_PATH='/tmp/dse_v4.db'

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

# ══════════════════════════════════════
#  GITHUB DATA FETCH
# ══════════════════════════════════════
_hist_cache={}

def get_hist_from_github(symbol):
    """GitHub থেকে historical CSV data আনে"""
    global _hist_cache
    # Cache check (আজকের data একবারই আনব)
    cache_key=f"{symbol}_{datetime.now(BD_TZ).strftime('%Y%m%d')}"
    if cache_key in _hist_cache:
        return _hist_cache[cache_key]

    try:
        url=f"{GITHUB_RAW}/{symbol}.csv"
        r=requests.get(url,headers={'User-Agent':'Mozilla/5.0'},timeout=15)
        if r.status_code!=200:
            return None
        # CSV parse
        reader=csv.DictReader(io.StringIO(r.text))
        rows=list(reader)
        if not rows:return None

        closes=[float(row['Close']) for row in rows if row.get('Close')]
        highs =[float(row['High'])  for row in rows if row.get('High')]
        lows  =[float(row['Low'])   for row in rows if row.get('Low')]
        opens =[float(row['Open'])  for row in rows if row.get('Open')]
        vols  =[float(row['Volume'])for row in rows if row.get('Volume')]
        dates =[row['Date']         for row in rows if row.get('Date')]

        data={'closes':closes,'highs':highs,'lows':lows,
              'opens':opens,'vols':vols,'dates':dates}
        _hist_cache[cache_key]=data
        log.info(f"✅ {symbol}: {len(closes)} days from GitHub")
        return data
    except Exception as e:
        log.error(f"GitHub fetch error {symbol}: {e}")
        return None

def update_github_data(symbol,new_row):
    """প্রতিদিন নতুন data GitHub এ add করে"""
    if not GITHUB_TOKEN:return False
    try:
        # আগের file এর content নিই
        url=f"{GITHUB_API}/{symbol}.csv"
        headers={
            'Authorization':f'token {GITHUB_TOKEN}',
            'Accept':'application/vnd.github.v3+json'
        }
        r=requests.get(url,headers=headers,timeout=15)
        if r.status_code!=200:return False
        file_info=r.json()
        import base64
        old_content=base64.b64decode(file_info['content']).decode('utf-8')
        sha=file_info['sha']

        # নতুন row যোগ করি
        new_line=f"\n{new_row['date']},{new_row['open']},{new_row['high']},{new_row['low']},{new_row['close']},{new_row['volume']}"
        new_content=old_content.rstrip()+new_line
        import base64
        encoded=base64.b64encode(new_content.encode()).decode()

        # GitHub এ update করি
        payload={
            'message':f"Update {symbol} - {new_row['date']}",
            'content':encoded,
            'sha':sha
        }
        r2=requests.put(url,headers=headers,json=payload,timeout=20)
        return r2.status_code in(200,201)
    except Exception as e:
        log.error(f"GitHub update error {symbol}: {e}")
        return False

# ══════════════════════════════════════
#  MATH HELPERS
# ══════════════════════════════════════
def sf(txt):
    try:return float(str(txt).strip().replace(',','').replace('%','').replace('৳',''))
    except:return 0.0

def ema_calc(data,period):
    if not data or len(data)<period:return data[-1] if data else 0
    k=2/(period+1);v=sum(data[:period])/period
    for p in data[period:]:v=p*k+v*(1-k)
    return round(v,2)

def rsi_calc(closes,period=14):
    if len(closes)<period+1:return 50.0
    g,l=[],[]
    for i in range(1,len(closes)):
        d=closes[i]-closes[i-1];g.append(max(d,0));l.append(max(-d,0))
    ag=sum(g[-period:])/period;al=sum(l[-period:])/period
    if al==0:return 100.0
    return round(100-(100/(1+ag/al)),1)

def macd_calc(closes):
    if len(closes)<26:return 0,0,0
    e12=ema_calc(closes,12);e26=ema_calc(closes,26)
    ml=e12-e26;sl=ml*0.9;hist=ml-sl
    return round(ml,3),round(sl,3),round(hist,3)

def bb_calc(closes,period=20):
    if len(closes)<period:return 0,0,0
    r=closes[-period:];mid=sum(r)/period
    std=(sum((x-mid)**2 for x in r)/period)**0.5
    return round(mid+2*std,2),round(mid,2),round(mid-2*std,2)

def sma_calc(closes,period):
    if len(closes)<period:return closes[-1] if closes else 0
    return round(sum(closes[-period:])/period,2)

# ══════════════════════════════════════
#  FULL INDICATOR CALCULATION
# ══════════════════════════════════════
def get_all_indicators(symbol):
    """GitHub historical data থেকে সব indicators calculate করে"""
    data=get_hist_from_github(symbol)
    if not data or len(data['closes'])<20:
        return{'ok':False,'rsi':50,'macd_h':0,'bb_pos':'mid',
               'ma20':0,'ma50':0,'ema9':0,'ema21':0,'trend':'neutral',
               'avg_vol':0,'vol_spike':False,'vol_ratio':0,
               'swing_high':0,'swing_low':0,'ew_phase':'unknown',
               'fake_break':False,'candle_pattern':'N/A','pattern_score':0,
               'base_days':0}

    closes=data['closes'];highs=data['highs']
    lows=data['lows'];opens=data['opens'];vols=data['vols']

    # ── Core Indicators ──
    rsi=rsi_calc(closes)
    ml,sl_,mh=macd_calc(closes)
    bbu,bbm,bbl=bb_calc(closes)
    ma20=sma_calc(closes,20)
    ma50=sma_calc(closes,min(50,len(closes)))
    e9=ema_calc(closes,min(9,len(closes)))
    e21=ema_calc(closes,min(21,len(closes)))
    last=closes[-1]

    # BB position
    bp='upper' if last>bbu else 'lower' if last<bbl else 'mid'

    # Trend (MA alignment)
    if e9>e21>ma20>ma50:trend='strong_up'
    elif e9>e21 and ma20>ma50:trend='up'
    elif e9<e21<ma20<ma50:trend='strong_down'
    elif e9<e21 and ma20<ma50:trend='down'
    else:trend='neutral'

    # Volume analysis
    avg_vol=sum(vols[-20:])/max(len(vols[-20:]),1) if vols else 0
    cur_vol=vols[-1] if vols else 0
    vol_ratio=round(cur_vol/avg_vol,2) if avg_vol>0 else 0
    vol_spike=vol_ratio>=1.8

    # Swing High/Low (20 day)
    swing_high=max(highs[-20:]) if len(highs)>=20 else 0
    swing_low=min(lows[-20:]) if len(lows)>=20 else 0

    # Elliott Wave phase
    ew_phase='unknown'
    if len(closes)>=10:
        rec=sum(closes[-5:])/5;old=sum(closes[-10:-5])/5
        if last>rec>old:ew_phase='wave3_5'
        elif last<rec<old:ew_phase='wave_down'
        elif old>rec and last>rec:ew_phase='wave2_4_end'
        else:ew_phase='neutral'

    # Fake breakout detection
    fake_break=False
    if len(highs)>=6:
        prev_high=max(highs[-6:-1])
        if last>prev_high and rsi>75:fake_break=True

    # ── MULTI-CANDLE PATTERN ──
    candle_pattern='none';pattern_score=0
    if len(closes)>=5 and len(opens)>=5:
        c0,c1,c2=closes[-3],closes[-2],closes[-1]
        o0,o1,o2=opens[-3],opens[-2],opens[-1]
        h0,h1,h2=highs[-3],highs[-2],highs[-1]
        l0,l1,l2=lows[-3],lows[-2],lows[-1]

        body2=abs(c2-o2);rng2=h2-l2 if h2>l2 else 0.01
        body1=abs(c1-o1);rng1=h1-l1 if h1>l1 else 0.01
        body0=abs(c0-o0);rng0=h0-l0 if h0>l0 else 0.01

        bull2=c2>o2;bull1=c1>o1;bull0=c0>o0
        uw2=(h2-max(c2,o2))/rng2
        lw2=(min(c2,o2)-l2)/rng2

        # Real Hammer — আগে downtrend ছিল
        prev_down=closes[-5]>closes[-3] if len(closes)>=5 else False
        if lw2>0.5 and uw2<0.1 and bull2 and prev_down:
            candle_pattern='Real Hammer 🔨';pattern_score=6
        elif lw2>0.5 and uw2<0.1 and bull2:
            candle_pattern='Hammer 🔨';pattern_score=3

        # Bullish Engulfing
        elif not bull1 and bull2 and c2>o1 and o2<c1 and body2>body1:
            candle_pattern='Bullish Engulfing 🕯️';pattern_score=6

        # Morning Star
        elif not bull0 and body1<body0*0.3 and bull2 and c2>((c0+o0)/2):
            candle_pattern='Morning Star ⭐';pattern_score=7

        # Three White Soldiers
        elif bull0 and bull1 and bull2 and c2>c1>c0:
            candle_pattern='3 White Soldiers 🚀';pattern_score=8

        # Shooting Star
        elif uw2>0.5 and lw2<0.1 and not bull2:
            candle_pattern='Shooting Star 💫';pattern_score=-6

        # Evening Star
        elif bull0 and body1<body0*0.3 and not bull2 and c2<((c0+o0)/2):
            candle_pattern='Evening Star ⭐';pattern_score=-7

        # Bearish Engulfing
        elif bull1 and not bull2 and o2>c1 and c2<o1 and body2>body1:
            candle_pattern='Bearish Engulfing 📉';pattern_score=-6

        # Three Black Crows
        elif not bull0 and not bull1 and not bull2 and c2<c1<c0:
            candle_pattern='3 Black Crows 💀';pattern_score=-8

        # Doji
        elif body2<rng2*0.1:
            candle_pattern='Doji ⚖️';pattern_score=0

        # Strong bull close
        elif bull2 and body2>rng2*0.6:
            candle_pattern='Strong Bull ✅';pattern_score=2

    # Base detection (consolidation)
    base_days=0
    if len(highs)>=30:
        rng_30=max(highs[-30:])-min(lows[-30:])
        avg_30=sum(closes[-30:])/30
        if avg_30>0 and rng_30/avg_30<0.15:base_days=30

    return{
        'ok':True,'rsi':rsi,'macd':ml,'macd_sig':sl_,'macd_h':mh,
        'bb_upper':bbu,'bb_mid':bbm,'bb_lower':bbl,'bb_pos':bp,
        'ma20':ma20,'ma50':ma50,'ema9':e9,'ema21':e21,
        'trend':trend,'avg_vol':int(avg_vol),'cur_vol':int(cur_vol),
        'vol_spike':vol_spike,'vol_ratio':vol_ratio,
        'swing_high':swing_high,'swing_low':swing_low,
        'ew_phase':ew_phase,'fake_break':fake_break,
        'candle_pattern':candle_pattern,'pattern_score':pattern_score,
        'base_days':base_days,
    }

# ══════════════════════════════════════
#  BREAKOUT SCANNER
# ══════════════════════════════════════
def scan_breakouts(stocks):
    candidates=[]
    for s in stocks:
        if s['ltp']<MIN_PRICE or s['volume']<MIN_VOLUME:continue
        ind=get_all_indicators(s['symbol'])
        if not ind['ok']:continue

        score=0;signals=[];reasons=[]
        ltp=s['ltp'];chg=s['change'];vol=s['volume']
        vol_ratio=ind['vol_ratio']

        # 1. Volume Breakout
        if vol_ratio>=5:
            score+=8;signals.append(f"Vol {vol_ratio}x 🔥🔥🔥🔥")
            reasons.append(f"Volume গড়ের {vol_ratio}x — institutional entry")
        elif vol_ratio>=3:
            score+=6;signals.append(f"Vol {vol_ratio}x 🔥🔥🔥")
            reasons.append(f"Volume গড়ের {vol_ratio}x — strong buying")
        elif vol_ratio>=2:
            score+=4;signals.append(f"Vol {vol_ratio}x 🔥🔥")
            reasons.append(f"Volume গড়ের {vol_ratio}x — above average")
        elif vol_ratio>=1.5:
            score+=2;signals.append(f"Vol {vol_ratio}x 🔥")
        else:continue

        # 2. Base Breakout
        if ind['base_days']>0:
            score+=5;signals.append(f"Base {ind['base_days']}d 📦")
            reasons.append(f"{ind['base_days']} দিনের consolidation — parabolic move সম্ভব")

        # 3. Swing High Break
        sh=ind['swing_high']
        if sh>0 and ltp>=sh*0.98:
            score+=4;signals.append("Swing Break 💥")
            reasons.append(f"২০ দিনের high ৳{sh} break করেছে")

        # 4. MA/EMA Trend
        tr=ind['trend']
        if tr=='strong_up':
            score+=5;signals.append("EMA9>EMA21>MA20>MA50 🚀")
            reasons.append("Perfect MA alignment — strong uptrend")
        elif tr=='up':
            score+=3;signals.append("Trend Up ↑")

        # Fresh EMA crossover
        e9=ind['ema9'];e21=ind['ema21']

        # 5. RSI Momentum
        rsi=ind['rsi']
        if 50<=rsi<=65:
            score+=4;signals.append(f"RSI:{rsi} ✅")
            reasons.append(f"RSI {rsi} — breakout zone, আরো উপরে যাওয়ার space আছে")
        elif 45<=rsi<50:
            score+=2;signals.append(f"RSI:{rsi}")
        elif rsi>75:
            score-=3;signals.append(f"RSI:{rsi} OB⚠️")

        # 6. MACD
        if ind['macd_h']>0 and ind['macd']>ind['macd_sig']:
            score+=3;signals.append("MACD ✅")
            reasons.append("MACD bullish crossover confirmed")
        elif ind['macd_h']>0:
            score+=1;signals.append("MACD ↑")

        # 7. Candle Pattern
        cp=ind['candle_pattern'];ps=ind['pattern_score']
        if ps>=4:
            score+=ps;signals.append(cp)
            reasons.append(f"Candle: {cp}")
        elif ps>0:score+=ps;signals.append(cp)
        elif ps<0:score+=ps

        # 8. BB
        if ind['bb_pos']=='lower':
            score+=2;signals.append("BB Lower 🟢")
        elif ind['bb_pos']=='upper' and vol_ratio>3:
            score+=1;signals.append("BB Upper+Vol")

        # 9. Elliott Wave
        ep=ind['ew_phase']
        if ep=='wave2_4_end':
            score+=5;signals.append("EW W3/5 শুরু 🌊")
            reasons.append("EW Wave 2/4 শেষ — সবচেয়ে শক্তিশালী impulse আসছে")
        elif ep=='wave3_5':
            score+=2;signals.append("EW Impulse ↑")

        # Fake breakout check
        if ind['fake_break']:
            score-=4;signals.append("⚠️ FakeBreak")

        if score<8:continue

        # TP/SL
        sl=round(ind['swing_low']*0.99 if ind['swing_low']>0 else ltp*0.93,2)
        risk=ltp-sl
        if risk<=0:risk=ltp*0.05
        tp1=round(max(ltp*(1+TP1_MIN),ltp+risk*2),2)
        tp2=round(max(ltp*(1+TP2_MIN),ltp+risk*4),2)
        tp3=round(max(ltp*1.50,ltp+risk*6),2)

        candidates.append({
            **s,'score':score,'signals':signals,'reasons':reasons,'ind':ind,
            'entry':ltp,'sl':sl,'tp1':tp1,'tp2':tp2,'tp3':tp3,
            'vol_ratio':vol_ratio,'base_days':ind['base_days'],
            'candle':ind['candle_pattern'],'rsi':rsi,'trend':tr,
        })

    candidates.sort(key=lambda x:x['score'],reverse=True)
    return candidates[:10]

# ══════════════════════════════════════
#  DSE LIVE DATA
# ══════════════════════════════════════
def fetch_stocks():
    log.info("DSE data fetch...")
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
        log.info(f"✅ {len(unique)} stocks");return unique
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
#  AUTO UPDATE — প্রতিদিন নতুন data GitHub এ
# ══════════════════════════════════════
async def auto_update_data(bot):
    """প্রতিদিন trading শেষে GitHub এ নতুন data যোগ করে"""
    log.info("Auto data update শুরু...")
    stocks=fetch_stocks()
    if not stocks:
        log.info("DSE বন্ধ — update skip");return

    today=datetime.now(BD_TZ).strftime('%Y-%m-%d')
    updated=0;failed=0

    for s in stocks:
        new_row={
            'date':today,
            'open':s['high'],  # Opening approximate
            'high':s['high'],
            'low':s['low'],
            'close':s['ltp'],
            'volume':s['volume']
        }
        if update_github_data(s['symbol'],new_row):
            updated+=1
        else:
            failed+=1

    # Cache clear করি
    global _hist_cache
    _hist_cache={}

    msg=f"📊 *Data Update*\n✅ Updated: {updated}\n❌ Failed: {failed}\n📅 Date: {today}"
    await bot.send_message(chat_id=CHAT_ID,text=msg,parse_mode='Markdown')
    log.info(f"Data update done: {updated} updated, {failed} failed")

# ══════════════════════════════════════
#  ANALYSIS ENGINE
# ══════════════════════════════════════
def analyze(stocks,use_hist=False):
    scored=[]
    for s in stocks:
        ltp=s['ltp'];hi=s['high'];lo=s['low']
        chg=s['change'];vol=s['volume'];yd=s['yday']
        rng=hi-lo if hi>lo else ltp*0.01
        cp=(ltp-lo)/rng;uw=(hi-ltp)/rng;lw=(ltp-lo)/rng
        score=0.0;tags=[];inds=[];warnings=[]

        # Historical indicators
        ind={'ok':False,'rsi':50,'trend':'neutral','ew_phase':'unknown',
             'fake_break':False,'candle_pattern':'N/A','pattern_score':0,
             'macd_h':0,'bb_pos':'mid','vol_ratio':0}

        if use_hist:
            ind=get_all_indicators(s['symbol'])

        if ind['ok']:
            # ── Candle Pattern (multi-day, accurate) ──
            cp_=ind['candle_pattern'];ps=ind['pattern_score']
            if ps>=6:score+=ps;tags.append(cp_);inds.append('candle_strong_bull')
            elif ps>=3:score+=ps;tags.append(cp_);inds.append('candle_bull')
            elif ps>0:score+=ps;tags.append(cp_)
            elif ps<=-6:score+=ps;tags.append(cp_);inds.append('candle_strong_bear')
            elif ps<0:score+=ps;tags.append(cp_)

            # ── RSI ──
            rsi=ind['rsi']
            if rsi<30:score+=4;tags.append(f"RSI:{rsi} Oversold 🟢");inds.append('rsi_os')
            elif 30<=rsi<45:score+=3;tags.append(f"RSI:{rsi} 🟢");inds.append('rsi_good')
            elif 45<=rsi<60:score+=1;tags.append(f"RSI:{rsi}")
            elif rsi>=75:score-=3;tags.append(f"RSI:{rsi} OB⚠️");warnings.append(f"RSI {rsi} overbought")
            else:tags.append(f"RSI:{rsi}")

            # ── MACD ──
            if ind['macd_h']>0 and ind['macd']>ind['macd_sig']:
                score+=3;tags.append("MACD ↑ 🟢");inds.append('macd_bull')
            elif ind['macd_h']<0 and ind['macd']<ind['macd_sig']:
                score-=3;tags.append("MACD ↓ 🔴")

            # ── Bollinger Bands ──
            if ind['bb_pos']=='lower':score+=3;tags.append("BB Lower 🟢");inds.append('bb_low')
            elif ind['bb_pos']=='upper':score-=2;tags.append("BB Upper ⚠️")
            else:tags.append("BB Mid")

            # ── MA/EMA Trend ──
            tr=ind['trend']
            if tr=='strong_up':score+=5;tags.append("Trend ↑↑ 🚀");inds.append('trend_sup')
            elif tr=='up':score+=2;tags.append("Trend ↑");inds.append('trend_up')
            elif tr=='strong_down':score-=4;tags.append("Trend ↓↓")
            elif tr=='down':score-=2;tags.append("Trend ↓")

            # ── Elliott Wave ──
            ep=ind['ew_phase']
            if ep=='wave2_4_end':score+=4;tags.append("EW W2/4 End 🌊");inds.append('ew_end')
            elif ep=='wave3_5':score+=2;tags.append("EW Impulse")
            elif ep=='wave_down':score-=2;tags.append("EW Down")

            # ── Volume Ratio ──
            vr=ind['vol_ratio']
            if vr>=3:score+=3;tags.append(f"Vol {vr}x 🔥🔥🔥")
            elif vr>=2:score+=2;tags.append(f"Vol {vr}x 🔥🔥")
            elif vr>=1.5:score+=1;tags.append(f"Vol {vr}x 🔥")

            # ── Fake Breakout ──
            if ind['fake_break']:score-=4;tags.append("FakeBreak ⚠️");warnings.append("Fake breakout risk")

        else:
            # Historical data নেই — basic analysis
            if lw>0.4 and uw<0.15 and cp>0.6:
                score+=2;tags.append("Bull Shape")
            elif uw>0.4 and lw<0.15:
                score-=2;tags.append("Bear Shape")

            if vol>3000000:score+=3;tags.append(f"Vol:{vol//1000}K 🔥🔥🔥")
            elif vol>1000000:score+=2;tags.append(f"Vol:{vol//1000}K 🔥🔥")
            elif vol>300000:score+=1;tags.append(f"Vol:{vol//1000}K 🔥")
            else:tags.append(f"Vol:{vol//1000}K")

        # ── Price Change ──
        if 1.5<chg<=5:score+=2;tags.append(f"+{chg:.1f}%")
        elif chg>5:score+=1;tags.append(f"+{chg:.1f}%")
        elif chg<-3:score-=2;tags.append(f"{chg:.1f}%")
        elif chg<-1:score-=1;tags.append(f"{chg:.1f}%")

        # ── Fibonacci ──
        f618=lo+rng*0.618;f382=lo+rng*0.382
        if ltp>0:
            if abs(ltp-f618)/ltp<0.015:score+=2;tags.append("Fib 0.618 ✨")
            elif abs(ltp-f382)/ltp<0.015:score+=1;tags.append("Fib 0.382")

        # ── Gap ──
        if yd>0:
            gap=(ltp-yd)/yd*100
            if gap>1.5:score+=1;tags.append("Gap Up ⬆️")
            elif gap<-1.5:score-=1;tags.append("Gap Down ⬇️")

        s['ind']=ind;s['inds']=inds;s['warnings']=warnings

        # Signal
        if score>=12:signal="STRONG BUY 🟢🟢"
        elif score>=7:signal="BUY 🟢"
        elif score<=-12:signal="STRONG SELL 🔴🔴"
        elif score<=-7:signal="SELL 🔴"
        else:signal="HOLD 🟡"

        sl=round(lo*0.993,2);risk=ltp-sl
        if risk<=0:risk=ltp*0.04
        tp1=round(max(ltp*(1+TP1_MIN),ltp+risk*2.5),2)
        tp2=round(max(ltp*(1+TP2_MIN),ltp+risk*4.5),2)
        s.update({'score':round(score,1),'signal':signal,'tags':tags,'entry':ltp,'sl':sl,'tp1':tp1,'tp2':tp2})
        scored.append(s)

    scored.sort(key=lambda x:x['score'],reverse=True)
    return scored

# ══════════════════════════════════════
#  AI ANALYSIS
# ══════════════════════════════════════
def ai_chat(prompt):
    try:
        client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp=client.messages.create(model="claude-sonnet-4-20250514",max_tokens=600,
                                    messages=[{"role":"user","content":prompt}])
        return resp.content[0].text
    except Exception as e:
        return f"⚠️ AI: {str(e)[:80]}"

def ai_signal_analysis(buys,breakouts,sells,dsex,stats):
    today=datetime.now(BD_TZ).strftime("%d %B %Y")
    buy_txt=""
    for s in buys[:4]:
        ind=s.get('ind',{})
        buy_txt+=(f"{s['symbol']}: ৳{s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,} "
                  f"RSI:{ind.get('rsi','-')} Trend:{ind.get('trend','-')} "
                  f"Candle:{ind.get('candle_pattern','N/A')} EW:{ind.get('ew_phase','-')}\n")
    brk_txt="".join([f"{s['symbol']}: Vol {s['vol_ratio']}x {', '.join(s['signals'][:3])}\n"
                     for s in breakouts[:3]])
    prompt=(
        f"তুমি DSE Bangladesh expert analyst। তারিখ:{today} DSEX:{dsex} "
        f"Bot WinRate:{stats['wr']}%\n\n"
        f"BUY সিগনাল:\n{buy_txt}\n"
        f"Breakout Candidates:\n{brk_txt}\n\n"
        f"প্রতিটি BUY স্টকের জন্য ২ লাইনে বাংলায়: কেন কিনবেন, কী risk। "
        f"শেষে ৩ লাইনে আজকের DSE মার্কেট অবস্থা।"
    )
    return ai_chat(prompt)

# ══════════════════════════════════════
#  MESSAGE BUILDERS
# ══════════════════════════════════════
def fmt_breakout(s):
    tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
    tp2_pct=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
    tp3_pct=round((s['tp3']-s['ltp'])/s['ltp']*100,1)
    msg=f"🚀 *{s['symbol']}* (Score:{s['score']})\n"
    msg+=f"💰 `৳{s['ltp']}` ({s['change']:+.1f}%) | Vol:{s['volume']:,} ({s['vol_ratio']}x)\n"
    msg+=f"📊 RSI:`{s['rsi']}` | Trend:`{s['trend']}` | Candle:`{s['candle']}`\n"
    if s['base_days']>0:msg+=f"📦 Base: {s['base_days']} দিন\n"
    msg+=f"📥 Entry:`৳{s['entry']}` SL:`৳{s['sl']}`\n"
    msg+=f"🎯 TP1:`৳{s['tp1']}` _(+{tp1_pct}%)_\n"
    msg+=f"🎯 TP2:`৳{s['tp2']}` _(+{tp2_pct}%)_\n"
    msg+=f"🎯 TP3:`৳{s['tp3']}` _(+{tp3_pct}%)_ ← Parabolic\n"
    msg+=f"🏷 {' · '.join(s['signals'][:5])}\n"
    if s.get('reasons'):msg+=f"💡 _{s['reasons'][0]}_\n"
    msg+="\n"
    return msg

def fmt_signal(s):
    ind=s.get('ind',{})
    rsi=ind.get('rsi','-') if ind.get('ok') else '-'
    tr=ind.get('trend','-') if ind.get('ok') else '-'
    cp=ind.get('candle_pattern','N/A') if ind.get('ok') else 'N/A'
    tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
    tp2_pct=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
    msg=f"*{s['symbol']}* — {s['signal']} (Score:{s['score']})\n"
    msg+=f"💰 `৳{s['ltp']}` ({s['change']:+.1f}%) | Vol:{s['volume']:,}\n"
    if rsi!='-':msg+=f"📊 RSI:`{rsi}` | Trend:`{tr}`\n"
    if cp not in('N/A','none','N/A'):msg+=f"🕯️ `{cp}`\n"
    msg+=f"📥 Entry:`৳{s['entry']}` SL:`৳{s['sl']}`\n"
    msg+=f"🎯 TP1:`৳{s['tp1']}` _(+{tp1_pct}%)_ TP2:`৳{s['tp2']}` _(+{tp2_pct}%)_\n"
    msg+=f"🏷 {' · '.join(s['tags'][:4])}\n"
    if s.get('warnings'):msg+=f"⚠️ {s['warnings'][0]}\n"
    msg+="\n"
    return msg

def build_full_msg(scored,breakouts,dsex):
    now=datetime.now(BD_TZ).strftime("%d %b %Y %I:%M %p")
    stats=get_stats()
    buys=[s for s in scored if 'BUY' in s['signal']]
    sells=[s for s in scored if 'SELL' in s['signal']][:4]
    ai=ai_signal_analysis(buys[:4],breakouts[:3],sells,dsex,stats)

    for s in buys[:8]:
        save_signal(s['symbol'],s['signal'],s['entry'],s['sl'],
                    s['tp1'],s['tp2'],s['score'],s.get('inds',[]))

    msg=f"🏦 *DSE Signal Bot v4*\n"
    msg+=f"📅 {now} | 📊 DSEX:`{dsex}`\n"
    msg+=f"🧠 Win Rate:`{stats['wr']}%` ({stats['wins']}W/{stats['losses']}L/{stats['pending']}P)\n"
    msg+=f"{'━'*22}\n\n"

    # Breakout Scanner
    if breakouts:
        msg+=f"🚀 *BREAKOUT SCANNER — {len(breakouts)} টি*\n"
        msg+="_(Parabolic move এর আগের সংকেত)_\n\n"
        for s in breakouts[:5]:msg+=fmt_breakout(s)
        msg+=f"{'━'*22}\n\n"

    # Standard BUY
    if buys:
        normal=[s for s in buys if s['ltp']>=PENNY_THRESHOLD][:5]
        penny=[s for s in buys if s['ltp']<PENNY_THRESHOLD][:4]
        if normal:
            msg+=f"🟢 *BUY সিগনাল — {len(normal)} টি*\n\n"
            for s in normal:msg+=fmt_signal(s)
        if penny:
            msg+=f"💎 *Penny BUY — {len(penny)} টি* _(বেশি ঝুঁকি)_\n\n"
            for s in penny:msg+=fmt_signal(s)
    else:
        msg+="🟡 আজ BUY সিগনাল নেই\n\n"

    if sells:
        msg+=f"🔴 *SELL — {len(sells)} টি*\n"
        for s in sells:
            msg+=f"*{s['symbol']}* `৳{s['ltp']}` ({s['change']:+.1f}%) Score:{s['score']}\n"
        msg+="\n"

    msg+=f"{'━'*22}\n🤖 *AI বিশ্লেষণ*\n{ai}\n\n"
    msg+=f"💬 _যেকোনো স্টক সম্পর্কে message করুন_\n"
    msg+="⚠️ _Stop Loss সবসময় ব্যবহার করুন।_"
    return msg

# ══════════════════════════════════════
#  SEND SIGNALS
# ══════════════════════════════════════
async def send_signals(bot):
    log.info("Signal job শুরু...")
    await bot.send_message(chat_id=CHAT_ID,
        text="⏳ GitHub historical data + Full Analysis চলছে...")
    try:
        stocks=fetch_stocks()
        if not stocks:
            await bot.send_message(chat_id=CHAT_ID,
                text="❌ ডেটা নেই। DSE বন্ধ (শুক্র/শনি) বা trading hour শেষ।")
            return
        dsex=get_dsex()
        # Clear cache for fresh data
        global _hist_cache;_hist_cache={}
        breakouts=scan_breakouts(stocks)
        scored=analyze(stocks,use_hist=True)
        msg=build_full_msg(scored,breakouts,dsex)
        for i in range(0,len(msg),4000):
            await bot.send_message(chat_id=CHAT_ID,text=msg[i:i+4000],parse_mode='Markdown')
        log.info("✅ Signal sent")
    except Exception as e:
        log.error(f"Error:{e}")
        await bot.send_message(chat_id=CHAT_ID,text=f"❌ সমস্যা:\n{e}")

# ══════════════════════════════════════
#  OUTCOME CHECK
# ══════════════════════════════════════
async def check_outcomes(bot):
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
            conn=sqlite3.connect(DB_PATH);c=conn.cursor()
            if cur>=tp1:
                c.execute('UPDATE signals SET outcome="win",outcome_pct=? WHERE id=?',(pct,sid))
                report+=f"✅ *{sym}* +{pct}%\n";w+=1
            elif cur<=sl:
                c.execute('UPDATE signals SET outcome="loss",outcome_pct=? WHERE id=?',(pct,sid))
                report+=f"❌ *{sym}* {pct}%\n";l+=1
            conn.commit();conn.close()
        if w+l>0:
            report+=f"\n✅{w} ❌{l}"
            await bot.send_message(chat_id=CHAT_ID,text=report,parse_mode='Markdown')
    except Exception as e:
        log.error(f"Outcome error:{e}")

# ══════════════════════════════════════
#  CHAT HANDLER
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
            ctx_txt=f"\n{s['symbol']} Live: ৳{s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
            if ind['ok']:
                ctx_txt+=(f"RSI:{ind['rsi']} MACD:{'↑' if ind['macd_h']>0 else '↓'} "
                         f"BB:{ind['bb_pos']} Trend:{ind['trend']} EW:{ind['ew_phase']}\n"
                         f"Candle:{ind['candle_pattern']} VolRatio:{ind['vol_ratio']}x\n"
                         f"MA20:{ind['ma20']} MA50:{ind['ma50']} EMA9:{ind['ema9']} EMA21:{ind['ema21']}\n"
                         f"SwingHigh:{ind['swing_high']} SwingLow:{ind['swing_low']}\n")
            break
    prompt=(
        f"তুমি DSE Bangladesh expert analyst। বাংলায় সহজ ভাষায় উত্তর দাও।\n"
        f"Bot WinRate:{stats['wr']}% Total:{stats['total']}\n{ctx_txt}\n"
        f"User: {text}\n\n৬-৮ লাইনে technical analysis ও practical পরামর্শ দাও।"
    )
    await update.message.reply_text(ai_chat(prompt))

# ══════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════
async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "🏦 *DSE Signal Bot v4 — GitHub Data Edition*\n\n"
        "✅ ২০১২-২০২৬ সালের historical data connected!\n"
        "✅ প্রতিদিন auto data update\n\n"
        "🔬 *Analysis:*\n"
        "✅ Real Multi-Candle Pattern\n"
        "✅ RSI + MACD + Bollinger Bands\n"
        "✅ MA20, MA50, EMA9, EMA21\n"
        "✅ Elliott Wave Detection\n"
        "✅ Breakout Scanner (Parabolic)\n"
        "✅ Fake Breakout Filter\n"
        "✅ TP1:8% TP2:20% TP3:50%+\n\n"
        "📌 *Commands:*\n"
        "/signal — সম্পূর্ণ বিশ্লেষণ\n"
        "/breakout — Breakout Scanner\n"
        "/stats — Performance\n"
        "/top — Top Gainers\n"
        "/sell — Sell সিগনাল\n"
        "/penny — Penny stocks\n\n"
        "💬 *যেকোনো স্টক message করুন!*\n"
        "🕕 সন্ধ্যা ৬টায় auto signal\n"
        "⚠️ _বিনিয়োগে ঝুঁকি আছে_",parse_mode='Markdown')

async def cmd_signal(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("⏳ GitHub data + Full Analysis চলছে (৩-৫ মিনিট)...")
    await send_signals(ctx.bot)

async def cmd_breakout(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🚀 Breakout Scanner চলছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    candidates=scan_breakouts(stocks)
    if not candidates:
        await u.message.reply_text("আজ কোনো breakout candidate নেই।");return
    msg=f"🚀 *Breakout Scanner — {len(candidates)} টি*\n\n"
    for s in candidates[:6]:msg+=fmt_breakout(s)
    msg+="⚠️ _SL সবসময় দিন।_"
    for i in range(0,len(msg),4000):
        await u.message.reply_text(msg[i:i+4000],parse_mode='Markdown')

async def cmd_stats(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    stats=get_stats()
    msg=f"📊 *Bot Performance*\n\n"
    msg+=f"মোট Signal:`{stats['total']}` | ✅`{stats['wins']}` | ❌`{stats['losses']}`\n"
    msg+=f"⏳ Pending:`{stats['pending']}` | 🎯 Win Rate:`{stats['wr']}%`\n"
    msg+=f"📈 Avg Return:`{stats['avg']}%`\n\n"
    if stats['recent']:
        msg+="🕐 *সাম্প্রতিক:*\n"
        for sym,entry,tp1,outcome,pct,date in stats['recent'][:6]:
            ic="✅" if outcome=='win' else "❌" if outcome=='loss' else "⏳"
            msg+=f"{ic} {sym} ({date}) ৳{entry} → {pct:+.1f}%\n"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_top(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔥 লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    top=sorted(stocks,key=lambda x:x['change'],reverse=True)[:12]
    msg="🔥 *Top 12 Gainers*\n\n"
    for i,s in enumerate(top,1):
        p="💎" if s['ltp']<PENNY_THRESHOLD else ""
        msg+=f"{i}.{p}*{s['symbol']}* `৳{s['ltp']}` (+{s['change']:.1f}%) Vol:{s['volume']:,}\n"
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_sell(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("🔴 লোড হচ্ছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    scored=analyze(stocks,use_hist=True)
    sells=[s for s in scored if 'SELL' in s['signal']]
    if not sells:await u.message.reply_text("আজ SELL নেই।");return
    msg="🔴 *SELL সিগনাল*\n\n"
    for s in sells[:8]:msg+=fmt_signal(s)
    await u.message.reply_text(msg,parse_mode='Markdown')

async def cmd_penny(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("💎 Penny scan চলছে...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("❌ ডেটা নেই।");return
    penny=[s for s in stocks if s['ltp']<PENNY_THRESHOLD]
    if not penny:await u.message.reply_text("আজ penny stock নেই।");return
    scored=analyze(penny,use_hist=True)
    buys=[s for s in scored if 'BUY' in s['signal']]
    if not buys:await u.message.reply_text("আজ penny BUY নেই।");return
    msg="💎 *Penny BUY* _(বেশি ঝুঁকি)_\n\n"
    for s in buys[:8]:msg+=fmt_signal(s)
    await u.message.reply_text(msg,parse_mode='Markdown')

# ══════════════════════════════════════
#  SCHEDULER + MAIN
# ══════════════════════════════════════
async def post_init(app):
    init_db()
    sched=AsyncIOScheduler(timezone='UTC')
    # সন্ধ্যা ৬টায় signal (UTC 12:00)
    sched.add_job(send_signals,'cron',hour=12,minute=0,args=[app.bot])
    # বিকেল ৩টায় data update (UTC 09:00 = BD 15:00)
    sched.add_job(auto_update_data,'cron',hour=9,minute=30,args=[app.bot])
    # সকাল ১০টায় outcome check (UTC 04:00)
    sched.add_job(check_outcomes,'cron',hour=4,minute=0,args=[app.bot])
    sched.start()
    log.info("✅ Scheduler: Signal UTC12 | Update UTC09:30 | Check UTC04")

def main():
    init_db()
    log.info("🚀 DSE Signal Bot v4 GitHub Edition চালু...")
    app=Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("breakout",cmd_breakout))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("top",     cmd_top))
    app.add_handler(CommandHandler("sell",    cmd_sell))
    app.add_handler(CommandHandler("penny",   cmd_penny))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_message))
    log.info("✅ v4 GitHub Edition polling শুরু")
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__':
    main()
