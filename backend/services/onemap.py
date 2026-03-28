import httpx
import math
import os
from datetime import datetime, timezone

ONEMAP_EMAIL = os.environ.get("ONEMAP_EMAIL")
ONEMAP_PASSWORD = os.environ.get("ONEMAP_PASSWORD")

_token_cache: dict = {"token": None, "expires_at": 0.0}


async def get_token() -> str:
    """Returns a valid OneMap access token, refreshing if within 1h of expiry."""
    now = datetime.now(timezone.utc).timestamp()
    if _token_cache["token"] and _token_cache["expires_at"] > now + 3600:
        return _token_cache["token"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://www.onemap.gov.sg/api/auth/post/getToken",
            json={"email": ONEMAP_EMAIL, "password": ONEMAP_PASSWORD},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = float(data["expiry_timestamp"])
    return _token_cache["token"]


async def geocode(address: str, client: httpx.AsyncClient) -> dict | None:
    """
    Resolves a free-text Singapore address to lat/lng.
    Returns None if no results found.
    """
    resp = await client.get(
        "https://www.onemap.gov.sg/api/common/elastic/search",
        params={
            "searchVal": address,
            "returnGeom": "Y",
            "getAddrDetails": "Y",
            "pageNum": 1,
        },
        timeout=10.0,
    )
    results = resp.json().get("results", [])
    if not results:
        return None
    hit = results[0]
    return {
        "address": address,
        "resolved_address": hit.get("ADDRESS", address),
        "lat": float(hit["LATITUDE"]),
        "lng": float(hit["LONGITUDE"]),
    }


async def travel_time(
    start_lat: float,
    start_lng: float,
    end_lat: float,
    end_lng: float,
    mode: str,  # "pt" | "drive" | "walk"
    token: str,
    client: httpx.AsyncClient,
) -> int | None:
    """
    Returns travel time in whole minutes via OneMap Routing API.
    Returns None if no route is available.

    Walk calls are skipped early when straight-line distance exceeds 2.5 km
    to avoid unnecessary API quota usage.
    """
    if mode == "walk":
        dist_km = math.sqrt((end_lat - start_lat) ** 2 + (end_lng - start_lng) ** 2) * 111
        if dist_km > 2.5:
            return None

    params: dict = {
        "start": f"{start_lat},{start_lng}",
        "end": f"{end_lat},{end_lng}",
        "routeType": mode,
        "token": token,
    }

    if mode == "pt":
        params.update({
            "date": datetime.now().strftime("%Y-%m-%d"),
            "time": "10:00:00",  # fixed for consistent cross-listing comparison
            "mode": "TRANSIT",
            "numItineraries": "1",
        })

    try:
        resp = await client.get(
            "https://www.onemap.gov.sg/api/public/routingsvc/route",
            params=params,
            timeout=15.0,
        )
        data = resp.json()

        if mode == "pt":
            itineraries = data.get("plan", {}).get("itineraries", [])
            if not itineraries:
                return None
            return round(itineraries[0]["duration"] / 60)
        else:
            total_time = data.get("route_summary", {}).get("total_time")
            if total_time is None:
                return None
            return round(total_time / 60)

    except Exception:
        return None
