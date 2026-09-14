"""한국가스안전공사 전국 도시가스(CNG) 충전소 현황 — ODcloud 15001508.

2026-09-14 조사(docs/API_SOURCE_RESEARCH_2026-09-14 §1 #12)로 룰북·매트릭스의
「CNG 는 공개 API 없음」이 틀렸음이 확인됐다. 이 자료는 전국 190여 곳에 위경도가
있고 분기마다 갱신된다. 전량이 한 페이지 수준이라 전부 받아 메모리에 캐시한 뒤
좌표로 거른다(kgs.py 와 같은 구조).

ODcloud 파일변환 API 는 공공데이터포털 일반 API 와 응답 모양이 다르다.
{"page", "perPage", "totalCount", "currentCount", "data": [행…]} 이고 열 이름은
원본 파일의 한글 헤더 그대로다. 헤더는 갱신 시 바뀔 수 있어 흔한 별칭을 함께 받는다.

인증: 공공데이터포털 서비스키(`serviceKey` 쿼리). 데이터셋마다 활용신청이 따로
필요해 신청 전에는 401(`{"code":-401}`)이 온다 — 이 경우 PublicDataAPIError 로
올려 판정을 「없음」으로 위장하지 않는다.
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import (
    PublicDataAPIError,
    QuarantinedStation,
    coordinate_quarantine_reason,
)
from app.services.single_flight import LoopSafeLock


CNG_DATASET_ID = "15001508"
CNG_STATION_URL = (
    "https://api.odcloud.kr/api/15001508/v1/"
    "uddi:928477d9-ab3e-4b97-8731-d4ff962f0570"
)
# 공공데이터포털 데이터셋 페이지(활용신청 위치).
CNG_DATASET_PAGE_URL = "https://www.data.go.kr/data/15001508/fileData.do"

# ODcloud 는 perPage 상한이 넉넉하다. 전국 200건 미만이라 보통 1페이지로 끝난다.
PAGE_SIZE = 500

# 안전장치. 이 이상은 응답 이상으로 본다.
MAX_PAGES = 20

# 분기 갱신 자료라 하루 한 번이면 충분하다.
CACHE_TTL_SECONDS = 24 * 60 * 60

# 인증 실패(401/403 = 활용신청 전·키 오류)는 재시도해도 같은 답이다. 심사마다
# 헛호출하지 않도록 잠시 기억했다가 그동안은 호출 없이 같은 예외를 올린다.
# 캐시가 아니라 쿨다운이므로 판정은 계속 「원천 조회 실패」로 남는다.
AUTH_FAILURE_COOLDOWN_SECONDS = 10 * 60

# 열 이름 별칭. 앞에 있는 것부터 찾는다.
NAME_KEYS = ("시설명", "충전소명", "업소명", "사업장명", "BSES_NM", "NAME")
ADDRESS_KEYS = ("주소", "소재지", "소재지주소", "도로명주소", "ADDR", "ADDRESS")
LAT_KEYS = ("위도", "LAT", "latitude", "Y")
LNG_KEYS = ("경도", "LOT", "LON", "LNG", "longitude", "X")
REGION_KEYS = ("행정구역", "시도", "SECT_NM", "REGION")
BRANCH_KEYS = ("지사", "관할지사", "BRANCH")


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


class CngStation(NamedTuple):
    """CNG 충전소 한 곳."""

    name: str
    address: str
    region: str
    coordinates: Coordinates
    branch: str = ""

    @property
    def station_id(self) -> str:
        """이 자료는 고유키가 없어 좌표와 시설명으로 식별자를 만든다."""

        return (
            f"{self.coordinates.lat:.6f}:{self.coordinates.lng:.6f}:{self.name}"
        )


def _station_or_reason(row: dict[str, Any]) -> tuple[CngStation | None, str]:
    """행 하나를 충전소로 바꾼다. 격리 대상이면 (None, 사유)."""

    lat = _pick(row, LAT_KEYS)
    lng = _pick(row, LNG_KEYS)
    if not lat or not lng:
        return None, "좌표 결측"
    try:
        coordinates = Coordinates(lat=float(lat), lng=float(lng))
    except (TypeError, ValueError):
        return None, f"좌표 해석 불가(위도={lat!r}, 경도={lng!r})"
    address = _pick(row, ADDRESS_KEYS)
    reason = coordinate_quarantine_reason(address, coordinates)
    if reason:
        return None, reason
    return CngStation(
        name=_pick(row, NAME_KEYS),
        address=address,
        region=_pick(row, REGION_KEYS),
        coordinates=coordinates,
        branch=_pick(row, BRANCH_KEYS),
    ), ""


def _station(row: dict[str, Any]) -> CngStation | None:
    return _station_or_reason(row)[0]


class CngStationClient:
    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self._cache: list[CngStation] = []
        self._quarantined: list[QuarantinedStation] = []
        self._cached_at = 0.0
        # 마지막 인증 실패 시각·상태코드. 쿨다운 동안은 호출 없이 같은 예외를 올린다.
        self._auth_failed_at = 0.0
        self._auth_failed_status: int | None = None
        # 전국 목록 캐시 채우기를 직렬화하는 single-flight 잠금(캐시 스탬피드 방지).
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    @property
    def quarantined(self) -> list[QuarantinedStation]:
        """좌표 위생 검사로 격리된 충전소. 마지막 조회 기준."""

        return list(self._quarantined)

    def _ingest(self, rows: list[dict[str, Any]]) -> list[CngStation]:
        """행 목록을 충전소로 바꾸고 격리분은 사유와 함께 따로 모은다."""

        stations: list[CngStation] = []
        quarantined: list[QuarantinedStation] = []
        for row in rows:
            station, reason = _station_or_reason(row)
            if station is not None:
                stations.append(station)
            elif reason != "좌표 결측":
                quarantined.append(
                    QuarantinedStation(
                        name=_pick(row, NAME_KEYS),
                        address=_pick(row, ADDRESS_KEYS),
                        reason=reason,
                    )
                )
        self._quarantined = quarantined
        return stations

    async def stations_around(
        self,
        center: Coordinates,
        radius_m: float,
    ) -> list[CngStation]:
        """반경 안의 충전소. 전국 목록을 캐시해 두고 거리로 거른다."""

        stations = await self.all_stations()
        return [
            station
            for station in stations
            if haversine_meters(center, station.coordinates) <= radius_m
        ]

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    def _raise_if_auth_cooldown(self) -> None:
        """직전 인증 실패 뒤 쿨다운 중이면 호출 없이 같은 예외를 올린다."""

        if self._auth_failed_status is None:
            return
        if time.monotonic() - self._auth_failed_at >= AUTH_FAILURE_COOLDOWN_SECONDS:
            self._auth_failed_status = None
            return
        raise PublicDataAPIError(
            f"CNG 충전소 인증 실패 ({self._auth_failed_status}) — "
            "공공데이터포털 활용신청·서비스키를 확인해 주세요.",
            self._auth_failed_status,
        )

    async def all_stations(self) -> list[CngStation]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache

        async with self._fill_lock.get():
            if self._is_warm():
                return self._cache
            self._raise_if_auth_cooldown()

            all_rows: list[dict[str, Any]] = []
            # 조회가 실패하면 예외가 그대로 전파돼 캐시를 건드리지 않는다.
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                verify=shared_verify(),
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    try:
                        rows, total_count = await self._page(client, page)
                    except PublicDataAPIError as exc:
                        if exc.status_code in (401, 403):
                            self._auth_failed_at = time.monotonic()
                            self._auth_failed_status = exc.status_code
                        raise
                    all_rows.extend(rows)
                    if len(rows) < PAGE_SIZE or len(all_rows) >= total_count:
                        break

            stations = self._ingest(all_rows)
            self._cache = stations
            self._cached_at = time.monotonic()
            return stations

    async def _page(
        self,
        client: httpx.AsyncClient,
        page: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """한 페이지의 행과 전체 행 수(totalCount)."""

        params = {
            "serviceKey": self.service_key,
            "page": str(page),
            "perPage": str(PAGE_SIZE),
            "returnType": "JSON",
        }
        try:
            response = await client.get(CNG_STATION_URL, params=params)
        except httpx.HTTPError as exc:
            raise PublicDataAPIError(f"CNG 충전소 조회에 실패했습니다: {exc}") from exc

        if response.status_code != 200:
            raise PublicDataAPIError(
                f"CNG 충전소 응답 오류 ({response.status_code})",
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError("CNG 충전소 응답을 해석하지 못했습니다.") from exc
        if not isinstance(payload, dict):
            raise PublicDataAPIError("CNG 충전소 응답 형식이 다릅니다.")

        data = payload.get("data") or []
        rows = [row for row in data if isinstance(row, dict)]
        # totalCount 가 빠지면 「끝을 모른다」로 두고 페이지가 덜 찼을 때만 멈춘다
        # (len(rows) 로 대신하면 꽉 찬 첫 페이지에서 조기 종료된다).
        try:
            total_count = int(payload.get("totalCount"))
        except (TypeError, ValueError):
            total_count = MAX_PAGES * PAGE_SIZE
        return rows, total_count
