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
from urllib.parse import urlparse

import requests
import yaml
from bs4 import BeautifulSoup

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config.yaml"))
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
TIMEOUT = 25

OUT_OF_STOCK = (
    "out of stock", "sold out", "currently unavailable", "not available",
    "coming soon", "notify me when available"
)
IN_STOCK = (
    "add to cart", "add to bag", "ship it", "buy now", "in stock"
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
    "scheels.com": "SCHEELS",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class Result:
    name: str
    url: str
    retailer: str
    price: float | None
    in_stock: bool
    evidence: str
    cart_url: str


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


def parse_jsonld(soup: BeautifulSoup) -> tuple[str | None, float | None, bool | None]:
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
            return item.get("name"), price, stock
    return None, None, None


def inspect(item: dict[str, Any]) -> Result:
    name = str(item["name"])
    url = str(item["url"])
    retailer = retailer_name(url, item.get("retailer"))
    cart_url = str(item.get("cart_url") or url)
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    ld_name, ld_price, ld_stock = parse_jsonld(soup)
    price = ld_price if ld_price is not None else parse_price(text)

    out_hits = [p for p in OUT_OF_STOCK if p in text]
    in_hits = [p for p in IN_STOCK if p in text]
    if ld_stock is not None:
        in_stock = ld_stock
        evidence = "structured product availability"
    elif out_hits:
        in_stock = False
        evidence = out_hits[0]
    else:
        in_stock = bool(in_hits)
        evidence = in_hits[0] if in_hits else "no purchase signal found"

    return Result(ld_name or name, response.url, retailer, price, in_stock, evidence, cart_url)


def state_key(item: dict[str, Any]) -> str:
    return hashlib.sha256(str(item["url"]).encode("utf-8")).hexdigest()


def state_signature(result: Result) -> str:
    price = "none" if result.price is None else f"{result.price:.2f}"
    return f"{int(result.in_stock)}|{price}"


def send_alert(result: Result, max_price: float) -> None:
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing")
    price = "Price not detected" if result.price is None else f"${result.price:.2f}"
    payload = {
        "username": "Pokémon MSRP Alerts",
        "embeds": [{
            "title": f"IN STOCK: {result.name}",
            "url": result.cart_url,
            "description": "Open the button below to continue toward checkout.",
            "color": 5763719,
            "fields": [
                {"name": "Retailer", "value": result.retailer, "inline": True},
                {"name": "Price", "value": price, "inline": True},
                {"name": "Maximum", "value": f"${max_price:.2f}", "inline": True},
            ],
            "footer": {"text": "Confirm seller, price, shipping, and quantity before purchasing."},
        }],
        "components": [{
            "type": 1,
            "components": [{
                "type": 2,
                "style": 5,
                "label": "ADD TO CART / CHECKOUT",
                "url": result.cart_url,
            }],
        }],
    }
    response = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def main() -> int:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    previous: dict[str, str] = load_json(STATE_PATH, {})
    current = dict(previous)
    failures = 0

    for item in config.get("watches", []):
        if not item.get("enabled", True):
            continue
        key = state_key(item)
        try:
            result = inspect(item)
            maximum = float(item["max_price"])
            signature = state_signature(result)
            eligible = result.in_stock and result.price is not None and result.price <= maximum
            changed = previous.get(key) != signature
            print(f"{result.retailer}: {result.name} | stock={result.in_stock} | price={result.price} | {result.evidence}")
            if eligible and changed:
                send_alert(result, maximum)
                print("  Alert sent")
            current[key] = signature
        except Exception as exc:
            failures += 1
            print(f"ERROR checking {item.get('name', 'unknown')}: {exc}", file=sys.stderr)
        time.sleep(1)

    save_json(STATE_PATH, current)
    # A retailer block should not stop state from being saved or other checks from running.
    return 0 if failures < max(3, len(config.get("watches", []))) else 1


if __name__ == "__main__":
    raise SystemExit(main())
