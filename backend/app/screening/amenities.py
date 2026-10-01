"""생활편의시설 수집기 — 2차 심사표 배점의 입력을 만든다.

심사표가 지정한 인정 원천(localdata 대규모점포·심평원 종합병원·도시공원정보 등)은
아직 전부 붙지 않았다. 붙지 않은 자리는 지도 검색으로 근사하되, **무엇을 무엇으로
대체했는지**를 시설군마다 한국어 note 로 남긴다. 근사한 사실을 감추면 배점이
사실처럼 보여서 심사 결과를 오도한다.

거리 기준(평가기준 2항)
    사업부지 경계 → 시설 기준점 직선거리. 필지 경계가 없으면 주소점에서 잰다.
"""

from __future__ import annotations

import asyncio
import re
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NamedTuple

from app.models import Coordinates
from app.screening.front_door import (
    _AUTO_EXCLUDE_TOKENS,
    FACILITY_DOOR_TOKENS,
    HOSPITAL_EXCLUDE_TOKENS,
    station_exit_places,
    DATASET_GATE_SOURCE,
    DatasetGate,
    FrontDoorRef,
    FrontDoorStore,
    _university_tokens,
    auto_front_door,
    collect_door_candidates,
    dataset_gate_for,
    load_dataset_gates,
    normalize_key,
    university_base,
)
from app.screening.address_parcels import AddressParcelResolver
from app.screening.bus_headway import BusHeadwayResolver, StopHeadway
from app.screening import culture as culture_source
from app.screening import parks as park_source
from app.screening.scorebook import FACILITY_GROUPS
from app.rules_config import option_enabled, options_fingerprint
from app.lh_alignments import (
    LH_ALIGNMENT_SOURCE,
    LhAlignment,
    alignments_fingerprint,
    amenity_alignments,
)
from app.services.cadastral_local import CadastralLocalStore
from app.services.facility_store import FacilityStore
from app.services.geo import (
    distance_point_to_polygon_m,
    distance_point_to_polygons_m,
    distance_polygons_to_polygon_m,
    haversine_meters,
    nearest_boundary_point_multi,
    offset_coordinates,
    polygon_contains,
)
from app.services.institutions import is_public_office
from app.services.address_geocoder import make_address_geocoder
from app.services.kakao import KakaoClient
from app.services.naver_geocode import NaverGeocodeClient
from app.services.naver_search import NaverSearchClient
from app.services.seoul_bus import SeoulBusStopClient
from app.services.parcel_sanity import parcel_rejection_reason
from app.services.pnu_resolver import LegalDongIndex
from app.services.safemap_facilities import (
    SafemapFacility,
    SafemapFacilityFeed,
)
from app.services.hira_hospital import HiraHospitalClient
from app.services.school_locations import SchoolLocationClient
from app.services import traditional_market as market_source
from app.services.city_parks import CityParkClient
from app.services.ncmc_hospital import NcmcHospitalClient
from app.services.tago import TagoClient
from app.services.transfer_center import TransferCenterClient
from app.services.vworld import ParcelFeature, VWorldClient, VWorldPlace
from app.services.public_library import (
    LIBRARY_STANDARD_SOURCE,
    PublicLibraryAPIError,
    PublicLibraryClient,
)
from app.services.rail_stations import (
    KORAIL_STATION_SOURCE,
    KorailStationAPIError,
    KorailStationClient,
    StationExit,
    dataset_exits_for,
    load_station_exits,
    station_name,
)

# uvicorn 이 출력 설정을 걸어 둔 로거라 원천 장애가 서버 콘솔에 보인다.
logger = logging.getLogger("uvicorn.error")


# 등급 조건이 쓰는 최대 반경은 3km(주거여건 3km)다. 그보다 넓게 볼 이유가 없다.
MAX_RADIUS_M = 3000

# 카카오 로컬 API 한 질의당 하드 상한(3페이지 x 15건). 여기 닿으면 잘린 것이다.
KAKAO_RESULT_CAP = 45

# 사분면 재귀 깊이. 2 면 한 시설군당 최대 1+4+16=21 회로 묶인다.
MAX_SUBDIVIDE_DEPTH = 2

# 화면에 근거로 나열할 시설 수 상한. 세는 데 쓰는 거리 목록은 자르지 않는다.
MAX_HITS_PER_GROUP = 20

# 시설 경계(필지)에서 재는 시설군(docs/hazards/MEASUREMENT.md §3). 역·지하철은
# 출입구, 대학·종합병원은 정문이라 여기 없다. 버스정류장도 없다 — 정류장은 도로
# 필지 위에 있어 필지경계로 재면 도로 전체가 경계가 된다(2026-09-14 검수: 26m 오판).
# 정류장은 정류장 좌표(점)로 잰다.
BOUNDARY_GROUPS: frozenset[str] = frozenset(
    {
        # 종합병원 등 대형 필지 시설은 필지 경계(대지 끝점) 기준 — LH 09/22 결정 2
        # (09/11 결정 8 「정문 기본」을 바꿈). 대학교만 정문 좌표로 잰다.
        "hospital",
        "terminal",
        "transfer",
        "retail",
        "park",
        "culture",
        "public",
        "school_elementary",
        "school_middle",
        "school_high",
    }
)
# 시설군마다 필지 경계로 재는 시설 수 — 화면에 보이는 시설 전부다. 예전에는 좌표 기준
# 최근접 5곳만 경계로 쟀는데, 큰 공원은 중심 좌표가 멀어 5곳 밖으로 밀리고 경계로 재면
# 훨씬 가까웠다(2026-09-29 전주 서완산동2가: 완산공원 좌표 917m, LH앱 경계 557.8m).
# LH앱은 모든 시설을 경계(POLYGON_TO_POLYGON)로 잰다.
BOUNDARY_LOOKUP_PER_GROUP = MAX_HITS_PER_GROUP
# 필지 조회 동시 요청 수. 시설군 10개 × 20곳을 순서대로 부르면 수십 초가 걸린다.
BOUNDARY_LOOKUP_CONCURRENCY = 8
BOUNDARY_FALLBACK_NOTICE = "시설 경계(필지)를 확인하지 못해 시설 좌표로 쟀습니다."
# 시설 필지를 좌표가 아니라 시설 지번주소(PNU)로 먼저 찾는 시설군. LH앱은 학교 지번주소로
# 필지를 만든다. 좌표 필지는 옆 필지·도로라 50~150m 길었다(address_parcels 참조).
# 공원도 LH앱이 표준데이터 지번주소로 PNU 를 만든다(JB_54 location_basis=PNU). 큰 공원은
# 대표 좌표가 도로·옆 필지에 떨어져도 공원 지번 필지 경계로 잰다.
ADDRESS_PARCEL_GROUPS: frozenset[str] = frozenset(
    {"school_elementary", "school_middle", "school_high", "park"}
)
ADDRESS_PARCEL_NOTICE = "학교 지번주소 필지"
ADDRESS_PARCEL_NOTICES: dict[str, str] = {"park": "공원 지번주소 필지"}

# 역 출입구 조회(카카오 「{역명} N번출구」). 네이버 지역검색은 한 질의에 5건(무작위)만
# 돌려줘 출구가 여섯 이상인 역에서 가장 가까운 출구가 빠졌다(2026-09-14 건대입구역:
# 2번출구가 사업지 옆인데 5·1·3번만 와서 역 대표점 330m 로 쟀다). 카카오는 출구를
# 「지하철출구」 분류로 번호마다 정확히 돌려주므로 번호를 올리며 묻고, 연속으로
# 비면 그 역의 출구가 끝난 것으로 본다.
STATION_EXIT_CATEGORY = "지하철출구"
STATION_EXIT_MAX_NUMBER = 16
STATION_EXIT_MISS_STREAK = 3
STATION_EXIT_SEARCH_RADIUS_M = 1500

# 대학·종합병원 문 후보 — 카카오 「입출구」 분류 POI(「건국대학교 상허문」·「건국대학교병원
# 입구」·「세종대학교 정문」). 네이버 지역검색은 질의당 5건(무작위)이라 정문이 빠지는 일이
# 잦았다(2026-09-16 건국대학교병원: 정문 미확인 → 좌표 폴백). 시설명에 아래 접미를 붙여
# 묻고 분류가 입출구인 결과만 받는다.
GATE_CATEGORY = "입출구"
GATE_QUERY_SUFFIXES: tuple[str, ...] = ("정문", "문", "입구")
GATE_SEARCH_RADIUS_M = 3000

# LH 개별 맞춤 항목을 볼 범위 — 기준점이 수집 반경 + 이 거리 안인 항목만 본다. 이름으로
# 맞출 때 허용하는 거리(lh_alignments.NAME_MATCH_RADIUS_M)와 같게 둔다.
NAME_MATCH_MARGIN_M = 5000


def station_base(name: str) -> str:
    """역 이름에서 노선 꼬리(「2호선」·「(세종대)」)를 뗀 역명. 「건대입구역 2호선」→「건대입구역」."""

    stripped = name.strip()
    idx = stripped.find("역")
    return stripped[: idx + 1] if idx > 0 else stripped

# 담당자 수기 기준점 지정을 허용하는 역·터미널 계열 시설군(#11 「출구 여럿이면
# 담당자 선택」). 프런트 DESIGNATABLE_GROUPS 의 역 계열과 같은 집합이다.
_STATION_LIKE_DESIGNATABLE = ("railway", "subway", "terminal", "transfer")

KAKAO_DISABLED_NOTE = "카카오 REST API 키가 설정되지 않았습니다."

# 환승시설 원천 고지. 지정 원천은 전국대중교통환승센터 표준데이터(15034541, 활용 중)
# 하나다. 지도 검색(「환승센터」·「환승정류장」)은 LH 데이터셋 범위 밖이라 보충하지
# 않고, 표준데이터가 응답하지 않을 때만 대체로 쓰며 경고로 올린다.
TRANSFER_MISSING_NOTE = (
    "환승시설(간선급행버스체계법 제2조제3호다목) 위치 원천을 확보하지 못했습니다."
)
TRANSFER_STANDARD_SOURCE = "국토교통부 전국대중교통환승센터 표준데이터"
TRANSFER_STANDARD_NOTE = (
    "국토교통부 전국대중교통환승센터 표준데이터(운영 중)로 산정했습니다. 제공 기관에 "
    "등재되지 않은 지역의 환승시설은 세지 않습니다(LH 데이터셋과 같은 범위)."
)
TRANSFER_SUBSTITUTED_NOTE = (
    "전국대중교통환승센터 표준데이터(15034541)가 응답하지 않아 지도 검색"
    "(「환승센터」·「환승정류장」)으로 대체했습니다."
)
TRANSFER_FALLBACK_ALERT = (
    "국토교통부 전국대중교통환승센터 표준데이터가 응답하지 않아 카카오 지도 검색"
    "(「환승센터」·「환승정류장」)으로 환승시설을 대체했습니다. LH 데이터셋 범위와 "
    "다를 수 있으니 원천이 복구되면 다시 심사하세요."
)
TRANSFER_KEYWORDS: tuple[str, ...] = ("환승센터", "환승정류장")
# 지도 근사에서 환승시설로 인정하는 분류(잎). 「환승센터약국」·「환승센터 전기차충전소」·
# 「○○환승센터 2-3출입구」처럼 이름에 환승이 들어간 다른 시설을 거른다. 환승주차장은
# 간선급행버스체계법 환승시설에 들어가므로 주차장 분류는 남긴다.
TRANSFER_CATEGORY_LEAVES: frozenset[str] = frozenset(
    {"교통시설", "주차장", "버스정류장", "버스터미널", "환승센터"}
)

# ---------------------------------------------------------------------------
# 역·터미널·도서관 — 지도 회사와 무관한 원천(공공 API → VWorld 장소검색)을 먼저 쓴다.
# 카카오 장소검색은 마지막 대체다. 카카오·네이버 중 어느 지도가 살아 있든 판정이 같아야
# 한다(사용자 방침 2026-09-30, 카카오 일일 한도 소진으로 실측).
# ---------------------------------------------------------------------------
VWORLD_PLACE_SOURCE = "VWorld 장소검색(국토정보플랫폼)"
VWORLD_LIBRARY_SOURCE = "VWorld 장소검색 「공공도서관」 분류"
VWORLD_RAILWAY_SOURCE = "VWorld 장소검색 일반·고속철도역 분류"
VWORLD_TERMINAL_SOURCE = "VWorld 장소검색 시외·고속·종합버스터미널 분류"
VWORLD_SUBWAY_SOURCE = "VWorld 장소검색 지하철역·지하철역입구 분류"
LIBRARY_NONE_SOURCE = "도서관 미반영"

# VWorld 도서관 질의. 분류 문자열에도 걸려 이름에 「공공도서관」이 없는 곳도 온다.
VWORLD_LIBRARY_QUERY = "공공도서관"
VWORLD_LIBRARY_LEAF = "공공도서관"
# 표준데이터·VWorld 도서관이 이 거리 안이면 같은 도서관이다(같은 건물의 방 이름 줄 포함).
LIBRARY_SAME_SPOT_M = 100.0
# 표준데이터·VWorld 도서관 이름이 같으면(한쪽이 다른 쪽을 품으면) 이 거리 안까지 같은 도서관이다.
# 표준데이터 좌표가 수 km 틀린 줄이 있다(금강도서관 3.1km). LH 개별 맞춤 이름 거리와 같다.
LIBRARY_NAME_MATCH_M = 5000.0
# VWorld 철도역 질의와 받는 분류(잎). 「철도정거장」 분류는 화물역·무배치 간이역(동익산역·
# 동산역·황등역)까지 담아 LH 철도역 목록보다 넓다 — 쓰지 않는다.
VWORLD_RAILWAY_QUERY = "철도역"
VWORLD_RAILWAY_LEAVES: frozenset[str] = frozenset({"일반철도역", "고속철도역"})
# VWorld 터미널 질의(분류 이름). 「버스터미널」·「터미널」은 정류장까지 걸려 1만 건을 넘는다.
VWORLD_TERMINAL_QUERIES: tuple[str, ...] = ("시외버스터미널", "고속버스터미널", "종합버스터미널")
VWORLD_TERMINAL_LEAVES: frozenset[str] = frozenset(VWORLD_TERMINAL_QUERIES)
# 같은 이름이 이 거리 안에 여러 번(분류만 다르게) 실리면 한 곳이다.
VWORLD_SAME_PLACE_M = 150.0
VWORLD_SUBWAY_QUERY = "지하철역"
VWORLD_SUBWAY_LEAF = "지하철역"
VWORLD_SUBWAY_EXIT_LEAF = "지하철역입구"

RAILWAY_KORAIL_NOTE = (
    "한국철도공사 역위치 정보(지정 원천)로 역을 찾고, 전북은 LH 데이터셋이 모은 역 출구 "
    "좌표(철도역 JB_48·KTX역 JB_49)에서, 그 밖은 지도 출구 검색으로 찾은 가장 가까운 "
    "출입구에서 잽니다."
)
RAILWAY_VWORLD_NOTE = (
    "한국철도공사 역위치 정보 대신 VWorld 장소검색(일반·고속철도역 분류)으로 역을 찾았습니다 "
    "(전북 12역이 LH 철도역·KTX역 목록의 역과 같음). 전북은 LH 데이터셋 역 출구 좌표에서, 그 밖은 "
    "지도 출구 검색으로 찾은 가장 가까운 출입구에서 잽니다."
)
RAILWAY_KAKAO_NOTE = (
    "한국철도공사 역위치 정보 대신 카카오 지도 검색으로 근사했습니다. "
    "출입구 좌표는 LH 데이터셋 출구점(전북) 또는 지도 출구 검색으로 찾고, "
    "찾지 못한 역은 역 대표점으로 잽니다."
)
TERMINAL_VWORLD_NOTE = (
    "대중교통수단 터미널 정보(LH 데이터셋, 비공개 파일) 대신 VWorld 장소검색의 시외·고속·"
    "종합버스터미널 분류로 근사했습니다. LH 목록에 있는 철도역 이름 터미널·간이 정류소·공항은 "
    "이 분류에 없습니다."
)
TERMINAL_KAKAO_NOTE = "대중교통수단 터미널 정보 대신 카카오 지도 검색으로 근사했습니다."
SUBWAY_KAKAO_NOTE = (
    "역 출입구 좌표를 지도 검색(카카오 출구 번호별 조회 + 네이버 보충)으로 "
    "모아 가장 가까운 출입구를 기준점으로 씁니다. 출입구를 찾지 못한 역은 "
    "역 대표점으로 잽니다."
)
SUBWAY_VWORLD_NOTE = (
    "카카오 지도 검색 대신 VWorld 장소검색(지하철역·지하철역입구 분류)과 네이버 출구 검색으로 "
    "역과 출입구를 찾았습니다. 출입구를 찾지 못한 역은 역 대표점으로 잽니다."
)


FALLBACK_FAILED = "조회하지 못했습니다."


def _fallback_reason(exc: BaseException) -> str:
    """지정 원천이 빠진 까닭 한 토막 — 활용신청 전과 장애를 구분해 적는다."""

    if getattr(exc, "unapproved", False):
        return "활용신청 승인 전이라"
    return "응답하지 않아"


def _fallback_alert(
    failures: Sequence[tuple[str, BaseException]], used: str, caveat: str
) -> str:
    """앞 순위 원천이 빠져 다음 원천을 썼다는 경고. 빠진 원천이 없으면 빈 문자열.

    「전국도서관표준데이터(15013109)는 활용신청 승인 전이라 VWorld 장소검색 … 찾았습니다.」
    장애(승인 전이 아닌 실패)가 섞였으면 재심사 안내를 붙인다 — 조용히 대체하지 않는다.
    """

    if not failures:
        return ""
    head = ", ".join(f"{who} {_fallback_reason(exc)}" for who, exc in failures)
    text = " ".join(part for part in (head, used, caveat) if part)
    if any(not getattr(exc, "unapproved", False) for _, exc in failures):
        text += " 잠시 뒤 다시 심사해 주십시오."
    return text


def _exit_station(title: str) -> str:
    """출입구 이름의 역명. 「아차산역1번출입구」·「군자역(능동)3번출입구」 → 「아차산역」·「군자역」."""

    head = title.split("번출")[0].rstrip("0123456789")
    return station_base(station_name(head))


# 진행 화면용 원천 이름. 시설군 키와 같으면 심사표 라벨을 쓰고, 조회 단위가 다른 것만 따로 적는다.
FEED_LABELS: dict[str, str] = {
    **{group.key: group.label for group in FACILITY_GROUPS},
    "school": "초·중·고등학교",
    "bus_stop": "버스정류장",
    "traditional_market": "전통시장",
    "library": "공공도서관",
}

# (원천 이름, 표시 이름, 건수 또는 None, 성공 여부)
FeedProgressCallback = Callable[[str, str, int | None, bool], Awaitable[None]]


BUS_TAGO_ALERT = (
    "국토교통부 TAGO 정류소 조회가 응답하지 않아 카카오 지도 검색으로 버스정류장을 "
    "대체했습니다. 운행주기(15분) 판정을 하지 못했고 정류장이 빠졌을 수 있습니다."
)
BUS_HEADWAY_ALERT = (
    "국토교통부 TAGO 노선·배차 조회가 응답하지 않아 운행주기(15분) 판정 없이 "
    "정류장을 모두 셌습니다."
)
BUS_EMPTY_ALERT = (
    "반경 안에서 버스정류장을 한 곳도 찾지 못했습니다. 도심에서는 드문 일이라 "
    "TAGO·지도 검색 응답 이상일 수 있습니다."
)


def _layer_alert(layer: str, fallback: str) -> str:
    return (
        f"생활안전지도 {layer} 레이어가 응답하지 않아 {fallback}로 대체했습니다. "
        "지정 원천과 결과가 다를 수 있습니다."
    )


class SourceMissing(RuntimeError):
    """조회 장애가 아니라 쓸 원천이 아예 없을 때. 시설군 note 에 문구 그대로 싣는다."""

# 상업시설은 심사표가 조회처를 못박은 항목이라 근사하지 않는다. 대규모점포
# 원장이 적재돼 있으면 그것으로 산정하고, 미적재면 '없음'이 아니라 '미적재'로
# 남긴다(룰북 원칙: 원천 부재를 0 으로 단정하지 않는다).
RETAIL_MISSING_NOTE = (
    "심사표는 대규모점포 조회(localdata)와 전통시장통통 등재분만 인정합니다. "
    "대규모점포 원장이 아직 적재되지 않아 산정하지 않습니다."
)

# 상업시설 = 대규모점포 + 전통시장(LH 최종보고서 2차 상업시설, 전북 138곳). 전통시장은
# 전국전통시장표준데이터(2026-09-30 활용신청 승인)로 더한다. 원천이 빠지면
# market_source.RETAIL_STORES_ONLY_NOTE 로 내리고 경고한다(_build_localdata_group).
RETAIL_CONNECTED_NOTE = (
    "행정안전부 대규모점포 인허가 원장과 소상공인시장진흥공단 전국전통시장표준데이터로 "
    "산정했습니다. 대규모점포와 40m 안에 겹치는 시장은 한 번만 셉니다."
)

KAKAO_PLACE_SOURCE = "카카오 장소검색"
TAGO_SOURCE = "국토교통부 TAGO 정류소 근접조회"
TAGO_HEADWAY_SOURCE = "국토교통부 TAGO 정류소 근접조회 + 경유노선·노선 배차간격"

# 버스정류장 운행주기 고지. LH 심사 담당자 계산법(2026-09-15 백승환 대리 자료):
# 노선별 60/배차간격 합산 ÷4 = 15분당 평균 도착 버스 수 ≥ 1 → 인정.
BUS_STOP_HEADWAY_NOTE = (
    "운행주기 15분 판정 — 경유 노선별 60/배차간격(분)을 합산해 4로 나눈 「15분당 평균 "
    "도착 버스 수」가 1 이상이면 인정(LH 심사 담당자 계산법, 2026-09-15). 배차간격을 "
    "확인한 정류장 중 미달만 배점에서 빼고, 배차를 확인할 수 없는 정류장은 셌습니다."
)
BUS_STOP_NO_HEADWAY_NOTE = (
    "운행주기 15분 이내 요건을 확인할 수 있는 원천이 없어 전체 정류장을 셌습니다."
)
SEOUL_BUS_SOURCE = "서울 열린데이터광장 버스정류소 위치정보"
LOCALDATA_SOURCE = "행정안전부 지방행정인허가 대규모점포"
NCMC_HOSPITAL_SOURCE = "국립중앙의료원 전국 병·의원 찾기(종합병원)"
HIRA_HOSPITAL_SOURCE = "건강보험심사평가원 병원정보서비스(종별 종합병원·상급종합)"
# 생활안전지도 시설 레이어(2026-09-17 데이터 사용신청 승인). 레이어별 지정 원천.
SAFEMAP_SCHOOL_SOURCE = "교육부 학교알리미 초·중·고 위치(생활안전지도 IF_0035)"
SCHOOL_STANDARD_SOURCE = "전국초중등학교위치표준데이터(한국교육시설안전원)"
SAFEMAP_UNIVERSITY_SOURCE = "교육부 대학교 위치(생활안전지도 IF_0034)"
# 레이어 행에 붙이는 분류. _is_university 가 지도 검색과 같은 규칙으로 읽는다.
UNIVERSITY_LAYER_CATEGORY = "교육,학문 > 학교 > 대학교"
# 레이어 좌표는 본교 주소점이라 정문과 수백 m 떨어진다(전북 71곳 중 최대 612m ·
# 원광대). 정문이 반경 안인데 주소점이 반경 밖이라 빠지지 않게 넓혀 받고, 정문까지
# 잰 뒤 반경으로 다시 자른다(2026-09-30: 060 수의방역대학원 2,924m · 094 원광대 2,848m).
UNIVERSITY_LAYER_MARGIN_M = 1000
SAFEMAP_OFFICE_SOURCE = "행정안전부 민원행정기관 전자지도(생활안전지도 IF_0031)"
SAFEMAP_HOSPITAL_SOURCE = "국립중앙의료원 종합병원(생활안전지도 IF_0022)"

# 대학교 기준점 순서 — 담당자 수기 지정 → 표준 데이터셋 정문 좌표(대학알리미 + 수기
# 보완, university_gates_jeonbuk.csv) → LH 개별 맞춤 등록부 → 네이버 지역검색 문 후보.
UNIVERSITY_FRONT_DOOR_ORDER = (
    "기준점은 정문입니다 — 담당자 수기 지정, 표준 데이터셋 정문 좌표, LH 개별 맞춤, "
    "네이버 지역검색 문 후보 순으로 정하고, 정문을 확인하지 못한 캠퍼스는 대표점으로 "
    "재며 그 사실을 시설마다 적습니다."
)
UNIVERSITY_LAYER_NOTE = (
    "교육부 대학교 위치(생활안전지도 IF_0034 · LH 데이터셋 대학알리미와 같은 교육부 "
    "원천)로 대학교를 셉니다. " + UNIVERSITY_FRONT_DOOR_ORDER
)
UNIVERSITY_KAKAO_NOTE = (
    "교육부 대학교 위치(생활안전지도 IF_0034) 대신 지도 검색(「대학교」)으로 "
    "근사했습니다. " + UNIVERSITY_FRONT_DOOR_ORDER
)

# 정문을 어느 원천에서도 못 찾은 대학에 붙이는 지정 대기 고지(국장님 §3-3). 좌표
# 폴백을 쓰되 그 사실을 감추지 않는다.
FRONT_DOOR_PENDING_NOTICE = (
    "대학 정문 미확인 — 정문 기준점 지정 대기(현재 시설 좌표 기준 보수 폴백)"
)


# 국립중앙의료원 원장으로 종합병원을 산정할 때의 고지. 상급종합병원은 종합병원에
# 포함해 인정한다(2026-09-18 확정) — is_tertiary 는 표기 구분용이다.
HOSPITAL_NCMC_NOTE = (
    "국립중앙의료원 전국 병·의원 원장에서 종류=종합병원·상급종합병원을 산정했습니다 "
    "(상급종합병원은 종합병원에 포함해 인정 — 2026-09-18 확정)."
)
# 심사표 지정 원천. LH 표준 데이터셋(JB_00_HOSPITALS)도 이 목록이다.
HOSPITAL_HIRA_NOTE = (
    "건강보험심사평가원 병원정보서비스에서 종별=종합병원·상급종합병원을 산정했습니다 "
    "(심사표 지정 원천 · 상급종합병원은 종합병원에 포함해 인정 — 2026-09-18 확정)."
)
HOSPITAL_LAYER_NOTE = (
    "국립중앙의료원 종합병원 원장의 전국본(생활안전지도 IF_0022)으로 산정했습니다. "
    "종류=종합병원 381곳 · 일 단위 갱신."
)
SCHOOL_STANDARD_NOTE = (
    "전국초중등학교위치표준데이터(한국교육시설안전원 · 운영 중 학교)로 산정했습니다. "
    "이전한 학교도 현재 위치로 잽니다."
)
SCHOOL_LAYER_NOTE = (
    "교육부 학교알리미 초·중·고 위치(생활안전지도 IF_0035)로 산정했습니다. "
    "분교장·특수학교는 이름의 학교급으로 가릅니다."
)
SCHOOL_KAKAO_NOTE = (
    "교육부 학교 위치(생활안전지도 IF_0035) 대신 지도 학교 분류에서 이름 토큰으로 "
    "근사했습니다."
)
PUBLIC_LAYER_NOTE = (
    "도청·시군구청 본청과 행정복지센터(동 주민센터·읍면사무소)는 행정안전부 민원행정기관 "
    "전자지도(생활안전지도 IF_0031)로 세고, 공공도서관은 도서관 원천으로 더했습니다 "
    "(LH 데이터셋 공공시설 = 관공서 본청·행정복지센터·공공도서관)."
)
PUBLIC_KAKAO_NOTE = (
    "행안부 민원행정기관 전자지도(생활안전지도 IF_0031) 대신 지도 공공기관 분류로 "
    "근사했습니다."
)

# 시설군 판별은 이름이 아니라 카카오 category_name 의 마지막 조각으로 한다.
# 이름만 보면 '현대가구백화점'(가구점)·'영어도서관학원'(학원)·'쪽구름도서관
# 화장실'(화장실)이 그대로 배점 근거로 올라온다. 실측에서 전부 확인했다.
PARK_CATEGORIES: frozenset[str] = frozenset(
    {
        "공원",
        "도시근린공원",
        "도시자연공원",
        "근린공원",
        "어린이공원",
        "생태공원",
        "국립공원",
        "도립공원",
        "군립공원",
        "수목원",
    }
)

LIBRARY_CATEGORIES: frozenset[str] = frozenset(
    {"도서관", "국공립도서관", "작은도서관", "전문도서관", "어린이도서관", "공공도서관"}
)
# 공공도서관 원천(전국도서관표준데이터·VWorld 「공공도서관」 분류)이 준 분류. LH 공공도서관
# 목록과 같은 분류라 이름 토큰으로 다시 거르지 않는다.
PUBLIC_LIBRARY_LEAF = "공공도서관"

# 심평원 '종류=종합병원' 을 대신하는 근사 기준. 상급종합병원은 카카오에서
# '대학병원' 으로 분류되는데, LH 원장(전북 9건)에는 빠져 있다. 상급종합을
# 의료시설로 볼지는 LH 확인 대상이라 우선 함께 세고 note 로 밝힌다.
HOSPITAL_CATEGORIES: frozenset[str] = frozenset(
    {"종합병원", "상급종합병원", "대학병원"}
)

SCHOOL_TOKENS: tuple[str, ...] = ("초등학교", "중학교", "고등학교")

# 터미널 키워드 검색은 「G car zone 군산시외버스터미널 옆」(카셰어링)·「○○터미널
# 공중화장실」·「○○터미널 소화물취급소」처럼 터미널 이름을 빌린 부속·인접 시설까지
# 물어 온다(실측 2026-09-27 군산: 8건 중 진짜 터미널 2건). 분류 잎이 터미널로
# 끝나는 것만 대중교통수단 터미널로 본다 — 카카오는 「고속,시외버스터미널」 한
# 잎으로 시외·고속을 묶어 표기한다.
TERMINAL_LEAF_SUFFIX = "터미널"

# 고등교육기관으로 보는 카카오 「학교」 하위 분류.
#
# 「대학교」로 시작하는 것만 받으면 같은 성격의 학교가 원천 표기에 따라 갈린다.
# 실측(2026-09-11): 전주비전대학교는 「학교 > 대학교」, 전북과학대학교·군산간호
# 대학교·한국폴리텍대학 익산캠퍼스는 「학교 > 전문대학」으로 온다. 셋 다 고등
# 교육기관인데 앞의 하나만 잡히고 있었다. 판정이 분류 표기의 흔들림에 좌우되면
# 안 된다.
#
# 이 목록에 「전문대학」이 들어오면서 기능대학(한국폴리텍)도 함께 잡힌다.
# 기능대학(한국폴리텍)·전문대·사이버대는 대학교로 본다 — LH 확인 요청 7번
# (2026-09-11 회의) 「폴리텍 등 기능대학·전문대·사이버대 포함」, 2026-09-18 확정
# 적용. 「전문대학」 분류가 이를 담는다. 빠지는 학교가 보이면 이름 예외를 여기 한
# 줄로 붙인다 — 분류 문자열 매칭의 부작용으로 조용히 빠지는 상태로 두지 않는다.
UNIVERSITY_KINDS: tuple[str, ...] = ("대학교", "대학원", "전문대학")


class RawPlace(NamedTuple):
    """원천에서 받은 시설 한 곳. 거리 계산 전 단계."""

    name: str
    address: str
    category_name: str
    coordinates: Coordinates
    # 버스정류장(TAGO)만 채운다: "{cityCode}:{nodeId}". 운행주기 판정의 열쇠다.
    stop_ref: str = ""


class FeedResult(NamedTuple):
    """원천 호출 하나의 결과. 어떤 원천이 답했는지 함께 들고 다닌다."""

    places: tuple[RawPlace, ...]
    source_label: str
    # 버스정류장 피드만 채운다: 정류장명(공백 제거) → 운행주기 판정. 같은 이름의
    # 양방향 정류장은 하나로 합쳐(LH 시트도 「공수내다리(30497, 3050047)」처럼 한
    # 정류장으로 셌다) 15분당 도착 수가 큰 쪽을 대표로 둔다.
    headways: dict[str, StopHeadway] | None = None
    # 지정 원천이 장애로 실패해 대체 원천으로 떨어졌는지. 이런 결과는 캐시하지 않는다 —
    # 일시 장애 한 번이 캐시 수명(10분) 내내 낮은 점수로 굳는다.
    degraded: bool = False
    # 원천 장애·대체를 심사 결과에 경고로 올릴 문구. 비어 있으면 정상이다.
    # 조용히 대체 원천으로 넘어가지 않고, 담당자가 재심사를 판단할 수 있게 한다.
    alert: str = ""
    # 역 피드만 채운다: 원천이 역과 함께 준 출입구 (역명, 출구 이름, 좌표). VWorld 지하철역
    # 검색은 「○○역3번출입구」를 같은 질의로 돌려준다.
    station_doors: tuple[tuple[str, str, Coordinates], ...] = ()
    # 서버 사본(받은 지 하루 넘은 전량 목록)으로 답했으면 그 고지. 시설군 비고에 붙여
    # 실시간 답처럼 보이지 않게 한다(services/snapshot_store). 최신이면 빈 문자열.
    snapshot_note: str = ""


class MeasuredDoor(NamedTuple):
    """문·출구 후보 하나와 사업지 기준 거리(#7·#11)."""

    label: str
    coordinates: Coordinates
    distance_m: float
    selected: bool = False


class CollectedFacility(NamedTuple):
    """배점 근거로 화면에 보여줄 시설 한 곳.

    distance_m 은 아래 measurement_tier 로 잰 값 그대로다. 배점(distances_m)과
    화면 표기가 같은 계산에서 나오도록, 측정 기준을 여기 한 곳에 함께 담는다.
    """

    name: str
    address: str
    coordinates: Coordinates
    distance_m: float
    source_label: str
    # 시설 측 측정 기준(국장님 §3-2 3단):
    #   "front_door_parcel" | "front_door_point" | "site_boundary" | "coordinate"
    measurement_tier: str = "coordinate"
    # 국장님 표기 문구. 예: "(대지경계 ↔ 정문 필지경계 / 시설 정문)"
    measurement_label: str = ""
    # 정문 기준점 출처. 자동 채택이면 근사임이 드러나야 한다(§3-3).
    front_door_source: str = ""
    # 통필지 폴백·정문 미지정 등, 이 시설에 적용된 안전장치/폴백 고지.
    front_door_notice: str = ""
    # 문·출구 후보 전체(#7·#11). 기본은 가장 가까운 후보가 selected.
    front_door_candidates: tuple[MeasuredDoor, ...] = ()
    # 사업지↔시설 최단거리 선분(1차 HazardFacility 와 같은 형태, #5).
    nearest_boundary_point: Coordinates | None = None
    nearest_facility_point: Coordinates | None = None
    # 거리를 잰 시설 필지의 경계(site_boundary 일 때). 지도가 이 링을 그대로 칠한다.
    facility_ring: tuple[Coordinates, ...] = ()
    # 배점에 세는 시설인가. 버스정류장 운행주기 미달·미확인은 False 로 두고
    # 목록에는 남긴다(자료 부재를 조용히 감추지 않는다).
    counted: bool = True
    # 배점 인정/제외 사유 한 줄(버스정류장: 15분당 도착 수와 노선 배차).
    count_note: str = ""
    # LH 개별 맞춤(backend/data/lh_alignments.json)을 적용한 시설이면 그 한 줄.
    # 화면·엑셀에 「LH 개별 맞춤」으로 드러낸다. 비어 있으면 공공 API 값 그대로다.
    lh_alignment: str = ""


class GroupCollection(NamedTuple):
    """시설군 하나의 수집 결과."""

    key: str
    # "connected" | "substituted" | "missing"
    state: str
    note: str
    actual_source: str
    # 등급 판정에 쓰는 완전한 거리 목록(m). 상한 없이 전부 담는다.
    distances_m: tuple[float, ...]
    # 화면 표시용 근거 시설. MAX_HITS_PER_GROUP 개로 자른다.
    facilities: tuple[CollectedFacility, ...]
    # 시설군 차원의 정문 기준점 고지(대학교 전용). 정문 미지정·통필지 폴백 등을
    # 시설군 헤더에도 드러낸다. 조용히 좌표로 넘어가지 않기 위한 것이다(§3-3).
    front_door_notice: str = ""
    # 이 시설군을 채운 원천의 장애·대체 경고(FeedResult.alert 모음). 결과 상단 경고로 올린다.
    source_alert: str = ""


class GroupSpec(NamedTuple):
    """시설군 하나를 어떤 원천 조합으로 채우는지."""

    # 사용할 feed 이름들. 비어 있으면 원천 자체가 없다는 뜻이다.
    feeds: tuple[str, ...]
    # 조회에 성공했을 때의 수집 상태.
    state: str
    # 지정 원천과 다르거나 요건을 못 지킬 때의 고지. 화면에 그대로 나간다.
    note: str
    # 이 시설군이 카카오 원천에 의존하는지. 키가 없으면 통째로 missing 이다.
    kakao_backed: bool = True
    keep: Callable[[RawPlace], bool] | None = None
    # LOCALDATA 인허가 캐시(facility_store)로 채우는 시설군. 설정되면 카카오
    # feed 대신 인허가 원장을 조회한다. 원장이 미적재면 missing_note 로 남긴다.
    localdata_datasets: tuple[str, ...] = ()
    # 지정 원천이 미적재·미확보일 때 화면에 남길 사유. state 는 missing 이 된다.
    missing_note: str = ""


def _name_has(place: RawPlace, tokens: Sequence[str]) -> bool:
    haystack = f"{place.category_name} {place.name}"
    return any(token in haystack for token in tokens)


# 여객을 받지 않는 역. 「동익산화물역」처럼 이름이 「역」으로 끝나도 타고 내릴 수 없어
# 철도역으로 세지 않는다(내부망 앱 철도역·KTX역 목록에 없다).
_NON_PASSENGER_STATION_TOKENS: tuple[str, ...] = ("화물", "신호장", "조차장", "기지")


def _is_railway(place: RawPlace) -> bool:
    # "기차역" 키워드 검색은 역 앞 상가까지 물어 온다. 이름이 '역'으로 끝나는 것만 남긴다.
    return place.name.endswith("역") and not any(
        token in place.name for token in _NON_PASSENGER_STATION_TOKENS
    )


def _category_leaf(place: RawPlace) -> str:
    """카카오 category_name 의 마지막 조각. 이것이 실제 업종 분류다."""

    return place.category_name.split(">")[-1].strip()


def _is_general_hospital(place: RawPlace) -> bool:
    return _category_leaf(place) in HOSPITAL_CATEGORIES


def _is_park(place: RawPlace) -> bool:
    # 도시공원 표준데이터는 공원구분(소공원·묘지공원 등)과 관계없이 모두 공원이다(LH 기준).
    if park_source.is_standard_category(place.category_name):
        return True
    return _category_leaf(place) in PARK_CATEGORIES


def _is_terminal(place: RawPlace) -> bool:
    """분류 잎이 「…터미널」인 것만. 이름에 터미널이 들어간 카셰어링·화장실은 거른다."""

    return _category_leaf(place).endswith(TERMINAL_LEAF_SUFFIX)


# 공공시설로 세는 청사 — 도청과 시·군·구청 본청만(내부망 앱 JB_43 관공서 17건과 같다).
# 「전북도청출장소」·「종합상황실」·「○○사업소」처럼 청사 부속·산하기관은 이름 끝이
# 달라 걸러진다.
_MAIN_OFFICE_RE = re.compile(r"(도청|시청|군청|구청)$")
# 읍·면의 행정복지센터는 관공서 레이어(IF_0031)에 「○○면사무소」·「○○읍사무소」로
# 온다(전북 130곳). 동 주민센터·행정복지센터 114곳과 합치면 244곳으로 내부망 앱
# 행정복지센터 목록(JB_44, 242건)과 맞는다(2026-09-30 대조). 「관리사무소」 등은 거른다.
_TOWNSHIP_OFFICE_RE = re.compile(r"[가-힣]+[읍면]사무소$")
# 공공도서관이 아닌 도서관 — 내부망 앱 공공도서관 목록(JB_45)에 없다.
# 대학·학교 도서관도 공공도서관이 아니다(「전주대학교 도서관」).
_NON_PUBLIC_LIBRARY_TOKENS: tuple[str, ...] = (
    "작은도서관", "스마트도서관", "무인도서관", "대학", "학교",
)


def _is_public(place: RawPlace) -> bool:
    """공공시설 = 도청·시군구청 본청 + 공공도서관 + 행정복지센터(주민센터·읍면사무소).

    내부망 앱 표준 데이터셋의 공공시설은 관공서(JB_43, 도청·시군청 본청 17건)·
    공공도서관(JB_45)·행정복지센터(JB_44) 세 목록뿐이다(2026-09-28 대조). 관공서
    레이어·지도 공공기관 분류를 그대로 받으면 「전북도청출장소」·「종합상황실」·
    파출소·소방서까지 공공시설로 세어 결과가 갈린다. 그래서 세 종류만 남긴다.
    """

    name = place.name.replace(" ", "")
    leaf = _category_leaf(place)
    if leaf == PUBLIC_LIBRARY_LEAF:
        return True
    if leaf in LIBRARY_CATEGORIES:
        return leaf != "작은도서관" and not any(
            token in name for token in _NON_PUBLIC_LIBRARY_TOKENS
        )
    # 도서관 키워드로만 걸린 오탐(영어도서관학원·도서관 화장실)은 분류가 도서관이 아니다.
    if "도서관" in name:
        return False
    if "행정복지센터" in name or "주민센터" in name:
        return True
    if _TOWNSHIP_OFFICE_RE.fullmatch(name):
        return True
    return bool(_MAIN_OFFICE_RE.search(name))


def _school_filter(token: str) -> Callable[[RawPlace], bool]:
    # 표준데이터는 학교급을 분류로 준다(이름에 급이 없는 학교도 있다). 지도·레이어는 이름으로 가린다.
    return lambda place: token in place.name or _category_leaf(place) == token


def _is_university(place: RawPlace) -> bool:
    """대학교인지 판별한다. **이름이 아니라 분류를 본다.**

    이름에 「대학」이 들어가면 대학으로 보던 종전 방식은 대학이 아닌 것을 대거
    끌어들였다(2026-09-08 실측: 전주 3km 에서 300건. 「한국멜빈대학교 전국민안전
    인터넷신문사」·「서강대학교SLP 전주」·「투루카 전주대학교 신정문(휴인 크림빌)」
    같은 신문사·어학원·상가가 최근접으로 올라왔다).

    카카오 장소 분류는 대학교를 「교육,학문 > 학교 > 대학교」로 준다. 같은 반경에서
    이 분류로 거르면 4건이다. 분류가 비어 있는 원천은 채택하지 않는다 — 이름으로
    되돌아가면 같은 오탐이 다시 생긴다.

    받는 분류는 `UNIVERSITY_KINDS` 다. 전문대학이 빠져 있어 같은 성격의 학교가
    표기에 따라 갈리던 것을 2026-09-11 에 바로잡았다.
    """

    parts = [p.strip() for p in (place.category_name or "").split(">")]
    if "학교" not in parts:
        return False
    kind = parts[parts.index("학교") + 1] if len(parts) > parts.index("학교") + 1 else ""
    if kind in SCHOOL_TOKENS:
        return False  # 초·중·고는 별도 시설군이다
    if not any(kind.startswith(allowed) for allowed in UNIVERSITY_KINDS):
        return False
    # 원천 분류가 틀린 건이 있다(2026-09-08 실측: 「버거아이엔지 전주대점」이
    # 「교육,학문 > 학교 > 대학교」로 온다). 분류를 통과해도 이름에 「대학」이
    # 없으면 대학으로 보지 않는다. 분류와 이름을 함께 요구해 오탐을 막는다.
    return "대학" in place.name


GROUP_SPECS: dict[str, GroupSpec] = {
    # 지하철은 카카오(SW8 + 출구 번호별)가 1순위다 — VWorld 지하철역입구는 역마다 일부
    # 출구만 있다(실측 2026-09-30 건대입구역: 4번출입구 하나). 카카오가 막히면 VWorld 로
    # 대체하고 경고한다(_subways).
    "subway": GroupSpec(
        ("subway",), "connected", SUBWAY_KAKAO_NOTE, kakao_backed=False
    ),
    "railway": GroupSpec(
        ("railway",),
        "substituted",
        RAILWAY_VWORLD_NOTE,
        kakao_backed=False,
        keep=_is_railway,
    ),
    # 버스정류장은 TAGO 경유노선·배차간격으로 「운행주기 15분 이내」를 판정해
    # 인정 정류장만 센다(_apply_bus_headway). 배차를 확인할 수 없는 원천(서울시
    # 정류소·지도 검색)으로 채웠으면 아래 고지 그대로 근사(substituted)다.
    "bus_stop": GroupSpec(
        ("bus_stop",),
        "substituted",
        BUS_STOP_NO_HEADWAY_NOTE,
        kakao_backed=False,
    ),
    "terminal": GroupSpec(
        ("terminal",),
        "substituted",
        TERMINAL_VWORLD_NOTE,
        kakao_backed=False,
        keep=_is_terminal,
    ),
    "transfer": GroupSpec(
        ("transfer",), "substituted", TRANSFER_SUBSTITUTED_NOTE, kakao_backed=False
    ),
    # 심사표는 대규모점포 조회·전통시장통통 등재분만 인정한다. 카카오 '대형마트'
    # 분류에는 동네 마트(삼촌네마트·D마트)가, '백화점' 이름에는 가구점이 섞여
    # 들어와 근사가 성립하지 않는다(실측 확인). 대규모점포 원장(localdata)이
    # 2026-08-28 활용신청 승인돼 이제 지정 원천으로 직접 산정한다. 원장이
    # 미적재면 근사하지 않고 미적재로 남긴다.
    "retail": GroupSpec(
        # 전통시장 feed 는 대규모점포 원장에 더한다(_build_localdata_group). feed 로 두어
        # 원천 장애 경고가 _with_source_alert 로 올라간다.
        ("traditional_market",),
        "connected",
        RETAIL_CONNECTED_NOTE,
        kakao_backed=False,
        localdata_datasets=("large_scale_retail_stores",),
        missing_note=RETAIL_MISSING_NOTE,
    ),
    "hospital": GroupSpec(
        ("hospital",),
        "substituted",
        "심사표는 건강보험심사평가원 종류=종합병원 조회분만 인정합니다. "
        "현재는 지도 분류로 근사했습니다.",
        keep=_is_general_hospital,
    ),
    # 도시공원 표준데이터(park_client)가 답하면 connected 로 올린다(_build_group).
    # 표준데이터가 없거나 실패해 지도 검색으로 채웠으면 근사(substituted)다.
    "park": GroupSpec(
        ("park",),
        "substituted",
        "도시공원정보 표준데이터 대신 지도 검색으로 근사했습니다.",
        kakao_backed=False,
        keep=_is_park,
    ),
    # 문화시설은 LH 기준(인허가 공연장·박물관미술관·영화상영관)으로 센다. 카카오 CT1
    # feed 는 관리자 확장(culture_extended)이나 원장 미적재 대체일 때만 부른다
    # (screening/culture.py · _build_culture_group).
    "culture": GroupSpec(
        ("culture",),
        "connected",
        culture_source.CULTURE_STANDARD_NOTE,
        kakao_backed=False,
        missing_note=culture_source.CULTURE_MISSING_NOTE,
    ),
    # 공공·초중고는 생활안전지도 레이어(지정 원천)가 답하면 connected 로 올린다
    # (_build_group). 레이어가 없거나 실패해 지도 분류로 채웠으면 근사(substituted)다.
    "public": GroupSpec(
        ("public", "library"), "substituted", PUBLIC_KAKAO_NOTE, kakao_backed=False,
        keep=_is_public,
    ),
    "school_elementary": GroupSpec(
        ("school",), "substituted", SCHOOL_KAKAO_NOTE, kakao_backed=False,
        keep=_school_filter("초등학교"),
    ),
    "school_middle": GroupSpec(
        ("school",), "substituted", SCHOOL_KAKAO_NOTE, kakao_backed=False,
        keep=_school_filter("중학교"),
    ),
    "school_high": GroupSpec(
        ("school",), "substituted", SCHOOL_KAKAO_NOTE, kakao_backed=False,
        keep=_school_filter("고등학교"),
    ),
    # 대학교는 교육부 레이어(IF_0034)가 답하면 connected 로 올린다(_build_group).
    # 초·중·고 feed(school)는 섞지 않는다 — 그 feed 가 지도 학교 분류(SC4)로 대체되면
    # 지도사 사정에 따라 대학 목록이 달라진다.
    "university": GroupSpec(
        ("university",),
        "substituted",
        UNIVERSITY_KAKAO_NOTE,
        kakao_backed=False,
        keep=_is_university,
    ),
}


def _kakao_place(document: dict[str, Any]) -> RawPlace | None:
    try:
        lat = float(document.get("y"))
        lng = float(document.get("x"))
    except (TypeError, ValueError):
        return None
    return RawPlace(
        name=str(document.get("place_name") or "").strip(),
        address=str(
            document.get("road_address_name") or document.get("address_name") or ""
        ).strip(),
        category_name=str(document.get("category_name") or ""),
        coordinates=Coordinates(lat=lat, lng=lng),
    )


def _feed_enabled(feed: SafemapFacilityFeed | None) -> bool:
    return feed is not None and feed.enabled


def _snapshot_note(feed: object, label: str) -> str:
    """피드가 서버 사본(받은 지 하루 넘은 목록)으로 답했으면 시설군 비고에 붙일 고지."""

    notice = getattr(feed, "snapshot_notice", "")
    return f"{label} — {notice}." if isinstance(notice, str) and notice else ""


def _layer_places(
    rows: Sequence[SafemapFacility],
    category_of: Callable[[SafemapFacility], str],
) -> tuple[RawPlace, ...]:
    return tuple(
        RawPlace(row.name, row.address, category_of(row), row.coordinates)
        for row in rows
        if row.name
    )


def _tago_place(row: dict[str, Any]) -> RawPlace | None:
    lat = row.get("gpslati") or row.get("gpsLati")
    lng = row.get("gpslong") or row.get("gpsLong")
    try:
        coordinates = Coordinates(lat=float(lat), lng=float(lng))
    except (TypeError, ValueError):
        return None
    city_code = str(row.get("citycode") or row.get("cityCode") or "").strip()
    node_id = str(row.get("nodeid") or row.get("nodeId") or "").strip()
    return RawPlace(
        name=str(row.get("nodenm") or row.get("nodeNm") or "").strip(),
        address="",
        category_name="버스정류장",
        coordinates=coordinates,
        stop_ref=f"{city_code}:{node_id}" if city_code and node_id else "",
    )


def _stop_name_key(name: str) -> str:
    return name.replace(" ", "")


class AmenityCollector:
    """사업지 한 곳의 생활편의시설을 시설군별로 모은다."""

    def __init__(
        self,
        kakao: KakaoClient,
        tago: TagoClient | None = None,
        cache_ttl_seconds: int = 600,
        facility_store: FacilityStore | None = None,
        hospital_client: NcmcHospitalClient | None = None,
        hira_client: HiraHospitalClient | None = None,
        school_client: SchoolLocationClient | None = None,
        market_client: market_source.TraditionalMarketClient | None = None,
        park_client: CityParkClient | None = None,
        front_door_store: FrontDoorStore | None = None,
        cadastral_store: CadastralLocalStore | None = None,
        naver: NaverSearchClient | None = None,
        vworld: VWorldClient | None = None,
        transfer_client: TransferCenterClient | None = None,
        seoul_bus: SeoulBusStopClient | None = None,
        safemap_schools: SafemapFacilityFeed | None = None,
        safemap_universities: SafemapFacilityFeed | None = None,
        safemap_offices: SafemapFacilityFeed | None = None,
        safemap_hospitals: SafemapFacilityFeed | None = None,
        safemap_fire: SafemapFacilityFeed | None = None,
        dataset_gates: tuple[DatasetGate, ...] | None = None,
        alignments: Sequence[LhAlignment] | None = None,
        naver_geocode: NaverGeocodeClient | None = None,
        library_client: PublicLibraryClient | None = None,
        korail_client: KorailStationClient | None = None,
        station_exits: tuple[StationExit, ...] | None = None,
    ) -> None:
        self.kakao = kakao
        # 공공도서관 1순위 원천(전국도서관표준데이터 15013109). 미승인·장애면 VWorld.
        self.library_client = library_client
        # 철도역 1순위 원천(한국철도공사 역위치 정보 15127532). 미승인·장애면 VWorld.
        self.korail_client = korail_client
        # LH 데이터셋 역 출구점(전북). None 이면 저장소 CSV 를 읽는다.
        self.station_exits = load_station_exits() if station_exits is None else station_exits
        # LH 개별 맞춤 항목. None 이면 매 수집마다 등록부 파일(lh_alignments.json)을
        # 읽는다 — 항목을 고치면 재시작 없이 다음 심사부터 반영된다. 테스트는 직접 준다.
        self._fixed_alignments = None if alignments is None else tuple(alignments)
        # 표준 데이터셋 대학 정문 좌표(수기 지정 다음 순위). None 이면 저장소 CSV 를 읽는다.
        self.dataset_gates = (
            load_dataset_gates() if dataset_gates is None else dataset_gates
        )
        # 소방서·119안전센터(IF_0038). 관공서 레이어의 보강(같은 자리 40m 는 뺀다).
        self.safemap_fire = safemap_fire
        # 생활안전지도 시설 레이어(2026-09-17 승인). 초·중·고(IF_0035)·관공서(IF_0031)는
        # 지정 원천으로 지도 분류를 대체하고, 대학교(IF_0034)도 지정 원천이며, 종합병원
        # (IF_0022)은 국립중앙의료원 시도 조회의 전국본 폴백이다. 키가 없으면 미사용.
        self.safemap_schools = safemap_schools
        self.safemap_universities = safemap_universities
        self.safemap_offices = safemap_offices
        self.safemap_hospitals = safemap_hospitals
        # 서울 버스정류소(TAGO 가 서울을 제공하지 않아 따로 붙인다). 키가 없으면 미사용.
        self.seoul_bus = seoul_bus
        # 환승시설 지정 원천(환승센터 표준데이터). 활용신청 전(403)에는 지도 근사.
        self.transfer_client = transfer_client
        # 시설 경계(필지) 조회. 없거나 키가 없으면 로컬 지적도로, 그것도 없으면
        # 좌표로 폴백하고 그 사실을 시설마다 적는다.
        self.vworld = vworld
        self.tago = tago
        # 버스정류장 운행주기(15분) 판정기. TAGO 가 노선 정보를 주면 정류장마다
        # 15분당 도착 버스 수를 계산해 인정 정류장만 배점에 센다.
        self.headway_resolver: BusHeadwayResolver | None = (
            BusHeadwayResolver(tago)
            if tago is not None and hasattr(tago, "route_info")
            else None
        )
        self.cache_ttl_seconds = cache_ttl_seconds
        # 지정 원천을 인허가 캐시에서 직접 채우는 시설군(대규모점포 등)에 쓴다.
        # 없으면 해당 시설군은 missing_note 로 남긴다.
        self.facility_store = facility_store
        # 의료시설(종합병원) 지정 원천. 있으면 카카오 근사 대신 이걸 쓴다.
        self.hospital_client = hospital_client
        # 종합병원 1순위 원천(심사표 지정). 국립중앙의료원은 갱신이 늦어 뒤로 둔다.
        self.hira_client = hira_client
        # 초·중·고 1순위 원천(현재 위치). 생활안전지도 레이어는 이전 학교가 옛 부지다.
        self.school_client = school_client
        # 상업시설의 전통시장 원천(대규모점포 원장에 더한다). 없으면 대규모점포만 센다.
        self.market_client = market_client
        # 공원 1순위 원천(LH 생활권공원과 같은 도시공원 표준데이터). 실패 시 카카오 대체.
        self.park_client = park_client
        # 대학 정문 수기 지정 저장소. 없으면 자동 채택/좌표 폴백만 쓴다.
        self.front_door_store = front_door_store
        # 정문 필지경계를 조회할 로컬 지적도. 인덱스가 없으면 필지
        # 기준을 못 쓰고 좌표 기준으로 폴백한다(그 사실을 고지한다).
        self.cadastral_store = cadastral_store
        # 로컬 지적도는 읽기 전용 SQLite 연결 하나를 쓴다. 필지 조회를 동시에 돌려도
        # 로컬 폴백은 한 번에 하나씩만 들어가게 막는다.
        self._cadastral_lock = asyncio.Lock()
        # 학교 지번주소 → 필지(PNU). 주소별로 기억해 같은 학교를 다시 찾지 않는다.
        # 카카오가 막히면 네이버(NCP)·VWorld 로 주소 좌표를 찾아 그 자리 필지를 쓴다.
        self.address_parcels = AddressParcelResolver(
            kakao,
            self._parcel_by_pnu,
            LegalDongIndex.from_env(),
            locate=make_address_geocoder(kakao, vworld, naver_geocode),
            fetch_at=lambda point: self._facility_parcel(point, {}),
        )
        # 대학 정문 좌표를 확보할 지역검색. 표준 데이터셋이 대학 정문·역 출구를
        # 같은 API 로 확보했으므로(2026-09-08 회신) 기준점이 어긋나지 않는다.
        self.naver = naver
        # 시설명(정규화 base) → 문 후보 목록(label, 좌표). collect() 가 판정 전에
        # 채운다. 대학·종합병원을 같은 경로로 다룬다(#7·#8·#11).
        self._front_door_candidates: dict[str, tuple[tuple[str, Coordinates], ...]] = {}
        # 역명 → 출구 후보들(label, 좌표). 역은 대표점이 아니라 출입구에서 재야 한다
        # (LH 과업내용서 예외기준: 지하철=출입구). 출입구가 여럿이면 사업지에서
        # 가장 가까운 것을 쓴다 — 실제 접근 경로가 그렇고, 하나뿐이면 결과가 같다.
        # 키는 노선 꼬리를 뗀 역명(station_base). 환승역은 노선별 place 가 따로
        # 오지만 출구는 역 하나의 것이므로 함께 쓴다.
        self._station_doors: dict[str, tuple[tuple[str, Coordinates], ...]] = {}
        # 좌표·반경이 같은 재조회를 막는다. analysis 의 TTLCache 와 같은 방식이다.
        self._cache: dict[str, tuple[float, dict[str, GroupCollection]]] = {}

    # -- 공개 API ----------------------------------------------------------
    def feed_count(self) -> int:
        """한 번의 수집에서 조회하는 원천(feed) 수. 진행률 분모로 쓴다."""

        return len(self._feed_results(Coordinates(lat=0.0, lng=0.0), MAX_RADIUS_M))

    async def collect(
        self,
        rings: Sequence[Sequence[Coordinates]],
        center: Coordinates,
        radius_m: int = MAX_RADIUS_M,
        progress: FeedProgressCallback | None = None,
    ) -> dict[str, GroupCollection]:
        key = self._cache_key(rings, center, radius_m)
        cached = self._cache_get(key)
        if cached is not None:
            if progress is not None:
                for name in self._feed_results(center, radius_m):
                    await progress(name, FEED_LABELS.get(name, name), None, True)
            return cached

        feeds = self._feed_results(center, radius_m)
        names = tuple(feeds)

        async def tracked(name: str, coro: Awaitable[FeedResult]) -> FeedResult | BaseException:
            # 원천 하나가 끝나는 즉시 보고한다(gather 결과 순서는 그대로다).
            try:
                outcome: FeedResult | BaseException = await coro
            except BaseException as exc:  # noqa: BLE001 — 실패도 결과로 넘긴다(return_exceptions 와 같다)
                outcome = exc
            if progress is not None:
                ok = isinstance(outcome, FeedResult)
                await progress(
                    name, FEED_LABELS.get(name, name), len(outcome.places) if ok else None, ok
                )
            return outcome

        outcomes = await asyncio.gather(*(tracked(n, c) for n, c in feeds.items()))
        results: dict[str, FeedResult | BaseException] = dict(zip(names, outcomes))

        await self._prefetch_front_doors(results)
        await self._prefetch_station_entrances(results)

        valid_rings = [list(ring) for ring in rings if len(ring) >= 4]
        collections = {
            group.key: self._build_group(
                group.key, results, valid_rings, center, radius_m
            )
            for group in FACILITY_GROUPS
        }
        collections = await self._attach_boundaries(collections, valid_rings, center)
        collections = await self._apply_lh_alignments(
            collections, valid_rings, center, radius_m
        )
        collections = {
            key: _with_source_alert(collection, results)
            for key, collection in collections.items()
        }
        degraded = [
            name
            for name, outcome in results.items()
            if isinstance(outcome, BaseException)
            or (isinstance(outcome, FeedResult) and outcome.degraded)
        ] + [key for key, collection in collections.items() if collection.source_alert]
        if degraded:
            logger.warning("주변시설 원천 장애(%s): 이번 결과는 캐시하지 않음", ", ".join(degraded))
        else:
            self._cache_set(key, collections)
        return collections

    # -- 시설 경계 측정(초·중·고·공원·상업·문화·공공·버스정류장) ----------------
    async def _attach_boundaries(
        self,
        collections: dict[str, GroupCollection],
        rings: list[list[Coordinates]],
        center: Coordinates,
    ) -> dict[str, GroupCollection]:
        """BOUNDARY_GROUPS 의 최근접 시설을 필지 경계 기준으로 다시 잰다.

        시설마다 좌표를 품는 필지 1개를 조회해(VWorld → 로컬 지적도) 사업지
        대지경계 ↔ 시설 필지경계 최단거리로 바꾼다. 조회할 원천이 없으면 거리는
        그대로 두되, 시설마다 좌표 폴백 사실을 적는다.
        """

        can_fetch = self._boundary_source_ready()
        parcel_cache: dict[str, ParcelFeature | None] = {}
        updated: dict[str, GroupCollection] = {}
        for key, collection in collections.items():
            if key not in BOUNDARY_GROUPS or not collection.facilities:
                updated[key] = collection
                continue
            head = list(collection.facilities[:BOUNDARY_LOOKUP_PER_GROUP])
            tail = list(collection.facilities[BOUNDARY_LOOKUP_PER_GROUP:])
            # 도청·시청 같은 공공기관 청사는 최근접 몇 곳 밖이어도 필지를 조회한다. 주변에
            # 작은 주민센터·작은도서관이 많으면 청사가 늘 뒤로 밀려 부지 안 점으로 재였다
            # (2026-09-28 전주 효자동2가: 전라북도청이 공공시설 6번째 밖).
            institutional = [
                f for f in tail if is_public_office(f.name)
            ][:BOUNDARY_LOOKUP_PER_GROUP]
            head += institutional
            tail = [f for f in tail if f not in institutional]
            # 학교와 전통시장은 지번주소 필지가 먼저다(LH앱 location_basis=PNU).
            by_address = [
                can_fetch and (key in ADDRESS_PARCEL_GROUPS or _is_market(f)) for f in head
            ]
            address_parcels: dict[int, ParcelFeature | None] = (
                await self._prefetch_address_parcels(head, by_address)
                if any(by_address)
                else {}
            )
            if can_fetch:
                await self._prefetch_parcels(
                    [f for i, f in enumerate(head) if address_parcels.get(i) is None],
                    parcel_cache,
                )
            measured: list[CollectedFacility] = []
            # 경계로 잰(채택된) 시설 필지. 뒤쪽 시설이 같은 필지 안이면 조회 없이 재사용한다.
            accepted: list[ParcelFeature] = []
            for index, facility in enumerate(head):
                # 학교는 지번주소 필지가 우선이다(LH앱과 같은 필지). 못 찾으면 아래 좌표 경로.
                own = address_parcels.get(index)
                if own is not None:
                    result = _measure_to_parcel(facility, own, rings, center)
                    if result.measurement_tier == "site_boundary":
                        accepted.append(own)
                        label = (
                            market_source.MARKET_ADDRESS_PARCEL_NOTICE
                            if _is_market(facility)
                            else ADDRESS_PARCEL_NOTICES.get(key, ADDRESS_PARCEL_NOTICE)
                        )
                        result = result._replace(
                            front_door_notice=f"{label} · {result.front_door_notice}"
                        )
                        measured.append(result)
                        continue
                shared = _containing_parcel(accepted, facility.coordinates)
                if shared is not None:
                    measured.append(_measure_to_parcel(facility, shared, rings, center))
                    continue
                parcel = (
                    await self._facility_parcel(facility.coordinates, parcel_cache)
                    if can_fetch
                    else None
                )
                # 필지가 아무리 넓어도 경계로 잰다 — 대형 필지는 끝점(경계선) 기준이다
                # (8-14 회의록 §2 · LH 09/22 결정 2). 통필지 상한(5만㎡)은 2026-09-28 사용자
                # 결정으로 없앴다: 전북도청 101,019㎡ 필지가 좌표로 폴백돼 선이 부지 안
                # 점까지 들어갔다. 대학교는 이 경로가 아니라 정문 점으로 잰다.
                result = _measure_to_parcel(facility, parcel, rings, center)
                if parcel is not None and result.measurement_tier == "site_boundary":
                    accepted.append(parcel)
                measured.append(result)
            # 최근접 몇 곳 밖이어도 이미 경계로 잰 필지 안의 시설(도청 안 도서관·출장소
            # 등)은 같은 필지 경계로 잰다. 같은 부지인데 하나는 경계, 하나는 부지 안 점으로
            # 재면 선과 거리가 어긋난다.
            for facility in tail:
                parcel = _containing_parcel(accepted, facility.coordinates)
                measured.append(
                    _measure_to_parcel(facility, parcel, rings, center)
                    if parcel is not None
                    else facility
                )
            facilities = sorted(measured, key=lambda f: f.distance_m)
            shown = len(collection.facilities)
            distances = sorted(
                [f.distance_m for f in facilities]
                + list(collection.distances_m[shown:])
            )
            updated[key] = collection._replace(
                facilities=tuple(facilities), distances_m=tuple(distances)
            )
        return updated

    async def _prefetch_parcels(
        self,
        facilities: Sequence[CollectedFacility],
        cache: dict[str, ParcelFeature | None],
    ) -> None:
        """시설 필지를 동시에 받아 캐시에 채운다. 뒤의 측정 루프는 캐시만 읽는다."""

        semaphore = asyncio.Semaphore(BOUNDARY_LOOKUP_CONCURRENCY)

        async def fetch(point: Coordinates) -> None:
            async with semaphore:
                await self._facility_parcel(point, cache)

        await asyncio.gather(*(fetch(f.coordinates) for f in facilities))

    async def _prefetch_address_parcels(
        self,
        facilities: Sequence[CollectedFacility],
        wanted: Sequence[bool] | None = None,
    ) -> dict[int, ParcelFeature | None]:
        """시설 지번주소 필지를 동시에 찾는다(순번 → 필지, 못 찾으면 None).

        wanted 가 주어지면 True 인 시설만 찾는다(상업시설은 전통시장만 주소 필지).
        """

        semaphore = asyncio.Semaphore(BOUNDARY_LOOKUP_CONCURRENCY)
        mask = list(wanted) if wanted is not None else [True] * len(facilities)

        async def fetch(facility: CollectedFacility, want: bool) -> ParcelFeature | None:
            if not want:
                return None
            async with semaphore:
                try:
                    return await self.address_parcels.parcel_for(
                        facility.address, facility.coordinates
                    )
                except Exception:
                    return None

        found = await asyncio.gather(*(fetch(f, w) for f, w in zip(facilities, mask)))
        return dict(enumerate(found))

    async def _parcel_by_pnu(self, pnu: str) -> ParcelFeature | None:
        """PNU 로 필지를 받는다. VWorld 우선, 로컬 지적도 폴백(_facility_parcel 과 같은 순서)."""

        if self.vworld is not None and self.vworld.enabled:
            try:
                parcel = await self.vworld.parcel_by_pnu(pnu)
            except Exception:
                parcel = None
            if parcel is not None:
                return parcel
        if not self._cadastral_ready():
            return None
        try:
            async with self._cadastral_lock:
                local = await asyncio.to_thread(self.cadastral_store.parcel_by_pnu, pnu)
        except Exception:
            return None
        if local is None:
            return None
        return ParcelFeature(
            pnu=local.pnu,
            address=local.address,
            jibun=local.jibun,
            ring=list(local.ring),
            area_m2=local.area_m2,
        )

    def _boundary_source_ready(self) -> bool:
        if self.vworld is not None and self.vworld.enabled:
            return True
        return self._cadastral_ready()

    def _cadastral_ready(self) -> bool:
        """로컬 지적도 인덱스가 실제 조회 가능한지(hazard_review 와 같은 판정)."""

        store = self.cadastral_store
        if store is None:
            return False
        try:
            return bool(store.status().available)
        except Exception:
            return False

    async def _facility_parcel(
        self,
        coordinates: Coordinates,
        cache: dict[str, ParcelFeature | None],
    ) -> ParcelFeature | None:
        """좌표를 품는 필지. VWorld 우선, 로컬 지적도 폴백. 실패는 None."""

        key = f"{coordinates.lat:.6f}:{coordinates.lng:.6f}"
        if key in cache:
            return cache[key]
        parcel: ParcelFeature | None = None
        if self.vworld is not None and self.vworld.enabled:
            try:
                parcel = await self.vworld.parcel_at(coordinates.lat, coordinates.lng)
            except Exception:  # VWorldAPIError·네트워크 — 로컬 폴백으로 넘어간다
                parcel = None
        if parcel is None and self._cadastral_ready():
            try:
                async with self._cadastral_lock:
                    local = await asyncio.to_thread(
                        self.cadastral_store.parcel_at, coordinates.lat, coordinates.lng
                    )
            except Exception:
                local = None
            if local is not None:
                parcel = ParcelFeature(
                    pnu=local.pnu,
                    address=local.address,
                    jibun=local.jibun,
                    ring=list(local.ring),
                    area_m2=local.area_m2,
                )
        if parcel is not None and len(parcel.ring) < 4:
            parcel = None
        cache[key] = parcel
        return parcel

    async def _prefetch_front_doors(
        self,
        results: dict[str, "FeedResult | BaseException"],
    ) -> None:
        """이번 반경에 잡힌 대학·종합병원의 문 후보를 지역검색으로 미리 확보한다.

        정문 기준은 LH 과업내용서의 예외기준(대학교=정문)과 2026-09-11 결정(#8,
        대형 필지 시설=정문 기본)이다. 판정 경로는 동기라 여기서 미리 받아 둔다.
        「{시설명} 정문」한 번 질의로 정문·후문·출입구 등 문 후보를 전부 모은다
        (시설당 네이버 호출 1회). 같은 시설을 여러 사업지에서 다시 묻지 않도록
        프로세스 안에 남긴다. 종합병원 이름도 함께 받아(네이버 호출량은 이번 반경의
        종합병원 수만큼만 는다) 대학과 동일한 3단 측정 경로를 타게 한다.
        """

        use_naver = self.naver is not None and self.naver.enabled
        use_kakao = self.kakao.enabled
        if not (use_naver or use_kakao):
            return
        # (base_name, exclude_tokens, 시설 좌표) 목록. 대학은 정문 공유 단위
        # (university_base)로, 종합병원은 시설명 그대로 묻는다. 종합병원 정문 후보는
        # '병원' 토큰을 배제하지 않는다(HOSPITAL_EXCLUDE_TOKENS).
        targets: list[tuple[str, tuple[str, ...], Coordinates]] = []
        seen: set[str] = set()

        def _add(base: str, exclude_tokens: tuple[str, ...], at: Coordinates) -> None:
            key = normalize_key(base)
            if key and key not in self._front_door_candidates and key not in seen:
                seen.add(key)
                targets.append((base, exclude_tokens, at))

        result = results.get("university")
        if isinstance(result, FeedResult):
            for place in result.places:
                if _is_university(place):
                    _add(
                        university_base(place.name),
                        _AUTO_EXCLUDE_TOKENS,
                        place.coordinates,
                    )
        # 종합병원은 09/22 결정으로 필지 경계 기준이라 정문을 묻지 않는다.
        if not targets:
            return

        async def one(
            base: str, exclude_tokens: tuple[str, ...], at: Coordinates
        ) -> None:
            # 문 후보는 네이버 지역검색 하나로 정한다(LH 표준 데이터셋이 대학 정문을 같은
            # API 로 확보했다). 카카오 「입출구」는 네이버가 없거나 실패했을 때만 대체로
            # 묻는다 — 둘을 섞으면 카카오 한도·장애에 따라 가장 가까운 문이 바뀐다.
            doors: list[tuple[str, Coordinates]] = []
            naver_ok = False
            if use_naver:
                try:
                    places = await self.naver.local(f"{base} 정문")
                    naver_ok = True
                except Exception:
                    places = []
                doors.extend(
                    collect_door_candidates(
                        base,
                        places,
                        door_tokens=FACILITY_DOOR_TOKENS,
                        exclude_tokens=exclude_tokens,
                    )
                )
            if not naver_ok and use_kakao:
                doors.extend(await self._kakao_gates(base, at))
            merged = _dedupe_doors(doors)
            if merged:
                self._front_door_candidates[normalize_key(base)] = tuple(merged)

        await asyncio.gather(
            *(one(base, excl, at) for base, excl, at in targets),
            return_exceptions=True,
        )

    async def _kakao_gates(
        self, base: str, at: Coordinates
    ) -> list[tuple[str, Coordinates]]:
        """카카오 「입출구」 POI 중 이 시설의 문(정문·후문·○○문·입구)을 모은다."""

        tokens = _university_tokens(base) or ("".join(base.split()),)
        doors: list[tuple[str, Coordinates]] = []
        for suffix in GATE_QUERY_SUFFIXES:
            try:
                documents = await self.kakao.search_keyword(
                    f"{base} {suffix}", at.lat, at.lng, GATE_SEARCH_RADIUS_M, max_pages=1
                )
            except Exception:
                break  # 조회 장애 — 지금까지 모은 것으로 간다
            for document in documents:
                place = _kakao_place(document)
                if place is None or GATE_CATEGORY not in place.category_name:
                    continue
                squashed = "".join(place.name.split())
                if not any(token in squashed for token in tokens):
                    continue
                doors.append((place.name.strip(), place.coordinates))
        return doors

    async def _prefetch_station_entrances(
        self,
        results: dict[str, "FeedResult | BaseException"],
    ) -> None:
        """이번 반경에 잡힌 역의 출입구 좌표를 지역검색으로 미리 확보한다.

        표준 데이터셋도 같은 API 로 「○○역출구」를 수집했다(2026-09-08 회신).
        역 이름 그대로 「출구」를 붙여 찾고, 그 역 이름을 포함하지 않는 결과
        (인근 주차장·건물 출구)는 버린다.
        """

        use_kakao = self.kakao.enabled
        use_naver = self.naver is not None and self.naver.enabled
        stations: dict[str, Coordinates] = {}
        # 원천이 역과 함께 준 출입구(VWorld 지하철역입구). 지도 출구 검색에 더한다.
        feed_doors: dict[str, list[tuple[str, Coordinates]]] = {}
        for feed in ("railway", "subway"):
            result = results.get(feed)
            if not isinstance(result, FeedResult):
                continue
            for base, label, point in result.station_doors:
                feed_doors.setdefault(base, []).append((label, point))
            for place in result.places:
                base = station_base(place.name)
                if not base or base in self._station_doors or base in stations:
                    continue
                # LH 데이터셋이 모은 출구점이 있는 역(전북 철도역)은 그 점에서만 잰다 —
                # 지도 회사 출구 검색 결과에 따라 거리가 달라지지 않게 한다.
                dataset = dataset_exits_for(base, place.coordinates, self.station_exits)
                if dataset:
                    self._station_doors[base] = tuple(_dedupe_doors(dataset))
                    continue
                stations[base] = place.coordinates

        async def one(base: str, at: Coordinates) -> None:
            doors: list[tuple[str, Coordinates]] = list(feed_doors.get(base, ()))
            if use_kakao:
                doors.extend(await self._kakao_station_exits(base, at))
            if use_naver:
                doors.extend(await self._naver_station_exits(base))
            merged = _dedupe_doors(doors)
            if merged:
                self._station_doors[base] = tuple(merged)

        await asyncio.gather(
            *(one(b, at) for b, at in stations.items()), return_exceptions=True
        )

    async def _kakao_station_exits(
        self, base: str, at: Coordinates
    ) -> list[tuple[str, Coordinates]]:
        """「{역명} N번출구」를 번호 순으로 물어 「지하철출구」 분류 결과만 모은다."""

        squashed_base = "".join(base.split())
        doors: list[tuple[str, Coordinates]] = []
        misses = 0
        for number in range(1, STATION_EXIT_MAX_NUMBER + 1):
            try:
                documents = await self.kakao.search_keyword(
                    f"{base} {number}번출구",
                    at.lat,
                    at.lng,
                    STATION_EXIT_SEARCH_RADIUS_M,
                    max_pages=1,
                )
            except Exception:
                break  # 조회 장애 — 지금까지 모은 출구(또는 네이버 보충)로 간다
            found = False
            for document in documents:
                place = _kakao_place(document)
                if place is None:
                    continue
                if STATION_EXIT_CATEGORY not in place.category_name:
                    continue
                if squashed_base not in "".join(place.name.split()):
                    continue
                doors.append((place.name.strip(), place.coordinates))
                found = True
            misses = 0 if found else misses + 1
            if misses >= STATION_EXIT_MISS_STREAK:
                break
        return doors

    async def _naver_station_exits(self, base: str) -> list[tuple[str, Coordinates]]:
        """네이버 「{역명} 출구」·「{역명}출구」(질의당 최대 5건·무작위). 그 역 출구만 남긴다.

        철도역은 카카오에 출구 POI 가 없어 이 경로가 유일한 출구 원천이다. 5건 무작위라
        한 번에 빠질 수 있어 붙여 쓴 질의로 한 번 더 묻는다(표준 데이터셋 이름이
        「익산역출구」 꼴이다). 역 앞 주차장·오피스텔 출구는 station_exit_places 가 버린다.
        """

        places: list[Any] = []
        for query in (f"{base} 출구", f"{''.join(base.split())}출구"):
            try:
                places.extend(await self.naver.local(query))
            except Exception:
                continue
        return station_exit_places(base, places)

    def _with_station_entrance(
        self,
        place: RawPlace,
        center: Coordinates,
    ) -> RawPlace:
        """역 대표점을 가장 가까운 출입구 좌표로 바꾼다. 없으면 그대로 둔다."""

        doors = self._station_doors.get(station_base(place.name))
        if not doors:
            return place
        _, nearest = min(doors, key=lambda item: haversine_meters(center, item[1]))
        return place._replace(coordinates=nearest)

    def _station_door_candidates(
        self,
        station_name: str,
        rings: list[list[Coordinates]],
        center: Coordinates,
    ) -> tuple[MeasuredDoor, ...]:
        """역 출구 후보를 사업지 기준 거리로 매긴다(#11). 기본 selected = 최근접."""

        doors = self._station_doors.get(station_base(station_name))
        if not doors:
            return ()
        measured = [
            MeasuredDoor(label, coords, _distance_m(coords, rings, center))
            for label, coords in doors
        ]
        nearest = min(measured, key=lambda d: d.distance_m)
        return tuple(
            door._replace(selected=(door.coordinates == nearest.coordinates))
            for door in measured
        )

    def _measure_station_like(
        self,
        place: RawPlace,
        rings: list[list[Coordinates]],
        center: Coordinates,
        source_label: str,
        *,
        is_station: bool,
        designatable: bool,
    ) -> CollectedFacility:
        """역·터미널·환승시설 한 곳을 잰다. 담당자 수기 기준점 지정을 우선한다(#11).

        우선순위 — 담당자 수기 지정(기준점 필지/좌표) > 기존 동작(역은 최근접 출구,
        그 외 시설 좌표). 수기 지정이 있으면 measurement_tier·front_door_source 에
        「수기 지정」이 드러나고, 출구 후보가 있으면 지정 좌표와 일치하는 출구를
        selected 로 표시한다(없으면 측정 자체는 지정 기준점으로 한다).
        """

        # 출구 후보 전체(#11). 역·지하철만 채운다.
        candidates = (
            self._station_door_candidates(place.name, rings, center)
            if is_station
            else ()
        )

        manual = (
            self.front_door_store.get(place.name)
            if designatable and self.front_door_store is not None
            else None
        )
        if manual is not None:
            site_token = "대지경계" if rings else "주소점"

            def build(
                distance: float,
                tier: str,
                label: str,
                front_door_source: str,
                notice: str,
                facility_point: Coordinates | None,
                selected_point: Coordinates | None = None,
            ) -> CollectedFacility:
                cands = tuple(
                    c._replace(
                        selected=(
                            selected_point is not None
                            and c.coordinates == selected_point
                        )
                    )
                    for c in candidates
                )
                return CollectedFacility(
                    name=place.name,
                    address=place.address,
                    coordinates=place.coordinates,
                    distance_m=distance,
                    source_label=source_label,
                    measurement_tier=tier,
                    measurement_label=label,
                    front_door_source=front_door_source,
                    front_door_notice=notice,
                    front_door_candidates=cands,
                    nearest_facility_point=facility_point,
                    nearest_boundary_point=(
                        _anchor_for(facility_point, rings) if facility_point else None
                    ),
                )

            return self._measure_from_ref(
                place, manual, rings, center, site_token, build
            )

        return CollectedFacility(
            name=place.name,
            address=place.address,
            coordinates=place.coordinates,
            distance_m=_distance_m(place.coordinates, rings, center),
            source_label=source_label,
            # 역·지하철은 출구 후보 전체를 싣는다(#11). 기본은 최근접 출구.
            front_door_candidates=candidates,
            # 최단거리선(#5): 시설 기준점은 측정에 쓴 좌표(역은 최근접 출구).
            nearest_facility_point=place.coordinates,
            nearest_boundary_point=_anchor_for(place.coordinates, rings),
        )

    # -- 원천 호출 ---------------------------------------------------------
    def _feed_results(
        self,
        center: Coordinates,
        radius_m: int,
    ) -> dict[str, Any]:
        """feed 이름 → 코루틴. 전부 한 번에 gather 한다."""

        feeds: dict[str, Any] = {
            "bus_stop": self._bus_stops(center, radius_m),
            "transfer": self._transfer_centers(center, radius_m),
        }
        # 지정 원천 레이어가 있는 시설군은 카카오 키와 무관하게 조회한다(실패 시 지도 폴백).
        school_standard_ready = self.school_client is not None and self.school_client.enabled
        if school_standard_ready or _feed_enabled(self.safemap_schools):
            feeds["school"] = self._schools(center, radius_m)
        if _feed_enabled(self.safemap_offices):
            feeds["public"] = self._layer_offices(center, radius_m)
        if _feed_enabled(self.safemap_universities):
            feeds["university"] = self._universities(center, radius_m)
        if self.market_client is not None and self.market_client.enabled:
            feeds["traditional_market"] = self._traditional_markets(center, radius_m)
        if self.park_client is not None and self.park_client.enabled:
            feeds["park"] = self._parks(center, radius_m)
        # 역·터미널·도서관은 공공 API → VWorld 장소검색 → 카카오(마지막 대체) 순으로 스스로
        # 원천을 고른다. 아래 카카오 분류·키워드 목록은 이미 맡은 이름을 건너뛴다.
        feeds["subway"] = self._subways(center, radius_m)
        feeds["railway"] = self._railways(center, radius_m)
        feeds["terminal"] = self._terminals(center, radius_m)
        feeds["library"] = self._libraries(center, radius_m)
        if not self.kakao.enabled:
            # 키가 없으면 호출 자체를 만들지 않는다. 상태는 missing 으로 내려간다.
            return feeds

        # 상업시설(대형마트 MT1·「백화점」·「전통시장」)은 대규모점포 원장과 전통시장
        # 표준데이터로만 세므로 카카오를 부르지 않는다(쓰는 시설군이 없어 한도만 쓴다).
        category = {
            "subway": "SW8",
            "hospital": "HP8",
            "culture": "CT1",
            "public": "PO3",
            "school": "SC4",
        }
        keyword = {
            "railway": "기차역",
            "terminal": "버스터미널",
            "park": "공원",
            "library": "도서관",
            "university": "대학교",
        }
        # 의료시설은 국립중앙의료원 원장(시도 조회)이나 그 전국본 레이어가 있으면
        # 카카오 HP8 근사 대신 그걸 쓴다. 코루틴을 만들었다가 덮으면 await 되지 않아
        # 경고가 나므로 지정 원천이 있는 시설군은 미리 뺀다.
        use_designated_hospital = (
            (self.hira_client is not None and self.hira_client.enabled)
            or (self.hospital_client is not None and self.hospital_client.enabled)
            or _feed_enabled(self.safemap_hospitals)
        )
        for name, code in category.items():
            if name == "hospital" and use_designated_hospital:
                continue
            # 문화시설 CT1 은 LH 기준 밖이라 확장·대체일 때만 부른다.
            if name == "culture" and not self._culture_plan().use_kakao:
                continue
            if name in feeds:
                continue
            feeds[name] = self._kakao_category(code, center, radius_m)
        for name, query in keyword.items():
            if name in feeds:
                continue  # 지정 원천이 이미 맡았다(공원 표준데이터·대학교 레이어).
            feeds[name] = self._kakao_keyword(query, center, radius_m)
        if use_designated_hospital:
            feeds["hospital"] = self._designated_hospitals(center, radius_m)
        return feeds

    async def _schools(self, center: Coordinates, radius_m: int) -> FeedResult:
        """초·중·고: 학교 위치 표준데이터 → 실패 시 생활안전지도 레이어 → 지도 분류.

        생활안전지도 레이어는 이전한 학교를 옛 부지로 갖고 있어(전라중학교) 표준데이터가
        응답하지 않을 때만 쓰고, 그 사실을 원천 장애 경고로 올린다.
        """

        if self.school_client is not None and self.school_client.enabled:
            try:
                records = await self.school_client.schools_around(center, radius_m)
                return FeedResult(
                    tuple(
                        RawPlace(r.name, r.address, f"교육,학문 > 학교 > {r.kind}", r.coordinates)
                        for r in records
                    ),
                    SCHOOL_STANDARD_SOURCE,
                    snapshot_note=_snapshot_note(
                        self.school_client, "전국초중등학교위치표준데이터"
                    ),
                )
            except Exception:
                logger.warning("학교 위치 표준데이터 실패: 생활안전지도 레이어로 대체", exc_info=True)
                standard_alert = (
                    "전국초중등학교위치표준데이터가 응답하지 않아 다른 원천으로 학교를 "
                    "찾았습니다. 이전한 학교가 옛 위치로 잡혔을 수 있습니다."
                )
        else:
            standard_alert = ""
        if _feed_enabled(self.safemap_schools):
            result = await self._layer_schools(center, radius_m)
        else:
            if not self.kakao.enabled:
                raise RuntimeError("학교 위치 원천이 모두 응답하지 않았습니다.")
            result = await self._kakao_category("SC4", center, radius_m)
        if not standard_alert:
            return result
        alert = " ".join(filter(None, [standard_alert, result.alert]))
        return result._replace(degraded=True, alert=alert)

    async def _traditional_markets(self, center: Coordinates, radius_m: int) -> FeedResult:
        """전통시장(상업시설). 실패하면 빈 결과 + 경고 — 대규모점포만으로 세되 조용히 넘기지 않는다."""

        assert self.market_client is not None
        try:
            records = await self.market_client.markets_around(center, radius_m)
        except Exception:
            logger.warning("전통시장 표준데이터 실패: 대규모점포만 반영", exc_info=True)
            return FeedResult(
                (),
                market_source.MARKET_SOURCE,
                degraded=True,
                alert=market_source.MARKET_OUTAGE_ALERT,
            )
        return FeedResult(
            tuple(
                RawPlace(r.name, r.address, f"전통시장 > {r.kind}", r.coordinates)
                for r in records
            ),
            market_source.MARKET_SOURCE,
            snapshot_note=_snapshot_note(self.market_client, "전국전통시장표준데이터"),
        )

    async def _parks(self, center: Coordinates, radius_m: int) -> FeedResult:
        """공원: 도시공원 표준데이터 → 실패 시 카카오 「공원」 검색(경고).

        관리자 옵션(park_kakao_supplement)을 켜면 표준데이터에 없는 카카오 공원을 더한다.
        """

        assert self.park_client is not None
        try:
            records = await self.park_client.parks_around(center, radius_m)
        except Exception:
            logger.warning("도시공원 표준데이터 실패: 지도 검색으로 대체", exc_info=True)
            if not self.kakao.enabled:
                raise
            fallback = await self._kakao_keyword("공원", center, radius_m)
            return fallback._replace(degraded=True, alert=park_source.PARK_STANDARD_ALERT)
        places = tuple(
            RawPlace(r.name, r.address, park_source.standard_category(r.kind), r.coordinates)
            for r in records
        )
        park_snapshot_note = _snapshot_note(self.park_client, "전국도시공원정보표준데이터")
        standard = FeedResult(
            places, park_source.PARK_STANDARD_SOURCE, snapshot_note=park_snapshot_note
        )
        if not (
            self.kakao.enabled and option_enabled(park_source.PARK_SUPPLEMENT_OPTION)
        ):
            return standard
        try:
            kakao = await self._kakao_keyword("공원", center, radius_m)
        except Exception:
            logger.warning("카카오 공원 보강 실패: 표준데이터만 사용", exc_info=True)
            return standard._replace(degraded=True, alert=park_source.PARK_SUPPLEMENT_ALERT)
        known = [(p.name, p.coordinates) for p in places]
        extra = tuple(
            p
            for p in kakao.places
            if _is_park(p) and not park_source.is_duplicate(p.name, p.coordinates, known)
        )
        return FeedResult(
            places + extra,
            f"{park_source.PARK_STANDARD_SOURCE} + {KAKAO_PLACE_SOURCE}",
            snapshot_note=park_snapshot_note,
        )

    # -- 생활안전지도 시설 레이어 ----------------------------------------------
    async def _layer_schools(self, center: Coordinates, radius_m: int) -> FeedResult:
        """초·중·고(IF_0035). 실패하면 지도 학교 분류(SC4)로 폴백하고 근사로 남긴다."""

        assert self.safemap_schools is not None
        try:
            rows = await self.safemap_schools.facilities_around(center, radius_m)
            return FeedResult(
                _layer_places(rows, lambda r: f"교육,학문 > 학교 > {r.kind}"),
                SAFEMAP_SCHOOL_SOURCE,
                snapshot_note=_snapshot_note(self.safemap_schools, "생활안전지도 학교 레이어"),
            )
        except Exception:
            logger.warning("생활안전지도 학교 레이어 실패: 지도 분류로 대체", exc_info=True)
            if not self.kakao.enabled:
                raise
            fallback = await self._kakao_category("SC4", center, radius_m)
            return fallback._replace(
                degraded=True, alert=_layer_alert("학교(초·중·고)", "카카오 지도 학교 분류")
            )

    async def _layer_offices(self, center: Coordinates, radius_m: int) -> FeedResult:
        """관공서(IF_0031) — 공공시설의 본청·행정복지센터 원천.

        전북 IF_0031 에는 도서관 행이 없다(2026-09-30 실측 700곳: 우체국·파출소·
        주민센터·읍면사무소·본청 등). 공공도서관은 library feed 가 맡는다. 다른 시도에서
        도서관 행이 오면 도서관 분류로 두어 _is_public 이 같은 규칙으로 가린다.
        """

        assert self.safemap_offices is not None
        try:
            rows = await self.safemap_offices.facilities_around(center, radius_m)
        except Exception:
            logger.warning("생활안전지도 관공서 레이어 실패: 지도 분류로 대체", exc_info=True)
            if not self.kakao.enabled:
                raise
            fallback = await self._kakao_category("PO3", center, radius_m)
            return fallback._replace(
                degraded=True, alert=_layer_alert("관공서", "카카오 지도 공공기관 분류")
            )
        places = list(
            _layer_places(
                rows,
                lambda r: ("문화,예술 > 도서관" if "도서관" in r.name else "공공기관 > 관공서"),
            )
        )
        # 소방서(IF_0038)는 보강하지 않는다 — 내부망 앱 공공시설에 소방서가 없다.
        return FeedResult(
            tuple(places),
            SAFEMAP_OFFICE_SOURCE,
            snapshot_note=_snapshot_note(self.safemap_offices, "생활안전지도 관공서 레이어"),
        )

    async def _universities(self, center: Coordinates, radius_m: int) -> FeedResult:
        """대학교 지정 원천: 교육부 대학교 위치(생활안전지도 IF_0034).

        LH 데이터셋 대학교 목록(대학알리미)과 같은 교육부 원천이라 전북 71곳이 LH 와
        같은 이름·주소로 온다(2026-09-30 대조). 지도 검색(카카오 「대학교」 키워드)은
        지도사 사정에 따라 결과가 갈려 판정 원천으로 쓰지 않고, 레이어가 실패했을 때만
        대체로 부르며 그 사실을 경고로 올린다(원천 장애는 조용히 넘기지 않는다).
        기준점은 이 좌표가 아니라 정문이다(_measure_with_front_door).
        """

        assert self.safemap_universities is not None
        try:
            rows = await self.safemap_universities.facilities_around(
                center, radius_m + UNIVERSITY_LAYER_MARGIN_M
            )
        except Exception:
            logger.warning("생활안전지도 대학교 레이어 실패: 지도 검색으로 대체", exc_info=True)
            if not self.kakao.enabled:
                raise
            fallback = await self._kakao_keyword("대학교", center, radius_m)
            return fallback._replace(
                degraded=True,
                alert=_layer_alert("대학교(IF_0034)", "카카오 지도 「대학교」 검색"),
            )
        return FeedResult(
            _layer_places(rows, lambda r: UNIVERSITY_LAYER_CATEGORY),
            SAFEMAP_UNIVERSITY_SOURCE,
            snapshot_note=_snapshot_note(
                self.safemap_universities, "생활안전지도 대학교 레이어"
            ),
        )

    async def _designated_hospitals(
        self, center: Coordinates, radius_m: int
    ) -> FeedResult:
        """종합병원 지정 원천: 심평원 반경 조회 → 국립중앙의료원 시도 조회 → 전국본 레이어.

        심평원이 심사표 지정 원천이고 LH 데이터셋과 같은 목록이다. 국립중앙의료원은
        갱신이 늦어(「정읍한국병원」 누락) 심평원이 응답하지 않을 때만 쓰고, 그 사실을
        원천 장애 경고로 올린다.
        """

        hira_ready = self.hira_client is not None and self.hira_client.enabled
        ncmc_ready = self.hospital_client is not None and self.hospital_client.enabled
        layer_ready = _feed_enabled(self.safemap_hospitals)
        hira_alert = ""
        if hira_ready:
            assert self.hira_client is not None
            try:
                records = await self.hira_client.hospitals_around(center, radius_m)
                return FeedResult(
                    tuple(
                        RawPlace(r.name, r.address, r.div_name, r.coordinates)
                        for r in records
                    ),
                    HIRA_HOSPITAL_SOURCE,
                )
            except Exception:
                logger.warning("심평원 병원정보 조회 실패: 국립중앙의료원으로 대체", exc_info=True)
                if not (ncmc_ready or layer_ready):
                    raise
                hira_alert = (
                    "건강보험심사평가원 병원정보 조회가 응답하지 않아 국립중앙의료원 원장으로 "
                    "대체했습니다. 갱신이 늦어 최근 지정된 종합병원이 빠졌을 수 있습니다."
                )
        ncmc_alert = ""
        if ncmc_ready:
            try:
                result = await self._ncmc_hospitals(center, radius_m)
                return result._replace(degraded=bool(hira_alert), alert=hira_alert)
            except Exception:
                logger.warning("국립중앙의료원 조회 실패: 생활안전지도 레이어로 대체", exc_info=True)
                if not layer_ready:
                    raise
                ncmc_alert = " ".join(
                    filter(
                        None,
                        [
                            hira_alert,
                            "국립중앙의료원 병원 조회가 응답하지 않아 생활안전지도 병원 "
                            "레이어로 대체했습니다.",
                        ],
                    )
                )
        ncmc_alert = ncmc_alert or hira_alert
        assert self.safemap_hospitals is not None
        rows = await self.safemap_hospitals.facilities_around(center, radius_m)
        return FeedResult(
            _layer_places(rows, lambda r: f"의료,건강 > 병원 > {r.kind}"),
            SAFEMAP_HOSPITAL_SOURCE,
            degraded=bool(ncmc_alert),
            alert=ncmc_alert,
            snapshot_note=_snapshot_note(self.safemap_hospitals, "생활안전지도 병원 레이어"),
        )

    async def _kakao_category(
        self,
        code: str,
        center: Coordinates,
        radius_m: int,
    ) -> FeedResult:
        documents = await self._search_uncapped(
            lambda c, r: self.kakao.search_category(code, c.lat, c.lng, r),
            center,
            radius_m,
        )
        return FeedResult(_places(documents, _kakao_place), KAKAO_PLACE_SOURCE)

    async def _kakao_keyword(
        self,
        query: str,
        center: Coordinates,
        radius_m: int,
    ) -> FeedResult:
        documents = await self._search_uncapped(
            lambda c, r: self.kakao.search_keyword(query, c.lat, c.lng, r),
            center,
            radius_m,
        )
        return FeedResult(_places(documents, _kakao_place), KAKAO_PLACE_SOURCE)

    async def _ncmc_hospitals(
        self,
        center: Coordinates,
        radius_m: int,
    ) -> FeedResult:
        """국립중앙의료원 종합병원 원장. 시도를 구해 받아 반경으로 거른다.

        이 API 는 반경검색이 없어 시도 단위로 받는다. 시도는 카카오 역지오코딩으로
        구한다. 시도를 못 구하면 조회 자체가 불가능하므로 성공한 빈 피드가 아니라
        예외를 올려 상위(_build_group)가 시설군을 'missing' 으로 내리게 한다. 빈
        피드로 두면 병원이 없는 것과 조회 실패가 구분되지 않아 2차 주거여건 등급이
        잘못 내려간다.
        """

        sido = await self._resolve_sido(center)
        if not sido:
            raise RuntimeError(
                "역지오코딩으로 시도를 구하지 못해 국립중앙의료원 종합병원을 "
                "조회할 수 없습니다."
            )
        assert self.hospital_client is not None
        records = await self.hospital_client.hospitals_around(center, sido, radius_m)
        places = tuple(
            RawPlace(
                name=record.name,
                address=record.address,
                category_name=record.div_name,
                coordinates=record.coordinates,
            )
            for record in records
            if record.name
        )
        return FeedResult(places, NCMC_HOSPITAL_SOURCE)

    async def _resolve_sido(self, center: Coordinates) -> str:
        """좌표가 속한 시도 이름. 카카오 법정동 주소의 첫 토큰을 쓴다."""

        if not self.kakao.enabled:
            return ""
        try:
            info = await self.kakao.region_info(center.lat, center.lng)
        except Exception:
            return ""
        name = (info.legal_name or "").strip()
        return name.split()[0] if name else ""

    async def _search_uncapped(
        self,
        search: Callable[[Coordinates, int], Awaitable[list[dict[str, Any]]]],
        center: Coordinates,
        radius_m: int,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        """카카오 45건 상한에 걸리면 영역을 사분면으로 쪼개 다시 훑는다.

        카카오 로컬 API 는 한 질의당 3페이지 x 15건이 하드 상한이고 거리순으로
        자른다. 3km 반경에서 병원을 부르면 가까운 의원이 45칸을 채워 먼
        종합병원이 통째로 사라진다. 상한에 닿은 질의만 쪼개므로, 시설이 성긴
        지역에서는 호출이 늘지 않는다.
        """

        documents = await search(center, radius_m)
        if len(documents) < KAKAO_RESULT_CAP or depth >= MAX_SUBDIVIDE_DEPTH:
            return documents

        # 반지름 R 원을 (±R/2, ±R/2) 네 점에서 0.75R 로 덮는다. 가장 먼 경계점
        # (R, 0) 까지가 0.707R 이므로 빈틈이 생기지 않는다.
        offset = radius_m / 2
        sub_radius = max(1, int(radius_m * 0.75))
        quadrants = [
            offset_coordinates(center, north, east)
            for north, east in ((offset, offset), (offset, -offset), (-offset, offset), (-offset, -offset))
        ]
        batches = await asyncio.gather(
            *(self._search_uncapped(search, q, sub_radius, depth + 1) for q in quadrants),
            return_exceptions=True,
        )

        merged: dict[tuple[str, str, str], dict[str, Any]] = {}
        for batch in [documents, *batches]:
            if isinstance(batch, BaseException):
                continue  # 한 사분면 실패로 나머지 결과까지 버리지 않는다
            for row in batch:
                key = (
                    str(row.get("place_name") or ""),
                    str(row.get("x") or ""),
                    str(row.get("y") or ""),
                )
                merged.setdefault(key, row)
        return list(merged.values())

    async def _stop_headways(
        self, places: Sequence[RawPlace]
    ) -> dict[str, StopHeadway] | None:
        """TAGO 정류장마다 운행주기를 판정해 정류장명 키로 돌려준다.

        판정기가 없거나 조회가 통째로 실패하면 None — 그때는 전체 정류장을 세고
        그 사실을 고지한다(BUS_STOP_NO_HEADWAY_NOTE).
        """

        if self.headway_resolver is None:
            return None
        refs = [
            tuple(place.stop_ref.split(":", 1))
            for place in places
            if place.stop_ref and ":" in place.stop_ref
        ]
        if not refs:
            return None
        try:
            by_ref = await self.headway_resolver.resolve_many(refs)  # type: ignore[arg-type]
        except Exception:
            logger.warning("TAGO 운행주기 조회 실패", exc_info=True)
            raise
        merged: dict[str, StopHeadway] = {}
        for place in places:
            headway = by_ref.get(place.stop_ref)
            if headway is None:
                continue
            key = _stop_name_key(place.name)
            current = merged.get(key)
            # 같은 이름(양방향) 정류장은 하나로 보고 더 유리한 판정을 대표로 둔다.
            if current is None or (
                headway.determined
                and (
                    not current.determined
                    or headway.arrivals_per_15min > current.arrivals_per_15min
                )
            ):
                merged[key] = headway
        return merged

    async def _bus_stops(self, center: Coordinates, radius_m: int) -> FeedResult:
        """TAGO 정류소 근접조회. 실패하면 지도 검색으로 대체한다.

        TAGO 는 약 500m 만 문서화돼 있어 그 밖은 어차피 비어 있다. 그래도 정류장은
        0.5km 등급이 핵심이라 공식 원천을 먼저 쓴다.
        """

        tago_failed = False
        if self.tago is not None and self.tago.enabled:
            try:
                rows = await self.tago.nearby_stops(center.lat, center.lng)
                if rows:
                    places = _places(rows, _tago_place)
                    try:
                        headways = await self._stop_headways(places)
                    except Exception:
                        # 정류장은 받았으니 전체 정류장을 세되, 캐시하지 않고 다음 심사에서 다시 판정한다.
                        return FeedResult(
                            places, TAGO_SOURCE, degraded=True, alert=BUS_HEADWAY_ALERT
                        )
                    if headways is None:
                        return FeedResult(places, TAGO_SOURCE)
                    return FeedResult(places, TAGO_HEADWAY_SOURCE, headways)
                # TAGO 는 서울을 제공하지 않는다(2026-09-16 실측 0건). 빈 결과는 장애가
                # 아니라 미제공 지역일 수 있으니 서울시 원천 → 지도 순으로 넘어간다.
            except Exception:
                # 공공 API 장애를 정류장 0개로 둔갑시키지 않는다. 지도로 넘어간다.
                logger.warning("TAGO 정류소 근접조회 실패: 지도 검색으로 대체", exc_info=True)
                tago_failed = True
        if self.seoul_bus is not None and self.seoul_bus.enabled:
            try:
                stops = await self.seoul_bus.stops_around(center, radius_m)
                if stops:
                    return FeedResult(
                        tuple(
                            RawPlace(s.name, "", f"교통,수송 > 버스정류장 > {s.stop_type}", s.coordinates)
                            for s in stops
                        ),
                        SEOUL_BUS_SOURCE,
                        snapshot_note=_snapshot_note(
                            self.seoul_bus, "서울시 버스정류소 위치정보"
                        ),
                    )
            except Exception:
                logger.warning("서울시 정류소 조회 실패: 지도 검색으로 대체", exc_info=True)
        if not self.kakao.enabled:
            raise RuntimeError("TAGO·카카오 원천이 모두 설정되지 않았습니다.")
        documents = await self.kakao.search_keyword(
            "버스정류장", center.lat, center.lng, radius_m
        )
        return FeedResult(
            _places(documents, _kakao_place),
            KAKAO_PLACE_SOURCE,
            degraded=tago_failed,
            alert=BUS_TAGO_ALERT if tago_failed else "",
        )

    async def _transfer_centers(self, center: Coordinates, radius_m: int) -> FeedResult:
        """환승시설 = 전국대중교통환승센터 표준데이터(지정 원천)만.

        표준데이터가 응답하면 그것만 쓴다(연결). 지도 검색 보충은 LH 데이터셋 범위 밖의
        환승정류장·환승주차장을 더해 결과를 갈라 뺐다. 표준데이터가 실패하거나 키가
        없을 때만 카카오 지도 검색으로 대체하고 경고를 올린다.
        """

        standard_error: Exception | None = None
        if self.transfer_client is not None and self.transfer_client.enabled:
            try:
                centers = await self.transfer_client.centers_around(center, radius_m)
            except Exception as exc:
                logger.warning("환승센터 표준데이터 실패", exc_info=True)
                standard_error = exc
            else:
                return FeedResult(
                    tuple(
                        _dedupe(
                            [
                                RawPlace(c.name, c.address, "교통,수송 > 환승센터", c.coordinates)
                                for c in centers
                            ]
                        )
                    ),
                    TRANSFER_STANDARD_SOURCE,
                    snapshot_note=_snapshot_note(
                        self.transfer_client, "전국대중교통환승센터표준데이터"
                    ),
                )
        if not self.kakao.enabled:
            if standard_error is not None:
                raise standard_error
            raise SourceMissing(TRANSFER_MISSING_NOTE)
        found: list[RawPlace] = []
        for keyword in TRANSFER_KEYWORDS:
            documents = await self.kakao.search_keyword(
                keyword, center.lat, center.lng, radius_m
            )
            for place in _places(documents, _kakao_place):
                if "환승" not in place.name:
                    continue
                if _category_leaf(place) not in TRANSFER_CATEGORY_LEAVES:
                    continue
                found.append(place)
        result = FeedResult(tuple(_dedupe(found)), KAKAO_PLACE_SOURCE)
        if standard_error is None:
            return result  # 표준데이터 키 미설정 — 근사(substituted) 고지로 드러난다
        return result._replace(degraded=True, alert=TRANSFER_FALLBACK_ALERT)

    # -- 역·터미널·도서관(지도 회사와 무관한 원천 우선) ---------------------------
    def _place_search_ready(self) -> bool:
        """VWorld 장소검색을 쓸 수 있는가(키 + 장소검색을 갖춘 클라이언트)."""

        return (
            self.vworld is not None
            and bool(getattr(self.vworld, "enabled", False))
            and hasattr(self.vworld, "places_around")
        )

    async def _vworld_places(
        self,
        queries: Sequence[str],
        center: Coordinates,
        radius_m: int,
        leaves: frozenset[str],
    ) -> list[VWorldPlace]:
        """VWorld 장소검색 여러 질의를 모아 분류 잎이 leaves 인 것만 남긴다. 하나라도 실패하면 올린다."""

        assert self.vworld is not None
        batches = await asyncio.gather(
            *(self.vworld.places_around(query, center, radius_m) for query in queries)
        )
        seen: set[str] = set()
        places: list[VWorldPlace] = []
        for batch in batches:
            for place in batch:
                if place.id in seen or place.category_leaf not in leaves:
                    continue
                seen.add(place.id)
                places.append(place)
        return places

    async def _libraries(self, center: Coordinates, radius_m: int) -> FeedResult:
        """공공도서관: 전국도서관표준데이터 → VWorld 「공공도서관」 분류 → 카카오 「도서관」.

        LH 공공시설의 도서관은 공공도서관 목록(JB_45)이다. VWorld 「공공도서관」 분류는 그
        66곳과 이름까지 같았다(2026-09-30 전북 전역 대조). 원천이 모두 실패해도 공공시설
        전체를 버리지 않고 도서관만 빼고 세며, 그 사실을 경고로 올린다.
        """

        failures: list[tuple[str, BaseException]] = []
        if self.library_client is not None and self.library_client.enabled:
            try:
                records = await self.library_client.libraries_around(center, radius_m)
            except Exception as exc:  # noqa: BLE001 — 다음 원천으로 넘어가고 경고한다
                logger.warning("전국도서관표준데이터 실패: VWorld 로 대체 (%s)", exc)
                failures.append(("전국도서관표준데이터(15013109)는", exc))
            else:
                result = await self._standard_libraries(records, center, radius_m)
                return result._replace(
                    snapshot_note=_snapshot_note(self.library_client, "전국도서관표준데이터")
                )
        if self._place_search_ready():
            try:
                found = await self._vworld_places(
                    (VWORLD_LIBRARY_QUERY,), center, radius_m, frozenset({VWORLD_LIBRARY_LEAF})
                )
                places = tuple(
                    RawPlace(p.title, p.address, p.category, p.coordinates) for p in found
                )
                return FeedResult(
                    places,
                    VWORLD_LIBRARY_SOURCE,
                    degraded=bool(failures),
                    alert=_fallback_alert(
                        failures,
                        "VWorld 장소검색 「공공도서관」 분류로 도서관을 찾았습니다.",
                        "LH 데이터셋 공공도서관 목록과 같은 분류입니다.",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("VWorld 도서관 검색 실패 (%s)", exc)
                failures.append(("VWorld 장소검색은", exc))
        if self.kakao.enabled:
            try:
                result = await self._kakao_keyword("도서관", center, radius_m)
            except Exception as exc:  # noqa: BLE001
                failures.append(("카카오 장소검색은", exc))
            else:
                return result._replace(
                    degraded=bool(failures),
                    alert=_fallback_alert(
                        failures,
                        "카카오 지도 「도서관」 검색으로 도서관을 찾았습니다.",
                        "작은도서관·학교도서관이 섞이거나 공공도서관이 빠졌을 수 있습니다.",
                    ),
                )
        # 도서관 원천이 하나도 답하지 않았다 — 공공시설은 관공서·주민센터로만 센다.
        alert = _fallback_alert(
            failures, "도서관을 찾지 못해 공공시설을 도서관 없이 셌습니다.", ""
        ) or "도서관 위치 원천이 설정되지 않아 공공시설을 도서관 없이 셌습니다."
        return FeedResult((), LIBRARY_NONE_SOURCE, degraded=True, alert=alert)

    async def _standard_libraries(
        self, records: Sequence[Any], center: Coordinates, radius_m: int
    ) -> FeedResult:
        """표준데이터 공공도서관(목록) + VWorld 「공공도서관」 분류(위치·이름·보충).

        표준데이터는 지자체가 올린 값을 그대로 싣는다. 실측(2026-09-30 전북 66행):
          - 김제시립도서관(본관·금구·만경분관)·명봉도서관이 없다(LH 66곳 중 59곳만 같다).
          - 군산시립·설림도서관이 방 이름(「(자료열람실)」·「(학습실)」)으로 두 줄씩 실렸다.
          - 좌표가 틀린 줄이 있다 — 금강도서관은 군산시립도서관 자리(3.1km 차이), 장수군립·
            임피채만식도서관은 수 km, 전북도청도서관 238m·오수도서관 241m 어긋난다.
            VWorld 좌표는 카카오 좌표와 같은 자리였다(부송도서관 682.9m 로 일치).
          - 이름이 「인후도서관」 꼴이라 LH(「전주시립인후도서관」)와 다르다.
        그래서 표준데이터 도서관마다 VWorld 공공도서관을 이름(한쪽이 다른 쪽을 품음,
        LIBRARY_NAME_MATCH_M 안) → 같은 자리(LIBRARY_SAME_SPOT_M) 순으로 짝지어, 짝이 있으면
        VWorld 이름·좌표(LH 와 같은 표기)로 적고, 짝이 없는 표준데이터 도서관은 그대로,
        표준데이터에 없는 VWorld 공공도서관은 더한다. VWorld 가 응답하지 않으면 표준데이터만
        쓰고 경고한다.
        """

        standard = _merge_same_place(
            [
                RawPlace(
                    _strip_room_suffix(r.name),
                    r.address,
                    f"문화,예술 > 도서관 > {PUBLIC_LIBRARY_LEAF}",
                    r.coordinates,
                )
                for r in records
            ],
            within_m=LIBRARY_SAME_SPOT_M,
        )
        if not self._place_search_ready():
            return FeedResult(tuple(standard), LIBRARY_STANDARD_SOURCE)
        try:
            # 표준데이터 좌표가 틀린 도서관도 이름으로 짝지을 수 있게 넓게 묻는다.
            found = await self._vworld_places(
                (VWORLD_LIBRARY_QUERY,),
                center,
                radius_m + int(LIBRARY_NAME_MATCH_M),
                frozenset({VWORLD_LIBRARY_LEAF}),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("VWorld 공공도서관 보충 실패: 표준데이터만 사용 (%s)", exc)
            return FeedResult(
                tuple(standard),
                LIBRARY_STANDARD_SOURCE,
                degraded=True,
                alert=(
                    "VWorld 장소검색이 응답하지 않아 공공도서관을 전국도서관표준데이터만으로 "
                    "셌습니다. 표준데이터에 없는 공공도서관(김제시립도서관 등)이 빠지거나 "
                    "좌표가 틀린 도서관이 있을 수 있으니 잠시 뒤 다시 심사해 주십시오."
                ),
            )
        merged = [
            place
            for place in _merge_library_sources(standard, found)
            if haversine_meters(center, place.coordinates) <= radius_m
        ]
        return FeedResult(
            tuple(merged), f"{LIBRARY_STANDARD_SOURCE} + {VWORLD_LIBRARY_SOURCE}(위치·보충)"
        )

    async def _railways(self, center: Coordinates, radius_m: int) -> FeedResult:
        """철도역: 한국철도공사 역위치 정보 → VWorld 일반·고속철도역 분류 → 카카오 「기차역」."""

        failures: list[tuple[str, BaseException]] = []
        if self.korail_client is not None and self.korail_client.enabled:
            try:
                records = await self.korail_client.stations_around(center, radius_m)
                return FeedResult(
                    tuple(
                        RawPlace(r.name, "", "교통,수송 > 기차역", r.coordinates)
                        for r in records
                    ),
                    KORAIL_STATION_SOURCE,
                    snapshot_note=_snapshot_note(
                        self.korail_client, "한국철도공사 역위치 정보"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("한국철도공사 역위치 정보 실패: VWorld 로 대체 (%s)", exc)
                failures.append(("한국철도공사 역위치 정보(15127532)는", exc))
        if self._place_search_ready():
            try:
                found = await self._vworld_places(
                    (VWORLD_RAILWAY_QUERY,), center, radius_m, VWORLD_RAILWAY_LEAVES
                )
                places = _merge_same_place(
                    [
                        RawPlace(station_name(p.title), p.address, p.category, p.coordinates)
                        for p in found
                    ]
                )
                return FeedResult(
                    tuple(places),
                    VWORLD_RAILWAY_SOURCE,
                    degraded=bool(failures),
                    alert=_fallback_alert(
                        failures,
                        "VWorld 장소검색(일반·고속철도역 분류)으로 철도역을 찾았습니다.",
                        "전북은 LH 철도역·KTX역 목록과 같은 역이 잡힙니다.",
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("VWorld 철도역 검색 실패 (%s)", exc)
                failures.append(("VWorld 장소검색은", exc))
        return await self._kakao_last_resort(
            "기차역", center, radius_m, failures, "철도역",
            "역 앞 상가 등이 섞이거나 역이 빠졌을 수 있습니다.",
        )

    async def _terminals(self, center: Coordinates, radius_m: int) -> FeedResult:
        """터미널: VWorld 시외·고속·종합버스터미널 분류 → 카카오 「버스터미널」·「고속버스터미널」.

        LH 터미널 목록(산림빅데이터 대중교통수단터미널정보, 비공개)과 같은 공공 API 는 없다.
        VWorld 분류는 LH 전북 68곳 중 버스터미널 이름의 곳을 거의 모두 담고, 역 이름
        터미널(「익산」 = 익산역 필지)·간이 정류소·군산공항은 담지 않는다.
        """

        failures: list[tuple[str, BaseException]] = []
        if self._place_search_ready():
            try:
                found = await self._vworld_places(
                    VWORLD_TERMINAL_QUERIES, center, radius_m, VWORLD_TERMINAL_LEAVES
                )
                places = _merge_same_place(
                    [RawPlace(p.title, p.address, p.category, p.coordinates) for p in found]
                )
                return FeedResult(tuple(places), VWORLD_TERMINAL_SOURCE)
            except Exception as exc:  # noqa: BLE001
                logger.warning("VWorld 터미널 검색 실패 (%s)", exc)
                failures.append(("VWorld 장소검색은", exc))
        if not self.kakao.enabled:
            return await self._kakao_last_resort(
                "버스터미널", center, radius_m, failures, "터미널", ""
            )
        try:
            first, second = await asyncio.gather(
                self._kakao_keyword("버스터미널", center, radius_m),
                self._kakao_keyword("고속버스터미널", center, radius_m),
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(("카카오 장소검색은", exc))
            raise RuntimeError(_fallback_alert(failures, FALLBACK_FAILED, "")) from exc
        return FeedResult(
            first.places + second.places,
            KAKAO_PLACE_SOURCE,
            degraded=bool(failures),
            alert=_fallback_alert(
                failures,
                "카카오 지도 검색으로 터미널을 찾았습니다.",
                "지도 회사 분류라 결과가 다를 수 있습니다.",
            ),
        )

    async def _subways(self, center: Coordinates, radius_m: int) -> FeedResult:
        """지하철역: 카카오 SW8(+ 출구 번호별 조회) → VWorld 지하철역·지하철역입구 분류.

        지하철만 카카오가 1순위다 — VWorld 지하철역입구는 역마다 일부 출구만 있어 가장
        가까운 출구가 빠질 수 있다. 카카오가 막히면(일일 한도 등) VWorld 로 역과 출구를
        찾고 경고한다. 네이버 출구 검색은 두 경우 모두 보충으로 붙는다.
        """

        failures: list[tuple[str, BaseException]] = []
        if self.kakao.enabled:
            try:
                return await self._kakao_category("SW8", center, radius_m)
            except Exception as exc:  # noqa: BLE001
                logger.warning("카카오 지하철역 조회 실패: VWorld 로 대체 (%s)", exc)
                failures.append(("카카오 장소검색은", exc))
        if not self._place_search_ready():
            if failures:
                raise RuntimeError(_fallback_alert(failures, FALLBACK_FAILED, ""))
            raise SourceMissing(KAKAO_DISABLED_NOTE)
        assert self.vworld is not None
        try:
            found = await self.vworld.places_around(
                VWORLD_SUBWAY_QUERY, center, radius_m + STATION_EXIT_SEARCH_RADIUS_M
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(("VWorld 장소검색은", exc))
            raise RuntimeError(_fallback_alert(failures, FALLBACK_FAILED, "")) from exc
        stations = _merge_same_place(
            [
                RawPlace(station_name(p.title), p.address, p.category, p.coordinates)
                for p in found
                if p.category_leaf == VWORLD_SUBWAY_LEAF
                and haversine_meters(center, p.coordinates) <= radius_m
            ],
            within_m=500.0,
        )
        bases = {station_base(s.name) for s in stations}
        doors = tuple(
            (base, p.title, p.coordinates)
            for p in found
            if p.category_leaf == VWORLD_SUBWAY_EXIT_LEAF
            and (base := _exit_station(p.title)) in bases
        )
        return FeedResult(
            tuple(stations),
            VWORLD_SUBWAY_SOURCE,
            degraded=bool(failures),
            alert=_fallback_alert(
                failures,
                "VWorld 장소검색(지하철역·지하철역입구 분류)으로 지하철역과 출입구를 찾았습니다.",
                "VWorld 에 없는 출입구가 있어 가장 가까운 출입구가 빠졌을 수 있습니다.",
            ),
            station_doors=doors,
        )

    async def _kakao_last_resort(
        self,
        query: str,
        center: Coordinates,
        radius_m: int,
        failures: list[tuple[str, BaseException]],
        what: str,
        caveat: str,
    ) -> FeedResult:
        """앞선 원천이 모두 빠졌을 때 카카오 키워드 검색. 카카오도 없으면 올린다."""

        if not self.kakao.enabled:
            if failures:
                raise RuntimeError(_fallback_alert(failures, FALLBACK_FAILED, ""))
            raise SourceMissing(f"{what} 위치 원천(공공 API·VWorld·카카오)이 설정되지 않았습니다.")
        try:
            result = await self._kakao_keyword(query, center, radius_m)
        except Exception as exc:  # noqa: BLE001
            if not failures:
                raise
            failures.append(("카카오 장소검색은", exc))
            raise RuntimeError(_fallback_alert(failures, FALLBACK_FAILED, "")) from exc
        return result._replace(
            degraded=bool(failures),
            alert=_fallback_alert(
                failures, f"카카오 지도 「{query}」 검색으로 {what}을 찾았습니다.", caveat
            ),
        )

    # -- 시설군 조립 -------------------------------------------------------
    def _build_group(
        self,
        key: str,
        results: dict[str, FeedResult | BaseException],
        rings: list[list[Coordinates]],
        center: Coordinates,
        radius_m: int,
    ) -> GroupCollection:
        spec = GROUP_SPECS[key]
        if key == "culture":
            return self._build_culture_group(results, rings, center, radius_m)
        if spec.localdata_datasets:
            return self._build_localdata_group(key, spec, rings, center, radius_m, results)
        if not spec.feeds:
            return GroupCollection(key, "missing", spec.note, "", (), ())
        if spec.kakao_backed and not self.kakao.enabled:
            return GroupCollection(key, "missing", KAKAO_DISABLED_NOTE, "", (), ())

        places: list[RawPlace] = []
        sources: list[str] = []
        # 여러 원천을 합치는 시설군(공공 = 관공서 레이어 + 도서관)은 한 원천이 실패해도
        # 나머지로 센다. 실패한 원천은 _with_source_alert 가 「일부 원천 실패」로 올린다
        # — 도서관 검색 하나가 막혔다고 주민센터까지 통째로 빠지면 안 된다(2026-09-30
        # 카카오 한도 소진 때 공공시설이 전부 사라졌다). 모든 원천이 실패해야 missing 이다.
        failure: GroupCollection | None = None
        for feed in spec.feeds:
            outcome = results.get(feed)
            if isinstance(outcome, BaseException):
                failure = failure or GroupCollection(
                    key,
                    "missing",
                    str(outcome)
                    if isinstance(outcome, SourceMissing)
                    else f"원천 조회에 실패했습니다: {outcome}",
                    "",
                    (),
                    (),
                )
                continue
            if outcome is None:
                failure = failure or GroupCollection(
                    key, "missing", KAKAO_DISABLED_NOTE, "", (), ()
                )
                continue
            places.extend(outcome.places)
            if outcome.source_label not in sources:
                sources.append(outcome.source_label)
        if failure is not None and not sources:
            return failure

        keep = spec.keep
        kept = [
            place
            for place in _dedupe(places)
            if not is_planned_facility(place.name) and (keep is None or keep(place))
        ]
        if key in ("railway", "subway"):
            # 역은 대표점이 아니라 출입구에서 잰다(LH 과업내용서 예외기준).
            kept = [self._with_station_entrance(place, center) for place in kept]
        source_label = " + ".join(sources)
        group_notice = ""
        if key == "university":
            # 대학교만 시설 측 3단(정문 → 부지경계 → 좌표)으로 잰다(LH 09/22 결정 2).
            # 종합병원은 BOUNDARY_GROUPS 로 필지 경계에서 잰다.
            stop_points = _stop_points(results.get("bus_stop"))
            pending_notice = FRONT_DOOR_PENDING_NOTICE
            exclude_tokens = _AUTO_EXCLUDE_TOKENS
            facilities = sorted(
                (
                    self._measure_with_front_door(
                        place,
                        rings,
                        center,
                        source_label,
                        stop_points,
                        pending_notice=pending_notice,
                        exclude_tokens=exclude_tokens,
                        use_university_base=(key == "university"),
                    )
                    for place in kept
                ),
                key=lambda facility: facility.distance_m,
            )
            facilities = _collapse_same_gate(facilities)
            # 레이어는 캠퍼스 대표점을 주므로 반경을 넓혀 받았다(_universities). 정문까지
            # 잰 거리가 반경 밖인 학교는 뺀다 — LH앱도 정문 거리 반경 안만 싣는다.
            if SAFEMAP_UNIVERSITY_SOURCE in sources:
                facilities = [f for f in facilities if f.distance_m <= radius_m]
            group_notice = _front_door_group_notice(facilities)
        else:
            is_station = key in ("railway", "subway")
            # 역·터미널·환승시설은 담당자 수기 기준점 지정을 허용한다(#11 「출구 여럿이면
            # 담당자 선택」). 지정이 있으면 그 기준점으로 재고, 없으면 기존 동작
            # (역은 최근접 출구, 그 외 좌표)을 그대로 쓴다.
            designatable = key in _STATION_LIKE_DESIGNATABLE
            facilities = sorted(
                (
                    self._measure_station_like(
                        place,
                        rings,
                        center,
                        source_label,
                        is_station=is_station,
                        designatable=designatable,
                    )
                    for place in kept
                ),
                key=lambda facility: facility.distance_m,
            )
        state = spec.state
        note = spec.note
        # 의료시설을 국립중앙의료원 지정 원천으로 채웠으면 근사가 아니라 연결이다.
        if key == "hospital" and HIRA_HOSPITAL_SOURCE in sources:
            state = "connected"
            note = HOSPITAL_HIRA_NOTE
        elif key == "hospital" and NCMC_HOSPITAL_SOURCE in sources:
            state = "connected"
            note = HOSPITAL_NCMC_NOTE
        elif key == "hospital" and SAFEMAP_HOSPITAL_SOURCE in sources:
            state = "connected"
            note = HOSPITAL_LAYER_NOTE
        # 초·중·고·공공도 생활안전지도 지정 원천이 답했으면 연결이다.
        if key.startswith("school_") and SCHOOL_STANDARD_SOURCE in sources:
            state = "connected"
            note = SCHOOL_STANDARD_NOTE
        elif key.startswith("school_") and SAFEMAP_SCHOOL_SOURCE in sources:
            state = "connected"
            note = SCHOOL_LAYER_NOTE
        if key == "park" and any(
            src.startswith(park_source.PARK_STANDARD_SOURCE) for src in sources
        ):
            state = "connected"
            note = (
                park_source.PARK_SUPPLEMENT_NOTE
                if any("+" in src for src in sources)
                else park_source.PARK_STANDARD_NOTE
            )
        if key == "university" and SAFEMAP_UNIVERSITY_SOURCE in sources:
            state = "connected"
            note = UNIVERSITY_LAYER_NOTE
        if key == "public" and any(src.startswith(SAFEMAP_OFFICE_SOURCE) for src in sources):
            state = "connected"
            note = PUBLIC_LAYER_NOTE
        # 환승시설도 표준데이터로 채웠으면 연결이다.
        if key == "transfer" and any(
            src.startswith(TRANSFER_STANDARD_SOURCE) for src in sources
        ):
            state = "connected"
            note = TRANSFER_STANDARD_NOTE
        # 역·터미널은 실제로 답한 원천에 맞춰 고지를 고른다(공공 API → VWorld → 카카오).
        if key == "railway" and KORAIL_STATION_SOURCE in sources:
            state = "connected"
            note = RAILWAY_KORAIL_NOTE
        elif key == "railway" and KAKAO_PLACE_SOURCE in sources:
            note = RAILWAY_KAKAO_NOTE
        if key == "terminal" and KAKAO_PLACE_SOURCE in sources:
            note = TERMINAL_KAKAO_NOTE
        if key == "subway" and VWORLD_SUBWAY_SOURCE in sources:
            state = "substituted"
            note = SUBWAY_VWORLD_NOTE
        if key == "public":
            library = results.get("library")
            if isinstance(library, FeedResult) and library.source_label:
                note = f"{note} 도서관 원천: {library.source_label}."
        # 버스정류장은 운행주기 15분 판정으로 인정 정류장만 배점에 센다.
        if key == "bus_stop":
            outcome = results.get("bus_stop")
            headways = outcome.headways if isinstance(outcome, FeedResult) else None
            if headways is not None:
                facilities = _apply_bus_headway(facilities, headways)
                state = "connected"
                note = BUS_STOP_HEADWAY_NOTE
        # 서버 사본(받은 지 하루 넘은 목록)으로 답한 원천은 비고에 기준일을 드러낸다.
        snapshot_notes = [
            outcome.snapshot_note
            for outcome in (results.get(feed) for feed in spec.feeds)
            if isinstance(outcome, FeedResult) and outcome.snapshot_note
        ]
        if snapshot_notes:
            note = " ".join([note, *dict.fromkeys(snapshot_notes)]).strip()
        return GroupCollection(
            key=key,
            state=state,
            note=note,
            actual_source=" + ".join(sources),
            distances_m=tuple(
                facility.distance_m for facility in facilities if facility.counted
            ),
            facilities=tuple(facilities[:MAX_HITS_PER_GROUP]),
            front_door_notice=group_notice,
        )

    def _build_localdata_group(
        self,
        key: str,
        spec: GroupSpec,
        rings: list[list[Coordinates]],
        center: Coordinates,
        radius_m: int,
        results: dict[str, FeedResult | BaseException] | None = None,
    ) -> GroupCollection:
        """인허가 캐시(facility_store)로 채우는 시설군.

        원장이 적재돼 있으면 지정 원천으로 직접 산정하고, 미적재면 '없음'이
        아니라 missing_note 로 남긴다(룰북: 원천 부재를 0 으로 단정하지 않는다).
        """

        store = self.facility_store
        if store is None:
            return GroupCollection(key, "missing", spec.missing_note, "", (), ())

        ready = store.ready_datasets()
        available = [d for d in spec.localdata_datasets if d in ready]
        if not available:
            return GroupCollection(key, "missing", spec.missing_note, "", (), ())

        # facility_store 는 이미 영업중(SALS_STTS_CD='01') 만 적재한다. 반경으로
        # 1차로 좁힌 뒤 사업부지 경계 기준 거리로 다시 잰다.
        stored = store.facilities_around(center, radius_m, available)
        facilities = sorted(
            (
                CollectedFacility(
                    name=item.name,
                    address=item.road_address or item.address,
                    coordinates=item.coordinates,
                    distance_m=_distance_m(item.coordinates, rings, center),
                    source_label=LOCALDATA_SOURCE,
                    # 최단거리선(#5): _build_group 과 같이 시설 기준점·대지경계 앵커를
                    # 채운다. 채우지 않으면 상업시설 hit 의 선분이 null 로 빠진다.
                    nearest_facility_point=item.coordinates,
                    nearest_boundary_point=_anchor_for(item.coordinates, rings),
                )
                for item in stored
            ),
            key=lambda facility: facility.distance_m,
        )
        note = spec.note
        actual_source = LOCALDATA_SOURCE
        if key == "retail":
            # 상업시설 = 대규모점포 + 전통시장. 시장 feed 가 없거나 실패했으면 대규모점포만
            # 세고 고지를 바꾼다(실패 경고는 _with_source_alert 가 올린다).
            outcome = (results or {}).get("traditional_market")
            if isinstance(outcome, FeedResult) and not outcome.degraded:
                markets = _market_facilities(outcome, stored, rings, center)
                facilities = sorted(facilities + markets, key=lambda f: f.distance_m)
                actual_source = f"{LOCALDATA_SOURCE} + {market_source.MARKET_SOURCE}"
                # 전통시장 목록을 서버 사본(받은 지 하루 넘음)으로 답했으면 기준일을 드러낸다.
                if outcome.snapshot_note:
                    note = f"{note} {outcome.snapshot_note}".strip()
            else:
                note = market_source.RETAIL_STORES_ONLY_NOTE
        return GroupCollection(
            key=key,
            state=spec.state,
            note=note,
            actual_source=actual_source,
            distances_m=tuple(facility.distance_m for facility in facilities),
            facilities=tuple(facilities[:MAX_HITS_PER_GROUP]),
        )

    # -- 문화시설(인허가 3종 + 선택 확장) -----------------------------------
    def _culture_plan(self) -> culture_source.CulturePlan:
        ready: set[str] = set()
        if self.facility_store is not None:
            try:
                ready = self.facility_store.ready_datasets()
            except Exception:  # 적재 상태를 못 읽으면 미적재로 보고 대체·경고로 간다.
                logger.exception("문화시설 인허가 원장 적재 상태 조회 실패")
        return culture_source.plan_culture(
            ready, option_enabled(culture_source.CULTURE_EXTENDED_OPTION)
        )

    def _build_culture_group(
        self,
        results: dict[str, FeedResult | BaseException],
        rings: list[list[Coordinates]],
        center: Coordinates,
        radius_m: int,
    ) -> GroupCollection:
        """문화시설 — 기본은 LH 기준 인허가 3종, 확장·미적재 대체일 때만 카카오 CT1 을 더한다.

        원장이 빠져 카카오로 메웠으면 근사(substituted)로 내리고 source_alert 로 경고한다.
        """

        plan = self._culture_plan()
        facilities: list[CollectedFacility] = []
        sources: list[str] = []
        if plan.use_localdata and self.facility_store is not None:
            seen: list[tuple[str, Coordinates]] = []
            for item in self.facility_store.facilities_around(center, radius_m, plan.ready):
                # 영화상영관 원장은 관(스크린)마다 한 행이다. 같은 극장은 한 곳으로 센다.
                venue = culture_source.venue_name(item.name)
                if any(
                    culture_source.name_key(name) == culture_source.name_key(venue)
                    and haversine_meters(point, item.coordinates)
                    <= culture_source.DEDUPE_RADIUS_M
                    for name, point in seen
                ):
                    continue
                seen.append((venue, item.coordinates))
                suspended = "휴업" in (item.status or "")
                facilities.append(
                    CollectedFacility(
                        name=venue,
                        address=item.road_address or item.address,
                        coordinates=item.coordinates,
                        distance_m=_distance_m(item.coordinates, rings, center),
                        source_label=culture_source.CULTURE_LOCALDATA_SOURCE,
                        nearest_facility_point=item.coordinates,
                        nearest_boundary_point=_anchor_for(item.coordinates, rings),
                        count_note=(
                            culture_source.CULTURE_SUSPENDED_COUNT_NOTE if suspended else ""
                        ),
                    )
                )
            sources.append(culture_source.CULTURE_LOCALDATA_SOURCE)

        outcome = results.get("culture")
        kakao_ok = isinstance(outcome, FeedResult)
        if plan.use_kakao and kakao_ok:
            existing = [(f.name, f.coordinates) for f in facilities]
            for place in _dedupe(outcome.places):
                if is_planned_facility(place.name):
                    continue
                if culture_source.is_duplicate(place.name, place.coordinates, existing):
                    continue
                existing.append((place.name, place.coordinates))
                measured = self._measure_station_like(
                    place,
                    rings,
                    center,
                    outcome.source_label,
                    is_station=False,
                    designatable=False,
                )
                if plan.extended and not plan.fallback:
                    measured = measured._replace(
                        count_note=culture_source.CULTURE_KAKAO_COUNT_NOTE
                    )
                facilities.append(measured)
            sources.append(outcome.source_label)

        alert = plan.alert(kakao_ok)
        if not sources:
            return GroupCollection(
                "culture", "missing", plan.note(kakao_ok), "", (), (), source_alert=alert
            )
        facilities.sort(key=lambda facility: facility.distance_m)
        return GroupCollection(
            key="culture",
            state=plan.state,
            note=plan.note(kakao_ok),
            actual_source=" + ".join(sources),
            distances_m=tuple(facility.distance_m for facility in facilities),
            facilities=tuple(facilities[:MAX_HITS_PER_GROUP]),
            source_alert=alert,
        )

    # -- 정문·출구 3단 측정(대학·종합병원 공용) -----------------------------
    def _measure_with_front_door(
        self,
        place: RawPlace,
        rings: list[list[Coordinates]],
        center: Coordinates,
        source_label: str,
        stop_points: list[tuple[str, Coordinates]],
        *,
        pending_notice: str,
        exclude_tokens: tuple[str, ...] = _AUTO_EXCLUDE_TOKENS,
        use_university_base: bool = True,
    ) -> CollectedFacility:
        """대학교·종합병원 시설 한 곳을 시설 측 3단 기준으로 잰다(국장님 §3-2 · #8).

        우선순위 — 담당자 수기 지정(정문 좌표 > 정문 필지) > 표준 데이터셋 정문 좌표
        (대학교) > 네이버 문 후보(가장 가까운 문) > 정류장 원장 자동 채택 > 좌표 폴백+고지. 문 후보는 전부 실어 담당자가 다른
        문을 고를 수 있게 한다(#7·#11). 배점에 쓰는 거리와 화면 표기가 같은 계산에서
        나오도록 여기서 잰 distance_m 을 그대로 담는다. 최단거리선(#5)도 함께 싣는다.
        """

        site_token = "대지경계" if rings else "주소점"
        # 문 후보 캐시 키는 _prefetch_front_doors 가 저장한 키와 같아야 한다.
        #   대학 — normalize_key(university_base(name)): 같은 정문을 쓰는 단위로 묶는다.
        #   종합병원 — normalize_key(name): 이름 그대로(정규화만). university_base 로
        #     찾으면 「전북대학교병원」이 「전북대학교」 정문 후보로 오인 대체된다.
        candidate_base = university_base(place.name) if use_university_base else place.name
        candidates = tuple(
            MeasuredDoor(label, coords, _distance_m(coords, rings, center))
            for label, coords in self._front_door_candidates.get(
                normalize_key(candidate_base), ()
            )
        )

        def build(
            distance: float,
            tier: str,
            label: str,
            front_door_source: str,
            notice: str,
            facility_point: Coordinates | None,
            selected_point: Coordinates | None = None,
        ) -> CollectedFacility:
            cands = tuple(
                c._replace(
                    selected=(
                        selected_point is not None
                        and c.coordinates == selected_point
                    )
                )
                for c in candidates
            )
            return CollectedFacility(
                name=place.name,
                address=place.address,
                coordinates=place.coordinates,
                distance_m=distance,
                source_label=source_label,
                measurement_tier=tier,
                measurement_label=label,
                front_door_source=front_door_source,
                front_door_notice=notice,
                front_door_candidates=cands,
                nearest_facility_point=facility_point,
                nearest_boundary_point=(
                    _anchor_for(facility_point, rings) if facility_point else None
                ),
            )

        # 1순위 — 담당자 수기 지정(정문 필지/좌표).
        manual = (
            self.front_door_store.get(place.name)
            if self.front_door_store is not None
            else None
        )
        if manual is not None:
            return self._measure_from_ref(
                place, manual, rings, center, site_token, build, prefer_point=True
            )

        # 2순위(대학교) — 표준 데이터셋 정문 좌표. 대학알리미 + 수기 보완으로 정문을
        # 특정해 둔 값이라 지역검색 문 후보(부설학교·주차장 정문이 섞인다)보다 앞선다.
        # 캠퍼스 필지 경계를 정문 대용으로 쓰지 않으므로 통필지 상한도 필요 없다.
        if use_university_base:
            gate = dataset_gate_for(place.name, place.coordinates, self.dataset_gates)
            if gate is not None:
                return build(
                    _distance_m(gate.coordinates, rings, center),
                    "front_door_point",
                    f"({site_token} ↔ 정문 좌표)",
                    f"{DATASET_GATE_SOURCE} · {gate.label}",
                    "",
                    gate.coordinates,
                    selected_point=gate.coordinates,
                )

        # 3순위 — 네이버 문 후보. 기본은 사업지에 가장 가까운 문(#7·#11).
        if candidates:
            nearest = min(candidates, key=lambda c: c.distance_m)
            return build(
                nearest.distance_m,
                "front_door_point",
                f"({site_token} ↔ 정문/출구 좌표)",
                f"네이버 지역검색('{nearest.label}')",
                "",
                nearest.coordinates,
                selected_point=nearest.coordinates,
            )

        # 4순위 — 정류장 원장 자동 채택.
        auto = auto_front_door(place.name, stop_points, exclude_tokens=exclude_tokens)
        if auto is not None and auto.coordinates is not None:
            return build(
                _distance_m(auto.coordinates, rings, center),
                "front_door_point",
                f"({site_token} ↔ 정문 좌표)",
                auto.source_label,
                "",
                auto.coordinates,
            )

        # 5순위 — 정문 미확인: 좌표(대표점)로 보수 폴백하고 그 사실을 고지한다.
        return build(
            _distance_m(place.coordinates, rings, center),
            "coordinate",
            f"({site_token} ↔ 시설 좌표)",
            "",
            pending_notice,
            place.coordinates,
        )

    def _measure_from_ref(
        self,
        place: RawPlace,
        ref: FrontDoorRef,
        rings: list[list[Coordinates]],
        center: Coordinates,
        site_token: str,
        build,
        *,
        prefer_point: bool = False,
    ) -> CollectedFacility:
        """담당자 수기 지정(FrontDoorRef) 하나를 잰다.

        prefer_point(대학교)면 정문 좌표가 있을 때 그 점으로 잰다 — LH 기준이 「대학교 =
        정문(점)」이라, 필지까지 함께 지정돼도 캠퍼스 필지 경계(담장)로 재면 정문보다
        가까워진다. 좌표 없이 필지만 지정됐으면 담당자가 고른 그 필지 경계로 잰다.
        """

        if prefer_point and ref.coordinates is not None:
            return build(
                _distance_m(ref.coordinates, rings, center),
                "front_door_point",
                f"({site_token} ↔ 정문 좌표)",
                ref.source_label,
                "",
                ref.coordinates,
                selected_point=ref.coordinates,
            )

        # 정문 필지(PNU) 지정 — 필지경계로 잰다. 면적 상한은 두지 않는다.
        if ref.has_parcel:
            measured = self._measure_front_door_parcel(ref, rings, center)
            if measured is not None:
                distance, tier, label, notice, facility_point = measured
                return build(
                    distance, tier, label, ref.source_label, notice, facility_point
                )

        # 1순위(좌표형) — 정문 좌표만 지정됐거나, 필지 해석에 실패해 좌표로 폴백.
        coord = ref.coordinates
        if coord is not None:
            fallback_notice = (
                ""
                if not ref.has_parcel
                else "정문 필지를 지적도에서 조회하지 못해 정문 좌표 기준으로 잽니다."
            )
            return build(
                _distance_m(coord, rings, center),
                "front_door_point",
                f"({site_token} ↔ 정문 좌표)",
                ref.source_label,
                fallback_notice,
                coord,
                selected_point=coord,
            )

        # 정문 지정에 필지도 좌표도 없으면(불완전 지정) 좌표 폴백으로 되돌린다.
        return build(
            _distance_m(place.coordinates, rings, center),
            "coordinate",
            f"({site_token} ↔ 시설 좌표)",
            ref.source_label,
            "정문 지정에 필지·좌표가 모두 없어 시설 좌표로 폴백했습니다.",
            place.coordinates,
        )

    def _measure_front_door_parcel(
        self,
        ref: FrontDoorRef,
        rings: list[list[Coordinates]],
        center: Coordinates,
    ) -> tuple[float, str, str, str, Coordinates | None] | None:
        """정문 필지경계 기준 측정. (거리, tier, 표기, 고지, 시설측 최단점) 또는 None.

        면적 상한(구 통필지 5만㎡)은 없앴다(2026-09-28). 대학교 정문은 정문 좌표가
        있으면 _measure_from_ref 가 점으로 먼저 재므로, 여기는 담당자가 필지만 고른
        경우의 경계 측정이다.
        """

        store = self.cadastral_store
        if store is None:
            return None
        parcel = store.parcel_by_pnu(ref.pnu)
        if parcel is None or len(parcel.ring) < 4:
            return None

        site_token = "대지경계" if rings else "주소점"
        # 정상 필지 — 사업지 대지경계 ↔ 정문 필지경계.
        facility_point: Coordinates | None = None
        if rings:
            measured = distance_polygons_to_polygon_m(rings, parcel.ring)
            distance = measured.distance_m
            facility_point = measured.nearest_b
        else:
            distance = distance_point_to_polygon_m(center, parcel.ring)
        pnu_note = f"정문 필지 PNU {parcel.pnu}"
        if parcel.jibun:
            pnu_note += f" · 지번 {parcel.jibun}"
        return (
            distance,
            "front_door_parcel",
            f"({site_token} ↔ 정문 필지경계 / 시설 정문)",
            pnu_note,
            facility_point,
        )

    # -- LH 개별 맞춤(lh_alignments.json) -------------------------------------
    def _alignments(self) -> tuple[LhAlignment, ...]:
        if self._fixed_alignments is not None:
            return tuple(e for e in self._fixed_alignments if e.scope == "amenity")
        return amenity_alignments()

    async def _apply_lh_alignments(
        self,
        collections: dict[str, GroupCollection],
        rings: list[list[Coordinates]],
        center: Coordinates,
        radius_m: int,
    ) -> dict[str, GroupCollection]:
        """공공 API 로 모은 시설 중 LH 데이터셋과 다른 곳을 LH 기준으로 맞춘다.

        경계 측정(_attach_boundaries)이 끝난 뒤 마지막에 한 번 적용한다. 맞춘 시설에는
        lh_alignment 한 줄을 붙여 화면·엑셀에 「LH 개별 맞춤」으로 드러낸다. 사업지에서
        먼 항목(기준점이 반경 + 이름 맞춤 거리 밖)은 건드리지 않는다.
        """

        entries = [
            entry
            for entry in self._alignments()
            if entry.group in collections
            and (
                entry.lh.coordinates is None
                or haversine_meters(center, entry.lh.coordinates)
                <= radius_m + NAME_MATCH_MARGIN_M
            )
        ]
        if not entries:
            return collections

        updated = dict(collections)
        # 시설군 키 → 맞춘 뒤 시설 목록(자르기 전). 끝에 한 번에 거리 목록을 다시 만든다.
        working: dict[str, list[CollectedFacility]] = {}
        parcels: dict[str, ParcelFeature | None] = {}

        def facilities_of(key: str) -> list[CollectedFacility]:
            if key not in working:
                working[key] = list(updated[key].facilities)
            return working[key]

        for entry in entries:
            collection = updated[entry.group]
            # 원천이 아예 없는 시설군에 LH 시설만 끼워 넣으면 「찾았다」로 읽힌다. 건드리지 않는다.
            if collection.state == "missing":
                continue
            facilities = facilities_of(entry.group)
            matched = [
                index
                for index, facility in enumerate(facilities)
                if entry.matches(facility.name, facility.coordinates)
            ]
            action = entry.action
            if action == "exclude":
                dropped = set(matched)
                working[entry.group] = [
                    f for i, f in enumerate(facilities) if i not in dropped
                ]
                continue
            if action == "rename":
                for index in matched:
                    facility = facilities[index]
                    facilities[index] = facility._replace(
                        name=entry.lh_name,
                        lh_alignment=_alignment_note(entry, facility.name),
                    )
                continue
            if action == "point":
                for index in matched:
                    facilities[index] = await self._measure_at_lh(
                        facilities[index], entry, rings, center, parcels
                    )
                continue
            # add · merge — 있으면 LH 위치로 맞추고, 없으면 LH 시설을 더한다.
            if matched:
                for index in matched:
                    facilities[index] = (
                        await self._measure_at_lh(
                            facilities[index], entry, rings, center, parcels
                        )
                    )._replace(name=entry.lh_name)
                present = True
            else:
                added = await self._lh_facility(entry, rings, center, parcels)
                present = added is not None and added.distance_m <= radius_m
                if present:
                    facilities.append(added)
            if action == "merge" and present:
                for source in entry.merge_from:
                    if source.group not in updated:
                        continue
                    source_list = facilities_of(source.group)
                    working[source.group] = [
                        f
                        for f in source_list
                        if not entry.matches_merge_source(source.group, f.name, f.coordinates)
                    ]

        for key, facilities in working.items():
            collection = updated[key]
            ordered = sorted(facilities, key=lambda f: f.distance_m)
            if key == "university":
                # 맞춘 정문이 같아진 학교(원광대·원광디지털대)는 다시 한 줄로 합친다.
                ordered = _collapse_same_gate(ordered)
                if SAFEMAP_UNIVERSITY_SOURCE in collection.actual_source:
                    # LH 정문으로 옮긴 뒤 반경 밖이 된 학교도 뺀다(_build_group 과 같은 규칙).
                    ordered = [f for f in ordered if f.distance_m <= radius_m]
            updated[key] = collection._replace(
                facilities=tuple(ordered[:MAX_HITS_PER_GROUP]),
                distances_m=_replace_distances(
                    collection.distances_m, collection.facilities, ordered
                ),
            )
        return updated

    async def _lh_parcel(
        self, pnu: str, cache: dict[str, ParcelFeature | None]
    ) -> ParcelFeature | None:
        if pnu not in cache:
            parcel = (
                await self._parcel_by_pnu(pnu) if self._boundary_source_ready() else None
            )
            cache[pnu] = parcel if parcel is not None and len(parcel.ring) >= 4 else None
        return cache[pnu]

    async def _measure_at_lh(
        self,
        facility: CollectedFacility,
        entry: LhAlignment,
        rings: list[list[Coordinates]],
        center: Coordinates,
        parcels: dict[str, ParcelFeature | None],
    ) -> CollectedFacility:
        """시설 한 곳을 LH 기준점으로 다시 잰다. PNU 가 있으면 그 필지 경계, 없으면 LH 좌표.

        LH 가 지정한 필지는 지목(철도용지 등)과 관계없이 그대로 쓴다 — 익산역 필지(지목
        철)를 대중교통터미널로 재는 것이 LH 데이터셋 기준이다.
        """

        site_token = "대지경계" if rings else "주소점"
        note = _alignment_note(entry, facility.name)
        point = entry.lh.coordinates
        fallback = ""
        # 문 후보는 참고로 남기되, 잰 기준점이 LH 값으로 바뀌었으니 선택 표시는 지운다.
        facility = facility._replace(
            front_door_candidates=tuple(
                c._replace(selected=False) for c in facility.front_door_candidates
            )
        )
        if entry.lh.pnu:
            parcel = await self._lh_parcel(entry.lh.pnu, parcels)
            if parcel is not None:
                if rings:
                    measured = distance_polygons_to_polygon_m(rings, parcel.ring)
                    distance = measured.distance_m
                    facility_point: Coordinates | None = measured.nearest_b
                    boundary_point: Coordinates | None = measured.nearest_a
                else:
                    distance = distance_point_to_polygon_m(center, parcel.ring)
                    facility_point = None
                    boundary_point = None
                notice = f"LH 데이터셋 필지 PNU {parcel.pnu or entry.lh.pnu}"
                if parcel.jibun:
                    notice += f" · 지번 {parcel.jibun}"
                return facility._replace(
                    distance_m=distance,
                    measurement_tier="site_boundary",
                    measurement_label=f"({site_token} ↔ LH 필지 경계)",
                    front_door_source=f"{LH_ALIGNMENT_SOURCE} · {entry.lh_name}",
                    front_door_notice=notice,
                    nearest_facility_point=facility_point or facility.nearest_facility_point,
                    nearest_boundary_point=boundary_point or facility.nearest_boundary_point,
                    facility_ring=tuple(parcel.ring),
                    lh_alignment=note,
                )
            fallback = "LH 필지를 조회하지 못해 LH 좌표로 쟀습니다."
        if point is None:
            return facility._replace(lh_alignment=note, front_door_notice=fallback)
        tier = "front_door_point" if entry.group == "university" else "coordinate"
        return facility._replace(
            distance_m=_distance_m(point, rings, center),
            measurement_tier=tier,
            measurement_label=f"({site_token} ↔ LH 좌표)",
            front_door_source=f"{LH_ALIGNMENT_SOURCE} · {entry.lh_name}",
            front_door_notice=fallback,
            nearest_facility_point=point,
            nearest_boundary_point=_anchor_for(point, rings),
            facility_ring=(),
            lh_alignment=note,
        )

    async def _lh_facility(
        self,
        entry: LhAlignment,
        rings: list[list[Coordinates]],
        center: Coordinates,
        parcels: dict[str, ParcelFeature | None],
    ) -> CollectedFacility | None:
        """공공 API 에 없는 LH 시설 한 곳을 만든다. 기준점이 없으면 None."""

        point = entry.lh.coordinates
        if point is None:
            return None
        base = CollectedFacility(
            name=entry.lh_name,
            address=entry.lh.address,
            coordinates=point,
            distance_m=_distance_m(point, rings, center),
            source_label=LH_ALIGNMENT_SOURCE,
            nearest_facility_point=point,
            nearest_boundary_point=_anchor_for(point, rings),
        )
        measured = await self._measure_at_lh(base, entry, rings, center, parcels)
        return measured._replace(lh_alignment=_alignment_note(entry, ""))

    # -- 캐시 --------------------------------------------------------------
    def _cache_key(
        self,
        rings: Sequence[Sequence[Coordinates]],
        center: Coordinates,
        radius_m: int,
    ) -> str:
        # 거리는 필지 경계에 따라 달라지므로 경계 지문도 키에 넣는다.
        fingerprint = ";".join(
            f"{len(ring)}:{ring[0].lat:.6f},{ring[0].lng:.6f}" for ring in rings if ring
        )
        # 정문 지정이 바뀌면 캐시된 거리가 낡는다. 지정 리비전을 키에 넣어 새 지정이
        # 다음 검토부터(캐시 TTL 을 기다리지 않고) 반영되게 한다(§3-3).
        revision = self.front_door_store.revision() if self.front_door_store else 0
        # 관리자 판정 옵션(운행주기·문화시설 범위 등)이 바뀌면 시설 목록이 달라진다.
        options = options_fingerprint()
        # LH 개별 맞춤 항목이 바뀌면 맞춘 거리·이름이 달라진다.
        aligned = (
            alignments_fingerprint()
            if self._fixed_alignments is None
            else ",".join(entry.id for entry in self._fixed_alignments)
        )
        return (
            f"{center.lat:.5f},{center.lng:.5f}:{radius_m}:{fingerprint}:fd{revision}:"
            f"{options}:lh{aligned}"
        )

    def _cache_get(self, key: str) -> dict[str, GroupCollection] | None:
        cached = self._cache.get(key)
        if not cached:
            return None
        expires_at, value = cached
        if expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        return value

    def _cache_set(self, key: str, value: dict[str, GroupCollection]) -> None:
        self._cache[key] = (time.monotonic() + self.cache_ttl_seconds, value)


def _places(
    rows: Sequence[dict[str, Any]],
    parse: Callable[[dict[str, Any]], RawPlace | None],
) -> tuple[RawPlace, ...]:
    parsed = (parse(row) for row in rows if isinstance(row, dict))
    return tuple(place for place in parsed if place is not None and place.name)


def _apply_bus_headway(
    facilities: Sequence[CollectedFacility],
    headways: dict[str, StopHeadway],
) -> list[CollectedFacility]:
    """정류장마다 운행주기 판정을 붙이고, 인정 정류장이 앞에 오도록 정렬한다.

    배차간격을 확인한 정류장만 거른다 — 확인했는데 미달이면 counted=False 로 남겨
    화면에는 보이되 배점에서 뺀다. 배차를 확인할 수 없는 정류장(원천이 배차를 주지
    않는 지역·이름 불일치)은 센다. 자료 부재로 정류장을 없는 것으로 접지 않는다
    (2026-09-25 결정 — 참조 앱은 운행주기를 보지 않는다).
    """

    tagged: list[CollectedFacility] = []
    # 기본(운행주기 기준 끔)은 LH 내부망 앱처럼 모든 정류장을 센다. 관리자 설정에서 켜면
    # 아래에서 15분 기준 미달 정류장을 배점에서 뺀다.
    # 판정 근거로 배차 정보는 그대로 적어 둔다.
    if not option_enabled("bus_headway_filter"):
        for facility in facilities:
            headway = headways.get(_stop_name_key(facility.name))
            note = headway.label if headway is not None else "운행주기 판정 정보 없음"
            tagged.append(
                facility._replace(counted=True, count_note=f"운행주기 미적용(LH 기준) · {note}")
            )
        return sorted(tagged, key=lambda f: f.distance_m)
    for facility in facilities:
        headway = headways.get(_stop_name_key(facility.name))
        if headway is None or not headway.determined:
            note = headway.label if headway is not None else "운행주기 판정 정보 없음"
            tagged.append(facility._replace(counted=True, count_note=note))
            continue
        tagged.append(
            facility._replace(counted=headway.qualifies, count_note=headway.label)
        )
    return sorted(tagged, key=lambda f: (not f.counted, f.distance_m))


def _stop_points(outcome: "FeedResult | BaseException | None") -> list[tuple[str, Coordinates]]:
    """버스정류장 피드에서 (정류장명, 좌표) 목록을 뽑는다. 정문 자동 채택 후보 원천이다."""

    if not isinstance(outcome, FeedResult):
        return []
    return [(place.name, place.coordinates) for place in outcome.places if place.name]


def _front_door_group_notice(facilities: Sequence[CollectedFacility]) -> str:
    """시설군 헤더에 올릴 정문 기준점 고지. 시설별 고지를 중복 없이 모은다(대학·병원)."""

    seen: list[str] = []
    for facility in facilities:
        note = facility.front_door_notice
        if note and note not in seen:
            seen.append(note)
    return " / ".join(seen)


def _dedupe_doors(
    doors: Sequence[tuple[str, Coordinates]],
) -> list[tuple[str, Coordinates]]:
    """같은 출구(좌표 ≈ 같음)가 카카오·네이버에서 겹치면 먼저 온 것만 남긴다."""

    kept: list[tuple[str, Coordinates]] = []
    for label, coords in doors:
        if any(haversine_meters(coords, seen) <= 15 for _, seen in kept):
            continue
        kept.append((label, coords))
    return kept


def _collapse_same_gate(
    facilities: Sequence[CollectedFacility],
) -> list[CollectedFacility]:
    """같은 정문으로 잰 대학 시설(본교·대학원·단과대학)을 한 곳으로 합친다.

    지도 검색은 「전북대학교 대학원」·「전북대학교 환경생명자원대학」처럼 한 캠퍼스의
    부속을 따로 준다. 정문을 공유하므로 거리선이 정확히 겹쳐 시설군 건수(7)와 지도에
    보이는 선(3)이 어긋났다. 내부망 앱도 대학은 캠퍼스 정문 하나로 센다. 대표 이름은
    캠퍼스 단위 이름(university_base 가 그대로인 것)을, 없으면 가장 짧은 이름을 쓴다.
    """

    groups: dict[tuple[float, float], list[CollectedFacility]] = {}
    order: list[tuple[float, float] | int] = []
    singles: dict[int, CollectedFacility] = {}
    for index, facility in enumerate(facilities):
        point = facility.nearest_facility_point
        if facility.measurement_tier.startswith("front_door") and point is not None:
            key = (round(point.lat, 5), round(point.lng, 5))
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(facility)
        else:
            singles[index] = facility
            order.append(index)
    collapsed: list[CollectedFacility] = []
    for key in order:
        if isinstance(key, int):
            collapsed.append(singles[key])
            continue
        members = groups[key]
        nearest = min(members, key=lambda f: f.distance_m)
        label = min(
            members,
            key=lambda f: (university_base(f.name) != f.name.strip(), len(f.name)),
        )
        collapsed.append(nearest._replace(name=label.name, address=label.address))
    return sorted(collapsed, key=lambda facility: facility.distance_m)


def _is_market(facility: CollectedFacility) -> bool:
    return facility.source_label == market_source.MARKET_SOURCE


def _market_facilities(
    outcome: FeedResult,
    stores: Sequence[Any],
    rings: list[list[Coordinates]],
    center: Coordinates,
) -> list[CollectedFacility]:
    """전통시장 feed → 상업시설 시설. 대규모점포와 40m 안에 겹치는 시장은 뺀다."""

    records = market_source.drop_duplicate_markets(
        (
            market_source.MarketRecord(p.name, "", p.address, "", p.coordinates)
            for p in outcome.places
        ),
        [store.coordinates for store in stores],
    )
    return [
        CollectedFacility(
            name=record.name,
            address=record.address,
            coordinates=record.coordinates,
            distance_m=_distance_m(record.coordinates, rings, center),
            source_label=market_source.MARKET_SOURCE,
            nearest_facility_point=record.coordinates,
            nearest_boundary_point=_anchor_for(record.coordinates, rings),
        )
        for record in records
    ]


def _with_source_alert(
    collection: GroupCollection,
    results: dict[str, "FeedResult | BaseException"],
) -> GroupCollection:
    """시설군을 채운 원천의 장애·대체 경고를 모아 싣는다(심사 결과 상단 경고용)."""

    spec = GROUP_SPECS.get(collection.key)
    # 시설군 조립 단계에서 이미 단 경고(문화시설 원장 미적재 등)는 지우지 않고 잇는다.
    messages: list[str] = [collection.source_alert] if collection.source_alert else []
    for feed in spec.feeds if spec else ():
        outcome = results.get(feed)
        if isinstance(outcome, SourceMissing):
            continue  # 원천이 아예 없는 것은 장애가 아니다. note 로 이미 드러난다.
        if isinstance(outcome, BaseException):
            label = FEED_LABELS.get(feed, feed)
            if collection.state == "missing":
                messages.append(
                    f"{label} 원천 조회가 실패했습니다({outcome}). 이 시설군은 산정하지 못했습니다."
                )
            else:
                messages.append(
                    f"{label} 원천 조회가 실패했습니다({outcome}). 이 시설군은 나머지 원천으로만 "
                    "셌으니 빠진 시설이 있을 수 있습니다. 원천이 복구되면 다시 심사하세요."
                )
        elif isinstance(outcome, FeedResult) and outcome.alert:
            messages.append(outcome.alert)
    if (
        collection.key == "bus_stop"
        and collection.state != "missing"
        and not collection.distances_m
    ):
        messages.append(BUS_EMPTY_ALERT)
    if not messages:
        return collection
    return collection._replace(source_alert=" ".join(dict.fromkeys(messages)))


def is_planned_facility(name: str) -> bool:
    """아직 짓지 않은 시설인가. 지도 POI 에는 「한국문화원형콘텐츠체험전시관 (2027년예정)」
    처럼 개관 전 시설도 올라온다. 없는 시설로 등급을 올리면 안 되고, 내부망 앱 표준
    데이터셋(공연장·미술관·박물관·영화상영관 원장)에도 없으므로 2차 편의시설에서 뺀다
    (사용자 결정 2026-09-28).
    """

    return "예정" in (name or "")


def _alignment_note(entry: LhAlignment, api_name: str) -> str:
    """맞춘 시설에 붙는 한 줄. 이름을 바꿨으면 공공 API 표기도 함께 남긴다."""

    note = entry.note()
    if api_name and normalize_key(api_name) != normalize_key(entry.lh_name):
        note += f" (공공 API 표기 「{api_name}」)"
    return note


def _replace_distances(
    distances: Sequence[float],
    before: Sequence[CollectedFacility],
    after: Sequence[CollectedFacility],
) -> tuple[float, ...]:
    """등급 판정용 거리 목록에서 화면 시설(before)의 거리를 빼고 맞춘 뒤 거리(after)로 바꾼다.

    거리 목록은 화면 20곳 밖까지 전부 담는다. 화면 시설 몫만 값으로 한 번씩 지워 바꾸고
    나머지는 그대로 둔다. 배점에 세지 않는 시설(운행주기 미달 정류장)은 원래 목록에 없다.
    """

    remaining = list(distances)
    for facility in before:
        if not facility.counted:
            continue
        try:
            remaining.remove(facility.distance_m)
        except ValueError:
            continue
    remaining.extend(f.distance_m for f in after if f.counted)
    return tuple(sorted(remaining))


def _dedupe(places: Sequence[RawPlace]) -> list[RawPlace]:
    """이름+주소가 같으면 같은 시설로 본다. 키워드 검색을 겹쳐 쓰면 반드시 겹친다."""

    seen: set[tuple[str, str]] = set()
    unique: list[RawPlace] = []
    for place in places:
        signature = (place.name.replace(" ", ""), place.address.replace(" ", ""))
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(place)
    return unique


_ROOM_SUFFIX_RE = re.compile(r"\s*\([^()]*실\)\s*$")


def _strip_room_suffix(name: str) -> str:
    """표준데이터가 도서관 방마다 따로 실은 줄의 방 이름을 뗀다. 「군산시립도서관(학습실)」→「군산시립도서관」."""

    return _ROOM_SUFFIX_RE.sub("", name or "").strip() or (name or "").strip()


def _merge_library_sources(
    standard: Sequence[RawPlace], vworld: Sequence[VWorldPlace]
) -> list[RawPlace]:
    """표준데이터 공공도서관과 VWorld 공공도서관을 한 목록으로 합친다(_standard_libraries 참조).

    짝짓기: 이름(정규화 뒤 한쪽이 다른 쪽을 품고 LIBRARY_NAME_MATCH_M 안, 가장 가까운 것) →
    남은 것끼리 같은 자리(LIBRARY_SAME_SPOT_M). 짝이 있으면 VWorld 이름·좌표, 없으면 각자.
    """

    def key(name: str) -> str:
        return re.sub(r"[\s()\[\]·.,\-]", "", name or "")

    pair: dict[int, int] = {}
    used: set[int] = set()
    for i, known in enumerate(standard):
        k = key(known.name)
        candidates = [
            (haversine_meters(known.coordinates, p.coordinates), j)
            for j, p in enumerate(vworld)
            if j not in used
            and k
            and (k in key(p.title) or key(p.title) in k)
            and haversine_meters(known.coordinates, p.coordinates) <= LIBRARY_NAME_MATCH_M
        ]
        if candidates:
            j = min(candidates)[1]
            pair[i] = j
            used.add(j)
    for i, known in enumerate(standard):
        if i in pair:
            continue
        candidates = [
            (haversine_meters(known.coordinates, p.coordinates), j)
            for j, p in enumerate(vworld)
            if j not in used
            and haversine_meters(known.coordinates, p.coordinates) <= LIBRARY_SAME_SPOT_M
        ]
        if candidates:
            j = min(candidates)[1]
            pair[i] = j
            used.add(j)
    merged: list[RawPlace] = []
    for i, known in enumerate(standard):
        if i in pair:
            p = vworld[pair[i]]
            merged.append(known._replace(name=p.title, coordinates=p.coordinates))
        else:
            merged.append(known)
    merged.extend(
        RawPlace(p.title, p.address, p.category, p.coordinates)
        for j, p in enumerate(vworld)
        if j not in used
    )
    return merged


def _merge_same_place(
    places: Sequence[RawPlace], within_m: float = VWORLD_SAME_PLACE_M
) -> list[RawPlace]:
    """같은 이름이 가까이(within_m) 여러 번 실린 장소를 하나로 둔다(먼저 온 것).

    VWorld 는 한 터미널을 분류만 달리해 세 번(시외·고속·종합) 싣고, 지하철역은
    「군자(능동)역」·「군자역(능동)」처럼 표기만 다른 두 점을 싣는다. 이름이 다르면
    같은 자리여도 따로 둔다(정읍고속버스터미널·정읍시외버스공용터미널).
    """

    kept: list[RawPlace] = []
    for place in places:
        key = normalize_key(place.name)
        if any(
            normalize_key(other.name) == key
            and haversine_meters(other.coordinates, place.coordinates) <= within_m
            for other in kept
        ):
            continue
        kept.append(place)
    return kept


def _distance_m(
    point: Coordinates,
    rings: Sequence[Sequence[Coordinates]],
    center: Coordinates,
) -> float:
    """사업부지 경계 → 시설 기준점 직선거리. 경계가 없으면 주소점에서 잰다."""

    if rings:
        return distance_point_to_polygons_m(point, rings)
    return haversine_meters(center, point)


def _measure_to_parcel(
    facility: CollectedFacility,
    parcel: ParcelFeature | None,
    rings: Sequence[Sequence[Coordinates]],
    center: Coordinates,
) -> CollectedFacility:
    """시설 한 곳을 필지 경계 기준으로 바꾼 새 값. 못 바꾸면 사유만 적어 돌려준다.

    필지 면적 상한은 두지 않는다 — 대형 필지도 끝점(경계선)으로 잰다.
    """

    if parcel is None:
        return facility._replace(front_door_notice=BOUNDARY_FALLBACK_NOTICE)
    rejection = parcel_rejection_reason(parcel.jibun)
    if rejection:
        # 좌표가 도로·하천 필지 위에 떨어진 경우(지도 POI 가 시설 앞 도로에 찍힘).
        # 그 필지를 경계로 쓰면 도로망 전체가 시설이 된다. 좌표로 잰다.
        return facility._replace(front_door_notice=f"{rejection} — 시설 좌표로 쟀습니다.")
    site_token = "대지경계" if rings else "주소점"
    if rings:
        measured = distance_polygons_to_polygon_m(rings, parcel.ring)
        distance = measured.distance_m
        facility_point: Coordinates | None = measured.nearest_b
        boundary_point: Coordinates | None = measured.nearest_a
    else:
        distance = distance_point_to_polygon_m(center, parcel.ring)
        facility_point = None
        boundary_point = None
    notice = f"시설 필지 PNU {parcel.pnu}" if parcel.pnu else "시설 필지"
    if parcel.jibun:
        notice += f" · 지번 {parcel.jibun}"
    return facility._replace(
        distance_m=distance,
        measurement_tier="site_boundary",
        measurement_label=f"({site_token} ↔ 시설 경계)",
        front_door_notice=notice,
        nearest_facility_point=facility_point or facility.nearest_facility_point,
        nearest_boundary_point=boundary_point or facility.nearest_boundary_point,
        facility_ring=tuple(parcel.ring),
    )


def _containing_parcel(
    parcels: Sequence[ParcelFeature], point: Coordinates
) -> ParcelFeature | None:
    """이미 경계로 잰 필지 중 점을 품는 것. 도형 이상치는 없는 것으로 본다."""

    for parcel in parcels:
        try:
            if polygon_contains(parcel.ring, point):
                return parcel
        except Exception:  # 지적 도형 이상치 방어 — 좌표 측정으로 남는다
            continue
    return None


def _anchor_for(
    point: Coordinates,
    rings: Sequence[Sequence[Coordinates]],
) -> Coordinates | None:
    """최단거리선이 시작하는 대지경계 위의 점(#5). 경계가 없으면 None(주소점 기준)."""

    if not rings:
        return None
    try:
        return nearest_boundary_point_multi(rings, point)
    except Exception:  # 지적 도형 이상치 방어 — 선을 못 그어도 판정은 계속한다
        return None
