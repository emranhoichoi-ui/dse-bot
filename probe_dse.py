"""Temporary probe: dsebd.org redesign - check which endpoints serve price data."""
import requests,urllib3
urllib3.disable_warnings()
H={'User-Agent':'Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36'}
urls=["https://dsebd.org/api/live/prices","https://dsebd.org/api/live/companies/quotes","https://dsebd.org/api/live/market",
"https://old.dsebd.org/latest_share_price_scroll_by_value.php",
"https://old.dsebd.org/day_end_archive.php?startDate=2026-09-22&endDate=2026-09-22&archive=data",
"https://old.dsebd.org/day_end_archive.php?startDate=2026-09-24&endDate=2026-09-24&archive=data"]
with open("data/_debug_probe.txt","w") as f:
    for u in urls:
        try:
            r=requests.get(u,headers=H,timeout=30,verify=False)
            t=r.text
            f.write(f"\n\n##### {u}\nHTTP {r.status_code} ctype={r.headers.get('content-type')} len={len(t)} tr={t.count('<tr')} TRADINGCODE={'TRADING CODE' in t.upper()}\n")
            i=t.find('BRACBANK')
            f.write(t[:1500] if i<0 else t[max(0,i-600):i+900])
        except Exception as e:
            f.write(f"\n\n##### {u}\nERR {e}\n")
