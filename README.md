# URL Shortener with Analytics

A scalable URL shortener built in small daily commits as big-tech interview prep.
The point is not just a working shortener — it's the design decisions, trade-offs,
and measurements recorded along the way, so you can talk about them in an interview.

## What it does (Day 1)
- `POST /api/shorten` — takes a long URL, returns a short code
- `GET /{code}` — redirects to the original URL and records a click
- `GET /api/stats/{code}` — total clicks for a code

## Stack
Python, FastAPI, SQLite (for Day 1; Postgres + Redis arrive later in the plan).

## Run it
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Run tests
```bash
pytest
```

## Roadmap (one feature + tests per day)
- Week 1: Core API, SQLite storage, base62 codes, tests
- Week 2: Redis-style caching layer, rate limiting, custom aliases, expiry
- Week 3: Click analytics (per-day counts, referrer, top links), API for stats
- Week 4: Load test, latency numbers, scaling notes, system-design write-up

## Design decisions (log them here as you go)
- Short codes: base62 of an auto-incrementing id — simple, collision-free, but
  predictable. Custom aliases and a hash-based alternative are planned.
- Clicks: recorded on redirect, not on shorten. Analytics are aggregated
  per day, not stored as one row per click forever.
- Caching: hot links will be cached in front of the database; the database
  stays the source of truth.
