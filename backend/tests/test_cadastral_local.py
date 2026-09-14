"""전북 연속지적도 로컬 모듈 단위 테스트.

실제 1.1GB shapefile 없이 돈다. 픽스처는 pyshp 로 작은 shapefile 을 그 자리에서
만들어 쓴다. 좌표는 알려진 WGS84 점을 EPSG:5186 으로 정변환해 넣으므로,
모듈이 다시 WGS84 로 역변환하면 원래 값으로 돌아와야 한다(왕복 검증).
"""

from __future__ import annotations

from pathlib import Path

import shapefile
from pyproj import Transformer

import sqlite3

from app.services.cadastral_local import (
    JEONBUK_LAT_MAX,
    JEONBUK_LAT_MIN,
    JEONBUK_LNG_MAX,
    JEONBUK_LNG_MIN,
    RTREE_TABLE,
    SOURCE_EPSG,
    WGS84_EPSG,
    CadastralLocalStore,
    add_spatial_rtree_index,
    build_index,
)


# 전주 인근 실재 좌표대. 변환 왕복 후 전북 범위 재검사를 통과해야 한다.
_JEONJU_LAT = 35.824
_JEONJU_LNG = 127.147

_TO_5186 = Transformer.from_crs(WGS84_EPSG, SOURCE_EPSG, always_xy=True)


def _square_5186(center_lat: float, center_lng: float, half_m: float) -> list[tuple[float, float]]:
    """WGS84 중심 주변의 정사각형을 EPSG:5186(m) 좌표 링으로 만든다."""

    cx, cy = _TO_5186.transform(center_lng, center_lat)
    ring = [
        (cx - half_m, cy - half_m),
        (cx + half_m, cy - half_m),
        (cx + half_m, cy + half_m),
        (cx - half_m, cy + half_m),
        (cx - half_m, cy - half_m),  # 닫힘
    ]
    return ring


def _write_fixture_shp(directory: Path) -> Path:
    """실측한 컬럼 구성(PNU, JIBUN, BCHK, COL_ADM_SE)을 흉내낸 작은 shp 생성."""

    base = directory / "fixture_cadastral"
    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON, encoding="euc-kr")
    writer.field("SGG_OID", "N", 33, 0)
    writer.field("JIBUN", "C", 100)
    writer.field("BCHK", "C", 1)
    writer.field("PNU", "C", 19)
    writer.field("COL_ADM_SE", "C", 5)

    # 필지 A: 전주 중심, 20m 사각형
    writer.poly([_square_5186(_JEONJU_LAT, _JEONJU_LNG, 20.0)])
    writer.record(1, "1183 답", "0", "5211110100101830000", "52111")

    # 필지 B: 살짝 떨어진 곳, 10m 사각형(더 작음 → 겹칠 때 우선)
    writer.poly([_square_5186(_JEONJU_LAT + 0.001, _JEONJU_LNG + 0.001, 10.0)])
    writer.record(2, "1184-1 대", "0", "5211110100101840001", "52111")

    writer.close()
    # .prj 는 pyshp 가 안 쓰므로 굳이 만들지 않는다. 인코딩은 명시적으로 전달된다.
    return Path(str(base) + ".shp")


def _build_store(tmp_path: Path) -> CadastralLocalStore:
    shp = _write_fixture_shp(tmp_path)
    db = tmp_path / "cadastral.sqlite"
    count = build_index(shp, db)
    assert count == 2
    return CadastralLocalStore(shp_path=shp, db_path=db)


# --- 좌표 변환 왕복 + 전북 범위 -------------------------------------------------


def test_epsg_constant_is_5186() -> None:
    assert SOURCE_EPSG == "EPSG:5186"


def test_transform_roundtrip_and_jeonbuk_range(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    parcel = store.parcel_by_pnu("5211110100101830000")
    assert parcel is not None

    # 왕복: 5186 으로 넣은 전주 좌표가 다시 WGS84 로 돌아왔는지(중심 근사).
    lat = sum(c.lat for c in parcel.ring) / len(parcel.ring)
    lng = sum(c.lng for c in parcel.ring) / len(parcel.ring)
    assert abs(lat - _JEONJU_LAT) < 1e-4
    assert abs(lng - _JEONJU_LNG) < 1e-4

    # 룰북 §7 ① 전북 범위 재검사.
    assert parcel.in_jeonbuk is True
    assert JEONBUK_LAT_MIN <= lat <= JEONBUK_LAT_MAX
    assert JEONBUK_LNG_MIN <= lng <= JEONBUK_LNG_MAX


def test_ring_is_closed_and_min_four_points(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    parcel = store.parcel_by_pnu("5211110100101830000")
    assert parcel is not None
    assert len(parcel.ring) >= 4
    assert parcel.ring[0].lat == parcel.ring[-1].lat
    assert parcel.ring[0].lng == parcel.ring[-1].lng


def test_area_is_computed_in_square_meters(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    parcel = store.parcel_by_pnu("5211110100101830000")
    assert parcel is not None
    # 20m half → 40m x 40m = 1600 m². 투영 왜곡으로 약간의 오차 허용.
    assert 1500 < parcel.area_m2 < 1700


# --- PNU 조회 -----------------------------------------------------------------


def test_parcel_by_pnu_hit_and_miss(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    assert store.parcel_by_pnu("5211110100101840001") is not None
    assert store.parcel_by_pnu("0000000000000000000") is None
    assert store.parcel_by_pnu("") is None


# --- 점 포함 조회 --------------------------------------------------------------


def test_parcel_at_contains_point(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    parcel = store.parcel_at(_JEONJU_LAT, _JEONJU_LNG)
    assert parcel is not None
    assert parcel.pnu == "5211110100101830000"


def test_parcel_at_outside_returns_none(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    # 서울쯤 — 어떤 필지 bbox 에도 안 든다.
    assert store.parcel_at(37.5665, 126.9780) is None


# --- 파일 부재 시 미적재 상태 --------------------------------------------------


def test_missing_source_reports_status_not_exception(tmp_path: Path) -> None:
    store = CadastralLocalStore(
        shp_path=tmp_path / "does_not_exist.shp",
        db_path=tmp_path / "does_not_exist.sqlite",
    )
    status = store.status()
    assert status.available is False
    assert status.db_present is False
    assert status.parcel_count == 0
    assert "미적재" in status.note
    # 조회도 예외 없이 None.
    assert store.parcel_by_pnu("5211110100101830000") is None
    assert store.parcel_at(_JEONJU_LAT, _JEONJU_LNG) is None


def test_shp_present_but_not_indexed_status(tmp_path: Path) -> None:
    shp = _write_fixture_shp(tmp_path)
    store = CadastralLocalStore(shp_path=shp, db_path=tmp_path / "not_built.sqlite")
    status = store.status()
    assert status.shp_present is True
    assert status.db_present is False
    assert status.available is False
    assert "build_index" in status.note


def test_status_available_after_build(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    status = store.status()
    assert status.available is True
    assert status.db_present is True
    assert status.parcel_count == 2


# --- 공간 인덱스(R-Tree) 성능 수정 회귀 --------------------------------------
#
# 실측(2026-08-29): 기존 ix_bbox 복합 인덱스는 EXPLAIN QUERY PLAN 상
# `SEARCH parcels USING INDEX ix_bbox (min_lat<?)` 로 첫 컬럼만 실제 탐색에
# 쓰이고 나머지 3조건은 그 넓은 범위 안에서 순차 필터된다(390만 필지 중 245만건
# 스캔, 조회 1건에 약 1.1~1.2초). R-Tree 가상테이블로 같은 조회가 1~2ms 로
# 줄었다(실측 860~3300배). 아래 테스트는 (1) 결과가 bbox 경로와 동일하고
# (2) 실제 인덱스가 쓰이며 (3) 기존 산출물에도 재빌드 없이 추가할 수 있음을
# 검증한다.


def _drop_rtree(db_path: Path) -> None:
    """R-Tree 없이 만들어진 예전 산출물을 흉내낸다(마이그레이션 테스트용)."""

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(f"DROP TABLE IF EXISTS {RTREE_TABLE}")
        conn.commit()
    finally:
        conn.close()


def test_build_index_creates_rtree_table(tmp_path: Path) -> None:
    shp = _write_fixture_shp(tmp_path)
    db = tmp_path / "cadastral.sqlite"
    build_index(shp, db)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (RTREE_TABLE,),
        ).fetchone()
        assert row[0] == 1
        count = conn.execute(f"SELECT COUNT(*) FROM {RTREE_TABLE}").fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_parcel_at_query_plan_uses_rtree_index(tmp_path: Path) -> None:
    store = _build_store(tmp_path)
    conn = store._connect()
    assert conn is not None
    plan = list(
        conn.execute(
            f"""
            EXPLAIN QUERY PLAN
            SELECT p.* FROM {RTREE_TABLE} r
            JOIN parcels p ON p.rowid = r.id
            WHERE r.min_lat <= ? AND r.max_lat >= ?
              AND r.min_lng <= ? AND r.max_lng >= ?
            """,
            (_JEONJU_LAT, _JEONJU_LAT, _JEONJU_LNG, _JEONJU_LNG),
        )
    )
    detail = " | ".join(str(dict(row)["detail"]) for row in plan)
    assert "VIRTUAL TABLE INDEX" in detail


def test_parcel_at_result_identical_with_and_without_rtree(tmp_path: Path) -> None:
    """R-Tree 유무와 무관하게 parcel_at()·sole_parcel_at() 결과가 같아야 한다.

    동작을 바꾸지 않는다는 원칙의 직접 검증이다. R-Tree 가 있는 새 산출물과
    (모듈이 실제로 만드는 상태) 없는 예전 산출물(수동으로 재현) 양쪽에서 같은
    점을 조회해 완전히 같은 결과가 나오는지 비교한다.
    """

    shp = _write_fixture_shp(tmp_path)
    db = tmp_path / "cadastral.sqlite"
    build_index(shp, db)

    with_rtree = CadastralLocalStore(shp_path=shp, db_path=db)
    parcel_with = with_rtree.parcel_at(_JEONJU_LAT, _JEONJU_LNG)
    sole_with = with_rtree.sole_parcel_at(_JEONJU_LAT, _JEONJU_LNG)
    with_rtree.close()

    _drop_rtree(db)
    without_rtree = CadastralLocalStore(shp_path=shp, db_path=db)
    assert without_rtree._connect() is not None
    assert without_rtree._has_rtree is False
    parcel_without = without_rtree.parcel_at(_JEONJU_LAT, _JEONJU_LNG)
    sole_without = without_rtree.sole_parcel_at(_JEONJU_LAT, _JEONJU_LNG)
    without_rtree.close()

    assert parcel_with is not None and parcel_without is not None
    assert parcel_with.pnu == parcel_without.pnu
    assert parcel_with.area_m2 == parcel_without.area_m2
    assert sole_with == sole_without

    # bbox 밖(서울)도 양쪽 다 None 이어야 한다.
    outside_with = CadastralLocalStore(shp_path=shp, db_path=db)
    # (rtree 는 이미 없는 상태의 db 를 그대로 재사용)
    assert outside_with.parcel_at(37.5665, 126.9780) is None
    outside_with.close()


def test_add_spatial_rtree_index_migrates_without_full_rebuild(tmp_path: Path) -> None:
    """기존 산출물에 shp 재파싱 없이 R-Tree 만 추가할 수 있어야 한다."""

    shp = _write_fixture_shp(tmp_path)
    db = tmp_path / "cadastral.sqlite"
    build_index(shp, db)  # 이제는 build_index 도 rtree 를 만들지만, 여기서는
    _drop_rtree(db)  # 예전 산출물(마이그레이션 전) 상태로 되돌린다.

    created = add_spatial_rtree_index(db)
    assert created is True

    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (RTREE_TABLE,),
        ).fetchone()
        assert row[0] == 1
        count = conn.execute(f"SELECT COUNT(*) FROM {RTREE_TABLE}").fetchone()[0]
        assert count == 2
        # shp 는 다시 읽지 않았다 — parcels 테이블 자체는 건드리지 않는 추가 전용
        # 마이그레이션이다.
        parcel_count = conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
        assert parcel_count == 2
    finally:
        conn.close()

    # 멱등: 이미 있으면 다시 만들지 않고 False.
    assert add_spatial_rtree_index(db) is False

    store = CadastralLocalStore(shp_path=shp, db_path=db)
    parcel = store.parcel_at(_JEONJU_LAT, _JEONJU_LNG)
    assert parcel is not None
    assert parcel.pnu == "5211110100101830000"
    store.close()


def test_connection_is_reused_not_reopened_per_query(tmp_path: Path) -> None:
    """읽기전용 연결이 조회마다 새로 열리지 않고 재사용되는지 확인한다."""

    store = _build_store(tmp_path)
    conn_first = store._connect()
    conn_second = store._connect()
    assert conn_first is conn_second
    store.parcel_at(_JEONJU_LAT, _JEONJU_LNG)
    assert store._connect() is conn_first
