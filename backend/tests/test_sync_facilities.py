"""인허가 원장 기동 시 자동 갱신 — 오래된 데이터셋만 받고, 실패분은 한 번 더 받는다."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import Settings
from app.models import Coordinates
from app.services.facility_store import FacilityStore
from app.services.localdata import DATASETS, LocalDataRecord, LocalDataSet
from app.sync_facilities import refresh_stale, stale_datasets

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def _record(dataset_key: str) -> LocalDataRecord:
    return LocalDataRecord(
        dataset_key=dataset_key,
        record_id=f"{dataset_key}-1",
        name="시설",
        address="전북 전주시 1",
        road_address="",
        coordinates=Coordinates(lat=35.82, lng=127.10),
        status="영업/정상",
        category="",
        extra={},
    )


def _stamp(path: Path, dataset_key: str, synced_at: datetime) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT OR REPLACE INTO sync_state VALUES (?,?,?)",
        (dataset_key, synced_at.isoformat(), 1),
    )
    connection.commit()
    connection.close()


class FakeClient:
    """fail 에 든 데이터셋은 fail_times 번 실패한 뒤 성공한다."""

    def __init__(self, fail: set[str] = frozenset(), fail_times: int = 1) -> None:
        self.calls: list[str] = []
        self._remaining = {key: fail_times for key in fail}

    async def fetch_all(self, dataset: LocalDataSet) -> list[LocalDataRecord]:
        self.calls.append(dataset.key)
        if self._remaining.get(dataset.key, 0) > 0:
            self._remaining[dataset.key] -= 1
            raise RuntimeError("요청이 과다합니다")
        return [_record(dataset.key)]


def test_stale_datasets_picks_missing_and_old_only(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    store = FacilityStore(db)
    fresh, old = DATASETS[0], DATASETS[1]
    _stamp(db, fresh.key, NOW - timedelta(hours=2))
    _stamp(db, old.key, NOW - timedelta(hours=25))

    stale = {dataset.key for dataset in stale_datasets(store, now=NOW)}

    assert fresh.key not in stale
    assert old.key in stale
    # 한 번도 안 받은 데이터셋도 대상이다.
    assert stale == {dataset.key for dataset in DATASETS} - {fresh.key}


@pytest.mark.asyncio
async def test_refresh_stale_skips_fresh_and_retries_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.sync_facilities.PAUSE_BETWEEN_DATASETS_SECONDS", 0)
    db = tmp_path / "f.db"
    store = FacilityStore(db)
    now = datetime.now(UTC)
    for dataset in DATASETS[1:]:
        _stamp(db, dataset.key, now)
    target = DATASETS[0]
    client = FakeClient(fail={target.key})

    await refresh_stale(
        Settings(demo_mode=False), store=store, client=client, retry_after_seconds=0  # type: ignore[arg-type]
    )

    # 최신 데이터셋은 건드리지 않고, 실패한 한 종만 재시도해 받았다.
    assert client.calls == [target.key, target.key]
    assert target.key in store.ready_datasets()


@pytest.mark.asyncio
async def test_refresh_stale_keeps_old_rows_when_download_keeps_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.sync_facilities.PAUSE_BETWEEN_DATASETS_SECONDS", 0)
    db = tmp_path / "f.db"
    store = FacilityStore(db)
    target = DATASETS[0]
    store.replace_dataset(target.key, [_record(target.key)])
    _stamp(db, target.key, datetime.now(UTC) - timedelta(days=3))
    for dataset in DATASETS[1:]:
        _stamp(db, dataset.key, datetime.now(UTC))

    await refresh_stale(
        Settings(demo_mode=False),
        store=store,
        client=FakeClient(fail={target.key}, fail_times=5),  # type: ignore[arg-type]
        retry_after_seconds=0,
    )

    # 받기에 실패해도 어제 원장은 지우지 않는다.
    assert target.key in store.ready_datasets()
