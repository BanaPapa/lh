/**
 * LH 서류심사 결과 타입. `backend/app/screening/models.py` 의 pydantic 모델을
 * 필드 그대로(스네이크 케이스 유지) 옮긴 것이다. 백엔드가 바뀌면 여기도 같이 바뀐다.
 *
 * 사업지·유해요소 판정 결과는 유해시설 검토와 같은 모델을 쓰므로 새로 선언하지 않고
 * `../hazard-review/types` 에서 가져다 쓴다.
 */
import type {
  HazardApplicationType,
  HazardFacility,
  HazardHousingType,
  HazardReviewResult,
  HazardSite,
} from "../hazard-review/types";
import type { Coordinates, ProgressStatus } from "../types";

/** 심사 기여 방식. 부적격 사유에 오르는지를 가른다. */
export type ScreeningEffect = "blocking" | "advisory" | "not_applicable";

/** 1차 매입제외 항목 한 줄의 결과. */
export type ScreeningItemOutcome =
  | "fail"
  | "pass"
  | "review"
  | "not_applicable";

/** 종합·1차 판정값. */
export type ScreeningVerdict = "fail" | "pass" | "review";

/** 2차 시설군의 수집 상태. */
export type ScreeningGroupState = "connected" | "substituted" | "missing";

/** 2차 심사표 3종. 신청유형 5종이 이 3종으로 접힌다. */
export type ScreeningSheetKey = "common" | "youth" | "senior";

/** 원천 종류. api=공개 API(링크), local=로컬 납품 파일, demo=데모 데이터. */
export type ScreeningDataSourceKind = "api" | "local" | "demo";

/**
 * 항목 판정에 실제로 붙은 원천 하나. `backend` 의 `HazardDataSource` 와 같은 필드다.
 * api 는 제공기관·데이터셋 라벨과 엔드포인트 URL, local 은 라벨 "로컬"과 파일명(detail).
 */
export interface ScreeningDataSource {
  kind: ScreeningDataSourceKind;
  label: string;
  /** api 원천 링크. local·demo 는 빈 문자열. */
  url: string;
  /** api 는 데이터셋 슬러그, local 은 파일명 등 부연. */
  detail: string;
}

/** 1차 매입제외 항목 한 줄. 심사표의 「주거환경 저해시설」 체크박스에 대응한다. */
export interface ScreeningExclusionItem {
  key: string;
  label: string;
  rule_id: string;
  rule_label: string;
  /** 이 신청유형에 적용된 임계거리(m). 매트릭스에서 미적용이면 null. */
  threshold_m: number | null;
  effect: ScreeningEffect;
  outcome: ScreeningItemOutcome;
  outcome_label: string;
  /** 왜 그 결과인지 한 줄. 부적격이면 그대로 부적격 사유가 된다. */
  reason: string;
  /** 데이터·기준 상태 고지. 통과 처리한 항목은 여기에 사유가 들어간다. */
  note: string;
  /** 판정에 쓰지 않고 참고로만 보여주는 항목인지. */
  passthrough: boolean;
  candidate_count: number;
  inside_threshold_count: number;
  nearest_distance_m: number | null;
  measurement_method: string;
  legal_reference: string;
  source_label: string;
  /** 이 항목 판정에 실제로 붙은 원천(공개 API·로컬 파일·데모). 미연결이면 빈 배열. */
  data_sources: ScreeningDataSource[];
  facilities: HazardFacility[];
}

/**
 * 심사표 2항 매입제외 체크리스트 한 줄.
 * 15개 항목 중 좌표로 자동 판정하는 것은 「주거환경 저해시설」 하나뿐이다.
 */
export interface ScreeningChecklistEntry {
  key: string;
  label: string;
  mode: "automatic" | "manual";
  note: string;
  /** 자동 판정 항목만 채워진다. */
  outcome: ScreeningItemOutcome | null;
  outcome_label: string;
}

/** 1차 매입제외 판정. 한 항목이라도 해당하면 매입불가다. */
export interface ScreeningStageOne {
  verdict: ScreeningVerdict;
  verdict_label: string;
  summary: string;
  /** 부적격 사유. 화면에 그대로 나열한다. */
  reasons: string[];
  review_reasons: string[];
  /** 판정에 넣지 않고 통과 처리한 항목의 고지 문구. */
  passthrough_notes: string[];
  items: ScreeningExclusionItem[];
  counts: Record<string, number>;
  /** 심사표 2항 전 항목. 자동 판정 범위를 화면에 그대로 드러낸다. */
  checklist: ScreeningChecklistEntry[];
  checklist_note: string;
}

/** 시설의 문·출구 후보 한 곳(#7·#11). 기본은 가장 가까운 후보가 selected. */
export interface FrontDoorCandidate {
  label: string;
  coordinates: Coordinates;
  distance_m: number;
  selected: boolean;
}

/**
 * 수기 지정된 기준점 한 건(#8·#9). `GET/POST/DELETE /api/screening/front-doors`
 * 의 응답 모델(backend router FrontDoorView)을 필드 그대로 옮긴 것이다.
 */
export interface FrontDoorView {
  facility: string;
  /** 옛 프런트 호환 키. facility 와 같은 값이다. */
  university: string;
  label: string;
  pnu: string;
  lat: number | null;
  lng: number | null;
  /** "manual"(수기) | "auto_stop"(정류장 근사). */
  origin: string;
  source_label: string;
  /** 저장소 리비전. 지정이 바뀔 때마다 오른다. */
  revision: number;
}

/** 배점 근거가 된 시설 한 곳. */
export interface ScreeningFacilityHit {
  name: string;
  address: string;
  distance_m: number;
  group: string;
  group_label: string;
  source_label: string;
  /** 경계가 아니라 Point 로 잰 예외 시설이면 그 기준. */
  point_basis: string;
  measurement_method: string;
  /** 시설 측 측정 기준 단계: front_door_parcel | front_door_point | site_boundary | coordinate. */
  measurement_tier?: string;
  /** 정문 기준점 출처. 자동 채택이면 근사임이 드러난다. */
  front_door_source?: string;
  /** 통필지 폴백·정문 미지정 등 이 시설에 적용된 안전장치/폴백 고지. */
  front_door_notice?: string;
  /** 문·출구 후보 전체(#7·#11). 기본은 가장 가까운 후보가 selected. */
  front_door_candidates?: FrontDoorCandidate[];
  /** 시설 마커 좌표. */
  coordinates?: Coordinates | null;
  /** 사업지↔시설 최단거리 선분(1차 HazardFacility 와 같은 형태, #5). */
  nearest_boundary_point?: Coordinates | null;
  nearest_facility_point?: Coordinates | null;
  /** 거리를 잰 시설 필지 경계(site_boundary). 지도는 이 링을 그대로 칠한다. */
  facility_ring?: Coordinates[];
}

  /** 배점에 센 시설인가. 버스정류장 운행주기 미달·미확인은 false(목록에는 남는다). */
  counted?: boolean;
  /** 배점 인정/제외 사유(버스정류장: 15분당 평균 도착 버스 수와 노선 배차). */
  count_note?: string;
/** 시설군 하나의 수집 상태. 지정 원천과 실제 원천을 함께 보인다. */
export interface ScreeningGroupStatus {
  key: string;
  label: string;
  state: ScreeningGroupState;
  state_label: string;
  designated_source: string;
  actual_source: string;
  /** 지정 원천과 다르거나 요건을 못 지킬 때의 고지. 화면에 그대로 노출한다. */
  note: string;
  count: number;
  nearest_distance_m: number | null;
  point_exception: string;
  hits: ScreeningFacilityHit[];
}

/** 평가항목의 등급 한 줄. */
export interface ScreeningTier {
  points: number;
  condition: string;
  achieved: boolean;
  selected: boolean;
}

/** 평가항목 하나 — 대중교통 접근성 · 주거여건 · 교육여건 · 역세권 가점. */
export interface ScreeningCriterion {
  key: string;
  label: string;
  maximum: number;
  /** 확정 점수. 데이터 미확보로 등급을 확정할 수 없으면 null. */
  awarded: number | null;
  awarded_min: number;
  awarded_max: number;
  determined: boolean;
  tier_condition: string;
  tiers: ScreeningTier[];
  groups: ScreeningGroupStatus[];
  basis: string;
  note: string;
}

/** 심사표에는 있으나 공간분석으로 산정할 수 없는 항목. */
export interface ScreeningOutOfScopeItem {
  label: string;
  maximum: number;
  reason: string;
}

/** 2차 생활편의성 배점 — 심사표 3종 중 신청유형에 맞는 한 장. */
export interface ScreeningStageTwo {
  sheet_key: string;
  sheet_label: string;
  living_maximum: number;
  living_score: number | null;
  living_score_min: number;
  living_score_max: number;
  determined: boolean;
  criteria: ScreeningCriterion[];
  bonus: ScreeningCriterion | null;
  out_of_scope: ScreeningOutOfScopeItem[];
  out_of_scope_points: number;
  total_sheet_points: number;
  pass_threshold: number;
  /** 1차 부적격 상태에서 낸 참고 배점인지. */
  reference_only: boolean;
  note: string;
}

export interface ScreeningResult {
  screening_id: string;
  created_at: string;
  demo: boolean;
  site: HazardSite;
  housing_type: HazardHousingType;
  housing_type_label: string;
  application_type: HazardApplicationType;
  application_type_label: string;
  rule_pack_id: string;
  rule_pack_version: string;
  /** 종합 판정. 1차 결과가 그대로 올라온다. 2차는 합·불을 가르지 않는다. */
  verdict: ScreeningVerdict;
  verdict_label: string;
  verdict_summary: string;
  stage_one: ScreeningStageOne;
  stage_two: ScreeningStageTwo;
  /** 원본 유해요소 판정 결과. 지도·근거 화면이 그대로 재사용한다. */
  hazard_review: HazardReviewResult;
  calculation_note: string;
  disclaimer: string;
}

export interface ScreeningRequest {
  site: HazardSite;
  rule_pack_id: string;
  requested_by: string;
  /** 1차 부적격이어도 2차 배점을 함께 산정할지. */
  include_stage_two_on_fail: boolean;
}

export interface ScreeningProgressItem {
  id: string;
  label: string;
  status: ProgressStatus;
  progress: number;
  count: number | null;
  message: string;
}

export interface ScreeningJobStart {
  job_id: string;
}

export interface ScreeningJobStatus {
  job_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  progress: number;
  stage: string;
  message: string;
  items: ScreeningProgressItem[];
  screening_id: string | null;
  result: ScreeningResult | null;
  error: string;
}

/** 심사표 정본의 등급 한 줄. 결과와 달리 판정 상태가 붙지 않는다. */
export interface ScoresheetTier {
  points: number;
  condition: string;
}

export interface ScoresheetCriterion {
  key: string;
  label: string;
  maximum: number;
  groups: string[];
  tiers: ScoresheetTier[];
  basis: string;
}

export interface Scoresheet {
  key: string;
  label: string;
  living_maximum: number;
  criteria: ScoresheetCriterion[];
}

/** GET /api/screening/scoresheets 응답. 심사표 3종과 공통 항목을 담는다. */
export interface ScoresheetsResponse {
  sheets: Scoresheet[];
  bonus: ScoresheetCriterion | null;
  out_of_scope: ScreeningOutOfScopeItem[];
  total_sheet_points: number;
  pass_threshold: number;
}
