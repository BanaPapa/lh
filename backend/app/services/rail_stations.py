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

서버 사본(2026-10-01): 받아 둔 역 목록을 파일(korail_stations_cache.json)로 실어 서버가
켜지자마자 쓰고, 하루가 지났으면 뒤에서 새로 받는다(snapshot_store 참고).
"""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.snapshot_store import DATA_DIR, SnapshotList, trim_rows


KORAIL_STATION_DATASET_ID = "15127532"
KORAIL_STATION_URL = (
    "https://api.odcloud.kr/api/15127532/v1/uddi:c1d09745-9e5c-48e4-b26c-c1833592509c"
)
KORAIL_STATION_SOURCE = "한국철도공사 역위치 정보"

PAGE_SIZE = 1000
MAX_PAGES = 5
CACHE_TTL_SECONDS = 24 * 60 * 60

# 역 목록 사본(앱 배선만 이 경로를 준다)과 사본에 남기는 열.
DEFAULT_SNAPSHOT_PATH = DATA_DIR / "korail_stations_cache.json"
SNAPSHOT_KEY = "stations"
SNAPSHOT_COLUMNS = ("지역본부", "역명", "위도", "경도", "출입구 개수")

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


def stations_from_rows(rows: list[Any]) -> list[StationRecord]:
    """역위치 행 목록 → 역 목록(API 행·사본 행 공통)."""

    return [
        record
        for record in (station_record(row) for row in rows if isinstance(row, dict))
        if record is not None
    ]


class KorailStationClient:
    """한국철도공사 역위치 정보 전량(202역) → 하루 캐시(+ 서버 사본) → 반경 필터."""

    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        snapshot_path: Path | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self._list: SnapshotList[StationRecord] = SnapshotList(
            snapshot_path, SNAPSHOT_KEY, KORAIL_STATION_SOURCE,
            fetch=self._fetch_rows, parse=stations_from_rows, error=KorailStationAPIError,
            empty_message="한국철도공사 역위치 정보가 비어 있습니다.",
        )

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    @property
    def snapshot_notice(self) -> str:
        """받은 지 하루 넘은 목록(서버 사본)으로 답하고 있으면 그 고지. 아니면 빈 문자열."""

        return self._list.notice

    @property
    def data_as_of(self) -> datetime | None:
        return self._list.as_of

    async def stations_around(
        self, center: Coordinates, radius_m: float
    ) -> list[StationRecord]:
        stations = await self._all_stations()
        return [s for s in stations if haversine_meters(center, s.coordinates) <= radius_m]

    async def _all_stations(self) -> list[StationRecord]:
        """전국 역 목록. 메모리 → 서버 사본(하루 넘었으면 뒤에서 갱신) → API 순."""

        return await self._list.get()

    async def refresh(self, force: bool = False) -> list[StationRecord]:
        """전국 목록을 API 에서 새로 받아 메모리와 서버 사본을 갈아 끼운다."""

        return await self._list.refresh(force)

    async def _fetch_rows(self, _previous: list[StationRecord]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                page_rows, total = await self._page(client, page)
                rows.extend(page_rows)
                if len(page_rows) < PAGE_SIZE or page * PAGE_SIZE >= total:
                    break
        return trim_rows(rows, SNAPSHOT_COLUMNS)

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
