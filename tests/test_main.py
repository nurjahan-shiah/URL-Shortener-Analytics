from fastapi.testclient import TestClient

from app.main import app

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
