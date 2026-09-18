"""서울시 액화석유가스업 현황 — 서울 열린데이터광장 OpenAPI `SeoulListLPGSales`.

공공데이터포털의 서울 파일(15048520, 2018년 · 링크형)은 API 가 없고, 열린데이터광장이 같은
원장을 **실시간 OpenAPI** 로 준다(2026-09-17 실측 520건, 허가일 2026-04 행까지 있음).
열: 시군구코드(REL_TRANS_CGG_CODE) · 인허가번호 · 업소번호 · 사업종류(LPG_BSIN_SORT_CODE:
저장소설치 · 판매사업 · 충전사업 · 가스용품제조사업 …) · 상호 · 전화 · 허가일자 · 주소(SITE_ADDR).
좌표가 없어 주소를 카카오로 지오코딩한다. 서울 사업지에서만 쓴다.

호출: `http://openapi.seoul.go.kr:8088/{KEY}/json/SeoulListLPGSales/{start}/{end}/` —
버스정류소(seoul_bus)와 같은 인증키(SEOUL_OPEN_DATA_KEY)다. 한 번에 1,000행까지.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.models import Coordinates
from app.services.address_candidates import address_candidates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import PublicDataAPIError
from app.services.lpg_municipal import (
    GeocodeFailure,
    MunicipalFacility,
    MunicipalLookup,
    classify_kind,
    sido_of,
)
from app.services.single_flight import LoopSafeLock

SEOUL_LPG_SERVICE = "SeoulListLPGSales"
SEOUL_LPG_BASE = "http://openapi.seoul.go.kr:8088"
SEOUL_LPG_DATASET_ID = "OA-13652"
SEOUL_LPG_TITLE = "서울시 액화석유가스업 현황(열린데이터광장)"
SEOUL_LPG_PAGE_URL = "https://data.seoul.go.kr/dataList/OA-13652/S/1/datasetView.do"
PAGE_SIZE = 1000
MAX_PAGES = 5
CACHE_TTL_SECONDS = 24 * 60 * 60

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


def seoul_lpg_url(service_key: str, start: int, end: int) -> str:
    return f"{SEOUL_LPG_BASE}/{service_key}/json/{SEOUL_LPG_SERVICE}/{start}/{end}/"


def _text(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return "" if value in (None, "") else " ".join(str(value).split())


class SeoulLpgClient:
    """서울 액화석유가스업 전량(520건)을 지오코딩해 캐시하고, 서울 사업지에만 답한다.

    LpgMunicipalClient 와 같은 `facilities_for_site` 모양이라 서비스가 같은 루프로 돈다.
    """

    def __init__(
        self,
        service_key: str,
        geocode: Geocoder | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self._geocode = geocode
        self.timeout = timeout
        self._transport = transport
        self._cache: list[MunicipalFacility] = []
        self._failures: list[GeocodeFailure] = []
        self._cached_at = 0.0
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key and self._geocode is not None)

    @property
    def geocode_failures(self) -> list[GeocodeFailure]:
        return list(self._failures)

    @property
    def unavailable(self) -> dict[str, str]:
        return {}

    @property
    def has_fast_path(self) -> bool:
        """전량 지오코딩(약 500건)이 심사 중에 일어나지 않게, 캐시가 데워졌을 때만 준비로 본다."""

        return self._is_warm()

    async def facilities_for_site(
        self, site_address: str, center: Coordinates, radius_m: float
    ) -> MunicipalLookup:
        if not self.enabled or sido_of(site_address) != "서울":
            return MunicipalLookup([], [], {}, [])
        try:
            facilities = await self.all_facilities()
        except PublicDataAPIError as exc:
            return MunicipalLookup([], [], {SEOUL_LPG_DATASET_ID: str(exc)}, [])
        near = [f for f in facilities if haversine_meters(center, f.coordinates) <= radius_m]
        return MunicipalLookup(near, [SEOUL_LPG_DATASET_ID], {}, list(self._failures))

    async def probe_total(self) -> int:
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            _, total = await self._page(client, 1, 1)
        return total

    async def all_facilities(self) -> list[MunicipalFacility]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache
        async with self._fill_lock.get():
            if self._is_warm():
                return self._cache
            rows = await self._fetch_rows()
            facilities, failures = await self._geocode_rows(rows)
            self._cache = facilities
            self._failures = failures
            self._cached_at = time.monotonic()
            return facilities

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    async def _geocode_rows(
        self, rows: list[dict[str, Any]]
    ) -> tuple[list[MunicipalFacility], list[GeocodeFailure]]:
        assert self._geocode is not None
        facilities: list[MunicipalFacility] = []
        failures: list[GeocodeFailure] = []
        for index, row in enumerate(rows):
            name = _text(row, "TRNM_NM")
            address = _text(row, "SITE_ADDR")
            kind_raw = _text(row, "LPG_BSIN_SORT_CODE")
            if not name or not address:
                continue
            coordinates: Coordinates | None = None
            for candidate in address_candidates(address):
                coordinates = await self._geocode(candidate)
                if coordinates is not None:
                    break
            if coordinates is None:
                failures.append(GeocodeFailure(name, address))
                continue
            record_id = _text(row, "PERM_NT_NO") or f"row-{index}"
            facilities.append(
                MunicipalFacility(
                    SEOUL_LPG_DATASET_ID, SEOUL_LPG_TITLE, record_id, name, address,
                    classify_kind(kind_raw), kind_raw, _text(row, "PERM_NT_YMD"), coordinates,
                )
            )
        return facilities, failures

    async def _fetch_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            for page in range(MAX_PAGES):
                start = page * PAGE_SIZE + 1
                page_rows, total = await self._page(client, start, start + PAGE_SIZE - 1)
                rows.extend(page_rows)
                if not page_rows or len(rows) >= total:
                    break
        return rows

    async def _page(
        self, client: httpx.AsyncClient, start: int, end: int
    ) -> tuple[list[dict[str, Any]], int]:
        try:
            response = await client.get(seoul_lpg_url(self.service_key, start, end))
        except httpx.HTTPError as exc:
            raise PublicDataAPIError(f"서울 액화석유가스업 조회에 실패했습니다: {exc}") from exc
        if response.status_code != 200:
            raise PublicDataAPIError(
                f"서울 액화석유가스업 응답 오류 ({response.status_code})", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError("서울 액화석유가스업 응답을 해석하지 못했습니다.") from exc
        body = payload.get(SEOUL_LPG_SERVICE) if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            result = (payload or {}).get("RESULT", {}) if isinstance(payload, dict) else {}
            raise PublicDataAPIError(
                f"서울 액화석유가스업 오류 {result.get('CODE', '')}: {result.get('MESSAGE', '')}"
            )
        result = body.get("RESULT") or {}
        if str(result.get("CODE", "")) != "INFO-000":
            raise PublicDataAPIError(
                f"서울 액화석유가스업 오류 {result.get('CODE')}: {result.get('MESSAGE', '')}"
            )
        rows = [row for row in (body.get("row") or []) if isinstance(row, dict)]
        try:
            total = int(body.get("list_total_count"))
        except (TypeError, ValueError):
            total = MAX_PAGES * PAGE_SIZE
        return rows, total
