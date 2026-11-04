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
