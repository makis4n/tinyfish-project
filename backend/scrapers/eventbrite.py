"""
Eventbrite Singapore scraper.

Flow:
  1. Scrape event listing pages → get event cards
  2. Geocode venue addresses via OneMap
  3. Upsert normalised rows into Supabase listings table
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from database import supabase
from services import onemap, tinyfish

log = logging.getLogger(__name__)

MAX_PAGES = 3

LISTING_GOAL = """
Extract every event card visible on this page.
Return a JSON object with a single key "events" — an array where each item has:
  - title       (string)
  - date_text   (string, e.g. "Sat, 4 Apr" or "4 Apr 2026 · 7:00 PM")
  - venue_name  (string, name of venue or "Online" if virtual)
  - price_text  (string, e.g. "Free", "From $20", "$10 – $50")
  - image_url   (string, full URL or empty string)
  - detail_url  (string, full URL to the event page)
Return ONLY the JSON object. No markdown, no explanation.
"""

DETAIL_GOAL = """
Extract the following from this Eventbrite event page.
Return a JSON object with:
  - venue_address (string, full Singapore address including postal code if shown)
  - description   (string, 2–3 sentence summary of the event)
  - tags          (array of strings, event category tags if shown, e.g. ["music", "festival"])
Return ONLY the JSON object. No markdown, no explanation.
"""


# ── Parsing helpers ──────────────────────────────────────────

def _parse_price(price_text: str) -> tuple[int | None, int | None]:
    if not price_text:
        return None, None
    if re.search(r'\bfree\b', price_text, re.IGNORECASE):
        return 0, 0
    amounts = [round(float(p) * 100) for p in re.findall(r'\d+(?:\.\d+)?', price_text)]
    if not amounts:
        return None, None
    return amounts[0], amounts[-1]


def _parse_date(date_text: str) -> datetime | None:
    if not date_text:
        return None
    for fmt in ("%d %b %Y", "%a, %d %b %Y", "%a, %d %b", "%d %b"):
        try:
            dt = datetime.strptime(date_text.split("·")[0].strip(), fmt)
            if dt.year == 1900:
                dt = dt.replace(year=datetime.now().year)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _extract_json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE)
        return json.loads(cleaned)
    raise ValueError(f"Unexpected result type: {type(raw)}")


# ── Scrape steps ─────────────────────────────────────────────

async def _scrape_listing_page(page: int) -> list[dict]:
    url = f"https://www.eventbrite.sg/d/singapore--singapore/events/?page={page}"
    log.info(f"Scraping Eventbrite page {page}: {url}")
    result = await tinyfish.run_automation(url, LISTING_GOAL, browser_profile="stealth")
    data = _extract_json(result)
    return data.get("events", [])


async def _scrape_detail(detail_url: str, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            result = await tinyfish.run_automation(detail_url, DETAIL_GOAL, browser_profile="stealth")
            return _extract_json(result)
        except Exception as exc:
            log.warning(f"Detail scrape failed for {detail_url}: {exc}")
            return {}


async def _geocode_all(items: list[dict]) -> list[tuple[float | None, float | None]]:
    import httpx
    results = []
    async with httpx.AsyncClient() as client:
        for item in items:
            address = item.get("venue_address") or item.get("venue_name") or ""
            if address.lower() == "online":
                results.append((None, None))
                continue
            geo = await onemap.geocode(address, client)
            results.append((geo["lat"], geo["lng"]) if geo else (None, None))
            await asyncio.sleep(0.5)
    return results


# ── Normalisation ─────────────────────────────────────────────

def _build_row(card: dict, detail: dict, lat: float | None, lng: float | None) -> dict:
    price_min, price_max = _parse_price(card.get("price_text", ""))
    starts_at = _parse_date(card.get("date_text", ""))
    detail_url = card.get("detail_url", "")
    source_id = detail_url.rstrip("/").split("/")[-1] or re.sub(r'\W+', '-', card.get("title", "").lower())
    tags = ["event"] + [t.lower() for t in detail.get("tags", [])]

    return {
        "source": "eventbrite",
        "source_id": source_id,
        "source_url": detail_url or None,
        "type": "event",
        "tags": list(dict.fromkeys(tags)),  # dedupe while preserving order
        "name": card.get("title", "").strip(),
        "description": detail.get("description"),
        "image_url": card.get("image_url") or None,
        "address": detail.get("venue_address") or card.get("venue_name"),
        "postal_code": None,
        "lat": lat,
        "lng": lng,
        "price_min": price_min,
        "price_max": price_max,
        "starts_at": starts_at.isoformat() if starts_at else None,
        "ends_at": None,
        "raw_data": {**card, **detail},
    }


# ── Main entry point ──────────────────────────────────────────

async def run() -> dict:
    log.info("Starting Eventbrite scrape")

    all_cards: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            cards = await _scrape_listing_page(page)
            if not cards:
                log.info(f"Page {page} returned no events — stopping")
                break
            all_cards.extend(cards)
            log.info(f"Page {page}: got {len(cards)} events (total {len(all_cards)})")
        except Exception as exc:
            log.error(f"Failed to scrape Eventbrite page {page}: {exc}")
            break

    if not all_cards:
        return {"status": "error", "message": "No events scraped"}

    sem = asyncio.Semaphore(5)
    details = await asyncio.gather(*[
        _scrape_detail(card["detail_url"], sem)
        for card in all_cards if card.get("detail_url")
    ])

    merged = [
        {**card, **detail}
        for card, detail in zip(all_cards, details)
    ]

    coords = await _geocode_all(merged)

    rows = [
        _build_row(card, detail, lat, lng)
        for (card, detail), (lat, lng) in zip(zip(all_cards, details), coords)
        if card.get("title")
    ]
    rows = list({r["source_id"]: r for r in rows}.values())

    if rows:
        supabase.table("listings").upsert(rows, on_conflict="source,source_id").execute()
        log.info(f"Upserted {len(rows)} rows")
        supabase.table("scrape_runs").insert({
            "source": "eventbrite",
            "status": "success",
            "rows_upserted": len(rows),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    return {"status": "success", "rows_upserted": len(rows)}
