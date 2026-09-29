"""한국가스안전공사 가스제품 제조업소정보 — ODcloud 15152505 (파일 변환 API).

LH 내부망 앱은 표준 데이터셋 원천 22번(「가스제품 제조업소정보」 파일, JB_22_GAS_PRODUCT_
MANUFACTURERS)을 「위험물 저장 및 처리 시설」 50m 로 판정한다(전북 28행 · 업소 기준 19곳 —
같은 업소가 생산품목마다 한 줄씩 있어 표준본이 「중복되는 정보 존재」로 REVIEW 에 넣었다).
이 앱은 법 정의상 아목(도시가스 제조시설)이 아니라 싣지 않았으나, 2026-09-30 「LH 앱과
같은 결과」 방침에 따라 같은 원천을 공공 API 로 받아 같은 50m 로 판정한다(관리자 설정
「판정 옵션」 gas_product_manufacturers 스위치 · 기본 켬 = LH 기준).

API 실측(2026-09-30): swagger(infuser.odcloud.kr/oas/docs?namespace=15152505/v1)의 uddi 는
78eca758-…, 열은 행정구역·업소명·소재지·생산품목·영업상태·법구분 여섯 개, 전국 1,549행
(기준일 2025-09-30 · 일회성 자료). 좌표가 없어 소재지를 카카오로 지오코딩한다. 이 키로는
401 「유효하지 않은 인증키」 — 공공데이터포털에서 이 데이터셋을 활용신청해야 열린다.

같은 업소(업소명+소재지)는 생산품목·법구분을 모아 한 곳으로 합친다(LH 표준본의 「중복」
행을 한 번만 센다). 영업상태가 폐업·말소인 행은 뺀다(LH 적재 규칙 「폐업·말소 제외」).
지오코딩 결과는 FacilityStore 에 `gas_product_file` 로 30일 저장해 다시 하지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
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

GAS_PRODUCT_DATASET_ID = "15152505"
GAS_PRODUCT_URL = (
    "https://api.odcloud.kr/api/15152505/v1/uddi:78eca758-4256-4835-ac5a-841bd8f135b7"
)
GAS_PRODUCT_DATASET_PAGE_URL = "https://www.data.go.kr/data/15152505/fileData.do"
GAS_PRODUCT_AS_OF = "2025-09-30"
STORE_DATASET_KEY = "gas_product_file"
STORE_MAX_AGE = timedelta(days=30)

PAGE_SIZE = 1000
MAX_PAGES = 10
CACHE_TTL_SECONDS = 24 * 60 * 60
RETRY_STATUSES = (429, 502, 503)
MAX_RETRIES = 4
RETRY_BASE_SECONDS = 0.8
# 폐업·말소는 LH 적재 규칙처럼 뺀다. 휴업은 LH 도 싣는다(「휴업 포함」).
CLOSED_STATUS = re.compile(r"폐업|말소|취소")

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


class GasProductManufacturer(NamedTuple):
    record_id: str
    name: str
    address: str
    coordinates: Coordinates
    status: str
    # 생산품목(예: 「압력용기 · 저장탱크」)·법구분(고법·액법). 판정 근거가 아니라 표시용.
    products: str
    law: str


class GeocodeFailure(NamedTuple):
    name: str
    address: str


class _Place(NamedTuple):
    name: str
    address: str
    status: str
    products: tuple[str, ...]
    laws: tuple[str, ...]


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value is None else " ".join(str(value).split())


def merge_rows(rows: list[dict[str, Any]]) -> list[_Place]:
    """생산품목마다 한 줄인 원장을 업소(업소명+소재지) 단위로 합친다. 폐업·말소는 뺀다."""

    places: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        name = _text(row, "업소명")
        address = _text(row, "소재지")
        status = _text(row, "영업상태")
        if not name or CLOSED_STATUS.search(status):
            continue
        slot = places.setdefault(
            (name, address),
            {"status": status, "products": [], "laws": []},
        )
        for field, key in (("products", "생산품목"), ("laws", "법구분")):
            value = _text(row, key)
            if value and value not in slot[field]:
                slot[field].append(value)
    return [
        _Place(name, address, slot["status"], tuple(slot["products"]), tuple(slot["laws"]))
        for (name, address), slot in places.items()
    ]


def record_id_for(name: str, address: str) -> str:
    return "g" + hashlib.sha1(f"{name}|{address}".encode()).hexdigest()[:12]


class GasProductFileClient:
    """가스제품 제조업소 전량 → 업소 단위 병합 → 지오코딩 → 캐시(메모리 24시간 · 저장소 30일)."""

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
        self._cache: list[GasProductManufacturer] = []
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
        """메모리 캐시나 신선한 저장분이 있어 심사 중 전량 수집·지오코딩 없이 답할 수 있는가."""

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

    async def manufacturers_around(
        self, center: Coordinates, radius_m: float
    ) -> list[GasProductManufacturer]:
        rows = await self.all_manufacturers()
        return [r for r in rows if haversine_meters(center, r.coordinates) <= radius_m]

    async def all_manufacturers(self) -> list[GasProductManufacturer]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache
        async with self._fill_lock.get():
            if self._is_warm():
                return self._cache
            stored = self._load_from_store()
            if stored is not None:
                rows, failures = stored, []
                self.loaded_from = "store"
            else:
                rows, failures = await self._load()
                self.loaded_from = "api"
                self.synced_at = datetime.now(UTC)
                self._save_to_store(rows)
            self._cache = rows
            self._failures = failures
            self._cached_at = time.monotonic()
            return rows

    async def probe_total(self) -> int:
        """첫 1건만 불러 키·엔드포인트 생존과 총건수를 확인한다(예열 전 점검용)."""

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            _, total = await self._page(client, 1, per_page=1)
        return total

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    async def _load(self) -> tuple[list[GasProductManufacturer], list[GeocodeFailure]]:
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
        found: list[GasProductManufacturer] = []
        failures: list[GeocodeFailure] = []
        for place in merge_rows(rows):
            coordinates = await self._geocode_any(place.address) if place.address else None
            if coordinates is None:
                failures.append(GeocodeFailure(place.name, place.address))
                continue
            found.append(
                GasProductManufacturer(
                    record_id=record_id_for(place.name, place.address),
                    name=place.name,
                    address=place.address,
                    coordinates=coordinates,
                    status=place.status or "영업",
                    products=" · ".join(place.products),
                    law=" · ".join(place.laws),
                )
            )
        return found, failures

    async def _geocode_any(self, address: str) -> Coordinates | None:
        assert self._geocode is not None
        for candidate in address_candidates(address):
            coordinates = await self._geocode(candidate)
            if coordinates is not None:
                return coordinates
        return None

    def _load_from_store(self) -> list[GasProductManufacturer] | None:
        if self._store is None or not self.has_fast_path:
            return None
        state = self._store.sync_state_for(STORE_DATASET_KEY)
        if state is None:
            return None
        self.synced_at = state.synced_at
        return [
            GasProductManufacturer(
                row.record_id, row.name, row.address, row.coordinates, row.status,
                row.category, (row.extra or {}).get("law", ""),
            )
            for row in self._store.dataset_records(STORE_DATASET_KEY)
        ]

    def _save_to_store(self, rows: list[GasProductManufacturer]) -> None:
        if self._store is None or not rows:
            return
        records = [
            LocalDataRecord(
                dataset_key=STORE_DATASET_KEY, record_id=r.record_id, name=r.name,
                address=r.address, road_address="", coordinates=r.coordinates,
                status=r.status, category=r.products, extra={"law": r.law},
            )
            for r in rows
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
                response = await client.get(GAS_PRODUCT_URL, params=params)
            except httpx.HTTPError as exc:
                raise PublicDataAPIError(f"가스제품 제조업소 조회에 실패했습니다: {exc}") from exc
            if response.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                break
            await asyncio.sleep(RETRY_BASE_SECONDS * (2 ** attempt))
        assert response is not None
        if response.status_code == 401:
            raise PublicDataAPIError(
                "가스제품 제조업소정보(15152505) 활용신청이 필요합니다 (401).", 401
            )
        if response.status_code != 200:
            raise PublicDataAPIError(
                f"가스제품 제조업소 응답 오류 ({response.status_code})", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError("가스제품 제조업소 응답을 해석하지 못했습니다.") from exc
        if not isinstance(payload, dict) or "data" not in payload:
            message = payload.get("msg") if isinstance(payload, dict) else ""
            raise PublicDataAPIError(f"가스제품 제조업소 오류: {message or '형식 다름'}")
        rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]
        try:
            total = int(payload.get("totalCount"))
        except (TypeError, ValueError):
            total = MAX_PAGES * PAGE_SIZE
        return rows, total
