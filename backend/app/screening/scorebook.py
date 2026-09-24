"""LH 신축매입약정 서류 심사표 2차(생활편의성) 배점 정본.

「26년 신축매입약정 서류 심사표 및 평가기준」의 3개 심사표를 데이터로 옮긴다.
표를 한 칸만 틀려도 점수가 통째로 달라지므로 등급표는 여기 한 곳에만 둔다.

근거
  - 신축매입약정(공통) 서류 심사표 — 일반·다자녀·신혼신생아Ⅰ·Ⅱ (각주 1)
  - 신축매입약정(청년·기숙사형) 서류 심사표
  - 신축매입약정(고령자) 서류 심사표
  - 평가 기준 2항 — 거리산정기준·시설분류·인정 원천

거리 기준(평가기준 2항)
  사업장의 사업부지 경계로부터 해당 평가항목 시설의 경계까지 직선거리.
  예외 — 지하철역·철도역은 최단거리 출입구 Point, 대학교는 공식 정문 Point.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, NamedTuple


# ---------------------------------------------------------------------------
# 심사표 종류
# ---------------------------------------------------------------------------
# 1차 매트릭스는 신청유형 5종(일반·다자녀·신혼·청년·고령자)으로 갈리지만,
# 2차 심사표는 3종뿐이다. 일반·다자녀·신혼이 「공통」 심사표를 함께 쓴다.
ScoreSheet = Literal["common", "youth", "senior"]

SCORE_SHEET_LABELS: dict[ScoreSheet, str] = {
    "common": "공통(일반·다자녀·신혼·신생아)",
    "youth": "청년·기숙사형",
    "senior": "고령자",
}

# 신청유형 → 심사표. 룰북 §1 ApplicationType 과 1:1로 맞춘다.
SHEET_BY_APPLICATION_TYPE: dict[str, ScoreSheet] = {
    "general": "common",
    "multi_child": "common",
    "newlywed": "common",
    "youth": "youth",
    "senior": "senior",
}


# ---------------------------------------------------------------------------
# 시설군 — 평가기준 2항 시설분류
# ---------------------------------------------------------------------------
class FacilityGroup(NamedTuple):
    """평가에 쓰는 시설군 하나."""

    key: str
    label: str
    # 심사표가 지정한 인정 원천. 다른 원천으로 근사하면 화면에 그대로 고지한다.
    designated_source: str
    # 경계가 아니라 Point 로 재는 예외 시설인지(평가기준 명문화 예외).
    point_exception: str = ""


FACILITY_GROUPS: tuple[FacilityGroup, ...] = (
    # 대중교통 — 지하철역·철도역·운행주기 15분 이내 버스정류장·환승시설·터미널
    FacilityGroup("subway", "지하철역", "공개 지도·역사 정보", "최단거리 출입구"),
    FacilityGroup("railway", "철도역", "한국철도공사 역위치 정보", "최단거리 출입구"),
    FacilityGroup("bus_stop", "버스정류장", "운행주기 15분 이내인 정류장만 인정"),
    FacilityGroup("terminal", "터미널", "대중교통수단 터미널 정보"),
    FacilityGroup("transfer", "환승시설", "간선급행버스체계법 제2조제3호다목"),
    # 주거여건 — 상업·의료 / 공원·문화·공공
    FacilityGroup("retail", "상업시설", "localdata.go.kr 대규모점포 · 전통시장통통"),
    FacilityGroup("hospital", "의료시설", "건강보험심사평가원 종류=종합병원"),
    FacilityGroup("park", "공원", "도시공원정보 표준데이터"),
    FacilityGroup("culture", "문화시설", "극장·공연장·전시장 등"),
    FacilityGroup("public", "공공시설", "주민센터·시군구청사·도서관 등"),
    # 교육여건
    FacilityGroup("school_elementary", "초등학교", "교육데이터플랫폼 학교별 위치정보"),
    FacilityGroup("school_middle", "중학교", "교육데이터플랫폼 학교별 위치정보"),
    FacilityGroup("school_high", "고등학교", "교육데이터플랫폼 학교별 위치정보"),
    FacilityGroup("university", "대학교", "대학알리미 학교개황정보", "공식 정문"),
)

FACILITY_GROUP_BY_KEY: dict[str, FacilityGroup] = {g.key: g for g in FACILITY_GROUPS}

# 대중교통 접근성에 드는 시설군(평가기준 2-1 시설분류).
TRANSIT_GROUPS: tuple[str, ...] = ("subway", "railway", "bus_stop", "terminal", "transfer")

# 주거여건 ②군 — 공원·문화·공공. "종류" 수를 세는 대상이다.
AMENITY_KIND_GROUPS: tuple[str, ...] = ("park", "culture", "public")

# 가점 역세권(5점) 대상 — 버스정류장은 빠진다.
STATION_AREA_GROUPS: tuple[str, ...] = ("subway", "railway", "terminal", "transfer")


# ---------------------------------------------------------------------------
# 측정 사실 — 등급 판정에 필요한 최소 입력
# ---------------------------------------------------------------------------
class Facts:
    """사업지 한 건의 측정 결과.

    등급 조건은 전부 이 객체에 대한 질의로만 쓴다. 데이터가 없는 시설군은
    `missing` 에 담아 두고, 낙관·비관 두 번 평가해서 확정 여부를 가린다.
    `named` 는 시설군별 (시설명, 거리) 목록으로, 배점 근거 문장에만 쓴다.
    """

    def __init__(
        self,
        distances_m: dict[str, list[float]],
        missing_groups: set[str],
        *,
        assume_missing_present: bool = False,
        named: dict[str, list[tuple[str, float]]] | None = None,
    ) -> None:
        # 시설군별 거리 목록(m). 오름차순 정렬을 가정하지 않는다.
        self.distances_m = distances_m
        self.missing_groups = missing_groups
        self._assume = assume_missing_present
        self.named = named or {}

    def count_within(self, group: str, km: float) -> int:
        """해당 시설군에서 반경 km 이내 시설 수."""

        if group in self.missing_groups:
            # 낙관 평가에서는 "둘 이상" 조건까지 만족시켜야 상한이 나온다.
            return 2 if self._assume else 0
        limit = km * 1000.0
        return sum(1 for d in self.distances_m.get(group, ()) if d <= limit)

    def has(self, group: str, km: float) -> bool:
        return self.count_within(group, km) > 0

    def has_any(self, groups: tuple[str, ...], km: float) -> bool:
        return any(self.has(g, km) for g in groups)

    def transit_count(self, km: float) -> int:
        """반경 km 이내 교통시설 수.

        평가기준 2-1 단서 — 동일 유형 시설이 2개 이상이면 2개 이상으로 인정한다.
        따라서 유형을 구분하지 않고 시설 수를 그대로 합산하면 된다.
        """

        return sum(self.count_within(g, km) for g in TRANSIT_GROUPS)

    def amenity_kind_count(self, km: float) -> int:
        """공원·문화·공공 중 반경 km 이내에 존재하는 '종류' 수(0~3)."""

        return sum(1 for g in AMENITY_KIND_GROUPS if self.has(g, km))

    def nearest_m(self, group: str) -> float | None:
        values = self.distances_m.get(group) or []
        if not values:
            return None
        return min(values)

    def nearest_name(self, group: str) -> tuple[str, float] | None:
        """가장 가까운 시설의 (이름, 거리). 이름을 모르면 이름을 비운다."""

        named = self.named.get(group)
        if named:
            return min(named, key=lambda item: item[1])
        distance = self.nearest_m(group)
        return None if distance is None else ("", distance)


# ---------------------------------------------------------------------------
# 등급 정의
# ---------------------------------------------------------------------------
def _km(km: float) -> str:
    return f"{km:g}km"


def _nearest_text(facts: Facts, group: str) -> str:
    """「전주새연초등학교 451m」 — 시설군 최근접 시설 한 줄."""

    label = FACILITY_GROUP_BY_KEY[group].label
    if group in facts.missing_groups:
        return f"{label} 원천 미확보"
    nearest = facts.nearest_name(group)
    if nearest is None:
        return f"{label} 없음"
    name, distance = nearest
    return f"{name or label} {distance:,.0f}m"


class Requirement(NamedTuple):
    """등급 조건을 이루는 요건 하나. 충족 여부와 근거 문장을 함께 낸다."""

    text: str
    check: Callable[[Facts], bool]
    evidence: Callable[[Facts], str]


def _req_has(group: str, km: float) -> Requirement:
    return Requirement(
        f"반경 {_km(km)} 이내 {FACILITY_GROUP_BY_KEY[group].label}",
        lambda f: f.has(group, km),
        lambda f: _nearest_text(f, group),
    )


def _req_any(groups: tuple[str, ...], km: float, text: str) -> Requirement:
    return Requirement(
        text,
        lambda f: f.has_any(groups, km),
        lambda f: " · ".join(_nearest_text(f, g) for g in groups),
    )


def _req_transit(km: float, minimum: int) -> Requirement:
    def evidence(f: Facts) -> str:
        parts: list[str] = []
        found = 0
        for group in TRANSIT_GROUPS:
            label = FACILITY_GROUP_BY_KEY[group].label
            if group in f.missing_groups:
                parts.append(f"{label} 원천 미확보")
                continue
            count = f.count_within(group, km)
            found += count
            if count:
                parts.append(f"{label} {count}개(최근접 {_nearest_text(f, group)})")
        head = f"{_km(km)} 이내 {found}개"
        return f"{head} — {' · '.join(parts)}" if parts else head

    return Requirement(
        f"반경 {_km(km)} 이내 교통시설 {'둘' if minimum >= 2 else '하나'} 이상",
        lambda f: f.transit_count(km) >= minimum,
        evidence,
    )


def _req_kinds(km: float, minimum: int) -> Requirement:
    def evidence(f: Facts) -> str:
        present = [
            _nearest_text(f, g)
            for g in AMENITY_KIND_GROUPS
            if g not in f.missing_groups and f.has(g, km)
        ]
        head = f"{_km(km)} 이내 {len(present)}종"
        return f"{head} — {' · '.join(present)}" if present else head

    return Requirement(
        f"반경 {_km(km)} 이내 공원·문화·공공시설 중 {minimum}가지 종류 이상",
        lambda f: f.amenity_kind_count(km) >= minimum,
        evidence,
    )


class Tier(NamedTuple):
    """평가항목 한 줄. 위에서부터 먼저 만족하는 등급을 채택한다.

    요건이 없는 등급(「해당 없음」)은 항상 참이다.
    """

    points: int
    condition: str
    requirements: tuple[Requirement, ...] = ()

    def check(self, facts: Facts) -> bool:
        return all(req.check(facts) for req in self.requirements)


class Criterion(NamedTuple):
    """평가항목 하나(대중교통 접근성 · 주거여건 · 교육여건 · 역세권)."""

    key: str
    label: str
    maximum: int
    # 이 항목을 산정하는 데 쓰이는 시설군. 하나라도 미확보면 확정 불가 가능성이 생긴다.
    groups: tuple[str, ...]
    tiers: tuple[Tier, ...]
    basis: str


# --- 대중교통 접근성 -------------------------------------------------------
def _transit_tiers(p1: int, p2: int, p3: int, p4: int, p5: int) -> tuple[Tier, ...]:
    """교통시설 등급표. 배점만 심사표별로 다르고 조건은 같다."""

    return (
        Tier(p1, "반경 0.5km 이내 교통시설 둘 이상", (_req_transit(0.5, 2),)),
        Tier(p2, "반경 0.5km 이내 교통시설 하나 이상", (_req_transit(0.5, 1),)),
        Tier(p3, "반경 1km 이내 교통시설 하나 이상", (_req_transit(1.0, 1),)),
        Tier(p4, "반경 1.5km 이내 교통시설 하나 이상", (_req_transit(1.5, 1),)),
        Tier(p5, "해당 없음"),
    )


# --- 주거여건 --------------------------------------------------------------
_RETAIL_HOSPITAL: tuple[str, ...] = ("retail", "hospital")


def _living_tiers(p1: int, p2: int, p3: int, p4: int, p5: int) -> tuple[Tier, ...]:
    """주거여건 등급표. 상업·의료 조합과 공원·문화·공공 종류 수로 갈린다."""

    either = _req_any(_RETAIL_HOSPITAL, 3, "반경 3km 이내 상업·의료시설 중 1개 이상")
    return (
        Tier(
            p1,
            "반경 2km 이내 상업·의료시설 모두 존재하고 공원·문화·공공시설 중 2가지 종류 이상 존재",
            (_req_has("retail", 2), _req_has("hospital", 2), _req_kinds(2, 2)),
        ),
        Tier(
            p2,
            "반경 3km 이내 상업·의료시설 모두 존재하고 공원·문화·공공시설 중 1개 이상 존재",
            (_req_has("retail", 3), _req_has("hospital", 3), _req_kinds(3, 1)),
        ),
        Tier(
            p3,
            "반경 3km 이내 상업·의료시설 중 1개 이상 존재하고 공원·문화·공공시설 중 2가지 종류 이상 존재",
            (either, _req_kinds(3, 2)),
        ),
        Tier(
            p4,
            "반경 3km 이내 상업·의료시설 중 1개 존재하고 공원·문화·공공시설 중 1개 이상 존재",
            (either, _req_kinds(3, 1)),
        ),
        Tier(p5, "해당 없음"),
    )


# --- 교육여건(공통) --------------------------------------------------------
# 요건은 조건 문장을 가장 좁은 반경으로 줄인 것이다. 「1km 이내 초·중·고 + 500m 이내
# 초·중」은 초·중이 500m 안에 있으면 1km 도 충족하므로 초 0.5 · 중 0.5 · 고 1 과 같다.
_COMMON_EDUCATION_TIERS: tuple[Tier, ...] = (
    Tier(
        10,
        "반경 1km 이내 초·중·고 모두 존재하고, 반경 500m 이내 초·중 모두 존재",
        (
            _req_has("school_elementary", 0.5),
            _req_has("school_middle", 0.5),
            _req_has("school_high", 1),
        ),
    ),
    Tier(
        8,
        "반경 1.5km 이내 초·중·고 모두 존재하고, 반경 1km 이내 초등학교 존재",
        (
            _req_has("school_elementary", 1),
            _req_has("school_middle", 1.5),
            _req_has("school_high", 1.5),
        ),
    ),
    Tier(
        6,
        "반경 2km 이내 초·중 모두 존재하고, 반경 3km 이내 고등학교 존재",
        (
            _req_has("school_elementary", 2),
            _req_has("school_middle", 2),
            _req_has("school_high", 3),
        ),
    ),
    Tier(
        4,
        "반경 3km 이내 초·중 모두 존재",
        (_req_has("school_elementary", 3), _req_has("school_middle", 3)),
    ),
    Tier(2, "해당 없음"),
)

# --- 교육여건(청년·기숙사형) ----------------------------------------------
_YOUTH_EDUCATION_TIERS: tuple[Tier, ...] = (
    Tier(5, "반경 1km 이내 대학교가 존재", (_req_has("university", 1),)),
    Tier(4, "반경 1.5km 이내 대학교가 존재", (_req_has("university", 1.5),)),
    Tier(3, "반경 2km 이내 대학교가 존재", (_req_has("university", 2),)),
    Tier(2, "반경 2.5km 이내 대학교가 존재", (_req_has("university", 2.5),)),
    Tier(1, "해당 없음"),
)

# --- 가점 · 역세권 ---------------------------------------------------------
_STATION_AREA_TIERS: tuple[Tier, ...] = (
    Tier(
        5,
        "반경 500m 이내 지하철역·철도역·터미널·환승시설 중 하나 이상 존재",
        (
            _req_any(
                STATION_AREA_GROUPS,
                0.5,
                "반경 0.5km 이내 지하철역·철도역·터미널·환승시설 중 하나 이상",
            ),
        ),
    ),
    Tier(0, "해당 없음"),
)


_TRANSIT_BASIS = (
    "시설분류 — 지하철역·철도역·운행주기 15분 이내인 버스정류장·환승시설·터미널. "
    "동일 유형 시설이 2개 이상이면 2개 이상으로 인정한다(평가기준 2-1)."
)
_LIVING_BASIS = (
    "상업시설(대형마트·백화점·재래시장)·의료시설(종합병원)과 "
    "공원·문화시설·공공시설의 존재 유무 및 개수로 평가한다(평가기준 2-2)."
)


def _criteria(sheet: ScoreSheet) -> tuple[Criterion, ...]:
    if sheet == "common":
        return (
            Criterion(
                "transit", "대중교통 접근성", 15, TRANSIT_GROUPS,
                _transit_tiers(15, 12, 9, 6, 3), _TRANSIT_BASIS,
            ),
            Criterion(
                "living", "주거여건", 15,
                ("retail", "hospital", *AMENITY_KIND_GROUPS),
                _living_tiers(15, 12, 9, 6, 3), _LIVING_BASIS,
            ),
            Criterion(
                "education", "교육여건", 10,
                ("school_elementary", "school_middle", "school_high"),
                _COMMON_EDUCATION_TIERS,
                "초등학교·중학교·고등학교 존재 유무 및 개수로 평가한다(평가기준 2-3).",
            ),
        )
    if sheet == "youth":
        return (
            Criterion(
                "transit", "대중교통 접근성", 20, TRANSIT_GROUPS,
                _transit_tiers(20, 16, 12, 8, 4), _TRANSIT_BASIS,
            ),
            Criterion(
                "living", "주거여건", 15,
                ("retail", "hospital", *AMENITY_KIND_GROUPS),
                _living_tiers(15, 12, 9, 6, 3), _LIVING_BASIS,
            ),
            Criterion(
                "education", "교육여건", 5, ("university",),
                _YOUTH_EDUCATION_TIERS,
                "신청물건과 가까운 대학교 존재 유무로 평가하며, 기준점은 공식 정문이다(평가기준 2-3).",
            ),
        )
    # senior — 교육여건 항목 자체가 없다.
    return (
        Criterion(
            "transit", "대중교통 접근성", 20, TRANSIT_GROUPS,
            _transit_tiers(20, 16, 12, 8, 4), _TRANSIT_BASIS,
        ),
        Criterion(
            "living", "주거여건", 20,
            ("retail", "hospital", *AMENITY_KIND_GROUPS),
            _living_tiers(20, 16, 12, 8, 4), _LIVING_BASIS,
        ),
    )


BONUS_CRITERION = Criterion(
    "station_area", "역세권 가점", 5, STATION_AREA_GROUPS,
    _STATION_AREA_TIERS,
    "반경 500m 이내 지하철역·철도역·터미널·환승시설 중 하나 이상 존재 시 가점(심사표 5-2).",
)


class Sheet(NamedTuple):
    key: ScoreSheet
    label: str
    # 생활편의성 총 배점. 세 심사표 모두 40점이며 내부 배분만 다르다.
    living_maximum: int
    criteria: tuple[Criterion, ...]


def sheet_for(application_type: str) -> Sheet:
    """신청유형에 해당하는 2차 심사표를 돌려준다."""

    key = SHEET_BY_APPLICATION_TYPE.get(application_type, "common")
    criteria = _criteria(key)
    return Sheet(key, SCORE_SHEET_LABELS[key], sum(c.maximum for c in criteria), criteria)


def evaluate(criterion: Criterion, facts: Facts) -> tuple[int, Tier]:
    """위에서부터 먼저 만족하는 등급을 채택한다. 마지막 등급은 항상 참이다."""

    for tier in criterion.tiers:
        if tier.check(facts):
            return tier.points, tier
    last = criterion.tiers[-1]
    return last.points, last


# ---------------------------------------------------------------------------
# 자동 산정 대상이 아닌 심사표 항목
# ---------------------------------------------------------------------------
# 총점 100점 중 생활편의성 40점(+역세권 가점 5점)만 좌표로 산정할 수 있다.
# 나머지는 공간분석의 대상이 아니므로 "산정 대상 아님"으로 명시한다.
# 이걸 빼먹고 40점을 총점처럼 보이게 하면 합격선(70점) 오해가 생긴다.
class OutOfScopeItem(NamedTuple):
    label: str
    maximum: int
    reason: str


OUT_OF_SCOPE_ITEMS: tuple[OutOfScopeItem, ...] = (
    OutOfScopeItem("공가율", 20, "GIS자산관리정보시스템 조회값 — 공간분석 대상 아님"),
    OutOfScopeItem("주민등록세대수 증가율", 10, "국가통계포털 시·군 증감률 — 공간분석 대상 아님"),
    OutOfScopeItem("지원단가 대비 탁감가액 비율", 15, "감정평가 결과 — 공간분석 대상 아님"),
    OutOfScopeItem("접면도로 넓이", 10, "사업부지 접도 실측 — 별도 확인"),
    OutOfScopeItem("주차대수", 5, "설계도서 확인 — 별도 확인"),
    OutOfScopeItem("정책사업(뉴빌리지)", 5, "뉴빌리지 사업지 여부 — 별도 확인"),
)

TOTAL_SHEET_POINTS = 100
PASS_THRESHOLD = 70
