/**
 * 결과 파일 내려받기. 백엔드가 파일을 만들어 돌려주고, 여기서는 받은 파일을 저장시킨다.
 * - 심사표 한 건: Excel 4시트(종합요약·1차 상세·2차 상세·데이터 스냅샷)
 * - 일괄 심사: Excel(결과표 + 건별 심사표) · PDF(A4 세로, 1쪽 결과표 + 건별 심사표)
 * 담당자 판단·메모는 브라우저에만 있으므로 요청에 실어 보낸다.
 */
import { API_BASE_URL } from "../api-base";
import type { BatchStatus } from "./batchApi";
import type { JudgementMap } from "./judgements";
import type { ScreeningResult } from "./types";

const CONNECTION_ERROR = "서류심사 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";

/** Content-Disposition 에서 파일명(RFC 5987 우선)을 꺼낸다. */
function filenameFrom(header: string | null, fallback: string): string {
  if (!header) return fallback;
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1]);
    } catch {
      // 잘못 인코딩된 이름 — 아래 일반 이름으로.
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain ? plain[1] : fallback;
}

/** 받은 파일을 브라우저 저장(다운로드)으로 넘긴다. */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // 다운로드가 시작될 시간을 준 뒤 해제한다.
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

async function postForFile(
  path: string,
  body: unknown,
  fallbackName: string,
  kind: string,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new Error(CONNECTION_ERROR);
  }
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as { detail?: unknown } | null;
    const detail = payload && typeof payload.detail === "string" ? payload.detail : "";
    if (response.status === 404) {
      // 서버가 새로 켜져 일괄 심사 결과가 메모리에서 사라진 경우다. 브라우저에 보관한
      // 결과가 있으면 호출한 쪽이 그것으로 다시 만든다.
      const gone = new Error(
        `${detail || "일괄 심사 결과를 찾을 수 없습니다."} 서버가 다시 시작되면 결과가 사라지므로, 심사가 끝난 뒤 바로 내려받거나 다시 심사해 주세요.`,
      );
      gone.name = "BatchGoneError";
      throw gone;
    }
    throw new Error(detail || `${kind} 을 만들지 못했습니다. (${response.status})`);
  }
  const blob = await response.blob();
  if (blob.size === 0) throw new Error(`${kind} 파일이 비어 있습니다. 다시 시도해 주세요.`);
  saveBlob(blob, filenameFrom(response.headers.get("Content-Disposition"), fallbackName));
}

/** 심사표 한 건의 결과 Excel. */
export function downloadScreeningXlsx(
  result: ScreeningResult,
  judgements: JudgementMap,
): Promise<void> {
  return postForFile(
    "/api/screening/export.xlsx",
    { sites: [{ result, judgements }] },
    "LH_screening.xlsx",
    "Excel",
  );
}

/**
 * 서버에 일괄 심사가 남아 있지 않을 때(배포 서버가 새로 켜짐·오래되어 지움) 쓰는 대비책.
 * 브라우저가 보관해 둔 결과표와 건별 결과로 같은 파일을 만든다.
 */
export interface BatchExportFallback {
  batch: BatchStatus;
  results: Record<string, ScreeningResult>;
}

/** 내려받기에 쓰지 않는 유해요소 원본·지도용 경계 좌표를 빼 요청을 가볍게 한다(건당 수백 KB → 수십 KB). */
function trimForExport(result: ScreeningResult): unknown {
  const stripHit = <T extends object>(hit: T) => ({
    ...hit,
    facility_ring: [],
    front_door_candidates: [],
    nearest_boundary_point: null,
    nearest_facility_point: null,
  });
  return {
    ...result,
    hazard_review: null,
    site: {
      ...result.site,
      parcels: result.site.parcels.map((parcel) => ({ ...parcel, geometry: [] })),
    },
    stage_one: {
      ...result.stage_one,
      items: result.stage_one.items.map((item) => ({
        ...item,
        facilities: item.facilities.map((facility) => ({
          ...facility,
          geometry: [],
          occupied_parcels: [],
          nearest_boundary_point: null,
          nearest_facility_point: null,
        })),
      })),
    },
    stage_two: {
      ...result.stage_two,
      criteria: result.stage_two.criteria.map((criterion) => ({
        ...criterion,
        groups: criterion.groups.map((group) => ({ ...group, hits: group.hits.map(stripHit) })),
      })),
      bonus: result.stage_two.bonus
        ? {
            ...result.stage_two.bonus,
            groups: result.stage_two.bonus.groups.map((group) => ({
              ...group,
              hits: group.hits.map(stripHit),
            })),
          }
        : null,
    },
  };
}

async function downloadBatchFile(
  kind: "xlsx" | "pdf",
  batchId: string,
  judgements: Record<string, JudgementMap>,
  fallback?: BatchExportFallback,
): Promise<void> {
  const label = kind === "xlsx" ? "Excel" : "PDF";
  const fallbackName = `LH_batch.${kind}`;
  try {
    await postForFile(
      `/api/screening/batch/${encodeURIComponent(batchId)}/export.${kind}`,
      { judgements },
      fallbackName,
      label,
    );
  } catch (cause) {
    const hasResults = fallback && Object.keys(fallback.results).length > 0;
    if (!(cause instanceof Error) || cause.name !== "BatchGoneError" || !hasResults) throw cause;
    // 서버가 결과를 잃었다 — 브라우저가 보관한 결과로 같은 파일을 만든다.
    const results = Object.fromEntries(
      Object.entries(fallback.results).map(([id, result]) => [id, trimForExport(result)]),
    );
    await postForFile(
      `/api/screening/batch/export.${kind}`,
      { batch: fallback.batch, results, judgements },
      fallbackName,
      label,
    );
  }
}

/** 일괄 심사 결과 Excel(결과표 + 건별 심사표). 결과는 서버에 있으므로 담당자 판단만 보낸다. */
export function downloadBatchXlsx(
  batchId: string,
  judgements: Record<string, JudgementMap>,
  fallback?: BatchExportFallback,
): Promise<void> {
  return downloadBatchFile("xlsx", batchId, judgements, fallback);
}

/** 일괄 심사 결과 PDF(A4 세로 — 1쪽 결과표, 2쪽부터 건별 심사표). */
export function downloadBatchPdf(
  batchId: string,
  judgements: Record<string, JudgementMap>,
  fallback?: BatchExportFallback,
): Promise<void> {
  return downloadBatchFile("pdf", batchId, judgements, fallback);
}
