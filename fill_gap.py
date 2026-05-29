"""
DSE Gap Filler v2
Uses dsebd.org/day_end_archive.php to fill missing dates
Runs in GitHub Actions (different IP than Railway)
"""
import os,csv,requests,time
from bs4 import BeautifulSoup
from datetime import datetime,timedelta

HEADERS={
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":"en-US,en;q=0.5",
}
DATA_DIR="data"

def get_trading_dates(from_date, to_date):
    """Sun-Thu dates generate koro (DSE trading days)"""
    dates=[]
    current=datetime.strptime(from_date,"%Y-%m-%d")
    end=datetime.strptime(to_date,"%Y-%m-%d")
    while current<=end:
        # 0=Mon,1=Tue,2=Wed,3=Thu,6=Sun in Python weekday
        # DSE: Sun=6, Mon=0, Tue=1, Wed=2, Thu=3
        if current.weekday() in(0,1,2,3,6):
            dates.append(current.strftime("%Y-%m-%d"))
        current+=timedelta(days=1)
    return dates

def fetch_all_stocks_for_date(date):
    """Ek diner sob stock er data ane"""
    url=f"https://www.dsebd.org/day_end_archive.php?endDate={date}&archive=data"
    try:
        r=requests.get(url,headers=HEADERS,timeout=20)
        if r.status_code!=200:
            print(f"  {date}: HTTP {r.status_code}")
            return{}
        soup=BeautifulSoup(r.text,"html.parser")
        tables=soup.find_all("table")
        if not tables:
            print(f"  {date}: No tables found")
            return{}

        stocks={}
        for table in tables:
            rows=table.find_all("tr")
            for row in rows[1:]:
                cols=row.find_all("td")
                if len(cols)<7:continue
                try:
                    sym=cols[1].get_text(strip=True).upper()
                    if not sym or len(sym)<2:continue
                    hi  =float(cols[2].get_text(strip=True).replace(",","") or 0)
                    lo  =float(cols[3].get_text(strip=True).replace(",","") or 0)
                    cl  =float(cols[4].get_text(strip=True).replace(",","") or 0)
                    yday=float(cols[5].get_text(strip=True).replace(",","") or 0)
                    vol =float(cols[6].get_text(strip=True).replace(",","") or 0)
                    if cl>0:
                        op=yday if yday>0 else cl
                        stocks[sym]={
                            "Date":date,"Open":round(op,2),
                            "High":round(hi,2) if hi>0 else round(cl,2),
                            "Low":round(lo,2) if lo>0 else round(cl,2),
                            "Close":round(cl,2),"Volume":int(vol)
                        }
                except:continue
        return stocks
    except Exception as e:
        print(f"  {date}: Error - {e}")
        return{}

def get_existing_dates(symbol):
    path=f"{DATA_DIR}/{symbol}.csv"
    if not os.path.exists(path):return set()
    with open(path,"r") as f:
        rows=list(csv.DictReader(f))
    return{row["Date"] for row in rows}

def append_row(symbol,row):
    path=f"{DATA_DIR}/{symbol}.csv"
    if not os.path.exists(path):return
    with open(path,"a",newline="") as f:
        w=csv.writer(f)
        w.writerow([row["Date"],row["Open"],row["High"],row["Low"],row["Close"],row["Volume"]])

def fill_gaps():
    today=datetime.now().strftime("%Y-%m-%d")
    
    # Find the gap: Jan 22 2026 to today
    gap_start="2026-01-23"
    
    # Get trading dates in the gap
    dates=get_trading_dates(gap_start,today)
    print(f"Trading dates to fill: {len(dates)}")
    print(f"From {dates[0]} to {dates[-1]}")
    print()
    
    # Track which dates already have data
    # Check a sample stock
    sample_stock="BRACBANK"
    existing=get_existing_dates(sample_stock)
    dates_to_fill=[d for d in dates if d not in existing]
    
    print(f"Dates already filled: {len(dates)-len(dates_to_fill)}")
    print(f"Dates to fill: {len(dates_to_fill)}")
    print()
    
    if not dates_to_fill:
        print("All dates already filled!")
        return
    
    filled_dates=0
    total_rows=0
    
    for i,date in enumerate(dates_to_fill):
        print(f"[{i+1}/{len(dates_to_fill)}] Fetching {date}...")
        stocks=fetch_all_stocks_for_date(date)
        
        if not stocks:
            print(f"  No data for {date} (market may have been closed)")
            time.sleep(1)
            continue
        
        # Add data to each stock file
        added=0
        for sym,row in stocks.items():
            existing_sym=get_existing_dates(sym)
            if date not in existing_sym:
                append_row(sym,row)
                added+=1
        
        total_rows+=added
        filled_dates+=1
        print(f"  Added {added} stock rows for {date}")
        time.sleep(2)  # Respectful delay
    
    print(f"\n=== DONE ===")
    print(f"Filled {filled_dates} dates, {total_rows} total rows added")

if __name__=="__main__":
    fill_gaps()
