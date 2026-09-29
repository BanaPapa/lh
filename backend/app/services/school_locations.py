"""전국초중등학교위치표준데이터 — 초·중·고 현재 위치(한국교육시설안전원).

2차 교육여건(초·중·고)의 1순위 원천이다. LH 표준 데이터셋(JB_51_SCHOOLS)과 같은
성격의 최신 원장이다. 생활안전지도 학교 레이어(IF_0035)는 이전한 학교를 옛 부지로
갖고 있었다(2026-09-29 전주 서신동: 「전라중학교」가 덕진동1가 옛 부지 306m 로 잡혀
교육여건 10점 — 실제는 송천동2가 1297 로 이전, LH앱 8점).

실호출로 확정한 사실(2026-09-29):
- 경로: `https://api.data.go.kr/openapi/tn_pubr_public_elesch_mskul_lc_api`, `type=json`.
- 전국 12,011곳. 한 페이지 1,000건이 0.2초라 전량을 받아 하루 캐시하고 좌표로 거른다.
  반경·지역 필터 파라미터는 없다(`lnmadr` 필터는 동작하지 않음).
- `schoolSe`(초등학교·중학교·고등학교), `operSttus`(운영), `bnhhSe`(본교·분교),
  `latitude`·`longitude`(WGS84), `lnmadr`(지번주소).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify


SCHOOL_LOCATION_URL = "https://api.data.go.kr/openapi/tn_pubr_public_elesch_mskul_lc_api"

PAGE_SIZE = 1000
MAX_PAGES = 30
CACHE_TTL_SECONDS = 24 * 60 * 60

SCHOOL_KINDS: frozenset[str] = frozenset({"초등학교", "중학교", "고등학교"})


class SchoolLocationAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SchoolRecord(NamedTuple):
    school_id: str
    name: str
    kind: str          # 초등학교·중학교·고등학교
    address: str
    coordinates: Coordinates
    branch: str = ""   # 본교·분교


def _school(row: dict[str, Any]) -> SchoolRecord | None:
    kind = str(row.get("schoolSe") or "").strip()
    if kind not in SCHOOL_KINDS:
        return None
    # 폐교·휴교는 세지 않는다. 표준데이터는 운영 중인 학교만 싣지만 방어한다.
    status = str(row.get("operSttus") or "").strip()
    if status and status != "운영":
        return None
    try:
        coordinates = Coordinates(lat=float(row["latitude"]), lng=float(row["longitude"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not (33.0 <= coordinates.lat <= 39.5 and 124.0 <= coordinates.lng <= 132.0):
        return None
    name = str(row.get("schoolNm") or "").strip()
    if not name:
        return None
    return SchoolRecord(
        school_id=str(row.get("schoolId") or ""),
        name=name,
        kind=kind,
        address=str(row.get("lnmadr") or row.get("rdnmadr") or "").strip(),
        coordinates=coordinates,
        branch=str(row.get("bnhhSe") or "").strip(),
    )


class SchoolLocationClient:
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
        self._schools: list[SchoolRecord] = []
        # 동시에 여러 심사가 처음 불러도 전량 적재는 한 번만 한다.
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    async def schools_around(
        self, center: Coordinates, radius_m: float
    ) -> list[SchoolRecord]:
        """반경 안의 운영 중인 초·중·고. 실패는 예외로 올린다(0곳과 구분)."""

        schools = await self._all_schools()
        return [
            school
            for school in schools
            if haversine_meters(center, school.coordinates) <= radius_m
        ]

    async def _all_schools(self) -> list[SchoolRecord]:
        if self._schools and time.monotonic() - self._loaded_at < CACHE_TTL_SECONDS:
            return self._schools
        async with self._lock:
            if self._schools and time.monotonic() - self._loaded_at < CACHE_TTL_SECONDS:
                return self._schools
            schools: list[SchoolRecord] = []
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                follow_redirects=True,
                verify=shared_verify(),
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    rows, total = await self._page(client, page)
                    schools.extend(
                        record for record in (_school(row) for row in rows) if record
                    )
                    if len(rows) < PAGE_SIZE or page * PAGE_SIZE >= total:
                        break
            if not schools:
                raise SchoolLocationAPIError("학교 위치 표준데이터가 비어 있습니다.")
            self._schools = schools
            self._loaded_at = time.monotonic()
            return schools

    async def _page(
        self, client: httpx.AsyncClient, page: int
    ) -> tuple[list[dict[str, Any]], int]:
        response = await client.get(
            SCHOOL_LOCATION_URL,
            params={
                "serviceKey": self.service_key,
                "type": "json",
                "numOfRows": str(PAGE_SIZE),
                "pageNo": str(page),
            },
        )
        if response.status_code != 200:
            raise SchoolLocationAPIError(
                f"학교 위치 표준데이터 HTTP {response.status_code}", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SchoolLocationAPIError("학교 위치 표준데이터 응답을 읽지 못했습니다.") from exc
        header = (payload.get("response") or payload).get("header") or {}
        code = str(header.get("resultCode", "00"))
        if code not in ("00", "0"):
            raise SchoolLocationAPIError(
                f"학교 위치 표준데이터 오류 {code}: {header.get('resultMsg', '')}"
            )
        body = (payload.get("response") or payload).get("body") or {}
        items = body.get("items")
        if isinstance(items, dict):
            items = items.get("item")
        rows = items if isinstance(items, list) else ([items] if isinstance(items, dict) else [])
        return [row for row in rows if isinstance(row, dict)], int(body.get("totalCount") or 0)
