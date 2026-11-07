import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import DB_PATH, app

client = TestClient(app)


def test_shorten_redirect_and_stats():
    r = client.post("/api/shorten", json={"url": "https://example.com/a-long-page"})
    assert r.status_code == 200
    code = r.json()["short_code"]

    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code in (307, 308)

    r = client.get(f"/api/stats/{code}")
    assert r.json()["clicks"] == 1


def test_unknown_code_404():
    assert client.get("/api/stats/doesnotexist123").status_code == 404


def test_custom_alias_redirect_and_stats():
    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/custom", "custom_alias": "my-link"},
    )
    assert r.status_code == 200
    assert r.json()["short_code"] == "my-link"

    r = client.get("/my-link", follow_redirects=False)
    assert r.status_code in (307, 308)

    r = client.get("/api/stats/my-link")
    assert r.json()["clicks"] == 1
    assert r.json()["long_url"] == "https://example.com/custom"


def test_custom_alias_conflict_409():
    first = client.post(
        "/api/shorten",
        json={"url": "https://example.com/first", "custom_alias": "taken-alias"},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/shorten",
        json={"url": "https://example.com/second", "custom_alias": "taken-alias"},
    )
    assert second.status_code == 409


def test_custom_alias_invalid_rejected():
    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/bad", "custom_alias": "has space"},
    )
    assert r.status_code == 422

    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/bad", "custom_alias": "has/slash"},
    )
    assert r.status_code == 422


def test_link_with_future_expiry_redirects():
    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/not-expired", "expires_in_seconds": 3600},
    )
    assert r.status_code == 200
    assert r.json()["expires_at"] is not None
    code = r.json()["short_code"]

    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code in (307, 308)

    r = client.get(f"/api/stats/{code}")
    assert r.json()["clicks"] == 1


def test_expired_link_returns_410():
    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/expired", "custom_alias": "will-expire"},
    )
    assert r.status_code == 200

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE urls SET expires_at = ? WHERE custom_alias = ?",
        (past, "will-expire"),
    )
    conn.commit()
    conn.close()

    r = client.get("/will-expire", follow_redirects=False)
    assert r.status_code == 410

    r = client.get("/api/stats/will-expire")
    assert r.status_code == 410


def test_expired_link_clicks_not_counted():
    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/no-count", "custom_alias": "no-count"},
    )
    assert r.status_code == 200

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE urls SET expires_at = ? WHERE custom_alias = ?",
        (past, "no-count"),
    )
    conn.commit()
    conn.close()

    client.get("/no-count", follow_redirects=False)
    conn = sqlite3.connect(DB_PATH)
    clicks = conn.execute(
        "SELECT clicks FROM urls WHERE custom_alias = ?", ("no-count",)
    ).fetchone()[0]
    conn.close()
    assert clicks == 0


def test_invalid_expiry_rejected():
    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/bad", "expires_in_seconds": 0},
    )
    assert r.status_code == 422

    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/bad", "expires_in_seconds": -5},
    )
    assert r.status_code == 422


def test_duplicate_url_returns_existing_code():
    first = client.post(
        "/api/shorten", json={"url": "https://example.com/duplicate"}
    )
    assert first.status_code == 200
    code = first.json()["short_code"]

    second = client.post(
        "/api/shorten", json={"url": "https://example.com/duplicate"}
    )
    assert second.status_code == 200
    assert second.json()["short_code"] == code

    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT COUNT(*) FROM urls WHERE long_url = ?",
        ("https://example.com/duplicate",),
    ).fetchone()[0]
    conn.close()
    assert rows == 1


def test_duplicate_url_does_not_reset_clicks():
    r = client.post(
        "/api/shorten", json={"url": "https://example.com/duplicate-clicks"}
    )
    assert r.status_code == 200
    code = r.json()["short_code"]

    client.get(f"/{code}", follow_redirects=False)

    r = client.post(
        "/api/shorten", json={"url": "https://example.com/duplicate-clicks"}
    )
    assert r.status_code == 200
    assert r.json()["short_code"] == code

    r = client.get(f"/api/stats/{code}")
    assert r.json()["clicks"] == 1


def test_duplicate_of_expired_link_creates_new_code():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/duplicate-expired",
            "custom_alias": "dup-expired",
        },
    )
    assert r.status_code == 200

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE urls SET expires_at = ? WHERE custom_alias = ?",
        (past, "dup-expired"),
    )
    conn.commit()
    conn.close()

    r = client.post(
        "/api/shorten", json={"url": "https://example.com/duplicate-expired"}
    )
    assert r.status_code == 200
    assert r.json()["short_code"] != "dup-expired"

    r = client.get(f"/{r.json()['short_code']}", follow_redirects=False)
    assert r.status_code in (307, 308)


def test_duplicate_url_with_same_alias_still_409():
    first = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/duplicate-alias",
            "custom_alias": "dup-alias",
        },
    )
    assert first.status_code == 200

    second = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/duplicate-alias",
            "custom_alias": "dup-alias",
        },
    )
    assert second.status_code == 409
