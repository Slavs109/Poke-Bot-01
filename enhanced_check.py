from __future__ import annotations

import hashlib
import os
import sys
import time
from typing import Any

import yaml

import check_stock


def quality_score(item: dict[str, Any], result: check_stock.Result) -> tuple[int, str]:
    """Score listing quality without claiming store quantity we cannot verify."""
    maximum = float(item["max_price"])
    first_party = bool(item.get("first_party", False))
    pickup = "pickup" in result.evidence.lower() or "pick up" in result.evidence.lower()

    if result.price is None:
        return 2, "Price could not be verified"
    if result.price > maximum + 5:
        return 1, "Above the configured MSRP limit"
    if result.price > maximum:
        return 3, "Slightly above the configured limit"
    if first_party and pickup:
        return 5, "MSRP or lower, first-party retailer, pickup signal detected"
    if first_party:
        return 4, "MSRP or lower from a first-party retailer"
    return 3, "Price qualifies, but seller quality is not confirmed"


def priority_label(item: dict[str, Any]) -> str:
    value = int(item.get("priority", 3))
    return {1: "Priority 1 — instant", 2: "Priority 2", 3: "Priority 3"}.get(value, "Priority 3")


def status_signature(rows: list[str]) -> str:
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def send_item_alert(
    item: dict[str, Any], result: check_stock.Result, stars: int, explanation: str,
    zip_codes: list[str], radius: int,
) -> None:
    price = "Unknown" if result.price is None else f"${result.price:.2f}"
    check_stock.post_discord({
        "username": "Pokémon MSRP Alerts",
        "embeds": [{
            "title": f"{'⭐' * stars} {item.get('set', 'Pokémon TCG')} found",
            "url": result.checkout_url,
            "description": result.name,
            "color": 5763719,
            "fields": [
                {"name": "Store", "value": result.retailer, "inline": True},
                {"name": "Price", "value": price, "inline": True},
                {"name": "Priority", "value": priority_label(item), "inline": True},
                {"name": "Quality", "value": f"{stars}/5 — {explanation}", "inline": False},
                {"name": "Local areas", "value": f"ZIPs {', '.join(zip_codes)} within {radius} miles", "inline": False},
                {"name": "Availability evidence", "value": result.evidence, "inline": False},
            ],
            "footer": {"text": "Store quantity is shown only when the retailer exposes it publicly. Confirm pickup and seller before paying."},
        }],
        "components": [{
            "type": 1,
            "components": [{
                "type": 2,
                "style": 5,
                "label": "OPEN EXACT ITEM",
                "url": result.checkout_url,
            }],
        }],
    })


def send_report(rows: list[str], zip_codes: list[str], radius: int) -> None:
    chunks: list[str] = []
    current = ""
    for row in rows:
        candidate = current + ("\n" if current else "") + row
        if len(candidate) > 1000:
            chunks.append(current)
            current = row
        else:
            current = candidate
    if current:
        chunks.append(current)

    fields = [
        {"name": "Local search area", "value": f"ZIPs {', '.join(zip_codes)} — {radius} miles", "inline": False}
    ]
    for index, chunk in enumerate(chunks[:20], start=1):
        fields.append({"name": f"Store results {index}", "value": chunk, "inline": False})

    check_stock.post_discord({
        "username": "Pokémon MSRP Alerts",
        "embeds": [{
            "title": "Pokémon MSRP and pickup report",
            "description": "Direct item pages only. Pickup is reported only when visible on the public product page.",
            "color": 3447003,
            "fields": fields,
        }],
    })


def main() -> int:
    config = yaml.safe_load(check_stock.CONFIG_PATH.read_text(encoding="utf-8")) or {}
    settings = config.get("alert_settings", {})
    local = config.get("local_inventory", {})
    zip_codes = [str(value) for value in local.get("zip_codes", ["75043", "75217"])]
    radius = int(local.get("radius_miles", 25))
    minimum_stars = int(settings.get("minimum_quality_stars", 4))

    previous: dict[str, str] = check_stock.load_json(check_stock.STATE_PATH, {})
    current = dict(previous)
    rows: list[str] = []
    failures = 0

    for item in config.get("watches", []):
        if not item.get("enabled", True):
            continue
        key = check_stock.state_key(item)
        label = str(item.get("name", "Unknown item"))
        try:
            result = check_stock.inspect(item)
            maximum = float(item["max_price"])
            stars, explanation = quality_score(item, result)
            qualifies = (
                result.in_stock
                and result.price is not None
                and result.price <= maximum
                and stars >= minimum_stars
            )
            pickup = "pickup" in result.evidence.lower() or "pick up" in result.evidence.lower()
            price = "?" if result.price is None else f"${result.price:.2f}"
            status = "FOUND" if qualifies else "nothing found"
            pickup_text = "pickup signal" if pickup else "pickup not confirmed"
            rows.append(
                f"**{result.retailer}** — {status} — {label} — {price} — "
                f"{stars}/5 — {pickup_text}\n<{result.checkout_url}>"
            )

            signature = f"{check_stock.state_signature(result)}|{stars}|{int(qualifies)}"
            changed = previous.get(key) != signature
            if qualifies and changed:
                send_item_alert(item, result, stars, explanation, zip_codes, radius)
            current[key] = signature
        except check_stock.RetailerBlockedError:
            rows.append(f"**{check_stock.retailer_name(str(item['url']), item.get('retailer'))}** — blocked; removed from useful results until it works")
        except TimeoutError:
            rows.append(f"**{check_stock.retailer_name(str(item['url']), item.get('retailer'))}** — timed out")
        except Exception as exc:
            failures += 1
            rows.append(f"**{label}** — error: {str(exc)[:120]}")
            print(f"ERROR {label}: {exc}", file=sys.stderr)
        time.sleep(1)

    check_stock.save_json(check_stock.STATE_PATH, current)
    if os.getenv("SEND_RUN_SUMMARY", "false").lower() == "true" and check_stock.WEBHOOK_URL:
        send_report(rows, zip_codes, radius)
    return 0 if failures < max(3, len(config.get("watches", []))) else 1


if __name__ == "__main__":
    raise SystemExit(main())
