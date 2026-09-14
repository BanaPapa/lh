"""행정안전부 지방행정인허가 개방 API(apis.data.go.kr/1741000) 조회.

숙박업·유흥주점·단란주점·대기배출사업장 등 영업허가 대장을 준다. 상업 지도
키워드 추정과 달리 실제 인허가 근거라 유해시설 판정의 공식 원천이 된다.

이 계열의 제약 두 가지가 설계를 결정한다.

1. 지역 필터가 없다. localCode·adresCode·lawdCd 등을 모두 시도했지만 전건이
   그대로 돌아온다. 사업지 주변만 뽑아올 방법이 없다.
2. numOfRows 가 100 으로 강제된다. 500 이나 1000 을 넣어도 100 건만 온다.

그래서 전량을 받아 로컬에 적재하고(sync) 조회는 로컬에서 한다.
좌표는 EPSG:5174(구 중부원점, Bessel)다. 위경도로 착각하면 안 된다.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any, NamedTuple

import httpx
from pyproj import Transformer

from app.models import Coordinates
from app.services.http_client import shared_verify


LOCALDATA_BASE = "https://apis.data.go.kr/1741000"

# 공공데이터포털 공통 상한. 더 큰 값을 넣어도 100건만 온다.
PAGE_SIZE = 100

# 동시 요청 수. 8이면 5만 건 데이터셋을 15초 안에 받는다. 더 올리면 429가 난다.
CONCURRENCY = 6

# 포털이 429(요청 과다)를 돌려줄 때의 재시도. 데이터셋을 연달아 받으면 걸린다.
MAX_RETRIES = 5
RETRY_BASE_SECONDS = 1.5

# 인허가 좌표계. 위경도가 아니라 구 중부원점 평면좌표다.
LOCALDATA_CRS = "EPSG:5174"

# 적재 대상 영업상태. 실측 코드는 01 영업/정상 · 02 휴업 · 03 폐업이다.
#
# 휴업을 여기서 버리면 안 된다. 판정층(rulebook.operating_state)이 「폐업·취소·
# 말소는 제외, 휴업·공란은 확정하지 않고 검토로 남긴다」는 정책을 이미 갖고 있는데,
# 적재층이 먼저 걸러 버리면 그 정책이 한 번도 실행되지 않는다. 실제로 그렇게
# 돌고 있었다 — 적재된 172,770행의 상태값이 「영업/정상」 하나뿐이었고, 휴업
# 주유소가 기준거리 안에 있어도 앱이 잡지 못했다(2026-09-11 회의 안건 ②, 041
# 진북동 사례). 폐업만 적재에서 버리고, 휴업을 판정에 올릴지는 판정층이 정한다.
INGESTED_STATUS_CODES: frozenset[str] = frozenset({"01", "02"})


class LocalDataAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LocalDataSet(NamedTuple):
    """인허가 데이터셋 하나와 유해시설 분류의 대응.

    slug 은 apis.data.go.kr/1741000/{slug}/info 의 경로 조각이다.

    체크리스트가 테마파크 3종·무도학원업만 `/info` 없이 표기해 한동안 그대로
    따랐으나, 2026-08-27 실호출로 네 경로 모두 `/info` 가 있어야 존재함을
    확인했다. `/info` 없이 부르면 400 NO_OPENAPI_SERVICE_ERROR 가 나고, 붙이면
    403 SERVICE_KEY_IS_NOT_REGISTERED_ERROR 가 난다. 즉 엔드포인트는 있고
    막고 있는 것은 활용신청뿐이라, 접미사 예외를 두지 않는다.
    """

    key: str
    label: str
    slug: str
    facility_type: str
    facility_type_label: str
    # 접미사 예외를 다시 만들어야 할 원천이 생길 때만 False 로 둔다.
    info_suffix: bool = True
    # 엔드포인트가 실제 응답을 주는지 확인되지 않은 경로. 미검증 경로는 적재
    # 전까지 해당 카테고리를 dataset_missing 으로 두되, "장애"가 아니라
    # "미검증"임을 note 로 구분한다. 실제 적재되면 자동으로 판정에 든다.
    verified: bool = True
    # category 로 쓸 응답 필드의 우선순위. 앞의 것이 비어 있으면 다음으로 넘어간다.
    # 기본값은 업태(BZSTAT_SE_NM) 우선인데, 판정 키가 업태가 아닌 원천은 여기서
    # 뒤집는다(예: 대기배출사업장의 종별). category 는 판정 분기에 쓰이므로 어느
    # 필드가 오는지가 곧 판정의 정확도다.
    category_fields: tuple[str, ...] = ("BZSTAT_SE_NM", "BTP_NM")


# 슬러그는 모두 박진주 대표 확정 체크리스트(jeonbuk_checklist_260826.xlsx)의
# 명시 엔드포인트에서 가져온다. 추정 슬러그(구 gas_stations·cng_stations)는
# 실엔드포인트 미확인이라 제거했다. 주유소·CNG 는 API 가 아니라 CSV 원천이므로
# 여기(1741000 API 레지스트리)에 두지 않는다.
DATASETS: tuple[LocalDataSet, ...] = (
    LocalDataSet(
        key="lodgings",
        label="숙박업",
        slug="lodgings",
        facility_type="lodging",
        facility_type_label="숙박시설",
    ),
    LocalDataSet(
        key="entertainment_bars",
        label="유흥주점영업",
        slug="entertainment_bars",
        facility_type="entertainment_bar",
        facility_type_label="유흥주점",
    ),
    LocalDataSet(
        key="singing_bars",
        label="단란주점영업",
        slug="singing_bars",
        facility_type="singing_bar",
        facility_type_label="단란주점",
    ),
    LocalDataSet(
        key="air_pollution",
        label="대기오염물질배출시설설치사업장",
        slug="air_pollution_facility_installation",
        facility_type="polluting_factory",
        facility_type_label="대기배출사업장",
        # 종별(1~5종)이 공장 나·다목 AND 의 판정 키다(H-01-나 §1 · H-01-다 §1).
        # 2026-09-08 실호출: BTP_NM(종별)은 100% 채워져 있고 BZSTAT_SE_NM(업태)이
        # 71% 에 있다. 기본 우선순위(업태 먼저)를 쓰면 종별이 업태에 덮여, 전북
        # 2,552건 중 1,367건(53%)의 종별이 사라져 나·다목 배정이 불가능해졌다.
        # 업태는 판정에 쓰지 않으므로 extra(BZSTAT_SE_NM)로 보존만 한다.
        category_fields=("BTP_NM",),
    ),
    LocalDataSet(
        key="oil_retailers",
        label="석유판매업",
        slug="oil_retailers",
        facility_type="oil_retailer",
        facility_type_label="석유판매업소",
    ),
    # 체크리스트 가목(석유대체연료판매)·사목(도료류 판매소)이 함께 지목한 원천.
    LocalDataSet(
        key="petroleum_alt_fuel_retailers",
        label="석유및석유대체연료판매업",
        slug="petroleum_alt_fuel_retailers",
        facility_type="oil_retailer",
        facility_type_label="석유대체연료판매업소",
    ),
    # 위험물 라목(액화가스 취급소) — 도시가스·고압가스업.
    LocalDataSet(
        key="city_gas_companies",
        label="일반도시가스업",
        slug="city_gas_companies",
        facility_type="high_pressure_gas",
        facility_type_label="일반도시가스업체",
    ),
    LocalDataSet(
        key="high_pressure_gas",
        label="고압가스업",
        slug="high_pressure_gas",
        facility_type="high_pressure_gas",
        facility_type_label="고압가스업소",
    ),
    # 위락 다목 테마파크 3종. 활용신청 승인 전까지 403 이 나는 상태다(2026-08-27 확인).
    LocalDataSet(
        key="comprehensive_amusement_facilities",
        label="종합테마파크업",
        slug="comprehensive_amusement_facilities",
        facility_type="theme_park_comprehensive",
        facility_type_label="종합테마파크업",
    ),
    LocalDataSet(
        key="general_amusement_facilities",
        label="일반테마파크업",
        slug="general_amusement_facilities",
        facility_type="theme_park_general",
        facility_type_label="일반테마파크업",
    ),
    LocalDataSet(
        key="amusement_facilities_other",
        label="기타테마파크업",
        slug="amusement_facilities_other",
        facility_type="theme_park_other",
        facility_type_label="기타테마파크업",
    ),
    # 2차 생활편의성 상업시설. 심사표가 "대규모점포 조회에 나오는 것만 인정"으로
    # 조회처를 못박은 항목이라 지도 검색으로 근사할 수 없다(카카오 '대형마트'
    # 분류에는 동네 마트가 섞인다). 슬러그는 박진주 대표 체크리스트 명시값이며
    # 2026-08-28 실호출에서 200(totalCount 4,183) 으로 활용신청 승인을 확인했다.
    # 응답 스키마는 표준(BPLC_NM·CRD_INFO_X/Y·SALS_STTS_CD·MNG_NO)이라
    # parse_record 를 그대로 쓴다. BZSTAT_SE_NM 에 '대규모점포' 업태가 온다.
    LocalDataSet(
        key="large_scale_retail_stores",
        label="대규모점포",
        slug="large_scale_retail_stores",
        facility_type="large_retail_store",
        facility_type_label="대규모점포",
    ),
    # 위험물 바목 특정고압가스 사용신고. 2026-08-28 실호출에서 200(totalCount
    # 11,250) 으로 활용신청 승인을 확인했다. 응답 스키마는 표준이라 parse_record
    # 를 그대로 쓴다(BZSTAT_SE_NM·BTP_NM 은 비어 있어 category 는 공란).
    LocalDataSet(
        key="specific_high_pressure_gas",
        label="특정고압가스업",
        slug="specific_high_pressure_gas",
        facility_type="high_pressure_gas",
        facility_type_label="특정고압가스업소",
    ),
    # 위락 마목 무도 2종. 둘 다 활용신청 승인 전까지 403 이 난다(2026-08-27 확인).
    LocalDataSet(
        key="dance_halls",
        label="무도장업",
        slug="dance_halls",
        facility_type="dance_hall",
        facility_type_label="무도장",
    ),
    LocalDataSet(
        key="dance_academies",
        label="무도학원업",
        slug="dance_academies",
        facility_type="dance_academy",
        facility_type_label="무도학원",
    ),
)

DATASET_BY_KEY = {dataset.key: dataset for dataset in DATASETS}


# 판정 필터에 쓰는 부가 컬럼. 응답에 있고 값이 비어 있지 않을 때만 extra 에 담는다.
# - MNFTR_SE_NM(제조구분)·BPLC_SIE_USG_SE_NM(사업소용도): 고압가스 자가설비 판별
#   (docs/hazards H-02-라바 §7-1 — 「냉동」은 건물 냉방용 자가설비)
# - BLDG_USG_NM(건축물용도): 테마파크 체육시설·근린생활시설 판별 (H-04-다 §6-1)
# - CULTR_SPTS_TPBIZ_NM(문화체육업종): 무도장·무도학원 구분
# - USE_PRPS(사용목적): 특정고압가스 사용신고 성격 확인 (H-02-라바 §7-3)
EXTRA_FIELDS: tuple[str, ...] = (
    # 대기배출사업장의 업태. category 를 종별로 쓰므로 업태는 여기 남긴다
    # (공장 여부를 사람이 되짚을 때 필요하다 — 편의점·정비소 식별).
    "BZSTAT_SE_NM",
    "MNFTR_SE_NM",
    "BPLC_SIE_USG_SE_NM",
    "BLDG_USG_NM",
    "CULTR_SPTS_TPBIZ_NM",
    "USE_PRPS",
)


class LocalDataRecord(NamedTuple):
    """인허가 사업장 한 곳."""

    dataset_key: str
    record_id: str
    name: str
    address: str
    road_address: str
    coordinates: Coordinates
    status: str
    category: str
    # 판정 필터용 부가 컬럼(EXTRA_FIELDS). 읽기 전용으로 다룬다.
    extra: dict[str, str] = {}


def _first_field(row: dict[str, Any], fields: tuple[str, ...]) -> str:
    """우선순위대로 훑어 처음 비어 있지 않은 값을 돌려준다. 전부 비면 빈 문자열."""

    for name in fields:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


@lru_cache(maxsize=1)
def _to_wgs84() -> Transformer:
    return Transformer.from_crs(LOCALDATA_CRS, "EPSG:4326", always_xy=True)


def parse_record(
    dataset: LocalDataSet,
    row: dict[str, Any],
) -> LocalDataRecord | None:
    """응답 한 행을 좌표까지 변환해 담는다. 좌표가 없거나 폐업이면 버린다."""

    if str(row.get("SALS_STTS_CD") or "").strip() not in INGESTED_STATUS_CODES:
        return None
    x = row.get("CRD_INFO_X")
    y = row.get("CRD_INFO_Y")
    if x in (None, "") or y in (None, ""):
        return None
    try:
        lng, lat = _to_wgs84().transform(float(x), float(y))
    except (TypeError, ValueError):
        return None
    # 변환 결과가 한반도 밖이면 원본 좌표 오류로 보고 버린다.
    if not (33.0 <= lat <= 39.5 and 124.0 <= lng <= 132.0):
        return None

    record_id = str(row.get("MNG_NO") or "").strip()
    if not record_id:
        record_id = f"{x}:{y}:{row.get('BPLC_NM') or ''}"
    return LocalDataRecord(
        dataset_key=dataset.key,
        record_id=record_id,
        name=str(row.get("BPLC_NM") or "").strip(),
        address=str(row.get("LOTNO_ADDR") or "").strip(),
        road_address=str(row.get("ROAD_NM_ADDR") or "").strip(),
        coordinates=Coordinates(lat=lat, lng=lng),
        status=str(row.get("SALS_STTS_NM") or "").strip(),
        # 어느 필드를 category 로 삼을지는 데이터셋이 정한다(LocalDataSet.category_fields).
        # 대기배출사업장은 종별(BTP_NM)이 판정 키라 업태보다 앞선다.
        category=_first_field(row, dataset.category_fields),
        extra={
            key: str(row.get(key) or "").strip()
            for key in EXTRA_FIELDS
            if str(row.get(key) or "").strip()
        },
    )


class LocalDataClient:
    def __init__(
        self,
        service_key: str,
        timeout: float = 40.0,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = CONCURRENCY,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self.concurrency = max(1, concurrency)

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    async def total_count(self, dataset: LocalDataSet) -> int:
        """전체 레코드 수. numOfRows=1 로 물어야 레코드 수가 온다."""

        async with self._client() as client:
            body = await self._page(client, dataset, page=1, size=1)
        try:
            return int(body.get("totalCount") or 0)
        except (TypeError, ValueError):
            return 0

    async def fetch_all(self, dataset: LocalDataSet) -> list[LocalDataRecord]:
        """데이터셋 전량. 페이지를 동시에 받아 시간을 줄인다."""

        if not self.enabled:
            return []
        total = await self.total_count(dataset)
        if total <= 0:
            return []
        pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

        semaphore = asyncio.Semaphore(self.concurrency)
        async with self._client() as client:

            async def one(page: int) -> list[dict[str, Any]]:
                async with semaphore:
                    body = await self._page(client, dataset, page, PAGE_SIZE)
                return _items(body)

            batches = await asyncio.gather(
                *[one(page) for page in range(1, pages + 1)]
            )

        records: list[LocalDataRecord] = []
        for rows in batches:
            for row in rows:
                record = parse_record(dataset, row)
                if record:
                    records.append(record)
        return records

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        )

    async def _page(
        self,
        client: httpx.AsyncClient,
        dataset: LocalDataSet,
        page: int,
        size: int,
    ) -> dict[str, Any]:
        params = {
            "serviceKey": self.service_key,
            "pageNo": str(page),
            "numOfRows": str(size),
            "type": "json",
        }
        suffix = "/info" if dataset.info_suffix else ""
        url = f"{LOCALDATA_BASE}/{dataset.slug}{suffix}"
        response = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise LocalDataAPIError(
                        f"{dataset.label} 조회에 실패했습니다: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_SECONDS * (2**attempt))
                continue
            # 429는 잠시 뒤 다시 하면 풀린다. 그 외 오류는 재시도해도 같다.
            if response.status_code != 429:
                break
            if attempt == MAX_RETRIES - 1:
                raise LocalDataAPIError(
                    f"{dataset.label} 요청이 과다합니다. 잠시 뒤 다시 시도하세요.",
                    429,
                )
            await asyncio.sleep(RETRY_BASE_SECONDS * (2**attempt))

        if response is None or response.status_code != 200:
            raise LocalDataAPIError(
                f"{dataset.label} 응답 오류 "
                f"({response.status_code if response else '응답 없음'})",
                response.status_code if response else None,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LocalDataAPIError(
                f"{dataset.label} 응답을 해석하지 못했습니다."
            ) from exc
        return (payload.get("response") or {}).get("body") or {}


def _items(body: dict[str, Any]) -> list[dict[str, Any]]:
    items = body.get("items") or []
    if isinstance(items, dict):
        items = items.get("item") or []
    return [row for row in items if isinstance(row, dict)]
