import { API_BASE_URL, apiRequest } from "../api-base";
import type { HazardApplicationType, HazardHousingType } from "../hazard-review/types";
import type { ScreeningResult } from "./types";

/** 올린 파일에서 읽은 사업지 한 줄. 좌표가 있으면 지오코딩을 건너뛴다. */
export interface BatchRowInput {
  id: string;
  address: string;
  lat: number | null;
  lng: number | null;
  pnu: string;
  housing_type: HazardHousingType | null;
  application_type: HazardApplicationType | null;
  /** 파일에 적힌 분류·신청유형 원문. */
  housing_label: string;
  application_label: string;
  /** 차수·접수일자·매도자명 — 결과에 그대로 실린다. */
  extras: Record<string, string>;
  /** 담당자가 확인할 점(분류 혼합·인식 불가·외 N필지 등). */
  warnings: string[];
}

export interface BatchParseResponse {
  file_name: string;
  sheet: string;
  header_row: number;
  columns: Record<string, string>;
  rows: BatchRowInput[];
  skipped: number;
  note: string;
}

export type BatchRowState = "queued" | "running" | "completed" | "failed" | "cancelled";

export interface BatchCriterionScore {
  key: string;
  label: string;
  awarded: number | null;
  awarded_min: number;
  awarded_max: number;
  maximum: number;
}

export interface BatchRowStatus {
  id: string;
  address: string;
  housing_type: HazardHousingType;
  application_type: HazardApplicationType;
  extras: Record<string, string>;
  status: BatchRowState;
  message: string;
  /** 진행률(0~100)과 지금 도는 단계. */
  progress: number;
  stage: string;
  error: string;
  screening_id: string | null;
  site_address: string;
  parcel_count: number;
  resolve_note: string;
  verdict: string;
  verdict_label: string;
  verdict_summary: string;
  living_score: number | null;
  living_score_min: number | null;
  living_score_max: number | null;
  living_maximum: number | null;
  determined: boolean;
  criteria: BatchCriterionScore[];
  bonus: BatchCriterionScore | null;
}

export interface BatchStatus {
  batch_id: string;
  status: "queued" | "running" | "completed" | "cancelled";
  created_at: string;
  housing_type: HazardHousingType;
  application_type: HazardApplicationType;
  total: number;
  done: number;
  /** 전체 진행률(0~100) — 건이 끝나기 전에도 움직인다. */
  progress: number;
  rows: BatchRowStatus[];
}

const CONNECTION_ERROR = "서류심사 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";

function request<T>(path: string, init?: RequestInit): Promise<T> {
  return apiRequest<T>(path, { ...init, connectionErrorMessage: CONNECTION_ERROR });
}

/** 파일은 multipart 로 보내야 하므로 JSON 헬퍼를 거치지 않는다. */
export async function parseBatchFile(file: File): Promise<BatchParseResponse> {
  const form = new FormData();
  form.append("file", file, file.name);
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/api/screening/batch/parse`, { method: "POST", body: form });
  } catch {
    throw new Error(CONNECTION_ERROR);
  }
  const body = (await response.json().catch(() => null)) as { detail?: unknown } | null;
  if (!response.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : "";
    throw new Error(detail || `파일을 읽지 못했습니다. (${response.status})`);
  }
  return body as unknown as BatchParseResponse;
}

export function startBatch(payload: {
  rows: BatchRowInput[];
  housing_type: HazardHousingType;
  application_type: HazardApplicationType;
  rule_pack_id: string;
}): Promise<{ batch_id: string }> {
  return request("/api/screening/batch", { method: "POST", body: JSON.stringify(payload) });
}

export function getBatch(batchId: string): Promise<BatchStatus> {
  return request(`/api/screening/batch/${encodeURIComponent(batchId)}`);
}

export function cancelBatch(batchId: string): Promise<BatchStatus> {
  return request(`/api/screening/batch/${encodeURIComponent(batchId)}/cancel`, { method: "POST" });
}

export function getBatchRowResult(batchId: string, rowId: string): Promise<ScreeningResult> {
  return request(
    `/api/screening/batch/${encodeURIComponent(batchId)}/rows/${encodeURIComponent(rowId)}/result`,
  );
}

export function batchTemplateUrl(): string {
  return `${API_BASE_URL}/api/screening/batch/template`;
}

export function batchExportUrl(batchId: string): string {
  return `${API_BASE_URL}/api/screening/batch/${encodeURIComponent(batchId)}/export`;
}
