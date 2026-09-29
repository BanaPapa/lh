/**
 * 1차 항목별 담당자 판단·메모. 서버에 두지 않고 이 브라우저(localStorage)에만 남긴다.
 * 키는 심사 ID 하나당 한 칸이고, 안에 1차 항목 키 → 판단을 담는다.
 *
 * 저장소 접근은 사생활 보호 모드·차단된 사이트 데이터에서 예외를 던질 수 있으므로
 * 모두 try/catch 로 감싸고, 실패하면 저장 없이 화면만 동작한다.
 */

export type JudgementState =
  | "unchecked"
  | "confirmed"
  | "evidence_requested"
  | "false_positive"
  | "excluded";

export interface ItemJudgement {
  state: JudgementState;
  memo: string;
}

/** 1차 항목 키(ScreeningExclusionItem.key) → 판단. */
export type JudgementMap = Record<string, ItemJudgement>;

/** 선택지 순서 그대로. 백엔드 export_xlsx.JUDGEMENT_LABELS 와 같은 키·라벨이다. */
export const JUDGEMENT_OPTIONS: { value: JudgementState; label: string }[] = [
  { value: "unchecked", label: "미확인" },
  { value: "confirmed", label: "확인 완료" },
  { value: "evidence_requested", label: "추가 증빙 요청" },
  { value: "false_positive", label: "오탐" },
  { value: "excluded", label: "적용 제외" },
];

const STORAGE_PREFIX = "lh-screening-judgement:";
const VALID_STATES = new Set<string>(JUDGEMENT_OPTIONS.map((o) => o.value));

function storageKey(screeningId: string): string {
  return `${STORAGE_PREFIX}${screeningId}`;
}

function sanitize(raw: unknown): JudgementMap {
  if (!raw || typeof raw !== "object") return {};
  const out: JudgementMap = {};
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!value || typeof value !== "object") continue;
    const { state, memo } = value as { state?: unknown; memo?: unknown };
    if (typeof state !== "string" || !VALID_STATES.has(state)) continue;
    out[key] = {
      state: state as JudgementState,
      memo: typeof memo === "string" ? memo : "",
    };
  }
  return out;
}

/** 심사 한 건의 담당자 판단. 없거나 읽지 못하면 빈 객체. */
export function loadJudgements(screeningId: string | null | undefined): JudgementMap {
  if (!screeningId) return {};
  try {
    const text = window.localStorage.getItem(storageKey(screeningId));
    return text ? sanitize(JSON.parse(text)) : {};
  } catch {
    return {};
  }
}

/** 판단을 저장한다. 미확인·메모 없음 항목은 빼서 기본값을 보관하지 않는다. */
export function saveJudgements(screeningId: string, map: JudgementMap): void {
  const kept = Object.fromEntries(
    Object.entries(map).filter(([, j]) => j.state !== "unchecked" || j.memo.trim() !== ""),
  );
  try {
    if (Object.keys(kept).length === 0) {
      window.localStorage.removeItem(storageKey(screeningId));
    } else {
      window.localStorage.setItem(storageKey(screeningId), JSON.stringify(kept));
    }
  } catch {
    // 저장이 막힌 브라우저 — 이번 화면에서만 유지된다.
  }
}

/** 여러 심사의 판단을 한 번에(일괄 Excel 요청용). 판단이 없는 심사는 뺀다. */
export function collectJudgements(
  screeningIds: (string | null | undefined)[],
): Record<string, JudgementMap> {
  const out: Record<string, JudgementMap> = {};
  for (const id of screeningIds) {
    if (!id) continue;
    const map = loadJudgements(id);
    if (Object.keys(map).length > 0) out[id] = map;
  }
  return out;
}
