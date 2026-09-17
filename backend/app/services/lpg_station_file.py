"""한국가스안전공사 전국 LPG 충전소 현황 — ODcloud 15001643 (파일 변환 API).

가스안전공사 LPG 충전소 조회 API(kgs.py, B410019)의 보조 원천이다. 같은 기관의
같은 명부를 파일로 올린 것이라 대부분 겹치지만, 2026-09-17 활용신청 승인으로 쓸 수
있게 됐고 반기 갱신본에 위경도·관리구분(자동차/용기/13kg 용기)이 있어 교차 대조에
쓴다. 판정 병합 시 같은 자리(40m) LPG 충전소는 중복으로 버린다(API 먼저).

파일 버전(uddi)이 9개 올라와 있다(2020-08 ~ 2025-11). 최신본(_20251127)만 쓴다 —
2025-01 이전 버전에는 좌표 컬럼이 없다. 응답은 ODcloud 공통 모양
{"page","perPage","totalCount","currentCount","data":[행…]} 이고 열 이름은 한글이다.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import (
    LpgStation,
    PublicDataAPIError,
    QuarantinedStation,
    coordinate_quarantine_reason,
)
from app.services.single_flight import LoopSafeLock

LPG_FILE_DATASET_ID = "15001643"
# 최신본(한국가스안전공사_전국 LPG 충전소 현황_20251127).
LPG_FILE_URL = (
    "https://api.odcloud.kr/api/15001643/v1/"
    "uddi:79564ca7-f877-4eae-8022-d89f3ca65845"
)
LPG_FILE_DATASET_PAGE_URL = "https://www.data.go.kr/data/15001643/fileData.do"

PAGE_SIZE = 1000
MAX_PAGES = 10
CACHE_TTL_SECONDS = 24 * 60 * 60

NAME_KEYS = ("업소명", "충전소명", "시설명")
ADDRESS_KEYS = ("주소", "소재지")
LAT_KEYS = ("위도",)
LNG_KEYS = ("경도",)
REGION_KEYS = ("행정구역", "행정 구역", "지역")
USAGE_KEYS = ("관리구분",)


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def station_from_row(row: dict[str, Any]) -> tuple[LpgStation | None, str]:
    """행 하나를 충전소로. 격리 대상이면 (None, 사유)."""

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
    return LpgStation(
        name=_pick(row, NAME_KEYS),
        address=address,
        region=_pick(row, REGION_KEYS),
        coordinates=coordinates,
        usage=_pick(row, USAGE_KEYS),
    ), ""


class LpgStationFileClient:
    """전국 LPG 충전소 파일(15001643) 전량 캐시 + 반경 조회."""

    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self._cache: list[LpgStation] = []
        self._quarantined: list[QuarantinedStation] = []
        self._cached_at = 0.0
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    @property
    def quarantined(self) -> list[QuarantinedStation]:
        return list(self._quarantined)

    async def stations_around(
        self, center: Coordinates, radius_m: float
    ) -> list[LpgStation]:
        stations = await self.all_stations()
        return [s for s in stations if haversine_meters(center, s.coordinates) <= radius_m]

    async def all_stations(self) -> list[LpgStation]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache
        async with self._fill_lock.get():
            if self._is_warm():
                return self._cache
            rows: list[dict[str, Any]] = []
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport, verify=shared_verify()
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    page_rows, total = await self._page(client, page)
                    rows.extend(page_rows)
                    if len(page_rows) < PAGE_SIZE or len(rows) >= total:
                        break
            self._cache = self._ingest(rows)
            self._cached_at = time.monotonic()
            return self._cache

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    def _ingest(self, rows: list[dict[str, Any]]) -> list[LpgStation]:
        stations: list[LpgStation] = []
        quarantined: list[QuarantinedStation] = []
        for row in rows:
            station, reason = station_from_row(row)
            if station is not None:
                stations.append(station)
            elif reason != "좌표 결측":
                quarantined.append(
                    QuarantinedStation(
                        name=_pick(row, NAME_KEYS), address=_pick(row, ADDRESS_KEYS),
                        reason=reason,
                    )
                )
        self._quarantined = quarantined
        return stations

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
            response = await client.get(LPG_FILE_URL, params=params)
        except httpx.HTTPError as exc:
            raise PublicDataAPIError(f"LPG 충전소 파일 조회에 실패했습니다: {exc}") from exc
        if response.status_code != 200:
            raise PublicDataAPIError(
                f"LPG 충전소 파일 응답 오류 ({response.status_code})", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError("LPG 충전소 파일 응답을 해석하지 못했습니다.") from exc
        if not isinstance(payload, dict):
            raise PublicDataAPIError("LPG 충전소 파일 응답 형식이 다릅니다.")
        rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]
        try:
            total = int(payload.get("totalCount"))
        except (TypeError, ValueError):
            total = MAX_PAGES * PAGE_SIZE
        return rows, total
