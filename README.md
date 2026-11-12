# URL Shortener with Analytics

A scalable URL shortener built in small daily commits as big-tech interview prep.
The point is not just a working shortener, it's the design decisions, trade-offs,
and measurements recorded along the way, so you can talk about them in an interview.

**Status:** In progress, one feature per day, pushed daily.
Day 9 is done (Nov 12, 2026). The project is finished when every item below is
checked off, which should be around Nov 19, 2026 if no day is missed.

## What it does so far
- `POST /api/shorten`, takes a long URL, returns a short code
- `POST /api/shorten` with `custom_alias`, takes a user-chosen code instead
- `POST /api/shorten` with `expires_in_seconds`, sets a lifetime for the link
- `POST /api/shorten` for a URL that already has a live link returns the
  existing code instead of creating a new row
- `POST /api/shorten` is rate limited to 10 requests per minute per IP,
  further requests get 429 with a `Retry-After` header
- `GET /{code}`, redirects to the original URL and records a click
- `GET /{code}` for a hot link is served from an in-memory LRU cache, so the
  redirect avoids a database lookup while clicks are still counted in SQLite
- `GET /{code}` for an expired link returns 410 Gone, and the click is not counted
- `GET /{code}` records a click event (UTC timestamp, referrer from the
  `Referer` header) in the `click_events` table on every redirect
- `GET /api/stats/{code}`, total clicks for a code
- `GET /api/stats/{code}/per-day`, click counts per UTC day, zero-filled
  across the requested range, defaults to the last 30 days including today;
  `start_date` and `end_date` query params take YYYY-MM-DD dates
- `GET /api/stats/top`, the most-clicked live links, most clicks first;
  `limit` query param, default 10, max 100

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
- [x] Day 4, Duplicate URLs return the existing code (Nov 7)
- [x] Day 5, In-memory LRU cache in front of the database for redirects (Nov 8)
- [x] Day 6, Rate limiting on shorten (per IP) (Nov 9)
- [x] Day 7, Click events table (timestamp, referrer per click) (Nov 10)
- [x] Day 8, Per-day click counts endpoint (Nov 11)
- [x] Day 9, Top links endpoint (Nov 12)
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
- Clicks: recorded on redirect, not on shorten. Every redirect appends one
  row to `click_events` (UTC timestamp, referrer from the `Referer` header,
  null when the header is absent), so per-day counts and referrer breakdowns
  can be derived later instead of pre-aggregated. The `urls.clicks` counter
  stays as the cheap total, and the two agree. An index on
  `(url_id, clicked_at)` keeps per-link, per-day queries fast. Events are
  append-only with no enforced foreign key, so a redirect served from the
  cache for a deleted row still records its event. Keeping one row per click
  forever grows the table without bound; a retention or rollup job is the
  next step once traffic justifies it.
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
- Duplicate URLs: shortening a URL that already has a live link returns the
  existing code, so one URL maps to one code and no duplicate rows pile up.
  The lookup is on the exact stored URL string. An expired link is not
  reused, shortening it again creates a fresh link. A requested
  `expires_in_seconds` is ignored when an existing live link is returned,
  the response carries the existing link's expiry instead. Alias requests
  are exempt: a taken alias still returns 409, even for the same URL.
- Redirect cache: an in-memory `OrderedDict` LRU, keyed by the requested
  code, capped at 1000 entries, sitting in front of SQLite on the redirect
  path only. A hit still writes the click to the database, and stats always
  read click counts from the database, so the cache can never serve stale
  analytics. The cached entry carries `expires_at`, checked on every hit,
  so an expired link is evicted instead of served. No TTL yet, and a row
  changed in the database after caching is not picked up until eviction;
  Day 14 adds TTL plus invalidation on delete.
- Rate limiting: `POST /api/shorten` only, 10 requests per minute per IP,
  sliding window over monotonic timestamps kept in memory. Over the limit
  returns 429 with a `Retry-After` header in seconds. The IP is the first
  entry of `X-Forwarded-For` when present, else the socket address, so the
  limit works behind a proxy. Redirects and stats are not limited, reads
  are cheap and a hot link should never be throttled. In-memory counters
  reset on restart and do not share across instances; a shared store such
  as Redis is the next step for multiple replicas. Invalid requests fail
  validation (422) before the limiter runs, so they do not consume quota.
- Per-day counts: derived from the `click_events` table with one SQL query,
  `GROUP BY date(clicked_at)` over the requested range, so the per-day
  numbers always add up to the total in `/api/stats`. Dates are UTC days,
  matching how events are timestamped. The response zero-fills days with
  no clicks, so a caller can plot it without filling gaps itself. The
  default window is the last 30 days; an explicit `start_date`/`end_date`
  is capped at 365 days to keep one request from forcing a huge response.
  A bad range (start after end, or an impossible date) returns 422, and
  unknown or expired codes behave like the other stats endpoint (404/410).
- Top links: one SQL query over `urls`, ordered by `clicks DESC, id ASC`
  with a `LIMIT`, so the ranking always agrees with `/api/stats` and ties
  resolve to the oldest link first for a stable order. Expired links are
  filtered out in the query, since they no longer redirect and return 410
  elsewhere. The endpoint is registered before `/api/stats/{code}`, so the
  literal path `/api/stats/top` is not swallowed as a short code. The
  default limit is 10 and the max is 100, so one request cannot force a
  huge response; a limit outside 1 to 100 returns 422.

