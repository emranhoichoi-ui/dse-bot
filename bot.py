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
SEP='='*25

_hist_cache={}

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
#  GITHUB DATA
# ══════════════════════════════════════
def get_hist_from_github(symbol):
    global _hist_cache
    cache_key=f"{symbol}_{datetime.now(BD_TZ).strftime('%Y%m%d')}"
    if cache_key in _hist_cache:
        return _hist_cache[cache_key]
    try:
        url=f"{GITHUB_RAW}/{symbol}.csv"
        r=requests.get(url,headers={'User-Agent':'Mozilla/5.0'},timeout=15)
        if r.status_code!=200:return None
        reader=csv.DictReader(io.StringIO(r.text))
        rows=list(reader)
        if not rows:return None
        closes=[float(row['Close']) for row in rows if row.get('Close')]
        highs=[float(row['High']) for row in rows if row.get('High')]
        lows=[float(row['Low']) for row in rows if row.get('Low')]
        opens=[float(row['Open']) for row in rows if row.get('Open')]
        vols=[float(row['Volume']) for row in rows if row.get('Volume')]
        dates=[row['Date'] for row in rows if row.get('Date')]
        data={'closes':closes,'highs':highs,'lows':lows,'opens':opens,'vols':vols,'dates':dates}
        _hist_cache[cache_key]=data
        return data
    except Exception as e:
        log.error(f"GitHub fetch {symbol}: {e}")
        return None

# ══════════════════════════════════════
#  MATH
# ══════════════════════════════════════
def sf(txt):
    try:return float(str(txt).strip().replace(',','').replace('%',''))
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
#  INDICATORS
# ══════════════════════════════════════
def get_all_indicators(symbol):
    data=get_hist_from_github(symbol)
    if not data or len(data['closes'])<20:
        return{'ok':False,'rsi':50,'macd_h':0,'bb_pos':'mid',
               'ma20':0,'ma50':0,'ema9':0,'ema21':0,'trend':'neutral',
               'avg_vol':0,'vol_ratio':0,'swing_high':0,'swing_low':0,
               'ew_phase':'unknown','fake_break':False,
               'candle_pattern':'N/A','pattern_score':0,'base_days':0}

    closes=data['closes'];highs=data['highs']
    lows=data['lows'];opens=data['opens'];vols=data['vols']

    rsi=rsi_calc(closes)
    ml,sl_,mh=macd_calc(closes)
    bbu,bbm,bbl=bb_calc(closes)
    ma20=sma_calc(closes,20)
    ma50=sma_calc(closes,min(50,len(closes)))
    e9=ema_calc(closes,min(9,len(closes)))
    e21=ema_calc(closes,min(21,len(closes)))
    last=closes[-1]

    bp='upper' if last>bbu else 'lower' if last<bbl else 'mid'

    if e9>e21>ma20>ma50:trend='strong_up'
    elif e9>e21 and ma20>ma50:trend='up'
    elif e9<e21<ma20<ma50:trend='strong_down'
    elif e9<e21 and ma20<ma50:trend='down'
    else:trend='neutral'

    avg_vol=sum(vols[-20:])/max(len(vols[-20:]),1) if vols else 0
    cur_vol=vols[-1] if vols else 0
    vol_ratio=round(cur_vol/avg_vol,2) if avg_vol>0 else 0

    swing_high=max(highs[-20:]) if len(highs)>=20 else 0
    swing_low=min(lows[-20:]) if len(lows)>=20 else 0

    ew_phase='unknown'
    if len(closes)>=10:
        rec=sum(closes[-5:])/5;old=sum(closes[-10:-5])/5
        if last>rec>old:ew_phase='wave3_5'
        elif last<rec<old:ew_phase='wave_down'
        elif old>rec and last>rec:ew_phase='wave2_4_end'
        else:ew_phase='neutral'

    fake_break=False
    if len(highs)>=6:
        prev_high=max(highs[-6:-1])
        if last>prev_high and rsi>75:fake_break=True

    # Multi-candle pattern
    candle_pattern='none';pattern_score=0
    if len(closes)>=5 and len(opens)>=5:
        c0,c1,c2=closes[-3],closes[-2],closes[-1]
        o0,o1,o2=opens[-3],opens[-2],opens[-1]
        h0,h1,h2=highs[-3],highs[-2],highs[-1]
        l0,l1,l2=lows[-3],lows[-2],lows[-1]
        body0=abs(c0-o0);body1=abs(c1-o1);body2=abs(c2-o2)
        rng2=h2-l2 if h2>l2 else 0.01
        bull2=c2>o2;bull1=c1>o1;bull0=c0>o0
        uw2=(h2-max(c2,o2))/rng2
        lw2=(min(c2,o2)-l2)/rng2
        prev_down=closes[-5]>closes[-3] if len(closes)>=5 else False

        if lw2>0.5 and uw2<0.1 and bull2 and prev_down:
            candle_pattern='Real Hammer';pattern_score=6
        elif lw2>0.5 and uw2<0.1 and bull2:
            candle_pattern='Hammer';pattern_score=3
        elif not bull1 and bull2 and c2>o1 and o2<c1 and body2>body1:
            candle_pattern='Bullish Engulfing';pattern_score=6
        elif not bull0 and body1<body0*0.3 and bull2 and c2>((c0+o0)/2):
            candle_pattern='Morning Star';pattern_score=7
        elif bull0 and bull1 and bull2 and c2>c1>c0:
            candle_pattern='3 White Soldiers';pattern_score=8
        elif uw2>0.5 and lw2<0.1 and not bull2:
            candle_pattern='Shooting Star';pattern_score=-6
        elif bull0 and body1<body0*0.3 and not bull2 and c2<((c0+o0)/2):
            candle_pattern='Evening Star';pattern_score=-7
        elif bull1 and not bull2 and o2>c1 and c2<o1 and body2>body1:
            candle_pattern='Bearish Engulfing';pattern_score=-6
        elif not bull0 and not bull1 and not bull2 and c2<c1<c0:
            candle_pattern='3 Black Crows';pattern_score=-8
        elif body2<rng2*0.1:
            candle_pattern='Doji';pattern_score=0
        elif bull2 and body2>rng2*0.6:
            candle_pattern='Strong Bull';pattern_score=2

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
        'vol_ratio':vol_ratio,'swing_high':swing_high,'swing_low':swing_low,
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
        ltp=s['ltp'];chg=s['change']
        vol_ratio=ind['vol_ratio']

        if vol_ratio>=5:
            score+=8;signals.append(f"Vol {vol_ratio}x")
            reasons.append(f"Volume gorore {vol_ratio}x - institutional entry")
        elif vol_ratio>=3:
            score+=6;signals.append(f"Vol {vol_ratio}x")
            reasons.append(f"Volume gorore {vol_ratio}x - strong buying")
        elif vol_ratio>=2:
            score+=4;signals.append(f"Vol {vol_ratio}x")
        elif vol_ratio>=1.5:
            score+=2;signals.append(f"Vol {vol_ratio}x")
        else:continue

        if ind['base_days']>0:
            score+=5;signals.append(f"Base {ind['base_days']}d")
            reasons.append(f"{ind['base_days']} diner consolidation - parabolic move shomvob")

        sh=ind['swing_high']
        if sh>0 and ltp>=sh*0.98:
            score+=4;signals.append("Swing Break")
            reasons.append(f"20 diner high {sh} break hoyeche")

        tr=ind['trend']
        if tr=='strong_up':
            score+=5;signals.append("EMA9>EMA21>MA20>MA50")
            reasons.append("Perfect MA alignment - strong uptrend")
        elif tr=='up':
            score+=3;signals.append("Trend Up")

        rsi=ind['rsi']
        if 50<=rsi<=65:
            score+=4;signals.append(f"RSI:{rsi} OK")
            reasons.append(f"RSI {rsi} - breakout zone, aro upore jawar space ache")
        elif 45<=rsi<50:
            score+=2;signals.append(f"RSI:{rsi}")
        elif rsi>75:
            score-=3;signals.append(f"RSI:{rsi} OB")

        if ind['macd_h']>0 and ind['macd']>ind['macd_sig']:
            score+=3;signals.append("MACD Bull")
            reasons.append("MACD bullish crossover confirmed")
        elif ind['macd_h']>0:
            score+=1;signals.append("MACD Up")

        cp=ind['candle_pattern'];ps=ind['pattern_score']
        if ps>=4:score+=ps;signals.append(cp)
        elif ps>0:score+=ps;signals.append(cp)
        elif ps<0:score+=ps

        if ind['bb_pos']=='lower':
            score+=2;signals.append("BB Lower")
        elif ind['bb_pos']=='upper' and vol_ratio>3:
            score+=1;signals.append("BB Upper+Vol")

        ep=ind['ew_phase']
        if ep=='wave2_4_end':
            score+=5;signals.append("EW Wave3/5")
            reasons.append("EW Wave 2/4 shesh - shobcheye shoktishshali impulse ashche")
        elif ep=='wave3_5':
            score+=2;signals.append("EW Impulse")

        if ind['fake_break']:
            score-=4;signals.append("FakeBreak!")

        if score<8:continue

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
        log.info(f"OK {len(unique)} stocks");return unique
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

        ind={'ok':False,'rsi':50,'trend':'neutral','ew_phase':'unknown',
             'fake_break':False,'candle_pattern':'N/A','pattern_score':0,
             'macd_h':0,'bb_pos':'mid','vol_ratio':0,'macd':0,'macd_sig':0}

        if use_hist:
            ind=get_all_indicators(s['symbol'])

        if ind['ok']:
            cp_=ind['candle_pattern'];ps=ind['pattern_score']
            if ps>=6:score+=ps;tags.append(cp_);inds.append('candle_bull')
            elif ps>=3:score+=ps;tags.append(cp_)
            elif ps>0:score+=ps;tags.append(cp_)
            elif ps<=-6:score+=ps;tags.append(cp_);inds.append('candle_bear')
            elif ps<0:score+=ps;tags.append(cp_)

            rsi=ind['rsi']
            if rsi<30:score+=4;tags.append(f"RSI:{rsi} Oversold");inds.append('rsi_os')
            elif 30<=rsi<45:score+=3;tags.append(f"RSI:{rsi}");inds.append('rsi_good')
            elif 45<=rsi<60:score+=1;tags.append(f"RSI:{rsi}")
            elif rsi>=75:score-=3;tags.append(f"RSI:{rsi} OB");warnings.append(f"RSI {rsi} overbought")
            else:tags.append(f"RSI:{rsi}")

            if ind['macd_h']>0 and ind['macd']>ind['macd_sig']:
                score+=3;tags.append("MACD Bull");inds.append('macd_bull')
            elif ind['macd_h']<0 and ind['macd']<ind['macd_sig']:
                score-=3;tags.append("MACD Bear")

            if ind['bb_pos']=='lower':score+=3;tags.append("BB Lower");inds.append('bb_low')
            elif ind['bb_pos']=='upper':score-=2;tags.append("BB Upper")
            else:tags.append("BB Mid")

            tr=ind['trend']
            if tr=='strong_up':score+=5;tags.append("Trend StrongUp");inds.append('trend_sup')
            elif tr=='up':score+=2;tags.append("Trend Up")
            elif tr=='strong_down':score-=4;tags.append("Trend StrongDown")
            elif tr=='down':score-=2;tags.append("Trend Down")

            ep=ind['ew_phase']
            if ep=='wave2_4_end':score+=4;tags.append("EW W2/4 End");inds.append('ew_end')
            elif ep=='wave3_5':score+=2;tags.append("EW Impulse")
            elif ep=='wave_down':score-=2;tags.append("EW Down")

            vr=ind['vol_ratio']
            if vr>=3:score+=3;tags.append(f"Vol {vr}x")
            elif vr>=2:score+=2;tags.append(f"Vol {vr}x")
            elif vr>=1.5:score+=1;tags.append(f"Vol {vr}x")

            if ind['fake_break']:score-=4;tags.append("FakeBreak");warnings.append("Fake breakout risk")
        else:
            if lw>0.4 and uw<0.15 and cp>0.6:score+=2;tags.append("Bull Shape")
            elif uw>0.4 and lw<0.15:score-=2;tags.append("Bear Shape")
            if vol>3000000:score+=3;tags.append(f"Vol:{vol//1000}K")
            elif vol>1000000:score+=2;tags.append(f"Vol:{vol//1000}K")
            elif vol>300000:score+=1;tags.append(f"Vol:{vol//1000}K")
            else:tags.append(f"Vol:{vol//1000}K")

        if 1.5<chg<=5:score+=2;tags.append(f"+{chg:.1f}%")
        elif chg>5:score+=1;tags.append(f"+{chg:.1f}%")
        elif chg<-3:score-=2;tags.append(f"{chg:.1f}%")
        elif chg<-1:score-=1;tags.append(f"{chg:.1f}%")

        f618=lo+rng*0.618;f382=lo+rng*0.382
        if ltp>0:
            if abs(ltp-f618)/ltp<0.015:score+=2;tags.append("Fib 0.618")
            elif abs(ltp-f382)/ltp<0.015:score+=1;tags.append("Fib 0.382")

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

# ══════════════════════════════════════
#  AI
# ══════════════════════════════════════
def ai_chat(prompt):
    try:
        client=anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp=client.messages.create(model="claude-sonnet-4-20250514",max_tokens=500,
                                    messages=[{"role":"user","content":prompt}])
        return resp.content[0].text
    except Exception as e:
        return f"AI credit shesh. Notun key lagbe. Error: {str(e)[:50]}"

def ai_summary(buys,breakouts,sells,dsex,stats):
    today=datetime.now(BD_TZ).strftime("%d %B %Y")
    txt=""
    for s in buys[:4]:
        ind=s.get('ind',{})
        txt+=f"{s['symbol']}: {s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,} RSI:{ind.get('rsi','-')} Trend:{ind.get('trend','-')}\n"
    brk=""
    for s in breakouts[:3]:
        brk+=f"{s['symbol']}: Vol {s['vol_ratio']}x\n"
    prompt=(
        f"Tumi DSE Bangladesh expert analyst. Banglay sohoj vashay uttor dao.\n"
        f"Tarikh:{today} DSEX:{dsex} WinRate:{stats['wr']}%\n\n"
        f"BUY signal:\n{txt}\nBreakout:\n{brk}\n\n"
        f"Protita BUY stock er jonno 2 laine: keno kinben, ki risk. "
        f"Sheshe 2 laine market obostha."
    )
    return ai_chat(prompt)

# ══════════════════════════════════════
#  MESSAGE BUILDERS (Plain Text Only)
# ══════════════════════════════════════
def fmt_breakout(s):
    tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
    tp2_pct=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
    tp3_pct=round((s['tp3']-s['ltp'])/s['ltp']*100,1)
    lines=[
        f"BREAKOUT: {s['symbol']} (Score:{s['score']})",
        f"Daam: {s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,} ({s['vol_ratio']}x)",
        f"RSI:{s['rsi']} Trend:{s['trend']} Candle:{s['candle']}",
    ]
    if s['base_days']>0:lines.append(f"Base: {s['base_days']} din")
    lines.append(f"Entry:{s['entry']} SL:{s['sl']}")
    lines.append(f"TP1:{s['tp1']} (+{tp1_pct}%)")
    lines.append(f"TP2:{s['tp2']} (+{tp2_pct}%)")
    lines.append(f"TP3:{s['tp3']} (+{tp3_pct}%) Parabolic Target")
    lines.append(f"Signals: {' | '.join(s['signals'][:4])}")
    if s.get('reasons'):lines.append(f"Reason: {s['reasons'][0]}")
    lines.append("")
    return "\n".join(lines)

def fmt_signal(s):
    ind=s.get('ind',{})
    rsi=ind.get('rsi','-') if ind.get('ok') else '-'
    tr=ind.get('trend','-') if ind.get('ok') else '-'
    cp=ind.get('candle_pattern','') if ind.get('ok') else ''
    tp1_pct=round((s['tp1']-s['ltp'])/s['ltp']*100,1)
    tp2_pct=round((s['tp2']-s['ltp'])/s['ltp']*100,1)
    lines=[
        f"{s['symbol']} -- {s['signal']} (Score:{s['score']})",
        f"Daam: {s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,}",
    ]
    if rsi!='-':lines.append(f"RSI:{rsi} Trend:{tr}")
    if cp and cp not in('none','N/A'):lines.append(f"Candle: {cp}")
    lines.append(f"Entry:{s['entry']} SL:{s['sl']}")
    lines.append(f"TP1:{s['tp1']} (+{tp1_pct}%) TP2:{s['tp2']} (+{tp2_pct}%)")
    if s.get('tags'):lines.append(f"Tags: {' | '.join(s['tags'][:4])}")
    if s.get('warnings'):lines.append(f"Shotorkota: {s['warnings'][0]}")
    lines.append("")
    return "\n".join(lines)

def build_msg(scored,breakouts,dsex):
    now=datetime.now(BD_TZ).strftime("%d %b %Y %I:%M %p")
    stats=get_stats()
    buys=[s for s in scored if 'BUY' in s['signal']]
    sells=[s for s in scored if 'SELL' in s['signal']][:4]
    ai=ai_summary(buys[:4],breakouts[:3],sells,dsex,stats)

    for s in buys[:8]:
        save_signal(s['symbol'],s['signal'],s['entry'],s['sl'],
                    s['tp1'],s['tp2'],s['score'],s.get('inds',[]))

    parts=[]
    parts.append(f"DSE Signal Bot v4")
    parts.append(f"Tarikh: {now}")
    parts.append(f"DSEX: {dsex}")
    parts.append(f"Win Rate: {stats['wr']}% ({stats['wins']}W/{stats['losses']}L/{stats['pending']}P)")
    parts.append(SEP)

    if breakouts:
        parts.append(f"BREAKOUT SCANNER -- {len(breakouts)} ti Stock")
        parts.append("(Parabolic move er agher shongket)")
        parts.append("")
        for s in breakouts[:5]:parts.append(fmt_breakout(s))
        parts.append(SEP)

    if buys:
        normal=[s for s in buys if s['ltp']>=PENNY_THRESHOLD][:5]
        penny=[s for s in buys if s['ltp']<PENNY_THRESHOLD][:4]
        if normal:
            parts.append(f"BUY SIGNAL -- {len(normal)} ti")
            parts.append("")
            for s in normal:parts.append(fmt_signal(s))
        if penny:
            parts.append(f"PENNY BUY -- {len(penny)} ti (beshi jhuki)")
            parts.append("")
            for s in penny:parts.append(fmt_signal(s))
    else:
        parts.append("Aj kono BUY signal nei")
        parts.append("")

    if sells:
        parts.append(f"SELL -- {len(sells)} ti")
        for s in sells:
            parts.append(f"{s['symbol']} {s['ltp']} ({s['change']:+.1f}%) Score:{s['score']}")
        parts.append("")

    parts.append(SEP)
    parts.append("AI Bishleshan:")
    parts.append(ai)
    parts.append("")
    parts.append("Jekono stock somporke message korun!")
    parts.append("STOP LOSS shobshomai byabohar korun.")

    return "\n".join(parts)

# ══════════════════════════════════════
#  AUTO UPDATE
# ══════════════════════════════════════
async def auto_update_data(bot):
    log.info("Auto data update...")
    stocks=fetch_stocks()
    if not stocks:return
    today=datetime.now(BD_TZ).strftime('%Y-%m-%d')
    updated=0
    for s in stocks:
        try:
            url=f"{GITHUB_API}/{s['symbol']}.csv"
            gh_headers={'Authorization':f'token {GITHUB_TOKEN}','Accept':'application/vnd.github.v3+json'}
            r=requests.get(url,headers=gh_headers,timeout=15)
            if r.status_code!=200:continue
            info=r.json()
            import base64
            old=base64.b64decode(info['content']).decode('utf-8')
            if today in old:continue
            new_line=f"\n{today},{s['high']},{s['high']},{s['low']},{s['ltp']},{s['volume']}"
            new_content=old.rstrip()+new_line
            encoded=base64.b64encode(new_content.encode()).decode()
            payload={'message':f"Update {s['symbol']} {today}",'content':encoded,'sha':info['sha']}
            requests.put(url,headers=gh_headers,json=payload,timeout=20)
            updated+=1
        except:pass
    global _hist_cache;_hist_cache={}
    log.info(f"Updated {updated} stocks")

# ══════════════════════════════════════
#  SEND SIGNALS
# ══════════════════════════════════════
async def send_signals(bot):
    log.info("Signal job shuru...")
    await bot.send_message(chat_id=CHAT_ID,text="Bishleshan cholche, ektu opekkha korun...")
    try:
        stocks=fetch_stocks()
        if not stocks:
            await bot.send_message(chat_id=CHAT_ID,text="Data nei. DSE bondho (Shukro/Shani) ba trading hour shesh.")
            return
        dsex=get_dsex()
        global _hist_cache;_hist_cache={}
        breakouts=scan_breakouts(stocks)
        scored=analyze(stocks,use_hist=True)
        msg=build_msg(scored,breakouts,dsex)
        # Split into 4000 char chunks
        for i in range(0,len(msg),4000):
            await bot.send_message(chat_id=CHAT_ID,text=msg[i:i+4000])
        log.info("Signal pathano hoyeche")
    except Exception as e:
        log.error(f"Error:{e}")
        await bot.send_message(chat_id=CHAT_ID,text=f"Shomshsha hoyeche: {str(e)[:200]}")

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
            ctx_txt=f"\n{s['symbol']}: {s['ltp']} ({s['change']:+.1f}%) Vol:{s['volume']:,}\n"
            if ind['ok']:
                ctx_txt+=f"RSI:{ind['rsi']} MACD:{'up' if ind['macd_h']>0 else 'down'} BB:{ind['bb_pos']} Trend:{ind['trend']} EW:{ind['ew_phase']}\n"
                ctx_txt+=f"Candle:{ind['candle_pattern']} Vol:{ind['vol_ratio']}x\n"
                ctx_txt+=f"MA20:{ind['ma20']} MA50:{ind['ma50']} EMA9:{ind['ema9']} EMA21:{ind['ema21']}\n"
            break
    prompt=(
        f"Tumi DSE expert analyst. Banglay sohoj vashay uttor dao.\n"
        f"WinRate:{stats['wr']}% Total:{stats['total']}\n{ctx_txt}\n"
        f"User: {text}\n\n6-8 laine technical analysis o practical poramorsh dao."
    )
    reply=ai_chat(prompt)
    await update.message.reply_text(reply)

# ══════════════════════════════════════
#  COMMANDS
# ══════════════════════════════════════
async def cmd_start(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    msg=(
        "DSE Signal Bot v4 - GitHub Data Edition\n\n"
        "2012-2026 historical data connected!\n"
        "Protidin auto data update hoy.\n\n"
        "Analysis Engine:\n"
        "- Real Multi-Candle Pattern (10+ types)\n"
        "- RSI + MACD + Bollinger Bands\n"
        "- MA20, MA50, EMA9, EMA21\n"
        "- Elliott Wave Detection\n"
        "- Breakout Scanner (Parabolic)\n"
        "- Fake Breakout Filter\n"
        "- TP1:8% TP2:20% TP3:50%+\n\n"
        "Commands:\n"
        "/signal - Purno bishleshan\n"
        "/breakout - Breakout scanner\n"
        "/stats - Performance\n"
        "/top - Top Gainers\n"
        "/sell - Sell signal\n"
        "/penny - Penny stocks\n\n"
        "Jekono stock er naam likhe pathiye din!\n"
        "Sondha 6tar auto signal ashbe.\n"
        "BIJONOGE JHUKI ACHE."
    )
    await u.message.reply_text(msg)

async def cmd_signal(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Bishleshan cholche (3-5 minit lagbe)...")
    await send_signals(ctx.bot)

async def cmd_breakout(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Breakout scanner cholche...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    candidates=scan_breakouts(stocks)
    if not candidates:
        await u.message.reply_text("Aj kono breakout candidate nei.");return
    msg=f"BREAKOUT SCANNER -- {len(candidates)} ti Stock\n\n"
    for s in candidates[:6]:msg+=fmt_breakout(s)
    msg+="STOP LOSS shobshomai din."
    for i in range(0,len(msg),4000):
        await u.message.reply_text(msg[i:i+4000])

async def cmd_stats(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    stats=get_stats()
    msg=f"Bot Performance:\n\n"
    msg+=f"Moto Signal: {stats['total']}\n"
    msg+=f"Win: {stats['wins']} | Loss: {stats['losses']}\n"
    msg+=f"Pending: {stats['pending']}\n"
    msg+=f"Win Rate: {stats['wr']}%\n"
    msg+=f"Avg Return: {stats['avg']}%\n\n"
    if stats['recent']:
        msg+="Shampratik:\n"
        for sym,entry,tp1,outcome,pct,date in stats['recent'][:6]:
            ic="WIN" if outcome=='win' else "LOSS" if outcome=='loss' else "..."
            msg+=f"{ic} {sym} ({date}) {entry} -> {pct:+.1f}%\n"
    await u.message.reply_text(msg)

async def cmd_top(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Lod holche...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    top=sorted(stocks,key=lambda x:x['change'],reverse=True)[:12]
    msg="Top 12 Gainers:\n\n"
    for i,s in enumerate(top,1):
        p="[Penny] " if s['ltp']<PENNY_THRESHOLD else ""
        msg+=f"{i}. {p}{s['symbol']} {s['ltp']} (+{s['change']:.1f}%) Vol:{s['volume']:,}\n"
    await u.message.reply_text(msg)

async def cmd_sell(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Lod holche...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    scored=analyze(stocks,use_hist=True)
    sells=[s for s in scored if 'SELL' in s['signal']]
    if not sells:await u.message.reply_text("Aj SELL nei.");return
    msg="SELL Signal:\n\n"
    for s in sells[:8]:msg+=fmt_signal(s)
    await u.message.reply_text(msg)

async def cmd_penny(u:Update,ctx:ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text("Penny scan cholche...")
    stocks=fetch_stocks()
    if not stocks:await u.message.reply_text("Data nei.");return
    penny=[s for s in stocks if s['ltp']<PENNY_THRESHOLD]
    if not penny:await u.message.reply_text("Aj penny stock nei.");return
    scored=analyze(penny,use_hist=True)
    buys=[s for s in scored if 'BUY' in s['signal']]
    if not buys:await u.message.reply_text("Aj penny BUY nei.");return
    msg="Penny BUY (beshi jhuki):\n\n"
    for s in buys[:8]:msg+=fmt_signal(s)
    await u.message.reply_text(msg)

# ══════════════════════════════════════
#  SCHEDULER + MAIN
# ══════════════════════════════════════
async def post_init(app):
    init_db()
    sched=AsyncIOScheduler(timezone='UTC')
    sched.add_job(send_signals,'cron',hour=12,minute=0,args=[app.bot])
    sched.add_job(auto_update_data,'cron',hour=9,minute=30,args=[app.bot])
    sched.add_job(check_outcomes,'cron',hour=4,minute=0,args=[app.bot])
    sched.start()
    log.info("Scheduler: Signal UTC12 | Update UTC09:30 | Check UTC04")

def main():
    init_db()
    log.info("DSE Signal Bot v4 shuru holche...")
    app=Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("signal",  cmd_signal))
    app.add_handler(CommandHandler("breakout",cmd_breakout))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("top",     cmd_top))
    app.add_handler(CommandHandler("sell",    cmd_sell))
    app.add_handler(CommandHandler("penny",   cmd_penny))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_message))
    log.info("Bot polling shuru")
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__':
    main()
