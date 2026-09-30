"""철도역 원천 — 한국철도공사 역위치 정보(15127532)와 LH 데이터셋 역 출구 좌표.

LH 내부망 앱 표준 데이터셋의 철도역(JB_48, 13점)·KTX역(JB_49, 7점)은 한국철도공사 역위치
정보에서 역을 고르고 역마다 출구 좌표를 담당자가 수기로 모은 점이다(「익산역출구1」·
「익산역출구2」). 이 앱은
    1) 역 목록 — 한국철도공사 역위치 정보(ODcloud 15127532). 실호출(2026-09-30 승인 직후):
       전국 202역, 역명은 「전주」처럼 「역」 없이 온다. 전북본부 12역(군산·김제·남원·대야·
       삼례·신태인·오수·익산·임실·전주·정읍·함열)이 LH 철도역·KTX역 목록의 역과 같다.
       장애 동안에는 VWorld 장소검색 「철도역」(일반·고속철도역 분류)으로 대체한다 — 이
       분류도 전북은 같은 12역이다.
    2) 출구 좌표 — LH 데이터셋이 모은 출구점을 저장소 CSV(backend/data/station_exits_
       jeonbuk.csv)로 두고 그 역은 이 점에서만 잰다. 지도 회사 출구 검색(카카오 번호별·
       네이버)에 기대지 않으므로 어느 지도가 살아 있든 같은 거리가 나온다. CSV 에 없는
       역(전북 밖)만 지도 출구 검색을 쓴다.

ODcloud 응답 필드(swagger 확인): 지역본부·역명·위도·경도·출입구 개수.
"""

from __future__ import annotations

import asyncio
import csv
import re
import time
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify


KORAIL_STATION_DATASET_ID = "15127532"
KORAIL_STATION_URL = (
    "https://api.odcloud.kr/api/15127532/v1/uddi:c1d09745-9e5c-48e4-b26c-c1833592509c"
)
KORAIL_STATION_SOURCE = "한국철도공사 역위치 정보"

PAGE_SIZE = 1000
MAX_PAGES = 5
CACHE_TTL_SECONDS = 24 * 60 * 60

STATION_EXITS_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "station_exits_jeonbuk.csv"
)
STATION_EXIT_DATASET_SOURCE = "LH 데이터셋 역 출구 좌표(철도역 JB_48·KTX역 JB_49)"
# 역 좌표에서 이 거리 안의 출구만 그 역 출구로 본다(동명 역 방지).
STATION_EXIT_MAX_M = 1_500.0

_PAREN_RE = re.compile(r"\([^)]*\)")


def station_name(raw: str) -> str:
    """역 이름을 「○○역」 꼴로 맞춘다.

    원천마다 표기가 다르다 — VWorld 고속철도역은 「익산」·「전주」, 한국철도공사는 역명만,
    지하철은 「군자(능동)역」·「군자역(능동)」. 괄호 부기를 떼고 「역」으로 끝나게 한다.
    """

    text = _PAREN_RE.sub("", raw or "").strip()
    text = re.sub(r"\s+", "", text)
    if not text:
        return ""
    return text if text.endswith("역") else f"{text}역"


class StationRecord(NamedTuple):
    name: str
    region: str
    coordinates: Coordinates
    exit_count: int


class StationExit(NamedTuple):
    label: str
    station: str
    coordinates: Coordinates


class KorailStationAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def unapproved(self) -> bool:
        return self.status_code in (401, 403)


def station_record(row: dict[str, Any]) -> StationRecord | None:
    name = station_name(str(row.get("역명") or ""))
    try:
        coordinates = Coordinates(lat=float(row.get("위도")), lng=float(row.get("경도")))
    except (TypeError, ValueError):
        return None
    if not name or not (33.0 <= coordinates.lat <= 39.5 and 124.0 <= coordinates.lng <= 132.0):
        return None
    try:
        exits = int(row.get("출입구 개수") or 0)
    except (TypeError, ValueError):
        exits = 0
    return StationRecord(name, str(row.get("지역본부") or "").strip(), coordinates, exits)


class KorailStationClient:
    """한국철도공사 역위치 정보 전량(202역) → 하루 캐시 → 반경 필터."""

    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self._loaded_at = 0.0
        self._stations: list[StationRecord] = []
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    async def stations_around(
        self, center: Coordinates, radius_m: float
    ) -> list[StationRecord]:
        stations = await self._all_stations()
        return [s for s in stations if haversine_meters(center, s.coordinates) <= radius_m]

    async def _all_stations(self) -> list[StationRecord]:
        if self._stations and time.monotonic() - self._loaded_at < CACHE_TTL_SECONDS:
            return self._stations
        async with self._lock:
            if self._stations and time.monotonic() - self._loaded_at < CACHE_TTL_SECONDS:
                return self._stations
            stations: list[StationRecord] = []
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport, verify=shared_verify()
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    rows, total = await self._page(client, page)
                    stations.extend(r for r in (station_record(row) for row in rows) if r)
                    if len(rows) < PAGE_SIZE or page * PAGE_SIZE >= total:
                        break
            if not stations:
                raise KorailStationAPIError("한국철도공사 역위치 정보가 비어 있습니다.")
            self._stations = stations
            self._loaded_at = time.monotonic()
            return stations

    async def _page(
        self, client: httpx.AsyncClient, page: int
    ) -> tuple[list[dict[str, Any]], int]:
        try:
            response = await client.get(
                KORAIL_STATION_URL,
                params={
                    "serviceKey": self.service_key,
                    "page": str(page),
                    "perPage": str(PAGE_SIZE),
                    "returnType": "JSON",
                },
            )
        except httpx.HTTPError as exc:
            raise KorailStationAPIError(f"한국철도공사 역위치 정보 요청 실패: {exc}") from exc
        if response.status_code != 200:
            raise KorailStationAPIError(
                f"한국철도공사 역위치 정보 HTTP {response.status_code}"
                + (" (활용신청 승인 전)" if response.status_code in (401, 403) else ""),
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise KorailStationAPIError("한국철도공사 역위치 정보 응답을 읽지 못했습니다.") from exc
        if not isinstance(payload, dict) or "data" not in payload:
            detail = payload.get("msg") if isinstance(payload, dict) else ""
            raise KorailStationAPIError(f"한국철도공사 역위치 정보 오류: {detail}")
        rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]
        try:
            total = int(payload.get("totalCount"))
        except (TypeError, ValueError):
            total = 0
        return rows, total


def load_station_exits(path: Path | None = None) -> tuple[StationExit, ...]:
    """LH 데이터셋 역 출구 CSV 를 읽는다. 없거나 깨졌으면 빈 튜플(지도 출구 검색으로 간다)."""

    target = path or STATION_EXITS_PATH
    try:
        with target.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return ()
    exits: list[StationExit] = []
    for row in rows:
        label = (row.get("exit_name") or "").strip()
        station = station_name(row.get("station") or "")
        try:
            point = Coordinates(lat=float(row["lat"]), lng=float(row["lng"]))
        except (KeyError, TypeError, ValueError):
            continue
        if label and station:
            exits.append(StationExit(label, station, point))
    return tuple(exits)


def dataset_exits_for(
    station: str,
    at: Coordinates,
    exits: tuple[StationExit, ...],
    max_m: float = STATION_EXIT_MAX_M,
) -> list[tuple[str, Coordinates]]:
    """역 이름이 같고 역 좌표에서 max_m 안인 LH 출구점."""

    target = station_name(station)
    return [
        (e.label, e.coordinates)
        for e in exits
        if e.station == target and haversine_meters(at, e.coordinates) <= max_m
    ]
