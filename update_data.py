"""
DSE Daily Data Updater
Protidin DSE close er por today's data add kore
"""
import requests,csv,os,time
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup
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
        r=requests.get(url,headers=HEADERS,timeout=15,verify=False)
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
    url="https://www.dsebd.org/latest_share_price_scroll_by_value.php"
    stocks={}
    today=datetime.now().strftime('%Y-%m-%d')
    try:
        r=requests.get(url,headers=HEADERS,timeout=30,verify=False)
        r.raise_for_status()
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
    if trading is not True:
        reason="DSE chhuti (holiday)" if trading is False else "check failed/uncertain - shafe thakar jonno skip"
        print(f"Aj ({today}) {reason} - update skip kora holo")
        return

    stocks=fetch_today()
    if not stocks:
        print("Kono data pawa jaini - DSE bondho thakte pare")
        return

    updated=0;skipped=0;new_stocks=0
    for sym,row in stocks.items():
        if update_csv(sym,row):
            updated+=1
        else:
            skipped+=1

    print(f"Done! Updated:{updated} Skipped(already exists):{skipped}")

if __name__=='__main__':
    main()
