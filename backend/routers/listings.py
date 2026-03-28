from fastapi import APIRouter, Query
from typing import Optional
from database import supabase

router = APIRouter()


@router.get("/listings")
def get_listings(
    type: Optional[str] = Query(None, description="food | event | activity"),
    price_max: Optional[int] = Query(None, description="Max price in SGD cents"),
    tags: Optional[str] = Query(None, description="Comma-separated tags"),
):
    q = supabase.table("listings").select("*")

    if type:
        q = q.eq("type", type)

    if price_max is not None:
        q = q.or_(f"price_min.is.null,price_min.lte.{price_max}")

    if tags:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if tag_list:
            q = q.contains("tags", tag_list)

    return q.execute().data
