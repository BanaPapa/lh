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
  /** 이 평가항목의 받은 등급 근거에 적힌 시설인지. 기본 지도 표시(초록) 대상이다. */
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
    // 받은 등급의 근거 문구에 적힌 시설 전부를 기본 표시(초록)로 올린다. 주거여건처럼
    // 요건이 여럿이면 근거 시설도 여럿이다(상업·의료·공원·문화·공공 각각). 항목 전체
    // 최근접 1곳만 올리면 근거에 적힌 공원이 지도에 안 나온다(금암동 473-6 검수).
    const evidence = criterion.tiers
      .filter((tier) => tier.selected)
      .flatMap((tier) => tier.requirements.map((req) => req.evidence))
      .join(" | ");
    let marked = false;
    for (const ref of criterionRefs) {
      if (!ref.hit.coordinates || !ref.hit.name) continue;
      if (evidence.includes(`${ref.hit.name} ${formatMeters(ref.hit.distance_m)}`)) {
        ref.isCriterionNearest = true;
        marked = true;
      }
    }
    // 근거 문구로 못 찾으면(등급 미확정 등) 좌표가 있는 최근접 1곳으로 물러선다.
    if (!marked) {
      let nearest: ScreeningHitRef | null = null;
      for (const ref of criterionRefs) {
        if (!ref.hit.coordinates) continue;
        if (!nearest || ref.hit.distance_m < nearest.hit.distance_m) {
          nearest = ref;
        }
      }
      if (nearest) nearest.isCriterionNearest = true;
    }
    refs.push(...criterionRefs);
  }
  return refs;
}

/** 근거 문구의 거리 표기(「1,045m」)와 같은 꼴. */
function formatMeters(distance: number): string {
  return `${Math.round(distance).toLocaleString("en-US")}m`;
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
      return "시설 경계";
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

/**
 * 법령 목 순서(가·나·다…·「다-1」)로 종류를 정렬하는 키. 라벨 앞머리를 읽는다.
 * 목 표기가 없는 라벨(「공장 있음」·「자동차용 천연가스충전소」)은 뒤로 보낸다.
 */
export function categoryOrderKey(label: string): number {
  const match = /^([가-힣])(?:·[가-힣])?(?:-(\d+))?\./.exec(label.trim());
  if (!match) return 99;
  const order = "가나다라마바사아자차카타파하".indexOf(match[1]);
  if (order < 0) return 99;
  return order * 10 + (match[2] ? Number(match[2]) : 0);
}
