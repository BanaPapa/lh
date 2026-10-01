"""서울 열린데이터광장 「서울시 버스정류소 위치정보」(busStopLocationXyInfo).

국토부 TAGO 정류소 근접조회는 서울을 제공하지 않아(2026-09-16 실측: 잠실 0건) 서울
사업지의 버스정류장이 0으로 나왔다. 서울시 API 는 정류소 전량(약 11,200건)을 주므로
받아서 24시간 캐시하고 반경으로 거른다. 인증키는 data.seoul.go.kr 에서 즉시 발급된다.

응답(실측): {"busStopLocationXyInfo": {"list_total_count": N, "RESULT": {"CODE": "INFO-000"},
"row": [{"STOPS_NO", "STOPS_NM", "XCRD"(경도), "YCRD"(위도), "NODE_ID", "STOPS_TYPE"}]}}
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import PublicDataAPIError
from app.services.snapshot_store import DATA_DIR, SnapshotList, trim_rows

SEOUL_BUS_URL = "http://openapi.seoul.go.kr:8088/{key}/json/busStopLocationXyInfo/{start}/{end}/"
PAGE_SIZE = 1000
MAX_PAGES = 30
CACHE_TTL_SECONDS = 24 * 3600
# 정류소 전량 서버 사본(services/snapshot_store). 서울 사업지의 첫 심사가 12쪽을 기다리지
# 않는다. 앱 배선만 이 경로를 준다.
DEFAULT_SNAPSHOT_PATH = DATA_DIR / "seoul_bus_stops_cache.json"
SNAPSHOT_KEY = "bus_stops"
SNAPSHOT_COLUMNS = ("STOPS_NO", "STOPS_NM", "XCRD", "YCRD", "STOPS_TYPE")
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


def stops_from_rows(rows: list[Any]) -> list[SeoulBusStop]:
    """정류소 행 목록 → 버스정류소(API 행·사본 행 공통)."""

    return [
        stop for stop in (_stop(row) for row in rows if isinstance(row, dict)) if stop is not None
    ]


class SeoulBusStopClient:
    def __init__(
        self,
        api_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        snapshot_path: Path | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport
        # 정류소 전량(메모리 하루 캐시 + 서버 사본). 동시 조회는 한 번만 원격을 탄다.
        self._list: SnapshotList[SeoulBusStop] = SnapshotList(
            snapshot_path, SNAPSHOT_KEY, "서울시 버스정류소 위치정보",
            fetch=self._fetch_rows, parse=stops_from_rows, error=PublicDataAPIError,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def snapshot_notice(self) -> str:
        """받은 지 하루 넘은 목록(서버 사본)으로 답하고 있으면 그 고지. 아니면 빈 문자열."""

        return self._list.notice

    @property
    def data_as_of(self) -> datetime | None:
        return self._list.as_of

    async def stops_around(self, center: Coordinates, radius_m: float) -> list[SeoulBusStop]:
        if not in_seoul(center):
            return []
        stops = await self.all_stops()
        return [s for s in stops if haversine_meters(center, s.coordinates) <= radius_m]

    async def all_stops(self) -> list[SeoulBusStop]:
        """서울 전량. 메모리 → 서버 사본(하루 넘었으면 뒤에서 갱신) → API 순."""

        if not self.enabled:
            return []
        return await self._list.get()

    async def refresh(self, force: bool = False) -> list[SeoulBusStop]:
        """전량을 API 에서 새로 받아 메모리와 서버 사본을 갈아 끼운다."""

        if not self.enabled:
            return []
        return await self._list.refresh(force)

    async def _fetch_rows(self, _previous: list[SeoulBusStop]) -> list[dict[str, Any]]:
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
        return trim_rows(rows, SNAPSHOT_COLUMNS)

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
