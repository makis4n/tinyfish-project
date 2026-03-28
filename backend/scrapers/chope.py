"""
Chope Singapore scraper.

Flow:
  1. Scrape restaurant listing pages → get venue cards
  2. Geocode addresses via OneMap
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

# Covers all major regions of Singapore for geographic spread
AREAS = [
    ("orchard",         "Orchard / Somerset"),
    ("clarke-quay",     "Clarke Quay / River Valley"),
    ("chinatown",       "Chinatown / Tanjong Pagar"),
    ("bugis",           "Bugis / Bras Basah"),
    ("east-coast",      "East Coast / Katong"),
    ("jurong",          "Jurong / West Singapore"),
    ("woodlands",       "Woodlands / North Singapore"),
    ("serangoon",       "Serangoon / North East"),
    ("harbourfront",    "Harbourfront / Sentosa"),
    ("novena",          "Novena / Thomson"),
]

LISTING_GOAL = """
Extract every restaurant card visible on this page.
Return a JSON object with a single key "restaurants" — an array where each item has:
  - name         (string)
  - cuisine      (string, e.g. "Japanese", "Chinese", "Café")
  - neighbourhood (string, area or district, e.g. "Orchard", "CBD")
  - address      (string, full address if shown, otherwise empty string)
  - price_range  (string, e.g. "$", "$$", "$$$" or a dollar amount range)
  - image_url    (string, full URL or empty string)
  - detail_url   (string, full URL to the restaurant page)
Return ONLY the JSON object. No markdown, no explanation.
"""


# ── Parsing helpers ──────────────────────────────────────────

def _parse_price(price_range: str) -> tuple[int | None, int | None]:
    """
    Converts price indicators to SGD cent ranges.
    $ ≈ under $15, $$ ≈ $15–40, $$$ ≈ $40–80, $$$$ ≈ $80+
    """
    if not price_range:
        return None, None
    symbols = price_range.count("$")
    mapping = {1: (0, 1500), 2: (1500, 4000), 3: (4000, 8000), 4: (8000, None)}
    return mapping.get(symbols, (None, None))


def _extract_json(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        cleaned = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.MULTILINE)
        return json.loads(cleaned)
    raise ValueError(f"Unexpected result type: {type(raw)}")


def _infer_tags(cuisine: str, neighbourhood: str) -> list[str]:
    tags = []
    if cuisine:
        tags.append(cuisine.lower().replace(" ", "-"))
    if neighbourhood:
        tags.append(neighbourhood.lower().replace(" ", "-"))
    tags.append("restaurant")
    return tags


# ── Scrape steps ─────────────────────────────────────────────

async def _scrape_area(area_slug: str, area_label: str) -> list[dict]:
    url = f"https://chope.co/sg/restaurants?location={area_slug}"
    log.info(f"Scraping Chope area '{area_label}': {url}")
    try:
        result = await tinyfish.run_automation(url, LISTING_GOAL, browser_profile="stealth")
        data = _extract_json(result)
        restaurants = data.get("restaurants", [])
        # Tag each restaurant with its area so geocoding has a fallback
        for r in restaurants:
            if not r.get("neighbourhood"):
                r["neighbourhood"] = area_label
        log.info(f"  → got {len(restaurants)} restaurants from '{area_label}'")
        return restaurants
    except Exception as exc:
        log.warning(f"  → failed to scrape '{area_label}': {exc}")
        return []


async def _geocode_all(restaurants: list[dict]) -> list[tuple[float | None, float | None]]:
    """Geocodes sequentially with delay to respect OneMap rate limits."""
    import httpx
    results = []
    async with httpx.AsyncClient() as client:
        for r in restaurants:
            address = r.get("address") or f"{r.get('neighbourhood', '')}, Singapore"
            geo = await onemap.geocode(address, client)
            results.append((geo["lat"], geo["lng"]) if geo else (None, None))
            await asyncio.sleep(0.5)
    return results


# ── Normalisation ─────────────────────────────────────────────

def _build_row(restaurant: dict, lat: float | None, lng: float | None) -> dict:
    price_min, price_max = _parse_price(restaurant.get("price_range", ""))
    detail_url = restaurant.get("detail_url", "")
    source_id = detail_url.rstrip("/").split("/")[-1] or restaurant["name"].lower().replace(" ", "-")

    return {
        "source": "chope",
        "source_id": source_id,
        "source_url": detail_url or None,
        "type": "food",
        "tags": _infer_tags(restaurant.get("cuisine", ""), restaurant.get("neighbourhood", "")),
        "name": restaurant.get("name", "").strip(),
        "description": None,
        "image_url": restaurant.get("image_url") or None,
        "address": restaurant.get("address") or restaurant.get("neighbourhood"),
        "postal_code": None,
        "lat": lat,
        "lng": lng,
        "price_min": price_min,
        "price_max": price_max,
        "starts_at": None,
        "ends_at": None,
        "raw_data": restaurant,
    }


# ── Main entry point ──────────────────────────────────────────

async def run() -> dict:
    log.info(f"Starting Chope scrape across {len(AREAS)} areas")

    all_restaurants: list[dict] = []
    for area_slug, area_label in AREAS:
        restaurants = await _scrape_area(area_slug, area_label)
        all_restaurants.extend(restaurants)
        log.info(f"Running total: {len(all_restaurants)} restaurants")

    if not all_restaurants:
        return {"status": "error", "message": "No restaurants scraped"}

    coords = await _geocode_all(all_restaurants)

    rows = [
        _build_row(r, lat, lng)
        for r, (lat, lng) in zip(all_restaurants, coords)
        if r.get("name")
    ]

    # Deduplicate by source_id — keep last occurrence
    rows = list({r["source_id"]: r for r in rows}.values())

    if rows:
        supabase.table("listings").upsert(rows, on_conflict="source,source_id").execute()
        log.info(f"Upserted {len(rows)} rows")

        supabase.table("scrape_runs").insert({
            "source": "chope",
            "status": "success",
            "rows_upserted": len(rows),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }).execute()

    return {"status": "success", "rows_upserted": len(rows)}
