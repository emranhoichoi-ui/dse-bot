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

def fetch_today():
    """dsebd.org theke ajer sob stock er data ano"""
    # 2026-09-24 theke ..._by_value.php HTTP 404 dicche, kintu dsebd.org-er
    # onno latest-share-price page gulo (same table format) chalu ache -
    # tai ekta ekta kore try kori, prothom je ta 200 dey seta use kori.
    urls=["https://www.dsebd.org/latest_share_price_scroll_by_value.php",
          "https://www.dsebd.org/latest_share_price_scroll_by_ltp.php",
          "https://www.dsebd.org/latest_share_price_scroll_by_change.php",
          "https://www.dsebd.org/latest_share_price_scroll_l.php",
          "https://www.dsebd.org/latest_share_price_scroll_group.php"]
    stocks={}
    today=datetime.now().strftime('%Y-%m-%d')
    try:
        r=None
        for url in urls:
            try:
                rr=_dse_get(url,headers=HEADERS,timeout=30,verify=False)
                print(f"  try {url.split('/')[-1]}: HTTP {rr.status_code}")
                if rr.status_code==200:
                    r=rr;break
            except Exception as ee:
                print(f"  try {url.split('/')[-1]}: {ee.__class__.__name__}")
        if r is None:
            raise Exception("kono latest-share-price page-i 200 dey ni")
        soup=BeautifulSoup(r.text,'html.parser')
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
            try:
                nums=[]
                for c in cells[si+1:]:
                    try:nums.append(float(c.replace(',','')))
                    except:nums.append(0.0)
                if len(nums)<5:continue
                ltp=nums[0]
                op=nums[1] if len(nums)>1 and nums[1]>0 else ltp
                hi=nums[2] if len(nums)>2 and nums[2]>0 else ltp
                lo=nums[3] if len(nums)>3 and nums[3]>0 else ltp
                vol=0
                for n in nums[6:]:
                    if 100<=n<=999999999 and n>vol:vol=n
                if ltp>0:
                    stocks[sym]={
                        'Date':today,'Open':round(op,2),
                        'High':round(hi,2),'Low':round(lo,2),
                        'Close':round(ltp,2),'Volume':int(vol)
                    }
            except:continue
        print(f"Fetched {len(stocks)} stocks for {today}")
        return stocks
    except Exception as e:
        print(f"Fetch error: {e}")
        return{}

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
    # DSE kokhono Fri/Sat trade hoy na - ei script age eta check korto na,
    # tai shuk/shoni o "ajker" data hishebe stale/bhul row likhe dicchilo,
    # jeta RSI/MACD calculation nosto korar main karon chilo.
    today_wd=datetime.now().weekday()  # 0=Mon..4=Fri,5=Sat,6=Sun
    if today_wd in(4,5):
        print("Aj Fri/Sat - DSE bondho, update skip kora holo")
        return

    # Eid/Puja/sorkari chhutir din-o (Sun-Thu hoyeo) DSE bondho thakte
    # pare - eta dhorar jonno real check kori.
    today=datetime.now().strftime('%Y-%m-%d')
    trading=is_dse_trading_day(today)
    if trading is False:
        print(f"Aj ({today}) DSE chhuti (holiday) - update skip kora holo")
        return

    stocks=fetch_today()
    if not stocks:
        print("Kono data pawa jaini - DSE bondho thakte pare")
        return

    if trading is None:
        # Archive page check fail korle (404/SSL/timeout - 2026-09-22 SSL
        # ar 2026-09-24 HTTP 404 dutoi hoyeche) puro update skip na kore,
        # live data nijei stale holiday-copy kina check kori: chhutir dine
        # live page ager diner hubohu OHLCV dekhay (Aug-5, Eid, Aug-26 -
        # prai 100% row identical chilo). Shotti trading dine volume/dam
        # beshirvag stock-e bodlay.
        same=0;compared=0
        for sym,row in stocks.items():
            path=f"{DATA_DIR}/{sym}.csv"
            if not os.path.exists(path):continue
            with open(path) as f:
                rows=list(csv.DictReader(f))
            if not rows:continue
            last=rows[-1]
            if last['Date'].strip()==today:continue
            try:
                identical=(abs(float(last['Close'])-row['Close'])<1e-9 and
                           abs(float(last['High'])-row['High'])<1e-9 and
                           abs(float(last['Low'])-row['Low'])<1e-9 and
                           int(float(last['Volume']))==int(row['Volume']))
            except:continue
            compared+=1
            if identical:same+=1
        ratio=same/compared if compared else 1.0
        print(f"Archive check uncertain - fallback: {same}/{compared} stock ager diner hubohu copy ({ratio*100:.0f}%)")
        if compared<50 or ratio>=0.5:
            print(f"Aj ({today}) stale/holiday data mone hocche ba jothesto tulona nei - update skip")
            return
        print(f"Aj ({today}) live data notun - trading day hishebe update korchi")

    updated=0;skipped=0;new_stocks=0
    for sym,row in stocks.items():
        if update_csv(sym,row):
            updated+=1
        else:
            skipped+=1

    print(f"Done! Updated:{updated} Skipped(already exists):{skipped}")

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
