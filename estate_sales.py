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
PHOTO_STATE_PATH = Path(os.getenv("ESTATE_PHOTO_STATE_PATH", "estate_photo_state.json"))
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
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
    "pokemon", "pokémon", "pokemon cards", "pokemon tcg", "tcg",
    "magic the gathering", "mtg", "yu-gi-oh", "yugioh",
    "one piece card game", "lorcana", "digimon card game",
    "flesh and blood", "dragon ball super card game", "weiss schwarz",
    "elite trainer box", "etb",
]


@dataclass
class VisionHit:
    matched: bool = False
    confidence: int = 0
    labels: list[str] | None = None
    reason: str = ""
    photo_url: str = ""


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
    trigger: str
    vision: VisionHit | None = None


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


def literal_keyword_present(text: str, keyword: str) -> bool:
    needle = keyword.strip().lower()
    if not needle:
        return False
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", text.lower()) is not None


def find_keywords(text: str, keywords: list[str]) -> list[str]:
    hits: list[str] = []
    for term in keywords:
        needle = str(term).strip().lower()
        if needle and literal_keyword_present(text, needle) and needle not in hits:
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
        return f"{float(match.group(1)):g} miles away"
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


def photo_urls(soup: BeautifulSoup, base_url: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for image in soup.select("img"):
        candidates: list[str] = []
        for attr in ("data-src", "data-lazy-src", "src"):
            value = image.get(attr)
            if isinstance(value, str):
                candidates.append(value)
        srcset = image.get("srcset") or image.get("data-srcset")
        if isinstance(srcset, str):
            candidates.extend(part.strip().split(" ")[0] for part in srcset.split(","))
        for candidate in candidates:
            if not candidate or candidate.startswith("data:"):
                continue
            url = urljoin(base_url, candidate)
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"}:
                continue
            low = url.lower()
            if any(x in low for x in ("logo", "icon", "avatar", "favicon", "sprite", "tracking")):
                continue
            if url not in seen:
                seen.add(url)
                found.append(url)
    return found


def build_search_urls(location: dict[str, Any]) -> list[str]:
    state = str(location.get("state", "TX"))
    city = str(location["city"]).strip().replace(" ", "-")
    zip_code = str(location["zip"])
    return [
        f"https://www.estatesales.net/{state}/{city}/{zip_code}",
        f"https://estatesales.org/estate-sales/{state.lower()}/{city.lower()}/{zip_code}",
    ]


def response_output_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in payload.get("output", []):
        for part in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                chunks.append(str(part.get("text", "")))
    return "\n".join(chunks).strip()


def analyze_photo_batch(image_urls: list[str], model: str) -> VisionHit:
    if not OPENAI_API_KEY or not image_urls:
        return VisionHit()
    content: list[dict[str, Any]] = [{
        "type": "input_text",
        "text": (
            "Inspect these estate-sale photos. Decide whether ANY photo clearly shows NON-SPORTS "
            "trading cards or sealed non-sports TCG products. Positive examples include Pokemon, "
            "Magic: The Gathering, Yu-Gi-Oh!, One Piece Card Game, Lorcana, Digimon, Flesh and Blood, "
            "Dragon Ball Super Card Game, Weiss Schwarz, graded non-sports card slabs, booster packs, "
            "booster boxes, or Elite Trainer Boxes. IGNORE sports cards, baseball/football/basketball/"
            "hockey/soccer cards, generic collectibles, playing cards, postcards, board games, and items "
            "that merely resemble cards. Be conservative. Return ONLY JSON exactly like: "
            '{"match":true,"confidence":92,"labels":["Pokemon cards"],"photo_index":2,'
            '"reason":"visible Pokemon card binder pages"}. If uncertain, match=false.'
        ),
    }]
    for url in image_urls:
        content.append({"type": "input_image", "image_url": url, "detail": "auto"})
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json={"model": model, "input": [{"role": "user", "content": content}]},
        timeout=90,
    )
    response.raise_for_status()
    raw = response_output_text(response.json())
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
    data = json.loads(raw)
    index = int(data.get("photo_index", 0) or 0)
    photo = image_urls[index - 1] if 1 <= index <= len(image_urls) else image_urls[0]
    return VisionHit(
        matched=bool(data.get("match", False)),
        confidence=max(0, min(100, int(data.get("confidence", 0) or 0))),
        labels=[str(x) for x in data.get("labels", [])][:8],
        reason=str(data.get("reason", ""))[:300],
        photo_url=photo,
    )


def analyze_photos(urls: list[str], photo_cfg: dict[str, Any]) -> VisionHit:
    if not OPENAI_API_KEY or not photo_cfg.get("enabled", False):
        return VisionHit()
    maximum = int(photo_cfg.get("max_photos_per_sale", 8))
    batch_size = max(1, int(photo_cfg.get("photos_per_request", 4)))
    model = str(photo_cfg.get("model", "gpt-5-mini"))
    minimum = int(photo_cfg.get("minimum_confidence", 85))
    selected = urls[:maximum]
    best = VisionHit()
    for start in range(0, len(selected), batch_size):
        batch = selected[start:start + batch_size]
        hit = analyze_photo_batch(batch, model)
        if hit.confidence > best.confidence:
            best = hit
        if hit.matched and hit.confidence >= minimum:
            return hit
    if best.confidence < minimum:
        best.matched = False
    return best


def sale_details(session: requests.Session, url: str, radius: int) -> tuple[requests.Response, BeautifulSoup, str, list[str]]:
    response = fetch(session, url)
    soup = BeautifulSoup(response.text, "html.parser")
    text = normalize_text(soup)
    return response, soup, text, photo_urls(soup, response.url)


def state_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def photos_signature(urls: list[str]) -> str:
    return hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()


def post_discord(payload: dict[str, Any]) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL secret is missing; skipping Discord message", file=sys.stderr)
        return
    response = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def send_alert(match: SaleMatch) -> None:
    fields = [
        {"name": "Source", "value": match.source, "inline": True},
        {"name": "Distance", "value": match.distance, "inline": True},
        {"name": "Trigger", "value": match.trigger, "inline": True},
        {"name": "Location", "value": match.location[:1024], "inline": False},
        {"name": "Sale date/time", "value": match.dates[:1024], "inline": False},
        {"name": "Photos detected", "value": str(match.photo_count), "inline": True},
    ]
    if match.keywords:
        fields.append({"name": "Exact keyword(s)", "value": ", ".join(match.keywords)[:1024], "inline": False})
    if match.vision and match.vision.matched:
        labels = ", ".join(match.vision.labels or []) or "Non-sports trading cards"
        fields.extend([
            {"name": "Photo confidence", "value": f"{match.vision.confidence}%", "inline": True},
            {"name": "Photo identified", "value": labels[:1024], "inline": False},
            {"name": "Why", "value": match.vision.reason[:1024] or "Visible trading-card product", "inline": False},
        ])
    embed: dict[str, Any] = {
        "title": f"TRADING CARD MATCH: {match.title}"[:256],
        "url": match.url,
        "description": "Matched by exact listing text and/or a conservative scan of the sale photos.",
        "color": 10181046,
        "fields": fields,
    }
    if match.vision and match.vision.photo_url:
        embed["image"] = {"url": match.vision.photo_url}
    post_discord({"username": "Estate Sale Card Finds", "embeds": [embed]})


def send_summary(scanned_pages: int, checked_sales: int, matches: int, photo_scans: int, errors: list[str]) -> None:
    if not FORCE_REPORT:
        return
    vision_status = "enabled" if OPENAI_API_KEY else "disabled (OPENAI_API_KEY missing)"
    post_discord({
        "username": "Estate Sale Card Finds",
        "embeds": [{
            "title": "Estate-sale card scan completed",
            "color": 3447003 if not errors else 16753920,
            "fields": [
                {"name": "Search pages", "value": str(scanned_pages), "inline": True},
                {"name": "Sales checked", "value": str(checked_sales), "inline": True},
                {"name": "Matches", "value": str(matches), "inline": True},
                {"name": "New photo scans", "value": str(photo_scans), "inline": True},
                {"name": "Vision", "value": vision_status, "inline": True},
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

    radius = int(estate.get("radius_miles", 25))
    keywords = [str(x).lower() for x in estate.get("keywords", DEFAULT_KEYWORDS)]
    locations = estate.get("locations", [])
    max_sales = int(estate.get("max_sales_per_search_page", 40))
    delay = float(estate.get("request_delay_seconds", 0.5))
    photo_cfg = estate.get("photo_analysis", {}) or {}
    photo_scan_limit = int(photo_cfg.get("max_new_sales_scanned_per_run", 15))

    previous: dict[str, dict[str, Any]] = load_json(STATE_PATH, {})
    current = dict(previous)
    photo_state: dict[str, dict[str, Any]] = load_json(PHOTO_STATE_PATH, {})
    errors: list[str] = []
    scanned_pages = checked_sales = matches = photo_scans = 0

    session = requests.Session()
    session.headers.update(HEADERS)
    candidate_urls: list[str] = []
    seen_urls: set[str] = set()

    for location in locations:
        for search_url in build_search_urls(location):
            try:
                response = fetch(session, search_url)
                scanned_pages += 1
                for url in listing_urls(BeautifulSoup(response.text, "html.parser"), response.url)[:max_sales]:
                    if url not in seen_urls:
                        seen_urls.add(url)
                        candidate_urls.append(url)
            except Exception as exc:
                errors.append(f"{source_name(search_url)} search: {exc}")
            time.sleep(delay)

    for url in candidate_urls:
        try:
            checked_sales += 1
            response, soup, text, photos = sale_details(session, url, radius)
            hits = find_keywords(text, keywords)
            vision = VisionHit()
            key = state_key(response.url)
            signature = photos_signature(photos)

            if not hits and photo_cfg.get("enabled", False) and OPENAI_API_KEY and photos:
                cached = photo_state.get(key, {})
                if cached.get("photos_signature") == signature:
                    vision = VisionHit(
                        matched=bool(cached.get("matched", False)),
                        confidence=int(cached.get("confidence", 0) or 0),
                        labels=list(cached.get("labels", [])),
                        reason=str(cached.get("reason", "")),
                        photo_url=str(cached.get("photo_url", "")),
                    )
                elif photo_scans < photo_scan_limit:
                    photo_scans += 1
                    vision = analyze_photos(photos, photo_cfg)
                    photo_state[key] = {
                        "photos_signature": signature,
                        "matched": vision.matched,
                        "confidence": vision.confidence,
                        "labels": vision.labels or [],
                        "reason": vision.reason,
                        "photo_url": vision.photo_url,
                    }

            if not hits and not vision.matched:
                continue

            matches += 1
            trigger = "exact keyword + photo" if hits and vision.matched else ("exact keyword" if hits else "photo recognition")
            match = SaleMatch(
                title=extract_title(soup),
                url=response.url,
                source=source_name(response.url),
                location=extract_location(text),
                distance=extract_distance(text, radius),
                dates=extract_dates(text),
                keywords=hits,
                photo_count=len(photos),
                trigger=trigger,
                vision=vision if vision.matched else None,
            )
            alert_signature = {
                "keywords": hits,
                "title": match.title,
                "vision": bool(vision.matched),
                "vision_confidence": vision.confidence if vision.matched else 0,
                "vision_photo": vision.photo_url if vision.matched else "",
            }
            if previous.get(key) != alert_signature:
                send_alert(match)
                print(f"MATCH {trigger} | {match.title} | {match.url}")
            current[key] = alert_signature
        except Exception as exc:
            errors.append(f"{urlparse(url).netloc}: {exc}")
        time.sleep(delay)

    save_json(STATE_PATH, current)
    save_json(PHOTO_STATE_PATH, photo_state)
    send_summary(scanned_pages, checked_sales, matches, photo_scans, errors)
    print(f"Estate scan: pages={scanned_pages} sales={checked_sales} matches={matches} photo_scans={photo_scans} errors={len(errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
