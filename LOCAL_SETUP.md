# Recommended local setup

GitHub Actions remains enabled as a backup, but large retailers often block shared cloud IP addresses. Running the checker from a home PC, mini PC, NAS, or Raspberry Pi is more reliable because requests come from a normal residential connection.

## Docker setup

1. Install Docker Desktop (Windows/Mac) or Docker Engine with Compose (Linux/Raspberry Pi).
2. Clone this repository and open its folder.
3. Copy `.env.example` to `.env`.
4. Put your current Discord webhook URL in `.env`.
5. Start the checker:

```bash
docker compose up -d --build
```

View live output:

```bash
docker compose logs -f
```

Stop it:

```bash
docker compose down
```

The container checks the Pokémon Center queue and all enabled store pages every `check_interval_minutes` from `config.yaml`. Stock and queue state are stored in a Docker volume so restarts do not repeat old alerts.

## Plain Python setup

```bash
python -m pip install -r requirements.txt
```

Set `DISCORD_WEBHOOK_URL` in your environment, then run:

```bash
python run_forever.py
```

## Buying strategy

Use alerts as a fast link—not automatic checkout. Create accounts at the retailers you care about, save shipping/payment details, stay signed in on your phone, and verify the seller and final price before purchasing. Prefer direct retailer listings and local pickup when available. Avoid marketplace listings above the configured price limit.
