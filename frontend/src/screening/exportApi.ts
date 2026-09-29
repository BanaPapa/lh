/**
 * 결과 Excel 내려받기. 백엔드가 openpyxl 로 4시트(종합요약·1차 상세·2차 상세·
 * 데이터 스냅샷)를 만들어 돌려주고, 여기서는 받은 파일을 저장시킨다.
 * 담당자 판단·메모는 브라우저에만 있으므로 요청에 실어 보낸다.
 */
import { API_BASE_URL } from "../api-base";
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

async function postForXlsx(path: string, body: unknown, fallbackName: string): Promise<void> {
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
    throw new Error(detail || `Excel 을 만들지 못했습니다. (${response.status})`);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filenameFrom(response.headers.get("Content-Disposition"), fallbackName);
  document.body.appendChild(link);
  link.click();
  link.remove();
  // 다운로드가 시작될 시간을 준 뒤 해제한다.
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** 심사표 한 건의 결과 Excel. */
export function downloadScreeningXlsx(
  result: ScreeningResult,
  judgements: JudgementMap,
): Promise<void> {
  return postForXlsx(
    "/api/screening/export.xlsx",
    { sites: [{ result, judgements }] },
    "LH_screening.xlsx",
  );
}

/** 일괄 심사 결과 Excel. 결과는 서버에 있으므로 담당자 판단만 보낸다. */
export function downloadBatchXlsx(
  batchId: string,
  judgements: Record<string, JudgementMap>,
): Promise<void> {
  return postForXlsx(
    `/api/screening/batch/${encodeURIComponent(batchId)}/export.xlsx`,
    { judgements },
    "LH_batch.xlsx",
  );
}
