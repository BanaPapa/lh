"""접속자별 심사 시작 제한."""

from __future__ import annotations

from app import rate_limit


def setup_function() -> None:
    rate_limit._hits.clear()


def test_per_minute_limit_then_recovers() -> None:
    assert rate_limit.check("1.1.1.1", 0.0, per_minute=2, per_day=0) == ""
    assert rate_limit.check("1.1.1.1", 1.0, per_minute=2, per_day=0) == ""
    assert "1분에 2건" in rate_limit.check("1.1.1.1", 2.0, per_minute=2, per_day=0)
    # 다른 접속자는 따로 센다.
    assert rate_limit.check("2.2.2.2", 2.0, per_minute=2, per_day=0) == ""
    # 1분이 지나면 다시 된다.
    assert rate_limit.check("1.1.1.1", 61.0, per_minute=2, per_day=0) == ""


def test_per_day_limit() -> None:
    for i in range(3):
        assert rate_limit.check("3.3.3.3", i * 100.0, per_minute=0, per_day=3) == ""
    assert "하루 3건" in rate_limit.check("3.3.3.3", 400.0, per_minute=0, per_day=3)
    assert rate_limit.check("3.3.3.3", rate_limit.DAY + 1.0, per_minute=0, per_day=3) == ""


def test_middleware_only_counts_screening_starts(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    monkeypatch.setattr(settings, "rate_limit_per_day", 0)
    client = TestClient(app)
    headers = {"x-forwarded-for": "9.9.9.9"}

    # 헬스체크·조회는 세지 않는다.
    for _ in range(3):
        assert client.get("/api/health", headers=headers).status_code == 200
    # 심사 시작은 두 번째부터 막힌다(본문이 틀려도 제한이 먼저다).
    client.post("/api/screening/jobs", json={}, headers=headers)
    blocked = client.post("/api/screening/jobs", json={}, headers=headers)
    assert blocked.status_code == 429
