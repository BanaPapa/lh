"""전국 화장시설 현황 조회 — 보건복지부(1352000) ODMS_DATA_05_1.

1차 매입제외 화장장(500m·전 유형) 판정의 정본 원천이다. 전북 5건짜리 CSV 보조와
달리 전국 62건을 준다. 사용자 원칙(API 우선)에 따라 이 API 를 정본으로 쓴다.

실호출로 확정한 사실(2026-08-28):
- 경로: `https://apis.data.go.kr/1352000/ODMS_DATA_05_1/callData05_1Api`
- 허용 파라미터는 `serviceKey · pageNo · numOfRows · apiType` 뿐이다. `type` 등을
  하나라도 더 넣으면 INVALID_REQUEST_PARAMETER_ERROR 가 난다. 그래서 localdata
  클라이언트(항상 type=json 을 붙인다)를 재사용하지 않고 별도로 호출한다.
- `apiType=JSON` 이면 JSON, 생략하면 XML. resultCode 00·totalCount 62.
- 좌표가 없다. 주소를 지오코딩해야 한다. 62건뿐이라 카카오 지오코딩으로 붙인다.
  지오코딩 실패 건은 삭제하지 않고 격리해 사유와 함께 남긴다(룰북 §7 ⑤).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.single_flight import LoopSafeLock


CREMATORIUM_URL = "https://apis.data.go.kr/1352000/ODMS_DATA_05_1/callData05_1Api"

PAGE_SIZE = 100
MAX_PAGES = 10

CACHE_TTL_SECONDS = 24 * 60 * 60

# 좌표 없는 원천을 주소로 붙이는 지오코더. Coordinates 또는 None(실패)을 준다.
Geocoder = Callable[[str], Awaitable[Coordinates | None]]

# 지오코딩보다 우선하는 검증 좌표. 시설명(공백 제거) → (lat, lng, 근거).
# H-06 §6-3 — 전주시승화원은 「효자동3가 3」을 지오코딩하면 실제 승화원(효자동3가
# 산 170-1 · 콩쥐팥쥐로 1705-138)과 1,931m 어긋난다. 500m 기준 항목에서 이 오차는
# 판정을 뒤집으므로, VWorld 도로명·카카오 키워드 교차확인으로 확정한 값을 고정한다.
# 근거: docs/hazards/H-06-화장장.md §6-3 · evidence/H-06_화장장_좌표검증_20260901.csv
VERIFIED_COORDINATES: dict[str, tuple[float, float, str]] = {
    "전주시승화원": (
        35.824386,
        127.088977,
        "VWorld 도로명(콩쥐팥쥐로 1705-138)·카카오 키워드 교차확인 24m 일치 (H-06 §6-3)",
    ),
}


def _name_key(name: str) -> str:
    return "".join((name or "").split())


def verified_coordinate(name: str) -> tuple[Coordinates, str] | None:
    """시설명이 검증표에 있으면 (좌표, 근거)를 돌려준다."""

    entry = VERIFIED_COORDINATES.get(_name_key(name))
    if entry is None:
        return None
    lat, lng, basis = entry
    return Coordinates(lat=lat, lng=lng), basis


class CrematoriumAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Crematorium(NamedTuple):
    """화장시설 한 곳(지오코딩 성공분)."""

    facility_id: str
    name: str
    coordinates: Coordinates
    address: str
    region: str        # ctpv 시도
    sigungu: str
    gubun: str         # 공설/사설
    brazier_count: str  # brzCnt 화장로수
    # 좌표 출처. "geocode"(주소 지오코딩) 또는 "verified"(검증표 고정값).
    coordinate_basis: str = "geocode"


class GeocodeFailure(NamedTuple):
    """지오코딩에 실패해 격리한 화장시설. 삭제하지 않고 사유와 함께 남긴다."""

    name: str
    address: str
    reason: str


def _facility_id(row: dict[str, Any]) -> str:
    name = str(row.get("fcltNm") or "").strip()
    addr = str(row.get("addr") or "").strip()
    return f"{name}:{addr}"


class CrematoriumClient:
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
        self._cache: list[Crematorium] = []
        self._failures: list[GeocodeFailure] = []
        self._cached_at = 0.0
        # 전국 목록 캐시 채우기를 직렬화하는 single-flight 잠금. 동시 호출이 여럿
        # 이어도 실제 조회는 1회만 일어난다(캐시 스탬피드 방지).
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        # 지오코더가 없으면 좌표를 붙일 수 없어 판정 원천이 되지 못한다.
        return bool(self.service_key and self._geocode is not None)

    @property
    def geocode_failures(self) -> list[GeocodeFailure]:
        """지오코딩 실패로 격리된 화장시설. 마지막 조회 기준."""

        return list(self._failures)

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    async def all_crematoriums(self) -> list[Crematorium]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache

        async with self._fill_lock.get():
            # 잠금을 잡은 뒤 다시 확인한다. 먼저 들어간 호출이 이미 채웠으면
            # 재조회하지 않는다(double-checked).
            if self._is_warm():
                return self._cache

            # 조회가 실패하면 예외가 그대로 전파돼 캐시를 건드리지 않는다. 실패를
            # 캐시하지 않으므로 다음 호출이 다시 시도할 수 있다.
            rows = await self._fetch_rows()
            located: list[Crematorium] = []
            failures: list[GeocodeFailure] = []
            assert self._geocode is not None
            for row in rows:
                name = str(row.get("fcltNm") or "").strip()
                address = str(row.get("addr") or "").strip()
                basis = "geocode"
                verified = verified_coordinate(name)
                if verified is not None:
                    # 검증표 좌표가 지오코딩보다 우선한다. 주소가 없어도 살린다.
                    coordinates, basis = verified[0], f"verified: {verified[1]}"
                elif not address:
                    failures.append(GeocodeFailure(name, address, "주소 없음"))
                    continue
                else:
                    try:
                        coordinates = await self._geocode(address)
                    except Exception as exc:  # 지오코딩 장애를 화장장 소실로 두지 않는다.
                        failures.append(
                            GeocodeFailure(name, address, f"지오코딩 오류: {exc}")
                        )
                        continue
                    if coordinates is None:
                        failures.append(GeocodeFailure(name, address, "지오코딩 결과 없음"))
                        continue
                located.append(
                    Crematorium(
                        facility_id=_facility_id(row),
                        name=name,
                        coordinates=coordinates,
                        address=address,
                        region=str(row.get("ctpv") or "").strip(),
                        sigungu=str(row.get("sigungu") or "").strip(),
                        gubun=str(row.get("gubun") or "").strip(),
                        brazier_count=str(row.get("brzCnt") or "").strip(),
                        coordinate_basis=basis,
                    )
                )

            self._cache = located
            self._failures = failures
            self._cached_at = time.monotonic()
            return located

    async def crematoriums_around(
        self,
        center: Coordinates,
        radius_m: float,
    ) -> list[Crematorium]:
        """반경 안의 화장시설. 전국 목록을 캐시해 두고 거리로 거른다."""

        facilities = await self.all_crematoriums()
        return [
            facility
            for facility in facilities
            if haversine_meters(center, facility.coordinates) <= radius_m
        ]

    async def _fetch_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=True,
            verify=shared_verify(),
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                page_rows, total = await self._page(client, page)
                rows.extend(page_rows)
                if len(page_rows) < PAGE_SIZE or len(rows) >= total:
                    break
        return rows

    async def _page(
        self,
        client: httpx.AsyncClient,
        page: int,
    ) -> tuple[list[dict[str, Any]], int]:
        # 허용 파라미터는 이 넷뿐이다. type 등을 더 넣으면 400 이 난다.
        params = {
            "serviceKey": self.service_key,
            "pageNo": str(page),
            "numOfRows": str(PAGE_SIZE),
            "apiType": "JSON",
        }
        try:
            response = await client.get(CREMATORIUM_URL, params=params)
        except httpx.HTTPError as exc:
            raise CrematoriumAPIError(f"화장시설 조회에 실패했습니다: {exc}") from exc

        if response.status_code != 200:
            raise CrematoriumAPIError(
                f"화장시설 응답 오류 ({response.status_code})",
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CrematoriumAPIError("화장시설 응답을 해석하지 못했습니다.") from exc

        code = str(payload.get("resultCode") or "00").strip()
        if code not in ("00", "0"):
            raise CrematoriumAPIError(
                f"화장시설 오류: {payload.get('resultMsg') or code}"
            )

        items = payload.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        rows = [row for row in items if isinstance(row, dict)]
        try:
            total = int(payload.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = 0
        return rows, total
