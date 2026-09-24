"""LH 서류 심사 결과 모델.

1차 매입제외 판정과 2차 생활편의성 배점을 심사표 한 장의 형태로 합친다.
`hazard_review` 는 유해요소 판정 엔진이고, 여기는 그 결과를 심사 관점
(적격/부적격 · 사유 · 배점)으로 옮기는 표현 계층이다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.hazard_review.data_sources import HazardDataSource as ScreeningDataSource
from app.hazard_review.models import HazardFacility, HazardReviewResult, HazardSite
from app.hazard_review.rulebook import ApplicationType, HousingType
from app.models import Coordinates, ProgressStatus


# ---------------------------------------------------------------------------
# 1차 매입제외
# ---------------------------------------------------------------------------
# 심사 기여 방식. 사용자가 보는 "부적격 사유"에 오르는지를 가른다.
ScreeningEffect = Literal["blocking", "advisory", "not_applicable"]

# 항목 한 줄의 결과.
ItemOutcome = Literal["fail", "pass", "review", "not_applicable"]

ITEM_OUTCOME_LABELS: dict[str, str] = {
    "fail": "매입제외",
    "pass": "통과",
    "review": "검토 필요",
    "not_applicable": "해당 없음",
}


class ScreeningExclusionItem(BaseModel):
    """1차 매입제외 항목 한 줄. 심사표의 「주거환경 저해시설」 체크박스에 대응한다."""

    key: str
    label: str
    rule_id: str
    rule_label: str
    # 이 신청유형에 적용된 임계거리(m). 매트릭스에서 미적용이면 None.
    threshold_m: int | None = None
    effect: ScreeningEffect
    outcome: ItemOutcome
    outcome_label: str
    # 왜 그 결과인지 한 줄. 부적격이면 그대로 부적격 사유가 된다.
    reason: str
    # 데이터·기준 상태 고지. "통과 처리"한 항목은 여기에 사유가 들어간다.
    note: str = ""
    # 판정에 쓰지 않고 참고로만 보여주는 항목인지(공장·군부대 등).
    passthrough: bool = False
    candidate_count: int = 0
    inside_threshold_count: int = 0
    nearest_distance_m: float | None = Field(default=None, ge=0)
    measurement_method: str = ""
    legal_reference: str = ""
    source_label: str = ""
    # 이 항목 판정에 실제로 붙은 원천(공개 API·로컬 파일·데모). hazard_review 가
    # 채운 값을 그대로 실어 나른다. 심사표 「데이터」 열이 이걸 칩으로 그린다.
    data_sources: list[ScreeningDataSource] = Field(default_factory=list)
    facilities: list[HazardFacility] = Field(default_factory=list)


class ScreeningChecklistEntry(BaseModel):
    """심사표 2항 매입제외 체크리스트 한 줄.

    15개 항목 중 좌표로 자동 판정하는 것은 「주거환경 저해시설」 하나뿐이다.
    나머지를 화면에서 빼면 자동 판정 통과가 매입적격처럼 읽히므로 전 항목을
    심사표 순서 그대로 싣고, 자동 판정 대상이 아닌 항목은 별도 확인으로 둔다.
    """

    key: str
    label: str
    mode: Literal["automatic", "manual"]
    note: str = ""
    # 자동 판정 항목만 채운다. 수기 확인 항목은 None.
    outcome: ItemOutcome | None = None
    outcome_label: str


class ScreeningStageOne(BaseModel):
    """1차 매입제외 판정 — 한 항목이라도 해당하면 매입불가(심사표 2항)."""

    verdict: Literal["fail", "pass", "review"]
    verdict_label: str
    summary: str
    # 부적격 사유. 화면·출력물에 그대로 나열한다.
    reasons: list[str] = Field(default_factory=list)
    # 검토 필요로 남은 항목의 사유.
    review_reasons: list[str] = Field(default_factory=list)
    # 판정에 넣지 않고 통과 처리한 항목의 고지 문구.
    passthrough_notes: list[str] = Field(default_factory=list)
    items: list[ScreeningExclusionItem] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    # 심사표 2항 전 항목. 자동 판정 범위를 화면에서 그대로 드러내기 위한 것이다.
    checklist: list[ScreeningChecklistEntry] = Field(default_factory=list)
    checklist_note: str = ""


# ---------------------------------------------------------------------------
# 2차 생활편의성
# ---------------------------------------------------------------------------
GroupState = Literal["connected", "substituted", "missing"]

GROUP_STATE_LABELS: dict[str, str] = {
    "connected": "연결됨",
    "substituted": "대체 원천",
    "missing": "데이터 미확보",
}


class FrontDoorCandidate(BaseModel):
    """시설의 문·출구 후보 한 곳(#7·#11).

    정문/후문/동문 등 여러 문이나 여러 출구가 있을 때 전부 싣고, 기본은 사업지에
    가장 가까운 후보(수기 지정이 있으면 그것)를 selected=True 로 표시한다. 담당자가
    지도에서 다른 문을 고를 수 있는 근거 데이터다. 후보가 1개뿐이면 그 1개만 싣는다.
    """

    label: str
    coordinates: Coordinates
    distance_m: float = Field(ge=0)
    selected: bool = False


class ScreeningFacilityHit(BaseModel):
    """배점 근거가 된 시설 한 곳."""

    name: str
    address: str = ""
    distance_m: float = Field(ge=0)
    group: str
    group_label: str
    source_label: str = ""
    # 경계가 아니라 Point 로 잰 예외 시설이면 그 기준을 적는다.
    point_basis: str = ""
    measurement_method: str = ""
    # 시설 측 측정 기준 단계(국장님 §3-2 3단):
    #   "front_door_parcel" | "front_door_point" | "site_boundary" | "coordinate"
    measurement_tier: str = ""
    # 정문 기준점 출처. 자동 채택이면 근사임이 드러난다(§3-3).
    front_door_source: str = ""
    # 통필지 폴백·정문 미지정 등 이 시설에 적용된 안전장치/폴백 고지.
    front_door_notice: str = ""
    # 문·출구 후보 전체(#7·#11). 기본은 가장 가까운 후보가 selected=True.
    front_door_candidates: list[FrontDoorCandidate] = Field(default_factory=list)
    # 배점에 센 시설인가. 버스정류장 운행주기 미달·미확인은 False(목록에는 남는다).
    counted: bool = True
    # 배점 인정/제외 사유(버스정류장: 15분당 평균 도착 버스 수와 노선 배차).
    count_note: str = ""
    # 시설 마커 좌표(지도 표시용).
    coordinates: Coordinates | None = None
    # 사업지↔시설 최단거리 선분(1차 HazardFacility 와 같은 형태). 프런트가 같은
    # 그리기 코드를 재사용한다. boundary→facility 두 좌표로 선을 긋는다.
    #   nearest_boundary_point: 사업지 대지경계 위의 최단점(경계 없으면 None → 주소점)
    #   nearest_facility_point: 거리를 만든 시설 측 기준점(정문·출구·좌표)
    nearest_boundary_point: Coordinates | None = None
    nearest_facility_point: Coordinates | None = None
    # 거리를 잰 시설 필지 경계(site_boundary). 지도가 재계산하지 않고 이 링을 칠한다.
    facility_ring: list[Coordinates] = Field(default_factory=list)


class ScreeningGroupStatus(BaseModel):
    """시설군 하나의 수집 상태. 심사표가 지정한 원천과 실제 원천을 함께 보인다."""

    key: str
    label: str
    state: GroupState
    state_label: str
    designated_source: str = ""
    actual_source: str = ""
    # 지정 원천과 다르거나 요건을 못 지킬 때의 고지. 화면에 그대로 노출한다.
    note: str = ""
    count: int = 0
    nearest_distance_m: float | None = Field(default=None, ge=0)
    point_exception: str = ""
    # 대학교 정문 기준점 고지(정문 미지정·통필지 폴백 등). 조용히 좌표로 넘어가지
    # 않도록 시설군 헤더에도 드러낸다(§3-3).
    front_door_notice: str = ""
    hits: list[ScreeningFacilityHit] = Field(default_factory=list)


class ScreeningRequirement(BaseModel):
    """등급 조건을 이루는 요건 하나와 그 판정 근거. 점수가 왜 나왔는지 보이는 단위다."""

    text: str
    # 원천 미확보로 충족 여부를 가릴 수 없으면 None.
    met: bool | None
    # 「전주새연초등학교 451m」처럼 실제로 잰 최근접 시설.
    evidence: str = ""


class ScreeningTier(BaseModel):
    """평가항목의 등급 한 줄. 화면에 등급표를 그대로 그린다."""

    points: int
    condition: str
    achieved: bool = False
    selected: bool = False
    requirements: list[ScreeningRequirement] = Field(default_factory=list)


class ScreeningCriterion(BaseModel):
    """평가항목 하나 — 대중교통 접근성 · 주거여건 · 교육여건 · 역세권 가점."""

    key: str
    label: str
    maximum: int
    # 확정된 점수. 데이터 미확보로 등급을 확정할 수 없으면 None.
    awarded: int | None = None
    # 미확보 시설군을 최악·최선으로 가정했을 때의 점수 범위.
    awarded_min: int
    awarded_max: int
    determined: bool
    # 채택된 등급의 조건 문장. 미확정이면 범위를 만든 두 등급을 함께 적는다.
    tier_condition: str = ""
    tiers: list[ScreeningTier] = Field(default_factory=list)
    groups: list[ScreeningGroupStatus] = Field(default_factory=list)
    basis: str = ""
    note: str = ""


class ScreeningOutOfScopeItem(BaseModel):
    """심사표에는 있으나 공간분석으로 산정할 수 없는 항목."""

    label: str
    maximum: int
    reason: str


class ScreeningStageTwo(BaseModel):
    """2차 생활편의성 배점 — 심사표 3종 중 신청유형에 맞는 한 장."""

    sheet_key: str
    sheet_label: str
    living_maximum: int
    living_score: int | None = None
    living_score_min: int
    living_score_max: int
    determined: bool
    criteria: list[ScreeningCriterion] = Field(default_factory=list)
    bonus: ScreeningCriterion | None = None
    out_of_scope: list[ScreeningOutOfScopeItem] = Field(default_factory=list)
    out_of_scope_points: int = 0
    total_sheet_points: int = 100
    pass_threshold: int = 70
    # 1차 부적격 상태에서 낸 참고 배점인지. 룰북 §12 확인대기 항목이다.
    reference_only: bool = False
    note: str = ""


# ---------------------------------------------------------------------------
# 종합
# ---------------------------------------------------------------------------
class ScreeningResult(BaseModel):
    screening_id: str
    created_at: datetime
    demo: bool = False
    site: HazardSite
    housing_type: HousingType
    housing_type_label: str
    application_type: ApplicationType
    application_type_label: str
    rule_pack_id: str
    rule_pack_version: str
    # 종합 판정 — 1차 결과가 그대로 올라온다. 2차는 점수일 뿐 합·불을 가르지 않는다.
    verdict: Literal["fail", "pass", "review"]
    verdict_label: str
    verdict_summary: str
    stage_one: ScreeningStageOne
    stage_two: ScreeningStageTwo
    # 원본 유해요소 판정 결과. 지도·근거 화면이 그대로 재사용한다.
    hazard_review: HazardReviewResult
    calculation_note: str = ""
    disclaimer: str = ""


class ScreeningRequest(BaseModel):
    site: HazardSite
    rule_pack_id: str = ""
    requested_by: str = "prototype-user"
    # 1차 부적격이어도 2차 배점을 함께 산정할지. 기본은 참고용으로 함께 낸다.
    include_stage_two_on_fail: bool = True


class ScreeningProgressStep(BaseModel):
    """단계 안의 하위 작업 한 줄(예: 1차 조회 안의 규칙별 조회). 진행 화면이 펼쳐 보인다."""

    id: str
    label: str
    status: ProgressStatus = "pending"
    progress: int = Field(default=0, ge=0, le=100)
    count: int | None = None
    message: str = ""


class ScreeningProgressItem(BaseModel):
    id: str
    label: str
    status: ProgressStatus = "pending"
    progress: int = Field(default=0, ge=0, le=100)
    count: int | None = None
    message: str = ""
    # 하위 작업. 콜백 id 가 "부모/자식" 꼴이면 라우터가 여기로 모은다.
    steps: list[ScreeningProgressStep] = Field(default_factory=list)


class ScreeningJobStart(BaseModel):
    job_id: str


class ScreeningJobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    progress: int = Field(ge=0, le=100)
    stage: str = ""
    message: str = ""
    items: list[ScreeningProgressItem] = Field(default_factory=list)
    screening_id: str | None = None
    result: ScreeningResult | None = None
    error: str = ""
