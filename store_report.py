from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import requests
import yaml

import check_stock
import target_inventory

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config.yaml"))
STATE_PATH = Path(os.getenv("STATE_PATH", "state.json"))
REPORT_STATE_PATH = Path(os.getenv("REPORT_STATE_PATH", "report_state.json"))
QUEUE_STATE_PATH = Path(os.getenv("QUEUE_STATE_PATH", "queue_state.json"))
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
FORCE_REPORT = os.getenv("FORCE_REPORT", "false").lower() == "true"
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_SECONDS", "25"))


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def check_pokemon_center() -> tuple[str, str]:
    queue_state = load_json(QUEUE_STATE_PATH, {})
    queue_active = bool(queue_state.get("active", False))
    try:
        session = requests.Session()
        session.headers.update(check_stock.HEADERS)
        response = session.get("https://www.pokemoncenter.com/", timeout=TIMEOUT, allow_redirects=True)
        if response.status_code in {403, 429}:
            return "BLOCKED", f"HTTP {response.status_code}; queue active: {queue_active}"
        response.raise_for_status()
        return "REACHABLE", f"HTTP {response.status_code}; queue {'active' if queue_active else 'not detected'}"
    except requests.Timeout:
        return "TIMED OUT", f"No response within {TIMEOUT}s"
    except requests.RequestException as exc:
        return "ERROR", str(exc)[:180]


def report_signature(pokemon_center: tuple[str, str], stores: dict[str, list[str]]) -> str:
    encoded = json.dumps({"pokemon_center": pokemon_center, "stores": stores}, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def send_report(pokemon_center: tuple[str, str], stores: dict[str, list[str]], config: dict[str, Any]) -> None:
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing")

    status, detail = pokemon_center
    local = config.get("local_inventory", {})
    zips = ", ".join(str(z) for z in local.get("zip_codes", [])) or "not configured"
    radius = local.get("radius_miles", "?")
    fields = [{"name": f"Pokémon Center — {status}", "value": detail[:1024], "inline": False}]

    for store in sorted(stores):
        fields.append({"name": store, "value": "\n".join(stores[store])[:1024] or "No results", "inline": False})

    response = requests.post(
        WEBHOOK_URL,
        json={
            "username": "Pokémon MSRP Alerts",
            "embeds": [{
                "title": "Pokémon MSRP and local inventory report",
                "description": (
                    f"Inventory area: ZIP {zips}, within {radius} miles. "
                    "Target uses TCIN-based nearby-store checks. Reported quantities are estimates and can change before arrival."
                ),
                "color": 3447003,
                "fields": fields[:25],
                "footer": {"text": "Use the direct item link to confirm pickup and complete checkout."},
            }],
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def target_lines(item: dict[str, Any], config: dict[str, Any]) -> list[str]:
    local = config.get("local_inventory", {})
    zip_codes = [str(value) for value in local.get("zip_codes", [])]
    radius = int(local.get("radius_miles", 25))
    max_stores = int(local.get("max_stores_per_item", 8))
    message, inventory = target_inventory.lookup_target(item, zip_codes, radius)
    direct = str(item.get("checkout_url") or item["url"])
    available = [entry for entry in inventory if entry.pickup or entry.quantity and entry.quantity > 0]
    if not available:
        return [f"➖ **No local pickup found** — [{item['name']}]({direct})\n{message}"]

    lines: list[str] = []
    for entry in available[:max_stores]:
        quantity = f" — reported qty {entry.quantity}" if entry.quantity is not None else ""
        distance = f" — {entry.distance_miles:.1f} mi" if entry.distance_miles is not None else ""
        lines.append(
            f"🏪 **{entry.store_name}** — {entry.status}{quantity}{distance}\n"
            f"{entry.address}\n[{item['name']}]({direct})"
        )
    return lines


def main() -> int:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    previous: dict[str, str] = load_json(STATE_PATH, {})
    current = dict(previous)
    stores: dict[str, list[str]] = defaultdict(list)
    failures = 0

    pokemon_center = check_pokemon_center()
    print(f"Pokemon Center: {pokemon_center[0]} | {pokemon_center[1]}")

    for item in config.get("watches", []):
        if not item.get("enabled", True):
            continue
        name = str(item.get("name", "Unknown item"))
        store = check_stock.retailer_name(str(item["url"]), item.get("retailer"))
        location = str(item.get("location_scope", "Location not exposed"))
        key = check_stock.state_key(item)
        try:
            if item.get("inventory_provider") == "target":
                stores[store].extend(target_lines(item, config))

            result = check_stock.inspect(item)
            maximum = float(item["max_price"])
            eligible = result.in_stock and result.price is not None and result.price <= maximum
            signature = check_stock.state_signature(result)
            changed = previous.get(key) != signature
            price = "unknown price" if result.price is None else f"${result.price:.2f}"

            if eligible:
                stores[store].append(f"✅ **ONLINE FOUND** — [{result.name}]({result.checkout_url}) — {price}\n📍 {location}")
                if changed:
                    check_stock.send_alert(result, maximum)
                    print(f"Alert sent: {store} | {result.name}")
            elif item.get("inventory_provider") != "target":
                stores[store].append(f"➖ Nothing found — [{name}]({item['checkout_url']})\n📍 {location}\nReason: {result.evidence}")

            current[key] = signature
            print(f"{store}: eligible={eligible} price={result.price} link={result.checkout_url}")
        except check_stock.RetailerBlockedError as exc:
            stores[store].append(f"🚫 Blocked — [{name}]({item['checkout_url']})\n📍 {location}")
            print(f"BLOCKED {store}: {exc}", file=sys.stderr)
        except TimeoutError as exc:
            stores[store].append(f"⏱️ Timed out — [{name}]({item['checkout_url']})\n📍 {location}")
            print(f"TIMEOUT {store}: {exc}", file=sys.stderr)
        except Exception as exc:
            failures += 1
            stores[store].append(f"⚠️ Inventory unavailable — [{name}]({item['checkout_url']})\n📍 {location}\n{str(exc)[:160]}")
            print(f"ERROR {store}: {exc}", file=sys.stderr)
        time.sleep(1)

    save_json(STATE_PATH, current)
    signature = report_signature(pokemon_center, dict(stores))
    old_report = load_json(REPORT_STATE_PATH, {}).get("signature")
    if FORCE_REPORT or signature != old_report:
        send_report(pokemon_center, dict(stores), config)
        print("Store report sent")
    else:
        print("Store report unchanged; Discord summary skipped")
    save_json(REPORT_STATE_PATH, {"signature": signature})
    return 0 if failures < 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
