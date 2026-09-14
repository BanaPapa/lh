"""오피넷(한국석유공사) 주유소·충전소 조회.

유해시설 검토에서 '주유·가스 충전시설' 규칙의 공식 원천이다. 상업 지도 키워드
검색과 달리 사업자 등록 기반이라 누락이 적고 좌표 정확도가 높다.

좌표계 주의: 오피넷은 KATEC(TM128, Bessel 타원체)을 쓴다. 위경도로 바로 넣으면
엉뚱한 곳이 나오므로 요청 전과 응답 후에 반드시 변환해야 한다.
"""

from __future__ import annotations

import math
import time
from functools import lru_cache
from typing import Any, NamedTuple

import httpx
from pyproj import Transformer

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.single_flight import LoopSafeLock


OPINET_AROUND_URL = "http://www.opinet.co.kr/api/aroundAll.do"
OPINET_DETAIL_URL = "http://www.opinet.co.kr/api/detailById.do"

# 오피넷 반경검색 상한.
MAX_RADIUS_M = 5000

# 데운 스냅샷을 재사용하는 유효시간. 다른 전국 원천(kgs·safemap 등)과 같은 24h.
CACHE_TTL_SECONDS = 24 * 60 * 60

# 예열 커버 반경에서 이만큼을 뺀 거리 안에 든 사업지는 스냅샷으로 덮인 것으로 본다.
# 판정이 요청하는 반경(임계거리+여유구간, 보통 수백 m)보다 넉넉히 커야 판정 중
# 원격 재조회가 일어나지 않는다. 여유구간 5000m 같은 극단값은 이 마진을 넘어
# 커버가 성립하지 않으므로 사업지별 라이브 조회로 떨어진다(안건 ⑤는 오프라인
# 테스트로 대체됐다).
COVER_MARGIN_M = 1500

# 휘발유 취급소로 일반 주유소를 훑고, LPG로 충전소를 보충한다. 두 결과를 합치면
# 규칙이 말하는 '주유·가스 충전시설'을 사실상 모두 덮는다.
PRODUCT_CODES = ("B027", "K015")

KATEC_PROJ = (
    "+proj=tmerc +lat_0=38 +lon_0=128 +k=0.9999 +x_0=400000 +y_0=600000 "
    "+ellps=bessel +units=m +no_defs "
    "+towgs84=-115.80,474.99,674.11,1.16,-2.31,-1.63,6.43"
)

BRAND_LABELS = {
    "SKE": "SK에너지",
    "GSC": "GS칼텍스",
    "HDO": "HD현대오일뱅크",
    "SOL": "S-OIL",
    "RTO": "자영알뜰",
    "RTX": "고속도로알뜰",
    "NHO": "농협알뜰",
    "ETC": "자가상표",
    "E1G": "E1",
    "SKG": "SK가스",
}


class OpinetAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpinetStation(NamedTuple):
    """오피넷 주유소·충전소 한 곳."""

    station_id: str
    name: str
    brand: str
    coordinates: Coordinates
    address: str = ""
    road_address: str = ""
    lpg: bool = False


@lru_cache(maxsize=1)
def _transformers() -> tuple[Transformer, Transformer]:
    """WGS84 ↔ KATEC 변환기. 생성 비용이 커서 캐시한다."""

    return (
        Transformer.from_crs("EPSG:4326", KATEC_PROJ, always_xy=True),
        Transformer.from_crs(KATEC_PROJ, "EPSG:4326", always_xy=True),
    )


def to_katec(coordinates: Coordinates) -> tuple[float, float]:
    forward, _ = _transformers()
    return forward.transform(coordinates.lng, coordinates.lat)


def from_katec(x: float, y: float) -> Coordinates:
    _, inverse = _transformers()
    lng, lat = inverse.transform(x, y)
    return Coordinates(lat=lat, lng=lng)


def brand_label(code: str) -> str:
    return BRAND_LABELS.get(code.strip(), code.strip())


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = payload.get("RESULT") or {}
    oil = result.get("OIL")
    if isinstance(oil, dict):
        return [oil]
    return [row for row in (oil or []) if isinstance(row, dict)]


class OpinetClient:
    """오피넷 주유소·충전소 조회.

    오피넷은 전국 목록 엔드포인트가 없고 반경검색(aroundAll.do, 최대 5,000m)만
    제공한다. 그대로 두면 판정 118건이 사업지마다 원격 반경 조회를 새로 해서
    호출이 케이스 수에 비례해 늘고, 수십 건을 연달아 때리면 오피넷이 스로틀해
    「검토 필요」로 오염된 스냅샷이 나온다(2026-08-30 양성 대조 사고).

    그래서 다른 전국 원천(kgs·safemap)처럼 스냅샷을 한 번 데워 캐시하고 거리로
    거르는 방식을 붙였다. 전국 목록이 없으므로 사업지들을 덮는 커버 지점 집합을
    `warm_area()` 로 한 번 조회해(지리적으로 뭉쳐 케이스 수보다 훨씬 적다) 스냅샷을
    채운다. 데워진 뒤에는 `stations_around()` 가 원격을 타지 않고 스냅샷을 거리로
    거른다. 커버되지 않은 지점(단건 심사·극단 반경)은 그 지점만 라이브로 조회해
    스냅샷에 합친다. 실패는 캐시하지 않는다(single-flight).
    """

    def __init__(
        self,
        api_key: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._transport = transport
        # 데운 주유소·충전소 스냅샷. station_id 로 중복 제거한다.
        self._snapshot: dict[str, OpinetStation] = {}
        # 이미 조회해 스냅샷에 반영한 커버 지점. (KATEC x, y, 조회반경 m).
        self._covered: list[tuple[float, float, float]] = []
        self._cached_at = 0.0
        self._warmed = False
        # 스냅샷 채우기를 직렬화하는 single-flight 잠금(캐시 스탬피드 방지).
        self._fill_lock = LoopSafeLock()
        # 상세 조회 메모이즈. 같은 station_id 를 판정마다 다시 때리지 않는다.
        self._detail_cache: dict[str, OpinetStation | None] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _is_fresh(self) -> bool:
        return time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    def _covers(self, x: float, y: float, radius: float) -> bool:
        """(x,y) 반경 radius 안의 시설이 이미 데운 스냅샷으로 전부 덮이는가.

        커버 지점 (cx,cy,cr) 로부터 (x,y) 까지 거리에 radius 를 더해도 cr 이내면,
        (x,y) 반경 radius 안의 모든 시설이 그 커버 조회 반경 안에 들어 스냅샷에 있다.
        """

        if not (self._warmed and self._is_fresh()):
            return False
        return any(
            math.hypot(x - cx, y - cy) <= cr - radius
            for cx, cy, cr in self._covered
        )

    def _merge(self, found: dict[str, OpinetStation], station: OpinetStation) -> None:
        # 휘발유·LPG 양쪽에 잡히면 LPG 취급으로 합쳐 기록한다.
        existing = found.get(station.station_id)
        if existing:
            if station.lpg and not existing.lpg:
                found[station.station_id] = existing._replace(lpg=True)
            return
        found[station.station_id] = station

    def _filter(self, center: Coordinates, radius_m: float) -> list[OpinetStation]:
        return [
            station
            for station in self._snapshot.values()
            if haversine_meters(center, station.coordinates) <= radius_m
        ]

    async def _fetch_around(
        self,
        client: httpx.AsyncClient,
        center: Coordinates,
        radius: int,
    ) -> list[OpinetStation]:
        """한 지점의 반경 조회. 휘발유·LPG 두 상품을 합쳐 돌려준다."""

        x, y = to_katec(center)
        found: dict[str, OpinetStation] = {}
        for product in PRODUCT_CODES:
            payload = await self._get(
                client,
                OPINET_AROUND_URL,
                {
                    # 명세상 인증 파라미터는 `certkey` 다. 과거 `code` 로 보내
                    # 응답이 조용히 0건이 되던 버그를 고쳤다(2026-08-28 실호출 확인).
                    "certkey": self.api_key,
                    "x": f"{x:.0f}",
                    "y": f"{y:.0f}",
                    "radius": str(radius),
                    "sort": "1",
                    "prodcd": product,
                    "out": "json",
                },
            )
            for row in _rows(payload):
                station = self._station(row, lpg=product == "K015")
                if station:
                    self._merge(found, station)
        return list(found.values())

    async def warm_area(
        self,
        centers: list[Coordinates],
        radius_m: float = MAX_RADIUS_M,
    ) -> int:
        """사업지들을 덮는 커버 지점을 한 번 조회해 스냅샷을 데운다(예열 대상).

        커버 지점은 그리디로 뽑아 서로 (radius - COVER_MARGIN_M) 이상 떨어지게 한다.
        전북처럼 사업지가 뭉쳐 있으면 커버 지점 수가 케이스 수보다 훨씬 적다. 조회는
        직렬로 하여 스로틀을 피한다. 하나라도 실패하면 예외를 그대로 올리고 스냅샷을
        건드리지 않는다(실패를 캐시하지 않는다 → 예열 게이트가 판정을 중단시킨다).
        반환값은 데운 스냅샷의 시설 수다.
        """

        if not self.enabled:
            return 0
        radius = int(min(max(radius_m, 1), MAX_RADIUS_M))

        katec = [to_katec(c) for c in centers]
        cover_pts: list[tuple[float, float]] = []
        for x, y in katec:
            if not any(
                math.hypot(x - cx, y - cy) <= radius - COVER_MARGIN_M
                for cx, cy in cover_pts
            ):
                cover_pts.append((x, y))

        async with self._fill_lock.get():
            # 잠금을 잡은 뒤 다시 확인한다. 먼저 들어간 예열이 끝냈으면 재조회하지 않는다.
            if self._covers_all(katec, radius):
                return len(self._snapshot)
            # 성공할 때만 커밋한다. 실패 시 기존 스냅샷을 그대로 둔다.
            snapshot: dict[str, OpinetStation] = dict(self._snapshot)
            covered: list[tuple[float, float, float]] = list(self._covered)
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                verify=shared_verify(),
            ) as client:
                for x, y in cover_pts:
                    stations = await self._fetch_around(
                        client, from_katec(x, y), radius
                    )
                    for station in stations:
                        self._merge(snapshot, station)
                    covered.append((x, y, float(radius)))
            self._snapshot = snapshot
            self._covered = covered
            self._cached_at = time.monotonic()
            self._warmed = True
        return len(self._snapshot)

    def _covers_all(
        self, katec: list[tuple[float, float]], radius: int
    ) -> bool:
        return all(self._covers(x, y, radius) for x, y in katec)

    async def stations_around(
        self,
        center: Coordinates,
        radius_m: float,
    ) -> list[OpinetStation]:
        """반경 안의 주유소·충전소. 상세주소는 채우지 않는다.

        데운 스냅샷이 이 지점을 덮으면 원격을 타지 않고 거리로 거른다. 덮지 못하면
        이 지점만 라이브로 조회해 스냅샷에 합친 뒤 거른다(단건 심사·극단 반경 대비).
        """

        if not self.enabled:
            return []

        x, y = to_katec(center)
        radius = int(min(max(radius_m, 1), MAX_RADIUS_M))
        if self._covers(x, y, radius):
            return self._filter(center, radius)

        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            verify=shared_verify(),
        ) as client:
            stations = await self._fetch_around(client, center, radius)
        # 라이브 조회 성공분만 스냅샷에 합친다. 실패는 예외로 전파돼 캐시되지 않는다.
        for station in stations:
            self._merge(self._snapshot, station)
        self._covered.append((x, y, float(radius)))
        self._cached_at = time.monotonic()
        self._warmed = True
        # 라이브 조회는 오피넷이 이미 반경으로 거른 결과라 그대로 돌려준다(종전 동작
        # 유지). 상위 판정 서비스가 자기 search_limit_m 으로 다시 정확히 거른다.
        return stations

    async def station_detail(self, station_id: str) -> OpinetStation | None:
        """주소·LPG 취급 여부가 포함된 상세. 증빙 표기에 쓴다.

        같은 station_id 는 메모이즈해 판정마다 다시 때리지 않는다. 조회 실패는
        캐시하지 않고 예외로 올린다(상위에서 상세 없이 진행한다).
        """

        if not self.enabled or not station_id:
            return None
        if station_id in self._detail_cache:
            return self._detail_cache[station_id]

        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            verify=shared_verify(),
        ) as client:
            payload = await self._get(
                client,
                OPINET_DETAIL_URL,
                # detailById.do 도 `certkey · out · id` 명세다. around 와 동일 버그.
                {"certkey": self.api_key, "id": station_id, "out": "json"},
            )
        rows = _rows(payload)
        if not rows:
            self._detail_cache[station_id] = None
            return None
        row = rows[0]
        station = self._station(row, lpg=str(row.get("LPG_YN") or "N") == "Y")
        if not station:
            self._detail_cache[station_id] = None
            return None
        detail = station._replace(
            address=str(row.get("VAN_ADR") or ""),
            road_address=str(row.get("NEW_ADR") or ""),
        )
        self._detail_cache[station_id] = detail
        return detail

    @staticmethod
    def _station(row: dict[str, Any], lpg: bool) -> OpinetStation | None:
        station_id = str(row.get("UNI_ID") or "").strip()
        x = row.get("GIS_X_COOR")
        y = row.get("GIS_Y_COOR")
        if not station_id or x in (None, "") or y in (None, ""):
            return None
        try:
            coordinates = from_katec(float(x), float(y))
        except (TypeError, ValueError):
            return None
        return OpinetStation(
            station_id=station_id,
            name=str(row.get("OS_NM") or "").strip(),
            brand=brand_label(str(row.get("POLL_DIV_CD") or row.get("POLL_DIV_CO") or "")),
            coordinates=coordinates,
            lpg=lpg,
        )

    async def _get(
        self,
        client: httpx.AsyncClient,
        url: str,
        params: dict[str, str],
    ) -> dict[str, Any]:
        try:
            response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise OpinetAPIError(f"오피넷 요청에 실패했습니다: {exc}") from exc

        if response.status_code != 200:
            raise OpinetAPIError(
                f"오피넷 응답 오류 ({response.status_code})",
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise OpinetAPIError("오피넷 응답을 해석하지 못했습니다.") from exc
        if not isinstance(payload, dict):
            raise OpinetAPIError("오피넷 응답 형식이 올바르지 않습니다.")
        return payload
