"""
DSE Daily Data Updater
Protidin DSE close er por today's data add kore
"""
import requests,csv,os,time
from bs4 import BeautifulSoup
from datetime import datetime

HEADERS={'User-Agent':'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120'}
DATA_DIR='data'

def fetch_today():
    """dsebd.org theke ajer sob stock er data ano"""
    url="https://www.dsebd.org/latest_share_price_scroll_by_value.php"
    stocks={}
    today=datetime.now().strftime('%Y-%m-%d')
    try:
        r=requests.get(url,headers=HEADERS,timeout=30)
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

    with open(path,'a',newline='') as f:
        w=csv.writer(f)
        w.writerow([row['Date'],row['Open'],row['High'],row['Low'],row['Close'],row['Volume']])
    return True

def main():
    print("DSE data update shuru...")
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
