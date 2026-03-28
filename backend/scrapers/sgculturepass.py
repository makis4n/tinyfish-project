"""
SG Culture Pass scraper.

Flow:
  1. Scrape listing pages 1–MAX_PAGES → get event cards (title, dates, price, image, detail_url)
  2. Scrape each detail page → get venue name + address
  3. Geocode venue addresses via OneMap
  4. Upsert normalised rows into Supabase listings table
"""

import asyncio
import httpx
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from database import supabase
from services import onemap, tinyfish

log = logging.getLogger(__name__)

MAX_PAGES = 3          # ~15 events per page → up to ~45 events
DETAIL_CONCURRENCY = 5  # parallel detail-page scrapes

# ── TinyFish goals ───────────────────────────────────────────

LISTING_GOAL = """
Extract every event card visible on this page.
Return a JSON object with a single key "events" — an array where each item has:
  - title       (string)
  - date_text   (string, e.g. "27 Mar 2026 – 28 Mar 2026")
  - price_text  (string, e.g. "From $38" or "Free")
  - image_url   (string) — the real image URL for the event card photo. Look in the
                img element's srcset attribute and pick the largest URL (highest width
                descriptor). If srcset is absent, use src only if it starts with
                "https://" (ignore base64 data URIs). Use empty string if none found.
  - detail_url  (string, full URL to the event detail page)
Return ONLY the JSON object. No markdown, no explanation.
"""

DETAIL_GOAL = """
Extract the following from this event detail page.
Return a JSON object with:
  - venue_name    (string, name of the venue building or space)
  - venue_address (string, full Singapore address including postal code if shown)
  - description   (string, 2–3 sentence summary of the event)
  - image_url     (string) — the main hero/banner image for this event. Look in the
                  img element's srcset attribute and pick the largest URL (highest width
                  descriptor). If srcset is absent, use src only if it starts with
                  "https://". Use empty string if none found.
Return ONLY the JSON object. No markdown, no explanation.
"""


# ── Parsing helpers ──────────────────────────────────────────

def _parse_price(price_text: str) -> tuple[int | None, int | None]:
    """Returns (price_min, price_max) in SGD cents."""
    if not price_text:
        return None, None
    if re.search(r'\bfree\b', price_text, re.IGNORECASE):
        return 0, 0
    amounts = [round(float(p) * 100) for p in re.findall(r'\d+(?:\.\d+)?', price_text)]
    if not amounts:
        return None, None
    return amounts[0], amounts[-1]


def _parse_dates(date_text: str) -> tuple[datetime | None, datetime | None]:
    """
    Parses date ranges like:
      "27 Mar 2026 – 28 Mar 2026"
      "27 – 28 Mar 2026"
    Returns (starts_at, ends_at) as UTC-aware datetimes, or (None, None).
    """
    if not date_text:
        return None, None

    text = date_text.replace('–', '-').replace('—', '-')
    parts = [p.strip() for p in text.split('-', 1)]

    def to_dt(s: str) -> datetime | None:
        for fmt in ("%d %b %Y", "%d %B %Y"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    if len(parts) == 2:
        end = to_dt(parts[1])
        start = to_dt(parts[0])
        if start and end:
            return start, end
        # "27 – 28 Mar 2026": borrow month/year from end
        if end and not start:
            month_year = " ".join(parts[1].split()[-2:])
            start = to_dt(f"{parts[0]} {month_year}")
            return start, end

    return None, None


def _clean_image_url(url: str) -> str | None:
    """Reject base64 placeholders and relative paths; return None if unusable."""
    if not url:
        return None
    if url.startswith("data:"):
        return None
    if url.startswith("http"):
        return url
    return None


def _extract_json(raw: Any) -> dict:
    """
    TinyFish returns results as either a dict or a string.
    Handles both and returns the parsed dict.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        # Strip markdown fences if present
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE)
        return json.loads(cleaned)
    raise ValueError(f"Unexpected result type: {type(raw)}")


# ── Scrape steps ─────────────────────────────────────────────

async def _scrape_listing_page(page: int) -> list[dict]:
    url = f"https://www.sgculturepass.gov.sg/events?page={page}"
    log.info(f"Scraping listing page {page}: {url}")
    result = await tinyfish.run_automation(url, LISTING_GOAL, browser_profile="stealth")
    data = _extract_json(result)
    return data.get("events", [])


async def _scrape_detail_page(detail_url: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        log.info(f"Scraping detail: {detail_url}")
        try:
            result = await tinyfish.run_automation(detail_url, DETAIL_GOAL, browser_profile="stealth")
            return _extract_json(result)
        except Exception as exc:
            log.warning(f"Detail scrape failed for {detail_url}: {exc}")
            return {}


async def _geocode_all(addresses: list[str]) -> list[tuple[float | None, float | None]]:
    """
    Geocodes addresses sequentially with a 0.5s delay between calls
    to avoid hitting OneMap's rate limit.
    """
    results = []
    async with httpx.AsyncClient() as client:
        for address in addresses:
            if not address:
                results.append((None, None))
                continue
            geo = await onemap.geocode(address, client)
            results.append((geo["lat"], geo["lng"]) if geo else (None, None))
            await asyncio.sleep(0.5)
    return results


# ── Normalisation ─────────────────────────────────────────────

def _build_row(card: dict, detail: dict, lat: float | None, lng: float | None) -> dict:
    price_min, price_max = _parse_price(card.get("price_text", ""))
    starts_at, ends_at = _parse_dates(card.get("date_text", ""))

    return {
        "source": "sgculturepass",
        "source_id": card["detail_url"].rstrip("/").split("/")[-1],
        "source_url": card.get("detail_url"),
        "type": "event",
        "tags": ["culture", "singapore"],
        "name": card.get("title", "").strip(),
        "description": detail.get("description"),
        "image_url": _clean_image_url(detail.get("image_url", "")) or _clean_image_url(card.get("image_url", "")),
        "address": detail.get("venue_address") or detail.get("venue_name"),
        "postal_code": None,
        "lat": lat,
        "lng": lng,
        "price_min": price_min,
        "price_max": price_max,
        "starts_at": starts_at.isoformat() if starts_at else None,
        "ends_at": ends_at.isoformat() if ends_at else None,
        "raw_data": {**card, **detail},
    }


# ── Main entry point ──────────────────────────────────────────

async def run(limit: int | None = None) -> dict:
    """
    Scrapes SG Culture Pass and upserts results into Supabase.
    limit: max number of events to process (None = scrape all pages).
    Returns a summary dict suitable for the ingest endpoint response.
    """
    log.info(f"Starting SG Culture Pass scrape (limit={limit})")

    # 1. Scrape listing pages sequentially (they're JS-rendered, keep it polite)
    all_cards: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            cards = await _scrape_listing_page(page)
            if not cards:
                log.info(f"Page {page} returned no events — stopping")
                break
            all_cards.extend(cards)
            log.info(f"Page {page}: got {len(cards)} cards (total {len(all_cards)})")
        except Exception as exc:
            log.error(f"Failed to scrape listing page {page}: {exc}")
            break
        if limit and len(all_cards) >= limit:
            break

    if limit:
        all_cards = all_cards[:limit]

    if not all_cards:
        return {"status": "error", "message": "No events scraped from listing pages"}

    # 2. Scrape detail pages with limited concurrency
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    details = await asyncio.gather(*[
        _scrape_detail_page(card["detail_url"], sem)
        for card in all_cards
        if card.get("detail_url")
    ])

    # 3. Geocode venue addresses sequentially (rate-limit safe)
    coords = await _geocode_all([d.get("venue_address", "") for d in details])

    # 4. Build and upsert rows
    rows = [
        _build_row(card, detail, lat, lng)
        for card, detail, (lat, lng) in zip(all_cards, details, coords)
        if card.get("title")  # skip malformed cards
    ]

    if rows:
        supabase.table("listings").upsert(rows, on_conflict="source,source_id").execute()
        log.info(f"Upserted {len(rows)} rows")

        # Record scrape run
        supabase.table("scrape_runs").insert({
            "source": "sgculturepass",
            "status": "success",
            "rows_upserted": len(rows),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    return {"status": "success", "rows_upserted": len(rows)}
