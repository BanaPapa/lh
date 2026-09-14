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
