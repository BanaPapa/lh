"""API 외에 서버에 실은 파일 — 한 곳에 모은 목록(2026-09-30 사용자 요구).

공공 API 로 온전히 받을 수 없어 파일로 서버(Cloud Run 이미지)에 실은 자료는 배포판에서도
나중에 무엇이 실렸는지 볼 수 있어야 한다. 배포판은 API 연결 정보(키·점검)를 루프백 전용으로
감추므로, 그 자리에 이 목록을 보인다(GET /api/settings/bundled-files · 누구나 읽기 · 비밀 없음).

파일마다 이름·쓰임·출처(원천 데이터셋/ID)·기준일·건수를 적는다. 파일을 새로 실으면 여기
BUNDLED_FILES 에 한 줄을 더한다(각 서비스의 경로·기준일 상수를 그대로 가져와 어긋나지 않게 한다).
건수는 요청 때 파일을 직접 세고(CSV 행 · JSON 항목 · SQLite 행), 파일이 없으면 present=False 로
드러낸다 — 목록만 있고 파일이 빠진 배포를 숨기지 않는다.

API 사본(api_snapshot=True): 공공 API 로 받은 것을 파일로 둔 사본·지오코딩 캐시다. git 에는
없고(.gitignore) 배포 이미지에만 실리며, `python -m app.refresh_snapshots` 가 한 번에 다시
만든다. 야간 갱신 작업이 저장소(버킷)와 주고받을 파일 이름은 `snapshot_file_names()` —
`python -m app.refresh_snapshots --list-files` 가 이 목록을 그대로 찍는다. 사본을 새로 만들면
여기 한 줄만 더하면 화면 목록과 야간 갱신 파일 목록에 함께 들어간다.
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

from app.kst import KST
from app.lh_alignments import load_registry as load_lh_alignments
from app.screening.front_door import DATASET_GATES_PATH
from app.services import casino_registry as casino_source
from app.services import city_gas_registry as city_gas_source
from app.services import city_parks as park_source
from app.services import cng as cng_source
from app.services import cng_gyeongnam as cng_gyeongnam_source
from app.services import crematorium as crematorium_source
from app.services import gg_chemical as gg_chemical_source
from app.services import kgs as kgs_source
from app.services import lpg_municipal as lpg_municipal_source
from app.services import lpg_seoul as lpg_seoul_source
from app.services import lpg_station_file as lpg_file_source
from app.services import public_library as library_source
from app.services import rail_stations as rail_source
from app.services import safemap as safemap_source
from app.services import school_locations as school_source
from app.services import seoul_bus as seoul_bus_source
from app.services import traditional_market as market_source
from app.services import transfer_center as transfer_source
from app.services.facility_store import DEFAULT_DB_PATH as FACILITY_DB_PATH
from app.services.factory_registry import DEFAULT_GEOCODE_CACHE, DEFAULT_ROWS_CACHE
from app.services.localdata import GEOCODE_CACHE_PATH as LOCALDATA_GEOCODE_CACHE
from app.services.safemap_facilities import SNAPSHOT_LAYER_IDS, layer_snapshot_path
from app.services.safemap_layers import LAYER_BY_ID
from app.services.snapshot_store import DATA_DIR, snapshot_as_of, snapshot_row_count
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
    # 공공 API 로 받은 것을 파일로 둔 사본·캐시인가(git 에 없고 야간 갱신이 다시 만든다).
    api_snapshot: bool = False

    def view(self) -> BundledFileView:
        present = self.path.is_file()
        count: int | None = None
        if present:
            try:
                count = (self.counter or count_csv_rows)(self.path)
            except (OSError, ValueError, AttributeError, sqlite3.Error):
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


def _count_json_keys(path: Path) -> int:
    """지오코딩 캐시(주소 → 좌표)의 주소 수."""

    data = json.loads(path.read_text(encoding="utf-8"))
    return len(data) if isinstance(data, dict) else 0


def _file_modified(path: Path) -> str:
    """파일을 마지막으로 쓴 날짜(YYYY-MM-DD · 한국 표준시). 읽지 못하면 빈 문자열."""

    try:
        return datetime.fromtimestamp(path.stat().st_mtime, KST).strftime("%Y-%m-%d")
    except OSError:
        return ""


SNAPSHOT_NOTE = (
    "API 로 받은 자료를 파일로 둔 사본이라 git 에는 없고 배포 이미지에만 실립니다. 서버는 켜지자마자 "
    "이 사본으로 답하고, 받은 지 하루가 지나면 뒤에서 새로 받습니다. 새로 받기가 실패하면 사본을 "
    "계속 쓰되 결과에 「서버 사본 사용(기준일)」을 밝히고, 7일을 넘기면 조회 실패로 올립니다 "
    "(python -m app.refresh_snapshots 로 다시 만듭니다)."
)


COORDINATE_NOTE = (
    "주소 지오코딩 결과를 파일로 둔 것이라 git 에는 없고 배포 이미지에만 실립니다. 주소가 바뀐 "
    "항목과 새 항목만 다시 지오코딩합니다(python -m app.refresh_snapshots 가 채웁니다)."
)


def _snapshot_file(
    file_id: str,
    name: str,
    path: Path,
    purpose: str,
    source: str,
    source_id: str,
    source_url: str,
    count_unit: str = "곳",
    note: str = SNAPSHOT_NOTE,
) -> BundledFile:
    """전량 목록 API 사본 한 줄(기준일·건수는 사본 파일에서 읽는다)."""

    return BundledFile(
        id=file_id,
        name=name,
        path=path,
        purpose=purpose,
        source=source,
        source_id=source_id,
        source_url=source_url,
        as_of_reader=snapshot_as_of,
        count_unit=count_unit,
        note=note,
        counter=snapshot_row_count,
        api_snapshot=True,
    )


def _layer_snapshot_files() -> tuple[BundledFile, ...]:
    """생활안전지도 시설 레이어 사본(레이어마다 파일 하나)."""

    return tuple(
        BundledFile(
            id=f"safemap_layer_{layer_id}",
            name=f"생활안전지도 {LAYER_BY_ID[layer_id].label} 사본",
            path=layer_snapshot_path(layer_id),
            purpose=LAYER_BY_ID[layer_id].purpose,
            source=f"생활안전지도 오픈API {layer_id}({LAYER_BY_ID[layer_id].agency}) 사본",
            source_id=f"safemap.go.kr {layer_id}",
            source_url="https://www.safemap.go.kr/opna/data/dataList.do",
            as_of_reader=snapshot_as_of,
            count_unit="곳",
            note=SNAPSHOT_NOTE,
            counter=snapshot_row_count,
            api_snapshot=True,
        )
        for layer_id in SNAPSHOT_LAYER_IDS
    )


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
        api_snapshot=True,
    ),
    BundledFile(
        id="localdata_geocode_cache",
        name="인허가 원장 주소 지오코딩 캐시",
        path=LOCALDATA_GEOCODE_CACHE,
        purpose=(
            "인허가 원장에서 좌표가 빈 영업 중 사업장의 주소 → 좌표 캐시입니다. 원장을 다시 받을 때 "
            "새 주소만 지오코딩해 쿼터를 아낍니다."
        ),
        source="카카오·네이버·VWorld 주소검색 결과 캐시",
        source_id="localdata_geocode_cache.json",
        as_of_reader=_file_modified,
        count_unit="주소",
        note="원장 동기화(sync_facilities · refresh_snapshots) 때만 읽고 씁니다. 심사 경로는 쓰지 않습니다.",
        counter=_count_json_keys,
        api_snapshot=True,
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
        api_snapshot=True,
    ),
    BundledFile(
        id="factory_geocode_cache",
        name="등록공장 주소 지오코딩 캐시",
        path=DEFAULT_GEOCODE_CACHE,
        purpose=(
            "산단공 등록공장 목록에는 좌표가 없어 도로명주소를 지오코딩해 둔 캐시입니다. 같은 주소를 "
            "심사마다 다시 지오코딩하지 않습니다."
        ),
        source="카카오·네이버·VWorld 주소검색 결과 캐시",
        source_id="factory_geocode_cache.json",
        as_of_reader=_file_modified,
        count_unit="주소",
        note="심사 중 새 주소가 나오면 그때 더해집니다. git 에는 없고 배포 이미지에만 실립니다.",
        counter=_count_json_keys,
        api_snapshot=True,
    ),
    BundledFile(
        id="safemap_fuel",
        name="생활안전지도 전국 주유시설 사본",
        path=safemap_source.DEFAULT_SNAPSHOT_PATH,
        purpose=(
            "1차 주유소·LPG 충전소(25m) 판정의 전국 목록입니다. 받는 데 15쪽이 걸려, 서버가 켜진 "
            "직후 첫 심사에 「조회 실패 — 재심사 필요」가 뜨던 것을 사본으로 없앱니다."
        ),
        source="생활안전지도 오픈API IF_0033(전국 주유시설 현황) 사본",
        source_id="safemap.go.kr IF_0033",
        source_url="https://www.safemap.go.kr/opna/data/dataList.do",
        as_of_reader=snapshot_as_of,
        count_unit="곳",
        note=SNAPSHOT_NOTE,
        counter=snapshot_row_count,
        api_snapshot=True,
    ),
    BundledFile(
        id="kgs_lpg",
        name="가스안전공사 전국 LPG 충전소 사본",
        path=kgs_source.DEFAULT_SNAPSHOT_PATH,
        purpose=(
            "1차 LPG 충전소(25m) 판정의 전국 목록입니다. API 가 하루 종일 429 를 낸 날에도 받아 둔 "
            "목록으로 판정합니다(그날 결과에는 「서버 사본 사용(기준일)」이 붙습니다)."
        ),
        source="한국가스안전공사 LPG 충전소 조회 API 사본",
        source_id="data.go.kr B410019/kgsapi/lpg_station",
        source_url="https://www.data.go.kr/",
        as_of_reader=snapshot_as_of,
        count_unit="행",
        note=SNAPSHOT_NOTE,
        counter=snapshot_row_count,
        api_snapshot=True,
    ),
    BundledFile(
        id="crematorium",
        name="전국 화장시설 좌표 사본",
        path=crematorium_source.DEFAULT_SNAPSHOT_PATH,
        purpose=(
            "1차 화장장(500m) 판정의 전국 목록에 주소 지오코딩 좌표를 붙여 둔 사본입니다. 서버가 켜질 "
            "때마다 60여 곳을 다시 지오코딩하지 않습니다."
        ),
        source="보건복지부 전국 화장시설 현황 API + 주소 지오코딩 사본",
        source_id="data.go.kr 1352000/ODMS_DATA_05_1",
        source_url="https://www.data.go.kr/",
        as_of_reader=snapshot_as_of,
        count_unit="곳",
        note=SNAPSHOT_NOTE,
        counter=snapshot_row_count,
        api_snapshot=True,
    ),
    BundledFile(
        id="lpg_municipal",
        name="시군구 액화석유가스업 좌표 사본",
        path=lpg_municipal_source.DEFAULT_SNAPSHOT_PATH,
        purpose=(
            "1차 LPG 판매소(50m)·저장소 참고 핀의 시군구 파일에 좌표를 붙여 둔 사본입니다(데이터셋별). "
            "그 시군구의 첫 심사가 파일 수신·지오코딩을 기다리지 않습니다."
        ),
        source="공공데이터포털 시군구 액화석유가스업 파일(ODcloud) + 주소 지오코딩 사본",
        source_id="api.odcloud.kr (데이터셋 ID 별)",
        source_url=lpg_municipal_source.ODCLOUD_BASE,
        as_of_reader=snapshot_as_of,
        count_unit="곳",
        note=SNAPSHOT_NOTE + " 전북(익산시·부안군) 파일을 미리 받아 두고, 다른 시군구는 첫 심사 때 더해집니다.",
        counter=snapshot_row_count,
        api_snapshot=True,
    ),
    BundledFile(
        id="lpg_seoul",
        name="서울 액화석유가스업 좌표 사본",
        path=lpg_seoul_source.DEFAULT_SNAPSHOT_PATH,
        purpose=(
            "서울 사업지의 LPG 판매소·저장소 목록(520여 곳)에 좌표를 붙여 둔 사본입니다. 서버가 켜질 "
            "때마다 전량을 다시 지오코딩하지 않습니다."
        ),
        source="서울 열린데이터광장 액화석유가스업 현황 + 주소 지오코딩 사본",
        source_id=f"data.seoul.go.kr {lpg_seoul_source.SEOUL_LPG_DATASET_ID}",
        source_url=lpg_seoul_source.SEOUL_LPG_PAGE_URL,
        as_of_reader=snapshot_as_of,
        count_unit="곳",
        note=SNAPSHOT_NOTE,
        counter=snapshot_row_count,
        api_snapshot=True,
    ),
    *_layer_snapshot_files(),
    _snapshot_file(
        "city_parks", "전국 도시공원 사본", park_source.DEFAULT_SNAPSHOT_PATH,
        "2차 주거여건 「공원」의 전국 목록입니다(19쪽). 서버가 켜진 직후 붐빌 때 받다가 시간이 넘어 "
        "지도 검색 대체 + 원천 경고가 뜨던 것을 사본으로 없앱니다.",
        "전국도시공원정보표준데이터 사본", "data.go.kr tn_pubr_public_cty_park_info_api",
        park_source.CITY_PARK_URL,
    ),
    _snapshot_file(
        "school_locations", "전국 초·중·고 위치 사본", school_source.DEFAULT_SNAPSHOT_PATH,
        "2차 교육여건 초·중·고의 전국 목록입니다(12쪽).",
        "전국초중등학교위치표준데이터 사본", "data.go.kr tn_pubr_public_elesch_mskul_lc_api",
        school_source.SCHOOL_LOCATION_URL,
    ),
    _snapshot_file(
        "traditional_markets", "전국 전통시장 사본", market_source.DEFAULT_SNAPSHOT_PATH,
        "2차 상업시설에 더하는 전통시장의 전국 목록입니다.",
        "전국전통시장표준데이터 사본", "data.go.kr tn_pubr_public_trdit_mrkt_api",
        market_source.TRADITIONAL_MARKET_URL,
    ),
    _snapshot_file(
        "public_libraries", "전국 공공도서관 사본", library_source.DEFAULT_SNAPSHOT_PATH,
        "2차 공공시설에 세는 공공도서관의 전국 목록입니다(작은도서관·학교도서관은 뺀 행만 둡니다).",
        "전국도서관표준데이터 사본", f"data.go.kr {library_source.PUBLIC_LIBRARY_DATASET_ID}",
        library_source.PUBLIC_LIBRARY_URL,
    ),
    _snapshot_file(
        "korail_stations", "한국철도공사 역위치 사본", rail_source.DEFAULT_SNAPSHOT_PATH,
        "2차 철도역·KTX역의 전국 역 목록입니다(출구 좌표는 station_exits_jeonbuk.csv).",
        "한국철도공사 역위치 정보(ODcloud) 사본",
        f"data.go.kr {rail_source.KORAIL_STATION_DATASET_ID}", rail_source.KORAIL_STATION_URL,
        count_unit="역",
    ),
    _snapshot_file(
        "transfer_centers", "전국 환승센터 사본", transfer_source.DEFAULT_SNAPSHOT_PATH,
        "2차 환승시설의 전국 목록입니다.",
        "전국대중교통환승센터표준데이터 사본", "data.go.kr 15034541",
        transfer_source.TRANSFER_CENTER_URL,
    ),
    _snapshot_file(
        "seoul_bus_stops", "서울 버스정류소 사본", seoul_bus_source.DEFAULT_SNAPSHOT_PATH,
        "서울 사업지의 2차 버스정류장 목록입니다(국토부 TAGO 는 서울을 주지 않습니다).",
        "서울 열린데이터광장 버스정류소 위치정보 사본", "data.seoul.go.kr busStopLocationXyInfo",
        "https://data.seoul.go.kr/",
    ),
    _snapshot_file(
        "cng_stations", "가스안전공사 전국 CNG 충전소 사본", cng_source.DEFAULT_SNAPSHOT_PATH,
        "1차 CNG 충전소(25m) 판정의 전국 목록입니다.",
        "한국가스안전공사 전국 도시가스충전소 현황(ODcloud) 사본",
        f"data.go.kr {cng_source.CNG_DATASET_ID}", cng_source.CNG_DATASET_PAGE_URL,
        count_unit="행",
    ),
    _snapshot_file(
        "lpg_station_file", "가스안전공사 LPG 충전소 현황(파일) 사본",
        lpg_file_source.DEFAULT_SNAPSHOT_PATH,
        "1차 LPG 충전소(25m) 판정의 보조 목록입니다(조회 API 와 같은 명부의 파일본).",
        "한국가스안전공사 전국 LPG 충전소 현황(ODcloud) 사본",
        f"data.go.kr {lpg_file_source.LPG_FILE_DATASET_ID}",
        lpg_file_source.LPG_FILE_DATASET_PAGE_URL, count_unit="행",
    ),
    _snapshot_file(
        "cng_gyeongnam", "경남 천연가스 충전소 좌표 사본",
        cng_gyeongnam_source.DEFAULT_SNAPSHOT_PATH,
        "1차 CNG 충전소의 경남 보조 목록에 주소 지오코딩 좌표를 붙여 둔 사본입니다.",
        "경상남도 천연가스 충전소 설치 현황(ODcloud) + 주소 지오코딩 사본",
        f"data.go.kr {cng_gyeongnam_source.CNG_GYEONGNAM_DATASET_ID}",
        cng_gyeongnam_source.CNG_GYEONGNAM_DATASET_PAGE_URL,
    ),
    _snapshot_file(
        "gg_chemical", "경기 유해화학물질 취급사업장 사본", gg_chemical_source.DEFAULT_SNAPSHOT_PATH,
        "1차 마목 유독물 참고 핀(경기 한정 · 판정 아님)의 전량 목록입니다. 위험물 Rule 이 사업지 "
        "지역과 무관하게 이 목록을 묻기 때문에 첫 심사가 기다리지 않게 실어 둡니다.",
        "경기데이터드림 유해화학물질 취급사업장 현황 사본", "openapi.gg.go.kr ChmstryMttrBizplc",
        gg_chemical_source.GG_CHEMICAL_DATASET_PAGE_URL,
    ),
    _snapshot_file(
        "casino_registry", "카지노영업소 좌표 사본", casino_source.DEFAULT_SNAPSHOT_PATH,
        "1차 카지노영업소(25m · 다자녀) 명단 18곳의 주소 지오코딩 좌표입니다. 명단은 코드에 있고 "
        "여기에는 좌표만 둡니다(켤 때마다 다시 지오코딩하지 않습니다).",
        "한국카지노업관광협회 회원사 명단(코드) + 주소 지오코딩 좌표",
        f"명단 기준 {casino_source.CASINO_REGISTRY_AS_OF}", casino_source.CASINO_REGISTRY_URL,
        note=COORDINATE_NOTE,
    ),
    _snapshot_file(
        "city_gas_registry", "도시가스 제조시설 좌표 사본", city_gas_source.DEFAULT_SNAPSHOT_PATH,
        "1차 도시가스 제조시설 명단 12곳의 주소 지오코딩 좌표입니다. 명단은 코드에 있고 여기에는 "
        "좌표만 둡니다.",
        "도시가스 제조시설 명단(코드) + 주소 지오코딩 좌표",
        f"명단 기준 {city_gas_source.CITY_GAS_REGISTRY_AS_OF}",
        city_gas_source.CITY_GAS_REGISTRY_URL, note=COORDINATE_NOTE,
    ),
)


def snapshot_file_names() -> list[str]:
    """API 사본·캐시 파일 이름(backend/data 기준 상대 경로). 야간 갱신이 주고받을 파일이다."""

    names: list[str] = []
    for entry in BUNDLED_FILES:
        if not entry.api_snapshot:
            continue
        try:
            names.append(entry.path.relative_to(DATA_DIR).as_posix())
        except ValueError:
            names.append(entry.path.name)
    return names


def bundled_files_response() -> BundledFilesResponse:
    return BundledFilesResponse(
        description=(
            "공공 API 로 온전히 받을 수 없어 파일로 서버에 실은 자료입니다. 파일마다 쓰임·출처·"
            "기준일·건수를 보입니다."
        ),
        files=[entry.view() for entry in BUNDLED_FILES],
    )
