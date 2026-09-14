"""생활안전지도(safemap) 전국 주유시설 조회 — IF_0033.

한 원천이 주유소·LPG충전소·겸업을 한 번에 준다. 1차 매입제외의 23번(주유소)과
11번(LPG 충전소)을 동시에 채운다.

실호출로 확정한 사실(2026-08-28):
- 경로는 소문자 `openapi2` · http 스킴이다. http 는 https 로 302 리다이렉트하므로
  `follow_redirects=True` 가 필수다. 안 하면 302 HTML 본문을 받아 JSON 파싱이
  조용히 터진다.
- `numOfRows=1000` 은 정상, `numOfRows=1` 은 실패한다. 페이지당 1000으로 받는다.
- 좌표 `x`·`y` 는 EPSG:3857(Web Mercator)다. `gis_x_coor`·`gis_y_coor` 는
  좌표계를 특정하지 못해 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import time
from functools import lru_cache
from typing import Any, NamedTuple

import httpx
from pyproj import Transformer

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.single_flight import LoopSafeLock


SAFEMAP_IF0033_URL = "http://www.safemap.go.kr/openapi2/IF_0033"

# 실측: numOfRows=1 은 실패한다. 1000 으로 받고 15페이지면 전량(약 14,417건)이다.
PAGE_SIZE = 1000

# 안전장치. 전국이 15,000건 수준이라 이 이상은 응답 이상으로 본다.
MAX_PAGES = 40

# 주유시설은 하루에도 몇 곳씩 바뀌지 않는다. 하루 한 번이면 충분하다.
CACHE_TTL_SECONDS = 24 * 60 * 60

# 좌표 x·y 는 Web Mercator 다.
SAFEMAP_CRS = "EPSG:3857"


class SafemapAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SafemapStation(NamedTuple):
    """생활안전지도 주유시설 한 곳.

    한 레코드가 주유소·LPG충전소·겸업 중 하나다. lpg_yn·상표 컬럼으로 갈라
    `is_gas_station`·`is_lpg_station` 플래그를 세운다.
    """

    station_id: str  # uni_cd (오피넷 UNI_ID 와 같은 체계로 보임 — 보고 참조)
    name: str
    coordinates: Coordinates
    address: str = ""
    road_address: str = ""
    phone: str = ""
    oil_brand: str = ""  # poll_div_co (주유소 상표)
    gas_brand: str = ""  # gpoll_div_co (충전소 상표)
    lpg_yn: bool = False
    is_gas_station: bool = False
    is_lpg_station: bool = False
    # 상표·lpg_yn 이 전부 공란이라 주유소·LPG 어느 쪽으로도 가를 수 없는 시설.
    # is_gas_station·is_lpg_station 이 둘 다 False 인 상태와 함께 세운다.
    is_unclassified: bool = False


@lru_cache(maxsize=1)
def _to_wgs84() -> Transformer:
    """EPSG:3857 → WGS84 변환기. 생성 비용이 커서 캐시한다."""

    return Transformer.from_crs(SAFEMAP_CRS, "EPSG:4326", always_xy=True)


def _station(row: dict[str, Any]) -> SafemapStation | None:
    x = row.get("x")
    y = row.get("y")
    if x in (None, "") or y in (None, ""):
        return None
    try:
        lng, lat = _to_wgs84().transform(float(x), float(y))
    except (TypeError, ValueError):
        return None
    # 좌표가 한반도 밖이면 데이터 오류로 보고 버린다.
    if not (33.0 <= lat <= 39.5 and 124.0 <= lng <= 132.0):
        return None

    oil_brand = str(row.get("poll_div_co") or "").strip()
    gas_brand = str(row.get("gpoll_div_co") or "").strip()
    lpg_yn = str(row.get("lpg_yn") or "N").strip().upper() == "Y"

    # 주유소 상표가 있으면 주유소, 충전소 상표나 LPG 취급이면 충전소. 둘 다면 겸업.
    is_gas = bool(oil_brand)
    is_lpg = bool(gas_brand) or lpg_yn
    # 어느 쪽으로도 안 갈리면(상표·lpg_yn 이 전부 공란) 주유소로 단정하지 않는다.
    # 정체불명 시설을 주유소로 강제 분류하면 임계거리 이내일 때 매입제외로 승격돼
    # 「조회 안 된 구분」이 확정으로 둔갑한다(룰북 §3). 미분류로 남겨 상위 판정이
    # review_required 로 내리게 한다.
    is_unclassified = not is_gas and not is_lpg

    return SafemapStation(
        station_id=str(row.get("uni_cd") or "").strip(),
        name=str(row.get("os_nm") or "").strip(),
        coordinates=Coordinates(lat=lat, lng=lng),
        address=str(row.get("van_adr") or "").strip(),
        road_address=str(row.get("new_adr") or "").strip(),
        phone=str(row.get("tel") or "").strip(),
        oil_brand=oil_brand,
        gas_brand=gas_brand,
        lpg_yn=lpg_yn,
        is_gas_station=is_gas,
        is_lpg_station=is_lpg,
        is_unclassified=is_unclassified,
    )


class SafemapFuelClient:
    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self._cache: list[SafemapStation] = []
        self._cached_at = 0.0
        # 전량 조회가 완료돼 캐시가 데워졌는지. 빈 응답과 미조회를 구분하기 위해
        # 캐시 길이가 아니라 이 플래그로 판단한다.
        self._warmed = False
        # 백그라운드 프리페치 태스크. 요청 스레드를 막지 않고 캐시를 데운다.
        self._prefetch_task: asyncio.Task[None] | None = None
        # 전국 목록 캐시 채우기를 직렬화하는 single-flight 잠금. 동시 호출이 여럿
        # 이어도 실제 조회는 1회만 일어난다(캐시 스탬피드 방지).
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    def _is_warm(self) -> bool:
        return (
            self._warmed
            and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS
        )

    async def stations_around(
        self,
        center: Coordinates,
        radius_m: float,
    ) -> list[SafemapStation]:
        """반경 안의 주유시설. 전국 목록을 캐시해 두고 거리로 거른다.

        주의: 캐시가 비어 있으면 여기서 전량(약 14,417건·14페이지)을 동기로 받아
        요청 스레드를 2분 넘게 막는다. 판정 경로(요청-응답)에서는 이 메서드가 아니라
        `stations_around_cached()` 를 써서 콜드스타트로 요청이 멈추지 않게 한다.
        이 메서드는 오프라인 프리페치·테스트용으로 남긴다.
        """

        stations = await self.all_stations()
        return [
            station
            for station in stations
            if haversine_meters(center, station.coordinates) <= radius_m
        ]

    async def stations_around_cached(
        self,
        center: Coordinates,
        radius_m: float,
    ) -> list[SafemapStation]:
        """비차단 반경 조회.

        콜드스타트 조치(2026-08-28): 첫 호출이 전국 14페이지를 동기로 받아 요청을
        2분 넘게 막는 것을 없앤다. 캐시가 데워져 있으면 거리로 걸러 돌려주고, 아직
        비어 있으면 백그라운드 프리페치만 걸고 즉시 콜드 신호(SafemapAPIError)를
        올린다. 상위(판정 서비스)는 이 신호를 원천 조회 실패로 받아 '충돌 없음'이
        아니라 '스냅샷 미확보'로 정직하게 반영한다. 조용히 빈 리스트를 돌려주면
        미조회가 '충돌 없음'으로 둔갑하므로 그렇게 하지 않는다.
        """

        if not self.enabled:
            return []
        if self._is_warm():
            return [
                station
                for station in self._cache
                if haversine_meters(center, station.coordinates) <= radius_m
            ]
        self._ensure_prefetch()
        raise SafemapAPIError(
            "생활안전지도 주유시설 전국 캐시가 아직 준비되지 않았습니다. "
            "백그라운드 적재 중이니 잠시 뒤 재조회하세요."
        )

    def _ensure_prefetch(self) -> None:
        """백그라운드로 전량 캐시를 데운다. 이미 진행 중이면 중복 실행하지 않는다."""

        if self._prefetch_task is not None and not self._prefetch_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._prefetch_task = loop.create_task(self._safe_warm())

    async def _safe_warm(self) -> None:
        """프리페치 실패(429·키 오류 등)는 삼킨다. 다음 요청이 다시 시도한다."""

        try:
            await self.all_stations()
        except SafemapAPIError:
            pass

    async def all_stations(self) -> list[SafemapStation]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache

        async with self._fill_lock.get():
            # 잠금을 잡은 뒤 다시 확인한다. 먼저 들어간 호출이 이미 채웠으면
            # 재조회하지 않는다(double-checked).
            if self._is_warm():
                return self._cache

            stations: list[SafemapStation] = []
            # 조회가 실패하면 예외가 그대로 전파돼 캐시를 건드리지 않는다. 실패를
            # 캐시하지 않으므로 다음 호출이 다시 시도할 수 있다.
            # http → https 302 리다이렉트가 나므로 follow_redirects 를 켠다.
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                follow_redirects=True,
                verify=shared_verify(),
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    rows, total = await self._page(client, page)
                    for row in rows:
                        station = _station(row)
                        if station:
                            stations.append(station)
                    if len(rows) < PAGE_SIZE or page * PAGE_SIZE >= total:
                        break

            self._cache = stations
            self._cached_at = time.monotonic()
            self._warmed = True
            return stations

    async def _page(
        self,
        client: httpx.AsyncClient,
        page: int,
    ) -> tuple[list[dict[str, Any]], int]:
        params = {
            "serviceKey": self.service_key,
            "pageNo": str(page),
            "numOfRows": str(PAGE_SIZE),
            "returnType": "json",
        }
        try:
            response = await client.get(SAFEMAP_IF0033_URL, params=params)
        except httpx.HTTPError as exc:
            raise SafemapAPIError(f"주유시설 조회에 실패했습니다: {exc}") from exc

        if response.status_code != 200:
            raise SafemapAPIError(
                f"주유시설 응답 오류 ({response.status_code})",
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            # 302 를 안 따라갔거나 인증 실패로 HTML 이 오면 여기로 온다.
            raise SafemapAPIError("주유시설 응답을 해석하지 못했습니다.") from exc

        header = payload.get("header") or {}
        code = str(header.get("resultCode") or "00")
        # NORMAL_SERVICE 는 "00". 데이터 사용신청 미완이면
        # SERVICE_KEY_IS_NOT_REGISTERED_ERROR 가 온다 — 사유를 그대로 올린다.
        if code not in ("00", "0"):
            raise SafemapAPIError(
                f"주유시설 오류: {header.get('resultMsg') or code}"
            )

        body = payload.get("body") or {}
        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        rows = [row for row in items if isinstance(row, dict)]
        try:
            total = int(body.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = 0
        return rows, total
