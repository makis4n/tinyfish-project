# MeetGoWhere

A Singapore food & events aggregator with an interactive map and friend meetup finder. Scrapes listings from SG Culture Pass, Chope, and Eventbrite, normalises them into a unified database, and helps groups of friends find a venue that's fair for everyone to travel to.

## Features

- **Interactive map** — browse food venues and events across Singapore with filters for type, price, and tags; pins cluster by category at lower zoom levels
- **Mobile-friendly** — slide-in sidebar and listings panel on small screens
- **Meetup finder** — input multiple addresses and get venue suggestions ranked by fairness (minimises the longest commute in the group), with transit, drive, and walk times via OneMap
- **Multi-source scraping** — SG Culture Pass, Chope, and Eventbrite scraped via TinyFish browser automation; trigger manually via API

## Stack

| Layer | Tech |
|---|---|
| Frontend | React + TypeScript, Vite, Leaflet, leaflet.markercluster |
| Backend | FastAPI, Python 3.11 |
| Database | Supabase (PostgreSQL) |
| Scraping | TinyFish web automation |
| Geocoding & routing | OneMap Singapore API |

## Project Structure

```
tinyfish-project/
├── frontend/          # React + Vite app
├── backend/
│   ├── routers/       # listings, meetup, ingest endpoints
│   ├── scrapers/      # sgculturepass, chope, eventbrite
│   └── services/      # onemap, tinyfish clients
└── supabase/
    └── migrations/    # schema + seed data
```

## Local Setup

### Backend

```bash
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload   # http://localhost:8000, docs at /docs
```

Create `backend/.env`:
```
SUPABASE_URL=
SUPABASE_KEY=
ONEMAP_EMAIL=
ONEMAP_PASSWORD=
TINYFISH_API_KEY=
```

### Frontend

```bash
cd frontend
npm install
npm run dev   # http://localhost:5173
```

Create `frontend/.env.local`:
```
VITE_SUPABASE_URL=
VITE_SUPABASE_ANON_KEY=
VITE_API_URL=http://localhost:8000
```

### Database

Run the migration via the Supabase SQL Editor:
1. `supabase/migrations/001_initial_schema.sql`

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/listings` | Fetch active listings with optional `type`, `price_max`, `tags` filters. Expired events are excluded automatically. |
| `POST` | `/meetup` | Find venues near the midpoint of multiple addresses with travel times |
| `POST` | `/ingest/sgculturepass` | Trigger SG Culture Pass scrape (background) |
| `POST` | `/ingest/sgculturepass/sync` | Trigger SG Culture Pass scrape (blocking — waits for completion) |
| `POST` | `/ingest/chope` | Trigger Chope scrape (background) |
| `POST` | `/ingest/chope/sync` | Trigger Chope scrape (blocking) |
| `POST` | `/ingest/eventbrite` | Trigger Eventbrite scrape (background) |
| `POST` | `/ingest/eventbrite/sync` | Trigger Eventbrite scrape (blocking) |
| `POST` | `/ingest/geocode-retry` | Re-geocode listings with missing coordinates |

Scrapes are triggered manually. Use the `/sync` variants to wait for the full result:

```bash
curl -X POST https://your-api.com/ingest/sgculturepass/sync
curl -X POST https://your-api.com/ingest/eventbrite/sync
curl -X POST https://your-api.com/ingest/chope/sync
```

## Meetup Finder

```json
POST /meetup
{
  "addresses": ["Tampines MRT", "Bugis MRT", "Jurong East MRT"],
  "type": "food",
  "price_max": 2000,
  "radius_km": 8
}
```

Geocodes each address, queries candidates within radius of the centroid, fetches real travel times from OneMap for all friends × all candidates × all modes, and ranks by lowest maximum individual travel time.

## Deployment

- **Frontend** → Vercel (root: `frontend`, framework: Vite)
- **Backend** → Render (root: `backend`, start: `uvicorn main:app --host 0.0.0.0 --port $PORT`)
