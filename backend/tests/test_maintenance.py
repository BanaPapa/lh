"""자료 업데이트 중 표시 — 버킷 상태 파일 해석과 새 심사 보류."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import maintenance
from app.config import Settings, get_settings
from app.main import app

NOW = datetime(2026, 10, 1, 0, 5, tzinfo=UTC)  # 09:05 KST


def flag(minutes_left: int) -> dict:
    return {
        "started_at": (NOW - timedelta(minutes=5)).isoformat(),
        "expected_end": (NOW + timedelta(minutes=15)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=minutes_left)).isoformat(),
    }


def test_flag_marks_updating_with_kst_times() -> None:
    state = maintenance.state_from_flag(flag(40), NOW)
    assert state.updating is True
    assert state.started_at == "09:00"
    assert state.expected_end == "09:20"
    assert "업데이트" in state.message


def test_expired_or_broken_flag_is_ignored() -> None:
    # 빌드가 죽어 파일이 남아도 만료 뒤에는 앱을 막지 않는다.
    assert maintenance.state_from_flag(flag(-1), NOW).updating is False
    assert maintenance.state_from_flag({"expires_at": "언젠가"}, NOW).updating is False


@pytest.mark.asyncio
async def test_no_bucket_means_not_updating_and_read_failure_does_not_block(monkeypatch) -> None:
    maintenance.reset_cache()
    assert (await maintenance.maintenance_state(Settings(_env_file=None))).updating is False

    async def boom(bucket: str):
        raise RuntimeError("metadata server unreachable")

    monkeypatch.setattr(maintenance, "_read_flag", boom)
    state = await maintenance.maintenance_state(Settings(_env_file=None, snapshot_bucket="b"))
    assert state.updating is False
    maintenance.reset_cache()


def test_status_endpoint_and_start_is_held_while_updating() -> None:
    maintenance.reset_cache()
    until = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    settings = Settings(_env_file=None, demo_mode=True, maintenance_until=until)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        client = TestClient(app)
        body = client.get("/api/status/maintenance").json()
        assert body["updating"] is True and body["active_jobs"] == 0
    finally:
        app.dependency_overrides.pop(get_settings, None)
        maintenance.reset_cache()
