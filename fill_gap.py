"""
DSE Gap Filler v2
Uses dsebd.org/day_end_archive.php to fill missing dates
Runs in GitHub Actions (different IP than Railway)
"""
import os,csv,requests,time
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup
from datetime import datetime,timedelta

HEADERS={
    "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":"en-US,en;q=0.5",
}
DATA_DIR="data"
DEBUG_LOG=[]

def dbg(msg):
    print(msg)
    DEBUG_LOG.append(str(msg))

def write_debug_file():
    with open(f"{DATA_DIR}/_debug_fillgap.txt","w") as f:
        f.write("\n".join(DEBUG_LOG))

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

_DEBUG_CAPTURED=[False]

def fetch_all_stocks_for_date(date):
    """Ek diner sob stock er data ane - header theke column position ber kore,
    dsebd.org er table layout change hole o kaj kore"""
    url=f"https://www.dsebd.org/day_end_archive.php?endDate={date}&archive=data"
    capture=not _DEBUG_CAPTURED[0]
    try:
        r=requests.get(url,headers=HEADERS,timeout=20,verify=False)
        if capture:
            dbg(f"=== DEBUG for {date} ===")
            dbg(f"URL: {url}")
            dbg(f"HTTP status: {r.status_code}")
            dbg(f"Response length: {len(r.text)} chars")
            dbg(f"First 500 chars of response:\n{r.text[:500]}")
        if r.status_code!=200:
            dbg(f"  {date}: HTTP {r.status_code}")
            _DEBUG_CAPTURED[0]=capture or _DEBUG_CAPTURED[0]
            return{}
        soup=BeautifulSoup(r.text,"html.parser")
        tables=soup.find_all("table")
        if capture:
            dbg(f"Tables found: {len(tables)}")
            for ti,t in enumerate(tables[:3]):
                trs=t.find_all("tr")
                dbg(f"  table[{ti}]: {len(trs)} rows")
                if trs:
                    first_row_cells=trs[0].find_all(["th","td"])
                    dbg(f"    header row cells: {[c.get_text(strip=True) for c in first_row_cells]}")
                if len(trs)>1:
                    second_row_cells=trs[1].find_all(["th","td"])
                    dbg(f"    2nd row cells: {[c.get_text(strip=True) for c in second_row_cells]}")
        if not tables:
            dbg(f"  {date}: No tables found")
            _DEBUG_CAPTURED[0]=True
            return{}

        stocks={}
        col={}
        for table in tables:
            rows=table.find_all("tr")
            if len(rows)<2:continue

            # --- try to map columns from the header row ---
            header_cells=rows[0].find_all(["th","td"])
            header_txt=[h.get_text(strip=True).upper() for h in header_cells]
            col={}
            for i,name in enumerate(header_txt):
                if ('TRADING' in name and 'CODE' in name) or name in('SYMBOL','SCRIP'):col['sym']=i
                elif name in('HIGH','HIGH*'):col['high']=i
                elif name in('LOW','LOW*'):col['low']=i
                elif name.startswith('CLOSING') or name in('CLOSEP','CLOSEP*','CLOSE','LTP','LTP*'):col['close']=i
                elif name in('YCP','YCP*','OPENING PRICE*','OPEN'):col['ycp']=i
                elif name in('VOLUME','VOLUME*'):col['vol']=i

            use_header=all(k in col for k in('sym','high','low','close'))
            if capture:dbg(f"  column map detected: {col}  use_header={use_header}")

            for row in rows[1:]:
                cols=row.find_all("td")
                if len(cols)<7:continue
                try:
                    if use_header and max(col.values())<len(cols):
                        sym=cols[col['sym']].get_text(strip=True).upper()
                        hi=float(cols[col['high']].get_text(strip=True).replace(",","") or 0)
                        lo=float(cols[col['low']].get_text(strip=True).replace(",","") or 0)
                        cl=float(cols[col['close']].get_text(strip=True).replace(",","") or 0)
                        yday=float(cols[col['ycp']].get_text(strip=True).replace(",","") or 0) if 'ycp' in col else 0
                        vol=float(cols[col['vol']].get_text(strip=True).replace(",","") or 0) if 'vol' in col else 0
                    else:
                        # fallback: old hardcoded positions (kept for safety)
                        sym=cols[1].get_text(strip=True).upper()
                        hi  =float(cols[2].get_text(strip=True).replace(",","") or 0)
                        lo  =float(cols[3].get_text(strip=True).replace(",","") or 0)
                        cl  =float(cols[4].get_text(strip=True).replace(",","") or 0)
                        yday=float(cols[5].get_text(strip=True).replace(",","") or 0)
                        vol =float(cols[6].get_text(strip=True).replace(",","") or 0)
                    if not sym or len(sym)<2:continue
                    if cl>0:
                        op=yday if yday>0 else cl
                        stocks[sym]={
                            "Date":date,"Open":round(op,2),
                            "High":round(hi,2) if hi>0 else round(cl,2),
                            "Low":round(lo,2) if lo>0 else round(cl,2),
                            "Close":round(cl,2),"Volume":int(vol)
                        }
                except:continue
        dbg(f"  {date}: parsed {len(stocks)} stocks (header_map={col})")
        if capture:_DEBUG_CAPTURED[0]=True
        return stocks
    except Exception as e:
        dbg(f"  {date}: Error - {type(e).__name__}: {e}")
        if capture:_DEBUG_CAPTURED[0]=True
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
    # Purbe ei check chilo na - fole GitHub API die (bot.py auto_update_data)
    # kono row add korar por file trailing newline chara thakle, ei function
    # shei last line er shathe mishe giye CSV row corrupt kore felto
    # (jeta RSI/MACD calculation nosto korchilo). Ekhon check kore newline
    # add kore nei jodi dorkar hoy.
    needs_leading_nl=False
    if os.path.getsize(path)>0:
        with open(path,"rb") as f:
            f.seek(-1,2)
            if f.read(1)!=b"\n":
                needs_leading_nl=True
    with open(path,"a",newline="") as f:
        if needs_leading_nl:
            f.write("\n")
        w=csv.writer(f)
        w.writerow([row["Date"],row["Open"],row["High"],row["Low"],row["Close"],row["Volume"]])

def fill_gaps():
    today=datetime.now().strftime("%Y-%m-%d")

    # Find the gap: Jan 22 2026 to today
    gap_start="2026-01-23"

    # Get trading dates in the gap
    dates=get_trading_dates(gap_start,today)
    dbg(f"Trading dates to fill: {len(dates)}")
    dbg(f"From {dates[0]} to {dates[-1]}")

    # Track which dates already have data
    # Check a sample stock
    sample_stock="BRACBANK"
    existing=get_existing_dates(sample_stock)
    dates_to_fill=[d for d in dates if d not in existing]

    dbg(f"Dates already filled: {len(dates)-len(dates_to_fill)}")
    dbg(f"Dates to fill: {len(dates_to_fill)}")

    if not dates_to_fill:
        dbg("All dates already filled!")
        write_debug_file()
        return

    filled_dates=0
    total_rows=0

    for i,date in enumerate(dates_to_fill):
        dbg(f"[{i+1}/{len(dates_to_fill)}] Fetching {date}...")
        stocks=fetch_all_stocks_for_date(date)

        if not stocks:
            dbg(f"  No data for {date} (market may have been closed, or fetch failed)")
            write_debug_file()  # write early so we see at least the first attempt even if later ones crash
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
        dbg(f"  Added {added} stock rows for {date}")
        time.sleep(2)  # Respectful delay

    dbg(f"\n=== DONE ===")
    dbg(f"Filled {filled_dates} dates, {total_rows} total rows added")
    write_debug_file()

if __name__=="__main__":
    try:
        fill_gaps()
    except Exception as e:
        dbg(f"FATAL: {type(e).__name__}: {e}")
        write_debug_file()
        raise
