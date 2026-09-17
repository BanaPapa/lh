"""LH 매입제외 유해요소 판정 규칙을 데이터로 옮긴 모듈.

유해요소(1차 매입제외) 판정의 유일한 기준이다. 매트릭스·상태값·Rule·카테고리를
여기 한 곳에 모아 두고, service.py 는 이 값만 참조한다. 하드코딩 상수를
분산시키면 표를 한 칸만 틀려도 추적이 안 되므로 정본을 한 파일로 고정한다.

근거(2026-09-02 기준): `docs/hazards/` 23개 항목 문서 + INDEX/TIMELINE/MEASUREMENT.
- 상위 구조는 LH 가 승인한 「매입제외시설 최종정리」 §2 종합표(2026-08-24 10:22)다.
- 유형별 적용은 LH 「위험물 시설현황 종합」(08-20 수정) 매트릭스다.
- 항목 상태 어휘는 LH 회신문 §6-2 를 그대로 쓴다(판정 적용·부분 적용·근사 적용·
  판정 미적용·개별 협의). 각 Category 의 doc_ref 가 근거 문서(H-번호)를 가리킨다.
- 룰북 v1.4(수행팀 파생물)는 LH 승인을 받은 적이 없으므로 근거로 쓰지 않는다
  (TIMELINE §7-2). 매트릭스 값은 두 문서가 같다.
"""

from __future__ import annotations

from typing import Literal, NamedTuple


# ---------------------------------------------------------------------------
# §1 신청 유형 입력
# ---------------------------------------------------------------------------
# 거리기준이 유형에 따라 달라지므로 판정 전에 반드시 확정한다.
HousingType = Literal["house", "officetel"]
ApplicationType = Literal["general", "multi_child", "newlywed", "youth", "senior"]

HOUSING_TYPE_LABELS: dict[str, str] = {
    "house": "주택",
    "officetel": "주거용 오피스텔",
}
APPLICATION_TYPE_LABELS: dict[str, str] = {
    "general": "일반",
    "multi_child": "다자녀",
    "newlywed": "신혼",
    "youth": "청년",
    "senior": "고령자",
}


# ---------------------------------------------------------------------------
# §4 판정 상태값 (6종)
# ---------------------------------------------------------------------------
HazardReviewStatus = Literal[
    "exclusion_match",
    "no_conflict_in_snapshot",
    "review_required",
    "dataset_missing",
    "geometry_missing",
    "not_applicable",
]

STATUS_LABELS: dict[str, str] = {
    "exclusion_match": "매입제외",
    "no_conflict_in_snapshot": "스냅샷 내 충돌 없음",
    "review_required": "검토 필요",
    "dataset_missing": "데이터셋 미확보",
    "geometry_missing": "경계 미확보",
    "not_applicable": "미적용",
}

# 종합 상태 승격 순서. 위험을 놓치지 않도록 강한 상태가 앞선다.
# exclusion_match > review_required > geometry_missing > dataset_missing
#   > no_conflict_in_snapshot > not_applicable
STATUS_PRIORITY: tuple[HazardReviewStatus, ...] = (
    "exclusion_match",
    "review_required",
    "geometry_missing",
    "dataset_missing",
    "no_conflict_in_snapshot",
    "not_applicable",
)


# ---------------------------------------------------------------------------
# §2 1차 매입제외 적용 매트릭스
# ---------------------------------------------------------------------------
# 값은 임계거리(m)이며 None 은 해당 유형에 Rule 미적용(not_applicable)이다.
# 컬럼은 §5 Rule 의 매트릭스 컬럼과 1:1 대응한다.
MATRIX_COLUMNS: tuple[str, ...] = (
    "factory",
    "hazmat",
    "fuel25",
    "amusement",
    "lodging",
    "cremation_military",
)

# 표를 한 칸도 틀리지 말 것. 특히 officetel/multi_child 는 lodging 25, amusement None.
MATRIX: dict[tuple[HousingType, ApplicationType], dict[str, int | None]] = {
    ("house", "general"): {
        "factory": 50, "hazmat": 50, "fuel25": 25,
        "amusement": None, "lodging": None, "cremation_military": 500,
    },
    ("house", "multi_child"): {
        "factory": 50, "hazmat": 50, "fuel25": 25,
        "amusement": 25, "lodging": 25, "cremation_military": 500,
    },
    ("house", "newlywed"): {
        "factory": 50, "hazmat": 50, "fuel25": 25,
        "amusement": None, "lodging": None, "cremation_military": 500,
    },
    ("house", "youth"): {
        "factory": 50, "hazmat": 50, "fuel25": 25,
        "amusement": None, "lodging": None, "cremation_military": 500,
    },
    ("house", "senior"): {
        "factory": 50, "hazmat": 50, "fuel25": 25,
        "amusement": None, "lodging": None, "cremation_military": 500,
    },
    ("officetel", "general"): {
        "factory": None, "hazmat": None, "fuel25": None,
        "amusement": None, "lodging": None, "cremation_military": 500,
    },
    ("officetel", "multi_child"): {
        "factory": None, "hazmat": None, "fuel25": None,
        "amusement": None, "lodging": 25, "cremation_military": 500,
    },
    ("officetel", "newlywed"): {
        "factory": None, "hazmat": None, "fuel25": None,
        "amusement": None, "lodging": None, "cremation_military": 500,
    },
    ("officetel", "youth"): {
        "factory": None, "hazmat": None, "fuel25": None,
        "amusement": None, "lodging": None, "cremation_military": 500,
    },
    ("officetel", "senior"): {
        "factory": None, "hazmat": None, "fuel25": None,
        "amusement": None, "lodging": None, "cremation_military": 500,
    },
}


# ---------------------------------------------------------------------------
# §5 Rule 정의 (6개)
# ---------------------------------------------------------------------------
class Rule(NamedTuple):
    rule_id: str
    label: str
    column: str          # MATRIX 컬럼 키
    legal_reference: str


RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="RB14-FACTORY",
        label="공장 (등록공장 검토 표시)",
        column="factory",
        legal_reference=(
            "주택건설기준 등에 관한 규정 제9조의2 제1항 제1호 가~라 · "
            "LH 확정 2026-09-11 「등록공장 검토 표시 · 자동 제외·매칭 판정 없음」"
        ),
    ),
    Rule(
        rule_id="RB14-HAZMAT",
        label="위험물 저장·처리시설",
        column="hazmat",
        legal_reference=(
            "주택건설기준 등에 관한 규정 제9조의2 제1항 제2호 · "
            "LH 08-20 「50m 이내 인접 시 매입제외」"
        ),
    ),
    Rule(
        rule_id="RB14-FUEL25",
        label="주유소·석유판매 취급소·자동차용 천연가스·LPG 충전소 (25m 예외)",
        column="fuel25",
        legal_reference=(
            "제9조의2 제2호 가목(주유소) + LH 내부규정 · LH 08-20 「도심 입지 감안 "
            "25m」 · 매입공고 제5조④항 (원본 미확보) · 자동차용 LPG 충전소 25m "
            "(LH 확정 2026-09-11 2차 보고 회의)"
        ),
    ),
    Rule(
        rule_id="RB14-AMUSEMENT",
        label="위락시설 (다자녀만)",
        column="amusement",
        legal_reference=(
            "건축법 시행령 별표1 제16호 · 매입전세임대사업처-4535호 거리표 25m · "
            "LH 08-20 매트릭스(다자녀 한정)"
        ),
    ),
    Rule(
        rule_id="RB14-LODGING",
        label="일반숙박시설 (다자녀만 · 오피스텔 포함)",
        column="lodging",
        legal_reference=(
            "매입전세임대사업처-4535호 거리표 25m · LH 08-20 「취사 불가 일반숙박」 · "
            "08-24 17:08 「관광숙박 8종 제외」"
        ),
    ),
    Rule(
        # 군부대·사격장은 공적 DB 부재로 08-14 LH 과업협의에서 판정 대상 제외.
        # 이 Rule 은 실질적으로 화장장만 판정한다. 라벨도 그에 맞춘다.
        rule_id="RB14-CREMATION-MILITARY",
        label="화장장 (군부대·사격장은 판정 제외)",
        column="cremation_military",
        legal_reference=(
            "매입전세임대사업처-4535호 거리표 500m · 전 분류·전 유형 공통"
        ),
    ),
)

RULE_BY_ID: dict[str, Rule] = {rule.rule_id: rule for rule in RULES}


def threshold_for(
    rule_id: str,
    housing_type: HousingType,
    application_type: ApplicationType,
) -> int | None:
    """해당 신청유형에 적용되는 임계거리(m). 미적용이면 None.

    매트릭스가 유일 기준이다. Rule 이나 조합이 없으면 None 을 돌려준다.
    """

    rule = RULE_BY_ID.get(rule_id)
    if rule is None:
        return None
    cell = MATRIX.get((housing_type, application_type))
    if cell is None:
        return None
    return cell.get(rule.column)


def thresholds_for(
    housing_type: HousingType,
    application_type: ApplicationType,
) -> dict[str, int | None]:
    """한 조합에 대한 Rule별 임계거리 표. 프런트 매트릭스 노출에 쓴다."""

    return {
        rule.rule_id: threshold_for(rule.rule_id, housing_type, application_type)
        for rule in RULES
    }


# ---------------------------------------------------------------------------
# §6 유해요소 종류 (30종 — LH 승인 23항목을 원천 단위로 쪼갠 것)
# ---------------------------------------------------------------------------
# 종수 근거: 박진주 대표 확정 체크리스트(2026-08-26). 테마파크 3종(종합/일반/기타)
# 과 무도 2종(무도장/무도학원)을 각각 분리하고, 도료류 판매소(사목)를 추가했다.
# data_state — LH 회신문 §6-2 상태 어휘(INDEX §5):
#   applied     판정 적용   — 자료가 확보돼 거리 계산에 쓴다
#   partial     부분 적용   — 일부 시설만 확보. 확보된 것만 판정한다
#   approximate 근사 적용   — 정확히 맞는 자료가 없어 유사 자료로 대신한다
#   manual      판정 미적용 — 자료가 없다. 「이상 없음」으로 접지 않고 별도 수기
#                             확인으로 표시한다(LH [요청 2] 승인 2026-08-24)
#   missing     판정 미적용 — 위와 같되 협의로 판정 자체가 제외된 항목(군부대·사격장)
#   negotiate   개별 협의   — 해당 시설이 나타나면 그때 협의한다(차목)
CategoryDataState = Literal[
    "applied", "partial", "approximate", "missing", "manual", "negotiate"
]

# 판정에 참여하지 않는 data_state. 후보 조회 없이 dataset_missing 으로 표시한다.
NON_JUDGED_DATA_STATES: frozenset[str] = frozenset({"missing", "manual", "negotiate"})
# 「판정 미적용 — 별도 수기 확인」 표시가 붙는 data_state.
MANUAL_CHECK_DATA_STATES: frozenset[str] = frozenset({"manual", "negotiate"})

# LH 회신문 §7 미확보 6개 목의 공통 표기. [요청 2] 로 승인된 처리라 문구를 고정한다.
MANUAL_CHECK_NOTE = "판정 미적용 — 별도 수기 확인 (LH [요청 2] 승인 2026-08-24)"

# 구현/확정/검증 축. data_state(원천 상태)와는 별개 축이다. 섞지 말 것.
# - ready: 어댑터·파이프라인이 갖춰져 판정에 실제로 참여
# - not_implemented: 어댑터/파이프라인이 아직 없음 (미구현)
# - unconfirmed: LH/팀 확인 대기 (미확정)
# - needs_verification: 엔드포인트/데이터는 등록됐으나 실응답 미확인 (검증필요)
ImplementationState = Literal[
    "ready",
    "not_implemented",
    "unconfirmed",
    "needs_verification",
]

# 군부대·사격장은 공적 DB 부재로 2026-08-14 LH 과업협의(회의록_0814 §4)에서 판정
# 대상 제외가 확정됐다(TIMELINE §1). 원천 자체가 없어 판정 불가.
JUDGMENT_EXCLUDED_REASON = (
    "공적 DB 부재로 판정 대상 제외 (LH 과업협의 2026-08-14 확정 · H-07)"
)


class Category(NamedTuple):
    """화면에 한 줄로 보여줄 유해요소 종류.

    Rule 은 법적 근거 단위라 6개뿐이지만, 실제로 보는 것은 화장장·유흥주점·
    LPG 충전소 같은 개별 종류다. Rule 안에 뭉쳐 놓으면 무엇이 걸렸는지 안 보인다.
    """

    key: str
    label: str
    rule_id: str
    source_label: str
    data_state: CategoryDataState
    note: str = ""
    # 근거 문서 번호(docs/hazards/H-*.md). 법령·근거상의 목 위치 그대로 붙인다.
    doc_ref: str = ""
    # 인허가 캐시(localdata) 데이터셋 키. 있으면 이것으로 후보를 가른다.
    datasets: tuple[str, ...] = ()
    # 그 외에는 facility_type 으로 가른다(opinet·kgs·demo).
    facility_types: tuple[str, ...] = ()
    # 후보 매칭에는 쓰지 않지만 판정이 성립하려면 반드시 적재돼 있어야 하는
    # 데이터셋(예: 공장 AND 의 factoryON 등록공장 CSV). 하나라도 미적재면 이
    # 종류는 dataset_missing 으로 내린다. 후보 매칭 datasets 와 분리해 둔다.
    required_datasets: tuple[str, ...] = ()
    # 건축물대장(용도) 교차확인이 AND 조건인 종류(§6.4 단란주점·테마파크).
    # 교차확인 어댑터가 미구현이라 이내여도 exclusion 으로 확정하지 않고
    # review_required 로 남긴다.
    requires_building_register: bool = False
    # 협의에 따라 판정에서 제외하는 종류(군부대·사격장). 리스트에는 보이되
    # 후보 조회·거리 계산·종합상태 승격·status_counts 집계에 일절 참여하지 않는다.
    # not_applicable + 판정제외 사유로만 표기한다.
    judgment_excluded: bool = False


CATEGORIES: tuple[Category, ...] = (
    # --- H-01 공장 — RB14-FACTORY (50m) ---------------------------------------
    # ⚠️ LH 확정 2026-09-11 (2차 보고 회의 안건 ①): 등록공장 전건을 대상으로 「공장 있음」
    # 검토 표시만 하고 자동 제외는 하지 않는다. 유해공장(소음 50dB·유해물질) 여부는
    # 공적 데이터로 전수 확인이 불가하므로 가~라목 매칭 판정도 하지 않는다. 이에 따라
    # 가~라·인접 5종을 「공장 있음(등록공장)」 한 종류로 합쳤다(화면·CSV 중복 방지).
    # 대기배출·소음배출 원장은 판정 근거로 쓰지 않고, 등록공장과 좌표/PNU 가 맞는 행이
    # 있으면 그 등록공장 note 에 「대기배출 신고 있음」/「소음배출 신고 있음」을 덧붙이는
    # 부가 정보로만 쓴다(service._annotate_registered_factories).
    Category(
        key="factory_registered",
        label="공장 있음 (등록공장)",
        rule_id="RB14-FACTORY",
        source_label="한국산업단지공단 공장등록 필지정보(API)",
        data_state="applied",
        note=(
            "등록공장이 기준 50m(+여유구간) 이내면 「공장 있음」으로 검토 표시한다 "
            "(LH 확정 2026-09-11: 유해공장 여부 공적 데이터로 전수 확인 불가 → "
            "자동 제외·매칭 판정 없음, 담당자 확인). 대기·소음 배출 신고는 등록공장과 "
            "같은 필지/좌표일 때만 부가 정보로 덧붙인다"
        ),
        doc_ref="H-01-공장-공통",
        facility_types=("factory",),
        required_datasets=("factory_registry",),
    ),
    # --- H-02 위험물 저장·처리시설 — RB14-HAZMAT (50m) --------------------------
    # 가목(주유소·석유판매소)은 25m 예외라 RB14-FUEL25 에 있다.
    Category(
        key="lpg_station",
        label="나. 자동차용 LPG 충전소",
        rule_id="RB14-FUEL25",
        source_label=(
            "한국가스안전공사 LPG 충전소 현황(API · 파일 15001643) · 생활안전지도 IF_0033"
        ),
        data_state="applied",
        note=(
            "자동차용 LPG 충전소는 주유소와 같은 25m 를 적용한다 "
            "(LH 확정 2026-09-11 2차 보고 회의 안건 ④). LPG 판매소·저장소·가스제품제조·"
            "도시가스는 위험물 50m 유지. 원본 좌표 19건(14%)이 1km 이상 오류라 "
            "서울시청 기본값·전북 이탈 좌표는 적재 단계에서 격리한다 (§6-4)"
        ),
        doc_ref="H-02-나",
        facility_types=("lpg_station",),
    ),
    Category(
        key="lpg_retailer",
        label="나. LPG 판매소",
        rule_id="RB14-HAZMAT",
        source_label="한국가스안전공사 LPG 판매소 현황(CSV · 좌표 없음)",
        data_state="partial",
        note=(
            "판매소 313건은 좌표가 없어 전건 지오코딩 대상(실패율 5.4%). LH 는 "
            "「충전소만 ○」로 표기했고 판매소 포함은 묵시 수용 — 서면 확인 대기 "
            "(H-02-나 §7-2)"
        ),
        doc_ref="H-02-나",
        facility_types=("lpg_retailer",),
    ),
    Category(
        key="lpg_storage",
        label="나. LPG 저장소",
        rule_id="RB14-HAZMAT",
        source_label="전북 자료 미확보 (체크리스트 「찾지 못함」)",
        data_state="manual",
        note=f"{MANUAL_CHECK_NOTE} · LH 회신문 §7 미확보 6개 목 (H-02-나 §6-3)",
        doc_ref="H-02-나",
    ),
    Category(
        key="hazmat_facility",
        label="다. 위험물 제조소·저장소·취급소",
        rule_id="RB14-HAZMAT",
        source_label="전북 위치자료 미공개 (전남은 위치 공개 · 소방서 협조 요청 대상)",
        data_state="manual",
        note=f"{MANUAL_CHECK_NOTE} · LH 회신문 §7 미확보 6개 목 (H-02-다)",
        doc_ref="H-02-다",
    ),
    Category(
        key="high_pressure_gas",
        label="라·바. 액화가스·고압가스 (같은 원장 · 한 줄 표시)",
        rule_id="RB14-HAZMAT",
        source_label="행안부 일반도시가스업 12 + 고압가스업 1,311 (+특정고압가스 462)",
        data_state="partial",
        note=(
            "라목·바목이 같은 원장을 쓰므로 한 종류로 묶어 이중 계상을 막는다 "
            "(H-02-라바 §7-4). 제2호 공통 전제(자가난방·자가발전 제외)에 따라 "
            "제조구분 「냉동」은 판정 미적용, 업태 「저장소·판매」만 확정 가능, "
            "그 외(제조·충전·공란)는 검토 — 수행팀 임시 처리이며 LH 확인 대기 "
            "(§7-1·§7-2). 특정고압가스는 사용신고 성격이라 확정하지 않는다 (§7-3). "
            "LNG 전용 시설은 미확보"
        ),
        doc_ref="H-02-라바",
        datasets=(
            "city_gas_companies",
            "high_pressure_gas",
            "specific_high_pressure_gas",
        ),
        facility_types=("high_pressure_gas",),
    ),
    Category(
        key="toxic_substance",
        label="마. 유독물 보관·저장·판매시설",
        rule_id="RB14-HAZMAT",
        source_label=(
            "유독물 시설 위치자료 미확보 (환경부 협조 요청 대상) · "
            "생활안전지도 화학물취급시설(IF_0049) 참고 핀"
        ),
        data_state="manual",
        note=f"{MANUAL_CHECK_NOTE} · LH 회신문 §7 미확보 6개 목 (H-02-마)",
        doc_ref="H-02-마",
        # 참고 핀 전용 시설 유형. 판정(data_state=manual)은 그대로다.
        facility_types=("chemical_handling",),
    ),
    Category(
        key="paint_retailer",
        label="사. 도료류 판매소 (근사 적용)",
        rule_id="RB14-HAZMAT",
        source_label="행안부 석유및석유대체연료판매업 96건으로 근사 (도료류 목록 없음)",
        data_state="approximate",
        note=(
            "도료류 자체 목록이 없어 석유대체연료판매업으로 근사한다 — [요청 1] 승인 "
            "범위. 같은 원천이 가목 25m 에도 배정돼([요청 3] 「포함으로 판단됨」) 두 "
            "기준으로 판정되며, 목 배정은 LH 질의 대기 (H-02-가 §7-4 · H-02-사 §7-1). "
            "석유판매업 원장의 「용제판매소」 41건이 실질 근사 후보이나 현재 가목 25m "
            "로 판정된다 (§6-3)"
        ),
        doc_ref="H-02-사",
        datasets=("petroleum_alt_fuel_retailers",),
    ),
    Category(
        key="city_gas_plant",
        label="아. 도시가스 제조시설",
        rule_id="RB14-HAZMAT",
        source_label="한국가스안전공사 가스제품 제조업소 (고유 18곳 · 좌표 없음)",
        data_state="applied",
        note=(
            "「가스제품 제조업소」(압력용기·연소기 제조공장)가 「도시가스 제조시설」에 "
            "맞는지 LH 확인 대기 — 아목 정의에 맞는 원장은 일반도시가스업 12건 "
            "(라목 배정)일 수 있다 (H-02-아 §7-1). 두 파일 중복 7건·행 중복은 적재 시 "
            "제거한다 (§6-3)"
        ),
        doc_ref="H-02-아",
        facility_types=("city_gas_plant",),
    ),
    Category(
        key="explosive_storage",
        label="자. 화약류 저장소",
        rule_id="RB14-HAZMAT",
        source_label="경찰청 자료는 연도별 개수 통계뿐 (위치 없음)",
        data_state="manual",
        note=f"{MANUAL_CHECK_NOTE} · [요청 4] 승인 (H-02-자)",
        doc_ref="H-02-자",
    ),
    Category(
        key="hazmat_other_similar",
        label="차. 그 밖에 가~자목과 비슷한 것",
        rule_id="RB14-HAZMAT",
        source_label=(
            "대상 미정의 — 유사 시설 발견 시 협의 · 생활안전지도 폐기물처리시설(IF_0051) 참고 핀"
        ),
        data_state="negotiate",
        note=(
            "개별 협의 항목. 후보 판별 절차가 정해지지 않아 자동 판정하지 않는다 "
            "(H-02-차 §7-1). 특정고압가스 462건은 라·바목 종류에서 검토로 다룬다"
        ),
        doc_ref="H-02-차",
        # 참고 핀 전용 시설 유형(폐기물처리시설). 판정(negotiate)은 그대로다.
        facility_types=("waste_treatment",),
    ),
    # --- 25m 예외 3종 — RB14-FUEL25 (25m · 주택 전 유형) ------------------------
    Category(
        key="gas_station",
        # 가목은 「주유소 및 석유판매소」 한 목이지만 원장이 달라 두 종류로 나눈다(가-1·가-2).
        label="가-1. 주유소 (기계식 세차설비 포함)",
        rule_id="RB14-FUEL25",
        source_label="행안부 석유판매업(업태 주유소) · 생활안전지도 IF_0033 · 오피넷 보조",
        data_state="applied",
        note=(
            "산업통상자원부 주유소 등록현황(11_gas_stations)은 2015~2025 변동 이력이라 "
            "판정 원천에서 뺀다 (H-02-가 §6-2). 휴업 53건 처리는 LH 확인 대기 (§7-1) — "
            "확정 전에는 검토로 남긴다"
        ),
        doc_ref="H-02-가",
        facility_types=("gas_station",),
    ),
    Category(
        key="oil_retailer",
        label="가-2. 석유 판매소 (일반·용제·부생연료유) · 석유대체연료판매업",
        rule_id="RB14-FUEL25",
        source_label="행안부 석유판매업 · 석유및석유대체연료판매업",
        data_state="applied",
        note=(
            "일반·용제·부생연료유판매소는 LH 기존 목록 선례로 포함. 특수판매소 1건은 "
            "폐업이라 실질 영향 0 (H-02-가 §7-2). 석유대체연료판매업은 [요청 3] "
            "「포함으로 판단됨」(25m) — 사목 배정과의 정리는 LH 질의 대기 (§7-4)"
        ),
        doc_ref="H-02-가",
        datasets=("oil_retailers", "petroleum_alt_fuel_retailers"),
    ),
    Category(
        key="cng_station",
        label="자동차용 천연가스충전소 (LH 내부규정)",
        rule_id="RB14-FUEL25",
        source_label=(
            "가스안전공사 전국 도시가스충전소 현황(ODcloud 15001508 · 위경도 보유) "
            "· 경남 천연가스 충전소(15055157 · 지오코딩) 보조"
        ),
        data_state="applied",
        note=(
            "2026-09-14 조사로 전국 공개 API(15001508, 분기 갱신·192행) 확인 → API 가 "
            "우선, 활용신청 승인 전(401)에는 로컬 CSV 로 판정한다. 원본 좌표 8건 전부 "
            "714~3,779m 오류 → VWorld·카카오 실측 좌표로 교체해 적재한다 (H-03 §6-2). "
            "현대자동차 전주공장 자가 충전소 2건 포함 여부는 LH 확인 대기 (§7-2). "
            "LNG 충전소 미확보 (§7-4)"
        ),
        doc_ref="H-03",
        facility_types=("cng_station",),
    ),
    # --- H-04 위락시설 — RB14-AMUSEMENT (25m · 주택 다자녀만) -------------------
    Category(
        key="entertainment_bar",
        label="나. 유흥주점",
        rule_id="RB14-AMUSEMENT",
        source_label="행안부 유흥주점영업 (영업 881건)",
        data_state="applied",
        note="교차 조건 없음. 좌표 품질 최상급(중앙값 3m) (H-04-나)",
        doc_ref="H-04-나",
        datasets=("entertainment_bars",),
        facility_types=("entertainment_bar",),
    ),
    Category(
        key="singing_bar",
        label="가. 단란주점 (제2종 근린생활시설 비해당)",
        rule_id="RB14-AMUSEMENT",
        source_label="행안부 단란주점영업 (영업 402건) × 건축물대장 용도",
        data_state="applied",
        note=(
            "제2종 근린생활시설(바닥면적 150㎡ 미만) 비해당이 AND 조건. 건축물대장 "
            "용도·면적 확인 전에는 「검토 필요」 상한 (H-04-가 §7-1)"
        ),
        doc_ref="H-04-가",
        datasets=("singing_bars",),
        facility_types=("singing_bar",),
        requires_building_register=True,
    ),
    Category(
        key="theme_park_comprehensive",
        label="다-1. 종합테마파크업 (구 유원시설업)",
        rule_id="RB14-AMUSEMENT",
        source_label="행안부 종합테마파크업 × 건축물대장 용도",
        data_state="applied",
        note=(
            "제2종 근린생활·운동시설 비해당이 두 겹 AND. 원장 건축물용도가 "
            "「체육시설」이면 즉시 제외, 「근린생활시설」은 제2종 여부 추가 확인 "
            "(H-04-다 §6-1)"
        ),
        doc_ref="H-04-다",
        datasets=("comprehensive_amusement_facilities",),
        facility_types=("theme_park_comprehensive",),
        requires_building_register=True,
    ),
    Category(
        key="theme_park_general",
        label="다-2. 일반테마파크업 (구 유원시설업)",
        rule_id="RB14-AMUSEMENT",
        source_label="행안부 일반테마파크업 × 건축물대장 용도",
        data_state="applied",
        note=(
            "제2종 근린생활·운동시설 비해당이 두 겹 AND. 원장 건축물용도가 "
            "「체육시설」이면 즉시 제외, 「근린생활시설」은 제2종 여부 추가 확인 "
            "(H-04-다 §6-1)"
        ),
        doc_ref="H-04-다",
        datasets=("general_amusement_facilities",),
        facility_types=("theme_park_general",),
        requires_building_register=True,
    ),
    Category(
        key="theme_park_other",
        label="다-3. 기타테마파크업 (구 유원시설업)",
        rule_id="RB14-AMUSEMENT",
        source_label="행안부 기타테마파크업 × 건축물대장 용도",
        data_state="applied",
        note=(
            "제2종 근린생활·운동시설 비해당이 두 겹 AND. 원장 건축물용도가 "
            "「체육시설」이면 즉시 제외, 「근린생활시설」은 제2종 여부 추가 확인 "
            "(H-04-다 §6-1)"
        ),
        doc_ref="H-04-다",
        datasets=("amusement_facilities_other",),
        facility_types=("theme_park_other",),
        requires_building_register=True,
    ),
    Category(
        key="dance_hall",
        label="마-1. 무도장",
        rule_id="RB14-AMUSEMENT",
        source_label="행안부 무도장업 (영업 3건)",
        data_state="applied",
        note=(
            "AND 조건 없음. 무도학원업과 좌표·주소가 같은 이중 등록 2건은 한 시설로 "
            "묶는다 (H-04-마 §6-5)"
        ),
        doc_ref="H-04-마",
        datasets=("dance_halls",),
        facility_types=("dance_hall",),
    ),
    Category(
        key="dance_academy",
        label="마-2. 무도학원",
        rule_id="RB14-AMUSEMENT",
        source_label="행안부 무도학원업 (영업 17건 · 08-26 재추출본)",
        data_state="applied",
        note=(
            "AND 조건 없음. 무도장업과 이중 등록된 시설은 무도장 쪽 한 줄로 표시한다 "
            "(H-04-마 §6-5)"
        ),
        doc_ref="H-04-마",
        datasets=("dance_academies",),
        facility_types=("dance_academy",),
    ),
    Category(
        key="casino",
        label="바. 카지노영업소",
        rule_id="RB14-AMUSEMENT",
        source_label="자료 미확보 (체크리스트 「찾지 못함」)",
        data_state="manual",
        note=(
            f"{MANUAL_CHECK_NOTE} · LH 회신문 §7 미확보 6개 목. 전북 실재 여부 "
            "확인 시 종결 가능 (H-04-바 §7-1)"
        ),
        doc_ref="H-04-바",
    ),
    # --- H-05 일반숙박시설 — RB14-LODGING (25m · 다자녀 · 오피스텔 포함) ---------
    Category(
        key="general_lodging",
        label="일반숙박시설 (관광숙박·생활숙박 제외 — LH 확정 2026-09-11)",
        rule_id="RB14-LODGING",
        source_label="행안부 숙박업 원장 (여관·여인숙·일반호텔·숙박업 기타)",
        data_state="applied",
        note=(
            "관광숙박시설 8종(관광호텔·수상관광호텔·한국전통호텔·가족호텔·호스텔·"
            "소형호텔·의료관광호텔·휴양콘도미니엄)은 업태 기준으로 제외한다 "
            "(LH 08-24 17:08 정정). 생활숙박업(숙박업(생활))도 제외한다 — 일반숙박시설만 "
            "판정 대상 (LH 확정 2026-09-11 2차 보고 회의 안건 ⑥)"
        ),
        doc_ref="H-05",
        datasets=("lodgings",),
        facility_types=("lodging",),
    ),
    # --- H-06·H-07 화장장·군부대 — RB14-CREMATION-MILITARY (500m · 전 유형) -------
    Category(
        key="crematorium",
        label="화장장",
        rule_id="RB14-CREMATION-MILITARY",
        source_label="보건복지부 전국 화장시설 현황(API 62건 · 주소 지오코딩)",
        data_state="applied",
        note=(
            "전주시승화원은 「효자동3가 산 170-1」(콩쥐팥쥐로 1705-138) 기준 좌표로 "
            "고정한다 — 「효자동3가 3」 지오코딩값은 1,931m 오류 (H-06 §6-3). "
            "봉안시설·자연장지는 포함하지 않는다 (§7-2)"
        ),
        doc_ref="H-06",
        facility_types=("crematorium",),
    ),
    Category(
        key="military_base",
        label="군부대",
        rule_id="RB14-CREMATION-MILITARY",
        source_label="공적 데이터 부재",
        data_state="missing",
        note="판정 미적용 — 공적 DB 부재로 08-14 LH 과업협의에서 판정 대상 제외 (H-07)",
        doc_ref="H-07",
        judgment_excluded=True,
    ),
    Category(
        key="shooting_range",
        label="사격장",
        rule_id="RB14-CREMATION-MILITARY",
        source_label="공적 데이터 부재",
        data_state="missing",
        note="판정 미적용 — 공적 DB 부재로 08-14 LH 과업협의에서 판정 대상 제외 (H-07)",
        doc_ref="H-07",
        judgment_excluded=True,
    ),
)

CATEGORY_BY_KEY: dict[str, Category] = {cat.key: cat for cat in CATEGORIES}


# ---------------------------------------------------------------------------
# §6.5 일반숙박시설 필터
# ---------------------------------------------------------------------------
# 관광숙박시설 8종과 취사 가능 콘도는 판정대상에서 제외한다(LH 2026-08-24 17:08 정정).
# 판별은 숙박업 원장의 업태(BZSTAT_SE_NM)로 한다 — H-05 §5·§6-1. 여관업·여인숙업·
# 일반호텔·숙박업 기타·숙박업(생활)은 대상, 관광호텔·휴양콘도미니엄업 등 8종은 제외.
TOURIST_ACCOMMODATION_BUSINESS_TYPES: tuple[str, ...] = (
    "관광호텔",
    "수상관광호텔",
    "한국전통호텔",
    "가족호텔",
    "호스텔",
    "소형호텔",
    "의료관광호텔",
    "휴양콘도",
)
# 업태가 없는 원천(데모·보조)에서만 상호·라벨 키워드로 대신 가른다.
TOURIST_ACCOMMODATION_KEYWORDS: tuple[str, ...] = (
    "관광호텔",
    "수상관광호텔",
    "한국전통호텔",
    "가족호텔",
    "호스텔",
    "소형호텔",
    "의료관광호텔",
    "휴양콘도미니엄",
    "취사",
    "콘도",
)


# ---------------------------------------------------------------------------
# §7 ③ 운영상태 필터
# ---------------------------------------------------------------------------
# 폐업·취소·말소는 판정 투입 전 제외. 상태 공란은 확정하지 않고 review_required 로
# 남긴다. 휴업은 일시 중단이므로 시설이 있는 것으로 보아 영업과 같이 판정 대상에
# 포함한다(LH 확정 2026-09-11 2차 보고 회의 안건 ②). 별도 「휴업」 검토 표시는 두지
# 않고 운영상태 열의 「휴업」 표기만 남긴다(business_status 원문 유지).
CLOSED_STATUS_KEYWORDS: tuple[str, ...] = ("폐업", "취소", "말소", "직권말소", "폐쇄")

OperatingState = Literal["active", "excluded", "hold"]


def operating_state(status: str) -> OperatingState:
    """운영상태 문자열을 판정 처리 구분으로 환산한다."""

    text = (status or "").strip()
    if not text:
        # 상태 공란은 확정하지 않고 검토 필요로 남긴다.
        return "hold"
    if any(keyword in text for keyword in CLOSED_STATUS_KEYWORDS):
        return "excluded"
    # 휴업 포함: 영업과 동일하게 판정 대상(active)으로 본다 (LH 확정 2026-09-11).
    return "active"


# ---------------------------------------------------------------------------
# §8 미확정 사항 (화면에 그대로 노출)
# ---------------------------------------------------------------------------
class PendingItem(NamedTuple):
    item: str
    status: str


PENDING_ITEMS: tuple[PendingItem, ...] = (
    # INDEX §10-1 — LH 판정 기준 결정(수행팀이 정할 수 없는 것). 답이 나오면
    # 해당 Category 의 data_state·note 와 service 판정 분기를 함께 고친다.
    PendingItem(
        "「지식산업센터 판정 제외」의 의미 — 신청지가 지산인 경우·건물 내 대기배출 사업장",
        "LH 회신 대기 (H-01-공장-공통 §6-3)",
    ),
    PendingItem(
        "석유대체연료 판매업 96건 — 가목(25m)인가 사목(50m)인가",
        "LH 질의 대기 · 현재 두 기준 모두 판정 (H-02-가 §7-4 · H-02-사 §7-1)",
    ),
    PendingItem(
        "LPG 판매소 313건을 나목 대상으로 확정할 것인가",
        "LH 서면 확인 대기 · 현재 포함 (H-02-나 §7-2)",
    ),
    PendingItem(
        "사업장 자가 CNG 충전소(현대자동차 전주공장 2건)도 대상인가",
        "LH 확인 대기 · 현재 포함 (H-03 §7-2)",
    ),
    PendingItem(
        "고압가스 「제조·충전·공란」 업태(자가설비 아님) 포함 여부",
        "LH 확인 대기 · 현재 검토로 표시 (냉동·기관 자가설비(저장소/충전)는 "
        "LH 확정 2026-09-11 로 판정 미적용) (H-02-라바 §7-2)",
    ),
    PendingItem(
        "「가스제품 제조업소」를 「도시가스 제조시설」로 볼 수 있는가 (28건 전부)",
        "LH 확인 대기 · 현재 근사로 판정 (H-02-아 §7-1)",
    ),
    PendingItem(
        "단란주점·테마파크 제2종 근린생활시설 판별 (건축물대장 용도·면적)",
        "건축물대장 확보 전 「검토 필요」 상한 (H-04-가 §7-1 · H-04-다 §7-1)",
    ),
    # 수행팀 내부·자료 미결 중 판정에 직접 닿는 것.
    PendingItem(
        "표준셋 수록 원칙 — CONFIRMED 만 vs REVIEW 동봉 (단란주점 402·유원시설 142건)",
        "팀 안건 ③ 미논의 (H-04-가 §7-2 · H-04-다 §7-2)",
    ),
    PendingItem(
        "위험물 통합 정본의 목 구분 소실 (가목 25m / 나~차목 50m)",
        "팀 안건 ④ 미논의 · 박진주 대표 차기판 재확인 (H-02-라바 §7-5)",
    ),
    PendingItem(
        "검증 표본에 주택/오피스텔 구분·다자녀 사례 없음 (신청분 80% 오피스텔)",
        "LH 8-29 약속 이행 요청 (TIMELINE §6-2·6-3)",
    ),
    PendingItem(
        "매입 공고문 원본 미확보 (25m 3종 「동시 매도신청 시 예외」 단서 문언)",
        "LH 요청 1순위 (MEASUREMENT §1)",
    ),
)


# ---------------------------------------------------------------------------
# 규칙팩 메타 (단일 팩)
# ---------------------------------------------------------------------------
# 팩 식별자는 API·프런트·대조 도구가 참조하므로 바꾸지 않는다. 버전만 올린다.
RULE_PACK_ID = "lh-rulebook-v1.4"
RULE_PACK_VERSION = "1.6"
# 규칙 근거. 룰북 v1.4 파생물이 아니라 LH 원본·회신 기준의 항목 문서 묶음이다.
RULE_PACK_BASIS = (
    "docs/hazards (INDEX·TIMELINE·MEASUREMENT + 23개 항목, 2026-09-02 검증본)"
)
