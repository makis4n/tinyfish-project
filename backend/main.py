from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

import logging
logging.basicConfig(level=logging.INFO)

from routers import listings, meetup, ingest

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(listings.router)
app.include_router(meetup.router)
app.include_router(ingest.router)


@app.get("/")
def read_root():
    return {"status": "Backend is alive"}