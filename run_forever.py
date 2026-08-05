from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config.yaml"))


def interval_seconds() -> int:
    try:
        config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        minutes = int(config.get("check_interval_minutes", 5))
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        minutes = 5
    return max(1, minutes) * 60


def run_script(path: str) -> int:
    completed = subprocess.run([sys.executable, path], check=False)
    return completed.returncode


def main() -> int:
    if not os.getenv("DISCORD_WEBHOOK_URL", "").strip():
        print("DISCORD_WEBHOOK_URL is missing", file=sys.stderr)
        return 2

    print("Pokemon stock checker local mode started")
    while True:
        started = datetime.now(timezone.utc).isoformat()
        print(f"\n[{started}] Starting queue and stock checks")

        queue_code = run_script("queue_monitor.py")
        stock_code = run_script("check_stock.py")
        print(f"Queue exit={queue_code}; stock exit={stock_code}")

        delay = interval_seconds()
        print(f"Next check in {delay // 60} minute(s)")
        time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
