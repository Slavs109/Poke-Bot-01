"""Send advertised men's apparel deal alerts via a configured Discord webhook."""
import os
import json
import hashlib
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from deal_sites import scan

STATE = Path("deal_alert_state.json")
def notify(result, webhook, minimum=70, state=STATE):
    parsed = urlparse(webhook)
    if parsed.scheme != "https" or parsed.hostname != "discord.com" or not parsed.path.startswith("/api/webhooks/"):
        raise ValueError("Expected a Discord HTTPS webhook")
    seen = json.loads(state.read_text()) if state.exists() else {}
    count = 0
    for deal in result["deals"]:
        discount = deal["advertised_max_discount"]
        key = hashlib.sha256(deal["url"].encode()).hexdigest()
        if discount < minimum or seen.get(key, 0) >= discount:
            continue
        title = deal["title"][:300]
        payload = {"content": f"**{discount}% advertised discount — {deal['source']}**\n{title}\n{deal['url']}\nPrice and stock not independently verified.", "allowed_mentions":{"parse":[]}}
        req = urllib.request.Request(webhook, data=json.dumps(payload).encode(), headers={"Content-Type":"application/json","User-Agent":"DealHunter/0.1"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as response:
            if not 200 <= response.status < 300:
                raise RuntimeError("Notification rejected")
        seen[key] = discount
        state.write_text(json.dumps(seen))
        count += 1
    return count

if __name__ == "__main__":
    result = scan()
    if not result["successful_sources"]:
        raise SystemExit("No live feeds available")
    webhook = os.environ.get("DEAL_DISCORD_WEBHOOK")
    if not webhook:
        raise SystemExit("Configure DEAL_DISCORD_WEBHOOK first")
    print("Alerts sent:", notify(result, webhook, int(os.environ.get("DEAL_ALERT_MINIMUM", "70"))))
