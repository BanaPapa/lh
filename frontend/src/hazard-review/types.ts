import type { Coordinates, ProgressStatus } from "../types";

/** 룰북 §4 판정 상태값 6종. 기존 5종은 폐기했다. */
export type HazardReviewStatus =
  | "exclusion_match"
  | "no_conflict_in_snapshot"
  | "review_required"
  | "dataset_missing"
  | "geometry_missing"
  | "not_applicable";

export type HazardSourceState =
  | "connected"
  | "partial"
  | "unavailable"
  | "failed";

export type HazardEvidenceGrade = "A" | "B" | "C" | "D";

/** 룰북 §1 신청 유형. 매트릭스가 이 두 값으로 임계거리를 정한다. */
export type HazardHousingType = "house" | "officetel";
export type HazardApplicationType =
  | "general"
  | "multi_child"
  | "newlywed"
  | "youth"
  | "senior";

/**
 * 항목 상태 — LH 회신문 §6-2 어휘(docs/hazards INDEX §5).
 * applied 판정 적용 · partial 부분 적용 · approximate 근사 적용 ·
 * manual/missing 판정 미적용(별도 수기 확인) · negotiate 개별 협의
 */
export type HazardCategoryDataState =
  | "applied"
  | "partial"
  | "approximate"
  | "missing"
  | "manual"
  | "negotiate";

/**
 * 구현/확정/검증 축. data_state(원천 상태)와는 별개 축이다.
 * - ready: 어댑터·파이프라인이 갖춰져 판정에 참여
 * - not_implemented: 미구현 (어댑터/파이프라인 없음)
 * - unconfirmed: 미확정 (LH/팀 확인 대기)
 * - needs_verification: 검증필요 (등록됐으나 실응답 미확인)
 */
export type HazardImplementationState =
  | "ready"
  | "not_implemented"
  | "unconfirmed"
  | "needs_verification";

export interface HazardParcel {
  parcel_id: string;
  pnu: string;
  address: string;
  area_m2: number | null;
  geometry: Coordinates[];
  geometry_source:
    | "official_polygon"
    | "parcel_polygon"
    | "provisional_polygon"
    | "point_fallback";
  geometry_note: string;
}

/** 판정에 넣을 사업지. 신청유형이 필수다(누락 시 백엔드 422). */
export interface HazardSite {
  name: string;
  address: string;
  coordinates: Coordinates;
  housing_type: HazardHousingType;
  application_type: HazardApplicationType;
  parcels: HazardParcel[];
}

export interface HazardRuleDefinition {
  rule_id: string;
  label: string;
  matrix_column: string;
  legal_reference: string;
  /** 신청유형 10조합의 임계거리표. 키는 "housing:application". */
  thresholds: Record<string, number | null>;
}

export interface HazardRulePack {
  id: string;
  title: string;
  version: string;
  effective_from: string;
  status: "draft" | "approved" | "retired";
  source_document_url: string;
  note: string;
  rules: HazardRuleDefinition[];
}

export interface HazardFacility {
  facility_id: string;
  facility_type: string;
  facility_type_label: string;
  name: string;
  coordinates: Coordinates;
  distance_m: number;
  /** distance_m 을 만든 대지경계 위의 지점. 없으면 주소점 기준 거리다. */
  nearest_boundary_point?: Coordinates | null;
  /** 시설 쪽 지적도 필지. 있으면 경계 대 경계로 잰 거리다. */
  geometry?: Coordinates[];
  nearest_facility_point?: Coordinates | null;
  parcel_pnu?: string;
  address: string;
  road_address: string;
  business_status: string;
  provider: string;
  source_label: string;
  source_record_id: string;
  source_url: string;
  source_as_of: string;
  geometry_type: "point" | "polygon";
  geometry_quality: HazardEvidenceGrade;
  geometry_note: string;
  classification_note: string;
  /** 용도지역(지적편집도) — 석유대체연료 판매업에만 붙는다 (LH 확정 2026-09-11 #5). */
  zoning_name?: string;
  zoning_class?: "residential" | "non_residential" | "unknown" | "";
  zoning_source?: string;
  metadata: Record<string, unknown>;
}

export interface HazardFinding {
  finding_id: string;
  rule_id: string;
  label: string;
  status: HazardReviewStatus;
  status_label: string;
  /** 해당 신청유형에 적용된 임계거리. 미적용이면 null. */
  threshold_m: number | null;
  measured_distance_m: number | null;
  measurement_method: string;
  evidence_grade: HazardEvidenceGrade | null;
  legal_reference: string;
  result_reason: string;
  review_note: string;
  facilities: HazardFacility[];
  /** 판정창(임계+버퍼) 밖·참고 반경 이내 시설. 판정·집계에 불참하고 지도 표시 전용. */
  nearby_facilities: HazardFacility[];
}

export interface HazardSourceStatus {
  source_id: string;
  label: string;
  state: HazardSourceState;
  retrieved_at: string;
  as_of: string | null;
  coverage_note: string;
  geometry_note: string;
  required_for: string[];
}

export interface HazardRuleBand {
  threshold_m: number;
  ring: Coordinates[];
}

/** 룰북 §8 미확정 사항. 화면에 그대로 노출한다. */
export interface HazardPendingItem {
  item: string;
  status: string;
}

export interface HazardReviewResult {
  review_id: string;
  created_at: string;
  demo: boolean;
  site: HazardSite;
  housing_type: HazardHousingType;
  application_type: HazardApplicationType;
  rule_pack: HazardRulePack;
  overall_status: HazardReviewStatus;
  overall_label: string;
  overall_summary: string;
  status_counts: Record<string, number>;
  data_completeness: number;
  boundary_coverage: number;
  source_freshness: number;
  findings: HazardFinding[];
  categories: HazardCategorySummary[];
  rule_bands: HazardRuleBand[];
  sources: HazardSourceStatus[];
  pending_items: HazardPendingItem[];
  source_snapshot_id: string;
  calculation_note: string;
  disclaimer: string;
}

export interface HazardReviewRequest {
  site: HazardSite;
  rule_pack_id: string;
  requested_by: string;
}

export interface HazardProgressItem {
  id: string;
  label: string;
  status: ProgressStatus;
  progress: number;
  count: number | null;
  message: string;
}

export interface HazardJobStart {
  job_id: string;
}

export interface HazardJobStatus {
  job_id: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  progress: number;
  stage: string;
  message: string;
  items: HazardProgressItem[];
  review_id: string | null;
  result: HazardReviewResult | null;
  error: string;
}

export interface HazardParcelResolveResponse {
  parcels: HazardParcel[];
  provisional: boolean;
  note: string;
}

/** 지도에 배경으로 까는 지적도 필지. 사업지 선택 대상이다. */
export interface CadastralParcel {
  pnu: string;
  jibun: string;
  address: string;
  area_m2: number;
  geometry: Coordinates[];
}

export interface CadastralParcelsResponse {
  parcels: CadastralParcel[];
  truncated: boolean;
  note: string;
}

/** 유해요소 한 종류(rulebook.CATEGORIES 중 하나)의 검토 결과. */
export interface HazardCategorySummary {
  key: string;
  label: string;
  rule_id: string;
  rule_label: string;
  /** 근거 문서 번호(docs/hazards/H-*.md). 예: "H-02-가". */
  doc_ref: string;
  /** 적용 임계거리. 매트릭스에서 미적용이면 null. */
  threshold_m: number | null;
  status: HazardReviewStatus;
  status_label: string;
  /** LH 회신문 §6-2 상태 어휘. */
  data_state: HazardCategoryDataState;
  /** 구현/확정/검증 축. data_state 와 별개. 배지 하나로 렌더한다. */
  implementation_state: HazardImplementationState;
  /** 위 상태의 한 줄 사유(무엇이 없어서 그런지). */
  implementation_note: string;
  /** 협의에 따라 판정 제외된 종류인지(군부대·사격장). */
  judgment_excluded: boolean;
  /** not_applicable 사유(매트릭스 미적용·지식산업센터 예외 등). */
  not_applicable_reason: string;
  /** 수기 확인이 필요한 종류인지(가목·화약류 저장소 등). */
  manual_check_required: boolean;
  candidate_count: number;
  inside_threshold_count: number;
  nearest_distance_m: number | null;
  source_label: string;
  source_connected: boolean;
  /** 원천이 안 붙어 있을 때 무엇을 하면 붙는지. 연결 버튼이 그대로 쓴다. */
  source_connect_kind: string;
  source_connect_hint: string;
  /** 이 종류가 담당하는 시설 유형(HazardFacility.facility_type). */
  facility_types?: string[];
  source_connect_command: string;
  note: string;
  confidence_score: number;
  score_breakdown: HazardScoreItem[];
  facilities: HazardFacility[];
}

/** 신뢰도 점수의 한 항목. 왜 그 점수인지 설명한다. */
export interface HazardScoreItem {
  label: string;
  earned: number;
  maximum: number;
  note: string;
}

/** 신청유형 조합 하나와 그 조합의 Rule별 적용 임계거리. */
export interface HazardApplicationCombo {
  housing_type: HazardHousingType;
  housing_type_label: string;
  application_type: HazardApplicationType;
  application_type_label: string;
  thresholds: Record<string, number | null>;
}

/** GET /application-types 응답. 주택×신청유형 매트릭스를 담는다. */
export interface HazardApplicationTypesResponse {
  housing_types: Record<string, string>;
  application_types: Record<string, string>;
  rules: { rule_id: string; label: string; matrix_column: string }[];
  combos: HazardApplicationCombo[];
}
