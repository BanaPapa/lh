import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Crosshair,
  Database,
  XCircle,
} from "lucide-react";
import type {
  HazardCategoryDataState,
  HazardImplementationState,
  HazardReviewStatus,
} from "./types";

export interface HazardStatusConfig {
  label: string;
  icon: typeof AlertTriangle;
  /** 카드·히어로 색 토큰. CSS 의 tone-* 와 짝을 이룬다. */
  tone: "danger" | "warning" | "caution" | "neutral" | "success" | "muted";
}

/** 룰북 §4 판정 상태값 6종의 라벨·아이콘·톤. */
export const HAZARD_STATUS_CONFIG: Record<
  HazardReviewStatus,
  HazardStatusConfig
> = {
  exclusion_match: { label: "매입제외", icon: XCircle, tone: "danger" },
  review_required: { label: "검토 필요", icon: AlertTriangle, tone: "warning" },
  geometry_missing: { label: "경계 미확보", icon: Crosshair, tone: "caution" },
  dataset_missing: { label: "데이터셋 미확보", icon: Database, tone: "neutral" },
  no_conflict_in_snapshot: {
    label: "스냅샷 내 충돌 없음",
    icon: CheckCircle2,
    tone: "success",
  },
  not_applicable: { label: "미적용", icon: Archive, tone: "muted" },
};

/** 항목 상태 배지 라벨 — LH 회신문 §6-2 어휘를 그대로 쓴다(docs/hazards INDEX §5). */
export const HAZARD_DATA_STATE_LABELS: Record<HazardCategoryDataState, string> = {
  applied: "판정 적용",
  partial: "부분 적용",
  approximate: "근사 적용",
  missing: "판정 미적용",
  manual: "판정 미적용",
  negotiate: "개별 협의",
};

/**
 * 구현/확정/검증 축 배지. data_state 배지와는 별개다.
 * ready 는 배지를 띄우지 않는다(정상 상태).
 */
export interface HazardImplementationBadge {
  label: string;
  /** CSS 의 hazard-impl-state.is-* 와 짝을 이룬다. */
  modifier: "not-implemented" | "unconfirmed" | "needs-verification";
}

export const HAZARD_IMPLEMENTATION_BADGES: Record<
  Exclude<HazardImplementationState, "ready">,
  HazardImplementationBadge
> = {
  not_implemented: { label: "미구현", modifier: "not-implemented" },
  unconfirmed: { label: "미확정", modifier: "unconfirmed" },
  needs_verification: { label: "검증필요", modifier: "needs-verification" },
};

export function hazardImplementationBadge(
  state: HazardImplementationState,
): HazardImplementationBadge | null {
  if (state === "ready") return null;
  return HAZARD_IMPLEMENTATION_BADGES[state];
}

/**
 * 룰북 §11 고지. no_conflict_in_snapshot 은 초록이라도 매입 적격이 아니다.
 * 이 문구를 반드시 함께 노출한다.
 */
export const NO_CONFLICT_NOTICE =
  "스냅샷 범위에서 충돌 미발견 · 매입 적격을 의미하지 않음";

/** 유해요소 결과 상단 배지(결과 레일 섹션 헤더)용 톤 매핑. */
export function hazardSectionTone(
  status: HazardReviewStatus,
): "neutral" | "ready" | "warning" | "danger" {
  if (status === "exclusion_match") return "danger";
  if (status === "review_required" || status === "geometry_missing") {
    return "warning";
  }
  if (status === "no_conflict_in_snapshot") return "ready";
  return "neutral";
}
