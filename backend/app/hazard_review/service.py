from __future__ import annotations

import asyncio
import contextvars
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from app.hazard_review.data_sources import (
    HazardDataSource,
    api_source,
    demo_source,
    local_source,
    localdata_source,
)
from app.hazard_review.models import (
    HazardCategorySummary,
    HazardEvidenceGrade,
    HazardFacility,
    HazardFinding,
    HazardOccupiedParcel,
    HazardPendingItem,
    HazardReviewRequest,
    HazardReviewResult,
    HazardRuleBand,
    HazardRuleDefinition,
    HazardRulePack,
    HazardScoreItem,
    HazardSourceState,
    HazardSourceStatus,
)
from app.hazard_review.rulebook import (
    APPLICATION_TYPE_LABELS,
    CATEGORIES,
    Category,
    HOUSING_TYPE_LABELS,
    HazardReviewStatus,
    ImplementationState,
    JUDGMENT_EXCLUDED_REASON,
    PENDING_ITEMS,
    RULE_PACK_ID,
    RULE_PACK_VERSION,
    RULES,
    STATUS_LABELS,
    STATUS_PRIORITY,
    MANUAL_CHECK_DATA_STATES,
    NON_JUDGED_DATA_STATES,
    RULE_PACK_BASIS,
    TOURIST_ACCOMMODATION_BUSINESS_TYPES,
    TOURIST_ACCOMMODATION_KEYWORDS,
    ApplicationType,
    HousingType,
    Rule,
    operating_state,
    threshold_for,
    thresholds_for,
)
from app.models import Coordinates
from app.services.geo import (
    buffer_rings,
    distance_point_to_polygons_m,
    distance_polygons_to_polygon_m,
    haversine_meters,
    max_extent_multi,
    nearest_boundary_point_multi,
    offset_coordinates,
)
from app.services.facility_store import FacilityStore
from app.services.kakao import KakaoClient
from app.services.cng import CngStationClient
from app.services.factory_registry import FactoryRegistryClient
from app.services.kgs import KgsLpgClient, PublicDataAPIError
from app.services.parcel_sanity import parcel_rejection_reason
from app.services.localdata import DATASET_BY_KEY
from app.services.building_register import (
    BuildingRegisterAPIError,
    BuildingRegisterClient,
    UseVerdict,
)
from app.services.cadastral_local import CadastralLocalStore
from app.services.crematorium import CrematoriumAPIError, CrematoriumClient
from app.services.local_wiring import LocalSourcesBundle
from app.services.pnu_resolver import PnuResolver
from app.services.noise_emission import NoiseEmissionAPIError, NoiseEmissionClient
from app.services.opinet import OpinetAPIError, OpinetClient
from app.services.safemap import SafemapAPIError, SafemapFuelClient, SafemapStation
from app.services.vworld import VWorldAPIError, VWorldClient, classify_zoning


HazardProgressCallback = Callable[
    [str, str, str, int, int | None, str],
    Awaitable[None],
]

# 후보 거리는 점 좌표 기준이므로, 시설 경계가 기준거리 안으로 들어올 수 있는
# 여유구간까지 조회한 뒤 경계 확인 대상으로 남긴다.
# 룰북 정본 값은 여전히 100 하나이며, 이 상수를 남긴다. HAZARD_BOUNDARY_BUFFER_M
# 환경변수는 안건 ⑤(여유구간 150 vs 100) 실영향 실측 전용 스위치이므로
# 미지정 시 기본값은 정본 값 100 그대로다.
BOUNDARY_BUFFER_M = int(os.environ.get("HAZARD_BOUNDARY_BUFFER_M", "100"))

# 예비검색 슬랙(m). 판정 여유구간(BOUNDARY_BUFFER_M)과는 분리된 값이다. 후보 1차
# 선별은 점 좌표 거리로 하는데, 시설 폴리곤이 크면 점 거리는 여유구간 밖이어도
# 경계 거리는 안일 수 있어(조준환 엔진 회귀에서 실측 5건 누락) 경계 부착 대상
# 후보를 점 거리 기준으로 좁게 자르면 놓친다. 그래서 경계 부착 대상 후보는
# search_limit_m + SEARCH_SLACK_M 까지 넓게 잡고, 경계 부착 후 거리로 최종
# 판정 후보(<= search_limit_m)만 남긴다. 슬랙을 넓혀도 판정 여유구간은 변하지 않는다.
SEARCH_SLACK_M = 150

# 판정에는 쓰지 않는, 지도 참고 표시 전용 반경. 판정창(임계+버퍼) 밖이지만 이
# 반경 이내인 시설을 nearby_facilities 로 실어 담당자가 "근처에 뭐가 있는지"를
# 참고하게 한다. status·candidate_count·거리·심사표·CSV 는 이 값에 영향받지 않는다.
HAZARD_CONTEXT_RADIUS_M = int(os.environ.get("HAZARD_CONTEXT_RADIUS_M", "1000"))

SOURCE_STATE_SCORE: dict[str, int] = {
    "connected": 100,
    "partial": 50,
    "unavailable": 0,
    "failed": 0,
}

FRESH_SOURCE_STATES = {"connected", "partial"}

# 실제 지적도에서 받은 필지로 인정하는 geometry_source.
CADASTRAL_SOURCES = {"official_polygon", "parcel_polygon"}

# 공식 등록 시설과 이 거리 안에 있는 후보는 같은 시설로 본다.
OFFICIAL_DEDUPE_M = 40

# 양쪽 다 연속지적도 필지 경계로 잰 경우. LH 공고가 말하는 기준이다.
PARCEL_MEASUREMENT = "대지경계 ↔ 대지경계 최단거리"
# 사업지만 경계이고 시설은 점 좌표인 경우. 실제보다 멀게 나온다.
BOUNDARY_MEASUREMENT = "신청 대지경계 ↔ 시설 후보점 최단거리"
POINT_MEASUREMENT = "주소점 ↔ 시설 후보점 예비거리"

# 등록공장이 기준거리 이내일 때의 고정 note (LH 확정 2026-09-11 안건 ①).
# 「공장 있음」 검토 표시만 하고 자동 제외·가~라목 매칭 판정은 하지 않는다.
FACTORY_PRESENT_NOTE = (
    "공장 있음 — 등록공장 소재 (LH 확정 2026-09-11: 유해공장 여부 공적 데이터로 "
    "전수 확인 불가 → 자동 제외·매칭 판정 없음, 담당자 확인)"
)
# 지식산업센터 지원시설 예외 note.
KNOWLEDGE_INDUSTRY_NOTE = "지식산업센터 지원시설 — 건축법상 공장 아님"

# 원천 조회 실패 note 에 쓰는 사람이 읽을 원천 이름.
# 판정 1회가 시작 시점의 로컬 원천 번들을 그 판정 내내 고정해서 보게 하는 핀.
# 워밍업이 self.local_sources 를 빈 묶음→완전 묶음으로 교체(단조적)하더라도,
# 이미 시작된 판정은 처음 붙잡은 번들만 본다. 그래야 후보 수집 시점에는 비었던
# 원천이 await 사이에 채워져 연결성 검사에서 「연결됨」으로 보이는 일이 없다
# (조회하지 않은 원천이 no_conflict_in_snapshot 으로 둔갑하는 것을 막는다).
# ContextVar 는 asyncio 태스크별로 격리되므로 동시 판정 간섭이 없다.
_pinned_local_sources: contextvars.ContextVar["LocalSourcesBundle | None"] = (
    contextvars.ContextVar("_pinned_local_sources", default=None)
)


SOURCE_FAILURE_LABELS: dict[str, str] = {
    "localdata": "행정안전부 지방행정인허가 대장",
    "opinet": "한국석유공사 오피넷",
    "kgs": "한국가스안전공사 LPG 현황",
    "factory_registry": "한국산업단지공단 공장등록 필지정보",
    "cng": "한국가스안전공사 도시가스(CNG) 충전소 현황",
    "safemap": "생활안전지도 전국 주유시설 현황",
    "crematorium": "보건복지부 전국 화장시설 현황",
    "noise_emission": "전국 소음진동배출시설 표준데이터",
}

# 데이터 상태가 applied 인데 전국 단위 원천이 아직 안 붙은 종류의 note.
APPLIED_NOT_CONNECTED_NOTE = (
    "룰북상 판정적용 대상이나 현재 원천 미연결 — 연결 시 자동판정"
)

# 공장: factoryON 등록공장 원천이 적재되기 전까지 「공장 있음」 판정 불가.
# 등록공장 원천이 없으면 공장 소재 여부를 확인할 수 없으므로 dataset_missing 이다.
FACTORY_REGISTRY_MISSING_NOTE = (
    "등록공장 원천 없음 — 산단공 공장등록 API 키(PUBLIC_DATA_SERVICE_KEY)를 연결하면 판정"
)

# §6.4 단란주점·테마파크: 건축물대장 용도 교차확인이 AND 조건이나 어댑터 미구현.
# 확인 안 된 조건으로 매입제외를 확정하지 않는다(룰북 §3).
BUILDING_REGISTER_NOTE = (
    "건축물대장 용도 교차확인 필요 (제2종 근린생활시설·운동시설 비해당 확인)"
)

# 생활안전지도 주유시설 중 상표·lpg_yn 이 전부 공란이라 주유소·LPG 어느 쪽으로도
# 가를 수 없는 시설. 확인 전에는 매입제외로 확정하지 않고 review_required 로만 남긴다.
SAFEMAP_UNCLASSIFIED_NOTE = (
    "생활안전지도 원천에 시설 구분 정보 없음 — 주유소·LPG충전소 여부 확인 필요."
)

# 감사(§F)가 "확인 필요"로 둔 미검증 엔드포인트. 장애가 아니라 미검증임을 밝힌다.
ENDPOINT_UNVERIFIED_NOTE = "엔드포인트 미검증 — 적재 시 응답 확인 필요"

# 고압가스 원장(라·바목) 자가설비·업태 필터 — H-02-라바 §7-1·§7-2. 제2호 공통 전제
# 「자가난방·자가발전 등 목적의 저장시설은 제외」에 따라 제조구분 「냉동」(건물 냉방용
# 냉동기)은 판정 미적용, 업태 「저장소·판매」만 확정 가능, 그 외는 검토. 수행팀 임시
# 처리(조준환 엔진과 동일 기준)이며 LH 서면 확인 전이다.
GAS_SELF_USE_KEYWORDS: tuple[str, ...] = ("냉동",)
GAS_CONFIRMABLE_KEYWORDS: tuple[str, ...] = ("저장", "판매")
GAS_SELF_USE_NOTE = (
    "제조구분 「냉동」 — 건물 냉방용 자가설비로 보아 판정 미적용 "
    "(제2호 공통 전제 · H-02-라바 §7-1, LH 확인 대기)"
)
GAS_REVIEW_NOTE = (
    "업태 「제조·충전·공란」 — 바목 문언(충전소·판매소·저장소) 해당 여부 미확정이라 "
    "매입제외로 확정하지 않는다 (H-02-라바 §7-2)"
)
SPECIFIC_GAS_REVIEW_NOTE = (
    "특정고압가스 사용신고 시설 — 목 배정·공통 전제 해당 여부 미확정 (H-02-라바 §7-3)"
)

# 고압가스 자가설비(기관 자체 사용) 제외 — LH 확정 2026-09-11 (2차 보고 회의 안건 ③).
# 병원 저장소·소방서 공기충전 등 자체 사용 목적의 고압가스 시설은 유해시설에서 제외하고
# 검토 표시도 하지 않는다(nearby 참고 핀에도 올리지 않는다). 냉동·냉방설비 미적용 규칙은
# 기존 유지(_classify_gas_facilities 의 냉동 처리).
#
# 원장 실측(data/facilities.db, 2026-09-11):
#   high_pressure_gas.category(=BZSTAT_SE_NM, 업태): 제조 / 저장소 / 판매
#   high_pressure_gas.extra['MNFTR_SE_NM'](제조구분): 냉동 / 일반 / 충전 / 특정 / (공란)
# 판별: 명칭이 기관 패턴이면서 업태가 「저장소」이거나 제조구분이 「충전」이면 자가설비로
# 본다(병원 저장소 = 업태 저장소 · 소방서 공기충전 = 제조구분 충전).
GAS_SELF_USE_STORAGE_KEYWORDS: tuple[str, ...] = ("저장",)
GAS_SELF_USE_FILLING_KEYWORDS: tuple[str, ...] = ("충전",)
# 특정고압가스 사용신고 원장(specific_high_pressure_gas)의 사용목적(USE_PRPS). 사용신고
# 시설은 가스를 쓰는 곳이라 업태·제조구분 컬럼이 비어 있어 위 판별에 걸리지 않았다
# (2026-09-14 건국대학교병원 사례: 사용목적 「의료용」이 50m 위험물로 표시). 사용목적이
# 「의료」면 기관 명칭과 무관하게 자체 사용이고, 기관 명칭 패턴이면서 사용목적이 적혀
# 있으면(= 사용신고 행) 역시 자체 사용으로 본다.
GAS_SELF_USE_PURPOSE_KEYWORDS: tuple[str, ...] = ("의료",)
GAS_INSTITUTION_NAME_KEYWORDS: tuple[str, ...] = (
    "소방", "119", "병원", "의료원", "보건소", "요양", "대학교", "학교",
    "연구소", "연구원", "수자원공사", "토지주택공사", "전기안전공사", "가스안전공사",
    "농업과학원", "도서관", "시청", "군청", "경찰", "교육청", "공항", "철도",
)


def is_self_use_gas(
    name: str,
    business_category: str,
    manufacture_type: str,
    use_purpose: str = "",
) -> bool:
    """고압가스 자가설비(기관 자체 사용) 행인지 판별한다 (LH 확정 2026-09-11 안건 ③).

    순수 함수. 명칭이 기관 패턴에 해당하면서, 업태가 「저장소」이거나 제조구분이
    「충전」이거나 사용목적이 적혀 있으면(특정고압가스 사용신고 행) 자가 사용 목적으로
    보아 유해시설 판정·검토·참고 핀에서 완전히 뺀다. 사용목적이 「의료」면 명칭과
    무관하게 자가 사용이다. 냉동(냉방설비)은 여기서 다루지 않는다 — 기존
    _classify_gas_facilities 의 판정 미적용 처리를 그대로 둔다.
    """

    purpose = use_purpose or ""
    if any(word in purpose for word in GAS_SELF_USE_PURPOSE_KEYWORDS):
        return True
    name = name or ""
    if not any(word in name for word in GAS_INSTITUTION_NAME_KEYWORDS):
        return False
    business = business_category or ""
    manufacture = manufacture_type or ""
    is_storage = any(word in business for word in GAS_SELF_USE_STORAGE_KEYWORDS)
    is_filling = any(word in manufacture for word in GAS_SELF_USE_FILLING_KEYWORDS)
    return is_storage or is_filling or bool(purpose.strip())

# 테마파크(다목) 건축물용도 필터 — H-04-다 §6-1. 원장 BLDG_USG_NM 이 「체육시설」이면
# 운동시설 해당으로 즉시 제외, 「근린생활시설」은 제2종 여부 추가 확인(검토).
THEME_PARK_SPORTS_USE_KEYWORDS: tuple[str, ...] = ("체육시설", "운동시설")
THEME_PARK_NEIGHBORHOOD_USE_KEYWORDS: tuple[str, ...] = ("근린생활",)

# 일반숙박 생활숙박업 — 제외 확정 (LH 확정 2026-09-11 안건 ⑥). 업태 「숙박업(생활)」
# 및 표기 변형(괄호·공백)을 「생활」 포함으로 가려 판정대상에서 뺀다. 원장 실측
# (data/facilities.db, 2026-09-11): lodgings.category 에 「숙박업(생활)」 6,855건.
LIVING_ACCOMMODATION_KEYWORDS: tuple[str, ...] = ("생활",)

# 무도장·무도학원 이중 등록 — H-04-마 §6-5. 한 사업장이 두 업종을 모두 등록한 경우
# 좌표·주소가 완전히 같다. 그대로 두면 같은 시설이 두 번 걸리므로 한 줄로 묶는다.
DANCE_DUPLICATE_M = 1.0


def _rule_definition(rule: Rule) -> HazardRuleDefinition:
    """Rule 하나를 응답용 정의로 옮긴다. 임계거리는 매트릭스 표로 담는다."""

    thresholds: dict[str, int | None] = {}
    for housing in ("house", "officetel"):
        for application in ("general", "multi_child", "newlywed", "youth", "senior"):
            thresholds[f"{housing}:{application}"] = threshold_for(
                rule.rule_id, housing, application  # type: ignore[arg-type]
            )
    return HazardRuleDefinition(
        rule_id=rule.rule_id,
        label=rule.label,
        matrix_column=rule.column,
        legal_reference=rule.legal_reference,
        thresholds=thresholds,
    )


RULE_PACK = HazardRulePack(
    id=RULE_PACK_ID,
    title="LH 매입제외 유해요소 판정 규칙 v1.6",
    version=RULE_PACK_VERSION,
    effective_from="2026-09-02",
    status="approved",
    source_document_url="",
    note=(
        "LH 승인 「매입제외시설 최종정리」 §2 종합표(2026-08-24)와 「위험물 시설현황 "
        "종합」 유형별 매트릭스(08-20)를 뼈대로, LH 회신·회의록으로 확정된 사항을 "
        f"반영한 1차 매입제외 판정 규칙팩입니다. 근거: {RULE_PACK_BASIS}. "
        "신청유형별 매트릭스로 임계거리가 결정됩니다."
    ),
    rules=[_rule_definition(rule) for rule in RULES],
)


def get_rule_packs() -> list[HazardRulePack]:
    return [RULE_PACK]


def get_rule_pack(rule_pack_id: str) -> HazardRulePack | None:
    return RULE_PACK if rule_pack_id == RULE_PACK_ID else None


def demo_facilities(center: Coordinates) -> list[dict[str, object]]:
    """DEMO_MODE 화면 확인용 표본. 판정 종류를 두루 밟도록 배치한다.

    실제 시설이 아니라 UI·판정 흐름 확인용이다. 거리는 주소점 기준으로 계산된다.
    """

    def row(
        row_id: str,
        name: str,
        facility_type: str,
        label: str,
        north: float,
        east: float,
        status: str = "정상영업",
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "id": row_id,
            "name": name,
            "facility_type": facility_type,
            "label": label,
            "coordinates": offset_coordinates(center, north, east),
            "status": status,
            "metadata": metadata or {},
        }

    return [
        # 화장장·군부대·사격장 (500m)
        row("demo-crematorium", "검증용 화장장", "crematorium", "화장장", 0, 480),
        # 공장 (50m) — 등록공장 소재건과 폐업건 (LH 확정 2026-09-11: 검토 표시만)
        row(
            "demo-factory-registered", "검증용 등록공장", "factory",
            "factoryON 등록공장", 0, 45,
            metadata={"factory_registered": True},
        ),
        row(
            "demo-factory-closed", "검증용 폐업공장", "factory",
            "폐업공장", 6, 6, status="폐업",
            metadata={"factory_registered": True},
        ),
        # 위험물 (50m)
        row("demo-lpg-station", "검증용 LPG 충전소", "lpg_station", "LPG 충전소", 35, 0),
        row(
            "demo-high-pressure", "검증용 고압가스업소", "high_pressure_gas",
            "고압가스", 0, 40, metadata={"gas_type_confirmed": True},
        ),
        # 주유·석유·CNG (25m)
        row("demo-gas-station", "검증용 주유소", "gas_station", "주유소", 0, 20),
        row(
            "demo-oil-retailer", "검증용 석유판매업소", "oil_retailer",
            "석유판매업소", 0, -18, metadata={"dataset": "oil_retailers"},
        ),
        row("demo-cng", "검증용 CNG 충전소", "cng_station", "CNG 충전소", 0, -22),
        # 위락시설 (25m · 다자녀)
        row("demo-bar", "검증용 유흥주점 별밤", "entertainment_bar", "유흥주점", 0, 21),
        row("demo-singing", "검증용 단란주점 노래", "singing_bar", "단란주점", 0, -20),
        row(
            "demo-theme", "검증용 종합테마파크", "theme_park_comprehensive",
            "종합테마파크업", 0, 24,
        ),
        row("demo-dance", "검증용 무도장", "dance_hall", "무도장", 0, -24),
        row("demo-dance-academy", "검증용 무도학원", "dance_academy", "무도학원", 3, -24),
        # 일반숙박 (25m · 다자녀) — 일반숙박과 제외 대상 관광호텔
        row("demo-lodging", "검증용 행복모텔", "lodging", "일반숙박시설", 0, 18),
        row("demo-tourist", "검증용 관광호텔 스카이", "lodging", "관광숙박시설", 0, -15),
    ]


class HazardReviewService:
    def __init__(
        self,
        kakao: KakaoClient,
        demo_mode: bool,
        opinet: OpinetClient | None = None,
        kgs_lpg: KgsLpgClient | None = None,
        facility_store: FacilityStore | None = None,
        vworld: VWorldClient | None = None,
        safemap: SafemapFuelClient | None = None,
        crematorium: CrematoriumClient | None = None,
        cadastral: CadastralLocalStore | None = None,
        local_sources: LocalSourcesBundle | None = None,
        building_register: BuildingRegisterClient | None = None,
        noise_emission: NoiseEmissionClient | None = None,
        pnu_resolver: "PnuResolver | None" = None,
        cng: CngStationClient | None = None,
        factory_registry: FactoryRegistryClient | None = None,
    ) -> None:
        # kakao 는 필지 확보 흐름에서만 쓰고, 유해요소 판정 후보는 공개원천만 쓴다.
        self.kakao = kakao
        self.demo_mode = demo_mode
        self.opinet = opinet
        self.kgs_lpg = kgs_lpg
        # 가스안전공사 CNG 충전소(ODcloud 15001508). 활용신청 전에는 401 이라
        # 조회 실패로 기록되고, 로컬 CSV 가 있으면 그것으로 판정한다.
        self.cng = cng
        self.facility_store = facility_store
        self.vworld = vworld
        self.safemap = safemap
        self.crematorium = crematorium
        # 전북 연속지적도 로컬 인덱스. 시설 쪽 필지 경계를 오프라인으로 붙일 때
        # VWorld 폴백으로 쓴다. 인덱스가 없으면 조용히 넘어간다.
        self.cadastral = cadastral
        # factoryON PNU 집합·표준본 공장·CNG 등 로컬 원천 묶음. 파일이 없으면
        # 빈 묶음이라 배선 전과 동일하게 동작한다. 실제 저장은 _local_sources_bundle
        # 이고, local_sources 프로퍼티가 판정 중에는 핀(_pinned_local_sources)을
        # 우선 반환한다(워밍업 교체와 진행 중 판정의 스냅샷 분리).
        self._local_sources_bundle = local_sources or LocalSourcesBundle()
        # 건축물대장 표제부 교차확인(§6.4 단란주점·테마파크 AND). None 이면 배선
        # 전과 동일하게 review_required 로 남는다.
        self.building_register = building_register
        # 소음진동배출시설 원천. LH 확정 2026-09-11 이후 판정 근거가 아니라 등록공장
        # 후보에 「소음배출 신고 있음」을 덧붙이는 부가 정보(주석) 전용이다. None·
        # 미승인(403)·CSV 미주입이면 enabled=False 라 주석을 달지 않을 뿐, 공장 판정
        # (등록공장 소재 여부)에는 영향이 없다.
        self.noise_emission = noise_emission
        # PNU 확정 파이프라인(설계서 §7.2). 시설 쪽 필지 경계·PNU 확정에 쓴다. 공장
        # 판정은 더 이상 소음-공장 PNU 매칭을 하지 않는다(LH 확정 2026-09-11).
        self.pnu_resolver = pnu_resolver
        # 산단공 공장등록 필지정보 API(15087615). 로컬 factoryON 표준본이 없을 때
        # 사업지 시군구의 등록공장을 API 로 받아 「공장 있음」 검토 표시를 낸다.
        # 좌표가 없어 카카오 지오코딩을 거치며, 시설 필지는 좌표로 다시 붙인다.
        self.factory_registry = factory_registry

    @property
    def local_sources(self) -> LocalSourcesBundle:
        """판정 중에는 시작 시점에 고정한 번들을, 그 밖에는 최신 번들을 반환한다.

        review() 가 시작하면서 현재 번들을 _pinned_local_sources 에 고정한다. 판정
        내내 후보 수집·PNU 대조·연결성 검사·원천 상태가 전부 같은 번들을 본다.
        워밍업이 끝나 self._local_sources_bundle 이 교체돼도 진행 중 판정은 영향을
        받지 않는다. 핀이 없을 때(워밍업·비판정 경로)는 최신 번들을 그대로 준다.
        """

        pinned = _pinned_local_sources.get()
        if pinned is not None:
            return pinned
        return self._local_sources_bundle

    @local_sources.setter
    def local_sources(self, value: LocalSourcesBundle) -> None:
        # 워밍업이 완성 번들로 교체할 때만 쓴다. 핀은 건드리지 않으므로 진행 중
        # 판정은 여전히 고정 번들을 본다(단조적 교체를 판정 경계와 분리한다).
        self._local_sources_bundle = value

    async def review(
        self,
        request: HazardReviewRequest,
        progress: HazardProgressCallback,
        cancel_event: asyncio.Event,
    ) -> HazardReviewResult:
        # 판정 시작 시점의 로컬 원천 번들을 이 판정 내내 고정한다. 후보 수집·PNU
        # 대조·연결성 검사·note·원천 상태가 전부 같은 스냅샷을 본다. 워밍업이
        # 중간에 self._local_sources_bundle 을 교체해도 이 판정은 처음 번들만 본다.
        token = _pinned_local_sources.set(self._local_sources_bundle)
        try:
            return await self._review_impl(request, progress, cancel_event)
        finally:
            _pinned_local_sources.reset(token)

    async def _review_impl(
        self,
        request: HazardReviewRequest,
        progress: HazardProgressCallback,
        cancel_event: asyncio.Event,
    ) -> HazardReviewResult:
        pack = get_rule_pack(request.rule_pack_id)
        if not pack:
            raise ValueError("선택한 유해시설 규칙팩을 찾을 수 없습니다.")

        housing: HousingType = request.site.housing_type
        application: ApplicationType = request.site.application_type

        await self._checkpoint(
            cancel_event, progress, "PARCEL", "신청필지 확보", 100,
            len(request.site.parcels),
            "신청 필지 형상과 신청유형을 확인했습니다.",
        )
        await self._checkpoint(
            cancel_event, progress, "RULES", "규칙팩 검증", 100, len(RULES),
            f"{HOUSING_TYPE_LABELS[housing]}·{APPLICATION_TYPE_LABELS[application]} "
            "매트릭스를 적용합니다.",
        )

        boundary_rings = self._site_boundaries(request)
        site_boundary_resolved = bool(boundary_rings)
        site_pnu = self._site_pnu(request)

        # 생활안전지도 주유시설은 HAZMAT(LPG)·FUEL25(주유소) 두 rule 이 같은
        # 전국 원천을 나눠 쓴다. rule 마다 stations_around_cached 를 따로 부르면
        # 캐시가 데워진 뒤에도 14,417건 반경필터를 두 번 반복하고, 콜드스타트
        # 구간에는 두 rule 이 각각 콜드 신호를 관찰한다. 판정 1회당 한 번만 받아
        # 두 rule 에 그대로 넘긴다(초과 반경으로 받아 각 rule 은 여전히 자기
        # search_limit_m 으로 정확히 거르므로 판정 결과는 바뀌지 않는다).
        safemap_snapshot, safemap_failed = await self._prefetch_safemap(
            request, housing, application
        )

        findings: list[HazardFinding] = []
        categories: list[HazardCategorySummary] = []
        # 원천 조회 실패는 스냅샷 자체가 없다는 뜻이라 no_conflict 로 둔갑하면 안 된다.
        failed_sources: set[str] = set()

        for rule in RULES:
            if cancel_event.is_set():
                raise asyncio.CancelledError
            item_id, item_label = self._progress_item(rule.rule_id)
            await progress(item_id, item_label, "running", 25, None, "후보를 조회합니다.")

            threshold = threshold_for(rule.rule_id, housing, application)
            rule_categories = [c for c in CATEGORIES if c.rule_id == rule.rule_id]
            # 판정 제외 종류(군부대·사격장)는 판정 경로에서 완전히 뺀다.
            # 후보 조회·거리 계산·종합상태 승격·status_counts 에 참여하지 않는다.
            judged_categories = [c for c in rule_categories if not c.judgment_excluded]
            excluded_categories = [c for c in rule_categories if c.judgment_excluded]

            # 판정창 밖·참고 반경 이내 시설. 판정·집계에는 불참하고 지도에만 얹는다.
            nearby: list[HazardFacility] = []
            if threshold is None:
                reason = (
                    f"{HOUSING_TYPE_LABELS[housing]}·"
                    f"{APPLICATION_TYPE_LABELS[application]} 신청유형에는 "
                    f"{rule.label} Rule 이 적용되지 않습니다."
                )
                summaries = [
                    self._not_applicable_category(cat, reason)
                    for cat in judged_categories
                ]
            else:
                facilities, nearby, rule_failed = await self._find_rule_facilities(
                    request, rule, judged_categories, threshold,
                    safemap_snapshot, safemap_failed,
                )
                failed_sources |= rule_failed
                summaries = [
                    self._evaluate_category(
                        cat, threshold, facilities, site_boundary_resolved,
                        site_pnu, rule_failed,
                    )
                    for cat in judged_categories
                ]

            # 판정 제외 종류는 리스트에는 보이되(판정 제외 배지) 판정에는 불참한다.
            excluded_summaries = [
                self._judgment_excluded_category(cat) for cat in excluded_categories
            ]
            categories.extend(summaries)
            categories.extend(excluded_summaries)
            # finding(rule 종합상태)에는 판정된 종류만 넘기고, 참고 시설은 지도
            # 표시용으로 따로 싣는다(판정·집계에는 불참).
            finding = self._finding_from_categories(
                rule, threshold, summaries, nearby
            )
            findings.append(finding)
            await self._checkpoint(
                cancel_event, progress, item_id, item_label, 100,
                sum(s.candidate_count for s in summaries), finding.result_reason,
            )

        await self._checkpoint(
            cancel_event, progress, "GEOMETRY", "시설 필지·경계 확인", 100, 0,
            "경계 미확보 후보는 확정판정하지 않고 경계 미확보로 남겼습니다.",
        )
        await self._checkpoint(
            cancel_event, progress, "DECISION", "규칙 판정", 100, len(findings),
            "신청유형별 임계거리와 데이터 상태를 반영해 결과를 생성했습니다.",
        )

        # 판정 제외 종류는 종합상태·status_counts·후보 집계에서 모두 뺀다.
        judged_categories_all = [c for c in categories if not c.judgment_excluded]
        overall_status = self._overall_status(judged_categories_all)
        now = datetime.now(UTC)
        sources = self._build_sources(now, boundary_rings, failed_sources)
        source_connection = self._source_connection_score(sources)
        source_freshness = self._source_freshness_score(sources)
        boundary_coverage = self._boundary_coverage_score(
            judged_categories_all, site_boundary_resolved
        )
        status_counts = {
            status: sum(1 for c in judged_categories_all if c.status == status)
            for status in STATUS_LABELS
        }
        candidate_count = sum(c.candidate_count for c in judged_categories_all)
        review_id = str(uuid4())
        result = HazardReviewResult(
            review_id=review_id,
            created_at=now,
            demo=self.demo_mode,
            site=request.site,
            housing_type=housing,
            application_type=application,
            rule_pack=pack,
            overall_status=overall_status,
            overall_label=STATUS_LABELS[overall_status],
            overall_summary=self._overall_summary(overall_status, candidate_count),
            status_counts=status_counts,
            data_completeness=self._data_completeness(
                source_connection, boundary_coverage, source_freshness
            ),
            boundary_coverage=boundary_coverage,
            source_freshness=source_freshness,
            findings=findings,
            categories=categories,
            rule_bands=self._rule_bands(boundary_rings, housing, application),
            sources=sources,
            pending_items=[
                HazardPendingItem(item=item.item, status=item.status)
                for item in PENDING_ITEMS
            ],
            source_snapshot_id=f"prototype:{review_id}",
            calculation_note=self._calculation_note(
                site_boundary_resolved,
                any(
                    facility.geometry_type == "polygon"
                    for c in categories
                    for facility in c.facilities
                ),
            ),
            disclaimer=(
                "이 결과는 LH 승인 매입제외 기준(2026-08-24)에 따른 1차 매입제외 "
                "사전 스크리닝이며 최종 적격·부적격 판정이 아닙니다. 「판정 미적용」 "
                "항목은 시설이 없다는 뜻이 아니라 자료가 없다는 뜻이므로 별도 수기 "
                "확인이 필요합니다."
            ),
        )
        await self._checkpoint(
            cancel_event, progress, "REPORT", "결과·증빙 생성", 100, candidate_count,
            "검토 결과와 JSON 증빙을 생성했습니다.",
        )
        return result

    # ------------------------------------------------------------------
    # 필지·거리 유틸
    # ------------------------------------------------------------------
    @staticmethod
    def _site_boundaries(request: HazardReviewRequest) -> list[list[Coordinates]]:
        """지적도에서 받은 신청 필지 경계 전체. 사업지는 여러 필지일 수 있다."""

        return [
            parcel.geometry
            for parcel in request.site.parcels
            if parcel.geometry_source in CADASTRAL_SOURCES
            and len(parcel.geometry) >= 4
        ]

    @staticmethod
    def _site_pnu(request: HazardReviewRequest) -> str:
        for parcel in request.site.parcels:
            if parcel.pnu and parcel.pnu != "확인 필요":
                return parcel.pnu
        return ""

    def _measure_distance(
        self,
        request: HazardReviewRequest,
        target: Coordinates,
        fallback_distance: float | None,
    ) -> float:
        """사업지에서 시설 후보까지의 거리.

        신청 필지가 실제 지적도 경계면 대지경계에서 재고, 임시 필지면 주소점
        기준으로 잰다.
        """

        boundaries = self._site_boundaries(request)
        if boundaries:
            return distance_point_to_polygons_m(target, boundaries)
        if fallback_distance is not None:
            return fallback_distance
        return haversine_meters(request.site.coordinates, target)

    def _boundary_anchor(
        self,
        request: HazardReviewRequest,
        target: Coordinates,
    ) -> Coordinates | None:
        """distance_m 을 만든 대지경계 위의 지점."""

        return nearest_boundary_point_multi(self._site_boundaries(request), target)

    async def _prefetch_safemap(
        self,
        request: HazardReviewRequest,
        housing: HousingType,
        application: ApplicationType,
    ) -> tuple[list[SafemapStation] | None, bool]:
        """생활안전지도 주유시설 스냅샷을 판정 1회당 한 번만 받는다.

        HAZMAT(LPG충전소)·FUEL25(주유소) 두 rule 이 같은 전국 원천을 나눠 쓴다.
        두 rule 의 반경 중 더 넓은 쪽으로 받아 두면(초과집합) 각 rule 은 여전히
        자신의 search_limit_m 으로 정확히 걸러 최종 판정은 이전과 동일하다.
        반환: (스냅샷 목록 또는 None, 콜드 신호 여부). 두 rule 이 이 결과를
        그대로 나눠 쓰고 콜드 신호(safemap_failed)도 함께 받아 각자
        failed_sources 에 반영한다(원천 조회 실패를 '충돌 없음'으로 삼키지
        않는다는 원칙은 그대로다).
        """

        if not self._safemap_ready():
            return None, False
        thresholds = [
            t
            for t in (
                threshold_for("RB14-HAZMAT", housing, application),
                threshold_for("RB14-FUEL25", housing, application),
            )
            if t is not None
        ]
        if not thresholds:
            return None, False
        boundaries = self._site_boundaries(request)
        search_limit_m = max(thresholds) + BOUNDARY_BUFFER_M
        # 참고 반경까지 함께 받아 둔다. 그러지 않으면 주유소·LPG 는 참고 시설이
        # 비게 된다. 각 rule 은 여전히 자기 search_limit_m 으로 판정 후보를 거른다.
        # 컨텍스트 반경이 작아도(0 포함) 경계 부착용 예비검색 슬랙은 확보해야, 점
        # 거리가 판정창 밖이어도 경계 거리는 안인 시설을 놓치지 않는다.
        fetch_limit_m = max(search_limit_m + SEARCH_SLACK_M, HAZARD_CONTEXT_RADIUS_M)
        search_radius_m = fetch_limit_m + round(
            max_extent_multi(request.site.coordinates, boundaries)
        )
        try:
            stations = await self.safemap.stations_around_cached(
                request.site.coordinates, search_radius_m
            )
        except SafemapAPIError:
            return None, True
        return stations, False

    # ------------------------------------------------------------------
    # 후보 조회 (공개원천만)
    # ------------------------------------------------------------------
    async def _find_rule_facilities(
        self,
        request: HazardReviewRequest,
        rule: Rule,
        rule_categories: list[Category],
        threshold: int,
        safemap_snapshot: list[SafemapStation] | None = None,
        safemap_failed: bool = False,
    ) -> tuple[list[HazardFacility], list[HazardFacility], set[str]]:
        now = datetime.now(UTC)
        search_limit_m = threshold + BOUNDARY_BUFFER_M
        # 조회는 참고 반경과 예비검색 슬랙 중 넓은 쪽까지 받는다. 컨텍스트 반경이
        # 작아도(0 포함) search_limit_m + SEARCH_SLACK_M 까지는 받아야, 점 거리가
        # 판정창 밖이어도 경계 거리는 안인 후보(경계 부착 전)를 놓치지 않는다.
        # 판정 후보는 아래에서 경계 거리로 search_limit_m 만큼 정확히 거른다.
        fetch_limit_m = max(search_limit_m + SEARCH_SLACK_M, HAZARD_CONTEXT_RADIUS_M)
        boundaries = self._site_boundaries(request)
        search_radius_m = fetch_limit_m + round(
            max_extent_multi(request.site.coordinates, boundaries)
        )

        facilities: list[HazardFacility] = []
        # 조회에 실패한 원천 식별자. 실패한 원천에 기댄 카테고리는
        # no_conflict 가 아니라 dataset_missing 으로 내려간다.
        failed_sources: set[str] = set()
        if self.demo_mode:
            facilities.extend(
                self._demo_rule_facilities(
                    request, rule_categories, fetch_limit_m, now
                )
            )
        else:
            facilities.extend(
                self._licence_rule_facilities(
                    request, rule_categories, fetch_limit_m, now, failed_sources
                )
            )
            facilities.extend(
                await self._provider_rule_facilities(
                    request, rule, fetch_limit_m, search_radius_m, now,
                    failed_sources, safemap_snapshot, safemap_failed,
                )
            )
            facilities.extend(
                self._local_source_facilities(
                    request, rule, fetch_limit_m, now, existing=facilities
                )
            )
            facilities.extend(
                await self._factory_api_facilities(
                    request, rule, fetch_limit_m, now, failed_sources
                )
            )

        # 판정용 후처리(경계 부착·건축물대장·무도장 병합)는 경계 부착 전 예비검색
        # 범위(판정창 + 예비검색 슬랙) 이내 후보에만 적용한다. 점 거리로 판정창을
        # 딱 맞춰 자르면, 폴리곤이 큰 시설은 점 거리가 판정창 밖이어도 경계 거리는
        # 안인 경우를 놓친다(조준환 엔진 회귀에서 실측 5건 누락). 슬랙까지 넓혀
        # 경계를 붙인 뒤, 최종 판정 후보는 경계 거리로 <= search_limit_m 만 남기고
        # 나머지는 참고 시설(nearby)로 보낸다. 참고 시설은 VWorld 호출량을 늘리지
        # 않도록 점 좌표 그대로 둔다(경계 미조회).
        prelim_limit_m = search_limit_m + SEARCH_SLACK_M
        candidates = [f for f in facilities if f.distance_m <= prelim_limit_m]
        nearby = [f for f in facilities if f.distance_m > prelim_limit_m]

        # 무도장·무도학원 이중 등록은 경계 조회 전에 한 줄로 묶는다(조회 낭비 방지).
        if rule.rule_id == "RB14-AMUSEMENT":
            candidates = self._merge_duplicate_dance_registrations(candidates)
        await self._attach_facility_boundaries(request, candidates)
        # 공장(LH 확정 2026-09-11): 등록공장 후보에 대기·소음 배출 신고를 부가 정보로만
        # 덧붙인다. 판정 근거로 쓰지 않는다(좌표 근접분만 주석).
        if rule.rule_id == "RB14-FACTORY":
            await self._annotate_registered_factories(
                request, candidates, search_radius_m, now
            )
        # §6.4 단란주점·테마파크 AND: 임계거리 이내 후보의 필지 표제부 용도를 조회해
        # 제2종 근린생활시설·운동시설 비해당 여부를 붙인다(버퍼 구간은 조회 낭비라 제외).
        if rule.rule_id == "RB14-AMUSEMENT" and self._building_register_ready():
            await self._attach_building_register(
                candidates, rule_categories, threshold
            )
        # 석유대체연료 판매업(LH 확정 2026-09-11 #5): 임계거리 이내 석유대체연료 후보에만
        # 용도지역(지적편집도)을 붙인다. 주유소·CNG·LPG 충전소는 조회하지 않는다
        # (결정 범위 밖). VWorld 호출을 아끼려 판정창 이내 후보에만 조회한다.
        if rule.rule_id == "RB14-FUEL25":
            await self._attach_zoning(candidates, threshold)
        # 경계 부착으로 거리가 재측정되면 판정창 밖으로 밀려날 수 있다. 기존 동작대로
        # 판정 후보는 <= search_limit_m 로 최종 거르고, 밀려난 후보는 참고 시설로 합류.
        facilities = []
        for facility in candidates:
            if facility.distance_m <= search_limit_m:
                facilities.append(facility)
            else:
                nearby.append(facility)
        facilities.sort(key=lambda item: item.distance_m)
        # 참고 시설: 판정 후보와 facility_id 중복 없이 거리순으로 둔다.
        candidate_ids = {f.facility_id for f in facilities}
        nearby = [f for f in nearby if f.facility_id not in candidate_ids]
        # 참고 핀은 컨텍스트 반경 이내만 남긴다. 조회는 예비검색 슬랙까지 넓혔으므로
        # 그 슬랙 구간(판정 후보로 승격되지 못한 것)이 참고 핀으로 새어 나오지 않게
        # 명시적으로 거른다. 컨텍스트 반경이 0 이면 참고 핀은 없다.
        nearby = [f for f in nearby if f.distance_m <= HAZARD_CONTEXT_RADIUS_M]
        # 참고 시설도 경계를 붙여 경계↔경계 거리로 보여 준다(2026-09-14 사용자
        # 결정). 종전엔 VWorld 호출을 아끼려 점 좌표로 뒀는데, 화면에서 선이 시설
        # 영역 안 점까지 들어가 "중심까지 재는 것 아니냐"는 오해를 낳았다. 경계
        # 재측정으로 거리가 늘어난 시설은 그대로 참고 시설로 남는다(판정 불변).
        await self._attach_facility_boundaries(request, nearby)
        nearby.sort(key=lambda item: item.distance_m)
        return facilities, nearby, failed_sources

    async def _factory_api_facilities(
        self,
        request: HazardReviewRequest,
        rule: Rule,
        search_limit_m: float,
        now: datetime,
        failed_sources: set[str],
    ) -> list[HazardFacility]:
        """산단공 공장등록 API 로 사업지 시군구의 등록공장을 「공장 있음」 후보로 만든다.

        로컬 표준본(factory_facilities)이 적재돼 있으면 그쪽이 정본이라 API 는 쓰지
        않는다. 시군구는 사업지 필지 PNU 앞 5자리에서 얻는다(PNU 가 없으면 조회 불가).
        매입제외 확정 근거가 아니라 검토 표시이므로(LH 확정 2026-09-11 #1), 조회
        실패는 failed_sources 로만 남긴다.
        """

        if rule.rule_id != "RB14-FACTORY" or not self._factory_api_ready():
            return []
        if self.local_sources.factory_registry_loaded:
            return []
        site_pnu = self._site_pnu(request)
        if len(site_pnu) < 5:
            return []
        radius = search_limit_m + max_extent_multi(
            request.site.coordinates, self._site_boundaries(request)
        )
        try:
            records = await self.factory_registry.factories_near(
                request.site.coordinates, radius, site_pnu[:5]
            )
        except Exception:  # noqa: BLE001 — 원천 장애는 failed_sources 로 드러낸다
            failed_sources.add("factory_registry")
            return []
        return self._records_to_facilities(
            request,
            tuple(records),
            search_limit_m,
            now,
            facility_type="factory",
            facility_type_label="등록공장",
            id_prefix="factory-api",
            source_label="한국산업단지공단 공장등록 필지정보(API)",
            classification_note=(
                "산단공 공장등록 원장의 등록공장입니다. 등록 사실만 확인되며 "
                "대기·소음 배출은 별도 확인이 필요합니다. 좌표는 도로명주소를 "
                "지오코딩한 값이라 시설 필지로 다시 확인합니다."
            ),
            metadata={"factory_registered": True, "factory_and": ""},
        )

    def _factory_api_ready(self) -> bool:
        return bool(
            self.factory_registry
            and self.factory_registry.enabled
            and not self.demo_mode
        )

    def _local_source_facilities(
        self,
        request: HazardReviewRequest,
        rule: Rule,
        search_limit_m: float,
        now: datetime,
        existing: list[HazardFacility] | None = None,
    ) -> list[HazardFacility]:
        """로컬 원천 묶음(factoryON 표준 공장·CNG)에서 후보를 만든다.

        - RB14-FACTORY: 표준본 공장(좌표·PNU 보유)을 「공장 있음」 후보로 넣는다.
          factory_registered=True 로 표시해 factory_registered 종류에만 걸리게 한다.
          절대 exclusion 으로 올리지 않는다(LH 확정 2026-09-11: 검토 표시만).
        - RB14-FUEL25: CNG 충전소(좌표 보유)를 넣는다. 가스안전공사 API(existing 에
          먼저 담김)와 같은 자리(OFFICIAL_DEDUPE_M)의 행은 중복으로 버린다(API 먼저).
        파일이 없으면 묶음이 비어 있어 아무 후보도 나오지 않는다(배선 전과 동일).
        """

        if rule.rule_id == "RB14-FACTORY":
            return self._records_to_facilities(
                request,
                self.local_sources.factory_facilities,
                search_limit_m,
                now,
                facility_type="factory",
                facility_type_label="factoryON 등록공장",
                id_prefix="factory-registry",
                source_label="factoryON 등록공장(표준본)",
                classification_note=(
                    "factoryON 등록공장입니다. 등록 사실만 확인되며 대기·소음 배출은 "
                    "별도 확인이 필요합니다."
                ),
                metadata={"factory_registered": True, "factory_and": ""},
            )
        if rule.rule_id == "RB14-FUEL25":
            local_cng = self._records_to_facilities(
                request,
                self.local_sources.cng_facilities,
                search_limit_m,
                now,
                facility_type="cng_station",
                facility_type_label="CNG 충전소",
                id_prefix="cng-local",
                source_label="자동차용 천연가스충전소 현황(CSV)",
                classification_note="등록된 CNG(천연가스) 충전소입니다.",
                metadata={},
            )
            api_cng = [
                f for f in (existing or []) if f.facility_type == "cng_station"
            ]
            return [
                facility
                for facility in local_cng
                if not any(
                    haversine_meters(facility.coordinates, api.coordinates)
                    <= OFFICIAL_DEDUPE_M
                    for api in api_cng
                )
            ]
        return []

    def _records_to_facilities(
        self,
        request: HazardReviewRequest,
        records: tuple,
        search_limit_m: float,
        now: datetime,
        *,
        facility_type: str,
        facility_type_label: str,
        id_prefix: str,
        source_label: str,
        classification_note: str,
        metadata: dict[str, object],
    ) -> list[HazardFacility]:
        facilities: list[HazardFacility] = []
        for record in records:
            coordinates = record.coordinates
            if coordinates is None:
                continue
            distance = self._measure_distance(request, coordinates, None)
            if distance > search_limit_m:
                continue
            facilities.append(
                HazardFacility(
                    facility_id=f"{id_prefix}:{record.record_id}",
                    facility_type=facility_type,
                    facility_type_label=facility_type_label,
                    name=record.name,
                    coordinates=coordinates,
                    distance_m=distance,
                    nearest_boundary_point=self._boundary_anchor(
                        request, coordinates
                    ),
                    address=record.address,
                    road_address=record.road_address,
                    business_status=record.status_text or "정상영업",
                    provider="local_sources",
                    source_label=source_label,
                    source_record_id=record.record_id,
                    source_as_of=now,
                    geometry_quality="C",
                    geometry_note="원천 등록 점 좌표 · 시설경계 미확인",
                    classification_note=classification_note,
                    # 표준본 공장은 PNU 를 갖고 있으므로 AND 매칭에 바로 쓸 수 있게
                    # 미리 실어 둔다. 시설 필지 조회(_attach_facility_boundaries)가
                    # 이 값을 덮어쓸 수 있으나, 표준본 PNU 가 이미 정확하다.
                    parcel_pnu=record.pnu or "",
                    metadata=dict(metadata),
                )
            )
        return facilities

    def _demo_rule_facilities(
        self,
        request: HazardReviewRequest,
        rule_categories: list[Category],
        search_limit_m: float,
        now: datetime,
    ) -> list[HazardFacility]:
        facilities: list[HazardFacility] = []
        for demo_row in demo_facilities(request.site.coordinates):
            coordinates = demo_row["coordinates"]
            if not isinstance(coordinates, Coordinates):
                continue
            metadata = dict(demo_row.get("metadata") or {})  # type: ignore[arg-type]
            facility = HazardFacility(
                facility_id=str(demo_row["id"]),
                facility_type=str(demo_row["facility_type"]),
                facility_type_label=str(demo_row["label"]),
                name=str(demo_row["name"]),
                coordinates=coordinates,
                distance_m=self._measure_distance(request, coordinates, None),
                nearest_boundary_point=self._boundary_anchor(request, coordinates),
                address="검증용 샘플 주소",
                business_status=str(demo_row.get("status") or "정상영업"),
                provider="demo",
                source_label="검증용 샘플",
                source_record_id=str(demo_row["id"]),
                source_as_of=now,
                geometry_quality="D",
                geometry_note="주소 좌표 기반 검증용 점",
                classification_note="UI·규칙 흐름 확인용이며 실제 시설이 아닙니다.",
                metadata=metadata,
            )
            if not self._facility_matches_rule(facility, rule_categories):
                continue
            if facility.distance_m > search_limit_m:
                continue
            facilities.append(facility)
        return facilities

    def _licence_rule_facilities(
        self,
        request: HazardReviewRequest,
        rule_categories: list[Category],
        search_limit_m: float,
        now: datetime,
        failed_sources: set[str],
    ) -> list[HazardFacility]:
        """로컬에 적재된 인허가 대장에서 찾은 시설."""

        dataset_keys = tuple(
            sorted(
                {
                    key
                    for cat in rule_categories
                    if cat.data_state in ("applied", "partial", "approximate")
                    for key in cat.datasets
                }
            )
        )
        if not dataset_keys or not self._store_ready() or not self.facility_store:
            return []
        radius = search_limit_m + max_extent_multi(
            request.site.coordinates, self._site_boundaries(request)
        )
        try:
            rows = self.facility_store.facilities_around(
                request.site.coordinates, radius, dataset_keys
            )
        except Exception:
            # 조회 실패는 "충돌 없음"이 아니다. 스냅샷 자체가 없다.
            failed_sources.add("localdata")
            return []
        facilities: list[HazardFacility] = []
        for row in rows:
            # 고압가스 자가설비(기관 자체 사용)는 판정·검토·참고 핀에서 완전히 뺀다
            # (LH 확정 2026-09-11 안건 ③). 후보 목록에 애초에 올리지 않는다.
            if row.dataset_key in (
                "high_pressure_gas",
                "specific_high_pressure_gas",
            ) and is_self_use_gas(
                row.name,
                row.category,
                str((getattr(row, "extra", None) or {}).get("MNFTR_SE_NM") or ""),
                str((getattr(row, "extra", None) or {}).get("USE_PRPS") or ""),
            ):
                continue
            distance = self._measure_distance(request, row.coordinates, None)
            if distance > search_limit_m:
                continue
            dataset = DATASET_BY_KEY.get(row.dataset_key)
            facilities.append(
                HazardFacility(
                    facility_id=f"licence:{row.dataset_key}:{row.record_id}",
                    facility_type=dataset.facility_type if dataset else "unknown",
                    facility_type_label=(
                        dataset.facility_type_label if dataset else row.dataset_key
                    ),
                    name=row.name,
                    coordinates=row.coordinates,
                    distance_m=distance,
                    nearest_boundary_point=self._boundary_anchor(
                        request, row.coordinates
                    ),
                    address=row.address,
                    road_address=row.road_address,
                    business_status=row.status or "",
                    provider="localdata",
                    source_label=(
                        f"행정안전부 지방행정인허가 {dataset.label if dataset else ''}"
                    ),
                    source_record_id=row.record_id,
                    source_as_of=now,
                    geometry_quality="C",
                    geometry_note="인허가 등록 점 좌표 · 시설경계 미확인",
                    classification_note=(
                        f"{dataset.label if dataset else row.dataset_key} 인허가 "
                        f"대장 등록 업소입니다. 업태 {row.category or '미상'}, "
                        f"영업상태 {row.status or '미상'}."
                    ),
                    metadata={
                        "dataset": row.dataset_key,
                        "business_category": row.category,
                        "business_status": row.status,
                        "air_grade": self._air_grade(row.category),
                        "extra": dict(getattr(row, "extra", None) or {}),
                    },
                )
            )
        return facilities

    async def _provider_rule_facilities(
        self,
        request: HazardReviewRequest,
        rule: Rule,
        search_limit_m: float,
        search_radius_m: float,
        now: datetime,
        failed_sources: set[str],
        safemap_snapshot: list[SafemapStation] | None = None,
        safemap_failed: bool = False,
    ) -> list[HazardFacility]:
        """주유·충전 공개 API(오피넷·가스안전공사) 원천."""

        facilities: list[HazardFacility] = []
        # 주유소·자동차용 LPG 충전소 모두 25m(RB14-FUEL25)로 본다
        # (LH 확정 2026-09-11 안건 ④). 두 종류 다 오피넷·가스안전공사·생활안전지도를
        # 공유하므로 FUEL25 rule 에서 함께 조회한다. HAZMAT(50m)에는 LPG 판매소·
        # 저장소·고압가스 등 인허가/로컬 원장 종류만 남는다.
        want_gas = rule.rule_id == "RB14-FUEL25"
        want_lpg = rule.rule_id == "RB14-FUEL25"
        want_crematorium = rule.rule_id == "RB14-CREMATION-MILITARY"
        if not (want_gas or want_lpg or want_crematorium):
            return facilities

        if (want_gas or want_lpg) and self._opinet_ready():
            try:
                stations = await self.opinet.stations_around(
                    request.site.coordinates, search_radius_m
                )
            except OpinetAPIError:
                # 오피넷 조회 실패. 이 원천에 기댄 카테고리는 스냅샷이 없다.
                failed_sources.add("opinet")
                stations = []
            for station in stations:
                distance = self._measure_distance(request, station.coordinates, None)
                if distance > search_limit_m:
                    continue
                try:
                    detail = await self.opinet.station_detail(station.station_id)
                except OpinetAPIError:
                    detail = None
                resolved = detail or station
                is_lpg = resolved.lpg
                if is_lpg and not want_lpg:
                    continue
                if not is_lpg and not want_gas:
                    continue
                facilities.append(
                    HazardFacility(
                        facility_id=f"opinet:{station.station_id}",
                        facility_type="lpg_station" if is_lpg else "gas_station",
                        facility_type_label="LPG 충전소" if is_lpg else "주유소",
                        name=resolved.name or station.name,
                        coordinates=station.coordinates,
                        distance_m=distance,
                        nearest_boundary_point=self._boundary_anchor(
                            request, station.coordinates
                        ),
                        address=resolved.address,
                        road_address=resolved.road_address,
                        business_status="정상영업",
                        provider="opinet",
                        source_label="한국석유공사 오피넷 주유소·충전소",
                        source_record_id=station.station_id,
                        source_as_of=now,
                        geometry_quality="C",
                        geometry_note="사업자 등록 점 좌표 · 시설경계 미확인",
                        classification_note=(
                            f"오피넷 등록 {'LPG 충전소' if is_lpg else '주유소'}입니다."
                        ),
                        metadata={"brand": station.brand},
                    )
                )

        if want_lpg and self._lpg_ready():
            try:
                lpg_stations = await self.kgs_lpg.stations_around(
                    request.site.coordinates, search_radius_m
                )
            except PublicDataAPIError:
                # 가스안전공사 조회 실패. LPG 충전소 스냅샷이 없다.
                failed_sources.add("kgs")
                lpg_stations = []
            for station in lpg_stations:
                distance = self._measure_distance(request, station.coordinates, None)
                if distance > search_limit_m:
                    continue
                # 같은 타입(LPG 충전소)끼리만 병합한다. 주유소와 LPG 충전소는
                # facility_type 이 달라 40m 안에 있어도 한 건으로 합치지 않는다
                # (LH 확정 2026-09-11: 둘 다 FUEL25 대상이라 각각 남겨야 한다).
                if any(
                    existing.facility_type == "lpg_station"
                    and haversine_meters(station.coordinates, existing.coordinates)
                    <= OFFICIAL_DEDUPE_M
                    for existing in facilities
                ):
                    continue
                facilities.append(
                    HazardFacility(
                        facility_id=f"kgs-lpg:{station.station_id}",
                        facility_type="lpg_station",
                        facility_type_label="LPG 충전소",
                        name=station.name,
                        coordinates=station.coordinates,
                        distance_m=distance,
                        nearest_boundary_point=self._boundary_anchor(
                            request, station.coordinates
                        ),
                        address=station.address,
                        business_status="정상영업",
                        provider="kgs",
                        source_label="한국가스안전공사 전국 LPG 충전소 현황",
                        source_record_id=station.station_id,
                        source_as_of=now,
                        geometry_quality="C",
                        geometry_note="허가 등록 점 좌표 · 시설경계 미확인",
                        classification_note=(
                            f"가스안전공사 등록 충전소입니다. 취급구분 "
                            f"{station.usage or '미상'}."
                        ),
                        metadata={"usage": station.usage, "region": station.region},
                    )
                )

        # CNG 충전소(가스안전공사 ODcloud 15001508). 2026-09-14 조사로 전국 자료에
        # 위경도가 있음이 확인돼 로컬 CSV 보조에서 API 판정으로 올린다. 로컬 CSV 는
        # _local_source_facilities 가 뒤에 붙이되 같은 자리 행은 중복으로 버린다.
        if want_gas and self._cng_ready():
            try:
                cng_stations = await self.cng.stations_around(
                    request.site.coordinates, search_radius_m
                )
            except PublicDataAPIError:
                # 활용신청 전 401 등. 스냅샷이 없으므로 실패로 기록한다.
                failed_sources.add("cng")
                cng_stations = []
            for station in cng_stations:
                distance = self._measure_distance(request, station.coordinates, None)
                if distance > search_limit_m:
                    continue
                if any(
                    existing.facility_type == "cng_station"
                    and haversine_meters(station.coordinates, existing.coordinates)
                    <= OFFICIAL_DEDUPE_M
                    for existing in facilities
                ):
                    continue
                facilities.append(
                    HazardFacility(
                        facility_id=f"cng:{station.station_id}",
                        facility_type="cng_station",
                        facility_type_label="CNG 충전소",
                        name=station.name,
                        coordinates=station.coordinates,
                        distance_m=distance,
                        nearest_boundary_point=self._boundary_anchor(
                            request, station.coordinates
                        ),
                        address=station.address,
                        business_status="정상영업",
                        provider="cng",
                        source_label="한국가스안전공사 전국 도시가스충전소 현황",
                        source_record_id=station.station_id,
                        source_as_of=now,
                        geometry_quality="C",
                        geometry_note="현황 등록 점 좌표 · 시설경계 미확인",
                        classification_note=(
                            "가스안전공사 도시가스(CNG) 충전소 현황에 등록된 "
                            "충전소입니다."
                        ),
                        metadata={"region": station.region, "branch": station.branch},
                    )
                )

        # 생활안전지도 IF_0033 — 주유소·LPG충전소·겸업을 한 원천이 준다.
        # lpg_yn·상표 컬럼으로 갈라 FUEL25(주유소)·HAZMAT(LPG충전소)에 각각 붙인다.
        # 조회 자체는 _prefetch_safemap() 이 판정 1회당 한 번만 하고, 여기서는 그
        # 스냅샷을 재사용한다(콜드 신호도 그때 이미 확정됐다).
        if (want_gas or want_lpg) and self._safemap_ready():
            if safemap_failed:
                # 콜드스타트로 요청을 막지 않는 비차단 조회의 결과. 캐시가 아직 안
                # 데워졌으면 스냅샷이 없다는 뜻이라, '충돌 없음'이 아니라 '스냅샷
                # 미확보'로 정직하게 반영되도록 failed_sources 에 담는다.
                failed_sources.add("safemap")
                fuel_stations = []
            else:
                fuel_stations = safemap_snapshot or []
            for station in fuel_stations:
                # 미분류 시설: 주유소·LPG 어느 쪽 상표도 lpg_yn 도 없어 구분 불가.
                # 주유소로 강제 분류하지 않고, 임계거리 이내면 review_required 후보로만
                # 남긴다(exclusion_match 로 승격 금지, 룰북 §3).
                unclassified = station.is_unclassified
                emit_gas = want_gas and station.is_gas_station
                emit_lpg = want_lpg and station.is_lpg_station
                if not (emit_gas or emit_lpg or (unclassified and (want_gas or want_lpg))):
                    continue
                distance = self._measure_distance(request, station.coordinates, None)
                if distance > search_limit_m:
                    continue
                is_lpg = emit_lpg
                combined = station.is_gas_station and station.is_lpg_station
                if unclassified:
                    facility_type = "fuel_unclassified"
                    facility_type_label = "주유/충전 미분류 시설"
                    note = SAFEMAP_UNCLASSIFIED_NOTE
                else:
                    facility_type = "lpg_station" if is_lpg else "gas_station"
                    facility_type_label = "LPG 충전소" if is_lpg else "주유소"
                    note = (
                        f"생활안전지도 등록 {'LPG 충전소' if is_lpg else '주유소'}입니다."
                    )
                    if combined:
                        note += " (주유소·LPG충전소 겸업)"
                # 중복 제거: uni_cd 가 오피넷 UNI_ID 와 같은 체계로 보여 우선키로
                # 쓴다(같은 레코드는 타입과 무관하게 한 건). 거리 기준 병합은 같은
                # facility_type 끼리만 한다 — 주유소와 LPG 충전소가 40m 안에 있어도
                # 서로 다른 시설이므로 둘 다 남긴다(LH 확정 2026-09-11, 둘 다 FUEL25).
                if any(
                    (
                        station.station_id
                        and station.station_id == existing.source_record_id
                    )
                    or (
                        existing.facility_type == facility_type
                        and haversine_meters(station.coordinates, existing.coordinates)
                        <= OFFICIAL_DEDUPE_M
                    )
                    for existing in facilities
                ):
                    continue
                facilities.append(
                    HazardFacility(
                        facility_id=(
                            f"safemap:{station.station_id}"
                            if station.station_id
                            else f"safemap:{station.coordinates.lat:.6f}"
                            f",{station.coordinates.lng:.6f}"
                        ),
                        facility_type=facility_type,
                        facility_type_label=facility_type_label,
                        name=station.name,
                        coordinates=station.coordinates,
                        distance_m=distance,
                        nearest_boundary_point=self._boundary_anchor(
                            request, station.coordinates
                        ),
                        address=station.address,
                        road_address=station.road_address,
                        business_status="정상영업",
                        provider="safemap",
                        source_label="생활안전지도 전국 주유시설 현황(IF_0033)",
                        source_record_id=station.station_id,
                        source_as_of=now,
                        geometry_quality="C",
                        geometry_note="주유시설 등록 점 좌표 · 시설경계 미확인",
                        classification_note=note,
                        metadata={
                            "oil_brand": station.oil_brand,
                            "gas_brand": station.gas_brand,
                            "lpg_yn": "Y" if station.lpg_yn else "N",
                            "unclassified_source": unclassified,
                        },
                    )
                )

        # 보건복지부 전국 화장시설 — 좌표가 없어 주소를 지오코딩해 붙인다.
        # 지오코딩 실패 건은 client 가 격리하며, 여기서는 좌표가 붙은 것만 다룬다.
        if want_crematorium and self._crematorium_ready():
            try:
                crematoriums = await self.crematorium.crematoriums_around(
                    request.site.coordinates, search_radius_m
                )
            except CrematoriumAPIError:
                failed_sources.add("crematorium")
                crematoriums = []
            for crematorium in crematoriums:
                distance = self._measure_distance(
                    request, crematorium.coordinates, None
                )
                if distance > search_limit_m:
                    continue
                facilities.append(
                    HazardFacility(
                        facility_id=f"crematorium:{crematorium.facility_id}",
                        facility_type="crematorium",
                        facility_type_label="화장장",
                        name=crematorium.name,
                        coordinates=crematorium.coordinates,
                        distance_m=distance,
                        nearest_boundary_point=self._boundary_anchor(
                            request, crematorium.coordinates
                        ),
                        address=crematorium.address,
                        business_status="정상영업",
                        provider="crematorium",
                        source_label="보건복지부 전국 화장시설 현황",
                        source_record_id=crematorium.facility_id,
                        source_as_of=now,
                        # 등록 점 좌표가 아니라 주소 지오코딩 점이라 한 등급 낮춘다.
                        geometry_quality="D",
                        geometry_note="주소 지오코딩 점 좌표 · 시설경계 미확인",
                        classification_note=(
                            f"{crematorium.gubun or ''} 화장시설 "
                            f"(화장로 {crematorium.brazier_count or '미상'}기)."
                        ),
                        metadata={
                            "region": crematorium.region,
                            "gubun": crematorium.gubun,
                        },
                    )
                )

        return facilities


    @staticmethod
    def _facility_extra(facility: HazardFacility, key: str) -> str:
        extra = facility.metadata.get("extra")
        if isinstance(extra, dict):
            return str(extra.get(key) or "")
        return ""

    def _classify_gas_facilities(
        self,
        facilities: list[HazardFacility],
    ) -> tuple[list[HazardFacility], int]:
        """고압가스 원장(라·바목) 후보를 확정 가능/검토/자가설비로 가른다.

        반환은 (판정에 넣을 후보, 자가설비로 뺀 건수). 데모 픽스처처럼 이미
        gas_type_confirmed 가 박힌 후보는 그대로 둔다.
        """

        kept: list[HazardFacility] = []
        self_use = 0
        for facility in facilities:
            if "gas_type_confirmed" in facility.metadata:
                kept.append(facility)
                continue
            dataset = str(facility.metadata.get("dataset") or "")
            business = str(facility.metadata.get("business_category") or "")
            manufacture = self._facility_extra(facility, "MNFTR_SE_NM")
            if dataset == "city_gas_companies":
                facility.metadata["gas_type_confirmed"] = True
                facility.metadata["gas_basis"] = "일반도시가스업 등록시설(라목)"
            elif dataset == "specific_high_pressure_gas":
                facility.metadata["gas_type_confirmed"] = False
                facility.metadata["gas_basis"] = SPECIFIC_GAS_REVIEW_NOTE
            elif any(word in manufacture for word in GAS_SELF_USE_KEYWORDS):
                self_use += 1
                continue
            elif any(word in business for word in GAS_CONFIRMABLE_KEYWORDS):
                facility.metadata["gas_type_confirmed"] = True
                facility.metadata["gas_basis"] = f"업태 「{business}」 — 바목 문언 해당"
            else:
                facility.metadata["gas_type_confirmed"] = False
                facility.metadata["gas_basis"] = GAS_REVIEW_NOTE
            kept.append(facility)
        return kept, self_use

    def _filter_theme_park_by_building_use(
        self,
        facilities: list[HazardFacility],
    ) -> tuple[list[HazardFacility], int]:
        """테마파크 후보의 원장 건축물용도로 운동시설 해당분을 뺀다(H-04-다 §6-1)."""

        kept: list[HazardFacility] = []
        dropped = 0
        for facility in facilities:
            use = self._facility_extra(facility, "BLDG_USG_NM")
            if any(word in use for word in THEME_PARK_SPORTS_USE_KEYWORDS):
                dropped += 1
                continue
            if any(word in use for word in THEME_PARK_NEIGHBORHOOD_USE_KEYWORDS):
                facility.metadata["bldg_use_neighborhood"] = use
            kept.append(facility)
        return kept, dropped

    @staticmethod
    def _merge_duplicate_dance_registrations(
        facilities: list[HazardFacility],
    ) -> list[HazardFacility]:
        """무도장·무도학원 이중 등록을 한 줄로 묶는다(H-04-마 §6-5).

        좌표가 사실상 같은(≤1m) 무도장·무도학원 쌍은 한 사업장이 두 업종을 등록한
        것이다. 무도장 쪽을 남기고 무도학원 쪽을 빼되, 남긴 쪽에 병합 사실을 기록한다.
        """

        halls = [f for f in facilities if f.facility_type == "dance_hall"]
        academies = [f for f in facilities if f.facility_type == "dance_academy"]
        if not halls or not academies:
            return facilities
        merged_ids: set[str] = set()
        for academy in academies:
            for hall in halls:
                if haversine_meters(academy.coordinates, hall.coordinates) <= DANCE_DUPLICATE_M:
                    merged_ids.add(academy.facility_id)
                    hall.metadata["merged_registration"] = academy.name
                    hall.facility_type_label = "무도장·무도학원 (동일 사업장 이중 등록)"
                    hall.classification_note = (
                        f"{hall.classification_note} 무도학원업 「{academy.name}」과 "
                        "좌표·주소가 같아 한 시설로 묶었습니다 (H-04-마 §6-5)."
                    ).strip()
                    break
        if not merged_ids:
            return facilities
        return [f for f in facilities if f.facility_id not in merged_ids]

    async def _attach_facility_boundaries(
        self,
        request: HazardReviewRequest,
        facilities: list[HazardFacility],
    ) -> None:
        """시설 쪽 지적도 필지를 붙이고 거리를 경계 대 경계로 다시 잰다.

        VWorld 를 우선 쓰되, 실패하거나 키가 없으면 전북 연속지적도 로컬 인덱스로
        폴백한다. 둘 다 없으면 시설은 점 좌표로 남고 확정판정하지 않는다.
        """

        boundaries = self._site_boundaries(request)
        if not boundaries:
            return
        vworld_ready = bool(self.vworld and self.vworld.enabled)
        cadastral_ready = self._cadastral_ready()
        can_fetch = vworld_ready or cadastral_ready

        seen: dict[str, tuple[list[Coordinates], str]] = {}
        for facility in facilities:
            # (B) 시설이 여러 필지를 점유(occupied_parcels 사전 주입)하면 각 필지까지
            # 재고 최소값으로 판정한다. 대부분의 시설은 단일 필지라 이 목록이 비어
            # 있고, 그때는 좌표를 품는 필지 1개를 조회해 종전과 동일하게 처리한다.
            prefilled = [p for p in facility.occupied_parcels if len(p.geometry) >= 4]
            if prefilled:
                parcels = [
                    (p.geometry, p.pnu, p.jibun, p.address, p.area_m2)
                    for p in prefilled
                ]
                enumerated = True
            elif not can_fetch:
                # 사전 주입 필지도 없고 조회할 원천도 없으면 점 좌표로 남긴다.
                continue
            else:
                key = f"{facility.coordinates.lat:.6f}:{facility.coordinates.lng:.6f}"
                cached = seen.get(key)
                if cached is None:
                    cached = await self._facility_parcel(
                        facility.coordinates, vworld_ready, cadastral_ready
                    )
                    seen[key] = cached
                ring, pnu = cached
                if pnu:
                    facility.parcel_pnu = pnu
                if len(ring) < 4:
                    continue
                parcels = [(ring, pnu, "", "", None)]
                # 점유 필지 열거 원천이 없어 좌표가 포함된 단일 필지만 잡았다.
                enumerated = False
            self._apply_parcel_distances(
                facility, boundaries, parcels, enumerated=enumerated
            )

    def _apply_parcel_distances(
        self,
        facility: HazardFacility,
        boundaries: list[list[Coordinates]],
        parcels: list[tuple[list[Coordinates], str, str, str, float | None]],
        *,
        enumerated: bool = True,
    ) -> None:
        """점유 필지들 각각까지 경계거리를 재고 최소값을 판정거리로 확정한다.

        정민재 이사 2026-08-28 확정: 시설이 여러 필지를 점유하면 거리는 최근접 필지
        기준으로 잰다. 판정에 쓴 필지(최근접)를 표시하고, 점유 필지 전체를
        occupied_parcels 로 실어 화면이 「외 N필지」로 뭉뚱그리지 않게 한다.
        최근접이라고 해서 확인되지 않은 AND·예외 조건을 확정으로 올리지 않는다
        (거리 기준만 최근접이다).

        enumerated=False 는 점유 필지를 열거해주는 원천이 없어 좌표가 포함된 단일
        필지만 잡았다는 뜻이다. 실제 시설이 좌표에서 먼 다른 필지도 점유하면 그
        필지가 임계 안이어도 놓친다. 이 사실을 metadata(single_parcel_only)와
        geometry_note 에 남겨 「다필지를 다 본 것처럼」 보이지 않게 한다.
        """

        measured_parcels: list[tuple[HazardOccupiedParcel, object]] = []
        for ring, pnu, jibun, address, area in parcels:
            if len(ring) < 4:
                continue
            try:
                measured = distance_polygons_to_polygon_m(boundaries, ring)
            except ValueError:
                continue
            occupied = HazardOccupiedParcel(
                pnu=pnu,
                jibun=jibun,
                address=address,
                area_m2=area,
                geometry=ring,
                distance_m=measured.distance_m,
            )
            measured_parcels.append((occupied, measured))
        if not measured_parcels:
            return
        # 최근접(최소 경계거리) 필지를 판정 필지로 고른다.
        nearest_occupied, nearest_measured = min(
            measured_parcels, key=lambda item: item[1].distance_m
        )
        nearest_occupied.is_judgment_parcel = True

        facility.distance_m = nearest_measured.distance_m
        facility.nearest_boundary_point = nearest_measured.nearest_a
        facility.nearest_facility_point = nearest_measured.nearest_b
        facility.geometry = nearest_occupied.geometry
        facility.geometry_type = "polygon"
        facility.geometry_quality = "B"
        facility.geometry_note = "연속지적도 시설 필지 경계 · 건물·설비 외곽선이 아님"
        if nearest_occupied.pnu:
            facility.parcel_pnu = nearest_occupied.pnu
            facility.judgment_parcel_pnu = nearest_occupied.pnu
        facility.occupied_parcels = [occ for occ, _ in measured_parcels]
        # 점유 필지 열거 원천이 없어 단일 필지(좌표 포함 필지)만으로 판정한 경우,
        # 그 사실을 결과에 드러낸다. 화면이 다필지를 전부 확인한 것으로 오해하지
        # 않게 한다. 열거 원천이 붙으면 enumerated=True 로 정상 다필지 판정이 된다.
        if not enumerated:
            facility.metadata["single_parcel_only"] = True
            facility.geometry_note = (
                f"{facility.geometry_note} · 점유 필지 열거 원천이 없어 좌표가 "
                "포함된 단일 필지만 판정했습니다(다른 점유 필지가 있으면 최근접 "
                "필지를 놓칠 수 있음)."
            )

    async def _facility_parcel(
        self,
        coordinates: Coordinates,
        vworld_ready: bool,
        cadastral_ready: bool,
    ) -> tuple[list[Coordinates], str]:
        """시설 좌표를 품는 지적 필지의 (링, PNU). VWorld 우선, 로컬 지적도 폴백."""

        if vworld_ready:
            try:
                parcel = await self.vworld.parcel_at(
                    coordinates.lat, coordinates.lng
                )
            except VWorldAPIError:
                parcel = None
            if parcel:
                if parcel_rejection_reason(parcel.jibun):
                    # 도로·하천 필지 위 좌표 — 필지를 붙이지 않고 점으로 남긴다.
                    return [], parcel.pnu
                return parcel.ring, parcel.pnu
        if cadastral_ready:
            try:
                # 연속지적도 조회는 동기 SQLite(bbox 인덱스) + Shapely 포함판정이라
                # 3.88M필지·2.10GB 인덱스에서 시설마다 async 경로에서 직접 돌리면
                # 이벤트 루프를 막는다. executor 로 옮겨 루프를 비운다. 호출부
                # (_attach_facility_boundaries)가 좌표 키로 캐시하고 순차로 await 하므로
                # 같은 시설을 반복 조회하지 않고 executor 동시 실행도 1건씩만 일어나
                # 읽기 전용 SQLite 연결 공유가 안전하다.
                local = await asyncio.to_thread(
                    self.cadastral.parcel_at, coordinates.lat, coordinates.lng
                )
            except Exception:
                local = None
            if local is not None:
                if parcel_rejection_reason(local.jibun):
                    return [], local.pnu
                return local.ring, local.pnu
        return [], ""

    async def _annotate_registered_factories(
        self,
        request: HazardReviewRequest,
        facilities: list[HazardFacility],
        search_radius_m: float,
        now: datetime,
    ) -> None:
        """등록공장 후보에 대기·소음 배출 신고를 부가 정보로만 덧붙인다.

        LH 확정 2026-09-11 (안건 ①): 공장은 등록공장 소재 여부만 「공장 있음」으로
        검토 표시하고, 유해공장(가~라목) 매칭 판정은 하지 않는다. 대기배출·소음배출
        원장은 판정 근거로 쓰지 않되, 등록공장과 같은 필지/좌표(OFFICIAL_DEDUPE_M
        이내)에 신고 이력이 있으면 그 사실만 note 에 덧붙여 담당자 확인을 돕는다.
        등록공장이 아닌 대기·소음 신고 사업장(우체국 냉방기·목욕탕 보일러 등)은
        여기서도 후보로 만들지 않는다 — 이 함수는 이미 후보인 등록공장에만 주석한다.
        """

        registered = [f for f in facilities if f.metadata.get("factory_registered")]
        if not registered:
            return

        # 대기배출 신고(행안부 대기오염물질배출시설) — 판정 원천 아님, 주석 전용.
        air_rows: list = []
        if self._store_ready() and self.facility_store:
            try:
                air_rows = list(
                    self.facility_store.facilities_around(
                        request.site.coordinates, search_radius_m, ("air_pollution",)
                    )
                )
            except Exception:
                air_rows = []

        # 소음배출 신고(전국 소음진동배출시설 표준데이터) — 판정 원천 아님, 주석 전용.
        noise_rows: list = []
        if self._noise_emission_ready():
            try:
                fetched = await self.noise_emission.facilities_around(
                    request.site.coordinates, search_radius_m
                )
                noise_rows = [n for n in fetched if n.is_noise_facility]
            except NoiseEmissionAPIError:
                noise_rows = []

        for factory in registered:
            notes: list[str] = []
            air_hit = next(
                (
                    r
                    for r in air_rows
                    if haversine_meters(factory.coordinates, r.coordinates)
                    <= OFFICIAL_DEDUPE_M
                ),
                None,
            )
            if air_hit is not None:
                grade = self._air_grade(getattr(air_hit, "category", "") or "")
                grade_text = f"종별 {grade}종" if grade else "종별 미상"
                notes.append(f"대기배출 신고 있음({grade_text})")
                factory.metadata["air_emission_reported"] = True
            if any(
                haversine_meters(factory.coordinates, n.coordinates)
                <= OFFICIAL_DEDUPE_M
                for n in noise_rows
            ):
                notes.append("소음배출 신고 있음")
                factory.metadata["noise_emission_reported"] = True
            if notes:
                factory.metadata["emission_notes"] = notes
                factory.classification_note = (
                    f"{factory.classification_note} {' · '.join(notes)}".strip()
                )

    async def _attach_building_register(
        self,
        facilities: list[HazardFacility],
        rule_categories: list[Category],
        threshold: int,
    ) -> None:
        """§6.4 AND 후보의 필지 표제부 용도를 조회해 판정 근거를 metadata 에 붙인다.

        임계거리 이내이고 필지 PNU 가 확보된 위락(단란주점·테마파크) 후보만 조회한다.
        조회 실패는 확정 근거가 아니므로 확인불가로 남긴다(br_error 표시). 결과는
        second_class/sports 3값과 한국어 판정근거(br_reason)로 실어 화면까지 낸다.
        """

        br_categories = [c for c in rule_categories if c.requires_building_register]
        if not br_categories or self.building_register is None:
            return
        targets = [
            f
            for f in facilities
            if f.distance_m <= threshold
            and f.parcel_pnu
            and any(self._in_category(f, cat) for cat in br_categories)
        ]
        if not targets:
            return
        pnus = [f.parcel_pnu for f in targets]
        try:
            results = await self.building_register.lookup_many(pnus)
        except BuildingRegisterAPIError:
            for facility in targets:
                facility.metadata["br_error"] = True
            return
        for facility in targets:
            result = results.get(facility.parcel_pnu)
            if result is None:
                facility.metadata["br_error"] = True
                continue
            facility.metadata["br_second_class"] = (
                result.second_class_neighborhood.value
            )
            facility.metadata["br_sports"] = result.sports_facility.value
            facility.metadata["br_reason"] = self._br_reason(facility, result)

    @staticmethod
    def _br_reason(facility: HazardFacility, result) -> str:
        """건축물대장 판정 근거 한 줄. 주용도 판정/기타용도 판정/확인불가를 구분한다.

        (제약) 표제부(getBrTitleInfo)는 건물 전체 면적만 주고 용도별 바닥면적이
        없어, 단란주점 150㎡ 같은 면적 기준은 표제부만으로 확정할 수 없다.
        """

        second = result.second_class_neighborhood
        sports = result.sports_facility
        # 대표 주용도 문자열(첫 동 기준). 근거를 사람이 읽게 넣는다.
        main = result.uses[0].main_purpose if result.uses else ""
        etc = result.uses[0].etc_purpose if result.uses else ""
        is_theme = facility.facility_type.startswith("theme_park")

        def basis(verdict, key: str) -> str:
            # 대상 키워드가 주용도에 있으면 주용도 판정, 기타용도에만 있으면 기타용도
            # 판정으로 근거를 구분한다(건축법 시행령 별표1 표준 용도 대분류 기준).
            main_n = main.replace(" ", "")
            etc_n = etc.replace(" ", "")
            if verdict.value == "해당":
                if key.replace(" ", "") in main_n:
                    return f"주용도('{main}')에 {key} 명시"
                return f"기타용도('{etc}')에 {key} 명시(별표1 표준용도)"
            if verdict.value == "비해당":
                return f"주용도('{main}')가 {key} 아님"
            return "표제부 용도를 표준용도로 식별 불가"

        if is_theme:
            parts = [
                f"제2종 근린생활시설: {basis(second, '제2종근린생활시설')}",
                f"운동시설: {basis(sports, '운동시설')}",
            ]
            if second.value == "비해당" and sports.value == "비해당":
                return "제2종 근린생활·운동시설 모두 비해당 → 매입제외 대상 확정. " + " / ".join(parts)
            if second.value == "해당" or sports.value == "해당":
                return "제2종 근린생활 또는 운동시설에 해당 → 위락 매입제외 대상 아님. " + " / ".join(parts)
            return "제2종 근린생활·운동시설 여부 확인불가(표제부는 용도별 바닥면적 미제공). " + " / ".join(parts)
        # 단란주점(가목)
        if second.value == "비해당":
            return f"제2종 근린생활시설 비해당 → 매입제외 대상 확정. {basis(second, '제2종근린생활시설')}"
        if second.value == "해당":
            return f"제2종 근린생활시설 해당 → 위락 매입제외 대상 아님. {basis(second, '제2종근린생활시설')}"
        return "제2종 근린생활시설 여부 확인불가(표제부는 용도별 바닥면적 미제공)."

    async def _attach_zoning(
        self,
        facilities: list[HazardFacility],
        threshold: int,
    ) -> None:
        """석유대체연료 판매업 후보에 용도지역(지적편집도)을 붙인다 (LH 확정 2026-09-11 #5).

        사업자등록 소재지만 있는 경우(주거지역 빌라 사무실 등)를 가려내기 위해 VWorld
        용도지역 레이어를 시설 좌표로 점 조회한다. 판정창 이내(임계거리 이하) 석유대체
        연료 후보만 조회해 VWorld 호출을 아낀다. 조회 실패·미확인은 zoning_class
        "unknown"(확인 요청 대상)으로 남긴다. 주유소·CNG·LPG 는 여기 오지 않는다
        (호출부가 dataset 으로 이미 석유대체연료만 걸러 넘긴다).
        """

        if self.vworld is None or not self.vworld.enabled:
            return
        targets = [
            f
            for f in facilities
            if f.distance_m <= threshold
            and f.metadata.get("dataset") == "petroleum_alt_fuel_retailers"
        ]
        for facility in targets:
            try:
                info = await self.vworld.zoning_at(
                    facility.coordinates.lat, facility.coordinates.lng
                )
            except VWorldAPIError:
                info = None
            name = info.name if info else ""
            facility.zoning_name = name
            facility.zoning_class = classify_zoning(name)
            facility.zoning_source = "VWorld 용도지역(지적편집도 LT_C_UQ111)"

    @staticmethod
    def _petroleum_zoning_hold(facility: HazardFacility) -> bool:
        """석유대체연료 후보가 용도지역 확인 요청 대상인지 (LH 확정 2026-09-11 #5).

        석유대체연료 판매업이면서 용도지역이 비주거로 확인되지 않은 건(주거지역·미확인·
        미조회)은 사업자등록 소재지 가능성이 있어 확정 제외로 올리지 않는다.
        """

        return (
            facility.metadata.get("dataset") == "petroleum_alt_fuel_retailers"
            and facility.zoning_class != "non_residential"
        )

    # ------------------------------------------------------------------
    # 원천 준비상태
    # ------------------------------------------------------------------
    def _opinet_ready(self) -> bool:
        return bool(self.opinet and self.opinet.enabled and not self.demo_mode)

    def _lpg_ready(self) -> bool:
        return bool(self.kgs_lpg and self.kgs_lpg.enabled and not self.demo_mode)

    def _cng_ready(self) -> bool:
        return bool(self.cng and self.cng.enabled and not self.demo_mode)

    def _safemap_ready(self) -> bool:
        return bool(self.safemap and self.safemap.enabled and not self.demo_mode)

    def _crematorium_ready(self) -> bool:
        return bool(
            self.crematorium and self.crematorium.enabled and not self.demo_mode
        )

    def _cadastral_ready(self) -> bool:
        """전북 연속지적도 로컬 인덱스가 실제로 적재돼 조회 가능한지.

        인덱스나 원본이 없으면 status().available 이 False 라 조용히 미적재로
        판단한다. status() 자체가 예외로 죽지 않게 설계돼 있다.
        """

        if self.cadastral is None or self.demo_mode:
            return False
        try:
            return self.cadastral.status().available
        except Exception:
            return False

    def _building_register_ready(self) -> bool:
        return bool(
            self.building_register
            and self.building_register.enabled
            and not self.demo_mode
        )

    def _noise_emission_ready(self) -> bool:
        """소음진동배출시설 원천이 실제로 조회 가능한지(공장 라목 게이트).

        API 활용신청 미완(403)이고 CSV 도 미주입이면 enabled=False 라 미연결이다.
        CSV 경로를 주입하면 전북 표준데이터로 보조 동작한다.
        """

        return bool(
            self.noise_emission
            and self.noise_emission.enabled
            and not self.demo_mode
        )

    def _factory_registry_loaded(self) -> bool:
        """등록공장 원천이 있는지 — 로컬 factoryON 표준본 또는 산단공 공장등록 API."""

        if self.demo_mode:
            return False
        return self.local_sources.factory_registry_loaded or self._factory_api_ready()

    def _store_ready(self) -> bool:
        return bool(
            self.facility_store
            and not self.demo_mode
            and self.facility_store.available
        )

    def _ready_datasets(self) -> set[str]:
        """실제 레코드가 적재된 인허가 데이터셋 키 집합.

        전역 준비상태(store.available)가 아니라 데이터셋별 준비상태를 본다.
        lodgings 하나만 적재됐다고 조회조차 안 한 gas_station·공장 카테고리까지
        '연결됨'으로 보면 '충돌 없음'이 잘못 나온다(룰북 §11).
        """

        if self.demo_mode or not self.facility_store:
            return set()
        getter = getattr(self.facility_store, "ready_datasets", None)
        if not callable(getter):
            return set()
        return set(getter())

    def _category_connected(self, category: Category) -> bool:
        """이 종류에 판정용 공개원천이 실제로 적재/연결돼 있는지.

        판정은 데이터셋별로 확인한다. 후보 매칭 datasets 중 하나라도 적재돼야
        하고, required_datasets(예: factoryON 등록공장 CSV)는 전부 적재돼 있어야
        한다. 하나라도 빠지면 이 종류는 dataset_missing 으로 내려간다.
        """

        if self.demo_mode:
            return True
        if category.data_state in NON_JUDGED_DATA_STATES:
            return False
        ready = self._ready_datasets()
        # AND 성립에 반드시 필요한 데이터셋이 전부 적재돼야 한다. factoryON 등록공장은
        # 인허가 캐시가 아니라 로컬 원천 묶음(표준본 공장 PNU 집합)에 있으므로
        # facility_store 가 아니라 _factory_registry_loaded() 로 확인한다.
        for key in category.required_datasets:
            if key == "factory_registry":
                if not self._factory_registry_loaded():
                    return False
            elif key not in ready:
                return False
        # 주유소·LPG 충전소는 LOCALDATA 원장이 우선, 공개 API 는 보조 원천이다.
        # datasets 검사보다 먼저 처리해 보조 원천만 붙어도 연결로 인정한다.
        if category.key == "gas_station":
            return (
                self._store_ready()
                or self._opinet_ready()
                or self._safemap_ready()
            )
        if category.key == "lpg_station":
            return (
                self._lpg_ready()
                or self._opinet_ready()
                or self._safemap_ready()
            )
        if category.key == "crematorium":
            return self._crematorium_ready()
        # 공장 있음(등록공장): factoryON 등록공장(표준본 공장 좌표)이 후보다. 파일만
        # 있고 좌표를 해석할 수 있는 레코드가 0건이면 조회할 스냅샷이 없는 것이므로,
        # 좌표 보유 공장 레코드가 최소 1건은 있어야 연결로 인정한다(없으면
        # dataset_missing). required 게이트(factory_registry PNU)도 위에서 통과해야 한다.
        if category.key == "factory_registered":
            return bool(self.local_sources.factory_facilities) or self._factory_api_ready()
        # CNG 충전소: 가스안전공사 API(ODcloud 15001508)가 우선, 좌표 보유 CSV(로컬
        # 원천)는 보조. 둘 중 하나라도 붙어 있으면 판정한다.
        if category.key == "cng_station":
            return self._cng_ready() or bool(self.local_sources.cng_facilities)
        if category.datasets:
            return any(key in ready for key in category.datasets)
        # 후보 매칭 데이터셋도 없고(공장 라목) required 도 이미 통과했다면, 소음 원천이
        # 아직 없어 판정할 수 없는 상태다(현행 유지). 그 외 전국 단위 공개원천이 아직
        # 없는 applied 종류(테마파크 등)도 여기로 온다.
        return False

    def _not_connected_note(self, category: Category) -> str:
        """연결되지 않은 종류의 dataset_missing note. 원인을 구분해 드러낸다."""

        # 공장 있음: 등록공장 원천(로컬 표준본·산단공 API) 둘 다 없을 때의 사유.
        if (
            "factory_registry" in category.required_datasets
            and not self._factory_registry_loaded()
        ):
            return FACTORY_REGISTRY_MISSING_NOTE
        # 미검증 엔드포인트(감사 §F)는 '장애'가 아니라 '미검증'으로 구분한다.
        ready = self._ready_datasets()
        unverified = [
            DATASET_BY_KEY[key].label
            for key in category.datasets
            if key not in ready
            and key in DATASET_BY_KEY
            and not DATASET_BY_KEY[key].verified
        ]
        if unverified:
            return f"{ENDPOINT_UNVERIFIED_NOTE} ({', '.join(unverified)})"
        if category.data_state == "applied":
            return APPLIED_NOT_CONNECTED_NOTE
        return "필요 데이터셋이 아직 연결되지 않았습니다."

    def _category_active_sources(self, category: Category) -> set[str]:
        """이 종류가 실제로 조회하는(=활성화된) 원천 식별자 집합."""

        if self.demo_mode:
            return set()
        sources: set[str] = set()
        if category.key == "gas_station":
            if self._store_ready():
                sources.add("localdata")
            if self._opinet_ready():
                sources.add("opinet")
            if self._safemap_ready():
                sources.add("safemap")
            return sources
        if category.key == "lpg_station":
            if self._lpg_ready():
                sources.add("kgs")
            if self._opinet_ready():
                sources.add("opinet")
            if self._safemap_ready():
                sources.add("safemap")
            return sources
        if category.key == "crematorium":
            if self._crematorium_ready():
                sources.add("crematorium")
            return sources
        if category.key == "cng_station":
            if self._cng_ready():
                sources.add("cng")
            return sources
        # 공장 있음(등록공장): 후보는 로컬 원천(factoryON 표준본 공장)이라 localdata
        # 실패와 무관하다. 활성 원천으로 집계하지 않는다(대기·소음은 주석 전용).
        if category.key == "factory_registered":
            return sources
        if category.datasets:
            ready = self._ready_datasets()
            if any(key in ready for key in category.datasets):
                sources.add("localdata")
        return sources

    def _local_source_detail(self, key: str) -> str:
        """로컬 칩 detail. 번들이 보존한 실제 적재 파일명을 쓴다(하드코딩 금지).

        표준본에서 적재됐으면 표준본 파일명(+짧은 구분), 원본이면 원본 파일명을 낸다.
        파일명을 모르면 빈 값 → 칩은 "로컬"만. 종전엔 원본 파일명을 박아, 표준본으로
        판정이 성립하면 표시 파일명이 실제 출처와 어긋났다(Codex 리뷰).
        """

        src = self.local_sources.source_files.get(key)
        return src.chip_detail() if src is not None else ""

    def _category_data_sources(
        self,
        category: Category,
        failed_sources: set[str] | None = None,
    ) -> list[HazardDataSource]:
        """이 종류의 판정에 「실제로 붙어 있는」 원천만 화면 칩으로 낸다.

        _category_active_sources 는 'localdata' 를 뭉뚱그린 한 식별자로만 돌려주는데,
        심사표 「데이터」 열은 데이터셋별 라벨·URL 이 있어야 API 판정임이 드러난다.
        그래서 여기서는 준비 술어(_ready_datasets·_opinet_ready 등)를 직접 읽어
        데이터셋 단위 칩을 만들고, active_sources 가 집계하지 않는 로컬 파일 원천
        (factoryON·CNG)까지 함께 담는다. 미연결이면 빈 리스트다.

        failed_sources 는 「이번 요청에서 조회가 실패한 provider 식별자 집합」이다.
        provider 가 준비돼(_opinet_ready 등) 있어도 이번 요청에서 실패했으면 그
        스냅샷은 실제 판정에 붙지 못했으므로 칩에서 뺀다(「데이터」 열 = 이 판정에
        실제로 붙은 원천). 실패 추적 단위가 provider 전체라 'localdata' 실패 시
        데이터셋별 칩이 함께 빠진다. 실패는 상태 배지로 이미 드러나므로 여기서
        '실패' 칩을 새로 만들지 않고 빼기만 한다. 실패로 목록이 비면 빈 리스트다.
        """

        # 데모 모드는 어떤 실원천도 붙지 않는다(안내 칩 하나만).
        if self.demo_mode:
            return [demo_source()]

        failed_sources = failed_sources or set()
        sources: list[HazardDataSource] = []
        ready = self._ready_datasets()

        # 행안부 인허가(LOCALDATA): 데이터셋마다 라벨·URL 이 달라 준비된 것만 낸다.
        # 주유소는 category.datasets 가 비어 있고 석유판매업 원장(업태=주유소)으로
        # 판정하므로 oil_retailers 를 명시로 붙인다(source_label 과 일치).
        # LOCALDATA 는 실패 추적이 'localdata' provider 단위라, 실패하면 이 요청에서
        # 데이터셋별 칩을 통째로 뺀다(데이터셋 단위 실패 추적은 없다).
        localdata_keys: list[str]
        if "localdata" in failed_sources:
            localdata_keys = []
        elif category.key == "gas_station":
            localdata_keys = ["oil_retailers"] if "oil_retailers" in ready else []
        else:
            localdata_keys = [key for key in category.datasets if key in ready]
        for key in localdata_keys:
            chip = localdata_source(key)
            if chip is not None:
                sources.append(chip)

        # 주유소·LPG 충전소의 보조 공개 API. 준비됐고 이번 요청에서 실패하지 않은
        # 것만 붙인다(API 먼저 규칙 유지). 실패한 provider 는 스냅샷이 없어 뺀다.
        if category.key == "gas_station":
            if self._opinet_ready() and "opinet" not in failed_sources:
                sources.append(api_source("opinet"))  # type: ignore[arg-type]
            if self._safemap_ready() and "safemap" not in failed_sources:
                sources.append(api_source("safemap"))  # type: ignore[arg-type]
        elif category.key == "lpg_station":
            if self._lpg_ready() and "kgs" not in failed_sources:
                sources.append(api_source("kgs"))  # type: ignore[arg-type]
            if self._opinet_ready() and "opinet" not in failed_sources:
                sources.append(api_source("opinet"))  # type: ignore[arg-type]
            if self._safemap_ready() and "safemap" not in failed_sources:
                sources.append(api_source("safemap"))  # type: ignore[arg-type]
        elif category.key == "crematorium":
            if self._crematorium_ready() and "crematorium" not in failed_sources:
                sources.append(api_source("crematorium"))  # type: ignore[arg-type]

        # CNG 충전소: 가스안전공사 API 가 붙어 있고 이번 요청에서 실패하지 않았으면
        # api 칩. 로컬 CSV 는 아래서 보조 칩으로 뒤에 붙는다(API 먼저).
        if category.key == "cng_station":
            if self._cng_ready() and "cng" not in failed_sources:
                sources.append(api_source("cng"))  # type: ignore[arg-type]

        # 로컬 납품 파일 원천. active_sources 에는 집계되지 않으므로 여기서 붙인다.
        # 등록공장은 공개 API 가 없어 XLSX 로만, CNG 는 API 보조로 CSV 를 쓴다(§A-1).
        if category.key == "factory_registered" and self.local_sources.factory_facilities:
            sources.append(local_source(self._local_source_detail("factory_registry")))
        elif category.key == "cng_station" and self.local_sources.cng_facilities:
            sources.append(local_source(self._local_source_detail("cng_stations")))

        return sources

    # ------------------------------------------------------------------
    # 카테고리 판정
    # ------------------------------------------------------------------
    def _implementation_state(
        self,
        category: Category,
    ) -> tuple[ImplementationState, str]:
        """구현/확정/검증 축을 판단한다. data_state(원천 상태)와는 별개 축이다.

        - 미구현(not_implemented): 어댑터/파이프라인이 아직 없음
          (건축물대장 교차확인, factoryON 등록공장 적재)
        - 검증필요(needs_verification): 엔드포인트/데이터는 등록됐으나 실응답 미확인
          (localdata verified=False 데이터셋)
        미확정(unconfirmed)은 룰북 §8 pending_items 로 별도 패널에 노출한다.
        우선순위: 미구현 > 검증필요. 낮은 축의 사유도 note 에 함께 싣는다.
        """

        not_implemented_reasons: list[str] = []
        # 건축물대장 교차확인 어댑터는 배선됐다. 클라이언트가 실제로 연결돼 있으면
        # 구현 축은 ready 다. 연결 전(키·활용신청 전)이면 여전히 미구현으로 남긴다.
        if category.requires_building_register and not self._building_register_ready():
            not_implemented_reasons.append(
                "건축물대장 용도 교차확인 미연결"
                "(제2종 근린생활·운동시설 AND)"
            )
        # factoryON 등록공장(PNU 집합)은 로컬 원천으로 배선됐다. 적재 전이면 미적재로
        # 남기고, 적재됐으면 이 사유를 뺀다.
        if (
            "factory_registry" in category.required_datasets
            and not self._factory_registry_loaded()
        ):
            not_implemented_reasons.append("등록공장 원천 없음(API 키·표준본 미연결)")

        unverified = [
            DATASET_BY_KEY[key].label
            for key in category.datasets
            if key in DATASET_BY_KEY and not DATASET_BY_KEY[key].verified
        ]

        note_parts: list[str] = []
        state: ImplementationState = "ready"
        if not_implemented_reasons:
            state = "not_implemented"
            note_parts.extend(not_implemented_reasons)
        if unverified:
            if state == "ready":
                state = "needs_verification"
            note_parts.append(
                f"엔드포인트 등록·실응답 미확인: {', '.join(unverified)}"
            )
        return state, " · ".join(note_parts)

    def _judgment_excluded_category(
        self,
        category: Category,
    ) -> HazardCategorySummary:
        """군부대·사격장 등 협의로 판정 제외된 종류의 요약.

        리스트에는 보이되(판정 제외 배지) 종합상태·status_counts 에는 참여하지
        않는다. 상태는 not_applicable, 사유는 JUDGMENT_EXCLUDED_REASON.
        """

        impl_state, impl_note = self._implementation_state(category)
        return HazardCategorySummary(
            key=category.key,
            label=category.label,
            rule_id=category.rule_id,
            rule_label=self._rule_label(category.rule_id),
            facility_types=list(category.facility_types),
            threshold_m=None,
            status="not_applicable",
            status_label=STATUS_LABELS["not_applicable"],
            data_state=category.data_state,
            implementation_state=impl_state,
            implementation_note=impl_note,
            judgment_excluded=True,
            not_applicable_reason=JUDGMENT_EXCLUDED_REASON,
            manual_check_required=False,
            source_label=category.source_label,
            note=category.note or JUDGMENT_EXCLUDED_REASON,
            doc_ref=category.doc_ref,
        )

    def _not_applicable_category(
        self,
        category: Category,
        reason: str,
    ) -> HazardCategorySummary:
        impl_state, impl_note = self._implementation_state(category)
        return HazardCategorySummary(
            key=category.key,
            label=category.label,
            rule_id=category.rule_id,
            rule_label=self._rule_label(category.rule_id),
            facility_types=list(category.facility_types),
            threshold_m=None,
            status="not_applicable",
            status_label=STATUS_LABELS["not_applicable"],
            data_state=category.data_state,
            implementation_state=impl_state,
            implementation_note=impl_note,
            not_applicable_reason=reason,
            manual_check_required=category.data_state in MANUAL_CHECK_DATA_STATES,
            source_label=category.source_label,
            note=reason,
            doc_ref=category.doc_ref,
        )

    def _evaluate_category(
        self,
        category: Category,
        threshold: int,
        rule_facilities: list[HazardFacility],
        site_boundary_resolved: bool,
        site_pnu: str,
        failed_sources: set[str] | None = None,
    ) -> HazardCategorySummary:
        failed_sources = failed_sources or set()
        connected = self._category_connected(category)
        manual = category.data_state in NON_JUDGED_DATA_STATES

        # 원천 미확보·수기 확인·개별 협의 종류는 후보 유무와 무관하게 dataset_missing.
        # 「판정 미적용 — 별도 수기 확인」(LH [요청 2] 승인)으로 표시하며 「이상 없음」
        # 으로 접지 않는다.
        if manual:
            return self._category_summary(
                category, threshold, "dataset_missing", category.note, [],
                connected=False, site_boundary_resolved=site_boundary_resolved,
                manual_check_required=(
                    category.data_state in MANUAL_CHECK_DATA_STATES
                ),
            )

        # 원천 조회가 실패했으면(활성 원천이 모두 실패) 스냅샷 자체가 없다.
        # no_conflict_in_snapshot 로 둔갑시키지 않고 dataset_missing 으로 낸다.
        active_sources = self._category_active_sources(category)
        failed_for_category = active_sources & failed_sources
        if active_sources and active_sources <= failed_sources:
            labels = ", ".join(
                SOURCE_FAILURE_LABELS.get(source, source)
                for source in sorted(failed_for_category)
            )
            return self._category_summary(
                category, threshold, "dataset_missing",
                f"원천 조회 실패({labels})로 스냅샷을 구성하지 못했습니다. "
                "조회 복구 후 재판정이 필요합니다.", [],
                connected=False, site_boundary_resolved=site_boundary_resolved,
            )

        matched = [f for f in rule_facilities if self._in_category(f, category)]
        # 일반숙박은 관광숙박시설 8종·취사 콘도와 생활숙박업을 판정대상에서 제외한다
        # (생활숙박 제외: LH 확정 2026-09-11 안건 ⑥).
        if category.key == "general_lodging":
            matched = [
                f
                for f in matched
                if not self._is_tourist_accommodation(f)
                and not self._is_living_accommodation(f)
            ]
        # 고압가스(라·바목): 자가설비(냉동)는 판정 미적용, 저장소·판매만 확정 가능.
        extra_note = ""
        if category.key == "high_pressure_gas":
            matched, self_use_count = self._classify_gas_facilities(matched)
            if self_use_count:
                extra_note = (
                    f"자가용 냉동설비 {self_use_count}건은 판정 미적용으로 뺐습니다 "
                    "(H-02-라바 §7-1)."
                )
        # 테마파크(다목): 원장 건축물용도가 체육시설이면 운동시설 해당으로 뺀다.
        if category.key.startswith("theme_park"):
            matched, sports_count = self._filter_theme_park_by_building_use(matched)
            if sports_count:
                extra_note = (
                    f"건축물용도 「체육시설」 {sports_count}건은 운동시설 해당으로 "
                    "뺐습니다 (H-04-다 §6-1)."
                )

        # 지식산업센터 지원시설 예외(공장 rule): 동일 PNU 지원시설은 제외.
        knic_note = ""
        if category.rule_id == "RB14-FACTORY":
            kept: list[HazardFacility] = []
            for facility in matched:
                if facility.metadata.get("knowledge_industry_center") and (
                    site_pnu and facility.parcel_pnu == site_pnu
                ):
                    knic_note = KNOWLEDGE_INDUSTRY_NOTE
                    continue
                kept.append(facility)
            matched = kept

        # 운영상태 필터: 폐업·취소·말소 제외, 휴업·공란은 review 후보.
        active: list[HazardFacility] = []
        hold: list[HazardFacility] = []
        for facility in matched:
            state = operating_state(facility.business_status)
            if state == "excluded":
                continue
            if state == "hold":
                hold.append(facility)
            else:
                active.append(facility)
        candidates = active + hold

        if not connected:
            # data_state=applied 인데 전국 단위 원천이 아직 없는 종류는
            # "판정적용" 배지와 "데이터셋 미확보"가 충돌한다. 그 모순을 note 로
            # 정직하게 드러낸다. factoryON 미적재·미검증 엔드포인트는 원인을
            # 구분해 밝힌다. 룰북 정본의 data_state 는 훼손하지 않는다.
            return self._category_summary(
                category, threshold, "dataset_missing",
                self._not_connected_note(category), candidates,
                connected=False, site_boundary_resolved=site_boundary_resolved,
            )

        status, note = self._decide_status(
            category, threshold, active, hold, site_boundary_resolved
        )
        if knic_note:
            note = f"{note} {knic_note}".strip()
        if extra_note:
            note = f"{note} {extra_note}".strip()
        # 부분 스냅샷 보호: 활성 원천 중 일부가 실패했는데(failed_for_category)
        # 남은 원천만으로 「충돌 없음」이 나오면, 실패한 원천 범위는 조회조차 못 한
        # 것이므로 no_conflict_in_snapshot 으로 둔갑시키지 않는다. 예: gas_station
        # 이 safemap 실패·다른 원천 0건이면 safemap 스냅샷이 없는데 충돌 없음이 된다.
        # 그런 음성 결과는 review_required 로 낮춰 실패 원천을 note 에 남긴다.
        # 양성 매치(임계거리 이내 후보를 실제로 찾은 경우)는 일부 원천이 실패해도
        # 찾은 것은 찾은 것이므로 그대로 둔다.
        if failed_for_category and status == "no_conflict_in_snapshot":
            labels = ", ".join(
                SOURCE_FAILURE_LABELS.get(source, source)
                for source in sorted(failed_for_category)
            )
            status = "review_required"
            note = (
                f"활성 원천 일부({labels}) 조회가 실패해 그 원천 범위의 스냅샷이 "
                "없습니다. 나머지 원천에서는 충돌이 없었으나, 실패 원천을 확인하지 "
                "못했으므로 충돌 없음으로 확정하지 않고 검토대상으로 남깁니다. "
                "조회 복구 후 재판정이 필요합니다."
            )
        return self._category_summary(
            category, threshold, status, note, candidates,
            connected=True, site_boundary_resolved=site_boundary_resolved,
            failed_sources=failed_sources,
        )

    def _decide_building_register(
        self,
        category: Category,
        threshold: int,
        inside_active: list[HazardFacility],
        buffer_active: list[HazardFacility],
        hold: list[HazardFacility],
        confirmed: Callable[[HazardFacility], bool],
    ) -> tuple[HazardReviewStatus, str]:
        """§6.4 단란주점·테마파크의 건축물대장 AND 판정.

        건축물대장 어댑터가 연결되지 않았으면(배선 전) 확인되지 않은 조건으로
        매입제외를 확정하지 않고 review_required 로 남긴다(룰북 §3). 연결됐으면
        임계거리 이내 후보의 표제부 용도로 판정한다.

        - 가목(단란주점): 제2종 근린생활시설 '비해당' → 매입제외 대상,
          '해당' → 후보 무효(제2종 근생이라 위락 아님), '확인불가' → 검토.
        - 다목(테마파크): '제2종 근린생활 비해당 AND 운동시설 비해당' → 매입제외 대상,
          하나라도 '해당' → 후보 무효, 하나라도 '확인불가' → 검토.
        '확인불가'는 절대 '비해당'으로 취급해 매입제외를 확정하지 않는다.
        """

        if not self._building_register_ready():
            if inside_active or buffer_active or hold:
                note = BUILDING_REGISTER_NOTE
                if buffer_active and not inside_active:
                    note = f"{note} 후보는 기준 {threshold}m 밖 여유구간에 있습니다."
                if hold:
                    note = (
                        f"{note} 휴업·상태 공란 후보는 확정하지 않고 검토로 남겼습니다."
                    )
                return "review_required", note
            return "no_conflict_in_snapshot", (
                f"스냅샷 범위 기준 {threshold}m 안에 해당 시설이 없습니다."
            )

        hazard: list[HazardFacility] = []
        uncertain: list[HazardFacility] = []
        excluded_by_use = 0  # 제2종 근생/운동시설 해당이라 위락 후보에서 빠진 건
        is_theme = category.key.startswith("theme_park")
        for facility in inside_active:
            second = str(facility.metadata.get("br_second_class") or "")
            sports = str(facility.metadata.get("br_sports") or "")
            if is_theme:
                if second == "해당" or sports == "해당":
                    excluded_by_use += 1
                    continue
                if second == "비해당" and sports == "비해당":
                    hazard.append(facility)
                else:
                    uncertain.append(facility)
            else:
                if second == "해당":
                    excluded_by_use += 1
                    continue
                if second == "비해당":
                    hazard.append(facility)
                else:
                    uncertain.append(facility)

        hazard_confirmed = [f for f in hazard if confirmed(f)]
        hazard_point = [f for f in hazard if not confirmed(f)]

        if hazard_confirmed:
            reason = str(hazard_confirmed[0].metadata.get("br_reason") or "")
            return "exclusion_match", (
                f"건축물대장 교차확인 결과 기준 {threshold}m 이내 매입제외 대상을 "
                f"확인했습니다. {reason}".strip()
            )
        if hazard_point:
            reason = str(hazard_point[0].metadata.get("br_reason") or "")
            return "geometry_missing", (
                "건축물대장 AND 는 확인됐으나 시설 경계 미확보로 확정판정하지 "
                f"않습니다. {reason}".strip()
            )
        if uncertain or buffer_active or hold:
            reason = ""
            if uncertain:
                reason = str(uncertain[0].metadata.get("br_reason") or "")
            note = f"{BUILDING_REGISTER_NOTE}"
            if reason:
                note = f"{note} {reason}"
            if buffer_active and not (uncertain or inside_active):
                note = f"{note} 후보는 기준 {threshold}m 밖 여유구간에 있습니다."
            if hold:
                note = f"{note} 휴업·상태 공란 후보는 확정하지 않고 검토로 남겼습니다."
            return "review_required", note
        if excluded_by_use:
            return "no_conflict_in_snapshot", (
                f"기준 {threshold}m 이내 후보가 제2종 근린생활시설"
                f"{'·운동시설' if is_theme else ''}에 해당하여 위락 매입제외 "
                "대상이 아닙니다(건축물대장 교차확인)."
            )
        return "no_conflict_in_snapshot", (
            f"스냅샷 범위 기준 {threshold}m 안에 해당 시설이 없습니다."
        )

    def _decide_status(
        self,
        category: Category,
        threshold: int,
        active: list[HazardFacility],
        hold: list[HazardFacility],
        site_boundary_resolved: bool,
    ) -> tuple[HazardReviewStatus, str]:
        """연결된 종류의 상태 판정. 경계 미확보는 확정판정하지 않는다."""

        def inside(f: HazardFacility) -> bool:
            # 임계값 동일은 포함, 경계 접촉·중첩은 0m.
            return f.distance_m <= threshold

        def confirmed(f: HazardFacility) -> bool:
            return site_boundary_resolved and f.geometry_type == "polygon"

        inside_active = [f for f in active if inside(f)]
        # 여유구간 검토는 경계를 확인하지 못한 후보를 메우기 위한 장치다. 시설
        # 폴리곤이 이미 붙어 distance_m 이 경계 대 경계 값이면 더 확인할 것이
        # 남아 있지 않으므로 검토 대상에서 뺀다. 빼지 않으면 확정된 통과까지
        # 끝없이 보류되어 판정이 끝나지 않는다.
        buffer_active = [f for f in active if not inside(f) and not confirmed(f)]
        key = category.key

        # §6.4 단란주점·테마파크: 건축물대장 용도 교차확인이 AND 조건이다.
        # 어댑터가 연결돼 있으면 표제부 용도로 확정하고, 연결 전이면 확인되지 않은
        # 조건으로 매입제외를 확정하지 않는다(룰북 §3, review_required).
        if category.requires_building_register:
            return self._decide_building_register(
                category, threshold, inside_active, buffer_active, hold,
                confirmed,
            )

        # 공장 있음(등록공장): LH 확정 2026-09-11 (안건 ①). 등록공장이 기준거리 이내면
        # 「공장 있음」으로 검토 표시만 한다. 유해공장(가~라목) 매칭 판정·자동 제외는
        # 하지 않으므로 절대 exclusion_match 로 올리지 않는다. 대기·소음 배출 신고는
        # 후보 note 에 부가 정보로만 덧붙여져 있다(_annotate_registered_factories).
        if key == "factory_registered":
            if inside_active or hold:
                return "review_required", FACTORY_PRESENT_NOTE
            if buffer_active:
                return "review_required", (
                    f"{FACTORY_PRESENT_NOTE} 경계 확인 대상 후보가 여유구간에 있습니다."
                )
            return "no_conflict_in_snapshot", (
                f"스냅샷 범위 기준 {threshold}m 안에 등록공장이 없습니다."
            )

        # 고압가스(partial): 시설종류가 확인된 건만 판정, 미확인은 review.
        if key == "high_pressure_gas":
            strong = [f for f in inside_active
                      if f.metadata.get("gas_type_confirmed") and confirmed(f)]
            inside_point = [f for f in inside_active
                            if f.metadata.get("gas_type_confirmed") and not confirmed(f)]
            unverified = [f for f in inside_active
                          if not f.metadata.get("gas_type_confirmed")]
            if strong:
                return "exclusion_match", (
                    f"시설종류가 확인된 고압가스업소가 기준 {threshold}m 이내에 있습니다."
                )
            if inside_point:
                return "geometry_missing", (
                    "시설종류는 확인됐지만 시설 경계 미확보로 확정판정하지 않습니다."
                )
            if unverified or buffer_active or hold:
                basis = ""
                if unverified:
                    basis = str(unverified[0].metadata.get("gas_basis") or "")
                return "review_required", (
                    "고압가스 후보가 있으나 시설종류 미확인 건이 있어 검토가 필요합니다."
                    + (f" {basis}" if basis else "")
                )
            return "no_conflict_in_snapshot", (
                f"스냅샷 범위 기준 {threshold}m 안에 해당 시설이 없습니다."
            )

        # 석유대체연료 판매업(LH 확정 2026-09-11 #5): 용도지역이 비주거(공업·상업 등)로
        # 확인된 건만 25m 그대로 확정한다. 주거지역 소재·용도지역 미확인은 사업자등록
        # 소재지(주거지역 빌라 사무실 등) 가능성이 있어 exclusion 으로 올리지 않고
        # review_required(확인 요청)로 남긴다. 비주거로 확인된 후보만 아래 일반 경로로
        # 내려보내 기존 25m 판정을 그대로 타게 한다.
        zoning_hold = [f for f in inside_active if self._petroleum_zoning_hold(f)]
        if zoning_hold:
            hold_ids = {id(f) for f in zoning_hold}
            inside_active = [f for f in inside_active if id(f) not in hold_ids]

        # 일반 applied 종류: 경계 확인 시 exclusion, 점 좌표 이내는 geometry_missing.
        # 생활안전지도 미분류 시설은 구분이 확인되지 않아 exclusion·geometry_missing
        # 으로 올리지 않고 review_required 로만 남긴다(룰북 §3).
        unclassified_inside = [
            f for f in inside_active if f.metadata.get("unclassified_source")
        ]
        classified_active = [
            f for f in inside_active if not f.metadata.get("unclassified_source")
        ]
        strong = [f for f in classified_active if confirmed(f)]
        inside_point = [f for f in classified_active if not confirmed(f)]

        def _zoning_suffix(items: list[HazardFacility]) -> str:
            name = next((f.zoning_name for f in items if f.zoning_name), "")
            return f" (용도지역 {name})" if name else ""

        if strong:
            return "exclusion_match", (
                f"기준 {threshold}m 이내에서 시설 경계로 확인된 대상이 "
                f"{len(strong)}곳 있습니다.{_zoning_suffix(strong)}"
            )
        # 주거지역·미확인 석유대체연료는 경계 확인 여부와 무관하게 확인 요청으로 남긴다.
        # review_required 가 geometry_missing 보다 우선순위가 높다(STATUS_PRIORITY).
        if zoning_hold:
            names = sorted({f.zoning_name for f in zoning_hold if f.zoning_name})
            label = ", ".join(names) if names else "미확인"
            return "review_required", (
                f"확인 요청 — 용도지역 {label} 소재. 석유대체연료 판매업이 기준 "
                f"{threshold}m 이내이나 사업자등록 소재지(주거지역 빌라·사무실 등) "
                "가능성이 있어 담당자 판단이 필요합니다 (LH 확정 2026-09-11)."
            )
        if inside_point:
            return "geometry_missing", (
                f"기준 {threshold}m 이내 후보가 있으나 시설 경계 미확보로 점 좌표로만 "
                f"재어 확정판정하지 않습니다.{_zoning_suffix(inside_point)}"
            )
        if unclassified_inside:
            return "review_required", SAFEMAP_UNCLASSIFIED_NOTE
        if buffer_active or hold:
            note = (
                f"기준 {threshold}m 밖 {BOUNDARY_BUFFER_M}m 여유구간에 경계 확인 "
                "대상이 있습니다."
            )
            if hold:
                note = f"{note} 휴업·상태 공란 후보는 확정하지 않고 검토로 남겼습니다."
            return "review_required", note
        return "no_conflict_in_snapshot", (
            f"스냅샷 범위 기준 {threshold}m 안에 해당 시설이 없습니다."
        )

    def _category_summary(
        self,
        category: Category,
        threshold: int,
        status: HazardReviewStatus,
        note: str,
        candidates: list[HazardFacility],
        connected: bool,
        site_boundary_resolved: bool,
        manual_check_required: bool = False,
        failed_sources: set[str] | None = None,
    ) -> HazardCategorySummary:
        inside = [f for f in candidates if f.distance_m <= threshold]
        score, breakdown = self._category_score(
            category, candidates, connected, site_boundary_resolved
        )
        connect_kind, connect_hint, connect_command = (
            ("connected", "", "")
            if connected
            else self._source_connect_guide(category)
        )
        impl_state, impl_note = self._implementation_state(category)
        # 연결된(=실제 판정된) 종류만 원천 칩을 낸다. 미연결·수기·판정제외는 빈
        # 리스트라 「데이터」 열 칸이 비고, API/로컬 판정만 칩으로 드러난다.
        # 이번 요청에서 실패한 provider 는 스냅샷이 붙지 못했으므로 칩에서 뺀다.
        data_sources = (
            self._category_data_sources(category, failed_sources)
            if connected
            else []
        )
        return HazardCategorySummary(
            key=category.key,
            label=category.label,
            rule_id=category.rule_id,
            rule_label=self._rule_label(category.rule_id),
            facility_types=list(category.facility_types),
            threshold_m=threshold,
            status=status,
            status_label=STATUS_LABELS[status],
            data_state=category.data_state,
            implementation_state=impl_state,
            implementation_note=impl_note,
            data_sources=data_sources,
            manual_check_required=manual_check_required,
            candidate_count=len(candidates),
            inside_threshold_count=len(inside),
            nearest_distance_m=(
                min(f.distance_m for f in candidates) if candidates else None
            ),
            source_label=category.source_label,
            source_connected=connected,
            source_connect_kind=connect_kind,
            source_connect_hint=connect_hint,
            source_connect_command=connect_command,
            note=note,
            confidence_score=score,
            score_breakdown=breakdown,
            facilities=sorted(candidates, key=lambda item: item.distance_m),
            doc_ref=category.doc_ref,
        )

    @staticmethod
    def _is_tourist_accommodation(facility: HazardFacility) -> bool:
        """관광숙박시설 8종·취사 콘도 여부(H-05 §5).

        숙박은 상호가 아니라 업태(BZSTAT_SE_NM)로 가른다 — 「○○호텔」이라도 업태가
        여관업이면 대상이고, 업태가 관광호텔이면 상호와 무관하게 제외한다. 업태가
        없는 원천(데모·보조 원천)에서만 상호·분류 라벨 키워드로 대신 가른다.
        """

        business = str(facility.metadata.get("business_category") or "").strip()
        if business:
            return any(word in business for word in TOURIST_ACCOMMODATION_BUSINESS_TYPES)
        haystack = " ".join([facility.name, facility.facility_type_label])
        return any(word in haystack for word in TOURIST_ACCOMMODATION_KEYWORDS)

    @staticmethod
    def _is_living_accommodation(facility: HazardFacility) -> bool:
        business = str(facility.metadata.get("business_category") or "")
        return any(word in business for word in LIVING_ACCOMMODATION_KEYWORDS)

    @staticmethod
    def _in_category(facility: HazardFacility, category: Category) -> bool:
        """후보가 이 종류에 속하는지.

        인허가 캐시·demo 데이터셋 표시가 있으면 데이터셋으로 가르고, 그 외에는
        facility_type 으로 가른다. 공장 대기배출은 종별(1~3 / 4~5)로 더 쪼갠다.
        """

        # 생활안전지도 미분류 주유시설: 주유소·LPG 어느 쪽인지 모르므로 두 종류
        # 모두의 후보로 잡아 어느 쪽도 「조회 안 됨」이 「충돌 없음」으로 새지 않게 한다.
        if facility.metadata.get("unclassified_source"):
            return category.key in ("gas_station", "lpg_station")

        dataset = str(facility.metadata.get("dataset") or "")
        # 공장 있음(등록공장): factoryON 등록공장(factory_registered 플래그)만 후보다
        # (LH 확정 2026-09-11). 대기·소음 신고 사업장은 등록공장이 아니면 공장으로
        # 올리지 않는다 — 주석(_annotate_registered_factories)으로만 쓴다.
        if category.key == "factory_registered":
            return bool(facility.metadata.get("factory_registered"))
        if dataset:
            return dataset in category.datasets
        return facility.facility_type in category.facility_types

    @staticmethod
    def _facility_matches_rule(
        facility: HazardFacility,
        rule_categories: list[Category],
    ) -> bool:
        return any(
            HazardReviewService._in_category(facility, cat)
            for cat in rule_categories
        )

    @staticmethod
    def _air_grade(category_text: str) -> int | None:
        """대기배출 종별 텍스트에서 1~5 종 등급을 뽑는다."""

        text = category_text or ""
        for grade in (1, 2, 3, 4, 5):
            if f"{grade}종" in text:
                return grade
        return None

    # ------------------------------------------------------------------
    # 신뢰도 점수
    # ------------------------------------------------------------------
    def _category_score(
        self,
        category: Category,
        matched: list[HazardFacility],
        connected: bool,
        site_boundary_resolved: bool,
    ) -> tuple[int, list[HazardScoreItem]]:
        items: list[HazardScoreItem] = []
        if connected:
            items.append(HazardScoreItem(
                label="원천 연결", earned=40, maximum=40,
                note=f"{category.source_label}에서 조회했습니다.",
            ))
        else:
            items.append(HazardScoreItem(
                label="원천 연결", earned=0, maximum=40,
                note="공개원천이 연결되지 않아 존재 여부를 확정할 수 없습니다.",
            ))
        items.append(HazardScoreItem(
            label="사업지 대지경계",
            earned=20 if site_boundary_resolved else 0, maximum=20,
            note=(
                "연속지적도 필지 경계를 사용했습니다."
                if site_boundary_resolved
                else "지적도 필지를 확보하지 못해 주소점 기준입니다."
            ),
        ))
        if not matched:
            items.append(HazardScoreItem(
                label="시설 대지경계", earned=25 if connected else 0, maximum=25,
                note="후보가 없어 잴 대상이 없습니다.",
            ))
        else:
            polygons = sum(1 for f in matched if f.geometry_type == "polygon")
            if polygons == len(matched):
                earned, note = 25, "모든 후보를 시설 대지경계로 쟀습니다."
            elif polygons:
                earned, note = 15, (
                    f"{len(matched)}곳 중 {polygons}곳만 시설 대지경계로 쟀습니다."
                )
            else:
                earned, note = 5, "시설을 점 좌표로 재 실제보다 멀게 나옵니다."
            items.append(HazardScoreItem(
                label="시설 대지경계", earned=earned, maximum=25, note=note,
            ))
        fresh = connected or bool(matched)
        items.append(HazardScoreItem(
            label="자료 최신성", earned=15 if fresh else 0, maximum=15,
            note=(
                "이번 검토에서 조회한 자료입니다."
                if fresh
                else "조회된 자료가 없습니다."
            ),
        ))
        return sum(item.earned for item in items), items

    def _source_connect_guide(self, category: Category) -> tuple[str, str, str]:
        if self.demo_mode:
            return (
                "demo",
                "지금은 데모 모드라 어떤 원천도 붙지 않습니다. 서버 .env 의 "
                "DEMO_MODE 를 false 로 두고 다시 띄우세요.",
                "",
            )
        if category.data_state in NON_JUDGED_DATA_STATES:
            return (
                "manual",
                "전국 단위 공개원천이 없는 종류입니다. 위치자료를 직접 확보해 "
                "수기 확인해야 합니다.",
                "",
            )
        if category.datasets:
            return (
                "localdata",
                "공공데이터포털 인증키를 넣고 인허가 대장을 한 번 적재하면 연결됩니다.",
                f"python -m app.sync_facilities {' '.join(category.datasets)}",
            )
        if category.key == "gas_station":
            return ("opinet", "오피넷 API 키(OPINET_API_KEY)를 넣고 다시 띄우세요.", "")
        if category.key == "lpg_station":
            return (
                "kgs",
                "가스안전공사 LPG API 는 공공데이터포털 인증키를 씁니다.",
                "",
            )
        return (
            "none",
            "전국 단위 공개원천이 아직 없는 종류입니다. 원천 확보 후 판정에 반영됩니다.",
            "",
        )

    # ------------------------------------------------------------------
    # 집계
    # ------------------------------------------------------------------
    def _finding_from_categories(
        self,
        rule: Rule,
        threshold: int | None,
        summaries: list[HazardCategorySummary],
        nearby: list[HazardFacility] | None = None,
    ) -> HazardFinding:
        status = self._promote([s.status for s in summaries])
        # 종류별 목록을 그대로 이어붙이면 앞선 종류의 먼 시설이 맨 앞에 온다.
        # 지도는 facilities[0] 에 최단거리선과 거리 라벨을 그리고 심사표는 그것을
        # 근거 시설로 적으므로, measured_distance_m 과 같은 시설이 오도록 다시
        # 거리순으로 정렬한다.
        facilities = sorted(
            (f for s in summaries for f in s.facilities),
            key=lambda facility: facility.distance_m,
        )
        measured = min(
            (f.distance_m for f in facilities), default=None
        )
        if threshold is None:
            reason = (
                f"{rule.label} Rule 은 이 신청유형에 적용되지 않습니다."
            )
        elif status == "exclusion_match":
            reason = f"기준 {threshold}m 이내에서 매입제외 대상을 확인했습니다."
        elif status == "review_required":
            reason = "검토 필요 종류가 있습니다. 판정 근거를 확인하세요."
        elif status == "geometry_missing":
            reason = "기준거리 이내 후보가 있으나 시설 경계 미확보로 확정하지 못했습니다."
        elif status == "dataset_missing":
            reason = "필요 데이터셋이 미확보된 종류가 있습니다."
        elif status == "no_conflict_in_snapshot":
            reason = f"스냅샷 범위 기준 {threshold}m 안에 충돌이 없습니다."
        else:
            reason = "적용 대상 종류가 없습니다."
        return HazardFinding(
            finding_id=f"{rule.rule_id}:{status}",
            rule_id=rule.rule_id,
            label=rule.label,
            status=status,
            status_label=STATUS_LABELS[status],
            threshold_m=threshold,
            measured_distance_m=measured,
            measurement_method=self._measurement_label(facilities),
            evidence_grade=self._finding_grade(facilities),
            legal_reference=rule.legal_reference,
            result_reason=reason,
            review_note=(
                "등록공장 소재·시설 경계·운영상태를 반영한 종류별 판정을 확인하세요."
            ),
            facilities=facilities,
            # 참고 시설은 measured_distance_m·evidence_grade·measurement_method
            # 계산에 넣지 않는다(위 값들은 facilities 만 본다). 지도 표시 전용.
            nearby_facilities=list(nearby or []),
        )

    @staticmethod
    def _finding_grade(
        facilities: list[HazardFacility],
    ) -> HazardEvidenceGrade | None:
        if not facilities:
            return None
        if any(f.geometry_type == "polygon" for f in facilities):
            return "B"
        return "D"

    @staticmethod
    def _measurement_label(facilities: list[HazardFacility]) -> str:
        if not facilities:
            return POINT_MEASUREMENT
        if all(f.geometry_type == "polygon" for f in facilities):
            return PARCEL_MEASUREMENT
        if any(f.nearest_boundary_point for f in facilities):
            return BOUNDARY_MEASUREMENT
        return POINT_MEASUREMENT

    @staticmethod
    def _promote(statuses: list[HazardReviewStatus]) -> HazardReviewStatus:
        for status in STATUS_PRIORITY:
            if status in statuses:
                return status
        return "not_applicable"

    def _overall_status(
        self,
        categories: list[HazardCategorySummary],
    ) -> HazardReviewStatus:
        return self._promote([c.status for c in categories])

    @staticmethod
    def _overall_summary(status: HazardReviewStatus, candidate_count: int) -> str:
        if status == "exclusion_match":
            return "기준거리 이내에서 매입제외 대상을 확인했습니다."
        if status == "review_required":
            return (
                f"검토 필요 종류가 있습니다. 후보 {candidate_count}곳의 판정 근거를 "
                "확인하세요."
            )
        if status == "geometry_missing":
            return "기준거리 이내 후보가 있으나 경계 미확보로 확정하지 못했습니다."
        if status == "dataset_missing":
            return "필요 데이터셋이 미확보된 종류가 있습니다."
        if status == "no_conflict_in_snapshot":
            return "사용 데이터 스냅샷 범위에서 충돌을 발견하지 못했습니다."
        return "이 신청유형에 적용되는 유해요소 Rule 이 없습니다."

    @staticmethod
    def _rule_label(rule_id: str) -> str:
        return next((r.label for r in RULES if r.rule_id == rule_id), rule_id)

    def _rule_bands(
        self,
        boundaries: list[list[Coordinates]],
        housing: HousingType,
        application: ApplicationType,
    ) -> list[HazardRuleBand]:
        """적용 임계거리별 필지 확장 도형. 미적용(None) 임계거리는 그리지 않는다."""

        if not boundaries:
            return []
        thresholds = {
            value
            for value in thresholds_for(housing, application).values()
            if value is not None
        }
        bands: list[HazardRuleBand] = []
        for threshold in sorted(thresholds):
            for ring in buffer_rings(boundaries, threshold):
                bands.append(HazardRuleBand(threshold_m=threshold, ring=ring))
        return bands

    @staticmethod
    def _calculation_note(
        site_boundary_resolved: bool,
        facility_parcels_resolved: bool = False,
    ) -> str:
        buffer_note = (
            f"기준거리 밖 {BOUNDARY_BUFFER_M}m 여유구간까지 함께 조회하고 해당 후보는 "
            "경계 확인 대상으로 표시합니다."
        )
        if site_boundary_resolved and facility_parcels_resolved:
            return (
                "거리는 신청 대지경계와 시설 대지경계 사이의 최단거리입니다. "
                "양쪽 모두 연속지적도 필지 경계이며 건물·설비 외곽선이 아닙니다. "
                f"{buffer_note}"
            )
        if site_boundary_resolved:
            return (
                "거리는 연속지적도 기준 신청 대지경계에서 시설 후보 점 좌표까지의 "
                f"최단거리입니다. {buffer_note} 시설 경계 미확보 후보는 확정판정하지 "
                "않고 경계 미확보로 남깁니다."
            )
        return (
            "현재 거리는 사업지 주소 좌표와 시설 후보 점 좌표 사이의 예비 직선거리입니다. "
            f"{buffer_note} 지적도 필지를 확보하면 대지경계 기준으로 다시 계산됩니다."
        )

    # ------------------------------------------------------------------
    # 원천 상태 · 완전성 지표
    # ------------------------------------------------------------------
    def _build_sources(
        self,
        now: datetime,
        boundary_rings: list[list[Coordinates]],
        failed_sources: set[str] | None = None,
    ) -> list[HazardSourceStatus]:
        failed_sources = failed_sources or set()

        def _state(source_key: str, ready: bool) -> HazardSourceState:
            # 조회를 시도했으나 실패한 원천은 미연결이 아니라 '실패'로 구분한다.
            if source_key in failed_sources:
                return "failed"
            return "connected" if ready else "unavailable"

        return [
            HazardSourceStatus(
                source_id="localdata-licence",
                label="행정안전부 지방행정인허가 대장",
                state=_state("localdata", self._store_ready()),
                retrieved_at=now,
                as_of=(
                    self.facility_store.latest_sync()
                    if self._store_ready() and self.facility_store
                    else None
                ),
                coverage_note=(
                    "숙박·유흥주점·단란주점·대기배출사업장·석유판매업의 영업 중인 "
                    "사업장입니다. 로컬 캐시에서 조회하므로 "
                    "python -m app.sync_facilities 로 갱신해야 합니다."
                    if self._store_ready()
                    else "인허가 캐시가 비어 있습니다. python -m app.sync_facilities 를 실행하세요."
                ),
                geometry_note="인허가 등록 점 좌표",
                required_for=["RB14-FACTORY", "RB14-FUEL25", "RB14-AMUSEMENT", "RB14-LODGING"],
            ),
            HazardSourceStatus(
                source_id="opinet-fuel",
                label="한국석유공사 오피넷 주유소·충전소",
                state=_state("opinet", self._opinet_ready()),
                retrieved_at=now,
                as_of=now if self._opinet_ready() else None,
                coverage_note=(
                    "주유소·LPG 충전소 사업자 등록 기반입니다."
                    if self._opinet_ready()
                    else "OPINET_API_KEY가 설정되지 않았습니다."
                ),
                geometry_note="사업자 등록 점 좌표",
                required_for=["RB14-FUEL25", "RB14-HAZMAT"],
            ),
            HazardSourceStatus(
                source_id="kgs-lpg",
                label="한국가스안전공사 전국 LPG 충전소 현황",
                state=_state("kgs", self._lpg_ready()),
                retrieved_at=now,
                as_of=now if self._lpg_ready() else None,
                coverage_note=(
                    "전국 LPG 충전소 허가 등록 기반입니다."
                    if self._lpg_ready()
                    else "공공데이터포털 인증키가 설정되지 않았습니다."
                ),
                geometry_note="허가 등록 점 좌표",
                required_for=["RB14-HAZMAT"],
            ),
            HazardSourceStatus(
                source_id="parcel-boundaries",
                label="공식 지적경계",
                state="partial" if boundary_rings else "unavailable",
                retrieved_at=now,
                as_of=now if boundary_rings else None,
                coverage_note=(
                    "VWorld 연속지적도에서 신청 필지 경계를 받았습니다. 참고도형이므로 "
                    "최종 배제 전 지적공부 확인이 필요합니다."
                    if boundary_rings
                    else "현재는 주소 좌표 주변의 검증용 임시 필지를 사용합니다."
                ),
                geometry_note=(
                    "연속지적도 대지경계"
                    if boundary_rings
                    else "VWorld 또는 승인된 지적도 연계 필요"
                ),
                required_for=[rule.rule_id for rule in RULES],
            ),
        ]

    @staticmethod
    def _source_connection_score(sources: list[HazardSourceStatus]) -> float:
        if not sources:
            return 0.0
        total = sum(SOURCE_STATE_SCORE.get(source.state, 0) for source in sources)
        return total / len(sources)

    @staticmethod
    def _source_freshness_score(sources: list[HazardSourceStatus]) -> int:
        if not sources:
            return 0
        fresh = sum(
            1
            for source in sources
            if source.state in FRESH_SOURCE_STATES and source.as_of is not None
        )
        return round(100 * fresh / len(sources))

    @staticmethod
    def _boundary_coverage_score(
        categories: list[HazardCategorySummary],
        site_boundary_resolved: bool,
    ) -> int:
        facilities = [f for c in categories for f in c.facilities]
        required = 1 + len(facilities)
        resolved = (1 if site_boundary_resolved else 0) + sum(
            1 for f in facilities if f.geometry_type == "polygon"
        )
        return round(100 * resolved / required)

    @staticmethod
    def _data_completeness(
        source_connection: float,
        boundary_coverage: int,
        source_freshness: int,
    ) -> int:
        return round((source_connection + boundary_coverage + source_freshness) / 3)

    @staticmethod
    def _progress_item(rule_id: str) -> tuple[str, str]:
        mapping = {
            "RB14-FACTORY": ("FACTORY", "공장"),
            "RB14-HAZMAT": ("HAZMAT", "위험물 저장·처리시설"),
            "RB14-FUEL25": ("FUEL", "주유·석유·천연가스"),
            "RB14-AMUSEMENT": ("AMUSEMENT", "위락시설"),
            "RB14-LODGING": ("LODGING", "일반숙박시설"),
            "RB14-CREMATION-MILITARY": ("CREMATION", "화장장 (군부대·사격장 판정 제외)"),
        }
        return mapping[rule_id]

    @staticmethod
    async def _checkpoint(
        cancel_event: asyncio.Event,
        progress: HazardProgressCallback,
        item_id: str,
        label: str,
        item_progress: int,
        count: int | None,
        message: str,
    ) -> None:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        await progress(item_id, label, "completed", item_progress, count, message)
