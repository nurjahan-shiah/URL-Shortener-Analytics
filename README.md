# URL Shortener with Analytics

A scalable URL shortener built in small daily commits as big-tech interview prep.
The point is not just a working shortener, it's the design decisions, trade-offs,
and measurements recorded along the way, so you can talk about them in an interview.

**Status:** In progress, one feature per day, pushed daily.
Day 3 is done (Nov 5, 2026). The project is finished when every item below is
checked off, which should be around Nov 19, 2026 if no day is missed.

## What it does so far
- `POST /api/shorten`, takes a long URL, returns a short code
- `POST /api/shorten` with `custom_alias`, takes a user-chosen code instead
- `POST /api/shorten` with `expires_in_seconds`, sets a lifetime for the link
- `GET /{code}`, redirects to the original URL and records a click
- `GET /{code}` for an expired link returns 410 Gone, and the click is not counted
- `GET /api/stats/{code}`, total clicks for a code

## Stack
Python, FastAPI, SQLite (for now; a caching layer arrives in Week 2).

## Run it
```bash
pip install -r requirements.txt
uvicorn app.main:app
```

## Run tests
```bash
pytest
```

## Daily build plan (one feature + tests per day)
- [x] Day 1, Core API: shorten, redirect, click stats (Nov 3)
- [x] Day 2, Custom aliases (user-chosen code, conflict returns 409) (Nov 4)
- [x] Day 3, Link expiry (expired links return 410) (Nov 5)
- [ ] Day 4, Duplicate URLs return the existing code
- [ ] Day 5, In-memory LRU cache in front of the database for redirects
- [ ] Day 6, Rate limiting on shorten (per IP)
- [ ] Day 7, Click events table (timestamp, referrer per click)
- [ ] Day 8, Per-day click counts endpoint
- [ ] Day 9, Top links endpoint
- [ ] Day 10, Deactivate / delete a link
- [ ] Day 11, List links with pagination
- [ ] Day 12, URL validation hardening + extra tests
- [ ] Day 13, Analytics summary endpoint (totals, top link)
- [ ] Day 14, Cache TTL + invalidation on delete
- [ ] Day 15, Load-test script + real benchmark numbers
- [ ] Day 16, System-design write-up (`docs/system-design.md`)
- [ ] Day 17, Final polish: interview talking points, cleanup, project complete

## Design decisions (log them here as you go)
- Short codes: base62 of an auto-incrementing id, simple, collision-free, but
  predictable. Custom aliases (Day 2) are the user-facing answer to that.
- Clicks: recorded on redirect, not on shorten. Analytics are aggregated
  per day, not stored as one row per click forever.
- Caching: hot links will be cached in front of the database; the database
  stays the source of truth.
- Unknown short codes return 404, including codes that decode to out-of-range
  ids (fixed after a test caught an OverflowError on Day 1).
- Custom aliases: stored in a unique column, looked up before generated
  codes, limited to letters, digits, hyphen and underscore, 1 to 64 chars.
  A taken alias, or an alias that would shadow an existing generated code,
  returns 409. Invalid characters return 422 from request validation.
- Link expiry: an optional `expires_in_seconds` at shorten time, stored as a
  UTC ISO timestamp in `expires_at`. The check happens in the shared resolver,
  so an expired link returns 410 on both redirect and stats, and the click
  is never counted. Expired rows are kept, not deleted, so stats history
  survives and a future cleanup job can purge them.
