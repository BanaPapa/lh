import os

from fastapi.testclient import TestClient


os.environ["DEMO_MODE"] = "true"
os.environ["KAKAO_REST_API_KEY"] = ""
os.environ["VWORLD_API_KEY"] = ""
os.environ["TAGO_SERVICE_KEY"] = ""

from app.main import app


client = TestClient(app)


def test_health_reports_demo_configuration() -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["demo_mode"] is True


def test_demo_geocode_returns_candidates() -> None:
    geocode = client.get("/api/geocode", params={"query": "판교역"})

    assert geocode.status_code == 200
    body = geocode.json()
    assert body["demo"] is True
    assert body["candidates"]
    candidate = body["candidates"][0]
    assert candidate["coordinates"]["lat"]
    assert candidate["coordinates"]["lng"]


def test_geocode_rejects_short_query() -> None:
    assert client.get("/api/geocode", params={"query": "a"}).status_code == 422
