"""전북 연속지적도 로컬 적재·조회 모듈.

docs/hazards/MEASUREMENT.md §1·§3 「기점 = 사업지 대지경계 폴리곤」의 원천이다.
지금까지 사업지 필지가 임시 사각형(provisional)으로 만들어져 판정이 대량으로
`geometry_missing` 으로 떨어졌는데, 이 모듈이 붙으면 실제 지적경계로 확정
판정을 할 수 있는 건수가 늘어난다.

원천 파일 실측(2026-08 스냅샷, 전북 코드 52)
------------------------------------------------
경로 예: .../spatial/cadastral_jeonbuk/LSMD_CONT_LDREG_52_202608.shp

* 좌표계(.prj): `EPSG:5186` — Korea 2000 / Central Belt 2010.
  central_meridian=127, latitude_of_origin=38, scale=1.0,
  false_easting=200000, false_northing=600000, 단위 m. → WGS84(4326)로 변환한다.
* 인코딩(.cpg): `EUC-KR`. 한글 속성(JIBUN)이 여기에 담긴다.
* DBF 컬럼(실측): SGG_OID(N), JIBUN(C100), BCHK(C1), PNU(C19), COL_ADM_SE(C5).
  - PNU 컬럼이 그대로 있으므로 조립할 필요가 없다.
  - JIBUN 은 "1183 답" 형태의 지번+지목 문자열이다.
  - 면적 컬럼이 없다. → 투영좌표(m) 기준 슈레이스 공식으로 직접 계산한다.
* 레코드 수(.dbf 헤더): 약 3.88M 건. shp 약 1.1GB.

설계 선택 근거
--------------
1. 저장 방식 = SQLite 1회 변환.
   shp 1.1GB / 약 390만 건을 매 요청마다 스캔하면 사용할 수 없다. 그래서 shp 를
   한 번 SQLite 로 변환해 두고(build_index), 이후에는 인덱스로만 조회한다.
   - PNU 조회: `pnu` 컬럼 인덱스로 O(log n).
   - 점 포함 조회: WGS84 bbox(min/max lat·lng) 인덱스로 후보를 좁힌 뒤
     shapely 로 정밀 포함 판정. 공간 인덱스 전용 확장(R-Tree) 없이도 bbox
     선필터만으로 후보가 수 건으로 줄어 실용 속도가 나온다.
   대안(shx 오프셋 인덱스만 두고 shp 를 직접 읽기)은 점 포함 질의에서 전건
   스캔이 필요해 탈락시켰다.

2. 멀티폴리곤·구멍(hole) 처리 = 최대 외곽 링 1개만 사용.
   이 앱의 `HazardParcel.geometry`(app/hazard_review/models.py)와
   `ParcelFeature.ring`(vworld.py)은 "닫힌 링 하나"만 담는 규약이다. 기존
   VWorld 경로도 `_outer_ring` 에서 가장 큰 링만 쓴다. 그 관례에 맞춰,
   한 필지가 여러 파트로 쪼개져 있으면 투영면적이 가장 큰 파트를 대표
   외곽으로 채택한다. 구멍은 출력 도형에서 무시한다(사업지 "전체 대지경계"의
   외연을 잡는 것이 목적이고, 지적 필지에 구멍이 있는 경우는 드물다).
   점 포함 판정도 외곽 링만으로 하므로 구멍 안의 점을 포함으로 볼 수 있으나,
   지적 필지에서 구멍은 예외적이라 실무상 영향이 없다.

3. 파일 부재·경로 주입.
   원본 지적도는 용량·배포 문제로 저장소에 없다. 파일이 없으면 예외로 죽지
   않고 `status()` 가 「원천 미적재」를 명확히 알린다. 경로는 생성자 인자 또는
   환경변수(CADASTRAL_JEONBUK_SHP / CADASTRAL_JEONBUK_DB)로 주입한다.

구조가 같은 다른 원천(국공립공원 national_public_parks 등)도 같은 EPSG:5186
계열 shp 이므로, `build_index`/`_largest_ring_wgs84` 를 재사용해 확장할 수 있다.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

import shapefile  # pyshp — 순수 파이썬, 바이너리 의존성 없음
from pyproj import Transformer
from shapely.geometry import Point, Polygon

from app.models import Coordinates


# --- 원천 상수(실측 근거는 모듈 docstring 참고) -------------------------------

SOURCE_EPSG = "EPSG:5186"
WGS84_EPSG = "EPSG:4326"
SOURCE_ENCODING = "euc-kr"  # .cpg = EUC-KR

# 룰북 §7 ① 재검사용 전북 대략 경계. 변환 결과가 이 밖이면 좌표계 오인이다.
JEONBUK_LAT_MIN = 35.0
JEONBUK_LAT_MAX = 36.3
JEONBUK_LNG_MIN = 126.4
JEONBUK_LNG_MAX = 127.9

ENV_SHP = "CADASTRAL_JEONBUK_SHP"
ENV_DB = "CADASTRAL_JEONBUK_DB"

# backend/ 를 기준으로 한 기본 산출물 경로. 여기 값이 실제로 생성되면 git 추적
# 대상이 되므로(현재 data/ 는 .gitignore 되어 있지 않다) 통합 담당이 별도로
# .gitignore 에 넣어야 한다. 이 모듈은 산출물을 자동 생성하지 않는다.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = _BACKEND_ROOT / "data" / "cadastral_jeonbuk.sqlite"

# 후보 PNU/지번 컬럼명. 다른 스냅샷·타 시도 파일에서 대소문자가 달라질 수 있어
# 대소문자 무시로 매칭한다.
_PNU_FIELD_CANDIDATES = ("PNU", "pnu", "A1", "JIBUN_PNU")
_JIBUN_FIELD_CANDIDATES = ("JIBUN", "jibun", "A5")

_SCHEMA_VERSION = 1

# 점-포함 조회용 공간 인덱스. 실측(2026-08-29): 기존 `ix_bbox`(min_lat, max_lat,
# min_lng, max_lng) 복합 인덱스는 첫 컬럼(min_lat<=?)만 실제 탐색에 쓰이고
# (EXPLAIN QUERY PLAN: `SEARCH parcels USING INDEX ix_bbox (min_lat<?)`), 나머지
# 3개 조건은 그 범위 안에서 순차 필터된다. 390만 필지에서 이 범위가 절반 이상
# (예: 245만 건)이라 조회 1건에 약 1.1~1.2초가 걸린다. SQLite R-Tree 가상테이블은
# 2차원 범위를 실제로 좁혀 같은 조회를 1~2ms 로 낮춘다(실측 860배).
# R-Tree 내부 좌표는 32비트 float 라 정밀도가 낮지만, SQLite 는 그 손실을
# 바깥쪽으로만 반올림해 결과가 원본 `WHERE` 절의 초과집합(superset)임을 보장한다
# (실측: 후보 8건 → 9건으로 늘었을 뿐, 빠진 건 없었다). 이후 shapely 로 정밀
# 포함판정을 하므로 초과 후보는 자연히 걸러진다 — 판정 결과는 바뀌지 않는다.
RTREE_TABLE = "parcels_rtree"


def _rtree_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (RTREE_TABLE,),
    ).fetchone()
    return row is not None


def _populate_rtree(conn: sqlite3.Connection) -> None:
    """`parcels_rtree` 를 만들고 기존 `parcels` 테이블에서 채운다.

    `parcels` 테이블 자체는 건드리지 않는다(추가 전용). rowid 를 그대로
    R-Tree 의 id 로 써서 조회 시 `parcels` 로 되짚어갈 수 있게 한다.
    """

    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {RTREE_TABLE} "
        "USING rtree(id, min_lat, max_lat, min_lng, max_lng)"
    )
    conn.execute(
        f"INSERT INTO {RTREE_TABLE} "
        "SELECT rowid, min_lat, max_lat, min_lng, max_lng FROM parcels"
    )


def add_spatial_rtree_index(db_path: str | os.PathLike[str]) -> bool:
    """이미 만들어진 지적도 SQLite 에 R-Tree 공간 인덱스를 추가한다.

    `build_index()` 전체 재실행(수 분, shapefile 재파싱) 없이 기존 산출물
    위에서 바로 돈다. 이미 있으면 아무 것도 하지 않고 False 를 돌려준다.
    읽기전용 조회 연결(`CadastralLocalStore._connect`)과 별도로 쓰기 연결을
    직접 연다 — 조회 경로는 `mode=ro` 라 인덱스를 만들 수 없기 때문이다.
    """

    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"지적도 인덱스 파일이 없습니다: {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        if _rtree_exists(conn):
            return False
        _populate_rtree(conn)
        conn.commit()
        return True
    finally:
        conn.close()


# pyproj Transformer 는 변환 호출에 대해 스레드 안전하므로 모듈 수준에서 1회 생성.
_TO_WGS84 = Transformer.from_crs(SOURCE_EPSG, WGS84_EPSG, always_xy=True)


class LocalParcel(NamedTuple):
    """로컬 지적도 필지 하나. app 의 ParcelFeature 와 필드를 맞춰 통합을 쉽게 한다."""

    pnu: str
    jibun: str
    address: str  # DBF 에 주소 컬럼이 없어 비어 있다. 통합 시 역지오코딩으로 채운다.
    ring: list[Coordinates]  # 첫 점 == 끝 점인 닫힌 링, 최소 4점
    area_m2: float
    in_jeonbuk: bool  # 룰북 §7 ① 범위 재검사 통과 여부


@dataclass(frozen=True)
class SourceStatus:
    """원천 적재 상태. 어떤 상황이든 예외 대신 이 값으로 알린다."""

    available: bool  # 조회 가능(SQLite 인덱스가 만들어져 있음)
    shp_present: bool
    db_present: bool
    parcel_count: int
    note: str


# --- 도형 헬퍼 ----------------------------------------------------------------


def _shoelace_area_m2(points: list[tuple[float, float]]) -> float:
    """투영좌표(m) 링의 절대 면적. 원본이 m 단위라 그대로 m²가 된다."""

    n = len(points)
    if n < 3:
        return 0.0
    total = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _largest_ring_wgs84(shape: "shapefile.Shape") -> tuple[list[Coordinates], float] | None:
    """shape 의 여러 파트 중 투영면적이 가장 큰 링을 골라 WGS84 링으로 변환한다.

    반환: (닫힌 WGS84 링[최소 4점], 투영면적 m²). 유효 링이 없으면 None.
    """

    points = shape.points
    if not points:
        return None
    parts = list(shape.parts) + [len(points)]

    best_seg: list[tuple[float, float]] | None = None
    best_area = -1.0
    for i in range(len(parts) - 1):
        seg = points[parts[i] : parts[i + 1]]
        if len(seg) < 3:
            continue
        area = _shoelace_area_m2(seg)
        if area > best_area:
            best_area = area
            best_seg = seg
    if best_seg is None:
        return None

    xs = [p[0] for p in best_seg]
    ys = [p[1] for p in best_seg]
    lngs, lats = _TO_WGS84.transform(xs, ys)
    ring = [Coordinates(lat=float(lat), lng=float(lng)) for lat, lng in zip(lats, lngs)]

    # 닫힌 링 보장(첫 점 == 끝 점).
    if ring[0].lat != ring[-1].lat or ring[0].lng != ring[-1].lng:
        ring.append(Coordinates(lat=ring[0].lat, lng=ring[0].lng))
    if len(ring) < 4:
        return None
    return ring, best_area


def _ring_in_jeonbuk(ring: list[Coordinates]) -> bool:
    """링 중심(대표점)이 전북 범위 안인지 재검사(룰북 §7 ①)."""

    lat = sum(c.lat for c in ring) / len(ring)
    lng = sum(c.lng for c in ring) / len(ring)
    return (
        JEONBUK_LAT_MIN <= lat <= JEONBUK_LAT_MAX
        and JEONBUK_LNG_MIN <= lng <= JEONBUK_LNG_MAX
    )


def _bbox(ring: list[Coordinates]) -> tuple[float, float, float, float]:
    lats = [c.lat for c in ring]
    lngs = [c.lng for c in ring]
    return min(lats), min(lngs), max(lats), max(lngs)


# --- 인덱스 빌드(1회 오프라인 변환) -------------------------------------------


def _resolve_field_index(field_names: list[str], candidates: tuple[str, ...]) -> int | None:
    lowered = [f.lower() for f in field_names]
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered.index(cand.lower())
    return None


def build_index(
    shp_path: str | os.PathLike[str],
    db_path: str | os.PathLike[str],
    *,
    limit: int | None = None,
    batch_size: int = 5000,
    progress: Callable[[int], None] | None = None,
) -> int:
    """shapefile 을 SQLite 로 1회 변환한다. 적재한 필지 수를 돌려준다.

    대용량이라 `iterShapeRecords()` 로 스트리밍하며, `limit` 으로 일부만 변환할
    수 있다(부분 검증·타 시군구 테스트용).
    """

    shp_path = Path(shp_path)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    reader = shapefile.Reader(str(shp_path), encoding=SOURCE_ENCODING)
    field_names = [f[0] for f in reader.fields[1:]]  # 첫 항목은 DeletionFlag
    pnu_idx = _resolve_field_index(field_names, _PNU_FIELD_CANDIDATES)
    jibun_idx = _resolve_field_index(field_names, _JIBUN_FIELD_CANDIDATES)
    if pnu_idx is None:
        raise ValueError(
            f"PNU 컬럼을 찾지 못했습니다. 실제 컬럼: {field_names}"
        )

    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE parcels(
                pnu TEXT,
                jibun TEXT,
                area_m2 REAL,
                min_lat REAL,
                min_lng REAL,
                max_lat REAL,
                max_lng REAL,
                in_jeonbuk INTEGER,
                ring_json TEXT
            );
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
            """
        )

        inserted = 0
        rows: list[tuple] = []
        for record in reader.iterShapeRecords():
            if limit is not None and inserted >= limit:
                break
            shape = record.shape
            attrs = record.record
            built = _largest_ring_wgs84(shape)
            if built is None:
                continue
            ring, area_m2 = built
            pnu = str(attrs[pnu_idx] or "").strip()
            if not pnu:
                continue
            jibun = str(attrs[jibun_idx] or "").strip() if jibun_idx is not None else ""
            min_lat, min_lng, max_lat, max_lng = _bbox(ring)
            in_jeonbuk = _ring_in_jeonbuk(ring)
            ring_json = json.dumps(
                [[round(c.lat, 8), round(c.lng, 8)] for c in ring],
                separators=(",", ":"),
            )
            rows.append(
                (
                    pnu,
                    jibun,
                    area_m2,
                    min_lat,
                    min_lng,
                    max_lat,
                    max_lng,
                    1 if in_jeonbuk else 0,
                    ring_json,
                )
            )
            inserted += 1
            if len(rows) >= batch_size:
                conn.executemany(
                    "INSERT INTO parcels VALUES(?,?,?,?,?,?,?,?,?)", rows
                )
                rows.clear()
                if progress is not None:
                    progress(inserted)

        if rows:
            conn.executemany("INSERT INTO parcels VALUES(?,?,?,?,?,?,?,?,?)", rows)

        conn.execute("CREATE INDEX ix_pnu ON parcels(pnu)")
        conn.execute(
            "CREATE INDEX ix_bbox ON parcels(min_lat, max_lat, min_lng, max_lng)"
        )
        # R-Tree 로 점-포함 조회를 빠르게 한다(모듈 docstring 「점-포함 조회」참고).
        # ix_bbox 는 남겨 둔다 — R-Tree 없는 산출물과의 호환(구버전 DB) 및 PNU 단독
        # 조회 등 다른 경로에 영향이 없어야 하므로 대체가 아니라 추가다.
        _populate_rtree(conn)
        conn.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?,?)",
            [
                ("schema_version", str(_SCHEMA_VERSION)),
                ("source_epsg", SOURCE_EPSG),
                ("parcel_count", str(inserted)),
                ("shp_path", str(shp_path)),
            ],
        )
        conn.commit()
        if progress is not None:
            progress(inserted)
        return inserted
    finally:
        conn.close()


# --- 조회 인터페이스 ----------------------------------------------------------


class CadastralLocalStore:
    """전북 연속지적도 로컬 조회기.

    파일이나 인덱스가 없어도 생성은 성공한다. 상태는 `status()` 로 확인한다.
    """

    def __init__(
        self,
        shp_path: str | os.PathLike[str] | None = None,
        db_path: str | os.PathLike[str] | None = None,
    ) -> None:
        env_shp = os.environ.get(ENV_SHP)
        env_db = os.environ.get(ENV_DB)
        self.shp_path: Path | None = (
            Path(shp_path) if shp_path else (Path(env_shp) if env_shp else None)
        )
        self.db_path: Path = Path(db_path or env_db or DEFAULT_DB_PATH)
        self._conn: sqlite3.Connection | None = None
        # parcels_rtree 존재 여부. 연결마다 sqlite_master 를 다시 묻지 않도록
        # 최초 연결 시 한 번만 확인해 캐시한다. None = 아직 확인 안 함.
        self._has_rtree: bool | None = None

    # -- 연결 관리 --

    def _connect(self) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        if not self.db_path.exists():
            return None
        # 읽기 전용 재사용 연결. 조회만 하므로 스레드 공유를 허용한다.
        conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._has_rtree = _rtree_exists(conn)
        return conn

    def _bbox_candidates(
        self, conn: sqlite3.Connection, lat: float, lng: float
    ) -> list[sqlite3.Row]:
        """점을 포함할 수 있는 필지 후보(초과집합)를 bbox 로 좁혀 돌려준다.

        `parcels_rtree` 가 있으면(신규 산출물이거나 마이그레이션 완료) R-Tree
        조인으로 좁히고(실측 1.1초 → 1~2ms), 없으면 기존 `ix_bbox` 복합 인덱스
        범위조회로 폴백한다. R-Tree 결과는 float32 반올림으로 원본보다 후보가
        약간 더 나올 수 있으나(초과집합 보장, 실측 근거는 모듈 상단 주석) 이후
        shapely 정밀판정이 그대로 걸러내므로 최종 결과는 동일하다.
        """

        if self._has_rtree:
            query = f"""
                SELECT p.* FROM {RTREE_TABLE} r
                JOIN parcels p ON p.rowid = r.id
                WHERE r.min_lat <= ? AND r.max_lat >= ?
                  AND r.min_lng <= ? AND r.max_lng >= ?
            """
        else:
            query = """
                SELECT * FROM parcels
                WHERE min_lat <= ? AND max_lat >= ?
                  AND min_lng <= ? AND max_lng >= ?
            """
        return conn.execute(query, (lat, lat, lng, lng)).fetchall()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- 상태 --

    def status(self) -> SourceStatus:
        shp_present = bool(self.shp_path and self.shp_path.exists())
        conn = self._connect()
        if conn is None:
            if shp_present:
                note = (
                    "지적도 원본은 있으나 인덱스(SQLite)가 없습니다. "
                    "build_index() 로 1회 변환이 필요합니다."
                )
            else:
                note = (
                    "전북 연속지적도 원천이 미적재 상태입니다. "
                    f"{ENV_SHP}/{ENV_DB} 로 경로를 주입하거나 파일을 배치하세요."
                )
            return SourceStatus(
                available=False,
                shp_present=shp_present,
                db_present=False,
                parcel_count=0,
                note=note,
            )
        try:
            count = conn.execute("SELECT COUNT(*) FROM parcels").fetchone()[0]
        except sqlite3.Error:
            return SourceStatus(
                available=False,
                shp_present=shp_present,
                db_present=True,
                parcel_count=0,
                note="인덱스 파일이 손상되었거나 형식이 맞지 않습니다.",
            )
        return SourceStatus(
            available=count > 0,
            shp_present=shp_present,
            db_present=True,
            parcel_count=int(count),
            note="연속지적도 로컬 인덱스가 적재되어 있습니다." if count else "인덱스가 비어 있습니다.",
        )

    # -- 조회 --

    def parcel_by_pnu(self, pnu: str) -> LocalParcel | None:
        """PNU 로 필지를 조회한다. 인덱스가 없거나 없는 PNU 면 None."""

        conn = self._connect()
        if conn is None or not pnu:
            return None
        row = conn.execute(
            "SELECT * FROM parcels WHERE pnu = ? LIMIT 1",
            (pnu.strip(),),
        ).fetchone()
        return self._row_to_parcel(row) if row else None

    def parcel_at(self, lat: float, lng: float) -> LocalParcel | None:
        """점(lat, lng)을 포함하는 필지를 조회한다.

        bbox 로 후보를 좁힌 뒤 shapely 로 정밀 포함 판정한다. 겹치는 후보가
        여럿이면 면적이 가장 작은(가장 촘촘한) 필지를 채택한다.
        """

        conn = self._connect()
        if conn is None:
            return None
        candidates = self._bbox_candidates(conn, lat, lng)
        if not candidates:
            return None

        point = Point(lng, lat)  # shapely 는 (x=lng, y=lat)
        best: LocalParcel | None = None
        for row in candidates:
            parcel = self._row_to_parcel(row)
            polygon = Polygon([(c.lng, c.lat) for c in parcel.ring])
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.covers(point):
                if best is None or parcel.area_m2 < best.area_m2:
                    best = parcel
        return best

    def sole_parcel_at(self, lat: float, lng: float) -> tuple[LocalParcel | None, int]:
        """점을 포함하는 필지가 **단 하나일 때만** 그 필지를 돌려준다.

        PNU 확정용 엄격 경로다(설계서 §7.2 ④). parcel_at() 은 후보가 여럿이면
        최소면적을 골라 하나로 좁히지만, 여기서는 경계 공유로 둘 이상이 잡히면
        확정하지 않도록 (None, 후보수) 를 돌려준다. 후보수>1 이면 상위(PnuResolver)
        가 검토대상으로 분리한다. parcel_at() 은 다른 경로가 쓰므로 바꾸지 않는다.

        반환: (단일 필지 또는 None, 점을 포함하는 후보 필지 수).
        """

        conn = self._connect()
        if conn is None:
            return None, 0
        candidates = self._bbox_candidates(conn, lat, lng)
        if not candidates:
            return None, 0
        point = Point(lng, lat)
        covering: list[LocalParcel] = []
        for row in candidates:
            parcel = self._row_to_parcel(row)
            polygon = Polygon([(c.lng, c.lat) for c in parcel.ring])
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.covers(point):
                covering.append(parcel)
        if len(covering) == 1:
            return covering[0], 1
        return None, len(covering)

    def representative_point(self, parcel: LocalParcel) -> Coordinates | None:
        """필지 내부의 대표점(설계서 §7.2 ⑤ CADASTRAL_REPRESENTATIVE_POINT).

        좌표가 없는 원천을 조립 PNU 로 확정할 때, 외부 조회 없이 좌표를 파생한다.
        shapely representative_point() 는 폴리곤 내부가 보장된 점을 준다(중심점이
        오목 필지에서 밖으로 나가는 문제를 피한다).
        """

        if len(parcel.ring) < 4:
            return None
        polygon = Polygon([(c.lng, c.lat) for c in parcel.ring])
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if polygon.is_empty:
            return None
        point = polygon.representative_point()
        return Coordinates(lat=float(point.y), lng=float(point.x))

    def pnu_prefix_exists(self, prefix: str) -> bool:
        """PNU 접두(법정동10+대지구분1+본번4)로 실재 필지가 있는지.

        실패 사유 구분(설계서 §7.2 ⑥)에 쓴다. 같은 본번의 다른 부번이 존재하면
        「지번 변경·합병·분할」, 본번 접두 자체가 없으면 「본번 자체 없음」이다.
        """

        conn = self._connect()
        if conn is None or not prefix:
            return False
        row = conn.execute(
            "SELECT 1 FROM parcels WHERE pnu LIKE ? LIMIT 1",
            (prefix + "%",),
        ).fetchone()
        return row is not None

    # -- 내부 --

    @staticmethod
    def _row_to_parcel(row: sqlite3.Row) -> LocalParcel:
        ring = [
            Coordinates(lat=float(lat), lng=float(lng))
            for lat, lng in json.loads(row["ring_json"])
        ]
        return LocalParcel(
            pnu=row["pnu"],
            jibun=row["jibun"] or "",
            address="",
            ring=ring,
            area_m2=float(row["area_m2"]),
            in_jeonbuk=bool(row["in_jeonbuk"]),
        )
