from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

import requests

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
STATE_PATH = Path(os.getenv("QUEUE_STATE_PATH", "queue_state.json"))
POKEMON_CENTER_URL = "https://www.pokemoncenter.com/"
TIMEOUT = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cache-Control": "no-cache",
}

QUEUE_MARKERS = (
    "virtual queue",
    "waiting room",
    "you are now in line",
    "you’re now in line",
    "you are in line",
    "estimated wait time",
    "keep this page open",
    "do not refresh",
    "you will be redirected automatically",
    "queue-it",
)


def load_previous() -> bool:
    try:
        return bool(json.loads(STATE_PATH.read_text(encoding="utf-8")).get("active", False))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False


def save_state(active: bool, final_url: str) -> None:
    STATE_PATH.write_text(
        json.dumps({"active": active, "final_url": final_url}, indent=2),
        encoding="utf-8",
    )


def queue_is_active(response: requests.Response) -> bool:
    final_url = response.url.lower()
    host = urlparse(final_url).netloc.lower()
    text = response.text.lower()
    url_signal = any(value in final_url for value in ("queue", "waitingroom", "waiting-room"))
    queue_host = "queue-it" in host or host.startswith("queue.")
    text_signal = any(marker in text for marker in QUEUE_MARKERS)
    return url_signal or queue_host or text_signal


def send_alert(final_url: str) -> None:
    if not WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL secret is missing")
    response = requests.post(
        WEBHOOK_URL,
        json={
            "username": "Pokémon MSRP Alerts",
            "embeds": [{
                "title": "Pokémon Center virtual queue is active",
                "url": POKEMON_CENTER_URL,
                "description": (
                    "Pokémon Center appears to have started its waiting room. "
                    "Open the official site and keep your queue page open."
                ),
                "color": 16753920,
                "fields": [
                    {"name": "Detected URL", "value": final_url[:1024], "inline": False},
                    {"name": "Important", "value": "A queue does not guarantee a product launch or stock.", "inline": False},
                ],
            }],
        },
        timeout=TIMEOUT,
    )
    response.raise_for_status()


def main() -> int:
    session = requests.Session()
    session.headers.update(HEADERS)
    response = session.get(POKEMON_CENTER_URL, timeout=TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    active = queue_is_active(response)
    previous = load_previous()
    print(f"Pokemon Center queue active={active} final_url={response.url}")
    if active and not previous:
        send_alert(response.url)
        print("Queue alert sent")
    save_state(active, response.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
