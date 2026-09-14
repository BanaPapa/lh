from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.hazard_review.data_sources import HazardDataSource
from app.hazard_review.rulebook import (
    ApplicationType,
    CategoryDataState,
    HazardReviewStatus,
    HousingType,
    ImplementationState,
)
from app.models import Coordinates, ProgressStatus


HazardSourceState = Literal["connected", "partial", "unavailable", "failed"]
HazardEvidenceGrade = Literal["A", "B", "C", "D"]


class HazardParcel(BaseModel):
    parcel_id: str
    pnu: str = ""
    address: str = ""
    area_m2: float | None = Field(default=None, ge=0)
    geometry: list[Coordinates] = Field(default_factory=list)
    geometry_source: Literal[
        "official_polygon",
        "parcel_polygon",
        "provisional_polygon",
        "point_fallback",
    ] = "provisional_polygon"
    geometry_note: str = ""

    @field_validator("geometry")
    @classmethod
    def validate_geometry(cls, value: list[Coordinates]) -> list[Coordinates]:
        if value and len(value) < 4:
            raise ValueError("parcel geometry must contain at least four coordinates")
        return value


class HazardSite(BaseModel):
    name: str
    address: str = ""
    coordinates: Coordinates
    # 룰북 §1 신청 유형. 매트릭스가 유형에 따라 임계거리를 바꾸므로 필수다.
    housing_type: HousingType
    application_type: ApplicationType
    parcels: list[HazardParcel] = Field(min_length=1)


class HazardReviewRequest(BaseModel):
    site: HazardSite
    # v1.4 단일 규칙팩만 존재하지만 프런트 호환을 위해 유지한다.
    rule_pack_id: str
    requested_by: str = "prototype-user"


class HazardRuleDefinition(BaseModel):
    """Rule 정의(§5). 임계거리는 단일값이 아니라 신청유형별 매트릭스를 참조한다."""

    rule_id: str
    label: str
    matrix_column: str
    legal_reference: str
    # 신청유형 10개 조합에 대한 임계거리표. 키는 "housing:application".
    thresholds: dict[str, int | None] = Field(default_factory=dict)


class HazardRulePack(BaseModel):
    id: str
    title: str
    version: str
    effective_from: str
    status: Literal["draft", "approved", "retired"] = "approved"
    source_document_url: str = ""
    note: str
    rules: list[HazardRuleDefinition]


class HazardOccupiedParcel(BaseModel):
    """시설이 점유하는 필지 하나. 시설이 여러 필지를 쓰면(공장의 「대표지번 외 N필지」
    등) 각 필지를 개별로 실어 화면이 「외 1필지」로 뭉뚱그리지 않게 한다(조준환 국장
    2026-08-26 요청). 담당자가 다른 필지를 골라 재판정할 수 있는 근거 데이터다.
    """

    pnu: str = ""
    jibun: str = ""
    address: str = ""
    area_m2: float | None = Field(default=None, ge=0)
    geometry: list[Coordinates] = Field(default_factory=list)
    # 이 필지 경계까지의 최단거리(m). 판정거리는 점유 필지 중 최소값이다.
    distance_m: float | None = Field(default=None, ge=0)
    # 판정에 실제로 쓴(최근접) 필지인지. 정민재 이사 2026-08-28 「최근접 필지」 확정.
    is_judgment_parcel: bool = False

    @field_validator("geometry")
    @classmethod
    def _validate_geometry(cls, value: list[Coordinates]) -> list[Coordinates]:
        if value and len(value) < 4:
            raise ValueError("parcel geometry must contain at least four coordinates")
        return value


class HazardFacility(BaseModel):
    facility_id: str
    facility_type: str
    facility_type_label: str
    name: str
    coordinates: Coordinates
    distance_m: float = Field(ge=0)
    address: str = ""
    road_address: str = ""
    business_status: str = "미확인"
    provider: str
    source_label: str
    source_record_id: str = ""
    source_url: str = ""
    source_as_of: datetime
    # distance_m 을 만드는 대지경계 위의 지점. 지도에 최단 연결선을 그릴 때 쓴다.
    # 이 값이 없으면 주소점 기준으로 잰 거리다.
    nearest_boundary_point: Coordinates | None = None
    # 시설 쪽 대지경계. 지적도에서 찾으면 거리도 경계 대 경계로 다시 잰다.
    # 복수 필지를 점유하면 판정에 쓴(최근접) 필지의 경계다.
    geometry: list[Coordinates] = Field(default_factory=list)
    nearest_facility_point: Coordinates | None = None
    parcel_pnu: str = ""
    # 판정에 쓴(최근접) 필지의 PNU. 대개 parcel_pnu 와 같지만 복수 점유 시 근거로
    # 명시한다. 담당자가 다른 필지로 바꾸면 그 필지로 재판정한다(다음 작업).
    judgment_parcel_pnu: str = ""
    # 시설이 점유하는 필지 전체(단일 필지 시설도 1개를 싣는다). 각 필지의 PNU·지번·
    # 폴리곤·거리를 담아 화면이 근거를 팝업할 수 있게 한다.
    occupied_parcels: list[HazardOccupiedParcel] = Field(default_factory=list)
    geometry_type: Literal["point", "polygon"] = "point"
    geometry_quality: HazardEvidenceGrade = "D"
    geometry_note: str
    classification_note: str
    # 용도지역(지적편집도) — 석유대체연료 판매업에만 붙인다(LH 확정 2026-09-11 #5).
    # zoning_name 은 용도지역명(예: "제2종일반주거지역"), zoning_class 는
    # "residential"/"non_residential"/"unknown"(미조회면 ""), zoning_source 는 출처.
    zoning_name: str = ""
    zoning_class: str = ""
    zoning_source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class HazardFinding(BaseModel):
    finding_id: str
    rule_id: str
    label: str
    status: HazardReviewStatus
    status_label: str
    # 해당 신청유형에 적용된 임계거리. 미적용이면 None.
    threshold_m: int | None = None
    measured_distance_m: float | None = Field(default=None, ge=0)
    measurement_method: str
    evidence_grade: HazardEvidenceGrade | None = None
    legal_reference: str
    result_reason: str
    review_note: str
    facilities: list[HazardFacility] = Field(default_factory=list)
    # 판정창(임계+버퍼) 밖·참고 반경(HAZARD_CONTEXT_RADIUS_M) 이내 시설.
    # 판정·집계·심사표에 불참하고 지도 표시 전용이다. 경계 미조회라 geometry 가 비어 있다.
    nearby_facilities: list[HazardFacility] = Field(default_factory=list)


class HazardSourceStatus(BaseModel):
    source_id: str
    label: str
    state: HazardSourceState
    retrieved_at: datetime
    as_of: datetime | None = None
    coverage_note: str
    geometry_note: str
    required_for: list[str] = Field(default_factory=list)


class HazardRuleBand(BaseModel):
    """규칙 기준거리만큼 신청 필지를 확장한 도형. 중심점 원을 대체한다."""

    threshold_m: int = Field(gt=0)
    ring: list[Coordinates] = Field(default_factory=list)


class HazardScoreItem(BaseModel):
    """신뢰도 점수의 한 항목. 왜 그 점수인지 설명한다."""

    label: str
    earned: int = Field(ge=0)
    maximum: int = Field(gt=0)
    note: str = ""


class HazardCategorySummary(BaseModel):
    """유해요소 한 종류(rulebook.CATEGORIES 중 하나)의 검토 결과."""

    key: str
    label: str
    rule_id: str
    rule_label: str
    # 적용 임계거리. 매트릭스에서 미적용이면 None.
    threshold_m: int | None = None
    status: HazardReviewStatus
    status_label: str
    # LH 회신문 §6-2 상태 어휘: applied(판정 적용) / partial(부분 적용) /
    # approximate(근사 적용) / manual·missing(판정 미적용) / negotiate(개별 협의)
    data_state: CategoryDataState
    # 근거 문서 번호(docs/hazards/H-*.md). 화면에서 항목 옆에 표시한다.
    doc_ref: str = ""
    # 구현/확정/검증 축. data_state 와는 별개다. 프런트가 배지 하나로 렌더한다.
    implementation_state: ImplementationState = "ready"
    # 위 상태의 한 줄 사유(무엇이 없어서 그런지). 배지 옆에 그대로 노출한다.
    implementation_note: str = ""
    # 협의에 따라 판정에서 제외된 종류인지(군부대·사격장). 리스트에는 보이되
    # 종합상태·status_counts 에는 참여하지 않는다. 프런트 "판정 제외" 배지용.
    judgment_excluded: bool = False
    # not_applicable 사유(매트릭스 미적용·지식산업센터 예외 등).
    not_applicable_reason: str = ""
    # 수기 확인이 필요한 종류인지(가목·화약류 저장소 등).
    manual_check_required: bool = False
    candidate_count: int = 0
    inside_threshold_count: int = 0
    nearest_distance_m: float | None = Field(default=None, ge=0)
    source_label: str = ""
    source_connected: bool = False
    # 이 종류의 판정에 실제로 붙은 원천 목록(공개 API·로컬 파일·데모). 심사표
    # 「데이터」 열이 이걸 그대로 칩으로 그려 API/로컬 판정을 한눈에 구분한다.
    # 미연결·수기·판정제외면 빈 리스트다.
    data_sources: list[HazardDataSource] = Field(default_factory=list)
    # 원천이 안 붙어 있을 때 무엇을 하면 붙는지. 화면의 연결 버튼이 이걸 그대로 쓴다.
    source_connect_kind: str = "none"
    source_connect_hint: str = ""
    source_connect_command: str = ""
    note: str = ""
    # 이 판정을 얼마나 믿을 수 있는지. 근거를 함께 내보내 블랙박스가 되지 않게 한다.
    confidence_score: int = Field(default=0, ge=0, le=100)
    score_breakdown: list[HazardScoreItem] = Field(default_factory=list)
    facilities: list[HazardFacility] = Field(default_factory=list)


class HazardPendingItem(BaseModel):
    """룰북 §8 미확정 사항. 화면에 그대로 노출한다."""

    item: str
    status: str


class HazardReviewResult(BaseModel):
    review_id: str
    created_at: datetime
    demo: bool
    site: HazardSite
    housing_type: HousingType
    application_type: ApplicationType
    rule_pack: HazardRulePack
    overall_status: HazardReviewStatus
    overall_label: str
    overall_summary: str
    status_counts: dict[str, int]
    data_completeness: int = Field(ge=0, le=100)
    boundary_coverage: int = Field(ge=0, le=100)
    source_freshness: int = Field(ge=0, le=100)
    findings: list[HazardFinding]
    categories: list[HazardCategorySummary] = Field(default_factory=list)
    rule_bands: list[HazardRuleBand] = Field(default_factory=list)
    sources: list[HazardSourceStatus]
    pending_items: list[HazardPendingItem] = Field(default_factory=list)
    source_snapshot_id: str
    calculation_note: str
    disclaimer: str


class HazardProgressItem(BaseModel):
    id: str
    label: str
    status: ProgressStatus = "pending"
    progress: int = Field(default=0, ge=0, le=100)
    count: int | None = None
    message: str = ""


class HazardJobStart(BaseModel):
    job_id: str


class HazardJobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    progress: int = Field(ge=0, le=100)
    stage: str = ""
    message: str = ""
    items: list[HazardProgressItem] = Field(default_factory=list)
    review_id: str | None = None
    result: HazardReviewResult | None = None
    error: str = ""


class HazardParcelResolveRequest(BaseModel):
    name: str
    address: str = ""
    coordinates: Coordinates


class HazardParcelResolveResponse(BaseModel):
    parcels: list[HazardParcel]
    provisional: bool = True
    note: str


class HazardApplicationCombo(BaseModel):
    """신청유형 조합 하나와 그 조합의 Rule별 적용 임계거리."""

    housing_type: HousingType
    housing_type_label: str
    application_type: ApplicationType
    application_type_label: str
    thresholds: dict[str, int | None]


class HazardApplicationTypesResponse(BaseModel):
    housing_types: dict[str, str]
    application_types: dict[str, str]
    rules: list[dict[str, str]]
    combos: list[HazardApplicationCombo]


class CadastralParcel(BaseModel):
    """지도에 깔 지적도 필지 한 건. 사업지 선택용이라 최소 정보만 담는다."""

    pnu: str
    jibun: str = ""
    address: str = ""
    area_m2: float = Field(default=0.0, ge=0)
    geometry: list[Coordinates] = Field(default_factory=list)


class CadastralParcelsResponse(BaseModel):
    parcels: list[CadastralParcel]
    truncated: bool = False
    note: str = ""
