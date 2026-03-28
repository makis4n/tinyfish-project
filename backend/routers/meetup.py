import asyncio
import httpx
from fastapi import APIRouter, HTTPException
from models import MeetupRequest
from database import supabase
from services import onemap

router = APIRouter()

MODES = ["pt", "drive", "walk"]


@router.post("/meetup")
async def find_meetup(req: MeetupRequest):
    if len(req.addresses) < 2:
        raise HTTPException(status_code=400, detail="Provide at least 2 addresses.")

    token = await onemap.get_token()

    # Geocode all addresses concurrently
    async with httpx.AsyncClient() as client:
        geocoded = await asyncio.gather(
            *[onemap.geocode(addr, client) for addr in req.addresses]
        )

    failed = [req.addresses[i] for i, g in enumerate(geocoded) if g is None]
    if failed:
        raise HTTPException(
            status_code=422,
            detail=f"Could not geocode: {', '.join(failed)}. "
                   "Try adding 'Singapore' or use a postal code.",
        )

    centroid_lat, centroid_lng = _centroid(geocoded)
    candidates = _fetch_candidates(centroid_lat, centroid_lng, req)

    if not candidates:
        return {
            "centroid": {"lat": centroid_lat, "lng": centroid_lng},
            "friends": list(geocoded),
            "results": [],
        }

    commutes = await _fetch_all_commutes(candidates, geocoded, token)
    results = _build_results(candidates, commutes, len(geocoded))

    return {
        "centroid": {"lat": centroid_lat, "lng": centroid_lng},
        "friends": list(geocoded),
        "results": results,
    }


# ── Helpers ──────────────────────────────────────────────────

def _centroid(geocoded: list[dict]) -> tuple[float, float]:
    lat = sum(g["lat"] for g in geocoded) / len(geocoded)
    lng = sum(g["lng"] for g in geocoded) / len(geocoded)
    return lat, lng


def _fetch_candidates(centroid_lat: float, centroid_lng: float, req: MeetupRequest) -> list[dict]:
    radius_deg = req.radius_km / 111.0
    q = (
        supabase.table("listings")
        .select("*")
        .gte("lat", centroid_lat - radius_deg)
        .lte("lat", centroid_lat + radius_deg)
        .gte("lng", centroid_lng - radius_deg)
        .lte("lng", centroid_lng + radius_deg)
        .not_.is_("lat", "null")
        .not_.is_("lng", "null")
    )
    if req.type:
        q = q.eq("type", req.type)
    if req.price_max is not None:
        q = q.or_(f"price_min.is.null,price_min.lte.{req.price_max}")
    return q.execute().data


async def _fetch_all_commutes(
    candidates: list[dict],
    geocoded: list[dict],
    token: str,
) -> list[list[int | None]]:
    """
    Returns a flat list of travel times matching the order of tasks built as:
      for each candidate × for each friend × for each mode
    All calls are fired concurrently in a single asyncio.gather.
    """
    tasks = []
    async with httpx.AsyncClient() as client:
        for candidate in candidates:
            for friend in geocoded:
                for mode in MODES:
                    tasks.append(onemap.travel_time(
                        friend["lat"], friend["lng"],
                        candidate["lat"], candidate["lng"],
                        mode, token, client,
                    ))
        return list(await asyncio.gather(*tasks))


def _build_results(
    candidates: list[dict],
    flat_times: list[int | None],
    n_friends: int,
) -> list[dict]:
    n_modes = len(MODES)
    results = []

    for ci, candidate in enumerate(candidates):
        commutes = {}
        for mi, mode in enumerate(MODES):
            # Reconstruct per-friend times from the flat list
            times = [
                flat_times[ci * n_friends * n_modes + fi * n_modes + mi]
                for fi in range(n_friends)
            ]
            valid = [t for t in times if t is not None]
            commutes[mode] = {
                "times_min": times,
                # None when not all friends have a viable route for this mode
                "max_time_min": max(valid) if len(valid) == n_friends else None,
                "fairness_score": max(valid) if len(valid) == n_friends else None,
            }
        results.append({**candidate, "commutes": commutes})

    # Sort by best available fairness: transit → drive → walk → unroutable last
    def sort_key(r):
        for mode in MODES:
            score = r["commutes"][mode]["fairness_score"]
            if score is not None:
                return score
        return float("inf")

    results.sort(key=sort_key)
    return results
