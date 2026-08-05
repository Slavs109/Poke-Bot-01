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
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
SEND_RUN_SUMMARY = os.getenv("SEND_RUN_SUMMARY", "false").lower() == "true"
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_SECONDS", "25"))

OUT_OF_STOCK = (
    "out of stock", "sold out", "currently unavailable", "not available",
    "coming soon", "notify me when available", "not available online"
)
IN_STOCK = (
    "add to cart", "add to bag", "ship it", "buy now", "in stock",
    "available for shipping", "available for pickup", "limited stock"
)

RETAILERS = {
    "pokemoncenter.com": "Pokémon Center",
    "target.com": "Target",
    "walmart.com": "Walmart",
    "bestbuy.com": "Best Buy",
    "gamestop.com": "GameStop",
    "amazon.com": "Amazon",
    "barnesandnoble.com": "Barnes & Noble",
    "costco.com": "Costco",
    "samsclub.com": "Sam's Club",
    "acehardware.com": "Ace Hardware",
    "scheels.com": "SCHEELS",
    "walgreens.com": "Walgreens",
    "cvs.com": "CVS",
    "bjs.com": "BJ's Wholesale Club",
    "meijer.com": "Meijer",
    "kroger.com": "Kroger",
    "fredmeyer.com": "Fred Meyer",
    "macys.com": "Macy's",
    "fivebelow.com": "Five Below",
    "dollargeneral.com": "Dollar General",
    "familydollar.com": "Family Dollar",
    "hottopic.com": "Hot Topic",
    "boxlunch.com": "BoxLunch",
    "academy.com": "Academy Sports + Outdoors",
    "dickssportinggoods.com": "DICK'S Sporting Goods",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class Result:
    name: str
    url: str
    retailer: str
    price: float | None
    in_stock: bool
    evidence: str
    checkout_url: str


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def retailer_name(url: str, override: str | None = None) -> str:
    if override:
        return override
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for domain, name in RETAILERS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or "Retailer"


def parse_price(text: str) -> float | None:
    values: list[float] = []
    for raw in re.findall(r"\$\s*([0-9]{1,4}(?:,[0-9]{3})*(?:\.\d{2})?)", text):
        try:
            value = float(raw.replace(",", ""))
            if 1 <= value <= 1000:
                values.append(value)
        except ValueError:
            continue
    return min(values) if values else None


def iter_jsonld(value: Any):
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from iter_jsonld(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_jsonld(item)


def normalize_url(base_url: str, candidate: Any) -> str | None:
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    value = urljoin(base_url, candidate.strip())
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def parse_jsonld(
    soup: BeautifulSoup, base_url: str
) -> tuple[str | None, float | None, bool | None, str | None]:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            raw = script.string or script.get_text(" ", strip=True)
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for item in iter_jsonld(data):
            if item.get("@type") != "Product" and "offers" not in item:
                continue
            offers = item.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                offers = {}
            raw_price = offers.get("price") or offers.get("lowPrice")
            try:
                price = float(raw_price) if raw_price is not None else None
            except (TypeError, ValueError):
                price = None
            availability = str(offers.get("availability", "")).lower()
            stock = None
            if availability:
                stock = "instock" in availability and "outofstock" not in availability
            product_url = (
                normalize_url(base_url, offers.get("url"))
                or normalize_url(base_url, item.get("url"))
            )
            return item.get("name"), price, stock, product_url
    return None, None, None, None


def best_page_url(soup: BeautifulSoup, response_url: str) -> str:
    canonical = soup.select_one('link[rel~="canonical"][href]')
    if canonical:
        value = normalize_url(response_url, canonical.get("href"))
        if value:
            return value
    og_url = soup.select_one('meta[property="og:url"][content]')
    if og_url:
        value = normalize_url(response_url, og_url.get("content"))
        if value:
            return value
    return response_url


def build_session(url: str, item: dict[str, Any]) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    host = urlparse(url).netloc.lower()
    warmup_url = item.get("warmup_url")
    if not warmup_url and host.endswith("pokemoncenter.com"):
        # Pokémon Center often requires cookies from its home page before a product/search request.
        warmup_url = "https://www.pokemoncenter.com/"
    if warmup_url:
        try:
            session.get(str(warmup_url), timeout=15, allow_redirects=True)
            time.sleep(1)
        except requests.RequestException:
            pass
    return session


def fetch_with_retry(session: requests.Session, url: str) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, timeout=TIMEOUT, allow_redirects=True)
            if response.status_code not in {403, 429, 500, 502, 503, 504}:
                response.raise_for_status()
                return response
            last_error = requests.HTTPError(
                f"{response.status_code} Client Error for url: {response.url}",
                response=response,
            )
        except requests.RequestException as exc:
            last_error = exc
        if attempt < 2:
            time.sleep(2 ** attempt)
    assert last_error is not None
    raise last_error


def inspect(item: dict[str, Any]) -> Result:
    name = str(item["name"])
    url = str(item["url"])
    retailer = retailer_name(url, item.get("retailer"))
    configured_checkout = item.get("checkout_url") or item.get("cart_url")
    session = build_session(url, item)
    response = fetch_with_retry(session, url)
    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    ld_name, ld_price, ld_stock, ld_url = parse_jsonld(soup, response.url)
    price = ld_price if ld_price is not None else parse_price(text)
    product_url = ld_url or best_page_url(soup, response.url)
    checkout_url = (
        normalize_url(response.url, configured_checkout)
        if configured_checkout
        else product_url
    ) or product_url

    out_hits = [phrase for phrase in OUT_OF_STOCK if phrase in text]
    in_hits = [phrase for phrase in IN_STOCK if phrase in text]
    if ld_stock is not None:
        in_stock = ld_stock
        evidence = "structured product availability"
    elif out_hits:
        in_stock = False
        evidence = out_hits[0]
    else:
        in_stock = bool(in_hits)
        evidence = in_hits[0] if in_hits else "no purchase signal found"

    return Result(
        ld_name or name,
        product_url,
        retailer,
        price,
        in_stock,
        evidence,
        checkout_url,
    )


def state_key(item: dict[str, Any]) -> str:
    return hashlib.sha256(str(item["url"]).encode("utf-8")).hexdigest()


def state_signature(result: Result) -> str:
    price = "none" if result.price is None else f"{result.price:.2f}"
    return f"{int(result.in_stock)}|{price}|{result.checkout_url}"


def post_discord(payload: dict[str, Any]) -> None:
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing")
    response = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def send_alert(result: Result, max_price: float) -> None:
    price = "Price not detected" if result.price is None else f"${result.price:.2f}"
    post_discord({
        "username": "Pokémon MSRP Alerts",
        "embeds": [{
            "title": f"IN STOCK: {result.name}",
            "url": result.url,
            "description": "Open the retailer page below to continue toward checkout.",
            "color": 5763719,
            "fields": [
                {"name": "Retailer", "value": result.retailer, "inline": True},
                {"name": "Price", "value": price, "inline": True},
                {"name": "Maximum", "value": f"${max_price:.2f}", "inline": True},
            ],
            "footer": {"text": "Confirm seller, price, shipping, membership, and quantity."},
        }],
        "components": [{
            "type": 1,
            "components": [{
                "type": 2,
                "style": 5,
                "label": "OPEN PRODUCT / CHECKOUT",
                "url": result.checkout_url,
            }],
        }],
    })


def send_summary(checked: int, eligible: int, failures: list[str]) -> None:
    if not SEND_RUN_SUMMARY or not WEBHOOK_URL:
        return
    error_text = "None" if not failures else "\n".join(f"• {value}" for value in failures[:8])
    post_discord({
        "username": "Pokémon MSRP Alerts",
        "embeds": [{
            "title": "Stock scan completed",
            "color": 3447003 if not failures else 16753920,
            "fields": [
                {"name": "Pages checked", "value": str(checked), "inline": True},
                {"name": "Qualifying items", "value": str(eligible), "inline": True},
                {"name": "Errors", "value": str(len(failures)), "inline": True},
                {"name": "Error details", "value": error_text[:1024], "inline": False},
            ],
        }],
    })


def main() -> int:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    previous: dict[str, str] = load_json(STATE_PATH, {})
    current = dict(previous)
    failures: list[str] = []
    checked = 0
    eligible_count = 0

    for item in config.get("watches", []):
        if not item.get("enabled", True):
            continue
        key = state_key(item)
        try:
            result = inspect(item)
            checked += 1
            maximum = float(item["max_price"])
            signature = state_signature(result)
            eligible = result.in_stock and result.price is not None and result.price <= maximum
            changed = previous.get(key) != signature
            print(
                f"{result.retailer}: {result.name} | stock={result.in_stock} | "
                f"price={result.price} | {result.evidence} | link={result.checkout_url}"
            )
            if eligible:
                eligible_count += 1
            if eligible and changed:
                send_alert(result, maximum)
                print("  Alert sent")
            current[key] = signature
        except Exception as exc:
            message = f"{item.get('name', 'unknown')}: {exc}"
            failures.append(message)
            print(f"ERROR checking {message}", file=sys.stderr)
        time.sleep(1)

    save_json(STATE_PATH, current)
    send_summary(checked, eligible_count, failures)
    return 0 if len(failures) < max(3, len(config.get("watches", []))) else 1


if __name__ == "__main__":
    raise SystemExit(main())
