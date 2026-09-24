"""
DSE Daily Data Updater
Protidin DSE close er por today's data add kore
"""
import requests,csv,os,time
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup

# ── dsebd.org SSL compat ──
# 2026-09-22 theke GitHub Actions runner e dsebd.org-er sathe
# "SSLV3_ALERT_HANDSHAKE_FAILURE" ashche (21 Sep porjonto thik chilo) -
# server-er TLS/cipher config probably bodleche, notun OpenSSL-er
# default security level e negotiate hocche na. Prothome normal request
# kori; SSLError hole legacy-compatible SSL context diye abar try kori.
import ssl as _ssl
from requests.adapters import HTTPAdapter as _HTTPAdapter

class _LegacySSLAdapter(_HTTPAdapter):
    def init_poolmanager(self,*args,**kwargs):
        ctx=_ssl.create_default_context()
        ctx.check_hostname=False
        ctx.verify_mode=_ssl.CERT_NONE
        try:ctx.set_ciphers('DEFAULT@SECLEVEL=0')
        except Exception:pass
        ctx.options|=getattr(_ssl,'OP_LEGACY_SERVER_CONNECT',0x4)
        try:ctx.minimum_version=_ssl.TLSVersion.TLSv1
        except Exception:pass
        kwargs['ssl_context']=ctx
        return super().init_poolmanager(*args,**kwargs)

_legacy_session=None
def _dse_get(url,**kwargs):
    global _legacy_session
    try:
        return requests.get(url,**kwargs)
    except requests.exceptions.SSLError as e:
        print(f"SSL error, legacy SSL context diye retry: {e.__class__.__name__}")
        if _legacy_session is None:
            _legacy_session=requests.Session()
            _legacy_session.mount('https://',_LegacySSLAdapter())
        kwargs.pop('verify',None)
        return _legacy_session.get(url,verify=False,**kwargs)
from datetime import datetime

HEADERS={'User-Agent':'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120'}
DATA_DIR='data'

def is_dse_trading_day(date_str):
    """Shudhu Fri/Sat na, Eid/Puja/sorkari chhutir din-o (jegulo Sun-Thu
    er modhdhe porte pare) dhorte dsebd.org er day_end_archive.php
    directly check kori - shei din shotti trade hoyeche kina.

    NOTE: age eta shudhu ?endDate=... diye query hoto (single-date-only
    form), kintu oi form dsebd.org theke আসল archive table dey na -
    ekta live ticker-strip page dey jeta unreliable vabe pass/fail
    dite pare. bot.py te age eta fix kora hoyechilo
    (?startDate=X&endDate=X range form byabohar kore, same date dutoy)
    kintu ei script (update_data.py, GitHub Actions e alada vabe chole)
    ke miss kora hoyechilo - fole 2026-08-27 (shotti trading day) e
    o eta walo update skip kore fele, karon single-endDate form
    incorrectly "no data table" dekhiyechilo. Ekhon bot.py-r shathe
    consistent range-query form byabohar kora hocche."""
    try:
        url=f"https://www.dsebd.org/day_end_archive.php?startDate={date_str}&endDate={date_str}&archive=data"
        r=_dse_get(url,headers=HEADERS,timeout=15,verify=False)
        print(f"TradingDayCheck({date_str}): HTTP {r.status_code}, response length {len(r.text)}")
        if r.status_code!=200:return None
        soup=BeautifulSoup(r.text,'html.parser')
        tables=soup.find_all('table')
        print(f"TradingDayCheck({date_str}): {len(tables)} tables found")
        for t in tables:
            rows=t.find_all('tr')
            if len(rows)<2:continue
            header_txt=' '.join(c.get_text(strip=True).upper() for c in rows[0].find_all(['th','td']))
            if 'TRADING CODE' in header_txt or 'LTP' in header_txt:
                print(f"TradingDayCheck({date_str}): real archive table found ({len(rows)} rows) -> trading day")
                return True
        print(f"TradingDayCheck({date_str}): no real data table -> treating as non-trading day")
        return False
    except Exception as e:
        print(f"TradingDayCheck error: {e}")
        return None

def fetch_live_api():
    """dsebd.org 2026-09-24 e redesign hoyeche (Next.js). Notun site-er JSON
    API theke data ani:
      /api/live/market -> session.sessionDate (shesh trading din), tradingDay
      /api/live/prices -> protita instrument:
        [code, ltp, ycp, open, high, low, closep, volume, value_mn, trades,
         pct_change, category, ..., sector, type]
    (BNICL/BRACBANK diye market-movers er open/high/low er sathe mile
    verify kora.) Ager HTML table-e OPEN chilo na, ekhon ashol Open pai.
    Return (session_date, {sym: row}) ba (None, {}) fail hole."""
    try:
        m=_dse_get("https://dsebd.org/api/live/market",headers=HEADERS,timeout=30,verify=False).json()
        sess=m.get('session',{}) or {}
        session_date=sess.get('sessionDate')
        print(f"Live market: sessionDate={session_date} tradingDay={sess.get('tradingDay')} phase={sess.get('phase')} tradeTime={m.get('totals',{}).get('tradeTime')}")
        p=_dse_get("https://dsebd.org/api/live/prices",headers=HEADERS,timeout=30,verify=False).json()
        rows=p if isinstance(p,list) else (p.get('rows') or p.get('data') or p.get('prices') or [])
        stocks={}
        for r in rows:
            if not isinstance(r,list) or len(r)<8:continue
            code=str(r[0]).strip().upper()
            try:
                ltp,ycp,op,hi,lo,closep,vol=[float(x or 0) for x in r[1:8]]
            except Exception:continue
            if closep<=0 or vol<=0:continue  # aj trade hoyni
            if op<=0:op=ycp if ycp>0 else closep
            if hi<=0:hi=max(op,closep)
            if lo<=0:lo=min(op,closep)
            stocks[code]={'Date':session_date,'Open':round(op,2),'High':round(hi,2),
                          'Low':round(lo,2),'Close':round(closep,2),'Volume':int(vol)}
        print(f"Live API: {len(rows)} instrument, {len(stocks)} traded")
        return session_date,stocks
    except Exception as e:
        print(f"Live API error: {e}")
        return None,{}

def update_csv(symbol,row):
    """CSV file e notun row add koro"""
    path=f"{DATA_DIR}/{symbol}.csv"
    if not os.path.exists(path):
        # New stock - create file
        with open(path,'w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['Date','Open','High','Low','Close','Volume'])
            w.writeheader()
            w.writerow(row)
        return True

    with open(path,'r') as f:
        lines=f.readlines()

    # Check if today already exists
    for line in lines[-5:]:
        if row['Date'] in line:
            return False

    # File trailing newline chara thakle age newline add kori - noile
    # notun row age-er last line er shathe mishe CSV corrupt hoye jay
    needs_leading_nl=False
    if os.path.getsize(path)>0:
        with open(path,'rb') as f:
            f.seek(-1,2)
            if f.read(1)!=b'\n':
                needs_leading_nl=True

    with open(path,'a',newline='') as f:
        if needs_leading_nl:
            f.write('\n')
        w=csv.writer(f)
        w.writerow([row['Date'],row['Open'],row['High'],row['Low'],row['Close'],row['Volume']])
    return True

def main():
    print("DSE data update shuru...")
    # sessionDate = DSE-er nijer bola shesh trading din. Chhutir din / Fri /
    # Sat e eta ager trading din-ei thake, ar oi date-er row age thekei ache
    # bole update_csv() skip kore - tai holiday duplicate row ar hobe na,
    # alada holiday-check lage na.
    session_date,stocks=fetch_live_api()
    if not session_date or not stocks:
        print("Kono data pawa jaini - update skip")
        return
    try:
        datetime.strptime(session_date,'%Y-%m-%d')
    except Exception:
        print(f"Onirvorjoggo sessionDate '{session_date}' - skip")
        return
    updated=0;skipped=0
    for sym,row in stocks.items():
        if update_csv(sym,row):updated+=1
        else:skipped+=1
    print(f"Done! Date:{session_date} Updated:{updated} Skipped(already exists):{skipped}")

class _Tee:
    """update_data.py-r output data/_debug_update.txt e-o likhi, jate
    workflow commit-e ashe - Actions log baire theke pora jay na."""
    def __init__(self,*s):self.s=s
    def write(self,x):
        for f in self.s:f.write(x)
    def flush(self):
        for f in self.s:f.flush()

if __name__=='__main__':
    import sys
    _logf=open(f"{DATA_DIR}/_debug_update.txt","w")
    sys.stdout=_Tee(sys.__stdout__,_logf)
    print(f"=== update_data run {datetime.now().isoformat()} ===")
    try:
        main()
    finally:
        sys.stdout=sys.__stdout__
        _logf.close()
