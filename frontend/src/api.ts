import type { GeocodeResponse } from "./types";
import { apiRequest } from "./api-base";

function request<T>(path: string, init?: RequestInit): Promise<T> {
  return apiRequest<T>(path, {
    ...init,
    connectionErrorMessage:
      "심사 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
  });
}

export function searchAddress(query: string): Promise<GeocodeResponse> {
  return request<GeocodeResponse>(
    `/api/geocode?query=${encodeURIComponent(query.trim())}`,
  );
}

/** 서버 비밀키 한 개의 상태. 원문은 내려오지 않고 마스킹 힌트만 온다. */
export interface ServerKeyStatus {
  key: string;
  label: string;
  description: string;
  configured: boolean;
  /** `****abcd` 또는 빈 문자열. */
  hint: string;
  scope: string;
  /** 발급처 이름. 없으면 빈 문자열(링크를 숨긴다). */
  issuer_name: string;
  /** 발급처 URL. 없으면 빈 문자열. */
  issuer_url: string;
}

export interface KeysStatusResponse {
  keys: ServerKeyStatus[];
  demo_mode: boolean;
}

/**
 * 서버 비밀키 갱신 요청.
 * - 필드 미포함 / 빈 문자열: 변경 안 함
 * - null: 명시적 삭제
 * - 그 외 값: 설정
 */
export interface KeysUpdatePayload {
  kakao_rest_api_key?: string | null;
  tago_service_key?: string | null;
  public_data_service_key?: string | null;
  naver_search_client_id?: string | null;
  naver_search_client_secret?: string | null;
  vworld_api_key?: string | null;
  opinet_api_key?: string | null;
  safemap_api_key?: string | null;
  seoul_open_data_key?: string | null;
  gg_open_api_key?: string | null;
  demo_mode?: boolean;
}

/** 외부 API 연결 한 줄. 키 유무(configured)와 실제 응답(state) 을 구분한다. */
export interface ConnectionStatus {
  id: string;
  label: string;
  purpose: string;
  key_name: string;
  configured: boolean;
  /** missing_key | ready | ok | failed */
  state: "missing_key" | "ready" | "ok" | "failed";
  detail: string;
  checked_at: number | null;
  issuer_name: string;
  issuer_url: string;
}

export interface ConnectionsResponse {
  connections: ConnectionStatus[];
  demo_mode: boolean;
}

export function getConnections(): Promise<ConnectionsResponse> {
  return request<ConnectionsResponse>("/api/settings/connections");
}

/** 키가 있는 원천을 실제로 호출해 본다. ids 를 주면 그 원천만. */
export function checkConnections(ids?: string[]): Promise<ConnectionsResponse> {
  return request<ConnectionsResponse>("/api/settings/connections/check", {
    method: "POST",
    body: JSON.stringify({ ids: ids ?? null }),
  });
}

export interface HealthResponse {
  status: string;
  demo_mode: boolean;
  kakao_configured: boolean;
  tago_configured: boolean;
  public_data_configured: boolean;
  naver_search_configured: boolean;
}

export function getSettingsKeys(): Promise<KeysStatusResponse> {
  return request<KeysStatusResponse>("/api/settings/keys");
}

export function updateSettingsKeys(
  payload: KeysUpdatePayload,
): Promise<KeysStatusResponse> {
  return request<KeysStatusResponse>("/api/settings/keys", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/api/health");
}

// ── 관리자 「기준 편집」 ───────────────────────────────────────────────
export interface ExcludedFacility {
  name: string;
  reason: string;
}

export interface RulesConfig {
  stage1_thresholds: Record<string, number | null>;
  relaxed_2027: boolean;
  excluded_facilities: ExcludedFacility[];
  options: Record<string, boolean>;
  updated_at: string;
}

/** 관리자 「판정 옵션」 스위치 하나. 기본값은 LH 기준(내부망 앱)과 같게 둔 쪽이다. */
export interface RuleOptionDef {
  key: string;
  label: string;
  description: string;
  default: boolean;
  group: string;
}

export interface RulesMatrixCell {
  key: string;
  housing_type: string;
  application_type: string;
  column: string;
  rule_id: string;
  default: number | null;
  value: number | null;
  overridden: boolean;
}

export interface RulesResponse {
  config: RulesConfig;
  rules: { rule_id: string; label: string; column: string }[];
  combos: {
    housing_type: string;
    housing_label: string;
    application_type: string;
    application_label: string;
  }[];
  cells: RulesMatrixCell[];
  default_pass_threshold: number;
  relaxed_pass_threshold: number;
  option_defs: RuleOptionDef[];
  option_values: Record<string, boolean>;
}

export interface RulesUpdatePayload {
  stage1_thresholds: Record<string, number | null>;
  relaxed_2027: boolean;
  excluded_facilities: ExcludedFacility[];
  options: Record<string, boolean>;
}

/** LH 개별 맞춤 — 공공 API 자료를 LH 데이터셋 기준에 일부러 맞춘 시설 한 곳(읽기 전용). */
export interface LhAlignmentEvidence {
  site: string;
  lh_distance_m: number | null;
  before_m: number | null;
  after_m: number | null;
  note: string;
}

export interface LhAlignmentEntry {
  id: string;
  scope: "amenity" | "hazard" | string;
  name: string;
  aliases: string[];
  group: string;
  group_label: string;
  action: string;
  action_label: string;
  summary: string;
  lh_name: string;
  lh_pnu: string;
  lh_lat: number | null;
  lh_lng: number | null;
  evidence: LhAlignmentEvidence[];
  reason: string;
  source: string;
  date: string;
}

export interface LhAlignmentsResponse {
  description: string;
  fingerprint: string;
  entries: LhAlignmentEntry[];
}

export function getLhAlignments(): Promise<LhAlignmentsResponse> {
  return request<LhAlignmentsResponse>("/api/settings/lh-alignments");
}

export function getRules(): Promise<RulesResponse> {
  return request<RulesResponse>("/api/settings/rules");
}

export function updateRules(payload: RulesUpdatePayload): Promise<RulesResponse> {
  return request<RulesResponse>("/api/settings/rules", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}
