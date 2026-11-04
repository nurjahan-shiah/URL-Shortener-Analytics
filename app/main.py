"""Day 1: URL shortener core, shorten, redirect, stats.
Day 2: custom aliases, user-chosen code, conflict returns 409.

Storage is SQLite, created next to this module as urls.db on first run.
Short codes are base62 of the row id: simple and collision-free.
Custom aliases are stored in their own column and take precedence in lookup.
"""
import re
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, HttpUrl

DB_PATH = Path(__file__).parent / "urls.db"
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ALIAS_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

app = FastAPI(title="URL Shortener with Analytics")


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS urls ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "long_url TEXT NOT NULL, clicks INTEGER NOT NULL DEFAULT 0, "
        "custom_alias TEXT UNIQUE)"
    )
    columns = [row[1] for row in conn.execute("PRAGMA table_info(urls)")]
    if "custom_alias" not in columns:
        conn.execute("ALTER TABLE urls ADD COLUMN custom_alias TEXT UNIQUE")
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
    """Return (id, long_url, clicks) for an alias or a generated code."""
    row = conn.execute(
        "SELECT id, long_url, clicks FROM urls WHERE custom_alias = ?",
        (code,),
    ).fetchone()
    if row is not None:
        return row
    row_id = _lookup_id(code)
    row = conn.execute(
        "SELECT id, long_url, clicks FROM urls WHERE id = ?", (row_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown short code")
    return row


class ShortenRequest(BaseModel):
    url: HttpUrl
    custom_alias: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$"
    )


@app.post("/api/shorten")
def shorten(req: ShortenRequest):
    conn = _db()
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
            "INSERT INTO urls (long_url, custom_alias) VALUES (?, ?)",
            (str(req.url), alias),
        )
        conn.commit()
        conn.close()
        return {"short_code": alias, "short_url": f"http://localhost:8000/{alias}"}
    cur = conn.execute("INSERT INTO urls (long_url) VALUES (?)", (str(req.url),))
    conn.commit()
    code = _encode(cur.lastrowid)
    conn.close()
    return {"short_code": code, "short_url": f"http://localhost:8000/{code}"}


@app.get("/{code}")
def redirect(code: str):
    conn = _db()
    row = _resolve(conn, code)
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
