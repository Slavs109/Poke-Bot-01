"""Discover advertised men's apparel discounts via Reddit public RSS.
Use only where permitted; no bypass of access restrictions."""
import json
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

SUBREDDITS = ("frugalmalefashion", "RunningShoeGeeks", "SneakerDeals", "deals")
ATOM = {"a": "http://www.w3.org/2005/Atom"}
APPAREL = re.compile(r"\b(men|mens|men's|shirt|shirts|jeans|pants|jacket|hoodie|sneakers|shoes|boots|shorts|polo|sweater|apparel|clothing)\b", re.I)
EXCLUDE = re.compile(r"\b(women|womens|women's|kids|children|girls|boys|expired|sold out)\b", re.I)
DISCOUNT = re.compile(r"\b(\d{2})\s*%\s*(?:off|discount)", re.I)
def scan(minimum=50):
    deals, errors = [], []
    for sub in SUBREDDITS:
        try:
            req = urllib.request.Request(f"https://www.reddit.com/r/{sub}/new/.rss", headers={"User-Agent": "DealHunter/0.1 personal apparel deal reader"})
            with urllib.request.urlopen(req, timeout=20) as response:
                root = ET.fromstring(response.read())
            for entry in root.findall("a:entry", ATOM):
                title = entry.findtext("a:title", "", ATOM)
                if EXCLUDE.search(title) or not APPAREL.search(title):
                    continue
                if sub != "frugalmalefashion" and not re.search(r"\b(men|mens|men's)\b", title, re.I):
                    continue
                matches = [int(n) for n in DISCOUNT.findall(title)]
                if not matches or max(matches) < minimum:
                    continue
                link = entry.find("a:link", ATOM)
                if link is None:
                    continue
                deals.append({"title":title,"url":link.get("href"),"discount":max(matches),"source":sub,"published":entry.findtext("a:published","",ATOM),"verified":False,"availability":"unknown"})
        except Exception as exc:
            errors.append({"source":sub,"error":str(exc)})
        time.sleep(2)
    unique = {d["url"]:d for d in deals if d["url"]}
    return {"scanned_at":time.time(),"deals":sorted(unique.values(),key=lambda d:d["discount"],reverse=True),"errors":errors}
if __name__ == "__main__":
    result = scan()
    Path("clothing_deals.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
