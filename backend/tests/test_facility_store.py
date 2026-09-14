"""인허가 로컬 캐시 — extra 컬럼(판정 필터용 부가 컬럼) 저장·이관.

구 스키마 DB(extra 없음)를 조회만으로는 바꾸지 않고, 적재 때만 컬럼을 더한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models import Coordinates
from app.services.facility_store import FacilityStore
from app.services.localdata import LocalDataRecord

CENTER = Coordinates(lat=35.82, lng=127.10)


def _record(name: str, extra: dict[str, str] | None = None) -> LocalDataRecord:
    return LocalDataRecord(
        dataset_key="high_pressure_gas",
        record_id=f"id-{name}",
        name=name,
        address="전북 전주시 1",
        road_address="",
        coordinates=CENTER,
        status="영업/정상",
        category="제조",
        extra=extra or {},
    )


def _legacy_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE facilities (
            dataset_key TEXT NOT NULL, record_id TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
            road_address TEXT NOT NULL DEFAULT '', lat REAL NOT NULL, lng REAL NOT NULL,
            status TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (dataset_key, record_id)
        );
        CREATE TABLE sync_state (
            dataset_key TEXT PRIMARY KEY, synced_at TEXT NOT NULL, record_count INTEGER NOT NULL
        );
        INSERT INTO facilities VALUES ('high_pressure_gas','old','구적재','','',35.82,127.10,'영업/정상','제조');
        INSERT INTO sync_state VALUES ('high_pressure_gas','2026-08-03T00:00:00+00:00',1);
        """
    )
    connection.commit()
    connection.close()


def _columns(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute("PRAGMA table_info(facilities)")}
    finally:
        connection.close()


def test_extra_round_trips(tmp_path: Path) -> None:
    store = FacilityStore(tmp_path / "f.db")
    store.replace_dataset("high_pressure_gas", [_record("냉동", {"MNFTR_SE_NM": "냉동"})])
    found = store.facilities_around(CENTER, 100, ["high_pressure_gas"])
    assert found[0].extra == {"MNFTR_SE_NM": "냉동"}


def test_legacy_schema_reads_without_touching_file(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    store = FacilityStore(path)
    found = store.facilities_around(CENTER, 100, ["high_pressure_gas"])
    assert found[0].name == "구적재"
    assert found[0].extra == {}
    # 조회만으로는 스키마를 바꾸지 않는다.
    assert "extra" not in _columns(path)


def test_legacy_schema_migrates_on_write(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    store = FacilityStore(path)
    store.replace_dataset("high_pressure_gas", [_record("새적재", {"BLDG_USG_NM": "체육시설"})])
    assert "extra" in _columns(path)
    found = store.facilities_around(CENTER, 100, ["high_pressure_gas"])
    assert [f.name for f in found] == ["새적재"]
    assert found[0].extra == {"BLDG_USG_NM": "체육시설"}
