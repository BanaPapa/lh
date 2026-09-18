"""국립중앙의료원 전국 병·의원 찾기 서비스 조회.

2차 심사표 의료시설(종합병원) 산정의 지정 원천이다. 카카오 '종합병원' 분류
근사와 달리 국립중앙의료원 원장이라 종류가 정확하다.

실호출로 확정한 사실(2026-08-28):
- 경로: `http://apis.data.go.kr/B552657/HsptlAsembySearchService/getHsptlMdcncListInfoInqire`
  (기관코드 B552657 = 국립중앙의료원). 응답은 XML 이다.
- 지역 필터는 `Q0`(시도)·`Q1`(시군구)만 있고 반경검색은 없다. 시도 단위로 받아
  캐시하고 좌표로 거른다.
- `dutyDivNam` 이 병원 종류다. 심사표는 '종류=종합병원'만 인정하므로 여기서
  종합병원·상급종합병원만 남긴다. 상급종합병원은 종합병원에 포함해 인정한다
  (2026-09-18 확정) — `is_tertiary` 는 표기 구분용이다.
- 좌표는 `wgs84Lat`·`wgs84Lon`(WGS84)다.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify


NCMC_HOSPITAL_URL = (
    "http://apis.data.go.kr/B552657/HsptlAsembySearchService"
    "/getHsptlMdcncListInfoInqire"
)

# 한 페이지 최대. 200 이면 시도 하나가 15페이지 안에 든다.
PAGE_SIZE = 200
MAX_PAGES = 40

# 병원 목록은 하루에도 몇 곳씩 바뀌지 않는다. 하루 한 번이면 충분하다.
CACHE_TTL_SECONDS = 24 * 60 * 60

# 심사표 '종류=종합병원' 판별. dutyDivNam 이 '종합병원' 이면 종합병원,
# '상급종합병원' 이면 상급종합이다. 둘 다 '종합병원' 을 포함한다.
GENERAL_HOSPITAL_TOKEN = "종합병원"
TERTIARY_TOKEN = "상급"


class NcmcHospitalAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HospitalRecord(NamedTuple):
    """종합병원 한 곳."""

    hpid: str
    name: str
    coordinates: Coordinates
    div_name: str      # dutyDivNam 원문(종합병원 / 상급종합병원)
    address: str = ""
    phone: str = ""
    is_tertiary: bool = False  # 상급종합병원 여부(LH 미확정 → 구분만 해 둔다)


def _text(item: ET.Element, tag: str) -> str:
    value = item.findtext(tag)
    return (value or "").strip()


def _hospital(item: ET.Element) -> HospitalRecord | None:
    div_name = _text(item, "dutyDivNam")
    # 심사표는 종합병원만 인정한다. '종합병원'·'상급종합병원' 만 남긴다.
    if GENERAL_HOSPITAL_TOKEN not in div_name:
        return None
    lat = _text(item, "wgs84Lat")
    lng = _text(item, "wgs84Lon")
    if not lat or not lng:
        return None
    try:
        coordinates = Coordinates(lat=float(lat), lng=float(lng))
    except (TypeError, ValueError):
        return None
    if not (33.0 <= coordinates.lat <= 39.5 and 124.0 <= coordinates.lng <= 132.0):
        return None
    return HospitalRecord(
        hpid=_text(item, "hpid"),
        name=_text(item, "dutyName"),
        coordinates=coordinates,
        div_name=div_name,
        address=_text(item, "dutyAddr"),
        phone=_text(item, "dutyTel1"),
        is_tertiary=TERTIARY_TOKEN in div_name,
    )


class NcmcHospitalClient:
    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        # 시도 이름 → (적재시각, 종합병원 목록). 시도 단위로 캐시한다.
        self._cache: dict[str, tuple[float, list[HospitalRecord]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    async def general_hospitals_in_sido(self, sido: str) -> list[HospitalRecord]:
        """시도 안의 종합병원·상급종합병원 전량. 시도별로 캐시한다."""

        if not self.enabled or not sido:
            return []
        cached = self._cache.get(sido)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

        hospitals: list[HospitalRecord] = []
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=True,
            verify=shared_verify(),
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                items, total = await self._page(client, sido, page)
                for item in items:
                    hospital = _hospital(item)
                    if hospital:
                        hospitals.append(hospital)
                if len(items) < PAGE_SIZE or page * PAGE_SIZE >= total:
                    break

        self._cache[sido] = (time.monotonic(), hospitals)
        return hospitals

    async def hospitals_around(
        self,
        center: Coordinates,
        sido: str,
        radius_m: float,
    ) -> list[HospitalRecord]:
        """반경 안의 종합병원. 시도 목록을 캐시해 두고 거리로 거른다."""

        hospitals = await self.general_hospitals_in_sido(sido)
        return [
            hospital
            for hospital in hospitals
            if haversine_meters(center, hospital.coordinates) <= radius_m
        ]

    async def _page(
        self,
        client: httpx.AsyncClient,
        sido: str,
        page: int,
    ) -> tuple[list[ET.Element], int]:
        params = {
            "ServiceKey": self.service_key,
            "Q0": sido,
            "pageNo": str(page),
            "numOfRows": str(PAGE_SIZE),
        }
        try:
            response = await client.get(NCMC_HOSPITAL_URL, params=params)
        except httpx.HTTPError as exc:
            raise NcmcHospitalAPIError(f"병원 조회에 실패했습니다: {exc}") from exc

        if response.status_code != 200:
            raise NcmcHospitalAPIError(
                f"병원 응답 오류 ({response.status_code})",
                response.status_code,
            )
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError as exc:
            raise NcmcHospitalAPIError("병원 응답을 해석하지 못했습니다.") from exc

        code = (root.findtext(".//resultCode") or "00").strip()
        if code not in ("00", "0"):
            msg = (root.findtext(".//resultMsg") or code).strip()
            raise NcmcHospitalAPIError(f"병원 조회 오류: {msg}")

        items = root.findall(".//item")
        try:
            total = int((root.findtext(".//totalCount") or "0").strip())
        except (TypeError, ValueError):
            total = 0
        return items, total
