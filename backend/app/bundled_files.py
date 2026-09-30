"""API 외에 서버에 실은 파일 — 한 곳에 모은 목록(2026-09-30 사용자 요구).

공공 API 로 온전히 받을 수 없어 파일로 서버(Cloud Run 이미지)에 실은 자료는 배포판에서도
나중에 무엇이 실렸는지 볼 수 있어야 한다. 배포판은 API 연결 정보(키·점검)를 루프백 전용으로
감추므로, 그 자리에 이 목록을 보인다(GET /api/settings/bundled-files · 누구나 읽기 · 비밀 없음).

파일마다 이름·쓰임·출처(원천 데이터셋/ID)·기준일·건수를 적는다. 파일을 새로 실으면 여기
BUNDLED_FILES 에 한 줄을 더한다(각 서비스의 경로·기준일 상수를 그대로 가져와 어긋나지 않게 한다).
건수는 요청 때 파일을 직접 세고(CSV 행 · JSON 항목 · SQLite 행), 파일이 없으면 present=False 로
드러낸다 — 목록만 있고 파일이 빠진 배포를 숨기지 않는다.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from app.lh_alignments import load_registry as load_lh_alignments
from app.screening.front_door import DATASET_GATES_PATH
from app.services.facility_store import DEFAULT_DB_PATH as FACILITY_DB_PATH
from app.services.factory_registry import DEFAULT_ROWS_CACHE
from app.services.factory_lots import (
    FACTORY_LOTS_AS_OF,
    FACTORY_LOTS_CSV,
    FACTORY_LOTS_DATASET_ID,
    FACTORY_LOTS_DATASET_TITLE,
    FACTORY_LOTS_URL,
)
from app.services.gas_product_file import (
    BUNDLED_GEOCODED_CSV as GAS_PRODUCT_GEOCODED_CSV,
    BUNDLED_SOURCE_CSV as GAS_PRODUCT_SOURCE_CSV,
    GAS_PRODUCT_AS_OF,
    GAS_PRODUCT_DATASET_ID,
    GAS_PRODUCT_DATASET_PAGE_URL,
)
from app.services.rail_stations import STATION_EXITS_PATH

BACKEND_DIR = Path(__file__).resolve().parents[1]


class BundledFileView(BaseModel):
    id: str
    name: str
    path: str
    purpose: str
    source: str
    source_id: str
    source_url: str
    as_of: str
    count: int | None
    count_unit: str
    present: bool
    size_bytes: int | None
    note: str


class BundledFilesResponse(BaseModel):
    description: str
    files: list[BundledFileView]


@dataclass(frozen=True)
class BundledFile:
    id: str
    name: str
    path: Path
    purpose: str
    source: str
    source_id: str = ""
    source_url: str = ""
    as_of: str = ""
    count_unit: str = "행"
    note: str = ""
    # 파일 경로 → 건수. 없으면 CSV 데이터 행을 센다.
    counter: Callable[[Path], int] | None = None
    # 기준일을 파일에서 읽어야 하면(동기화 시각 등) 요청 때 부른다.
    as_of_reader: Callable[[Path], str] | None = None

    def view(self) -> BundledFileView:
        present = self.path.is_file()
        count: int | None = None
        if present:
            try:
                count = (self.counter or count_csv_rows)(self.path)
            except (OSError, ValueError, sqlite3.Error):
                count = None
        try:
            relative = self.path.relative_to(BACKEND_DIR).as_posix()
        except ValueError:
            relative = self.path.name
        return BundledFileView(
            id=self.id,
            name=self.name,
            path=f"backend/{relative}",
            purpose=self.purpose,
            source=self.source,
            source_id=self.source_id,
            source_url=self.source_url,
            as_of=(self.as_of_reader(self.path) if self.as_of_reader else "") or self.as_of,
            count=count,
            count_unit=self.count_unit,
            present=present,
            size_bytes=self.path.stat().st_size if present else None,
            note=self.note,
        )


def count_csv_rows(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return max(sum(1 for _ in csv.reader(handle)) - 1, 0)


def _count_factory_lots(path: Path) -> int:
    """PNU·지적도 좌표까지 붙은 공장 수(런타임이 실제로 쓰는 행)."""

    with path.open(encoding="utf-8", newline="") as handle:
        return sum(1 for row in csv.DictReader(handle) if row.get("pnu") and row.get("lat"))


def _count_lh_alignments(path: Path) -> int:
    return len(load_lh_alignments(path).entries)


def _count_facility_rows(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM facilities").fetchone()[0])
    finally:
        connection.close()


def _facility_db_synced(path: Path) -> str:
    """인허가 사본의 가장 최근 동기화 날짜(YYYY-MM-DD). 읽지 못하면 빈 문자열."""

    if not path.is_file():
        return ""
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute("SELECT MAX(synced_at) FROM sync_state").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return ""
    return str(row[0] or "")[:10] if row else ""


def _count_factory_rows(path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    return sum(len(entry.get("rows") or []) for entry in data.values())


def _factory_rows_fetched(path: Path) -> str:
    """등록공장 목록 사본에서 가장 오래된 시군구의 받은 날짜(YYYY-MM-DD)."""

    if not path.is_file():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        stamps = [float(entry.get("fetched_at") or 0) for entry in data.values()]
    except (OSError, ValueError, AttributeError):
        return ""
    if not stamps:
        return ""
    return datetime.fromtimestamp(min(stamps), UTC).strftime("%Y-%m-%d")


BUNDLED_FILES: tuple[BundledFile, ...] = (
    BundledFile(
        id="factory_lots",
        name="전주시 등록공장 지번(PNU)",
        path=FACTORY_LOTS_CSV,
        purpose=(
            "등록공장(공장 있음) 검토에서 공장등록 API 가 주지 않는 등록 지번으로 시설 필지를 "
            "잡습니다. API 행과 회사명+도로명이 같은 공장은 도로명 지오코딩 대신 이 지번 필지로 잽니다."
        ),
        source=f"공공데이터포털 {FACTORY_LOTS_DATASET_TITLE}(전주시 기업지원과)",
        source_id=f"fileData {FACTORY_LOTS_DATASET_ID}",
        source_url=FACTORY_LOTS_URL,
        as_of=FACTORY_LOTS_AS_OF,
        count_unit="곳(PNU 확정)",
        note=(
            "원본 1,280곳 중 회사명·도로명·지번·기준일만 옮기고, 지번으로 PNU 와 필지 안 한 점을 "
            "붙였습니다(tools/build_factory_lots.py · VWorld). 전주시 밖 공장은 도로명 지오코딩을 씁니다."
        ),
        counter=_count_factory_lots,
    ),
    BundledFile(
        id="gas_product_source",
        name="가스용품 제조업소 원본",
        path=GAS_PRODUCT_SOURCE_CSV,
        purpose="위험물 저장 및 처리 시설(가스제품 제조업소) 50m 판정의 원장입니다.",
        source="공공데이터포털 한국가스안전공사_가스제품 제조업소정보",
        source_id=f"fileData {GAS_PRODUCT_DATASET_ID}",
        source_url=GAS_PRODUCT_DATASET_PAGE_URL,
        as_of=GAS_PRODUCT_AS_OF,
        note="파일 변환 API 가 이 키로 열리지 않아 원본 CSV 를 그대로 실었습니다(전국 · 생산품목마다 한 줄).",
    ),
    BundledFile(
        id="gas_product_geocoded",
        name="가스용품 제조업소 좌표",
        path=GAS_PRODUCT_GEOCODED_CSV,
        purpose="위 원본을 업소 단위로 합치고 소재지를 한 번 지오코딩해 둔 좌표입니다.",
        source="가스용품 제조업소 원본 + 카카오 주소검색",
        source_id=f"fileData {GAS_PRODUCT_DATASET_ID}",
        source_url=GAS_PRODUCT_DATASET_PAGE_URL,
        as_of=GAS_PRODUCT_AS_OF,
        count_unit="곳",
        note="서버가 켤 때마다 다시 지오코딩하지 않도록 미리 구워 둔 파일입니다.",
    ),
    BundledFile(
        id="university_gates",
        name="전북 대학교 정문 좌표",
        path=DATASET_GATES_PATH,
        purpose="대학교까지 거리를 캠퍼스 필지가 아니라 정문에서 잽니다(담당자 수기 지정 다음 순위).",
        source="LH 전북 생활입지 표준 데이터셋 「대학교」 정문 좌표(대학알리미 + 수기 보완)",
        source_id="편의시설/52_university_exit_update.xlsx",
        as_of="2026-09-16",
        count_unit="곳",
        note="09-16 판에서 옮기고, 전주대·전주비전대 정문은 LH 09-28 거리에 맞춰 네이버 지역검색 좌표로 바꿨습니다.",
    ),
    BundledFile(
        id="station_exits",
        name="전북 철도역 출구 좌표",
        path=STATION_EXITS_PATH,
        purpose="철도역·KTX역까지 거리를 역 건물이 아니라 출구에서 잽니다.",
        source="LH 데이터셋 역 출구 좌표(철도역 JB_48 · KTX역 JB_49)",
        source_id="48_railway_stations_exit_update.csv · 49_ktx_stations_exit_update.csv",
        as_of="2026-09-16",
        count_unit="곳",
    ),
    BundledFile(
        id="lh_alignments",
        name="LH 개별 맞춤 등록부",
        path=BACKEND_DIR / "data" / "lh_alignments.json",
        purpose=(
            "공공 API 로 모은 시설 중 LH 데이터셋과 위치·이름·범위가 달라 시설 한 곳씩 "
            "LH 기준에 맞춘 목록입니다(관리자 설정 「LH 개별 확인 제외」 탭에서 항목별 근거를 봅니다)."
        ),
        source="LH 데이터셋(진용성 v6 facility-sets) · LH 앱 결과 17건 표본",
        source_id="facility-sets/2026-09-16/facilities.xlsx",
        as_of="2026-09-30",
        count_unit="곳",
        counter=_count_lh_alignments,
    ),
    BundledFile(
        id="facility_store",
        name="인허가 시설 로컬 사본",
        path=FACILITY_DB_PATH,
        purpose=(
            "행안부 지방행정 인허가 API 는 지역 필터가 없어 전량을 받아 둔 사본입니다. 숙박·위락 등 "
            "인허가 시설을 사업지 주변에서 바로 찾습니다."
        ),
        source="행정안전부 지방행정 인허가 데이터(LOCALDATA) API 동기화 사본",
        source_id="localdata.go.kr",
        source_url="https://www.localdata.go.kr/",
        as_of_reader=_facility_db_synced,
        count_unit="행",
        note="API 로 받은 자료를 파일로 둔 것이라 git 에는 없고 배포 이미지에만 실립니다(sync_facilities.py 로 다시 만듭니다).",
        counter=_count_facility_rows,
    ),
    BundledFile(
        id="factory_rows",
        name="산단공 등록공장 목록 사본",
        path=DEFAULT_ROWS_CACHE,
        purpose=(
            "산단공 공장등록 API 는 시군구 하나(1천여 곳)를 주는 데 20~80초가 걸려 1차 공장 조회가 "
            "오래 걸렸습니다. 전북 시군구 목록을 받아 두고 곧바로 쓰며, 하루가 지나면 뒤에서 새로 받습니다."
        ),
        source="한국산업단지공단 공장등록 필지정보 API 사본",
        source_id="data.go.kr 15087615",
        source_url="https://www.data.go.kr/data/15087615/openapi.do",
        as_of_reader=_factory_rows_fetched,
        count_unit="곳",
        note="API 로 받은 자료를 파일로 둔 것이라 git 에는 없고 배포 이미지에만 실립니다.",
        counter=_count_factory_rows,
    ),
)


def bundled_files_response() -> BundledFilesResponse:
    return BundledFilesResponse(
        description=(
            "공공 API 로 온전히 받을 수 없어 파일로 서버에 실은 자료입니다. 파일마다 쓰임·출처·"
            "기준일·건수를 보입니다."
        ),
        files=[entry.view() for entry in BUNDLED_FILES],
    )
