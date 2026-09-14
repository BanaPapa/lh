/**
 * 2차 근거 시설을 지도에 올리기 위한 순수 헬퍼.
 * MapPanel 이 비대해지지 않도록 hits 평탄화·기본 표시 대상 선정·재계산 차이
 * 계산을 이 파일에 모은다. 지도 SDK 에 의존하지 않는다.
 */
import type { ScreeningFacilityHit, ScreeningResult } from "./types";

/** 심사표 시설 한 줄을 지도/표에서 공통으로 가리키기 위한 평탄화 구조. */
export interface ScreeningHitRef {
  /** 평가항목 키(대중교통·주거여건·교육여건·역세권 가점). */
  criterionKey: string;
  criterionLabel: string;
  /** 시설군 키(버스정류장·지하철역·대학 등). */
  groupKey: string;
  groupLabel: string;
  hit: ScreeningFacilityHit;
  /** 이 평가항목에서 점수를 결정한 최근접 시설인지. 기본 지도 표시 대상이다. */
  isCriterionNearest: boolean;
  /** 안정 식별자: criterionKey|groupKey|index. */
  key: string;
}

/**
 * stage_two.criteria(+bonus)의 모든 hit 을 평탄화한다.
 * 평가항목별로 좌표가 있는 최근접 시설 1곳을 기본 표시 대상으로 표시한다.
 */
export function flattenScreeningHits(
  result: ScreeningResult | null,
): ScreeningHitRef[] {
  if (!result) return [];
  const criteria = [
    ...result.stage_two.criteria,
    ...(result.stage_two.bonus ? [result.stage_two.bonus] : []),
  ];
  const refs: ScreeningHitRef[] = [];
  for (const criterion of criteria) {
    const criterionRefs: ScreeningHitRef[] = [];
    for (const group of criterion.groups) {
      group.hits.forEach((hit, index) => {
        criterionRefs.push({
          criterionKey: criterion.key,
          criterionLabel: criterion.label,
          groupKey: group.key,
          groupLabel: group.label,
          hit,
          isCriterionNearest: false,
          key: `${criterion.key}|${group.key}|${index}`,
        });
      });
    }
    // 좌표가 있는 시설 중 최근접 1곳을 기본 표시로 올린다. 점수를 결정한 시설이다.
    let nearest: ScreeningHitRef | null = null;
    for (const ref of criterionRefs) {
      if (!ref.hit.coordinates) continue;
      if (!nearest || ref.hit.distance_m < nearest.hit.distance_m) {
        nearest = ref;
      }
    }
    if (nearest) nearest.isCriterionNearest = true;
    refs.push(...criterionRefs);
  }
  return refs;
}

/**
 * 지도에 실제로 그릴 hit 을 고른다. 기본은 평가항목별 최근접 1곳이고,
 * 시설군 행을 펼치면(expandedGroupKey) 그 군의 hit 전부를 더 보여준다.
 */
export function visibleScreeningHits(
  refs: ScreeningHitRef[],
  expandedGroupKey: string | null,
): ScreeningHitRef[] {
  return refs.filter(
    (ref) =>
      ref.hit.coordinates != null &&
      (ref.isCriterionNearest || ref.groupKey === expandedGroupKey),
  );
}

/** 측정 기준을 이름표에 넣을 짧은 표기로 바꾼다(정문 좌표 / 시설 좌표 / 필지경계). */
export function measurementShortLabel(hit: ScreeningFacilityHit): string {
  switch (hit.measurement_tier) {
    case "front_door_parcel":
      return "정문 필지";
    case "front_door_point":
      return "정문 좌표";
    case "site_boundary":
      return "필지경계";
    case "coordinate":
      return "시설 좌표";
    default:
      return hit.measurement_method || "시설 좌표";
  }
}

export interface ScreeningDiff {
  /** 차이 줄을 붙일 시설군 키. */
  groupKey: string;
  text: string;
}

/**
 * 기준점 변경 후 직전 결과와의 차이를 한 줄로 만든다.
 * 예: "주거여건 12 → 15점 · 원광대병원 2,143m → 1,881m".
 */
export function computeDesignationDiff(
  previous: ScreeningResult | null,
  next: ScreeningResult | null,
  facility: string,
): ScreeningDiff | null {
  if (!previous || !next) return null;
  const locate = (result: ScreeningResult) => {
    const criteria = [
      ...result.stage_two.criteria,
      ...(result.stage_two.bonus ? [result.stage_two.bonus] : []),
    ];
    for (const criterion of criteria) {
      for (const group of criterion.groups) {
        const hit = group.hits.find((item) => item.name === facility);
        if (hit) return { criterion, group, hit };
      }
    }
    return null;
  };
  const before = locate(previous);
  const after = locate(next);
  if (!after) return null;

  const parts: string[] = [];
  const beforeScore = before?.criterion.awarded;
  const afterScore = after.criterion.awarded;
  if (beforeScore != null && afterScore != null && beforeScore !== afterScore) {
    parts.push(`${after.criterion.label} ${beforeScore} → ${afterScore}점`);
  }
  const beforeDist = before?.hit.distance_m;
  const afterDist = after.hit.distance_m;
  if (
    beforeDist != null &&
    Math.round(beforeDist) !== Math.round(afterDist)
  ) {
    parts.push(
      `${facility} ${Math.round(beforeDist).toLocaleString()}m → ${Math.round(
        afterDist,
      ).toLocaleString()}m`,
    );
  }
  if (parts.length === 0) {
    parts.push(`${facility} 기준점 반영 · 점수·거리 변화 없음`);
  }
  return { groupKey: after.group.key, text: parts.join(" · ") };
}
