"""경상남도 천연가스 충전소 설치 현황 — ODcloud 15055157 (파일 변환 API).

가스안전공사 전국 도시가스(CNG) 충전소 현황(cng.py, 15001508)의 지역 보조 원천이다.
경남 12곳(2026-09-17 실측)뿐이고 좌표가 없어 주소(「위치」)를 카카오로 지오코딩한다.
전국 현황과 같은 자리(40m)면 판정 병합에서 중복으로 버리고, 전국 현황에 빠진 곳만
보태는 용도다. 파일 버전이 둘(2019-01 · 최신)인데 최신본만 쓴다.

응답 열(최신본): 충전소명 · 위치 · 시군명 · 도시가스공급사 · 연번.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.cng import CngStation
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import PublicDataAPIError
from app.services.snapshot_store import DATA_DIR, SnapshotList

CNG_GYEONGNAM_DATASET_ID = "15055157"
CNG_GYEONGNAM_URL = (
    "https://api.odcloud.kr/api/15055157/v1/"
    "uddi:4c3bf683-f91d-4113-a00e-8d755c9f1591"
)
CNG_GYEONGNAM_DATASET_PAGE_URL = "https://www.data.go.kr/data/15055157/fileData.do"

PAGE_SIZE = 100
MAX_PAGES = 5
CACHE_TTL_SECONDS = 24 * 60 * 60

# 좌표까지 붙인 목록 서버 사본(services/snapshot_store). 앱 배선만 이 경로를 준다.
DEFAULT_SNAPSHOT_PATH = DATA_DIR / "cng_gyeongnam_cache.json"
SNAPSHOT_KEY = CNG_GYEONGNAM_DATASET_ID

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


class GeocodeFailure(NamedTuple):
    name: str
    address: str


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def snapshot_row(station: CngStation) -> dict[str, Any]:
    """사본 한 행 — 지오코딩한 좌표까지 둔다(켜질 때마다 다시 지오코딩하지 않는다)."""

    return {
        "name": station.name,
        "addr": station.address,
        "region": station.region,
        "branch": station.branch,
        "lat": station.coordinates.lat,
        "lng": station.coordinates.lng,
    }


def stations_from_snapshot(rows: list[Any]) -> list[CngStation]:
    stations: list[CngStation] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            coordinates = Coordinates(lat=float(row["lat"]), lng=float(row["lng"]))
        except (KeyError, TypeError, ValueError):
            continue
        stations.append(
            CngStation(
                name=str(row.get("name") or ""),
                address=str(row.get("addr") or ""),
                region=str(row.get("region") or ""),
                coordinates=coordinates,
                branch=str(row.get("branch") or ""),
            )
        )
    return stations


class CngGyeongnamClient:
    """경남 CNG 충전소 목록을 받아 지오코딩해 캐시한다(메모리 하루 + 서버 사본)."""

    def __init__(
        self,
        service_key: str,
        geocode: Geocoder | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        snapshot_path: Path | None = None,
    ) -> None:
        self.service_key = service_key
        self._geocode = geocode
        self.timeout = timeout
        self._transport = transport
        self._failures: list[GeocodeFailure] = []
        self._list: SnapshotList[CngStation] = SnapshotList(
            snapshot_path, SNAPSHOT_KEY, "경상남도 천연가스 충전소",
            fetch=self._located_rows, parse=stations_from_snapshot, error=PublicDataAPIError,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.service_key and self._geocode is not None)

    @property
    def geocode_failures(self) -> list[GeocodeFailure]:
        return list(self._failures)

    @property
    def snapshot_notice(self) -> str:
        """받은 지 하루 넘은 목록(서버 사본)으로 답하고 있으면 그 고지. 아니면 빈 문자열."""

        return self._list.notice

    @property
    def data_as_of(self) -> datetime | None:
        return self._list.as_of

    async def stations_around(
        self, center: Coordinates, radius_m: float
    ) -> list[CngStation]:
        stations = await self.all_stations()
        return [s for s in stations if haversine_meters(center, s.coordinates) <= radius_m]

    async def all_stations(self) -> list[CngStation]:
        """경남 목록. 메모리 → 서버 사본(하루 넘었으면 뒤에서 갱신) → API 순."""

        if not self.enabled:
            return []
        return await self._list.get()

    async def refresh(self, force: bool = False) -> list[CngStation]:
        """목록을 새로 받아 좌표를 붙이고 메모리와 서버 사본을 갈아 끼운다."""

        if not self.enabled:
            return []
        return await self._list.refresh(force)

    async def _located_rows(self, previous: list[CngStation]) -> list[dict[str, Any]]:
        """파일 행을 받아 좌표를 붙인 사본 행으로. 이름·주소가 그대로면 직전 좌표를 다시 쓴다."""

        rows = await self._fetch_rows()
        assert self._geocode is not None
        known = {(s.name, s.address): s.coordinates for s in previous}
        stations: list[CngStation] = []
        failures: list[GeocodeFailure] = []
        for row in rows:
            name = _text(row, "충전소명")
            address = _text(row, "위치")
            if not name or not address:
                continue
            coordinates = known.get((name, address)) or await self._geocode(address)
            if coordinates is None:
                failures.append(GeocodeFailure(name, address))
                continue
            stations.append(
                CngStation(
                    name=name,
                    address=address,
                    region=f"경남 {_text(row, '시군명')}".strip(),
                    coordinates=coordinates,
                    branch=_text(row, "도시가스공급사"),
                )
            )
        self._failures = failures
        return [snapshot_row(station) for station in stations]

    async def _fetch_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                page_rows, total = await self._page(client, page)
                rows.extend(page_rows)
                if len(page_rows) < PAGE_SIZE or len(rows) >= total:
                    break
        return rows

    async def _page(
        self, client: httpx.AsyncClient, page: int
    ) -> tuple[list[dict[str, Any]], int]:
        params = {
            "serviceKey": self.service_key,
            "page": str(page),
            "perPage": str(PAGE_SIZE),
            "returnType": "JSON",
        }
        try:
            response = await client.get(CNG_GYEONGNAM_URL, params=params)
        except httpx.HTTPError as exc:
            raise PublicDataAPIError(f"경남 CNG 충전소 조회에 실패했습니다: {exc}") from exc
        if response.status_code != 200:
            raise PublicDataAPIError(
                f"경남 CNG 충전소 응답 오류 ({response.status_code})", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError("경남 CNG 충전소 응답을 해석하지 못했습니다.") from exc
        if not isinstance(payload, dict):
            raise PublicDataAPIError("경남 CNG 충전소 응답 형식이 다릅니다.")
        rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]
        try:
            total = int(payload.get("totalCount"))
        except (TypeError, ValueError):
            total = MAX_PAGES * PAGE_SIZE
        return rows, total
