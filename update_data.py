import requests,csv,os
from bs4 import BeautifulSoup
from datetime import datetime

HEADERS={'User-Agent':'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120'}

def fetch_today():
    """dsebd.org থেকে আজকের সব স্টকের data আনে"""
    url="https://www.dsebd.org/latest_share_price_scroll_by_value.php"
    r=requests.get(url,headers=HEADERS,timeout=30)
    r.raise_for_status()
    soup=BeautifulSoup(r.text,'html.parser')
    stocks={}
    today=datetime.now().strftime('%Y-%m-%d')
    for row in soup.find_all('tr'):
        cols=row.find_all('td')
        if len(cols)<9:continue
        cells=[c.get_text(strip=True) for c in cols]
        sym=None;si=0
        for i,cell in enumerate(cells[:4]):
            cl=cell.replace('-','').replace('_','')
            if cl.isalpha() and 2<=len(cell)<=12 and cell.upper() not in('SL','NO','SYMBOL','NAME','CODE'):
                sym=cell.upper();si=i;break
        if not sym:continue
        try:
            nums=[]
            for c in cells[si+1:]:
                try:nums.append(float(c.replace(',','')))
                except:nums.append(0.0)
            if len(nums)<6:continue
            ltp=nums[0];hi=nums[2] if len(nums)>2 else ltp
            lo=nums[3] if len(nums)>3 else ltp
            op=nums[1] if len(nums)>1 else ltp
            vol=0
            for n in nums[6:]:
                if 1000<=n<=999999999 and n>vol:vol=n
            if ltp<=0:continue
            stocks[sym]={
                'date':today,'open':round(op,2),
                'high':round(hi,2),'low':round(lo,2),
                'close':round(ltp,2),'volume':int(vol)
            }
        except:continue
    print(f"Fetched {len(stocks)} stocks for {today}")
    return stocks

def update_csv(sym,row):
    """stock এর CSV file এ নতুন row যোগ করে"""
    path=f"data/{sym}.csv"
    if not os.path.exists(path):
        # নতুন file তৈরি
        with open(path,'w',newline='') as f:
            w=csv.writer(f)
            w.writerow(['Date','Open','High','Low','Close','Volume'])
            w.writerow([row['date'],row['open'],row['high'],row['low'],row['close'],row['volume']])
        return True
    # আগের file এ নতুন row যোগ
    with open(path,'r') as f:
        lines=f.readlines()
    # আজকের date আগে থেকে আছে কিনা check
    for line in lines[-5:]:
        if row['date'] in line:
            print(f"{sym}: already updated for {row['date']}")
            return False
    with open(path,'a',newline='') as f:
        w=csv.writer(f)
        w.writerow([row['date'],row['open'],row['high'],row['low'],row['close'],row['volume']])
    return True

def main():
    print("DSE data update শুরু...")
    stocks=fetch_today()
    if not stocks:
        print("কোনো data পাওয়া যায়নি — DSE বন্ধ থাকতে পারে")
        return
    updated=0;skipped=0
    for sym,row in stocks.items():
        if update_csv(sym,row):updated+=1
        else:skipped+=1
    print(f"Done! Updated:{updated} Skipped:{skipped}")

if __name__=='__main__':
    main()
