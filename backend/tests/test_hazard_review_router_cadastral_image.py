"""지적도 이미지 프록시 — 영역·크기 검사와 이미지 응답."""

from __future__ import annotations

from fastapi.testclient import TestClient


class _FakeVWorld:
    enabled = True

    async def cadastral_image(self, south, west, north, east, width, height):
        return b"\x89PNG fake"


def _client():
    from app.hazard_review.router import get_vworld_client
    from app.main import app

    app.dependency_overrides[get_vworld_client] = lambda: _FakeVWorld()
    return TestClient(app), app


def test_returns_png_with_cache_header() -> None:
    client, app = _client()
    try:
        r = client.get(
            "/api/hazard-review/cadastral-image",
            params={"south": 35.82, "west": 127.118, "north": 35.824, "east": 127.123, "width": 800, "height": 640},
        )
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert "max-age" in r.headers["cache-control"]
        assert r.content.startswith(b"\x89PNG")
    finally:
        app.dependency_overrides.clear()


def test_rejects_too_wide_area() -> None:
    client, app = _client()
    try:
        r = client.get(
            "/api/hazard-review/cadastral-image",
            params={"south": 35.0, "west": 127.0, "north": 35.5, "east": 127.5, "width": 800, "height": 640},
        )
        assert r.status_code == 422
    finally:
        app.dependency_overrides.clear()
