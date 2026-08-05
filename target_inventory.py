from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

import requests

import check_stock

TIMEOUT = int(os.getenv("PAGE_TIMEOUT_SECONDS", "25"))
TARGET_KEY = os.getenv("TARGET_REDSKY_KEY", "").strip()


@dataclass(frozen=True)
class StoreInventory:
    store_id: str
    store_name: str
    address: str
    distance_miles: float | None
    status: str
    quantity: int | None
    pickup: bool


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def discover_redsky_key(session: requests.Session, product_url: str) -> str | None:
    if TARGET_KEY:
        return TARGET_KEY
    response = session.get(product_url, timeout=TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    patterns = (
        r"redsky[^\"']+[?&]key=([A-Za-z0-9_-]{12,})",
        r'"apiKey"\s*:\s*"([A-Za-z0-9_-]{12,})"',
        r'"redskyKey"\s*:\s*"([A-Za-z0-9_-]{12,})"',
    )
    for pattern in patterns:
        match = re.search(pattern, response.text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def nearby_stores(session: requests.Session, key: str, zip_code: str, radius: int) -> list[dict[str, Any]]:
    endpoints = (
        "https://redsky.target.com/redsky_aggregations/v1/web/nearby_stores_v1",
        "https://redsky.target.com/redsky_aggregations/v1/web/store_location_v1",
    )
    params = {
        "key": key,
        "place": zip_code,
        "within": radius,
        "limit": 25,
        "channel": "WEB",
    }
    last_error: Exception | None = None
    for endpoint in endpoints:
        try:
            response = session.get(endpoint, params=params, timeout=TIMEOUT)
            if response.status_code in {403, 429}:
                raise check_stock.RetailerBlockedError(f"Target inventory blocked with HTTP {response.status_code}")
            response.raise_for_status()
            stores: list[dict[str, Any]] = []
            for node in _walk(response.json()):
                store_id = _first(node, "store_id", "location_id", "id")
                name = _first(node, "store_name", "location_name", "name")
                if store_id and name and str(store_id).isdigit():
                    stores.append(node)
            if stores:
                unique: dict[str, dict[str, Any]] = {}
                for store in stores:
                    store_id = str(_first(store, "store_id", "location_id", "id"))
                    unique[store_id] = store
                return list(unique.values())
        except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    if last_error:
        raise RuntimeError(f"Target nearby-store lookup failed: {last_error}")
    return []


def fulfillment(session: requests.Session, key: str, tcin: str, store_ids: list[str], zip_code: str) -> Any:
    endpoint = "https://redsky.target.com/redsky_aggregations/v1/web/fiats_v1"
    params = {
        "key": key,
        "tcin": tcin,
        "store_id": ",".join(store_ids),
        "zip": zip_code,
        "channel": "WEB",
    }
    response = session.get(endpoint, params=params, timeout=TIMEOUT)
    if response.status_code in {403, 429}:
        raise check_stock.RetailerBlockedError(f"Target fulfillment blocked with HTTP {response.status_code}")
    response.raise_for_status()
    return response.json()


def _store_meta(store: dict[str, Any]) -> tuple[str, str, str, float | None]:
    store_id = str(_first(store, "store_id", "location_id", "id") or "unknown")
    name = str(_first(store, "store_name", "location_name", "name") or f"Target {store_id}")
    address = _first(store, "address", "formatted_address", "address_line1")
    if isinstance(address, dict):
        address = ", ".join(str(v) for v in address.values() if v)
    distance = _first(store, "distance", "distance_miles")
    try:
        distance_value = float(distance) if distance is not None else None
    except (TypeError, ValueError):
        distance_value = None
    return store_id, name, str(address or "Address not exposed"), distance_value


def parse_inventory(payload: Any, stores: list[dict[str, Any]]) -> list[StoreInventory]:
    store_meta = {str(_first(s, "store_id", "location_id", "id")): _store_meta(s) for s in stores}
    results: dict[str, StoreInventory] = {}
    for node in _walk(payload):
        store_id = _first(node, "store_id", "location_id")
        if store_id is None:
            continue
        store_id = str(store_id)
        status_raw = str(_first(node, "availability_status", "availability", "inventory_status", "status") or "UNKNOWN")
        quantity_raw = _first(node, "location_available_to_promise_quantity", "available_to_promise_quantity", "quantity")
        try:
            quantity = int(float(quantity_raw)) if quantity_raw is not None else None
        except (TypeError, ValueError):
            quantity = None
        pickup_raw = _first(node, "order_pickup", "pickup", "is_pickup_available", "buy_url")
        pickup = bool(pickup_raw) or status_raw.upper() in {"IN_STOCK", "LIMITED_STOCK", "AVAILABLE"}
        meta = store_meta.get(store_id, (store_id, f"Target {store_id}", "Address not exposed", None))
        results[store_id] = StoreInventory(
            store_id=store_id,
            store_name=meta[1],
            address=meta[2],
            distance_miles=meta[3],
            status=status_raw.upper().replace(" ", "_"),
            quantity=quantity,
            pickup=pickup,
        )
    return sorted(results.values(), key=lambda x: (x.distance_miles is None, x.distance_miles or 9999, x.store_name))


def lookup_target(item: dict[str, Any], zip_codes: list[str], radius: int) -> tuple[str, list[StoreInventory]]:
    tcin = str(item.get("tcin") or "").strip()
    if not tcin:
        return "TCIN not configured", []
    session = requests.Session()
    session.headers.update(check_stock.HEADERS)
    key = discover_redsky_key(session, str(item["url"]))
    if not key:
        return "Target inventory key was not exposed; product-page check still ran", []

    combined: dict[str, StoreInventory] = {}
    for zip_code in zip_codes:
        stores = nearby_stores(session, key, zip_code, radius)
        store_ids = [str(_first(s, "store_id", "location_id", "id")) for s in stores]
        if not store_ids:
            continue
        payload = fulfillment(session, key, tcin, store_ids, zip_code)
        for result in parse_inventory(payload, stores):
            combined[result.store_id] = result
    return "Target local inventory checked", list(combined.values())
