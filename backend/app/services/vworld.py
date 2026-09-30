"""VWorld 연속지적도 조회.

PNU로 필지 폴리곤을 받아온다. 상업 지도 POI에는 없는 '땅의 모양'을 얻는 유일한
경로이며, 대지경계 기준 거리계산의 전제 조건이다.
"""

from __future__ import annotations

import math
from typing import Any, Literal, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters, polygon_area_m2
from app.services.http_client import shared_verify


VWORLD_DATA_URL = "https://api.vworld.kr/req/data"
# 주소 검색 API(느슨한 매칭). 카카오가 못 찾는 옛 지번의 보조 지오코딩에만 쓴다.
VWORLD_SEARCH_URL = "https://api.vworld.kr/req/search"

# 연속지적도(부번 포함). 지적도 도형은 이 레이어에서 나온다.
CADASTRAL_LAYER = "LP_PA_CBND_BUBUN"
# 국토교통부 GIS 건물통합정보. 건물 외곽선 + usability(건축물대장 주용도코드 5자리, 예 19000 =
# 위험물저장및처리시설) + 건물명·동명·층수. 실측(2026-09-18, 도곡동 200m 박스): 75동 중
# usability 공란 25동(대장 미연계) — 공란은 필지 표제부로 보완한다.
BUILDING_LAYER = "LT_C_BLDGINFO"

# 국토교통부 용도지역(지적편집도) 레이어. 점 조회로 용도지역명을 얻는다.
# 실측 응답(2026-09-13, VWORLD_API_KEY):
#   정읍 수성동 1011-8 POINT(126.859595 35.585262) → uname "제2종일반주거지역"
#   익산 영등동 191-35 POINT(126.974753 35.949162) → uname "일반공업지역"
# 한 점에 여러 용도지역 폴리곤이 겹쳐 오며(uname 이 공란인 중복 feature 포함),
# 첫 번째 비공란 uname 을 용도지역명으로 쓴다. 응답 속성 키는 `uname`.
ZONING_LAYER = "LT_C_UQ111"
# 용도지역명 속성 키(실측 확인). 바뀌면 여기만 고친다.
ZONING_NAME_KEY = "uname"
# 점 조회가 미확인이면 이 반경(m)으로 BOX 재조회를 1회 한다. 경계선 위 좌표가
# 인접 필지 사이 공백에 떨어지는 경우를 메운다(조준환 엔진과 같은 20m).
ZONING_RETRY_RADIUS_M = 20.0

# 조회 결과가 없을 때 VWorld가 돌려주는 정상 상태값.
EMPTY_STATUSES = {"NOT_FOUND", "NOT_FOUND_DATA"}

# VWorld data API 가 한 번에 돌려주는 최대 건수.
MAX_PAGE_SIZE = 1000

# 장소검색(type=place) 한 번에 훑는 최대 페이지 수. 3km 반경 사각형에서 분류 이름으로
# 물으면 수십~수백 건이다(실측 2026-09-30 전북 전역: 「공공도서관」 120건, 「철도역」 20건,
# 「시외버스터미널」 164건). 상한에 닿으면 잘린 것이므로 예외로 올린다.
PLACE_SEARCH_MAX_PAGES = 5

# 주거지역 판별 키워드. 전용·일반·준주거 전 종이 용도지역명에 「주거」를 포함한다
# (제1·2종전용주거지역, 제1·2·3종일반주거지역, 준주거지역).
RESIDENTIAL_ZONING_KEYWORD = "주거"

ZoningClass = Literal["residential", "non_residential", "unknown"]


class ZoningInfo(NamedTuple):
    """용도지역 조회 결과. name 은 VWorld uname(예: "제2종일반주거지역")."""

    name: str


def classify_zoning(name: str | None) -> ZoningClass:
    """용도지역명을 주거/비주거/미확인으로 분류한다(순수 함수).

    주거지역(전용·일반·준주거)은 「주거」를 포함하므로 키워드로 가른다. 공업·상업·
    녹지·관리지역 등은 non_residential, 공란·None 은 unknown(확인 요청 대상).
    """

    text = (name or "").strip()
    if not text:
        return "unknown"
    if RESIDENTIAL_ZONING_KEYWORD in text:
        return "residential"
    return "non_residential"


class VWorldAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BuildingFeature(NamedTuple):
    """건물통합정보 건물 하나. use_code 는 건축물대장 주용도코드(5자리) 또는 빈 문자열."""

    name: str
    dong_name: str
    use_code: str
    ring: list[Coordinates]
    ground_floors: int


class VWorldPlace(NamedTuple):
    """VWorld 장소검색(type=place) 결과 하나. category 는 「철도시설 > 철도/지하철 > 고속철도역」 꼴."""

    id: str
    title: str
    category: str
    road_address: str
    parcel_address: str
    coordinates: Coordinates

    @property
    def address(self) -> str:
        return self.road_address or self.parcel_address

    @property
    def category_leaf(self) -> str:
        return self.category.split(">")[-1].strip()


class ParcelFeature(NamedTuple):
    """지적도 필지 하나."""

    pnu: str
    address: str
    jibun: str
    ring: list[Coordinates]
    area_m2: float


def _outer_ring(geometry: dict[str, Any]) -> list[Coordinates]:
    """GeoJSON Polygon / MultiPolygon 에서 가장 넓은 외곽 링을 꺼낸다."""

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon":
        rings = [coordinates[0]] if coordinates else []
    elif geometry_type == "MultiPolygon":
        rings = [polygon[0] for polygon in coordinates if polygon]
    else:
        raise VWorldAPIError(f"지원하지 않는 도형 형식입니다: {geometry_type}")

    if not rings:
        raise VWorldAPIError("필지 도형이 비어 있습니다.")

    # 여러 조각이면 좌표가 가장 많은 링을 대표로 쓴다.
    largest = max(rings, key=len)
    return [Coordinates(lat=float(point[1]), lng=float(point[0])) for point in largest]


def _vworld_place(item: Any) -> VWorldPlace | None:
    if not isinstance(item, dict):
        return None
    title = str(item.get("title") or "").strip()
    point = item.get("point") or {}
    try:
        coordinates = Coordinates(lat=float(point["y"]), lng=float(point["x"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not title:
        return None
    address = item.get("address") or {}
    return VWorldPlace(
        id=str(item.get("id") or f"{title}@{coordinates.lat},{coordinates.lng}"),
        title=title,
        category=str(item.get("category") or "").strip(),
        road_address=str(address.get("road") or "").strip(),
        parcel_address=str(address.get("parcel") or "").strip(),
        coordinates=coordinates,
    )


class VWorldClient:
    def __init__(
        self,
        api_key: str,
        domain: str = "localhost",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.domain = domain
        self.timeout = timeout
        self._transport = transport
        # 용도지역 조회 캐시. 좌표 6자리 반올림 키로 프로세스 메모리에 둔다.
        # 118건 배치에서 같은 시설 좌표를 반복 조회해 VWorld 쿼터를 쓰지 않게 한다.
        # 미확인(None)도 캐시해 같은 배치에서 재조회하지 않는다.
        self._zoning_cache: dict[tuple[float, float], ZoningInfo | None] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def zoning_at(self, lat: float, lng: float) -> ZoningInfo | None:
        """좌표의 용도지역(지적편집도)을 조회한다. 미확인·실패·키없음은 None.

        점 조회 후 미확인이면 20m BOX 로 1회 재조회한다. 예외로 죽지 않게 내부에서
        VWorldAPIError·HTTP 오류를 삼키고 None 을 돌려준다(기존 호출부 관례와 동일).
        """

        if not self.enabled:
            return None
        key = (round(lat, 6), round(lng, 6))
        if key in self._zoning_cache:
            return self._zoning_cache[key]
        info = await self._lookup_zoning(lat, lng)
        self._zoning_cache[key] = info
        return info

    async def _lookup_zoning(self, lat: float, lng: float) -> ZoningInfo | None:
        name = await self._zoning_name(f"POINT({lng} {lat})")
        if not name:
            # 경계선 위 좌표 보정용 20m BOX 재조회 1회.
            dlat = ZONING_RETRY_RADIUS_M / 111_320.0
            dlng = ZONING_RETRY_RADIUS_M / (
                111_320.0 * max(math.cos(math.radians(lat)), 1e-6)
            )
            box = f"BOX({lng - dlng},{lat - dlat},{lng + dlng},{lat + dlat})"
            name = await self._zoning_name(box)
        return ZoningInfo(name=name) if name else None

    async def _zoning_name(self, geom_filter: str) -> str:
        """용도지역 레이어에서 첫 비공란 용도지역명을 꺼낸다. 실패·미확인은 빈 문자열."""

        if not self.enabled:
            return ""
        params = {
            "service": "data",
            "request": "GetFeature",
            "data": ZONING_LAYER,
            "key": self.api_key,
            # 도형은 분류에 쓰지 않으므로 받지 않는다(응답·쿼터 절약).
            "geometry": "false",
            "format": "json",
            "size": "10",
            "crs": "EPSG:4326",
            "domain": self.domain,
            "geomFilter": geom_filter,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                verify=shared_verify(),
            ) as client:
                response = await client.get(VWORLD_DATA_URL, params=params)
            if response.status_code != 200:
                return ""
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return ""
        body = payload.get("response") or {}
        if body.get("status") != "OK":
            return ""
        features = (
            (body.get("result") or {}).get("featureCollection") or {}
        ).get("features") or []
        for feature in features:
            name = str((feature.get("properties") or {}).get(ZONING_NAME_KEY) or "").strip()
            if name:
                return name
        return ""

    async def search_address_point(self, query: str, strict: bool = False) -> Coordinates | None:
        """VWorld 검색 API(주소)로 좌표 한 점을 찾는다. 모호하면 None.

        카카오 주소검색이 못 찾는 옛 지번(분할·합병으로 사라진 번지)의 보조다. 실측
        (2026-09-30): 산단공 등록공장 「전주시완산구 효자동2가 368번지」(현대콘크리트)는
        카카오·VWorld 지번 검색 모두 없음이고, 검색 API road 분류가 「쑥고개로 368
        (효자동2가) = 효자동2가 366-13」 한 건을 준다 — LH 표준 데이터셋이 이 공장을 둔
        필지(PNU …1036600 13)와 같다. 검색은 느슨한 매칭이라 **결과가 딱 한 건이고 그
        법정동 이름이 질의에 들어 있을 때만** 받는다(엉뚱한 동 같은 번호를 막는다).
        지번(parcel) 분류를 먼저, 없으면 도로명(road) 분류를 본다. 실패는 None.
        """

        if not self.enabled or not query.strip():
            return None
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            for category in ("parcel", "road"):
                params = {
                    "service": "search",
                    "request": "search",
                    "version": "2.0",
                    "query": query,
                    "type": "address",
                    "category": category,
                    "format": "json",
                    "size": "2",
                    "key": self.api_key,
                }
                try:
                    response = await client.get(VWORLD_SEARCH_URL, params=params)
                    body = response.json().get("response") or {}
                except (httpx.HTTPError, ValueError):
                    # strict: 통신 오류를 「못 찾음」과 구분해 올린다(캐시에 없음으로 굳지 않게).
                    if strict:
                        raise
                    return None
                if body.get("status") != "OK":
                    continue
                items = (body.get("result") or {}).get("items") or []
                if len(items) != 1:
                    continue
                item = items[0]
                parcel = str((item.get("address") or {}).get("parcel") or "").strip()
                dong = parcel.split(" ")[0] if parcel else ""
                if not dong or dong not in query.replace(" ", ""):
                    continue
                point = item.get("point") or {}
                try:
                    return Coordinates(lat=float(point["y"]), lng=float(point["x"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return None

    async def search_places(
        self,
        query: str,
        south: float,
        west: float,
        north: float,
        east: float,
        max_pages: int = PLACE_SEARCH_MAX_PAGES,
    ) -> list[VWorldPlace]:
        """VWorld 장소검색(국토정보플랫폼 POI)으로 사각 영역 안의 장소를 모두 받는다.

        지도 회사(카카오·네이버)와 무관한 반경 열거 원천이다. 질의어는 이름뿐 아니라 분류
        문자열에도 걸린다 — 「공공도서관」으로 물으면 분류가 공공도서관인 곳(이름에
        「공공도서관」이 없는 「전주시립인후도서관」 포함)이 온다. 실측(2026-09-30, 전북
        전역): 분류 「공공도서관」 67곳이 LH 데이터셋 공공도서관 66곳과 이름까지 같고
        (진안도서관 1곳 더), 「철도역」 질의의 일반·고속철도역 분류가 LH 철도역·KTX역과
        같다. 결과 없음은 빈 목록, 장애·키 오류는 VWorldAPIError(0곳과 구분한다).
        """

        if not self.enabled:
            raise VWorldAPIError("VWorld API 키가 설정되지 않았습니다.")
        places: list[VWorldPlace] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            for page in range(1, max_pages + 1):
                params = {
                    "service": "search",
                    "request": "search",
                    "version": "2.0",
                    "type": "place",
                    "query": query,
                    "bbox": f"{west},{south},{east},{north}",
                    "crs": "EPSG:4326",
                    "size": str(MAX_PAGE_SIZE),
                    "page": str(page),
                    "format": "json",
                    "key": self.api_key,
                    "domain": self.domain,
                }
                try:
                    response = await client.get(VWORLD_SEARCH_URL, params=params)
                except httpx.HTTPError as exc:
                    raise VWorldAPIError(f"VWorld 장소검색 요청에 실패했습니다: {exc}") from exc
                if response.status_code != 200:
                    raise VWorldAPIError(
                        f"VWorld 장소검색 응답 오류 ({response.status_code})",
                        response.status_code,
                    )
                try:
                    body = response.json().get("response") or {}
                except ValueError as exc:
                    raise VWorldAPIError("VWorld 장소검색 응답을 해석하지 못했습니다.") from exc
                status = body.get("status")
                if status in EMPTY_STATUSES:
                    break
                if status != "OK":
                    error = body.get("error") or {}
                    detail = error.get("text") or status or "알 수 없는 오류"
                    raise VWorldAPIError(f"VWorld 장소검색 오류: {detail}")
                items = (body.get("result") or {}).get("items") or []
                for item in items:
                    place = _vworld_place(item)
                    if place is None or place.id in seen:
                        continue
                    seen.add(place.id)
                    places.append(place)
                try:
                    total = int((body.get("record") or {}).get("total") or 0)
                except (TypeError, ValueError):
                    total = 0
                if len(items) < MAX_PAGE_SIZE or page * MAX_PAGE_SIZE >= total:
                    return places
            else:
                raise VWorldAPIError(
                    f"VWorld 장소검색 결과가 {max_pages * MAX_PAGE_SIZE}건을 넘어 잘렸습니다: {query}"
                )
        return places

    async def places_around(
        self,
        query: str,
        center: Coordinates,
        radius_m: float,
        max_pages: int = PLACE_SEARCH_MAX_PAGES,
    ) -> list[VWorldPlace]:
        """반경 안의 장소. 반경을 품는 사각형으로 묻고 직선거리로 다시 거른다."""

        dlat = radius_m / 111_320.0
        dlng = radius_m / (111_320.0 * max(math.cos(math.radians(center.lat)), 1e-6))
        places = await self.search_places(
            query,
            center.lat - dlat,
            center.lng - dlng,
            center.lat + dlat,
            center.lng + dlng,
            max_pages=max_pages,
        )
        return [p for p in places if haversine_meters(center, p.coordinates) <= radius_m]

    async def parcel_at(self, lat: float, lng: float) -> ParcelFeature | None:
        """좌표를 품는 필지를 조회한다.

        주소에서 조립한 PNU는 자투리 필지(역 출입구)나 존재하지 않는 지번(지하
        주소)으로 이어질 수 있다. 좌표로 직접 찾으면 실제 그 자리의 필지가 나온다.
        """

        return await self._fetch(
            {"geomFilter": f"POINT({lng} {lat})", "crs": "EPSG:4326"}
        )

    async def parcel_by_pnu(self, pnu: str) -> ParcelFeature | None:
        """PNU로 필지를 조회한다. 키가 없거나 필지를 못 찾으면 None."""

        return await self._fetch({"attrFilter": f"pnu:=:{pnu}"}, fallback_pnu=pnu)

    async def parcels_in_box(
        self,
        south: float,
        west: float,
        north: float,
        east: float,
        limit: int = MAX_PAGE_SIZE,
    ) -> list[ParcelFeature]:
        """사각 영역에 걸치는 필지들을 조회한다.

        지도에 지적도 경계를 깔아 사용자가 어느 필지를 누르는지 보이게 하는 용도다.
        """

        return await self._fetch_many(
            {
                "geomFilter": f"BOX({west},{south},{east},{north})",
                "crs": "EPSG:4326",
            },
            limit=limit,
        )

    async def buildings_in_box(
        self,
        south: float,
        west: float,
        north: float,
        east: float,
        limit: int = MAX_PAGE_SIZE,
    ) -> list[BuildingFeature]:
        """사각 영역의 건물(외곽선 + 주용도코드)을 조회한다 — 건축물대장 용도 스캔의 1차 원천."""

        features = await self._request_features(
            BUILDING_LAYER,
            {"geomFilter": f"BOX({west},{south},{east},{north})", "crs": "EPSG:4326"},
            limit=limit,
        )
        buildings: list[BuildingFeature] = []
        for feature in features:
            properties = feature.get("properties") or {}
            try:
                ring = _outer_ring(feature.get("geometry") or {})
            except VWorldAPIError:
                continue
            try:
                floors = int(float(properties.get("grnd_flr") or 0))
            except (TypeError, ValueError):
                floors = 0
            buildings.append(
                BuildingFeature(
                    name=str(properties.get("bld_nm") or ""),
                    dong_name=str(properties.get("dong_nm") or ""),
                    use_code=str(properties.get("usability") or "").strip(),
                    ring=ring,
                    ground_floors=floors,
                )
            )
        return buildings

    async def _fetch(
        self,
        filters: dict[str, str],
        fallback_pnu: str = "",
    ) -> ParcelFeature | None:
        features = await self._fetch_many(filters, limit=1, fallback_pnu=fallback_pnu)
        return features[0] if features else None

    async def _fetch_many(
        self,
        filters: dict[str, str],
        limit: int,
        fallback_pnu: str = "",
    ) -> list[ParcelFeature]:
        features = await self._request_features(CADASTRAL_LAYER, filters, limit)
        parcels: list[ParcelFeature] = []
        for feature in features:
            properties = feature.get("properties") or {}
            try:
                ring = _outer_ring(feature.get("geometry") or {})
            except VWorldAPIError:
                # 영역 조회는 필지가 많아 한 건이 깨져도 나머지는 살린다.
                continue
            parcels.append(
                ParcelFeature(
                    pnu=str(properties.get("pnu") or fallback_pnu),
                    address=str(properties.get("addr") or ""),
                    jibun=str(properties.get("jibun") or ""),
                    ring=ring,
                    area_m2=polygon_area_m2(ring),
                )
            )
        return parcels

    async def _request_features(
        self,
        layer: str,
        filters: dict[str, str],
        limit: int,
    ) -> list[dict[str, Any]]:
        """데이터 API 한 레이어를 조회해 GeoJSON feature 목록(limit 건까지)을 돌려준다."""

        if not self.enabled:
            return []

        params = {
            "service": "data",
            "request": "GetFeature",
            "data": layer,
            "key": self.api_key,
            "geometry": "true",
            "format": "json",
            "size": str(min(max(limit, 1), MAX_PAGE_SIZE)),
            "domain": self.domain,
            **filters,
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            verify=shared_verify(),
        ) as client:
            try:
                response = await client.get(VWORLD_DATA_URL, params=params)
            except httpx.HTTPError as exc:
                raise VWorldAPIError(f"VWorld 요청에 실패했습니다: {exc}") from exc

        if response.status_code != 200:
            raise VWorldAPIError(
                f"VWorld 응답 오류 ({response.status_code})",
                response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise VWorldAPIError("VWorld 응답을 해석하지 못했습니다.") from exc

        body = payload.get("response") or {}
        status = body.get("status")
        if status in EMPTY_STATUSES:
            return []
        if status != "OK":
            error = body.get("error") or {}
            detail = error.get("text") or status or "알 수 없는 오류"
            raise VWorldAPIError(f"VWorld 오류: {detail}")

        features = (
            (body.get("result") or {}).get("featureCollection") or {}
        ).get("features") or []
        return [f for f in features[:limit] if isinstance(f, dict)]
