from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "ebay_config.yaml"))
STATE_PATH = Path(os.getenv("EBAY_STATE_PATH", "ebay_state.json"))
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_SECONDS", "25"))
FORCE_REPORT = os.getenv("FORCE_REPORT", "false").lower() == "true"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

NOISE_WORDS = {
    "pokemon", "pokémon", "tcg", "card", "cards", "english", "eng", "mint", "near", "nm",
    "new", "authentic", "official", "rare", "holo", "foil", "fast", "shipping", "free",
    "read", "description", "look", "wow", "hot", "🔥", "⭐", "!!", "!!!", "the", "a", "an",
}

BAD_TITLE_TERMS = (
    "digital", "proxy", "custom", "fan made", "fanmade", "replica", "reprint", "orica",
    "mystery", "random card", "you choose", "pick your card", "code card", "online code",
    "empty box", "empty tin", "empty pack", "wrapper only", "art only", "photo only",
)


@dataclass
class Listing:
    title: str
    url: str
    price: float
    shipping: float
    image: str = ""
    condition: str = ""

    @property
    def total(self) -> float:
        return self.price + self.shipping


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def fetch(session: requests.Session, url: str) -> requests.Response:
    response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    return response


def money(text: str) -> float | None:
    match = re.search(r"\$\s*([0-9][0-9,]*(?:\.\d{2})?)", text.replace("US", ""), re.I)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def shipping_cost(text: str) -> float:
    low = text.lower()
    if "free shipping" in low or "free delivery" in low:
        return 0.0
    value = money(text)
    return value or 0.0


def clean_item_url(url: str) -> str:
    match = re.search(r"https?://www\.ebay\.com/itm/(?:[^/?]+/)?(\d+)", url)
    if match:
        return f"https://www.ebay.com/itm/{match.group(1)}"
    return url.split("?")[0]


def parse_search_results(html: str, limit: int) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    found: list[Listing] = []
    seen: set[str] = set()

    for item in soup.select("li.s-item"):
        title_el = item.select_one(".s-item__title")
        link_el = item.select_one("a.s-item__link[href]")
        price_el = item.select_one(".s-item__price")
        if not title_el or not link_el or not price_el:
            continue
        title = title_el.get_text(" ", strip=True)
        if not title or title.lower() == "shop on ebay":
            continue
        price = money(price_el.get_text(" ", strip=True))
        if price is None:
            continue
        url = clean_item_url(str(link_el.get("href", "")))
        if not url or url in seen:
            continue
        ship_el = item.select_one(".s-item__shipping, .s-item__logisticsCost")
        shipping = shipping_cost(ship_el.get_text(" ", strip=True)) if ship_el else 0.0
        cond_el = item.select_one(".SECONDARY_INFO, .s-item__subtitle")
        img_el = item.select_one("img[src]")
        image = str(img_el.get("src", "")) if img_el else ""
        seen.add(url)
        found.append(Listing(
            title=title,
            url=url,
            price=price,
            shipping=shipping,
            image=image,
            condition=cond_el.get_text(" ", strip=True) if cond_el else "",
        ))
        if len(found) >= limit:
            break
    return found


def ebay_search_url(query: str, sold: bool = False, buy_it_now: bool = False) -> str:
    params = [f"_nkw={quote_plus(query)}", "_sop=10", "rt=nc"]
    if sold:
        params.extend(["LH_Sold=1", "LH_Complete=1"])
    if buy_it_now:
        params.append("LH_BIN=1")
    return "https://www.ebay.com/sch/i.html?" + "&".join(params)


def plausible_pokemon_title(title: str) -> bool:
    low = title.lower()
    if "pokemon" not in low and "pokémon" not in low:
        return False
    return not any(term in low for term in BAD_TITLE_TERMS)


def normalize_tokens(title: str) -> list[str]:
    text = title.lower()
    text = re.sub(r"[^a-z0-9/#.+-]+", " ", text)
    raw = [t.strip("-+.") for t in text.split() if t.strip("-+.")]
    useful: list[str] = []
    for token in raw:
        if token in NOISE_WORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        if token not in useful:
            useful.append(token)
    return useful


def comp_query(title: str, max_tokens: int = 9) -> str:
    tokens = normalize_tokens(title)
    # Keep collector numbers, grading terms, years, set/card names, and the first useful title words.
    selected = tokens[:max_tokens]
    return "pokemon " + " ".join(selected)


def similarity_score(a: str, b: str) -> float:
    ta = set(normalize_tokens(a))
    tb = set(normalize_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def sold_comps(session: requests.Session, listing: Listing, cfg: dict[str, Any]) -> list[Listing]:
    query = comp_query(listing.title, int(cfg.get("comp_query_tokens", 9)))
    response = fetch(session, ebay_search_url(query, sold=True))
    candidates = parse_search_results(response.text, int(cfg.get("sold_results_to_parse", 30)))
    min_similarity = float(cfg.get("minimum_title_similarity", 0.40))
    comps = [x for x in candidates if plausible_pokemon_title(x.title) and similarity_score(listing.title, x.title) >= min_similarity]
    return comps


def robust_median(values: list[float]) -> float | None:
    values = sorted(v for v in values if v > 0)
    if not values:
        return None
    if len(values) >= 5:
        trim = max(1, int(len(values) * 0.10))
        if len(values) > trim * 2:
            values = values[trim:-trim]
    return float(statistics.median(values))


def listing_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def post_discord(payload: dict[str, Any]) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL missing; skipping alert", file=sys.stderr)
        return
    response = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def send_deal(listing: Listing, median: float, comps: int, discount: float, query: str) -> None:
    savings = median - listing.total
    fields = [
        {"name": "Price", "value": f"${listing.price:.2f}", "inline": True},
        {"name": "Shipping", "value": f"${listing.shipping:.2f}", "inline": True},
        {"name": "Total", "value": f"${listing.total:.2f}", "inline": True},
        {"name": "Recent sold median", "value": f"${median:.2f}", "inline": True},
        {"name": "Below sold median", "value": f"{discount:.0%}", "inline": True},
        {"name": "Potential savings", "value": f"${savings:.2f}", "inline": True},
        {"name": "Comparable sold listings", "value": str(comps), "inline": True},
        {"name": "Comp search", "value": query[:1024], "inline": False},
    ]
    embed: dict[str, Any] = {
        "title": f"🔥 REALLY GOOD EBAY DEAL: {listing.title}"[:256],
        "url": listing.url,
        "description": "Active eBay listing priced far below the median of similar recently sold listings. Verify condition/photos before buying.",
        "color": 5763719,
        "fields": fields,
    }
    if listing.image.startswith("http"):
        embed["thumbnail"] = {"url": listing.image}
    post_discord({"username": "Pokémon eBay Deal Hunter", "embeds": [embed]})


def send_summary(scanned: int, comp_checked: int, deals: int, errors: list[str]) -> None:
    if not FORCE_REPORT:
        return
    post_discord({
        "username": "Pokémon eBay Deal Hunter",
        "embeds": [{
            "title": "eBay deal scan completed",
            "color": 3447003 if not errors else 16753920,
            "fields": [
                {"name": "Active listings scanned", "value": str(scanned), "inline": True},
                {"name": "Listings comped", "value": str(comp_checked), "inline": True},
                {"name": "Strong deals", "value": str(deals), "inline": True},
                {"name": "Errors", "value": str(len(errors)), "inline": True},
                {"name": "Details", "value": ("\n".join(errors[:5]) or "None")[:1024], "inline": False},
            ],
        }],
    })


def main() -> int:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    cfg = config.get("ebay_deals", {})
    if not cfg.get("enabled", True):
        print("eBay deal finder disabled")
        return 0

    queries = [str(q) for q in cfg.get("search_queries", ["pokemon card"])]
    max_per_query = int(cfg.get("active_results_per_query", 25))
    max_comp_checks = int(cfg.get("max_comp_checks_per_run", 20))
    min_comps = int(cfg.get("minimum_sold_comps", 3))
    min_median = float(cfg.get("minimum_sold_median", 20.0))
    min_discount = float(cfg.get("minimum_discount_fraction", 0.40))
    min_savings = float(cfg.get("minimum_absolute_savings", 15.0))
    max_total = float(cfg.get("maximum_listing_total", 500.0))
    delay = float(cfg.get("request_delay_seconds", 1.0))

    previous: dict[str, Any] = load_json(STATE_PATH, {})
    current = dict(previous)
    errors: list[str] = []
    session = requests.Session()
    session.headers.update(HEADERS)

    active: list[Listing] = []
    seen_urls: set[str] = set()
    for query in queries:
        try:
            response = fetch(session, ebay_search_url(query, sold=False, buy_it_now=True))
            for listing in parse_search_results(response.text, max_per_query):
                if listing.url in seen_urls or not plausible_pokemon_title(listing.title):
                    continue
                if listing.total <= 0 or listing.total > max_total:
                    continue
                seen_urls.add(listing.url)
                active.append(listing)
        except Exception as exc:
            errors.append(f"active search '{query}': {exc}")
        time.sleep(delay)

    # Cheapest newly listed items first; this spends comp lookups where bargains are most likely.
    active.sort(key=lambda x: x.total)
    comp_checked = deals = 0

    for listing in active:
        if comp_checked >= max_comp_checks:
            break
        key = listing_key(listing.url)
        try:
            comps = sold_comps(session, listing, cfg)
            comp_checked += 1
            totals = [x.total for x in comps]
            median = robust_median(totals)
            if median is None or len(totals) < min_comps or median < min_median:
                continue
            discount = 1.0 - (listing.total / median)
            savings = median - listing.total
            qualifies = discount >= min_discount and savings >= min_savings
            signature = {
                "total": round(listing.total, 2),
                "median": round(median, 2),
                "comps": len(totals),
                "discount": round(discount, 4),
            }
            if qualifies:
                deals += 1
                if previous.get(key) != signature:
                    send_deal(listing, median, len(totals), discount, comp_query(listing.title, int(cfg.get("comp_query_tokens", 9))))
                    print(f"DEAL {discount:.0%} below median | ${listing.total:.2f} vs ${median:.2f} | {listing.title}")
            current[key] = signature
        except Exception as exc:
            errors.append(f"comp '{listing.title[:50]}': {exc}")
        time.sleep(delay)

    save_json(STATE_PATH, current)
    send_summary(len(active), comp_checked, deals, errors)
    print(f"eBay scan: active={len(active)} comped={comp_checked} deals={deals} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
