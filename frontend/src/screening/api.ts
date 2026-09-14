import type {
  FrontDoorView,
  ScoresheetsResponse,
  ScreeningJobStart,
  ScreeningJobStatus,
  ScreeningRequest,
} from "./types";
import { apiRequest } from "../api-base";

function request<T>(path: string, init?: RequestInit): Promise<T> {
  return apiRequest<T>(path, {
    ...init,
    connectionErrorMessage:
      "서류심사 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
  });
}

export function startScreeningJob(
  payload: ScreeningRequest,
): Promise<ScreeningJobStart> {
  return request<ScreeningJobStart>("/api/screening/jobs", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getScreeningJob(jobId: string): Promise<ScreeningJobStatus> {
  return request<ScreeningJobStatus>(
    `/api/screening/jobs/${encodeURIComponent(jobId)}`,
  );
}

export function cancelScreeningJob(jobId: string): Promise<ScreeningJobStatus> {
  return request<ScreeningJobStatus>(
    `/api/screening/jobs/${encodeURIComponent(jobId)}/cancel`,
    { method: "POST" },
  );
}

/** 2차 생활편의성 심사표 3종의 등급표 정본을 가져온다. */
export function fetchScoresheets(): Promise<ScoresheetsResponse> {
  return request<ScoresheetsResponse>("/api/screening/scoresheets");
}

/** 수기 지정된 기준점(문·필지) 목록을 가져온다. */
export function listFrontDoors(): Promise<FrontDoorView[]> {
  return request<FrontDoorView[]>("/api/screening/front-doors");
}

/**
 * 시설의 기준점을 수기 지정한다(#8·#9). PNU(지적 필지) 또는 좌표 중 하나는
 * 반드시 있어야 한다(둘 다 없으면 백엔드 422). 지정은 다음 심사부터 반영된다.
 */
export function designateFrontDoor(payload: {
  facility: string;
  pnu?: string;
  lat?: number;
  lng?: number;
  source_label?: string;
}): Promise<FrontDoorView> {
  return request<FrontDoorView>("/api/screening/front-doors", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** 시설의 수기 지정을 해제한다. 응답은 남은 지정 목록이다. */
export function removeFrontDoor(facility: string): Promise<FrontDoorView[]> {
  return request<FrontDoorView[]>(
    `/api/screening/front-doors/${encodeURIComponent(facility)}`,
    { method: "DELETE" },
  );
}
