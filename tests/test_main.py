import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.main import DB_PATH, app, cache_stats, redirect_cache

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Each test starts with a clean per-IP rate limit store."""
    main_module.reset_rate_limits()


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


def test_redirect_populates_cache_and_serves_from_it():
    r = client.post(
        "/api/shorten", json={"url": "https://example.com/cached-link"}
    )
    assert r.status_code == 200
    code = r.json()["short_code"]

    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert code in redirect_cache
    assert redirect_cache[code][1] == "https://example.com/cached-link"

    # The database row is gone, but the redirect still works from cache.
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM urls WHERE id = ?", (redirect_cache[code][0],))
    conn.commit()
    conn.close()

    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"] == "https://example.com/cached-link"


def test_cached_redirect_still_counts_clicks():
    r = client.post(
        "/api/shorten", json={"url": "https://example.com/cached-clicks"}
    )
    assert r.status_code == 200
    code = r.json()["short_code"]

    client.get(f"/{code}", follow_redirects=False)
    client.get(f"/{code}", follow_redirects=False)

    r = client.get(f"/api/stats/{code}")
    assert r.json()["clicks"] == 2


def test_cache_evicts_least_recently_used(monkeypatch):
    redirect_cache.clear()
    monkeypatch.setattr(main_module, "CACHE_MAX_SIZE", 2)

    codes = []
    for i in range(3):
        r = client.post(
            "/api/shorten",
            json={"url": f"https://example.com/lru-{i}"},
        )
        assert r.status_code == 200
        codes.append(r.json()["short_code"])

    for code in codes:
        r = client.get(f"/{code}", follow_redirects=False)
        assert r.status_code in (307, 308)

    assert len(redirect_cache) == 2
    assert codes[0] not in redirect_cache
    assert codes[1] in redirect_cache
    assert codes[2] in redirect_cache


def test_cache_refreshes_recency_on_hit(monkeypatch):
    redirect_cache.clear()
    monkeypatch.setattr(main_module, "CACHE_MAX_SIZE", 2)

    codes = []
    for i in range(2):
        r = client.post(
            "/api/shorten",
            json={"url": f"https://example.com/lru-hit-{i}"},
        )
        assert r.status_code == 200
        codes.append(r.json()["short_code"])

    for code in codes:
        client.get(f"/{code}", follow_redirects=False)

    # Revisit the first code so it becomes most recently used.
    client.get(f"/{codes[0]}", follow_redirects=False)

    r = client.post(
        "/api/shorten", json={"url": "https://example.com/lru-hit-2"}
    )
    assert r.status_code == 200
    client.get(f"/{r.json()['short_code']}", follow_redirects=False)

    assert codes[0] in redirect_cache
    assert codes[1] not in redirect_cache


def test_expired_link_is_not_cached():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/expired-cache",
            "custom_alias": "expired-cache",
        },
    )
    assert r.status_code == 200

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE urls SET expires_at = ? WHERE custom_alias = ?",
        (past, "expired-cache"),
    )
    conn.commit()
    conn.close()

    r = client.get("/expired-cache", follow_redirects=False)
    assert r.status_code == 410
    assert "expired-cache" not in redirect_cache


def test_cache_stats_counts_hits_and_misses():
    before = cache_stats()
    r = client.post(
        "/api/shorten", json={"url": "https://example.com/cache-stats"}
    )
    assert r.status_code == 200
    code = r.json()["short_code"]

    client.get(f"/{code}", follow_redirects=False)
    client.get(f"/{code}", follow_redirects=False)

    after = cache_stats()
    assert after["misses"] > before["misses"]
    assert after["hits"] > before["hits"]
    assert after["size"] >= 1
    assert after["max_size"] == main_module.CACHE_MAX_SIZE


def test_shorten_rate_limit_blocks_after_limit(monkeypatch):
    monkeypatch.setattr(main_module, "RATE_LIMIT_MAX_REQUESTS", 3)

    for i in range(3):
        r = client.post(
            "/api/shorten",
            json={"url": f"https://example.com/rate-limited-{i}"},
        )
        assert r.status_code == 200

    r = client.post(
        "/api/shorten", json={"url": "https://example.com/rate-limited-3"}
    )
    assert r.status_code == 429
    assert "retry-after" in r.headers
    assert int(r.headers["retry-after"]) > 0


def test_rate_limit_is_per_ip(monkeypatch):
    monkeypatch.setattr(main_module, "RATE_LIMIT_MAX_REQUESTS", 2)
    headers_a = {"X-Forwarded-For": "203.0.113.1"}
    headers_b = {"X-Forwarded-For": "203.0.113.2"}

    for i in range(2):
        r = client.post(
            "/api/shorten",
            json={"url": f"https://example.com/per-ip-a-{i}"},
            headers=headers_a,
        )
        assert r.status_code == 200

    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/per-ip-a-3"},
        headers=headers_a,
    )
    assert r.status_code == 429

    r = client.post(
        "/api/shorten",
        json={"url": "https://example.com/per-ip-b"},
        headers=headers_b,
    )
    assert r.status_code == 200


def test_rate_limit_does_not_block_redirects(monkeypatch):
    monkeypatch.setattr(main_module, "RATE_LIMIT_MAX_REQUESTS", 1)

    r = client.post(
        "/api/shorten", json={"url": "https://example.com/rate-redirect"}
    )
    assert r.status_code == 200
    code = r.json()["short_code"]

    r = client.post(
        "/api/shorten", json={"url": "https://example.com/rate-redirect-2"}
    )
    assert r.status_code == 429

    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code in (307, 308)


def test_click_events_table_schema():
    conn = sqlite3.connect(DB_PATH)
    columns = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(click_events)")
    }
    conn.close()
    assert columns == {
        "id": "INTEGER",
        "url_id": "INTEGER",
        "clicked_at": "TEXT",
        "referrer": "TEXT",
    }


def _click_events_for(code: str):
    """Return all click event rows for a code, oldest first."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, url_id, clicked_at, referrer FROM click_events "
        "WHERE url_id = (SELECT id FROM urls WHERE custom_alias = ? "
        "OR id = ?) ORDER BY id",
        (code, code),
    ).fetchall()
    conn.close()
    return rows


def test_redirect_records_click_event_with_timestamp_and_referrer():
    before = datetime.now(timezone.utc)
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/event-referrer",
            "custom_alias": "event-referrer",
        },
    )
    assert r.status_code == 200

    r = client.get(
        "/event-referrer",
        headers={"Referer": "https://example.com/some-page"},
        follow_redirects=False,
    )
    assert r.status_code in (307, 308)

    events = _click_events_for("event-referrer")
    assert len(events) == 1
    _, url_id, clicked_at, referrer = events[0]
    assert url_id is not None
    assert referrer == "https://example.com/some-page"
    clicked = datetime.fromisoformat(clicked_at)
    assert clicked.tzinfo is not None
    assert before <= clicked <= datetime.now(timezone.utc)


def test_click_event_without_referrer_stores_null():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/event-no-referrer",
            "custom_alias": "event-no-referrer",
        },
    )
    assert r.status_code == 200

    r = client.get("/event-no-referrer", follow_redirects=False)
    assert r.status_code in (307, 308)

    events = _click_events_for("event-no-referrer")
    assert len(events) == 1
    assert events[0][3] is None


def test_each_redirect_records_its_own_event():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/event-many",
            "custom_alias": "event-many",
        },
    )
    assert r.status_code == 200

    for i in range(3):
        r = client.get(
            "/event-many",
            headers={"Referer": f"https://example.com/page-{i}"},
            follow_redirects=False,
        )
        assert r.status_code in (307, 308)

    events = _click_events_for("event-many")
    assert len(events) == 3
    assert [e[3] for e in events] == [
        "https://example.com/page-0",
        "https://example.com/page-1",
        "https://example.com/page-2",
    ]
    # The total counter on the link row still agrees with the event rows.
    r = client.get("/api/stats/event-many")
    assert r.json()["clicks"] == 3


def test_cached_redirect_records_click_event():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/event-cached",
            "custom_alias": "event-cached",
        },
    )
    assert r.status_code == 200

    # First redirect misses the cache, the second is served from it.
    client.get("/event-cached", follow_redirects=False)
    r = client.get("/event-cached", follow_redirects=False)
    assert r.status_code in (307, 308)

    events = _click_events_for("event-cached")
    assert len(events) == 2


def test_expired_link_records_no_click_event():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/event-expired",
            "custom_alias": "event-expired",
        },
    )
    assert r.status_code == 200

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE urls SET expires_at = ? WHERE custom_alias = ?",
        (past, "event-expired"),
    )
    conn.commit()
    conn.close()

    r = client.get("/event-expired", follow_redirects=False)
    assert r.status_code == 410

    events = _click_events_for("event-expired")
    assert events == []


def _insert_click_events(alias: str, clicked_ats: list[str]) -> None:
    """Insert click event rows with the given ISO timestamps for a link."""
    conn = sqlite3.connect(DB_PATH)
    url_id = conn.execute(
        "SELECT id FROM urls WHERE custom_alias = ?", (alias,)
    ).fetchone()[0]
    for clicked_at in clicked_ats:
        conn.execute(
            "INSERT INTO click_events (url_id, clicked_at, referrer) "
            "VALUES (?, ?, ?)",
            (url_id, clicked_at, None),
        )
    conn.commit()
    conn.close()


def test_per_day_counts_group_clicks_by_day():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/per-day",
            "custom_alias": "per-day",
        },
    )
    assert r.status_code == 200

    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    two_days_ago = today - timedelta(days=2)
    _insert_click_events(
        "per-day",
        [
            datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
            .isoformat(),
            datetime.combine(
                yesterday, datetime.min.time(), tzinfo=timezone.utc
            ).isoformat(),
            datetime.combine(
                yesterday, datetime.min.time(), tzinfo=timezone.utc
            ).isoformat(),
        ],
    )

    r = client.get(
        "/api/stats/per-day/per-day",
        params={
            "start_date": two_days_ago.isoformat(),
            "end_date": today.isoformat(),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["short_code"] == "per-day"
    assert body["start_date"] == two_days_ago.isoformat()
    assert body["end_date"] == today.isoformat()
    assert body["total_clicks"] == 3
    assert body["per_day"] == [
        {"date": two_days_ago.isoformat(), "clicks": 0},
        {"date": yesterday.isoformat(), "clicks": 2},
        {"date": today.isoformat(), "clicks": 1},
    ]


def test_per_day_defaults_to_last_30_days():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/per-day-default",
            "custom_alias": "per-day-default",
        },
    )
    assert r.status_code == 200
    client.get("/per-day-default", follow_redirects=False)

    today = datetime.now(timezone.utc).date()
    r = client.get("/api/stats/per-day-default/per-day")
    assert r.status_code == 200
    body = r.json()
    assert body["start_date"] == (today - timedelta(days=29)).isoformat()
    assert body["end_date"] == today.isoformat()
    assert len(body["per_day"]) == 30
    assert body["total_clicks"] == 1
    assert body["per_day"][-1] == {"date": today.isoformat(), "clicks": 1}
    assert all(entry["clicks"] == 0 for entry in body["per_day"][:-1])


def test_per_day_agrees_with_total_stats():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/per-day-total",
            "custom_alias": "per-day-total",
        },
    )
    assert r.status_code == 200

    for _ in range(3):
        client.get("/per-day-total", follow_redirects=False)

    r = client.get("/api/stats/per-day-total/per-day")
    assert r.status_code == 200
    assert r.json()["total_clicks"] == 3

    r = client.get("/api/stats/per-day-total")
    assert r.json()["clicks"] == 3


def test_per_day_unknown_code_404():
    assert (
        client.get("/api/stats/doesnotexist123/per-day").status_code == 404
    )


def test_per_day_expired_link_410():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/per-day-expired",
            "custom_alias": "per-day-expired",
        },
    )
    assert r.status_code == 200

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE urls SET expires_at = ? WHERE custom_alias = ?",
        (past, "per-day-expired"),
    )
    conn.commit()
    conn.close()

    r = client.get("/api/stats/per-day-expired/per-day")
    assert r.status_code == 410


def test_per_day_rejects_start_after_end():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/per-day-range",
            "custom_alias": "per-day-range",
        },
    )
    assert r.status_code == 200

    r = client.get(
        "/api/stats/per-day-range/per-day",
        params={"start_date": "2026-11-11", "end_date": "2026-11-10"},
    )
    assert r.status_code == 422


def test_per_day_rejects_bad_date_and_wide_range():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/per-day-bad-range",
            "custom_alias": "per-day-bad-range",
        },
    )
    assert r.status_code == 200

    r = client.get(
        "/api/stats/per-day-bad-range/per-day",
        params={"start_date": "2026-02-30", "end_date": "2026-03-01"},
    )
    assert r.status_code == 422

    r = client.get(
        "/api/stats/per-day-bad-range/per-day",
        params={"start_date": "not-a-date", "end_date": "2026-03-01"},
    )
    assert r.status_code == 422

    r = client.get(
        "/api/stats/per-day-bad-range/per-day",
        params={"start_date": "2025-01-01", "end_date": "2026-12-31"},
    )
    assert r.status_code == 422


def test_top_links_orders_by_clicks():
    for alias, clicks in [
        ("top-a", 3),
        ("top-b", 1),
        ("top-c", 5),
    ]:
        r = client.post(
            "/api/shorten",
            json={
                "url": f"https://example.com/{alias}",
                "custom_alias": alias,
            },
        )
        assert r.status_code == 200
        for _ in range(clicks):
            client.get(f"/{alias}", follow_redirects=False)

    r = client.get("/api/stats/top", params={"limit": 100})
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 100
    mine = [link for link in body["links"] if link["short_code"] in ("top-a", "top-b", "top-c")]
    assert [link["short_code"] for link in mine] == ["top-c", "top-a", "top-b"]
    assert [link["clicks"] for link in mine] == [5, 3, 1]
    assert mine[0]["long_url"] == "https://example.com/top-c"


def test_top_links_limit_and_descending_order():
    r = client.get("/api/stats/top", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 2
    assert len(body["links"]) == 2
    clicks = [link["clicks"] for link in body["links"]]
    assert clicks == sorted(clicks, reverse=True)


def test_top_links_default_limit_and_validation():
    r = client.get("/api/stats/top")
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 10
    assert len(body["links"]) <= 10

    assert client.get("/api/stats/top", params={"limit": 0}).status_code == 422
    assert client.get("/api/stats/top", params={"limit": 101}).status_code == 422


def test_top_links_excludes_expired():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/top-expired",
            "custom_alias": "top-expired",
        },
    )
    assert r.status_code == 200
    for _ in range(5):
        client.get("/top-expired", follow_redirects=False)

    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE urls SET expires_at = ? WHERE custom_alias = ?",
        (past, "top-expired"),
    )
    conn.commit()
    conn.close()

    r = client.get("/api/stats/top", params={"limit": 100})
    assert r.status_code == 200
    assert all(
        link["short_code"] != "top-expired" for link in r.json()["links"]
    )


def test_top_links_shows_alias_and_generated_code():
    r = client.post(
        "/api/shorten",
        json={
            "url": "https://example.com/top-alias",
            "custom_alias": "top-alias",
        },
    )
    assert r.status_code == 200
    client.get("/top-alias", follow_redirects=False)

    r = client.post(
        "/api/shorten", json={"url": "https://example.com/top-generated"}
    )
    assert r.status_code == 200
    generated = r.json()["short_code"]
    client.get(f"/{generated}", follow_redirects=False)

    r = client.get("/api/stats/top", params={"limit": 100})
    assert r.status_code == 200
    by_code = {link["short_code"]: link for link in r.json()["links"]}
    assert by_code["top-alias"]["clicks"] == 1
    assert by_code["top-alias"]["long_url"] == "https://example.com/top-alias"
    assert by_code[generated]["clicks"] == 1
    assert by_code[generated]["long_url"] == "https://example.com/top-generated"



