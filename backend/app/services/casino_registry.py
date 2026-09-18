"""카지노영업소 명단 — 문화체육관광부 허가 18곳(외국인전용 17 + 강원랜드).

H-04-바 카지노영업소(위락시설 25m · 다자녀만)의 판정 원천이다. 카지노는 관광진흥법상
문체부 허가라 지자체 인허가(행안부 LOCALDATA)에 없고, 문체부 「카지노 현황」(3075667)은
HWP 집계·주소 없음, TourAPI 는 강원랜드 1곳만 등록돼 있어 API 로 전국 명단을 받을 길이
없다(2026-09-17 실측). 허가 업소가 18곳으로 고정돼 있어 H-04-바 §8 대로 목록을 코드
상수로 두고 주소를 카카오로 지오코딩한다 — 로컬 파일 원칙의 유일한 예외다.

명단 정본: 한국카지노업관광협회 회원사(koreacasino.or.kr/kcasino/asso/members.do,
2026-09-17 열람 · 18곳 전부 주소 기재). 문체부 허가 변경(신규·폐업·이전)이 있으면
이 표를 고치고 H-04-바 문서에 근거를 남긴다.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.single_flight import LoopSafeLock

CASINO_REGISTRY_URL = "http://koreacasino.or.kr/kcasino/asso/members.do"
CASINO_REGISTRY_LABEL = "한국카지노업관광협회 회원사 명단(문체부 허가 18곳)"
CASINO_REGISTRY_AS_OF = "2026-09-17"
CACHE_TTL_SECONDS = 24 * 60 * 60

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


class CasinoEntry(NamedTuple):
    name: str
    region: str
    venue: str
    address: str
    status: str = "영업"


# 회원사 페이지 표기 그대로. 주소는 도로명(지오코딩 입력).
CASINOS: tuple[CasinoEntry, ...] = (
    CasinoEntry("파라다이스카지노 워커힐점", "서울", "쉐라톤그랜드워커힐호텔", "서울특별시 광진구 워커힐로 177"),
    CasinoEntry("세븐럭카지노 강남코엑스점", "서울", "코엑스 컨벤션별관", "서울특별시 강남구 테헤란로87길 58"),
    CasinoEntry("세븐럭카지노 서울드래곤시티점", "서울", "서울드래곤시티", "서울특별시 용산구 청파로20길 95"),
    CasinoEntry("세븐럭카지노 부산롯데점", "부산", "롯데호텔부산", "부산광역시 부산진구 가야대로 772"),
    CasinoEntry("파라다이스카지노 부산점", "부산", "파라다이스호텔부산", "부산광역시 해운대구 해운대해변로 296"),
    CasinoEntry("파라다이스카지노 인천(파라다이스시티)", "인천", "파라다이스시티", "인천광역시 중구 영종해안남로321번길 186"),
    CasinoEntry("인스파이어카지노", "인천", "인스파이어엔터테인먼트리조트", "인천광역시 중구 공항문화로 127"),
    CasinoEntry("강원랜드카지노", "강원", "강원랜드호텔", "강원특별자치도 정선군 사북읍 하이원길 265"),
    # 2023-11 부터 휴업(협회 회원사 유지 · 허가 존속). 허가가 살아 있으므로 판정 대상에 둔다.
    CasinoEntry("알펜시아카지노", "강원", "알펜시아 홀리데이인리조트호텔", "강원특별자치도 평창군 대관령면 솔봉로 325", "휴업"),
    CasinoEntry("호텔인터불고대구카지노", "대구", "호텔인터불고대구", "대구광역시 수성구 팔현길 212"),
    CasinoEntry("공즈카지노", "제주", "라마다프라자제주호텔", "제주특별자치도 제주시 탑동로 66"),
    CasinoEntry("파라다이스카지노 제주점", "제주", "메종글래드제주", "제주특별자치도 제주시 노연로 80"),
    CasinoEntry("세븐스타카지노", "제주", "롯데호텔제주", "제주특별자치도 서귀포시 중문관광로72번길 35"),
    CasinoEntry("제주오리엔탈카지노", "제주", "제주오리엔탈호텔", "제주특별자치도 제주시 탑동로 47"),
    CasinoEntry("드림타워카지노", "제주", "제주드림타워", "제주특별자치도 제주시 노연로 12"),
    CasinoEntry("블루원카지노", "제주", "제주썬호텔", "제주특별자치도 제주시 삼무로 67"),
    CasinoEntry("레스에이카지노", "제주", "제주신화월드", "제주특별자치도 서귀포시 안덕면 신화역사로304번길 38"),
    CasinoEntry("제주 펠릭스 카지노", "제주", "제주신라호텔", "제주특별자치도 서귀포시 중문관광로72번길 75"),
)


class Casino(NamedTuple):
    """지오코딩까지 마친 카지노 한 곳."""

    facility_id: str
    name: str
    region: str
    venue: str
    address: str
    status: str
    coordinates: Coordinates


class GeocodeFailure(NamedTuple):
    name: str
    address: str


def _facility_id(entry: CasinoEntry) -> str:
    return "".join(entry.name.split())


class CasinoRegistryClient:
    """명단 18곳을 지오코딩해 캐시(24시간)하고 반경으로 거른다."""

    def __init__(self, geocode: Geocoder | None = None) -> None:
        self._geocode = geocode
        self._cache: list[Casino] = []
        self._failures: list[GeocodeFailure] = []
        self._cached_at = 0.0
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return self._geocode is not None

    @property
    def geocode_failures(self) -> list[GeocodeFailure]:
        return list(self._failures)

    async def casinos_around(self, center: Coordinates, radius_m: float) -> list[Casino]:
        casinos = await self.all_casinos()
        return [c for c in casinos if haversine_meters(center, c.coordinates) <= radius_m]

    async def all_casinos(self) -> list[Casino]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache
        async with self._fill_lock.get():
            if self._is_warm():
                return self._cache
            assert self._geocode is not None
            casinos: list[Casino] = []
            failures: list[GeocodeFailure] = []
            for entry in CASINOS:
                coordinates = await self._geocode(entry.address)
                if coordinates is None:
                    failures.append(GeocodeFailure(entry.name, entry.address))
                    continue
                casinos.append(
                    Casino(
                        facility_id=_facility_id(entry),
                        name=entry.name,
                        region=entry.region,
                        venue=entry.venue,
                        address=entry.address,
                        status=entry.status,
                        coordinates=coordinates,
                    )
                )
            self._cache = casinos
            self._failures = failures
            self._cached_at = time.monotonic()
            return casinos

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS
