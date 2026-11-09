"""Day 1: URL shortener core, shorten, redirect, stats.
Day 2: custom aliases, user-chosen code, conflict returns 409.
Day 3: link expiry, expired links return 410.
Day 4: duplicate URLs return the existing code.
Day 5: in-memory LRU cache in front of the database for redirects.
Day 6: rate limiting on shorten, per IP, sliding window.

Storage is SQLite, created next to this module as urls.db on first run.
Short codes are base62 of the row id: simple and collision-free.
Custom aliases are stored in their own column and take precedence in lookup.
Expiry is an optional UTC timestamp per link, set at shorten time.
Duplicate URLs reuse the existing live link, one URL maps to one code.
Redirects are served from an in-memory LRU cache keyed by code, so a hot
link avoids a database lookup. The database stays the source of truth:
clicks are still incremented in SQLite on every redirect, and stats
always read click counts from the database, never from the cache.
"""
import re
import sqlite3
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, HttpUrl

DB_PATH = Path(__file__).parent / "urls.db"
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# In-memory LRU cache for redirects: code -> (row id, long_url, expires_at).
# Bounded so a flood of distinct codes cannot grow memory without limit.
# No TTL yet, a cached entry is checked against its stored expires_at on
# every hit, and Day 14 adds TTL plus invalidation on delete.
CACHE_MAX_SIZE = 1000
redirect_cache: OrderedDict[str, tuple[int, str, str | None]] = OrderedDict()
_cache_hits = 0
_cache_misses = 0

app = FastAPI(title="URL Shortener with Analytics")


def cache_stats() -> dict:
    """Return cache size, hit/miss counts, and the configured max size."""
    return {
        "size": len(redirect_cache),
        "hits": _cache_hits,
        "misses": _cache_misses,
        "max_size": CACHE_MAX_SIZE,
    }


def _cache_get(code: str) -> tuple[int, str, str | None] | None:
    """Return the cached entry for a code, or None on a miss.

    A hit refreshes the entry's recency. An entry whose stored expiry has
    passed is evicted and treated as a miss, so the caller re-checks the
    database and returns 410.
    """
    global _cache_hits, _cache_misses
    entry = redirect_cache.get(code)
    if entry is None:
        _cache_misses += 1
        return None
    _cache_hits += 1
    expires_at = entry[2]
    if expires_at is not None and datetime.fromisoformat(
        expires_at
    ) <= datetime.now(timezone.utc):
        del redirect_cache[code]
        return None
    redirect_cache.move_to_end(code)
    return entry


def _cache_put(code: str, entry: tuple[int, str, str | None]) -> None:
    """Store an entry, evicting the least recently used entries if full."""
    redirect_cache[code] = entry
    redirect_cache.move_to_end(code)
    while len(redirect_cache) > CACHE_MAX_SIZE:
        redirect_cache.popitem(last=False)


# Rate limiting for shorten: a fixed number of requests per IP per sliding
# window, kept in memory. In-memory means the counters reset on restart and
# do not survive across instances, which is fine for a single process; a
# shared store (Redis) would be the next step for multiple replicas.
RATE_LIMIT_MAX_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60

# Per-IP list of monotonic timestamps of accepted shorten requests.
_rate_limit_store: dict[str, list[float]] = {}


def reset_rate_limits() -> None:
    """Clear all per-IP rate limit state. Used by tests."""
    _rate_limit_store.clear()


def _client_ip(request: Request) -> str:
    """Identify the caller for rate limiting.

    Prefer the first entry of X-Forwarded-For when present, since the app
    usually sits behind a proxy and the socket address would then be the
    proxy, not the caller. Fall back to the socket address.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(client_ip: str) -> None:
    """Record one shorten request for an IP, or raise 429 if over the limit.

    Uses a sliding window: only requests inside the last
    RATE_LIMIT_WINDOW_SECONDS count. The 429 carries a Retry-After header
    in seconds, so a well-behaved client knows when to retry.
    """
    now = time.monotonic()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    timestamps = _rate_limit_store.setdefault(client_ip, [])
    timestamps[:] = [t for t in timestamps if t > window_start]
    if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
        retry_after = int(RATE_LIMIT_WINDOW_SECONDS - (now - timestamps[0])) + 1
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded, try again later",
            headers={"Retry-After": str(retry_after)},
        )
    timestamps.append(now)


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS urls ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "long_url TEXT NOT NULL, clicks INTEGER NOT NULL DEFAULT 0, "
        "custom_alias TEXT UNIQUE, expires_at TEXT)"
    )
    columns = [row[1] for row in conn.execute("PRAGMA table_info(urls)")]
    if "custom_alias" not in columns:
        conn.execute("ALTER TABLE urls ADD COLUMN custom_alias TEXT UNIQUE")
    if "expires_at" not in columns:
        conn.execute("ALTER TABLE urls ADD COLUMN expires_at TEXT")
    conn.commit()
    return conn


def _encode(n: int) -> str:
    if n == 0:
        return _ALPHABET[0]
    out = []
    while n:
        n, r = divmod(n, 62)
        out.append(_ALPHABET[r])
    return "".join(reversed(out))


def _decode(code: str) -> int:
    n = 0
    for ch in code:
        if ch not in _ALPHABET:
            raise ValueError("Invalid character in short code")
        n = n * 62 + _ALPHABET.index(ch)
        if n > 2**63 - 1:
            raise ValueError("Short code out of range")
    return n


def _lookup_id(code: str) -> int:
    """Return the row id for a code, or raise a 404 for anything invalid."""
    try:
        return _decode(code)
    except ValueError:
        raise HTTPException(status_code=404, detail="Unknown short code")


def _resolve(conn, code: str):
    """Return (id, long_url, clicks, expires_at) for an alias or a generated code."""
    row = conn.execute(
        "SELECT id, long_url, clicks, expires_at FROM urls WHERE custom_alias = ?",
        (code,),
    ).fetchone()
    if row is None:
        row_id = _lookup_id(code)
        row = conn.execute(
            "SELECT id, long_url, clicks, expires_at FROM urls WHERE id = ?",
            (row_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown short code")
    if row[3] is not None:
        expires_at = datetime.fromisoformat(row[3])
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail="Link expired")
    return row


class ShortenRequest(BaseModel):
    url: HttpUrl
    custom_alias: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$"
    )
    expires_in_seconds: int | None = Field(default=None, gt=0)


@app.post("/api/shorten")
def shorten(req: ShortenRequest, request: Request):
    _check_rate_limit(_client_ip(request))
    conn = _db()
    expires_at = None
    if req.expires_in_seconds is not None:
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=req.expires_in_seconds)
        ).isoformat()
    if req.custom_alias is not None:
        alias = req.custom_alias
        taken = conn.execute(
            "SELECT id FROM urls WHERE custom_alias = ?", (alias,)
        ).fetchone()
        if taken is not None:
            conn.close()
            raise HTTPException(status_code=409, detail="Custom alias taken")
        try:
            existing_id = _decode(alias)
        except ValueError:
            existing_id = None
        if existing_id is not None:
            shadowed = conn.execute(
                "SELECT id FROM urls WHERE id = ?", (existing_id,)
            ).fetchone()
            if shadowed is not None:
                conn.close()
                raise HTTPException(
                    status_code=409, detail="Custom alias taken"
                )
        conn.execute(
            "INSERT INTO urls (long_url, custom_alias, expires_at) "
            "VALUES (?, ?, ?)",
            (str(req.url), alias, expires_at),
        )
        conn.commit()
        conn.close()
        return {
            "short_code": alias,
            "short_url": f"http://localhost:8000/{alias}",
            "expires_at": expires_at,
        }
    existing = conn.execute(
        "SELECT id, custom_alias, expires_at FROM urls "
        "WHERE long_url = ? ORDER BY id LIMIT 1",
        (str(req.url),),
    ).fetchone()
    if existing is not None:
        stored_expires_at = existing[2]
        live = stored_expires_at is None or datetime.fromisoformat(
            stored_expires_at
        ) > datetime.now(timezone.utc)
        if live:
            code = (
                existing[1] if existing[1] is not None else _encode(existing[0])
            )
            conn.close()
            return {
                "short_code": code,
                "short_url": f"http://localhost:8000/{code}",
                "expires_at": stored_expires_at,
            }
    cur = conn.execute(
        "INSERT INTO urls (long_url, expires_at) VALUES (?, ?)",
        (str(req.url), expires_at),
    )
    conn.commit()
    code = _encode(cur.lastrowid)
    conn.close()
    return {
        "short_code": code,
        "short_url": f"http://localhost:8000/{code}",
        "expires_at": expires_at,
    }


@app.get("/{code}")
def redirect(code: str):
    cached = _cache_get(code)
    if cached is not None:
        row_id, long_url, _expires_at = cached
        conn = _db()
        conn.execute(
            "UPDATE urls SET clicks = clicks + 1 WHERE id = ?", (row_id,)
        )
        conn.commit()
        conn.close()
        return RedirectResponse(url=long_url)
    conn = _db()
    row = _resolve(conn, code)
    _cache_put(code, (row[0], row[1], row[3]))
    conn.execute("UPDATE urls SET clicks = clicks + 1 WHERE id = ?", (row[0],))
    conn.commit()
    conn.close()
    return RedirectResponse(url=row[1])


@app.get("/api/stats/{code}")
def stats(code: str):
    conn = _db()
    row = _resolve(conn, code)
    conn.close()
    return {"short_code": code, "long_url": row[1], "clicks": row[2]}

