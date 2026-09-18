"""생활안전지도 시설 레이어를 판정·편의시설이 쓰는 공통 시설 레코드로 바꾼다.

safemap_layers.SafemapLayerClient 는 레이어의 행을 원본 dict 그대로 준다. 레이어마다
컬럼이 달라(2026-09-17 실측) 여기서 레이어별 파서로 이름·주소·구분·좌표를 뽑아
`SafemapFacility` 하나로 맞춘다. 판정 쪽(1차 유해시설)과 편의시설 쪽(2차 배점)이
같은 피드를 쓴다.

실측 컬럼(승인 뒤 첫 페이지):
- IF_0022 종합병원  : dutyname · dutyaddr · dutydivname · lat/lon · hpid (381건)
- IF_0031 관공서    : fclty_nm · rn_adres/adres · fclty_ty · x/y(EPSG:3857) (9,679건)
- IF_0034 대학교    : IF_0031 과 같은 모양 · fclty_ty="대학교" (1,621건)
- IF_0035 초·중·고  : fcltynm · lnmadr · fcltyty(01 초 · 02 중 · 03 고 · 06 기타) ·
                     latitude/longitude (12,215건)
- IF_0037 유아시설  : IF_0035 와 같은 모양 · fcltyty 05 유치원 등 (40,638건)
- IF_0040 환경배출  : fclty_nm · adres · fclty_se(수질 57,187 · 대기 3,949) · x/y —
                     61,136건 중 14,035건은 x=y=0(좌표 없음) → 뺀다
- IF_0049 화학물취급: entrps_nm · rn_adres · induty_nm · x/y (3,820건)
- IF_0038 소방시설  : IF_0031 과 같은 모양 · fclty_ty(119안전센터 · 소방서 · 소방기관 ·
                     소방기타=지역대·특수대응단) (2,136건)
- IF_0051 폐기물처리: IF_0040 과 같은 모양(fclty_nm · fclty_se · adres · x/y) — 사용신청
                     승인 전(2026-09-17) · 승인되면 그대로 붙는다
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.safemap_layers import LAYER_BY_ID, SafemapLayerClient

# EPSG:3857 (Web Mercator) 반지름. 생활안전지도 x/y 는 이 좌표계다(IF_0033 과 같다).
_MERCATOR_RADIUS_M = 6378137.0
# 한반도 밖 좌표는 원천 오류로 보고 뺀다.
_KOREA_BBOX = (33.0, 39.5, 124.0, 132.0)

# IF_0035 · IF_0037 의 fcltyty 코드(실측). 06 은 분교장·특수학교·공동실습소 등이라
# 이름 토큰으로 다시 가른다.
SCHOOL_TYPE_CODES: dict[str, str] = {
    "01": "초등학교",
    "02": "중학교",
    "03": "고등학교",
    "05": "유치원",
    "06": "기타",
}
SCHOOL_NAME_TOKENS: tuple[str, ...] = ("초등학교", "중학교", "고등학교")


class SafemapFacility(NamedTuple):
    """레이어 한 행. 판정·편의시설이 공통으로 읽는 모양."""

    layer_id: str
    record_id: str
    name: str
    address: str
    # 시설 구분: 초등학교/중학교/고등학교/대학교/관공서/종합병원/대기/수질/업종명.
    kind: str
    coordinates: Coordinates


def mercator_to_wgs84(x: float, y: float) -> Coordinates:
    lng = x / _MERCATOR_RADIUS_M * 180.0 / math.pi
    lat = math.degrees(math.atan(math.sinh(y / _MERCATOR_RADIUS_M)))
    return Coordinates(lat=lat, lng=lng)


def _in_korea(point: Coordinates) -> bool:
    south, north, west, east = _KOREA_BBOX
    return south <= point.lat <= north and west <= point.lng <= east


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", "-"):
            return str(value).strip()
    return ""


def _latlng(row: dict[str, Any], lat_key: str, lng_key: str) -> Coordinates | None:
    try:
        point = Coordinates(lat=float(row[lat_key]), lng=float(row[lng_key]))
    except (KeyError, TypeError, ValueError):
        return None
    return point if _in_korea(point) else None


def _mercator(row: dict[str, Any]) -> Coordinates | None:
    try:
        x = float(str(row.get("x") or "").strip())
        y = float(str(row.get("y") or "").strip())
    except ValueError:
        return None
    if x == 0.0 or y == 0.0:
        return None
    point = mercator_to_wgs84(x, y)
    return point if _in_korea(point) else None


def _school_kind(row: dict[str, Any], name: str) -> str:
    code = str(row.get("fcltyty") or "").strip()
    kind = SCHOOL_TYPE_CODES.get(code, "")
    if kind and kind != "기타":
        return kind
    # 분교장·특수학교·공동실습소는 이름 토큰으로 가른다(예: 가곡초등학교대곡분교장).
    for token in SCHOOL_NAME_TOKENS:
        if token in name:
            return token
    return kind or "기타"


def parse_school(layer_id: str, row: dict[str, Any]) -> SafemapFacility | None:
    name = _text(row, "fcltynm")
    point = _latlng(row, "latitude", "longitude")
    if not name or point is None:
        return None
    return SafemapFacility(
        layer_id=layer_id,
        record_id=_text(row, "fcltycd") or name,
        name=name,
        address=_text(row, "lnmadr"),
        kind=_school_kind(row, name),
        coordinates=point,
    )


def parse_office(layer_id: str, row: dict[str, Any]) -> SafemapFacility | None:
    """IF_0031 관공서 · IF_0034 대학교(같은 컬럼)."""

    name = _text(row, "fclty_nm")
    point = _mercator(row)
    if not name or point is None:
        return None
    return SafemapFacility(
        layer_id=layer_id,
        record_id=_text(row, "objt_id") or name,
        name=name,
        address=_text(row, "rn_adres", "adres"),
        kind=_text(row, "fclty_ty"),
        coordinates=point,
    )


def parse_hospital(layer_id: str, row: dict[str, Any]) -> SafemapFacility | None:
    name = _text(row, "dutyname")
    point = _latlng(row, "lat", "lon")
    if not name or point is None:
        return None
    return SafemapFacility(
        layer_id=layer_id,
        record_id=_text(row, "hpid") or name,
        name=name,
        address=_text(row, "dutyaddr"),
        kind=_text(row, "dutydivname") or "종합병원",
        coordinates=point,
    )


def parse_emission(layer_id: str, row: dict[str, Any]) -> SafemapFacility | None:
    name = _text(row, "fclty_nm")
    point = _mercator(row)
    if not name or point is None:
        return None
    return SafemapFacility(
        layer_id=layer_id,
        record_id=_text(row, "objt_id") or name,
        name=name,
        address=_text(row, "rn_adres", "adres"),
        kind=_text(row, "fclty_se"),
        coordinates=point,
    )


def parse_chemical(layer_id: str, row: dict[str, Any]) -> SafemapFacility | None:
    name = _text(row, "entrps_nm")
    point = _mercator(row)
    if not name or point is None:
        return None
    return SafemapFacility(
        layer_id=layer_id,
        record_id=_text(row, "objt_id") or name,
        name=name,
        address=_text(row, "rn_adres", "adres"),
        kind=_text(row, "induty_nm"),
        coordinates=point,
    )


Parser = Callable[[str, dict[str, Any]], SafemapFacility | None]

# IF_0038 중 공공시설로 세는 유형. 지역대·특수대응단(소방기타)은 뺀다.
FIRE_PUBLIC_KINDS: frozenset[str] = frozenset({"소방서", "119안전센터", "소방기관"})

PARSER_BY_LAYER: dict[str, Parser] = {
    "IF_0022": parse_hospital,
    "IF_0031": parse_office,
    "IF_0034": parse_office,
    "IF_0038": parse_office,
    "IF_0051": parse_emission,
    "IF_0035": parse_school,
    "IF_0037": parse_school,
    "IF_0040": parse_emission,
    "IF_0049": parse_chemical,
}

CACHE_TTL_SECONDS = 24 * 3600


class SafemapFacilityFeed:
    """레이어 하나를 전량 받아(24시간 캐시) 반경으로 거른 시설 목록을 준다.

    조회 실패는 예외로 올린다(SafemapAPIError). 빈 목록으로 접으면 「시설 없음」과
    구분되지 않아 2차 등급·1차 주석이 조용히 틀어진다.
    """

    def __init__(
        self,
        service_key: str,
        layer_id: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if layer_id not in PARSER_BY_LAYER:
            raise ValueError(f"시설 파서가 없는 생활안전지도 레이어: {layer_id}")
        self.layer_id = layer_id
        self.layer = LAYER_BY_ID[layer_id]
        self._client = SafemapLayerClient(
            service_key, layer_id, timeout=timeout, transport=transport
        )
        self._parse = PARSER_BY_LAYER[layer_id]
        self._cache: list[SafemapFacility] = []
        self._cached_at = 0.0

    @property
    def enabled(self) -> bool:
        return self._client.enabled

    @property
    def service_key(self) -> str:
        return self._client.service_key

    @property
    def has_fast_path(self) -> bool:
        """전량 캐시가 데워져 즉시 답할 수 있는가. 아니면 첫 호출이 전량 수신(IF_0049 3,820건
        약 20초)으로 판정을 막으므로, 판정 경로는 False 인 동안 이 레이어를 건너뛴다."""

        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    async def all_facilities(self) -> list[SafemapFacility]:
        if not self.enabled:
            return []
        if self._cache and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS:
            return self._cache
        rows = await self._client.all_rows()
        parsed = [self._parse(self.layer_id, row) for row in rows]
        facilities = [facility for facility in parsed if facility is not None]
        self._cache = facilities
        self._cached_at = time.monotonic()
        return facilities

    async def facilities_around(
        self, center: Coordinates, radius_m: float
    ) -> list[SafemapFacility]:
        facilities = await self.all_facilities()
        return [
            facility
            for facility in facilities
            if haversine_meters(center, facility.coordinates) <= radius_m
        ]
