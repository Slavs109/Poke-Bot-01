from __future__ import annotations

import hashlib, json, os, re, sys, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from typing import Any

import requests, yaml
from bs4 import BeautifulSoup

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "estate_config.yaml"))
STATE_PATH = Path(os.getenv("MARKET_STATE_PATH", "market_state.json"))
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_SECONDS", "25"))
FORCE_REPORT = os.getenv("FORCE_REPORT", "false").lower() == "true"
HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36","Accept-Language":"en-US,en;q=0.9"}


def load_json(path: Path, default: Any) -> Any:
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError): return default

def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")

def literal(text: str, term: str) -> bool:
    term = term.strip().lower()
    return bool(term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text.lower()))

def hits(text: str, keywords: list[str]) -> list[str]:
    return [k for k in keywords if literal(text, k)]

def fetch(session: requests.Session, url: str) -> requests.Response:
    r = session.get(url, timeout=TIMEOUT, allow_redirects=True); r.raise_for_status(); return r

def title_of(soup: BeautifulSoup) -> str:
    h1=soup.find("h1")
    if h1 and h1.get_text(" ",strip=True): return h1.get_text(" ",strip=True)[:180]
    return (soup.title.get_text(" ",strip=True) if soup.title else "Trading card listing")[:180]

def price_of(text: str) -> str:
    m=re.search(r"\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text)
    return "$"+m.group(1) if m else "Price not detected"

def location_of(text: str) -> str:
    pats=[r"(?:Posted|Last updated).*?\bin\s+([^\n|]{2,80},\s*TX)",r"\b([A-Za-z .'-]+,\s*TX\s*\d{0,5})\b",r"\b([A-Z][A-Za-z .'-]+,\s*TX)\b"]
    for p in pats:
        m=re.search(p,text,re.I)
        if m: return re.sub(r"\s+"," ",m.group(1)).strip()[:120]
    return "See listing"

def image_of(soup: BeautifulSoup, base: str) -> str:
    og=soup.select_one('meta[property="og:image"][content]')
    if og: return urljoin(base,str(og.get("content")))
    for img in soup.select("img[src],img[data-src]"):
        src=img.get("src") or img.get("data-src")
        if isinstance(src,str) and src and not src.startswith("data:"):
            low=src.lower()
            if not any(x in low for x in ("logo","icon","avatar","qr","sprite")): return urljoin(base,src)
    return ""

def item_urls(soup: BeautifulSoup, base: str, pattern: str, limit: int) -> list[str]:
    rx=re.compile(pattern,re.I); out=[]; seen=set()
    for a in soup.select("a[href]"):
        u=urljoin(base,str(a.get("href","")))
        if rx.search(u) and u not in seen:
            seen.add(u); out.append(u)
            if len(out)>=limit: break
    return out

def skip_listing(text: str, exclude_phrases: list[str]) -> bool:
    low=text.lower()
    return any(p.lower() in low for p in exclude_phrases)

def state_key(url: str) -> str: return hashlib.sha256(url.encode()).hexdigest()

def post(payload: dict[str,Any]) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL missing",file=sys.stderr); return
    r=requests.post(WEBHOOK_URL,json=payload,timeout=TIMEOUT); r.raise_for_status()

def alert(source: str, title: str, url: str, text_hits: list[str], price: str, location: str, image: str) -> None:
    embed={"title":f"CARD LISTING: {title}"[:256],"url":url,"description":"New public listing matched one of your exact trading-card keywords.","color":10181046,"fields":[{"name":"Source","value":source,"inline":True},{"name":"Price","value":price,"inline":True},{"name":"Location","value":location,"inline":True},{"name":"Exact keyword(s)","value":", ".join(text_hits)[:1024],"inline":False}]}
    if image: embed["thumbnail"]={"url":image}
    post({"username":"Cheap Card Finds","embeds":[embed]})

def main() -> int:
    cfg=yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    estate=cfg.get("estate_sales",{})
    keywords=[str(x).lower() for x in estate.get("keywords",[])]
    market=cfg.get("marketplace_sources",{}) or {}
    if not market.get("enabled",True): return 0
    previous=load_json(STATE_PATH,{})
    current=dict(previous); errors=[]; checked=matches=searches=0
    session=requests.Session(); session.headers.update(HEADERS)
    for src in market.get("sources",[]):
        if not src.get("enabled",True): continue
        name=str(src.get("name","Marketplace")); pattern=str(src.get("item_url_pattern",r"$^")); limit=int(src.get("max_items",40)); delay=float(src.get("delay_seconds",0.4)); excludes=[str(x) for x in src.get("exclude_phrases",[])]
        for search_url in src.get("search_urls",[]):
            try:
                r=fetch(session,str(search_url)); searches+=1
                urls=item_urls(BeautifulSoup(r.text,"html.parser"),r.url,pattern,limit)
                for u in urls:
                    try:
                        rr=fetch(session,u); checked+=1
                        soup=BeautifulSoup(rr.text,"html.parser"); text=re.sub(r"\s+"," ",soup.get_text(" ",strip=True))
                        found=hits(text,keywords)
                        if not found or skip_listing(text,excludes): continue
                        key=state_key(rr.url); sig={"title":title_of(soup),"keywords":found,"price":price_of(text)}
                        if previous.get(key)!=sig:
                            alert(name,sig["title"],rr.url,found,sig["price"],location_of(text),image_of(soup,rr.url)); matches+=1
                            print(f"MATCH {name} | {sig['title']} | {rr.url}")
                        current[key]=sig
                    except Exception as exc: errors.append(f"{name} item: {exc}")
                    time.sleep(delay)
            except Exception as exc: errors.append(f"{name} search: {exc}")
    save_json(STATE_PATH,current)
    if FORCE_REPORT:
        post({"username":"Cheap Card Finds","embeds":[{"title":"Marketplace scan completed","fields":[{"name":"Search pages","value":str(searches),"inline":True},{"name":"Listings checked","value":str(checked),"inline":True},{"name":"New matches","value":str(matches),"inline":True},{"name":"Errors","value":str(len(errors)),"inline":True},{"name":"Details","value":("\n".join(errors[:5]) or "None")[:1024],"inline":False}]}]})
    print(f"Marketplace scan: searches={searches} checked={checked} matches={matches} errors={len(errors)}")
    return 0

if __name__=="__main__": raise SystemExit(main())
