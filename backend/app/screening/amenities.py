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
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NamedTuple

from app.models import Coordinates
from app.screening.front_door import (
    _AUTO_EXCLUDE_TOKENS,
    CAMPUS_PARCEL_MAX_AREA_M2,
    FACILITY_DOOR_TOKENS,
    HOSPITAL_EXCLUDE_TOKENS,
    STATION_DOOR_TOKENS,
    FrontDoorRef,
    FrontDoorStore,
    auto_front_door,
    collect_door_candidates,
    normalize_key,
    university_base,
)
from app.screening.scorebook import FACILITY_GROUPS
from app.services.cadastral_local import CadastralLocalStore
from app.services.facility_store import FacilityStore
from app.services.geo import (
    distance_point_to_polygon_m,
    distance_point_to_polygons_m,
    distance_polygons_to_polygon_m,
    haversine_meters,
    nearest_boundary_point_multi,
    offset_coordinates,
)
from app.services.kakao import KakaoClient
from app.services.naver_search import NaverSearchClient
from app.services.ncmc_hospital import NcmcHospitalClient
from app.services.tago import TagoClient
from app.services.vworld import ParcelFeature, VWorldClient


# 등급 조건이 쓰는 최대 반경은 3km(주거여건 3km)다. 그보다 넓게 볼 이유가 없다.
MAX_RADIUS_M = 3000

# 카카오 로컬 API 한 질의당 하드 상한(3페이지 x 15건). 여기 닿으면 잘린 것이다.
KAKAO_RESULT_CAP = 45

# 사분면 재귀 깊이. 2 면 한 시설군당 최대 1+4+16=21 회로 묶인다.
MAX_SUBDIVIDE_DEPTH = 2

# 화면에 근거로 나열할 시설 수 상한. 세는 데 쓰는 거리 목록은 자르지 않는다.
MAX_HITS_PER_GROUP = 20

# 시설 경계(필지)에서 재는 시설군(docs/hazards/MEASUREMENT.md §3). 역·지하철은
# 출입구, 대학·종합병원은 정문이라 여기 없다. 버스정류장은 정류장이 속한 필지의
# 경계에서 잰다(2026-09-14 사용자 결정).
BOUNDARY_GROUPS: frozenset[str] = frozenset(
    {
        "bus_stop",
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
# 시설군마다 필지를 조회할 최근접 시설 수. 경계로 재면 거리는 줄기만 하므로
# 등급 판정에 쓰이는 최근접 몇 곳만 정확히 재면 되고, 그 뒤는 점 거리로 둔다.
BOUNDARY_LOOKUP_PER_GROUP = 5
# 통필지 안전장치가 적용되지 않는 시설군. 버스정류장은 도로 필지에 놓이므로
# 면적 임계로 거르면 대부분 좌표로 되돌아가 결정과 어긋난다.
BOUNDARY_NO_AREA_GUARD: frozenset[str] = frozenset({"bus_stop"})
BOUNDARY_FALLBACK_NOTICE = "시설 경계(필지)를 확인하지 못해 시설 좌표로 쟀습니다."

# 담당자 수기 기준점 지정을 허용하는 역·터미널 계열 시설군(#11 「출구 여럿이면
# 담당자 선택」). 프런트 DESIGNATABLE_GROUPS 의 역 계열과 같은 집합이다.
_STATION_LIKE_DESIGNATABLE = ("railway", "subway", "terminal", "transfer")

KAKAO_DISABLED_NOTE = "카카오 REST API 키가 설정되지 않았습니다."

# 원천이 아예 없는 시설군의 고지. 조회 실패가 아니라 '원천 부재'다.
TRANSFER_MISSING_NOTE = (
    "환승시설(간선급행버스체계법 제2조제3호다목) 위치 원천을 확보하지 못했습니다."
)

# 상업시설은 심사표가 조회처를 못박은 항목이라 근사하지 않는다. 대규모점포
# 원장이 적재돼 있으면 그것으로 산정하고, 미적재면 '없음'이 아니라 '미적재'로
# 남긴다(룰북 원칙: 원천 부재를 0 으로 단정하지 않는다).
RETAIL_MISSING_NOTE = (
    "심사표는 대규모점포 조회(localdata)와 전통시장통통 등재분만 인정합니다. "
    "대규모점포 원장이 아직 적재되지 않아 산정하지 않습니다."
)

# 대규모점포 원장으로 산정할 때의 고지. 전통시장(소상공인시장진흥공단
# 전통시장통통)은 아직 CSV 미적재라 대규모점포만 반영한 사실을 밝힌다.
RETAIL_CONNECTED_NOTE = (
    "행정안전부 대규모점포 인허가 원장으로 산정했습니다. "
    "전통시장(소상공인시장진흥공단 전통시장통통)은 아직 원장을 확보하지 못해 "
    "대규모점포만 반영했습니다."
)

KAKAO_PLACE_SOURCE = "카카오 장소검색"
TAGO_SOURCE = "국토교통부 TAGO 정류소 근접조회"
LOCALDATA_SOURCE = "행정안전부 지방행정인허가 대규모점포"
NCMC_HOSPITAL_SOURCE = "국립중앙의료원 전국 병·의원 찾기(종합병원)"

# 정문 미지정 대학 캠퍼스에 붙이는 지정 대기 고지(국장님 §3-3). 좌표 폴백을
# 쓰되 그 사실을 감추지 않는다.
FRONT_DOOR_PENDING_NOTICE = (
    "대학 캠퍼스 통필지 — 정문 기준점 지정 대기(현재 좌표 기준 보수 폴백)"
)

# 종합병원 등 대형 필지 시설의 정문 미확인 폴백 고지(#8, LH 확정 2026-09-11).
# 대형 필지 시설은 정문을 기본 기준점으로 재되, 확인 전에는 좌표로 보수 폴백한다.
HOSPITAL_FRONT_DOOR_PENDING_NOTICE = (
    "정문 미확인 — 시설 좌표 기준"
    "(LH 확정 2026-09-11: 대형 필지 시설은 정문 기본)"
)

# 국립중앙의료원 원장으로 종합병원을 산정할 때의 고지. 상급종합병원 인정 여부는
# LH 미확정이라 함께 계산하되 구분한다.
HOSPITAL_NCMC_NOTE = (
    "국립중앙의료원 전국 병·의원 원장에서 종류=종합병원만 산정했습니다. "
    "상급종합병원 인정 여부는 LH 미확정이라 함께 계산하되 구분해 둡니다."
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
    {"도서관", "국공립도서관", "작은도서관", "전문도서관", "어린이도서관"}
)

# 심평원 '종류=종합병원' 을 대신하는 근사 기준. 상급종합병원은 카카오에서
# '대학병원' 으로 분류되는데, LH 원장(전북 9건)에는 빠져 있다. 상급종합을
# 의료시설로 볼지는 LH 확인 대상이라 우선 함께 세고 note 로 밝힌다.
HOSPITAL_CATEGORIES: frozenset[str] = frozenset(
    {"종합병원", "상급종합병원", "대학병원"}
)

SCHOOL_TOKENS: tuple[str, ...] = ("초등학교", "중학교", "고등학교")

# 고등교육기관으로 보는 카카오 「학교」 하위 분류.
#
# 「대학교」로 시작하는 것만 받으면 같은 성격의 학교가 원천 표기에 따라 갈린다.
# 실측(2026-09-11): 전주비전대학교는 「학교 > 대학교」, 전북과학대학교·군산간호
# 대학교·한국폴리텍대학 익산캠퍼스는 「학교 > 전문대학」으로 온다. 셋 다 고등
# 교육기관인데 앞의 하나만 잡히고 있었다. 판정이 분류 표기의 흔들림에 좌우되면
# 안 된다.
#
# 이 목록에 「전문대학」이 들어오면서 기능대학(한국폴리텍)도 함께 잡힌다.
# 기능대학을 대학교로 볼지는 LH 확인 요청 7번(2026-09-11 회의)으로 미결이다.
# 「제외」로 회신되면 이름 예외를 여기 한 줄로 붙인다 — 지금처럼 분류 문자열
# 매칭의 부작용으로 조용히 빠지는 상태로 두지 않는다.
UNIVERSITY_KINDS: tuple[str, ...] = ("대학교", "대학원", "전문대학")


class RawPlace(NamedTuple):
    """원천에서 받은 시설 한 곳. 거리 계산 전 단계."""

    name: str
    address: str
    category_name: str
    coordinates: Coordinates


class FeedResult(NamedTuple):
    """원천 호출 하나의 결과. 어떤 원천이 답했는지 함께 들고 다닌다."""

    places: tuple[RawPlace, ...]
    source_label: str


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


def _is_railway(place: RawPlace) -> bool:
    # "기차역" 키워드 검색은 역 앞 상가까지 물어 온다. 이름이 '역'으로 끝나는 것만 남긴다.
    return place.name.endswith("역")


def _category_leaf(place: RawPlace) -> str:
    """카카오 category_name 의 마지막 조각. 이것이 실제 업종 분류다."""

    return place.category_name.split(">")[-1].strip()


def _is_general_hospital(place: RawPlace) -> bool:
    return _category_leaf(place) in HOSPITAL_CATEGORIES


def _is_park(place: RawPlace) -> bool:
    return _category_leaf(place) in PARK_CATEGORIES


def _is_public(place: RawPlace) -> bool:
    """공공시설 = PO3 공공기관 전부 + 도서관 분류만.

    '도서관' 키워드 검색은 영어학원·화장실까지 물어 온다. 공공기관 카테고리
    검색분은 분류가 제각각(시청·파출소·행정복지센터)이라 통과시키고, 도서관
    키워드에서 온 것만 분류로 거른다.
    """

    leaf = _category_leaf(place)
    if leaf in LIBRARY_CATEGORIES:
        return True
    # 도서관 키워드로만 걸리는 오탐을 막는다. 이름에 '도서관' 이 있는데 분류가
    # 도서관이 아니면 학원·화장실 계열이다.
    return "도서관" not in place.name


def _school_filter(token: str) -> Callable[[RawPlace], bool]:
    return lambda place: token in place.name


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
    "subway": GroupSpec(
        ("subway",),
        "connected",
        "역 출입구 좌표를 네이버 지역검색으로 확인해 기준점으로 씁니다. "
        "출입구를 찾지 못한 역은 역 대표점으로 잽니다.",
    ),
    "railway": GroupSpec(
        ("railway",),
        "substituted",
        "한국철도공사 역위치 정보 대신 지도 검색으로 근사했습니다. "
        "출입구 좌표는 네이버 지역검색으로 확인해 기준점으로 쓰고, "
        "찾지 못한 역은 역 대표점으로 잽니다.",
        keep=_is_railway,
    ),
    "bus_stop": GroupSpec(
        ("bus_stop",),
        "substituted",
        "운행주기 15분 이내 요건을 확인할 수 있는 원천이 없어 전체 정류장을 셌습니다.",
        kakao_backed=False,
    ),
    "terminal": GroupSpec(
        ("terminal", "express_terminal"),
        "substituted",
        "대중교통수단 터미널 정보 대신 지도 검색으로 근사했습니다.",
    ),
    "transfer": GroupSpec((), "missing", TRANSFER_MISSING_NOTE, kakao_backed=False),
    # 심사표는 대규모점포 조회·전통시장통통 등재분만 인정한다. 카카오 '대형마트'
    # 분류에는 동네 마트(삼촌네마트·D마트)가, '백화점' 이름에는 가구점이 섞여
    # 들어와 근사가 성립하지 않는다(실측 확인). 대규모점포 원장(localdata)이
    # 2026-08-28 활용신청 승인돼 이제 지정 원천으로 직접 산정한다. 원장이
    # 미적재면 근사하지 않고 미적재로 남긴다.
    "retail": GroupSpec(
        (),
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
    "park": GroupSpec(
        ("park",),
        "substituted",
        "도시공원정보 표준데이터 대신 지도 검색으로 근사했습니다.",
        keep=_is_park,
    ),
    "culture": GroupSpec(("culture",), "connected", ""),
    "public": GroupSpec(("public", "library"), "connected", "", keep=_is_public),
    "school_elementary": GroupSpec(
        ("school",), "connected", "", keep=_school_filter("초등학교")
    ),
    "school_middle": GroupSpec(("school",), "connected", "", keep=_school_filter("중학교")),
    "school_high": GroupSpec(("school",), "connected", "", keep=_school_filter("고등학교")),
    "university": GroupSpec(
        ("school", "university"),
        "substituted",
        "대학교는 네이버 지역검색으로 확인된 정문 좌표를 기준점으로 씁니다. "
        "정문을 확인하지 못한 캠퍼스는 대표점으로 재며, 그 사실을 시설마다 적습니다.",
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


def _tago_place(row: dict[str, Any]) -> RawPlace | None:
    lat = row.get("gpslati") or row.get("gpsLati")
    lng = row.get("gpslong") or row.get("gpsLong")
    try:
        coordinates = Coordinates(lat=float(lat), lng=float(lng))
    except (TypeError, ValueError):
        return None
    return RawPlace(
        name=str(row.get("nodenm") or row.get("nodeNm") or "").strip(),
        address="",
        category_name="버스정류장",
        coordinates=coordinates,
    )


class AmenityCollector:
    """사업지 한 곳의 생활편의시설을 시설군별로 모은다."""

    def __init__(
        self,
        kakao: KakaoClient,
        tago: TagoClient | None = None,
        cache_ttl_seconds: int = 600,
        facility_store: FacilityStore | None = None,
        hospital_client: NcmcHospitalClient | None = None,
        front_door_store: FrontDoorStore | None = None,
        cadastral_store: CadastralLocalStore | None = None,
        naver: NaverSearchClient | None = None,
        vworld: VWorldClient | None = None,
    ) -> None:
        self.kakao = kakao
        # 시설 경계(필지) 조회. 없거나 키가 없으면 로컬 지적도로, 그것도 없으면
        # 좌표로 폴백하고 그 사실을 시설마다 적는다.
        self.vworld = vworld
        self.tago = tago
        self.cache_ttl_seconds = cache_ttl_seconds
        # 지정 원천을 인허가 캐시에서 직접 채우는 시설군(대규모점포 등)에 쓴다.
        # 없으면 해당 시설군은 missing_note 로 남긴다.
        self.facility_store = facility_store
        # 의료시설(종합병원) 지정 원천. 있으면 카카오 근사 대신 이걸 쓴다.
        self.hospital_client = hospital_client
        # 대학 정문 수기 지정 저장소. 없으면 자동 채택/좌표 폴백만 쓴다.
        self.front_door_store = front_door_store
        # 정문 필지경계·통필지 면적을 조회할 로컬 지적도. 인덱스가 없으면 필지
        # 기준을 못 쓰고 좌표 기준으로 폴백한다(그 사실을 고지한다).
        self.cadastral_store = cadastral_store
        # 대학 정문 좌표를 확보할 지역검색. 표준 데이터셋이 대학 정문·역 출구를
        # 같은 API 로 확보했으므로(2026-09-08 회신) 기준점이 어긋나지 않는다.
        self.naver = naver
        # 시설명(정규화 base) → 문 후보 목록(label, 좌표). collect() 가 판정 전에
        # 채운다. 대학·종합병원을 같은 경로로 다룬다(#7·#8·#11).
        self._front_door_candidates: dict[str, tuple[tuple[str, Coordinates], ...]] = {}
        # 역명 → 출구 후보들(label, 좌표). 역은 대표점이 아니라 출입구에서 재야 한다
        # (LH 과업내용서 예외기준: 지하철=출입구). 출입구가 여럿이면 사업지에서
        # 가장 가까운 것을 쓴다 — 실제 접근 경로가 그렇고, 하나뿐이면 결과가 같다.
        self._station_doors: dict[str, tuple[tuple[str, Coordinates], ...]] = {}
        # 좌표·반경이 같은 재조회를 막는다. analysis 의 TTLCache 와 같은 방식이다.
        self._cache: dict[str, tuple[float, dict[str, GroupCollection]]] = {}

    # -- 공개 API ----------------------------------------------------------
    async def collect(
        self,
        rings: Sequence[Sequence[Coordinates]],
        center: Coordinates,
        radius_m: int = MAX_RADIUS_M,
    ) -> dict[str, GroupCollection]:
        key = self._cache_key(rings, center, radius_m)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        feeds = self._feed_results(center, radius_m)
        names = tuple(feeds)
        outcomes = await asyncio.gather(*feeds.values(), return_exceptions=True)
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
            head = collection.facilities[:BOUNDARY_LOOKUP_PER_GROUP]
            tail = collection.facilities[BOUNDARY_LOOKUP_PER_GROUP:]
            measured: list[CollectedFacility] = []
            for facility in head:
                parcel = (
                    await self._facility_parcel(facility.coordinates, parcel_cache)
                    if can_fetch
                    else None
                )
                measured.append(
                    _measure_to_parcel(
                        facility,
                        parcel,
                        rings,
                        center,
                        area_guard=key not in BOUNDARY_NO_AREA_GUARD,
                    )
                )
            facilities = sorted(measured + list(tail), key=lambda f: f.distance_m)
            shown = len(collection.facilities)
            distances = sorted(
                [f.distance_m for f in facilities]
                + list(collection.distances_m[shown:])
            )
            updated[key] = collection._replace(
                facilities=tuple(facilities), distances_m=tuple(distances)
            )
        return updated

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

        if self.naver is None or not self.naver.enabled:
            return
        # (base_name, exclude_tokens) 목록. 대학은 정문 공유 단위(university_base)로,
        # 종합병원은 시설명 그대로 묻는다. 종합병원 정문 후보는 '병원' 토큰을
        # 배제하지 않는다(HOSPITAL_EXCLUDE_TOKENS).
        targets: list[tuple[str, tuple[str, ...]]] = []
        seen: set[str] = set()

        def _add(base: str, exclude_tokens: tuple[str, ...]) -> None:
            key = normalize_key(base)
            if key and key not in self._front_door_candidates and key not in seen:
                seen.add(key)
                targets.append((base, exclude_tokens))

        for feed in ("school", "university"):
            result = results.get(feed)
            if isinstance(result, FeedResult):
                for place in result.places:
                    if _is_university(place):
                        _add(university_base(place.name), _AUTO_EXCLUDE_TOKENS)
        hospital_result = results.get("hospital")
        if isinstance(hospital_result, FeedResult):
            for place in hospital_result.places:
                if _is_general_hospital(place):
                    _add(place.name, HOSPITAL_EXCLUDE_TOKENS)
        if not targets:
            return

        async def one(base: str, exclude_tokens: tuple[str, ...]) -> None:
            try:
                places = await self.naver.local(f"{base} 정문")
            except Exception:
                return  # 문을 못 얻으면 종전 폴백(정류장 근사·좌표)으로 간다
            candidates = collect_door_candidates(
                base,
                places,
                door_tokens=FACILITY_DOOR_TOKENS,
                exclude_tokens=exclude_tokens,
            )
            if candidates:
                self._front_door_candidates[normalize_key(base)] = tuple(candidates)

        await asyncio.gather(
            *(one(base, excl) for base, excl in targets), return_exceptions=True
        )

    async def _prefetch_station_entrances(
        self,
        results: dict[str, "FeedResult | BaseException"],
    ) -> None:
        """이번 반경에 잡힌 역의 출입구 좌표를 지역검색으로 미리 확보한다.

        표준 데이터셋도 같은 API 로 「○○역출구」를 수집했다(2026-09-08 회신).
        역 이름 그대로 「출구」를 붙여 찾고, 그 역 이름을 포함하지 않는 결과
        (인근 주차장·건물 출구)는 버린다.
        """

        if self.naver is None or not self.naver.enabled:
            return
        names: list[str] = []
        for feed in ("railway", "subway"):
            result = results.get(feed)
            if not isinstance(result, FeedResult):
                continue
            for place in result.places:
                name = place.name.strip()
                if name and name not in self._station_doors and name not in names:
                    names.append(name)
        if not names:
            return

        async def one(name: str) -> None:
            try:
                places = await self.naver.local(f"{name} 출구")
            except Exception:
                return  # 못 얻으면 종전대로 역 대표점에서 잰다
            core = "".join(name.split())
            doors = tuple(
                (p.name.strip(), p.coordinates)
                for p in places
                if any(t in p.name for t in STATION_DOOR_TOKENS)
                and core in "".join(p.name.split())
            )
            if doors:
                self._station_doors[name] = doors

        await asyncio.gather(*(one(n) for n in names), return_exceptions=True)

    def _with_station_entrance(
        self,
        place: RawPlace,
        center: Coordinates,
    ) -> RawPlace:
        """역 대표점을 가장 가까운 출입구 좌표로 바꾼다. 없으면 그대로 둔다."""

        doors = self._station_doors.get(place.name.strip())
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

        doors = self._station_doors.get(station_name.strip())
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

        feeds: dict[str, Any] = {"bus_stop": self._bus_stops(center, radius_m)}
        if not self.kakao.enabled:
            # 키가 없으면 호출 자체를 만들지 않는다. 상태는 missing 으로 내려간다.
            return feeds

        category = {
            "subway": "SW8",
            "mart": "MT1",
            "hospital": "HP8",
            "culture": "CT1",
            "public": "PO3",
            "school": "SC4",
        }
        keyword = {
            "railway": "기차역",
            "terminal": "버스터미널",
            "express_terminal": "고속버스터미널",
            "department": "백화점",
            "market": "전통시장",
            "park": "공원",
            "library": "도서관",
            "university": "대학교",
        }
        # 의료시설은 국립중앙의료원 원장이 있으면 카카오 HP8 근사 대신 그걸 쓴다.
        # HP8 코루틴을 만들었다가 덮으면 await 되지 않아 경고가 나므로 미리 뺀다.
        use_ncmc = self.hospital_client is not None and self.hospital_client.enabled
        for name, code in category.items():
            if name == "hospital" and use_ncmc:
                continue
            feeds[name] = self._kakao_category(code, center, radius_m)
        for name, query in keyword.items():
            feeds[name] = self._kakao_keyword(query, center, radius_m)
        if use_ncmc:
            feeds["hospital"] = self._ncmc_hospitals(center, radius_m)
        return feeds

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

    async def _bus_stops(self, center: Coordinates, radius_m: int) -> FeedResult:
        """TAGO 정류소 근접조회. 실패하면 지도 검색으로 대체한다.

        TAGO 는 약 500m 만 문서화돼 있어 그 밖은 어차피 비어 있다. 그래도 정류장은
        0.5km 등급이 핵심이라 공식 원천을 먼저 쓴다.
        """

        if self.tago is not None and self.tago.enabled:
            try:
                rows = await self.tago.nearby_stops(center.lat, center.lng)
                return FeedResult(_places(rows, _tago_place), TAGO_SOURCE)
            except Exception:
                # 공공 API 장애를 정류장 0개로 둔갑시키지 않는다. 지도로 넘어간다.
                pass
        if not self.kakao.enabled:
            raise RuntimeError("TAGO·카카오 원천이 모두 설정되지 않았습니다.")
        documents = await self.kakao.search_keyword(
            "버스정류장", center.lat, center.lng, radius_m
        )
        return FeedResult(_places(documents, _kakao_place), KAKAO_PLACE_SOURCE)

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
        if spec.localdata_datasets:
            return self._build_localdata_group(key, spec, rings, center, radius_m)
        if not spec.feeds:
            return GroupCollection(key, "missing", spec.note, "", (), ())
        if spec.kakao_backed and not self.kakao.enabled:
            return GroupCollection(key, "missing", KAKAO_DISABLED_NOTE, "", (), ())

        places: list[RawPlace] = []
        sources: list[str] = []
        for feed in spec.feeds:
            outcome = results.get(feed)
            if isinstance(outcome, BaseException):
                return GroupCollection(
                    key,
                    "missing",
                    f"원천 조회에 실패했습니다: {outcome}",
                    "",
                    (),
                    (),
                )
            if outcome is None:
                return GroupCollection(key, "missing", KAKAO_DISABLED_NOTE, "", (), ())
            places.extend(outcome.places)
            if outcome.source_label not in sources:
                sources.append(outcome.source_label)

        keep = spec.keep
        kept = [place for place in _dedupe(places) if keep is None or keep(place)]
        if key in ("railway", "subway"):
            # 역은 대표점이 아니라 출입구에서 잰다(LH 과업내용서 예외기준).
            kept = [self._with_station_entrance(place, center) for place in kept]
        source_label = " + ".join(sources)
        group_notice = ""
        if key in ("university", "hospital"):
            # 대학교·종합병원(대형 필지 시설)은 시설 측 3단(정문 → 부지경계 → 좌표)으로
            # 잰다(국장님 §3-2 · LH 확정 2026-09-11 #8).
            stop_points = _stop_points(results.get("bus_stop"))
            if key == "university":
                pending_notice = FRONT_DOOR_PENDING_NOTICE
                exclude_tokens = _AUTO_EXCLUDE_TOKENS
            else:
                pending_notice = HOSPITAL_FRONT_DOOR_PENDING_NOTICE
                exclude_tokens = HOSPITAL_EXCLUDE_TOKENS
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
        if key == "hospital" and NCMC_HOSPITAL_SOURCE in sources:
            state = "connected"
            note = HOSPITAL_NCMC_NOTE
        return GroupCollection(
            key=key,
            state=state,
            note=note,
            actual_source=" + ".join(sources),
            distances_m=tuple(facility.distance_m for facility in facilities),
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
        return GroupCollection(
            key=key,
            state=spec.state,
            note=spec.note,
            actual_source=LOCALDATA_SOURCE,
            distances_m=tuple(facility.distance_m for facility in facilities),
            facilities=tuple(facilities[:MAX_HITS_PER_GROUP]),
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

        우선순위 — 담당자 수기 지정(정문 필지/좌표) > 네이버 문 후보(가장 가까운 문)
        > 정류장 원장 자동 채택 > 좌표 폴백+고지. 문 후보는 전부 실어 담당자가 다른
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
            return self._measure_from_ref(place, manual, rings, center, site_token, build)

        # 2순위 — 네이버 문 후보. 기본은 사업지에 가장 가까운 문(#7·#11).
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

        # 3순위 — 정류장 원장 자동 채택.
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

        # 4순위 — 정문 미확인: 좌표(대표점)로 보수 폴백하고 그 사실을 고지한다.
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
    ) -> CollectedFacility:
        """담당자 수기 지정(FrontDoorRef) 하나를 3단 기준으로 잰다."""

        # 1순위 — 정문 필지(PNU) 지정. 통필지 안전장치를 통과하면 필지경계로 잰다.
        if ref.has_parcel:
            measured = self._measure_front_door_parcel(ref, rings, center)
            if measured is not None:
                distance, tier, label, notice, facility_point = measured
                # 대형 통필지에 검증 정문 좌표가 없으면 확정하지 않고 좌표로 보수 폴백.
                if distance is None or tier == "front_door_pending":
                    return build(
                        _distance_m(place.coordinates, rings, center),
                        "coordinate",
                        f"({site_token} ↔ 시설 좌표)",
                        ref.source_label,
                        notice,
                        place.coordinates,
                    )
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
    ) -> tuple[float | None, str, str, str, Coordinates | None] | None:
        """정문 필지경계 기준 측정. (거리, tier, 표기, 고지, 시설측 최단점) 또는 None.

        캠퍼스 통필지(면적 임계 이상)는 검증된 정문 좌표가 있어야만 정문 기준으로
        잰다. 좌표 없이 PNU 만 온 지정은 필지 내부 임의 대표점을 대신 쓰면 실제
        정문이 캠퍼스 반대편일 때 그 점이 사업지에 더 가까워 거리가 짧아지고 접근성
        점수가 후해진다(안전장치가 막으려던 바로 그 왜곡). 그래서 대표점 폴백을
        제거하고, 검증 좌표가 없으면 거리를 확정하지 않고 대기(pending)로 돌려보낸다
        (distance=None, tier="front_door_pending"). 호출부가 좌표 폴백으로 처리하되
        그 사실을 화면에 드러낸다(국장님 §3-3).
        """

        store = self.cadastral_store
        if store is None:
            return None
        parcel = store.parcel_by_pnu(ref.pnu)
        if parcel is None or len(parcel.ring) < 4:
            return None

        site_token = "대지경계" if rings else "주소점"
        if parcel.area_m2 >= CAMPUS_PARCEL_MAX_AREA_M2:
            # 검증된 정문 좌표(정문 클릭점)가 있으면 그것으로 잰다 — 필지경계보다
            # 보수적이고 실제 정문 위치라 왜곡이 없다.
            if ref.coordinates is not None:
                notice = (
                    f"캠퍼스 통필지({parcel.area_m2:,.0f}㎡ ≥ "
                    f"{CAMPUS_PARCEL_MAX_AREA_M2:,.0f}㎡) — 필지경계 축소 왜곡을 막기 "
                    "위해 지정된 정문 좌표 기준으로 잽니다."
                )
                return (
                    _distance_m(ref.coordinates, rings, center),
                    "front_door_point",
                    f"({site_token} ↔ 정문 좌표)",
                    notice,
                    ref.coordinates,
                )
            # 검증 좌표가 없다 — 대표점을 쓰면 거리가 짧아질 수 있으므로 확정하지
            # 않고 대기로 돌려보낸다. 대형 통필지는 검증된 정문 좌표를 요구한다.
            notice = (
                f"캠퍼스 통필지({parcel.area_m2:,.0f}㎡ ≥ "
                f"{CAMPUS_PARCEL_MAX_AREA_M2:,.0f}㎡)에는 검증된 정문 좌표가 필요합니다. "
                "PNU 만 지정돼 필지 내부 대표점으로 거리를 짧게 만들지 않도록, 정문 "
                "기준 거리를 확정하지 않고 대기로 남깁니다(정문 좌표 지정 필요)."
            )
            return (None, "front_door_pending", "", notice, None)

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
        return f"{center.lat:.5f},{center.lng:.5f}:{radius_m}:{fingerprint}:fd{revision}"

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
    *,
    area_guard: bool,
) -> CollectedFacility:
    """시설 한 곳을 필지 경계 기준으로 바꾼 새 값. 못 바꾸면 사유만 적어 돌려준다."""

    if parcel is None:
        return facility._replace(front_door_notice=BOUNDARY_FALLBACK_NOTICE)
    if area_guard and parcel.area_m2 >= CAMPUS_PARCEL_MAX_AREA_M2:
        # 좌표가 떨어진 필지가 통필지(하천·단지 전체 등)면 경계가 시설 실체보다
        # 훨씬 넓어 거리가 부당하게 줄어든다. 좌표 기준을 유지하고 사유를 적는다.
        notice = (
            f"통필지({parcel.area_m2:,.0f}㎡ ≥ {CAMPUS_PARCEL_MAX_AREA_M2:,.0f}㎡)라 "
            "시설 경계로 재지 않고 시설 좌표로 쟀습니다."
        )
        return facility._replace(front_door_notice=notice)
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
    )


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
