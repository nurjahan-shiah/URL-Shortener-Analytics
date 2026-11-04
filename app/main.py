"""Day 1: URL shortener core — shorten, redirect, stats.

Storage is SQLite, created next to this module as urls.db on first run.
Short codes are base62 of the row id: simple and collision-free.
"""
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl

DB_PATH = Path(__file__).parent / "urls.db"
_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

app = FastAPI(title="URL Shortener with Analytics")


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS urls ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "long_url TEXT NOT NULL, clicks INTEGER NOT NULL DEFAULT 0)"
    )
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


class ShortenRequest(BaseModel):
    url: HttpUrl


@app.post("/api/shorten")
def shorten(req: ShortenRequest):
    conn = _db()
    cur = conn.execute("INSERT INTO urls (long_url) VALUES (?)", (str(req.url),))
    conn.commit()
    code = _encode(cur.lastrowid)
    conn.close()
    return {"short_code": code, "short_url": f"http://localhost:8000/{code}"}


@app.get("/{code}")
def redirect(code: str):
    row_id = _lookup_id(code)
    conn = _db()
    row = conn.execute(
        "SELECT long_url FROM urls WHERE id = ?", (row_id,)
    ).fetchone()
    if row is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Unknown short code")
    conn.execute("UPDATE urls SET clicks = clicks + 1 WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()
    return RedirectResponse(url=row[0])


@app.get("/api/stats/{code}")
def stats(code: str):
    row_id = _lookup_id(code)
    conn = _db()
    row = conn.execute(
        "SELECT long_url, clicks FROM urls WHERE id = ?", (row_id,)
    ).fetchone()
    conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown short code")
    return {"short_code": code, "long_url": row[0], "clicks": row[1]}
