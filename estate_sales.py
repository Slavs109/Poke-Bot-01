from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config.yaml"))
STATE_PATH = Path(os.getenv("ESTATE_STATE_PATH", "estate_state.json"))
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_SECONDS", "25"))
FORCE_REPORT = os.getenv("FORCE_REPORT", "false").lower() == "true"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

DEFAULT_KEYWORDS = [
    "pokemon",
    "pokémon",
    "pokemon cards",
    "trading cards",
    "tcg",
    "magic the gathering",
    "yu-gi-oh",
    "yugioh",
    "sports cards",
    "video games",
    "video game",
    "nintendo",
    "playstation",
    "xbox",
    "gamecube",
    "sega",
    "game boy",
    "gameboy",
    "pokemon center",
    "console",
    "retro games",
    "retro gaming",
    "vintage toys",
    "lego",
    "funko",
    "sealed games",
    "psa",
    "cgc",
    "bgs",
]

STRONG_TERMS = {
    "pokemon", "pokémon", "pokemon cards", "tcg", "magic the gathering",
    "yu-gi-oh", "yugioh", "video games", "video game", "nintendo",
    "playstation", "xbox", "gamecube", "sega", "game boy", "gameboy",
    "pokemon center", "retro games", "retro gaming", "psa", "cgc", "bgs",
}


@dataclass
class SaleMatch:
    title: str
    url: str
    source: str
    location: str
    distance: str
    dates: str
    keywords: list[str]
    photo_count: int
    confidence: int


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


def source_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "estatesales.net" in host:
        return "EstateSales.NET"
    if "estatesales.org" in host:
        return "EstateSales.org"
    if "estatesales.com" in host:
        return "EstateSales.com"
    return host.removeprefix("www.")


def listing_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    host = urlparse(base_url).netloc.lower()
    found: list[str] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href]"):
        href = urljoin(base_url, anchor.get("href", ""))
        parsed = urlparse(href)
        if parsed.netloc.lower() != host:
            continue
        path = parsed.path.lower()
        is_detail = False
        if "estatesales.net" in host:
            # Detail URLs normally continue past /TX/City/ZIP/...
            parts = [p for p in parsed.path.split("/") if p]
            is_detail = len(parts) >= 5 and parts[0].upper() == "TX"
        elif "estatesales.org" in host:
            is_detail = "/estate-sales/tx/" in path and bool(re.search(r"-\d{5,}$", path))
        elif "estatesales.com" in host:
            is_detail = "estate-sale" in path or "auction" in path
        if is_detail and href not in seen:
            seen.add(href)
            found.append(href)
    return found


def normalize_text(soup: BeautifulSoup) -> str:
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def find_keywords(text: str, keywords: list[str]) -> list[str]:
    low = text.lower()
    hits: list[str] = []
    for term in keywords:
        needle = str(term).strip().lower()
        if needle and needle in low and needle not in hits:
            hits.append(needle)
    return hits


def extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
        if title:
            return title
    if soup.title:
        return soup.title.get_text(" ", strip=True)[:180]
    return "Estate sale match"


def extract_location(text: str) -> str:
    patterns = [
        r"(?:Sale Address|Address)\s*[:\-]?\s*([^|]{0,120}?\bTX\s+\d{5})",
        r"([A-Za-z .'-]+,\s*TX\s+\d{5})",
        r"(\d{1,6}\s+[A-Za-z0-9 .#'-]+\s+[A-Za-z .'-]+,?\s*TX\s+\d{5})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()[:180]
    return "Location shown on listing"


def extract_distance(text: str, radius: int) -> str:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s+miles?\s+away\b", text, re.I)
    if match:
        miles = float(match.group(1))
        return f"{miles:g} miles away"
    return f"Listed in {radius}-mile search area"


def extract_dates(text: str) -> str:
    patterns = [
        r"((?:Sale|Auction|Bidding)\s+(?:starts|ends)[^.]{0,100})",
        r"((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+[A-Z][a-z]{2,8}\s+\d{1,2},?\s+20\d{2}[^|]{0,80})",
        r"(\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:\s+to\s+\w+\s+\d{1,2})?[^|]{0,60})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()[:160]
    return "See listing for dates/times"


def confidence_score(hits: list[str], text: str, photo_count: int) -> int:
    if not hits:
        return 0
    score = 40
    strong = sum(1 for hit in hits if hit in STRONG_TERMS)
    score += min(35, strong * 12)
    score += min(15, max(0, len(hits) - strong) * 5)
    if photo_count > 0:
        score += 5
    low = text.lower()
    if "sale description" in low or "about this sale" in low or "items" in low:
        score += 5
    return min(score, 100)


def photo_count(soup: BeautifulSoup) -> int:
    urls: set[str] = set()
    for image in soup.select("img[src], img[data-src]"):
        src = image.get("src") or image.get("data-src")
        if src and not src.startswith("data:"):
            urls.add(src)
    return len(urls)


def build_search_urls(location: dict[str, Any]) -> list[str]:
    state = str(location.get("state", "TX"))
    city = str(location["city"]).strip().replace(" ", "-")
    zip_code = str(location["zip"])
    return [
        f"https://www.estatesales.net/{state}/{city}/{zip_code}",
        f"https://estatesales.org/estate-sales/{state.lower()}/{city.lower()}/{zip_code}",
    ]


def inspect_sale(session: requests.Session, url: str, keywords: list[str], radius: int) -> SaleMatch | None:
    response = fetch(session, url)
    soup = BeautifulSoup(response.text, "html.parser")
    text = normalize_text(soup)
    hits = find_keywords(text, keywords)
    if not hits:
        return None
    photos = photo_count(soup)
    return SaleMatch(
        title=extract_title(soup),
        url=response.url,
        source=source_name(response.url),
        location=extract_location(text),
        distance=extract_distance(text, radius),
        dates=extract_dates(text),
        keywords=hits,
        photo_count=photos,
        confidence=confidence_score(hits, text, photos),
    )


def state_key(match: SaleMatch) -> str:
    return hashlib.sha256(match.url.encode("utf-8")).hexdigest()


def post_discord(payload: dict[str, Any]) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL secret is missing; skipping Discord message", file=sys.stderr)
        return
    response = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def send_alert(match: SaleMatch) -> None:
    keywords = ", ".join(match.keywords[:12])
    fields = [
        {"name": "Source", "value": match.source, "inline": True},
        {"name": "Distance", "value": match.distance, "inline": True},
        {"name": "Confidence", "value": f"{match.confidence}%", "inline": True},
        {"name": "Location", "value": match.location[:1024], "inline": False},
        {"name": "Sale date/time", "value": match.dates[:1024], "inline": False},
        {"name": "Matched keywords", "value": keywords[:1024], "inline": False},
        {"name": "Photos detected", "value": str(match.photo_count), "inline": True},
    ]
    post_discord({
        "username": "Estate Sale Finds",
        "embeds": [{
            "title": f"COLLECTIBLE MATCH: {match.title}"[:256],
            "url": match.url,
            "description": "A nearby estate-sale listing matched your collectible/gaming watch list.",
            "color": 10181046,
            "fields": fields,
        }],
    })


def send_summary(scanned_pages: int, checked_sales: int, matches: int, errors: list[str]) -> None:
    if not FORCE_REPORT:
        return
    post_discord({
        "username": "Estate Sale Finds",
        "embeds": [{
            "title": "Estate-sale scan completed",
            "color": 3447003 if not errors else 16753920,
            "fields": [
                {"name": "Search pages", "value": str(scanned_pages), "inline": True},
                {"name": "Sales checked", "value": str(checked_sales), "inline": True},
                {"name": "Matches", "value": str(matches), "inline": True},
                {"name": "Errors", "value": str(len(errors)), "inline": True},
                {"name": "Details", "value": ("\n".join(errors[:5]) or "None")[:1024], "inline": False},
            ],
        }],
    })


def main() -> int:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    estate = config.get("estate_sales", {})
    if not estate.get("enabled", False):
        print("Estate-sale scanning is disabled")
        return 0

    radius = int(estate.get("radius_miles", 50))
    keywords = [str(x).lower() for x in estate.get("keywords", DEFAULT_KEYWORDS)]
    locations = estate.get("locations", [])
    max_sales = int(estate.get("max_sales_per_search_page", 40))
    delay = float(estate.get("request_delay_seconds", 0.5))

    previous: dict[str, dict[str, Any]] = load_json(STATE_PATH, {})
    current = dict(previous)
    errors: list[str] = []
    scanned_pages = 0
    checked_sales = 0
    matches = 0

    session = requests.Session()
    session.headers.update(HEADERS)
    candidate_urls: list[str] = []
    seen_urls: set[str] = set()

    for location in locations:
        for search_url in build_search_urls(location):
            try:
                response = fetch(session, search_url)
                scanned_pages += 1
                soup = BeautifulSoup(response.text, "html.parser")
                urls = listing_urls(soup, response.url)
                for url in urls[:max_sales]:
                    if url not in seen_urls:
                        seen_urls.add(url)
                        candidate_urls.append(url)
            except Exception as exc:
                errors.append(f"{source_name(search_url)} search: {exc}")
            time.sleep(delay)

    for url in candidate_urls:
        try:
            checked_sales += 1
            match = inspect_sale(session, url, keywords, radius)
            if match is None:
                continue
            matches += 1
            key = state_key(match)
            signature = {
                "confidence": match.confidence,
                "keywords": match.keywords,
                "title": match.title,
            }
            if previous.get(key) != signature:
                send_alert(match)
                print(f"MATCH {match.confidence}% | {match.title} | {match.url}")
            current[key] = signature
        except Exception as exc:
            errors.append(f"{urlparse(url).netloc}: {exc}")
        time.sleep(delay)

    save_json(STATE_PATH, current)
    send_summary(scanned_pages, checked_sales, matches, errors)
    print(
        f"Estate scan: pages={scanned_pages} sales={checked_sales} "
        f"matches={matches} errors={len(errors)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
