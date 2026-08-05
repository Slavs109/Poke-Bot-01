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
        response = session.get(
            "https://www.pokemoncenter.com/",
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        if response.status_code in {403, 429}:
            return "BLOCKED", f"HTTP {response.status_code}; queue active: {queue_active}"
        response.raise_for_status()
        queue_text = "active" if queue_active else "not detected"
        return "REACHABLE", f"HTTP {response.status_code}; queue {queue_text}"
    except requests.Timeout:
        return "TIMED OUT", f"No response within {TIMEOUT}s"
    except requests.RequestException as exc:
        return "ERROR", str(exc)[:180]


def report_signature(pokemon_center: tuple[str, str], stores: dict[str, list[str]]) -> str:
    payload = {"pokemon_center": pokemon_center, "stores": stores}
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def send_report(pokemon_center: tuple[str, str], stores: dict[str, list[str]]) -> None:
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing")

    status, detail = pokemon_center
    fields = [{
        "name": f"Pokémon Center — {status}",
        "value": detail[:1024],
        "inline": False,
    }]

    for store in sorted(stores):
        value = "\n".join(stores[store])[:1024]
        fields.append({"name": store, "value": value or "No results", "inline": False})

    response = requests.post(
        WEBHOOK_URL,
        json={
            "username": "Pokémon MSRP Alerts",
            "embeds": [{
                "title": "Pokémon MSRP store report",
                "description": (
                    "Direct product checks only. A FOUND result includes the exact item link. "
                    "Blocked and timed-out stores are shown instead of being treated as stock results."
                ),
                "color": 3447003,
                "fields": fields[:25],
            }],
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()


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
        key = check_stock.state_key(item)
        try:
            result = check_stock.inspect(item)
            maximum = float(item["max_price"])
            eligible = result.in_stock and result.price is not None and result.price <= maximum
            signature = check_stock.state_signature(result)
            changed = previous.get(key) != signature
            price = "unknown price" if result.price is None else f"${result.price:.2f}"

            if eligible:
                stores[store].append(
                    f"✅ **FOUND** — [{result.name}]({result.checkout_url}) — {price}"
                )
                if changed:
                    check_stock.send_alert(result, maximum)
                    print(f"Alert sent: {store} | {result.name}")
            else:
                reason = result.evidence
                stores[store].append(f"➖ Nothing found — {name} ({reason})")

            current[key] = signature
            print(f"{store}: eligible={eligible} price={result.price} link={result.checkout_url}")
        except check_stock.RetailerBlockedError as exc:
            stores[store].append(f"🚫 Blocked — {name}")
            print(f"BLOCKED {store}: {exc}", file=sys.stderr)
        except TimeoutError as exc:
            stores[store].append(f"⏱️ Timed out — {name}")
            print(f"TIMEOUT {store}: {exc}", file=sys.stderr)
        except Exception as exc:
            failures += 1
            stores[store].append(f"⚠️ Error — {name}")
            print(f"ERROR {store}: {exc}", file=sys.stderr)
        time.sleep(1)

    save_json(STATE_PATH, current)
    signature = report_signature(pokemon_center, dict(stores))
    old_report = load_json(REPORT_STATE_PATH, {}).get("signature")
    if FORCE_REPORT or signature != old_report:
        send_report(pokemon_center, dict(stores))
        print("Store report sent")
    else:
        print("Store report unchanged; Discord summary skipped")
    save_json(REPORT_STATE_PATH, {"signature": signature})

    return 0 if failures < 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
