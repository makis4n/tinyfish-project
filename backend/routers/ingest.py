from fastapi import APIRouter, BackgroundTasks, Query
from typing import Optional
from scrapers import sgculturepass, chope, eventbrite
from database import supabase
from services import onemap
import asyncio
import httpx
import logging

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest")


@router.post("/sgculturepass")
async def ingest_sgculturepass(
    background_tasks: BackgroundTasks,
    limit: Optional[int] = Query(None, description="Max number of events to scrape. Omit for full run."),
):
    """
    Triggers a SG Culture Pass scrape in the background.
    Returns immediately — check the scrape_runs table in Supabase for results.
    """
    background_tasks.add_task(sgculturepass.run, limit=limit)
    return {"status": "started", "message": f"Scraping SG Culture Pass in background (limit={limit}). Check scrape_runs table for progress."}


@router.post("/geocode-retry")
async def geocode_retry(background_tasks: BackgroundTasks):
    """
    Re-geocodes all listings where lat/lng is null.
    Runs in the background — check logs or query Supabase for results.
    """
    background_tasks.add_task(_run_geocode_retry)
    return {"status": "started", "message": "Geocode retry running in background."}


async def _run_geocode_retry():
    rows = (
        supabase.table("listings")
        .select("id, address")
        .is_("lat", "null")
        .not_.is_("address", "null")
        .execute()
        .data
    )

    if not rows:
        log.info("Geocode retry: no rows with null lat/lng found")
        return

    log.info(f"Geocode retry: attempting {len(rows)} rows")
    updated = 0

    async with httpx.AsyncClient() as client:
        for row in rows:
            geo = await onemap.geocode(row["address"], client)
            if geo:
                supabase.table("listings").update(
                    {"lat": geo["lat"], "lng": geo["lng"]}
                ).eq("id", row["id"]).execute()
                updated += 1
            await asyncio.sleep(0.5)

    log.info(f"Geocode retry: updated {updated}/{len(rows)} rows")


@router.post("/chope")
async def ingest_chope(background_tasks: BackgroundTasks):
    background_tasks.add_task(chope.run)
    return {"status": "started", "message": "Scraping Chope in background. Check scrape_runs table for progress."}


@router.post("/eventbrite")
async def ingest_eventbrite(background_tasks: BackgroundTasks):
    background_tasks.add_task(eventbrite.run)
    return {"status": "started", "message": "Scraping Eventbrite in background. Check scrape_runs table for progress."}

