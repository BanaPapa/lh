/**
 * 심사표 상태값 → 라벨·아이콘·톤 매핑.
 * 톤 이름은 유해시설 검토(`../hazard-review/status`)와 같은 어휘를 쓰고,
 * CSS 의 `.tone-*` 규칙이 그 이름으로 색을 붙인다. 새 색을 만들지 않는다.
 */
import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Database,
  Link2,
  Shuffle,
  XCircle,
} from "lucide-react";
import type {
  ScreeningGroupState,
  ScreeningItemOutcome,
  ScreeningVerdict,
} from "./types";

export type ScreeningTone = "danger" | "warning" | "success" | "muted";

export interface ScreeningToneConfig {
  icon: typeof AlertTriangle;
  tone: ScreeningTone;
}

/** 종합·1차 판정. 라벨은 백엔드가 준 verdict_label 을 그대로 쓴다. */
export const SCREENING_VERDICT_CONFIG: Record<
  ScreeningVerdict,
  ScreeningToneConfig
> = {
  fail: { icon: XCircle, tone: "danger" },
  review: { icon: AlertTriangle, tone: "warning" },
  pass: { icon: CheckCircle2, tone: "success" },
};

/** 1차 매입제외 항목 한 줄의 결과. */
export const SCREENING_OUTCOME_CONFIG: Record<
  ScreeningItemOutcome,
  ScreeningToneConfig
> = {
  fail: { icon: XCircle, tone: "danger" },
  review: { icon: AlertTriangle, tone: "warning" },
  pass: { icon: CheckCircle2, tone: "success" },
  not_applicable: { icon: Archive, tone: "muted" },
};

/** 2차 시설군의 수집 상태. */
export const SCREENING_GROUP_STATE_CONFIG: Record<
  ScreeningGroupState,
  ScreeningToneConfig
> = {
  connected: { icon: Link2, tone: "success" },
  substituted: { icon: Shuffle, tone: "warning" },
  missing: { icon: Database, tone: "muted" },
};

/** 거리 표기. 값이 없으면 "—", 있으면 소수 첫째 자리까지. */
export function formatDistance(distanceM: number | null): string {
  if (distanceM === null || Number.isNaN(distanceM)) return "—";
  return `${distanceM.toFixed(1)}m`;
}

/** 임계거리 표기. 매트릭스 미적용이면 "—". */
export function formatThreshold(thresholdM: number | null): string {
  if (thresholdM === null) return "—";
  return `${thresholdM.toLocaleString()}m`;
}

/** 점수 표기. 확정 못 한 항목은 "—". */
export function formatPoints(points: number | null): string {
  return points === null ? "—" : String(points);
}
