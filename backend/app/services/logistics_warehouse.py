"""국토교통부 물류창고업 등록정보(apis.data.go.kr/1611000/whsinfoview2) — 환경부 등록 창고.

물류시설법 등록 창고 5,977건(2026-09-17)에 타 법률 창고가 섞여 있고, 창고번호 다섯 번째
글자가 등록 법률 구분이다(실측): C 환경부 화학물질관리법 보관·저장(286) · M 관세청 보세 ·
S 물류시설법 일반 · A/F/T 식품·수산·축산 냉동냉장 · P 항만. C 그룹은 유해화학물질
「보관·저장업」 영업허가 창고라 H-02-마 마목 「유독물 보관·저장시설」에 가장 가까운 전국
자료다. 판매시설은 없다.

앱에서는 **참고 핀**이다(판정 아님) — 마목은 LH [요청 2] 승인으로 「판정 미적용 — 별도
수기 확인」이고, 이 원장은 취급 물질이 유독물질인지 알려 주지 않는다.

호출 방식(실측):
- 목록 `WhsInfoList?serviceKey&pageNo&numOfRows(≤500)&type=json` → COMPANY_NAME ·
  WARE_NO · STORAGE_ITEM 만. 주소가 없다.
- 상세 `WhsInfoDetail?serviceKey&wareNo=<WARE_NO>&pageNo=1&numOfRows=10&type=json`
  → COMPANY_ADDRESS 가 여기서만 나온다. `sday/eday` 를 붙이면 0건이 되므로 넣지 않는다.
  파라미터 이름은 대소문자 무관(wareNo · WARENO 모두 동작).
- 좌표가 없어 주소를 카카오로 지오코딩한다. C 그룹만 상세·지오코딩하므로 286회다.
- 초당 1건 제한이라 전량 수집에 6분쯤 걸린다. 그래서 수집 결과를 FacilityStore(SQLite)에
  데이터셋 `logistics_chem_warehouse` 로 저장해 두고, STORE_MAX_AGE 안이면 API 를 다시
  부르지 않고 저장분을 쓴다(기동 즉시 사용 가능). 이 API 는 지역 조회가 없어 「검색한
  지역만」 받는 방식은 불가능하다 — 주소는 상세조회에만 있고 창고번호도 시도 코드가 아니다.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.address_candidates import address_candidates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.facility_store import FacilityStore
from app.services.kgs import PublicDataAPIError
from app.services.localdata import LocalDataRecord
from app.services.safemap_facilities import SafemapFacility
from app.services.single_flight import LoopSafeLock

WAREHOUSE_BASE_URL = "https://apis.data.go.kr/1611000/whsinfoview2"
WAREHOUSE_LIST_URL = f"{WAREHOUSE_BASE_URL}/WhsInfoList"
WAREHOUSE_DETAIL_URL = f"{WAREHOUSE_BASE_URL}/WhsInfoDetail"
WAREHOUSE_DATASET_PAGE_URL = "https://www.data.go.kr/data/3048029/openapi.do"
WAREHOUSE_LAYER_ID = "NLIC_CHEM"
# FacilityStore 데이터셋 키. 원장이 매일 갱신되지만 환경부 창고 변동은 드물어 7일 보관.
STORE_DATASET_KEY = "logistics_chem_warehouse"
STORE_MAX_AGE = timedelta(days=7)
# 창고번호 5번째 글자 — 환경부(화학물질관리법 보관·저장업) 등록 창고.
CHEMICAL_GROUP_CODE = "C"
CHEMICAL_KIND_LABEL = "보관·저장업(환경부 등록 창고)"

PAGE_SIZE = 500
MAX_PAGES = 40
# 포털이 상세조회를 초당 몇 건으로 제한한다(동시 4건에서 429, 2026-09-17). 순차 + 재시도.
DETAIL_CONCURRENCY = 1
RETRY_STATUSES = (429, 502, 503)
MAX_RETRIES = 5
RETRY_BASE_SECONDS = 0.8
# 상세조회는 초당 1건 남짓만 허용된다(LIMITED_NUMBER_OF_SERVICE_REQUESTS_PER_SECOND, 2026-09-17).
# 286건이라 예열에 5분쯤 걸리지만 백그라운드 예열이라 감수한다.
DETAIL_MIN_INTERVAL_SECONDS = 1.1
CACHE_TTL_SECONDS = 24 * 60 * 60

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


class GeocodeFailure(NamedTuple):
    name: str
    address: str


def is_chemical_warehouse(ware_no: str) -> bool:
    return len(ware_no) > 4 and ware_no[4] == CHEMICAL_GROUP_CODE


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


class LogisticsWarehouseClient:
    """C 그룹 창고를 목록→상세→지오코딩으로 모아 캐시(24시간)하고 반경으로 거른다.

    SafemapFacilityFeed 와 같은 모양이라 hazard_review 의 참고 핀 헬퍼가 그대로 받는다.
    """

    layer_id = WAREHOUSE_LAYER_ID

    def __init__(
        self,
        service_key: str,
        geocode: Geocoder | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        store: FacilityStore | None = None,
    ) -> None:
        self.service_key = service_key
        self._geocode = geocode
        self.timeout = timeout
        self._transport = transport
        self._store = store
        self._cache: list[SafemapFacility] = []
        self._failures: list[GeocodeFailure] = []
        self._cached_at = 0.0
        # 마지막으로 채운 경로. "store"(저장분 재사용) 또는 "api"(전량 수집).
        self.loaded_from = ""
        self.synced_at: datetime | None = None
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key and self._geocode is not None)

    @property
    def geocode_failures(self) -> list[GeocodeFailure]:
        return list(self._failures)

    @property
    def is_warm(self) -> bool:
        return self._is_warm()

    @property
    def has_fast_path(self) -> bool:
        """메모리 캐시가 데워졌거나 저장분이 신선해 즉시 답할 수 있는가.

        아니면 첫 호출이 전량 수집·지오코딩으로 수 분을 막으므로, 판정 경로는 이 값이
        False 인 동안 이 원천을 「미연결」로 다루고 기동 예열이 채우기를 기다린다.
        """

        if self._is_warm():
            return True
        if self._store is None:
            return False
        state = self._store.sync_state_for(STORE_DATASET_KEY)
        if state is None or state.record_count == 0:
            return False
        synced = state.synced_at
        if synced.tzinfo is None:
            synced = synced.replace(tzinfo=UTC)
        return datetime.now(UTC) - synced <= STORE_MAX_AGE

    async def probe_total(self) -> int:
        """목록 첫 1건만 불러 키·엔드포인트가 살아 있는지와 전체 창고 수를 확인한다.

        전량 예열은 상세조회 286건을 초당 1건으로 돌려 6분쯤 걸리므로, 설정 화면의
        60초 점검은 캐시가 차기 전에는 이 가벼운 호출로 대신한다.
        """

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            _, total = await self._call(
                client, WAREHOUSE_LIST_URL, {"pageNo": "1", "numOfRows": "1"}
            )
        return total

    async def facilities_around(
        self, center: Coordinates, radius_m: float
    ) -> list[SafemapFacility]:
        facilities = await self.all_facilities()
        return [f for f in facilities if haversine_meters(center, f.coordinates) <= radius_m]

    async def all_facilities(self) -> list[SafemapFacility]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache
        async with self._fill_lock.get():
            if self._is_warm():
                return self._cache
            stored = self._load_from_store()
            if stored is not None:
                facilities, failures = stored, []
                self.loaded_from = "store"
            else:
                facilities, failures = await self._load()
                self.loaded_from = "api"
                self.synced_at = datetime.now(UTC)
                self._save_to_store(facilities)
            self._cache = facilities
            self._failures = failures
            self._cached_at = time.monotonic()
            return facilities

    def _load_from_store(self) -> list[SafemapFacility] | None:
        """저장분이 있고 STORE_MAX_AGE 안이면 그것을 쓴다. 없거나 오래됐으면 None."""

        if self._store is None:
            return None
        state = self._store.sync_state_for(STORE_DATASET_KEY)
        if state is None or state.record_count == 0:
            return None
        synced = state.synced_at
        if synced.tzinfo is None:
            synced = synced.replace(tzinfo=UTC)
        if datetime.now(UTC) - synced > STORE_MAX_AGE:
            return None
        self.synced_at = synced
        return [
            SafemapFacility(
                WAREHOUSE_LAYER_ID, row.record_id, row.name, row.address,
                row.category or CHEMICAL_KIND_LABEL, row.coordinates,
            )
            for row in self._store.dataset_records(STORE_DATASET_KEY)
        ]

    def _save_to_store(self, facilities: list[SafemapFacility]) -> None:
        if self._store is None or not facilities:
            return
        records = [
            LocalDataRecord(
                dataset_key=STORE_DATASET_KEY, record_id=f.record_id, name=f.name,
                address=f.address, road_address="", coordinates=f.coordinates,
                status="등록", category=f.kind,
            )
            for f in facilities
        ]
        try:
            self._store.replace_dataset(STORE_DATASET_KEY, records)
        except Exception:  # noqa: BLE001 — 저장 실패는 캐시 효율 문제일 뿐 판정과 무관
            return

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    async def _load(self) -> tuple[list[SafemapFacility], list[GeocodeFailure]]:
        assert self._geocode is not None
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            rows = await self._list_rows(client)
            chemical = [row for row in rows if is_chemical_warehouse(_text(row, "WARE_NO"))]
            semaphore = asyncio.Semaphore(DETAIL_CONCURRENCY)

            async def detail(row: dict[str, Any]) -> dict[str, Any]:
                async with semaphore:
                    item = await self._detail(client, _text(row, "WARE_NO"))
                    await asyncio.sleep(DETAIL_MIN_INTERVAL_SECONDS)
                    return item

            details = await asyncio.gather(*(detail(row) for row in chemical))

        facilities: list[SafemapFacility] = []
        failures: list[GeocodeFailure] = []
        for row, item in zip(chemical, details, strict=True):
            ware_no = _text(row, "WARE_NO")
            name = _text(item, "COMPANY_NAME") or _text(row, "COMPANY_NAME")
            address = _text(item, "COMPANY_ADDRESS")
            if not name:
                continue
            if not address:
                failures.append(GeocodeFailure(name, ""))
                continue
            coordinates = await self._geocode_any(address)
            if coordinates is None:
                failures.append(GeocodeFailure(name, address))
                continue
            facilities.append(
                SafemapFacility(
                    WAREHOUSE_LAYER_ID, ware_no, name, address, CHEMICAL_KIND_LABEL, coordinates
                )
            )
        return facilities, failures

    async def _geocode_any(self, address: str) -> Coordinates | None:
        assert self._geocode is not None
        for candidate in address_candidates(address):
            coordinates = await self._geocode(candidate)
            if coordinates is not None:
                return coordinates
        return None

    async def _list_rows(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            items, total = await self._call(
                client, WAREHOUSE_LIST_URL,
                {"pageNo": str(page), "numOfRows": str(PAGE_SIZE)},
            )
            rows.extend(items)
            if not items or len(rows) >= total:
                break
        return rows

    async def _detail(self, client: httpx.AsyncClient, ware_no: str) -> dict[str, Any]:
        items, _ = await self._call(
            client, WAREHOUSE_DETAIL_URL,
            {"wareNo": ware_no, "pageNo": "1", "numOfRows": "10"},
        )
        return items[0] if items else {}

    async def _call(
        self, client: httpx.AsyncClient, url: str, extra: dict[str, str]
    ) -> tuple[list[dict[str, Any]], int]:
        params = {"serviceKey": self.service_key, "type": "json", **extra}
        response: httpx.Response | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.get(url, params=params)
            except httpx.HTTPError as exc:
                raise PublicDataAPIError(
                    f"물류창고업 등록정보 조회에 실패했습니다: {exc}"
                ) from exc
            if response.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                break
            await asyncio.sleep(RETRY_BASE_SECONDS * (2 ** attempt))
        assert response is not None
        if response.status_code != 200:
            raise PublicDataAPIError(
                f"물류창고업 등록정보 응답 오류 ({response.status_code})", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError("물류창고업 등록정보 응답을 해석하지 못했습니다.") from exc
        return _unwrap(payload)


def _unwrap(payload: Any) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(payload, dict):
        raise PublicDataAPIError("물류창고업 등록정보 응답 형식이 다릅니다.")
    # 공공데이터포털 공통 오류 봉투(미등록 키 등).
    portal = payload.get("OpenAPI_ServiceResponse")
    if isinstance(portal, dict):
        header = portal.get("cmmMsgHeader") or {}
        raise PublicDataAPIError(
            f"물류창고업 등록정보 오류: {header.get('errMsg', '')} "
            f"({header.get('returnReasonCode', '')})"
        )
    header = payload.get("header") or {}
    if str(header.get("ResultCode", "0")) not in ("0", "00"):
        raise PublicDataAPIError(
            f"물류창고업 등록정보 오류 {header.get('ResultCode')}: {header.get('resultMsg', '')}"
        )
    items = [row for row in (payload.get("items") or []) if isinstance(row, dict)]
    try:
        total = int(header.get("TotalCount"))
    except (TypeError, ValueError):
        total = MAX_PAGES * PAGE_SIZE
    return items, total
