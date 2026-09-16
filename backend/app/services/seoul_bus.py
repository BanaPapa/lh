"""서울 열린데이터광장 「서울시 버스정류소 위치정보」(busStopLocationXyInfo).

국토부 TAGO 정류소 근접조회는 서울을 제공하지 않아(2026-09-16 실측: 잠실 0건) 서울
사업지의 버스정류장이 0으로 나왔다. 서울시 API 는 정류소 전량(약 11,200건)을 주므로
받아서 24시간 캐시하고 반경으로 거른다. 인증키는 data.seoul.go.kr 에서 즉시 발급된다.

응답(실측): {"busStopLocationXyInfo": {"list_total_count": N, "RESULT": {"CODE": "INFO-000"},
"row": [{"STOPS_NO", "STOPS_NM", "XCRD"(경도), "YCRD"(위도), "NODE_ID", "STOPS_TYPE"}]}}
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import PublicDataAPIError
from app.services.single_flight import LoopSafeLock

SEOUL_BUS_URL = "http://openapi.seoul.go.kr:8088/{key}/json/busStopLocationXyInfo/{start}/{end}/"
PAGE_SIZE = 1000
MAX_PAGES = 30
CACHE_TTL_SECONDS = 24 * 3600
# 서울시 관할 대략 범위. 이 밖의 사업지는 서울 API 를 묻지 않는다.
SEOUL_BOUNDS = (37.41, 37.72, 126.73, 127.20)
# 정류소 유형 중 버스가 아닌 것(한강선착장 등)은 뺀다.
NON_BUS_TYPES: tuple[str, ...] = ("선착장",)


class SeoulBusStop(NamedTuple):
    stop_no: str
    name: str
    coordinates: Coordinates
    stop_type: str


def in_seoul(point: Coordinates) -> bool:
    south, north, west, east = SEOUL_BOUNDS
    return south <= point.lat <= north and west <= point.lng <= east


def _stop(row: dict[str, Any]) -> SeoulBusStop | None:
    try:
        lat = float(str(row.get("YCRD") or "").strip())
        lng = float(str(row.get("XCRD") or "").strip())
    except ValueError:
        return None
    if not in_seoul(Coordinates(lat=lat, lng=lng)):
        return None
    name = str(row.get("STOPS_NM") or "").strip()
    stop_type = str(row.get("STOPS_TYPE") or "").strip()
    if not name or any(token in stop_type for token in NON_BUS_TYPES):
        return None
    return SeoulBusStop(
        stop_no=str(row.get("STOPS_NO") or "").strip(),
        name=name,
        coordinates=Coordinates(lat=lat, lng=lng),
        stop_type=stop_type,
    )


class SeoulBusStopClient:
    def __init__(
        self,
        api_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport
        self._cache: list[SeoulBusStop] = []
        self._cached_at = 0.0
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def stops_around(self, center: Coordinates, radius_m: float) -> list[SeoulBusStop]:
        if not in_seoul(center):
            return []
        stops = await self.all_stops()
        return [s for s in stops if haversine_meters(center, s.coordinates) <= radius_m]

    async def all_stops(self) -> list[SeoulBusStop]:
        if not self.enabled:
            return []
        if self._cache and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS:
            return self._cache
        async with self._fill_lock.get():
            if self._cache and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS:
                return self._cache
            rows: list[dict[str, Any]] = []
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport, verify=shared_verify()
            ) as client:
                for page in range(MAX_PAGES):
                    start = page * PAGE_SIZE + 1
                    page_rows, total = await self._page(client, start, start + PAGE_SIZE - 1)
                    rows.extend(page_rows)
                    if len(page_rows) < PAGE_SIZE or len(rows) >= total:
                        break
            stops = [s for s in (_stop(row) for row in rows) if s is not None]
            self._cache = stops
            self._cached_at = time.monotonic()
            return stops

    async def _page(
        self, client: httpx.AsyncClient, start: int, end: int
    ) -> tuple[list[dict[str, Any]], int]:
        url = SEOUL_BUS_URL.format(key=self.api_key, start=start, end=end)
        response = await client.get(url)
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError(
                f"서울 정류소 응답을 읽지 못했습니다 (HTTP {response.status_code})"
            ) from exc
        body = payload.get("busStopLocationXyInfo")
        if not isinstance(body, dict):
            result = payload.get("RESULT") or {}
            raise PublicDataAPIError(
                f"서울 정류소 조회 실패 ({result.get('CODE')}) {result.get('MESSAGE') or ''}".strip()
            )
        code = (body.get("RESULT") or {}).get("CODE", "")
        if code and not code.startswith("INFO-000"):
            raise PublicDataAPIError(
                f"서울 정류소 조회 실패 ({code}) {(body.get('RESULT') or {}).get('MESSAGE') or ''}".strip()
            )
        return list(body.get("row") or []), int(body.get("list_total_count") or 0)
