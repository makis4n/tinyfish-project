from pydantic import BaseModel
from typing import Optional


class MeetupRequest(BaseModel):
    addresses: list[str]
    type: Optional[str] = None       # "food" | "event" | "activity"
    price_max: Optional[int] = None  # SGD cents
    radius_km: float = 8.0
