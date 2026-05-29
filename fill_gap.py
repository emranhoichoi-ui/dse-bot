"""
DSE Historical Data Gap Filler
Jan 22, 2026 theke ekhon porjonto missing data fill kore
stocksurferbd library use kore DSE theke data nabe
"""
import os,csv,requests,time
from datetime import datetime,timedelta
from bs4 import BeautifulSoup

HEADERS={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
DATA_DIR='data'
GAP_START='2026-01-23'  # day after our data ends

def get_stock_list():
    """data/ folder theke sob stock symbol nao"""
    stocks=[]
    if not os.path.exists(DATA_DIR):return stocks
    for f in os.listdir(DATA_DIR):
        if f.endswith('.csv') and f!='README.md':
            stocks.append(f.replace('.csv',''))
    return sorted(stocks)

def get_last_date(symbol):
    """Stock er CSV file er last date bero koro"""
    path=f"{DATA_DIR}/{symbol}.csv"
    if not os.path.exists(path):return None
    with open(path,'r') as f:
        rows=list(csv.DictReader(f))
    if not rows:return None
    return rows[-1]['Date']

def fetch_dse_history(symbol,from_date,to_date):
    """
    dsebd.org theke historical data fetch kore
    URL: https://www.dsebd.org/dseX_share.php
    """
    results=[]
    try:
        # Try dsebd.org company page
        url=f"https://www.dsebd.org/displayCompany.php?name={symbol}"
        r=requests.get(url,headers=HEADERS,timeout=20)
        soup=BeautifulSoup(r.text,'html.parser')

        # Find price history table
        tables=soup.find_all('table')
        for table in tables:
            rows=table.find_all('tr')
            for row in rows[1:]:
                cols=row.find_all('td')
                if len(cols)<6:continue
                try:
                    date_str=cols[0].get_text(strip=True)
                    # Parse various date formats
                    for fmt in['%d %b %Y','%Y-%m-%d','%d/%m/%Y','%B %d, %Y']:
                        try:
                            dt=datetime.strptime(date_str,fmt)
                            date_iso=dt.strftime('%Y-%m-%d')
                            break
                        except:continue
                    else:continue

                    if date_iso<from_date or date_iso>to_date:continue

                    op=float(cols[1].get_text(strip=True).replace(',','') or 0)
                    hi=float(cols[2].get_text(strip=True).replace(',','') or 0)
                    lo=float(cols[3].get_text(strip=True).replace(',','') or 0)
                    cl=float(cols[4].get_text(strip=True).replace(',','') or 0)
                    vol=float(cols[5].get_text(strip=True).replace(',','') or 0)

                    if cl>0:
                        results.append({
                            'Date':date_iso,'Open':op,'High':hi,
                            'Low':lo,'Close':cl,'Volume':int(vol)
                        })
                except:continue

        if results:
            results.sort(key=lambda x:x['Date'])
            print(f"  dsebd.org: {len(results)} rows fetched")
            return results

    except Exception as e:
        print(f"  dsebd error: {e}")

    return[]

def fetch_amarstock(symbol,from_date,to_date):
    """AmarStock theke data fetch"""
    results=[]
    try:
        url=f"https://www.amarstock.com/api/history/{symbol}"
        params={'from':from_date,'to':to_date,'interval':'day'}
        r=requests.get(url,headers=HEADERS,params=params,timeout=20)
        if r.status_code==200:
            data=r.json()
            for item in data:
                results.append({
                    'Date':item.get('date',''),
                    'Open':item.get('open',0),
                    'High':item.get('high',0),
                    'Low':item.get('low',0),
                    'Close':item.get('close',0),
                    'Volume':item.get('volume',0),
                })
    except Exception as e:
        print(f"  amarstock error: {e}")
    return results

def fetch_stocksurferbd(symbol,from_date,to_date):
    """stocksurferbd library use kore data nao"""
    results=[]
    try:
        from stocksurferbd import PriceData
        loader=PriceData()
        df=loader.get_history_data(symbol=symbol,market='DSE')
        if df is not None and len(df)>0:
            for _,row in df.iterrows():
                date_str=str(row.name)[:10] if hasattr(row,'name') else str(row.get('Date',''))
                if date_str<from_date or date_str>to_date:continue
                results.append({
                    'Date':date_str,
                    'Open':float(row.get('Open',row.get('open',0))),
                    'High':float(row.get('High',row.get('high',0))),
                    'Low':float(row.get('Low',row.get('low',0))),
                    'Close':float(row.get('Close',row.get('close',0))),
                    'Volume':int(row.get('Volume',row.get('volume',0))),
                })
            results.sort(key=lambda x:x['Date'])
            if results:print(f"  stocksurferbd: {len(results)} rows")
    except Exception as e:
        print(f"  stocksurferbd error: {e}")
    return results

def append_to_csv(symbol,new_rows):
    """CSV file e nতুন rows add koro (duplicate skip)"""
    path=f"{DATA_DIR}/{symbol}.csv"
    if not os.path.exists(path):return 0

    # Existing dates
    with open(path,'r') as f:
        existing=list(csv.DictReader(f))
    existing_dates={row['Date'] for row in existing}

    # Filter new rows
    to_add=[r for r in new_rows if r['Date'] not in existing_dates]
    if not to_add:return 0

    # Append
    with open(path,'a',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['Date','Open','High','Low','Close','Volume'])
        for row in sorted(to_add,key=lambda x:x['Date']):
            writer.writerow(row)

    return len(to_add)

def fill_gaps():
    today=datetime.now().strftime('%Y-%m-%d')
    stocks=get_stock_list()
    print(f"Total stocks to process: {len(stocks)}")
    print(f"Looking for gaps from {GAP_START} to {today}")
    print()

    filled=0;skipped=0;errors=0

    for i,sym in enumerate(stocks):
        last=get_last_date(sym)
        if not last:skipped+=1;continue

        # Check if gap exists
        last_dt=datetime.strptime(last,'%Y-%m-%d')
        gap_start_dt=datetime.strptime(GAP_START,'%Y-%m-%d')
        today_dt=datetime.now()

        # If last date is recent (within 3 days), skip
        if (today_dt-last_dt).days<=3:
            skipped+=1
            if i%50==0:print(f"[{i+1}/{len(stocks)}] {sym}: up to date ({last})")
            continue

        from_d=max(last,'2026-01-23')
        # Add 1 day to avoid duplicate
        from_dt=datetime.strptime(from_d,'%Y-%m-%d')+timedelta(days=1)
        from_d=from_dt.strftime('%Y-%m-%d')

        print(f"[{i+1}/{len(stocks)}] {sym}: gap from {from_d} to {today}")

        # Try multiple sources
        new_rows=[]

        # Source 1: stocksurferbd
        if not new_rows:
            new_rows=fetch_stocksurferbd(sym,from_d,today)
            time.sleep(0.5)

        # Source 2: dsebd.org
        if not new_rows:
            new_rows=fetch_dse_history(sym,from_d,today)
            time.sleep(1)

        if new_rows:
            added=append_to_csv(sym,new_rows)
            print(f"  Added {added} rows to {sym}.csv")
            filled+=1
        else:
            print(f"  No data found for {sym}")
            errors+=1

    print(f"\n=== DONE ===")
    print(f"Filled: {filled} | Skipped (up-to-date): {skipped} | No data: {errors}")

if __name__=='__main__':
    fill_gaps()
