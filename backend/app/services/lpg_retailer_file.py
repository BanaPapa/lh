"""한국가스안전공사 전국 LPG 판매소 현황 — ODcloud 15091481 (파일 변환 API).

H-02-나 나목 「LPG 판매소」의 전국 원천이다. 열은 업소명·주소·일련번호 세 개뿐이고
좌표가 없어 주소를 카카오로 지오코딩한다(4,542건, 2026-09-17 실측 · 기준일 2024-03-05
일회성 자료라 폐업·신규가 반영되지 않는다 — 화면 문구로 밝힌다). 지오코딩 결과는
FacilityStore 에 `lpg_retailer_file` 로 저장해 STORE_MAX_AGE 안이면 API·지오코딩을
다시 하지 않는다. 일련번호가 비는 행이 있어 record_id 는 「일련번호 또는 업소명+주소」다.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.address_candidates import address_candidates
from app.services.facility_store import FacilityStore
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import PublicDataAPIError
from app.services.localdata import LocalDataRecord
from app.services.single_flight import LoopSafeLock

LPG_RETAILER_DATASET_ID = "15091481"
LPG_RETAILER_URL = (
    "https://api.odcloud.kr/api/15091481/v1/uddi:f40c83ef-d9dd-49b7-a912-bfbdfad65118"
)
LPG_RETAILER_DATASET_PAGE_URL = "https://www.data.go.kr/data/15091481/fileData.do"
LPG_RETAILER_AS_OF = "2024-03-05"
STORE_DATASET_KEY = "lpg_retailer_file"
# 일회성 자료(2024-03)라 자주 다시 받을 이유가 없다. 지오코딩 4,542건이 비싸므로 30일.
STORE_MAX_AGE = timedelta(days=30)

PAGE_SIZE = 1000
MAX_PAGES = 10
CACHE_TTL_SECONDS = 24 * 60 * 60
RETRY_STATUSES = (429, 502, 503)
MAX_RETRIES = 4
RETRY_BASE_SECONDS = 0.8

NAME_KEYS = ("업소명", "상호", "업체명")
ADDRESS_KEYS = ("주소", "소재지", "도로명주소")
ID_KEYS = ("일련번호", "연번")

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


class LpgRetailer(NamedTuple):
    record_id: str
    name: str
    address: str
    coordinates: Coordinates


class GeocodeFailure(NamedTuple):
    name: str
    address: str


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def record_id_for(row: dict[str, Any]) -> str:
    serial = _pick(row, ID_KEYS)
    if serial:
        return serial
    digest = hashlib.sha1(
        f"{_pick(row, NAME_KEYS)}|{_pick(row, ADDRESS_KEYS)}".encode()
    ).hexdigest()[:12]
    return f"h{digest}"


class LpgRetailerFileClient:
    """전국 LPG 판매소 파일 전량 → 지오코딩 → 캐시(메모리 24시간 · 저장소 30일)."""

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
        self._cache: list[LpgRetailer] = []
        self._failures: list[GeocodeFailure] = []
        self._cached_at = 0.0
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

    async def retailers_around(self, center: Coordinates, radius_m: float) -> list[LpgRetailer]:
        retailers = await self.all_retailers()
        return [r for r in retailers if haversine_meters(center, r.coordinates) <= radius_m]

    async def all_retailers(self) -> list[LpgRetailer]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache
        async with self._fill_lock.get():
            if self._is_warm():
                return self._cache
            stored = self._load_from_store()
            if stored is not None:
                retailers, failures = stored, []
                self.loaded_from = "store"
            else:
                retailers, failures = await self._load()
                self.loaded_from = "api"
                self.synced_at = datetime.now(UTC)
                self._save_to_store(retailers)
            self._cache = retailers
            self._failures = failures
            self._cached_at = time.monotonic()
            return retailers

    async def probe_total(self) -> int:
        """첫 1건만 불러 키·엔드포인트 생존과 총건수를 확인한다(예열 전 점검용)."""

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            _, total = await self._page(client, 1, per_page=1)
        return total

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    async def _load(self) -> tuple[list[LpgRetailer], list[GeocodeFailure]]:
        assert self._geocode is not None
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                page_rows, total = await self._page(client, page)
                rows.extend(page_rows)
                if not page_rows or len(rows) >= total:
                    break
        retailers: list[LpgRetailer] = []
        failures: list[GeocodeFailure] = []
        for row in rows:
            name = _pick(row, NAME_KEYS)
            address = _pick(row, ADDRESS_KEYS)
            if not name:
                continue
            if not address:
                failures.append(GeocodeFailure(name, ""))
                continue
            coordinates = await self._geocode_any(address)
            if coordinates is None:
                failures.append(GeocodeFailure(name, address))
                continue
            retailers.append(LpgRetailer(record_id_for(row), name, address, coordinates))
        return retailers, failures

    async def _geocode_any(self, address: str) -> Coordinates | None:
        assert self._geocode is not None
        for candidate in address_candidates(address):
            coordinates = await self._geocode(candidate)
            if coordinates is not None:
                return coordinates
        return None

    def _load_from_store(self) -> list[LpgRetailer] | None:
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
            LpgRetailer(row.record_id, row.name, row.address, row.coordinates)
            for row in self._store.dataset_records(STORE_DATASET_KEY)
        ]

    def _save_to_store(self, retailers: list[LpgRetailer]) -> None:
        if self._store is None or not retailers:
            return
        records = [
            LocalDataRecord(
                dataset_key=STORE_DATASET_KEY, record_id=r.record_id, name=r.name,
                address=r.address, road_address="", coordinates=r.coordinates,
                status="등록", category="LPG 판매소",
            )
            for r in retailers
        ]
        try:
            self._store.replace_dataset(STORE_DATASET_KEY, records)
        except Exception:  # noqa: BLE001 — 저장 실패는 캐시 효율 문제일 뿐
            return

    async def _page(
        self, client: httpx.AsyncClient, page: int, per_page: int = PAGE_SIZE
    ) -> tuple[list[dict[str, Any]], int]:
        params = {
            "serviceKey": self.service_key,
            "page": str(page),
            "perPage": str(per_page),
            "returnType": "JSON",
        }
        response: httpx.Response | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.get(LPG_RETAILER_URL, params=params)
            except httpx.HTTPError as exc:
                raise PublicDataAPIError(f"LPG 판매소 파일 조회에 실패했습니다: {exc}") from exc
            if response.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                break
            await asyncio.sleep(RETRY_BASE_SECONDS * (2 ** attempt))
        assert response is not None
        if response.status_code == 401:
            raise PublicDataAPIError(
                "LPG 판매소 파일(15091481) 활용신청이 필요합니다 (401).", 401
            )
        if response.status_code != 200:
            raise PublicDataAPIError(
                f"LPG 판매소 파일 응답 오류 ({response.status_code})", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError("LPG 판매소 파일 응답을 해석하지 못했습니다.") from exc
        if not isinstance(payload, dict):
            raise PublicDataAPIError("LPG 판매소 파일 응답 형식이 다릅니다.")
        if "data" not in payload:
            raise PublicDataAPIError(
                f"LPG 판매소 파일 오류: {payload.get('msg') or payload.get('code')}"
            )
        rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]
        try:
            total = int(payload.get("totalCount"))
        except (TypeError, ValueError):
            total = MAX_PAGES * PAGE_SIZE
        return rows, total
