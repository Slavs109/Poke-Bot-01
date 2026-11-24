"""Personal RSS reader; retain original source titles and links."""
import re
import json
import time
import html
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from clothing_deals import APPAREL, EXCLUDE

FEEDS = {
    "Slickdeals": "https://slickdeals.net/rss.php",
    "DealNews": "https://www.dealnews.com/rss/todays-edition",
}
MEN = re.compile(r"\b(men|mens|men's|men’s|male)\b", re.I)
PERCENT = re.compile(r"\b(\d{1,2})\s*%\s*(?:off|discount)", re.I)
def parse(data, source, minimum=50):
    root = ET.fromstring(data)
    if root.tag != "rss":
        raise ValueError("Expected RSS feed")
    for item in root.findall("./channel/item"):
        title = item.findtext("title", "")
        description = item.findtext("description", "")
        link = item.findtext("link", "")
        if not link.startswith("https://") or not MEN.search(title) or not APPAREL.search(title) or EXCLUDE.search(title):
            continue
        discounts = [int(x) for x in PERCENT.findall(title)]
        if not discounts or max(discounts) < minimum:
            continue
        yield {"source":source,"title":title,"description":description,"url":link,"published":item.findtext("pubDate",""),"advertised_max_discount":max(discounts),"verified":False,"availability":"unknown"}
def scan():
    deals, errors, sources = [], [], []
    for name, url in FEEDS.items():
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"DealHunter/0.1 personal RSS reader"})
            with urllib.request.urlopen(req, timeout=20) as response:
                deals.extend(parse(response.read(4_000_000), name))
            sources.append(name)
        except Exception as exc:
            errors.append({"source":name,"error":str(exc)})
        time.sleep(3)
    unique = {d["url"]:d for d in deals}
    return {"deals":sorted(unique.values(), key=lambda d:d["advertised_max_discount"], reverse=True),"successful_sources":sources,"errors":errors,"scanned_at":time.time()}
if __name__ == "__main__":
    result = scan()
    Path("deal_sites_results.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
    if not result["successful_sources"]:
        raise SystemExit("No live feeds available")
