"""인허가 시설 로컬 캐시.

인허가 API는 지역 필터가 없어 사업지 주변만 뽑아올 수 없다. 전량을 여기에
적재해 두고 조회는 로컬에서 한다. 검토 한 건이 수천 번의 외부 호출을 하는 일이
없어야 한다.

SQLite 를 쓰는 이유는 별도 서버 없이 파일 하나로 끝나고, 위경도 인덱스만으로도
사각형 조회가 충분히 빠르기 때문이다.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.localdata import LocalDataRecord


DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "facilities.db"

METERS_PER_DEGREE_LAT = 111_320

SCHEMA = """
CREATE TABLE IF NOT EXISTS facilities (
    dataset_key   TEXT NOT NULL,
    record_id     TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    address       TEXT NOT NULL DEFAULT '',
    road_address  TEXT NOT NULL DEFAULT '',
    lat           REAL NOT NULL,
    lng           REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT '',
    category      TEXT NOT NULL DEFAULT '',
    extra         TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (dataset_key, record_id)
);
CREATE INDEX IF NOT EXISTS idx_facilities_lat_lng ON facilities (lat, lng);
CREATE TABLE IF NOT EXISTS sync_state (
    dataset_key  TEXT PRIMARY KEY,
    synced_at    TEXT NOT NULL,
    record_count INTEGER NOT NULL
);
"""


class StoredFacility(NamedTuple):
    dataset_key: str
    record_id: str
    name: str
    address: str
    road_address: str
    coordinates: Coordinates
    status: str
    category: str
    distance_m: float
    # 판정 필터용 부가 컬럼(localdata.EXTRA_FIELDS). 구 적재분은 빈 dict.
    extra: dict[str, str] = {}


def _dump_extra(extra: object) -> str:
    if not extra or not isinstance(extra, dict):
        return ""
    return json.dumps(extra, ensure_ascii=False, sort_keys=True)


def _load_extra(text: object) -> dict[str, str]:
    if not text or not isinstance(text, str):
        return {}
    try:
        loaded = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(k): str(v) for k, v in loaded.items()}


class SyncState(NamedTuple):
    dataset_key: str
    synced_at: datetime
    record_count: int


class FacilityStore:
    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """구 스키마(extra 컬럼 없음) DB 에 컬럼을 더한다.

        읽기 경로에서는 부르지 않는다 — 저장소에 든 facilities.db 를 조회만으로
        바꾸면 git 작업트리가 매번 더러워진다. 적재(replace_dataset) 때만 옮긴다.
        구 스키마 행은 extra 가 빈 dict 로 읽힌다.
        """

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(facilities)")
        }
        if "extra" not in columns:
            connection.execute(
                "ALTER TABLE facilities ADD COLUMN extra TEXT NOT NULL DEFAULT ''"
            )

    @property
    def available(self) -> bool:
        """한 건이라도 적재돼 있어야 조회 원천으로 인정한다."""

        return any(state.record_count > 0 for state in self.sync_states())

    def ready_datasets(self) -> set[str]:
        """실제로 레코드가 적재된 데이터셋 키 집합.

        원천 준비 판정은 전역이 아니라 데이터셋별이어야 한다. lodgings 하나만
        적재됐다고 gas_station·공장 카테고리까지 "연결됨"으로 보면 조회조차 안 한
        종류에 '충돌 없음'이 나온다(룰북 §11 위반).
        """

        return {
            state.dataset_key
            for state in self.sync_states()
            if state.record_count > 0
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def replace_dataset(
        self,
        dataset_key: str,
        records: Iterable[LocalDataRecord],
    ) -> int:
        """데이터셋을 통째로 갈아끼운다.

        인허가 데이터는 폐업으로 사라지는 행이 있어 증분 갱신이 위험하다.
        전량 교체가 단순하고 안전하다.
        """

        rows = [
            (
                record.dataset_key,
                record.record_id,
                record.name,
                record.address,
                record.road_address,
                record.coordinates.lat,
                record.coordinates.lng,
                record.status,
                record.category,
                _dump_extra(getattr(record, "extra", None)),
            )
            for record in records
        ]
        with self._connect() as connection:
            self._migrate(connection)
            connection.execute(
                "DELETE FROM facilities WHERE dataset_key = ?", (dataset_key,)
            )
            connection.executemany(
                "INSERT OR REPLACE INTO facilities "
                "(dataset_key, record_id, name, address, road_address, "
                " lat, lng, status, category, extra) VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            connection.execute(
                "INSERT OR REPLACE INTO sync_state "
                "(dataset_key, synced_at, record_count) VALUES (?,?,?)",
                (dataset_key, datetime.now(UTC).isoformat(), len(rows)),
            )
        return len(rows)

    def facilities_around(
        self,
        center: Coordinates,
        radius_m: float,
        dataset_keys: Sequence[str],
    ) -> list[StoredFacility]:
        """반경 안의 시설. 사각형으로 좁힌 뒤 실제 거리로 다시 거른다."""

        if not dataset_keys:
            return []
        delta_lat = radius_m / METERS_PER_DEGREE_LAT
        scale = max(math.cos(math.radians(center.lat)), 0.2)
        delta_lng = radius_m / (METERS_PER_DEGREE_LAT * scale)
        placeholders = ",".join("?" for _ in dataset_keys)
        query = (
            "SELECT * FROM facilities WHERE dataset_key IN "
            f"({placeholders}) AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?"
        )
        params = (
            *dataset_keys,
            center.lat - delta_lat,
            center.lat + delta_lat,
            center.lng - delta_lng,
            center.lng + delta_lng,
        )
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()

        found: list[StoredFacility] = []
        for row in rows:
            coordinates = Coordinates(lat=row["lat"], lng=row["lng"])
            distance = haversine_meters(center, coordinates)
            if distance > radius_m:
                continue
            found.append(
                StoredFacility(
                    dataset_key=row["dataset_key"],
                    record_id=row["record_id"],
                    name=row["name"],
                    address=row["address"],
                    road_address=row["road_address"],
                    coordinates=coordinates,
                    status=row["status"],
                    category=row["category"],
                    distance_m=distance,
                    extra=_load_extra(row["extra"] if "extra" in row.keys() else ""),
                )
            )
        found.sort(key=lambda item: item.distance_m)
        return found

    def dataset_records(self, dataset_key: str) -> list[StoredFacility]:
        """데이터셋 하나의 전체 레코드(거리 0). 전량 캐시형 원천의 재적재용."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM facilities WHERE dataset_key = ?", (dataset_key,)
            ).fetchall()
        return [
            StoredFacility(
                dataset_key=row["dataset_key"],
                record_id=row["record_id"],
                name=row["name"],
                address=row["address"],
                road_address=row["road_address"],
                coordinates=Coordinates(lat=row["lat"], lng=row["lng"]),
                status=row["status"],
                category=row["category"],
                distance_m=0.0,
                extra=_load_extra(row["extra"] if "extra" in row.keys() else ""),
            )
            for row in rows
        ]

    def sync_state_for(self, dataset_key: str) -> SyncState | None:
        return next(
            (state for state in self.sync_states() if state.dataset_key == dataset_key),
            None,
        )

    def sync_states(self) -> list[SyncState]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM sync_state").fetchall()
        return [
            SyncState(
                dataset_key=row["dataset_key"],
                synced_at=datetime.fromisoformat(row["synced_at"]),
                record_count=row["record_count"],
            )
            for row in rows
        ]

    def latest_sync(self) -> datetime | None:
        states = self.sync_states()
        return max((state.synced_at for state in states), default=None)
