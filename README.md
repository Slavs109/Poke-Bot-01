# Pokémon MSRP Alerts — GitHub Actions Version

This version runs from GitHub Actions, so your laptop does not need to stay on. It checks the URLs in `config.yaml` every five minutes and sends an alert through a Discord webhook when an item changes to **in stock** at or below your configured maximum price.

## 1. Create a Discord webhook

1. In Discord, open your server and the channel for alerts.
2. Click **Edit Channel → Integrations → Webhooks → New Webhook**.
3. Name it `Pokémon MSRP Alerts` and click **Copy Webhook URL**.
4. Keep the URL private. It works like a password for posting to that channel.

## 2. Create the GitHub repository

1. Sign in to GitHub and click **New repository**.
2. Name it `pokemon-msrp-alerts`.
3. Choose **Private** if available for your account and create it.
4. Extract this ZIP.
5. On the empty repository page, click **uploading an existing file**.
6. Drag in **all files and folders**, including the hidden `.github` folder.
7. Commit the files to the `main` branch.

If your browser does not upload the `.github` folder correctly, use GitHub Desktop instead. The workflow must end up at:

`.github/workflows/stock-check.yml`

## 3. Save the Discord webhook as a GitHub secret

1. In the repository, open **Settings**.
2. Open **Secrets and variables → Actions**.
3. Click **New repository secret**.
4. Name it exactly: `DISCORD_WEBHOOK_URL`
5. Paste the Discord webhook URL as the value and save it.

Do not paste the webhook into `config.yaml`, a README, or any public file.

## 4. Enable and test the workflow

1. Open the repository's **Actions** tab.
2. Select **Pokemon MSRP Stock Check**.
3. Click **Run workflow → Run workflow**.
4. Open the run to see its logs.

After that, GitHub schedules it automatically. Scheduled runs can start later than the exact cron time during busy periods.

## 5. Add exact product links

Edit `config.yaml`. Exact product pages are far more reliable than retailer search/category pages.

```yaml
watches:
  - name: "Prismatic Evolutions Elite Trainer Box"
    url: "https://RETAILER-DIRECT-PRODUCT-URL"
    cart_url: "https://OPTIONAL-DIRECT-CART-OR-PRODUCT-URL"
    max_price: 59.99
    enabled: true
```

When `cart_url` is omitted, the Discord checkout button opens `url`.

## Important limitations

- GitHub Actions is periodic, not a continuously connected Discord bot. Discord commands such as `!check` are not available.
- GitHub-hosted IP addresses may be blocked by some retailers. A failed retailer check appears in the Actions log.
- Direct cart URLs are retailer-specific and can expire. The normal product URL is the safest fallback.
- The workflow never submits an order, bypasses queues/CAPTCHAs, or defeats purchase limits.
- Confirm the seller and final price, especially on Amazon and Walmart marketplace pages.
