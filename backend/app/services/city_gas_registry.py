"""도시가스 제조시설 명단 — LNG 생산기지·민간 LNG 터미널·바이오가스 제조소(12곳).

H-02-아 도시가스 제조시설(위험물 50m)의 판정 원천이다. 도시가스사업법 §2 5호·시행규칙
§2⑤ 1호의 「가스제조시설」은 **도시가스의 하역·저장·기화·송출 시설**이고, 같은 항
4~6호가 나프타부생가스·바이오가스·합성천연가스 제조시설이다. 정압기지·밸브기지·
공급관리소는 「가스배관시설」(2호), 충전소는 「가스충전시설」(3호)이라 아목이 아니다.
이 정의에 맞는 전국 시설의 소재지를 주는 공개 API 는 없다(2026-09-18 실측 —
가스공사 생산기지 현황 15040982 는 용량표뿐, 가스안전공사 15067840 은 시도 집계,
LOCALDATA 에 나프타부생가스·바이오가스제조사업 업종 없음). 시설 수가 적고 안정적이라
카지노(H-04-바 §8)와 같이 명단을 코드 상수로 두고 주소를 카카오로 지오코딩한다.

명단 정본: 한국가스공사 생산기지 소개(kogas.or.kr goGisView) · 민간LNG산업협회 터미널
현황(lngkorea.or.kr/2_2_2.php) · 각 사 소재지 페이지 · 예스코 중랑 바이오가스(§3⑥
자기제조 허가 대상). 도시가스사 LNG 위성기지·LPG-Air 제조소·나프타부생가스제조사업소는
시도 허가 대장이 비공개라 **미확보**(rulebook 비고에 적음).

부지가 넓다(평택 기지 폭 약 1km). 점 좌표로 재면 실제보다 멀게 나오므로 서비스의 필지
경계 재측정(브이월드 연속지적도 필지 폴리곤)에 맡긴다 — 지오코딩 점이 품는 필지가 시설
부지라 경계 대 경계 거리가 된다.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from app.models import Coordinates
from app.services.address_candidates import address_candidates
from app.services.geo import haversine_meters
from app.services.single_flight import LoopSafeLock

CITY_GAS_REGISTRY_URL = "https://www.lngkorea.or.kr/2_2_2.php"
CITY_GAS_REGISTRY_LABEL = "도시가스 제조시설 명단(LNG 생산기지·터미널·바이오가스 제조소 12곳)"
CITY_GAS_REGISTRY_AS_OF = "2026-09-18"
CACHE_TTL_SECONDS = 24 * 60 * 60

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


class CityGasPlantEntry(NamedTuple):
    name: str
    operator: str
    region: str
    address: str
    kind: str
    status: str = "운영"
    source_url: str = ""


# 각 사 공개 소재지 표기 그대로. 도로명이 없는 부지는 지번(지오코딩 입력으로 쓴다).
CITY_GAS_PLANTS: tuple[CityGasPlantEntry, ...] = (
    CityGasPlantEntry("평택LNG생산기지", "한국가스공사", "경기", "경기도 평택시 포승읍 원정리 1",
                      "LNG생산기지", "운영", "https://www.kogas.or.kr/site/koGas/goGisView.do?gisIdx=12"),
    CityGasPlantEntry("인천LNG생산기지", "한국가스공사", "인천", "인천광역시 연수구 송도동 364",
                      "LNG생산기지", "운영", "https://www.kogas.or.kr/site/koGas/goGisView.do?gisIdx=13"),
    CityGasPlantEntry("통영LNG생산기지", "한국가스공사", "경남", "경상남도 통영시 광도면 안정리 1179",
                      "LNG생산기지", "운영", "https://www.kogas.or.kr/site/koGas/goGisView.do?gisIdx=14"),
    CityGasPlantEntry("삼척LNG생산기지", "한국가스공사", "강원", "강원특별자치도 삼척시 원덕읍 호산해변길 18",
                      "LNG생산기지", "운영", "https://www.kogas.or.kr/site/koGas/goGisView.do?gisIdx=15"),
    CityGasPlantEntry("제주LNG생산기지", "한국가스공사", "제주", "제주특별자치도 제주시 애월읍 애월해안로 59-38",
                      "LNG생산기지", "운영", "https://www.kogas.or.kr/site/koGas/goGisView.do?gisIdx=18"),
    # 1단계 2026 준공 예정. 허가·착공된 제조시설이라 명단에 두고 상태로 구분한다.
    CityGasPlantEntry("당진LNG생산기지", "한국가스공사", "충남", "충청남도 당진시 석문면 통정리 1600",
                      "LNG생산기지", "건설중", "https://www.motir.go.kr/"),
    CityGasPlantEntry("광양LNG터미널", "포스코인터내셔널", "전남", "전라남도 광양시 제철로 2148-581",
                      "민간LNG터미널", "운영", "https://www.poscointl.com/"),
    CityGasPlantEntry("보령LNG터미널", "보령엘엔지터미널(GS에너지·SK E&S)", "충남",
                      "충청남도 보령시 오천면 오천해안로 333", "민간LNG터미널", "운영",
                      "https://www.lngkorea.or.kr/2_2_2.php"),
    CityGasPlantEntry("코리아에너지터미널(울산 북항)", "한국석유공사·SK가스", "울산",
                      "울산광역시 남구 황성동 882-1", "민간LNG터미널", "운영", "https://www.ket.co.kr/"),
    CityGasPlantEntry("통영에코파워 LNG터미널", "통영에코파워(HDC)", "경남",
                      "경상남도 통영시 광도면 황리 1608", "민간LNG터미널", "운영",
                      "https://www.lngkorea.or.kr/2_2_2.php"),
    CityGasPlantEntry("동북아LNG허브터미널", "동북아엘엔지허브터미널(BS한양·GS에너지)", "전남",
                      "전라남도 여수시 묘도동", "민간LNG터미널", "건설중",
                      "https://www.lngkorea.or.kr/2_2_2.php"),
    # 일반도시가스사업자(예스코)의 바이오가스 자기제조 — 도시가스사업법 §3⑥ 허가 대상.
    CityGasPlantEntry("중랑 바이오가스 플랜트", "예스코", "서울", "서울특별시 성동구 자동차시장3길 64",
                      "바이오가스제조", "운영", "https://www.yesco.co.kr/"),
)


class CityGasPlant(NamedTuple):
    """지오코딩까지 마친 제조시설 한 곳."""

    facility_id: str
    name: str
    operator: str
    region: str
    address: str
    kind: str
    status: str
    source_url: str
    coordinates: Coordinates


class GeocodeFailure(NamedTuple):
    name: str
    address: str


def _facility_id(entry: CityGasPlantEntry) -> str:
    return "".join(entry.name.split())


class CityGasRegistryClient:
    """명단 12곳을 지오코딩해 캐시(24시간)하고 반경으로 거른다."""

    def __init__(self, geocode: Geocoder | None = None) -> None:
        self._geocode = geocode
        self._cache: list[CityGasPlant] = []
        self._failures: list[GeocodeFailure] = []
        self._cached_at = 0.0
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return self._geocode is not None

    @property
    def geocode_failures(self) -> list[GeocodeFailure]:
        return list(self._failures)

    async def plants_around(self, center: Coordinates, radius_m: float) -> list[CityGasPlant]:
        plants = await self.all_plants()
        return [p for p in plants if haversine_meters(center, p.coordinates) <= radius_m]

    async def all_plants(self) -> list[CityGasPlant]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache
        async with self._fill_lock.get():
            if self._is_warm():
                return self._cache
            assert self._geocode is not None
            plants: list[CityGasPlant] = []
            failures: list[GeocodeFailure] = []
            for entry in CITY_GAS_PLANTS:
                # 산단·항만 부지는 지번이 지오코더에 없기도 하다. 뒤 토큰을 떼며 재시도한다.
                coordinates: Coordinates | None = None
                for candidate in address_candidates(entry.address):
                    coordinates = await self._geocode(candidate)
                    if coordinates is not None:
                        break
                if coordinates is None:
                    failures.append(GeocodeFailure(entry.name, entry.address))
                    continue
                plants.append(
                    CityGasPlant(
                        facility_id=_facility_id(entry),
                        name=entry.name,
                        operator=entry.operator,
                        region=entry.region,
                        address=entry.address,
                        kind=entry.kind,
                        status=entry.status,
                        source_url=entry.source_url,
                        coordinates=coordinates,
                    )
                )
            self._cache = plants
            self._failures = failures
            self._cached_at = time.monotonic()
            return plants

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS
