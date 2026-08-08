from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "ebay_config.yaml"))
STATE_PATH = Path(os.getenv("EBAY_STATE_PATH", "ebay_state.json"))
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID", "").strip()
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "").strip()
TIMEOUT = int(os.getenv("PAGE_TIMEOUT_SECONDS", "25"))
FORCE_REPORT = os.getenv("FORCE_REPORT", "false").lower() == "true"

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
MARKETPLACE_ID = "EBAY_US"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

NOISE = {
    "pokemon", "pokémon", "tcg", "card", "cards", "english", "eng", "mint", "near", "nm",
    "new", "authentic", "official", "rare", "holo", "foil", "shipping", "free", "the", "a", "an",
    "2024", "2025", "2026", "usa", "us", "🔥", "⭐",
}

BAD_TERMS = (
    "digital", "proxy", "custom", "fan made", "fanmade", "replica", "orica", "reprint",
    "mystery", "random card", "you choose", "pick your card", "code card", "online code",
    "empty box", "empty tin", "empty pack", "wrapper only", "art only", "photo only",
    "read description", "damaged lot", "bulk lot", "100 cards", "50 cards", "25 cards",
)

SEALED_TERMS = (
    "booster box", "elite trainer box", " etb", "collection box", "booster bundle", "tin",
    "pokemon center elite trainer box", "pokemon center etb",
)

GRADE_RE = re.compile(r"\b(PSA|CGC|BGS|SGC)\s*(10|9\.5|9|8\.5|8|7\.5|7|6|5|4|3|2|1)\b", re.I)
CARD_NUM_RE = re.compile(r"\b(\d{1,3})\s*/\s*(\d{1,3})\b")


@dataclass
class Listing:
    item_id: str
    title: str
    url: str
    price: float
    shipping: float
    image: str = ""
    condition: str = ""
    seller_feedback_pct: float = 0.0
    seller_feedback_score: int = 0

    @property
    def total(self) -> float:
        return self.price + self.shipping


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def oauth_token(session: requests.Session) -> str:
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        raise RuntimeError(
            "Missing EBAY_CLIENT_ID / EBAY_CLIENT_SECRET GitHub secrets. "
            "Create free Production application keys in the eBay Developers Program."
        )
    raw = f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode("utf-8")
    auth = base64.b64encode(raw).decode("ascii")
    response = session.post(
        TOKEN_URL,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": OAUTH_SCOPE},
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"eBay OAuth failed: HTTP {response.status_code}: {response.text[:300]}")
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError("eBay OAuth response did not contain access_token")
    return str(token)


def money(value: Any) -> float:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def listing_from_item(item: dict[str, Any]) -> Listing | None:
    title = str(item.get("title", "")).strip()
    item_id = str(item.get("itemId", "")).strip()
    url = str(item.get("itemWebUrl", "")).strip()
    price = money(item.get("price"))
    if not title or not item_id or not url or price <= 0:
        return None

    shipping = 0.0
    options = item.get("shippingOptions") or []
    costs = [money(opt.get("shippingCost")) for opt in options if isinstance(opt, dict)]
    costs = [c for c in costs if c >= 0]
    if costs:
        shipping = min(costs)

    image = ""
    if isinstance(item.get("image"), dict):
        image = str(item["image"].get("imageUrl", ""))

    seller = item.get("seller") or {}
    try:
        feedback_pct = float(seller.get("feedbackPercentage") or 0)
    except (TypeError, ValueError):
        feedback_pct = 0.0
    try:
        feedback_score = int(seller.get("feedbackScore") or 0)
    except (TypeError, ValueError):
        feedback_score = 0

    return Listing(
        item_id=item_id,
        title=title,
        url=url,
        price=price,
        shipping=shipping,
        image=image,
        condition=str(item.get("condition", "")),
        seller_feedback_pct=feedback_pct,
        seller_feedback_score=feedback_score,
    )


def browse_search(
    session: requests.Session,
    token: str,
    query: str,
    limit: int,
    newest_first: bool,
) -> list[Listing]:
    params = {
        "q": query,
        "limit": str(max(1, min(limit, 200))),
        "filter": "buyingOptions:{FIXED_PRICE}",
    }
    if newest_first:
        params["sort"] = "newlyListed"
    response = session.get(
        BROWSE_URL,
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE_ID,
            "Accept": "application/json",
        },
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"eBay Browse API HTTP {response.status_code}: {response.text[:300]}")
    payload = response.json()
    results: list[Listing] = []
    for item in payload.get("itemSummaries", []) or []:
        if not isinstance(item, dict):
            continue
        listing = listing_from_item(item)
        if listing:
            results.append(listing)
    return results


def plausible(title: str) -> bool:
    low = title.lower()
    if "pokemon" not in low and "pokémon" not in low:
        return False
    return not any(term in low for term in BAD_TERMS)


def tokens(title: str) -> list[str]:
    text = title.lower().replace("pokémon", "pokemon")
    text = re.sub(r"[^a-z0-9/#.+-]+", " ", text)
    out: list[str] = []
    for token in text.split():
        token = token.strip("-+.")
        if not token or token in NOISE:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        if token not in out:
            out.append(token)
    return out


def similarity(a: str, b: str) -> float:
    aa, bb = set(tokens(a)), set(tokens(b))
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def grade_key(title: str) -> str:
    m = GRADE_RE.search(title)
    return f"{m.group(1).upper()} {m.group(2)}" if m else "RAW"


def card_number(title: str) -> str:
    m = CARD_NUM_RE.search(title)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def sealed_kind(title: str) -> str:
    low = title.lower()
    for term in SEALED_TERMS:
        if term.strip() in low:
            return term.strip()
    return ""


def same_product_type(candidate: Listing, comp: Listing) -> bool:
    cg = grade_key(candidate.title)
    og = grade_key(comp.title)
    if cg != og:
        return False

    cn = card_number(candidate.title)
    on = card_number(comp.title)
    if cn and on and cn != on:
        return False
    if cn and not on:
        return False

    cs = sealed_kind(candidate.title)
    os_ = sealed_kind(comp.title)
    if bool(cs) != bool(os_):
        return False
    if cs and os_ and cs != os_:
        return False
    return True


def comp_query(title: str, max_tokens: int) -> str:
    selected = tokens(title)[:max_tokens]
    grade = grade_key(title)
    number = card_number(title)
    sealed = sealed_kind(title)

    parts = ["pokemon"] + selected
    if number and number not in parts:
        parts.append(number)
    if grade != "RAW":
        parts.extend(grade.split())
    if sealed:
        parts.extend(sealed.split())

    deduped: list[str] = []
    for part in parts:
        if part and part not in deduped:
            deduped.append(part)
    return " ".join(deduped[: max_tokens + 5])


def comps_for(
    session: requests.Session,
    token: str,
    candidate: Listing,
    cfg: dict[str, Any],
) -> tuple[str, list[Listing]]:
    query = comp_query(candidate.title, int(cfg.get("comp_query_tokens", 8)))
    raw = browse_search(
        session,
        token,
        query,
        int(cfg.get("comparable_results", 60)),
        newest_first=False,
    )
    min_sim = float(cfg.get("minimum_title_similarity", 0.38))
    comps: list[Listing] = []
    for item in raw:
        if item.item_id == candidate.item_id or not plausible(item.title):
            continue
        if not same_product_type(candidate, item):
            continue
        if similarity(candidate.title, item.title) < min_sim:
            continue
        comps.append(item)
    return query, comps


def robust_market(values: list[float]) -> tuple[float | None, float | None]:
    values = sorted(v for v in values if v > 0)
    if not values:
        return None, None
    if len(values) >= 7:
        trim = max(1, int(len(values) * 0.15))
        if len(values) > trim * 2:
            values = values[trim:-trim]
    median = float(statistics.median(values))
    low_quartile = float(values[max(0, int((len(values) - 1) * 0.25))])
    return median, low_quartile


def deal_score(candidate: Listing, median: float, low_quartile: float, comp_count: int) -> int:
    discount = max(0.0, 1.0 - candidate.total / median)
    score = min(70, int(discount * 140))
    score += min(15, max(0, comp_count - 4) * 2)
    if candidate.total < low_quartile:
        score += 5
    if candidate.seller_feedback_pct >= 99.0 and candidate.seller_feedback_score >= 50:
        score += 10
    elif candidate.seller_feedback_pct >= 97.0 and candidate.seller_feedback_score >= 10:
        score += 5
    return min(100, score)


def state_key(item_id: str) -> str:
    return hashlib.sha256(item_id.encode("utf-8")).hexdigest()


def post_discord(payload: dict[str, Any]) -> None:
    if not WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL missing; skipping Discord message", file=sys.stderr)
        return
    response = requests.post(WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    response.raise_for_status()


def send_deal(
    listing: Listing,
    median: float,
    low_quartile: float,
    comps: int,
    discount: float,
    score: int,
    query: str,
) -> None:
    savings = median - listing.total
    tier = "🚨 NUCLEAR DEAL" if discount >= 0.50 else "🔥 STRONG DEAL"
    seller = (
        f"{listing.seller_feedback_pct:.1f}% ({listing.seller_feedback_score} feedback)"
        if listing.seller_feedback_pct
        else "Not reported"
    )
    fields = [
        {"name": "Total price", "value": f"${listing.total:.2f}", "inline": True},
        {"name": "Market median", "value": f"${median:.2f}", "inline": True},
        {"name": "Low quartile", "value": f"${low_quartile:.2f}", "inline": True},
        {"name": "Below market", "value": f"{discount:.0%}", "inline": True},
        {"name": "Potential savings", "value": f"${savings:.2f}", "inline": True},
        {"name": "Deal score", "value": f"{score}/100", "inline": True},
        {"name": "Comparable listings", "value": str(comps), "inline": True},
        {"name": "Seller", "value": seller, "inline": True},
        {"name": "Condition", "value": listing.condition or "Not reported", "inline": True},
        {"name": "Comp query", "value": query[:1024], "inline": False},
    ]
    embed: dict[str, Any] = {
        "title": f"{tier}: {listing.title}"[:256],
        "url": listing.url,
        "description": (
            "New Buy It Now listing priced far below similar live eBay listings. "
            "Check photos, authenticity, condition, and seller before buying."
        ),
        "color": 5763719,
        "fields": fields,
    }
    if listing.image.startswith("http"):
        embed["thumbnail"] = {"url": listing.image}
    post_discord({"username": "Pokémon eBay Deal Hunter", "embeds": [embed]})


def send_summary(scanned: int, comped: int, deals: int, errors: list[str]) -> None:
    if not FORCE_REPORT:
        return
    post_discord({
        "username": "Pokémon eBay Deal Hunter",
        "embeds": [{
            "title": "eBay API deal scan completed",
            "color": 3447003 if not errors else 16753920,
            "fields": [
                {"name": "New listings scanned", "value": str(scanned), "inline": True},
                {"name": "Listings comped", "value": str(comped), "inline": True},
                {"name": "Strong deals", "value": str(deals), "inline": True},
                {"name": "Errors", "value": str(len(errors)), "inline": True},
                {"name": "Details", "value": ("\n".join(errors[:5]) or "None")[:1024], "inline": False},
            ],
        }],
    })


def main() -> int:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    cfg = config.get("ebay_deals", {})
    if not cfg.get("enabled", True):
        print("eBay deal finder disabled")
        return 0

    errors: list[str] = []
    session = requests.Session()
    try:
        token = oauth_token(session)
    except Exception as exc:
        message = str(exc)
        errors.append(message)
        send_summary(0, 0, 0, errors)
        print(message, file=sys.stderr)
        return 1

    queries = [str(x) for x in cfg.get("search_queries", ["pokemon card"])]
    active_limit = int(cfg.get("active_results_per_query", 35))
    comp_limit = int(cfg.get("max_comp_checks_per_run", 30))
    min_comps = int(cfg.get("minimum_comparable_listings", 5))
    min_market = float(cfg.get("minimum_market_median", 20.0))
    min_discount = float(cfg.get("minimum_discount_fraction", 0.35))
    min_savings = float(cfg.get("minimum_absolute_savings", 20.0))
    min_score = int(cfg.get("minimum_deal_score", 65))
    max_total = float(cfg.get("maximum_listing_total", 750.0))
    delay = float(cfg.get("request_delay_seconds", 0.25))

    previous: dict[str, Any] = load_json(STATE_PATH, {})
    current = dict(previous)
    seen_ids: set[str] = set()
    active: list[Listing] = []

    for query in queries:
        try:
            for item in browse_search(session, token, query, active_limit, newest_first=True):
                if item.item_id in seen_ids or not plausible(item.title):
                    continue
                if item.total <= 0 or item.total > max_total:
                    continue
                seen_ids.add(item.item_id)
                active.append(item)
        except Exception as exc:
            errors.append(f"search '{query}': {exc}")
        time.sleep(delay)

    # Cheapest first so the limited comp budget is spent on likely bargains.
    active.sort(key=lambda x: x.total)
    comped = deals = 0

    for item in active:
        if comped >= comp_limit:
            break
        try:
            query, comps = comps_for(session, token, item, cfg)
            comped += 1
            totals = [x.total for x in comps]
            median, low_quartile = robust_market(totals)
            if median is None or low_quartile is None or len(totals) < min_comps or median < min_market:
                continue
            discount = 1.0 - item.total / median
            savings = median - item.total
            score = deal_score(item, median, low_quartile, len(totals))
            qualifies = discount >= min_discount and savings >= min_savings and score >= min_score
            key = state_key(item.item_id)
            signature = {
                "total": round(item.total, 2),
                "market": round(median, 2),
                "discount": round(discount, 4),
                "score": score,
            }
            if qualifies:
                deals += 1
                if previous.get(key) != signature:
                    send_deal(item, median, low_quartile, len(totals), discount, score, query)
                    print(
                        f"DEAL score={score} discount={discount:.0%} "
                        f"${item.total:.2f} vs ${median:.2f} | {item.title}"
                    )
            current[key] = signature
        except Exception as exc:
            errors.append(f"comp '{item.title[:55]}': {exc}")
        time.sleep(delay)

    save_json(STATE_PATH, current)
    send_summary(len(active), comped, deals, errors)
    print(f"eBay API scan: active={len(active)} comped={comped} deals={deals} errors={len(errors)}")
    return 0 if not errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
