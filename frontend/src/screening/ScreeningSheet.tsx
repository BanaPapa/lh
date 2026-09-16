import {
  AlertTriangle,
  ChevronRight,
  ClipboardCheck,
  Crosshair,
  DoorOpen,
  ExternalLink,
  MapPin,
  Printer,
  RotateCcw,
  X,
} from "lucide-react";
import { Fragment, useCallback, useEffect, useState } from "react";
import { categoryOrderKey } from "./screeningOverlays";
import {
  SCREENING_GROUP_STATE_CONFIG,
  SCREENING_OUTCOME_CONFIG,
  SCREENING_VERDICT_CONFIG,
  formatDistance,
  formatPoints,
  formatThreshold,
} from "./status";
import {
  measurementShortLabel,
  type ScreeningDiff,
} from "./screeningOverlays";
import type {
  FrontDoorCandidate,
  FrontDoorView,
  ScreeningCriterion,
  ScreeningDataSource,
  ScreeningFacilityHit,
  ScreeningGroupStatus,
  ScreeningResult,
  ScreeningExclusionItem,
} from "./types";

/**
 * 시설명을 지정 목록 키와 맞추기 위해 정규화한다(백엔드 front_door.normalize_key 와
 * 동일 규칙 — 모든 공백 제거). 지정 목록의 `facility` 는 정규화 키이고 `hit.name` 은
 * 원문이라, 「전북대학교 전주캠퍼스」처럼 공백이 있으면 그대로 비교하면 배지·해제
 * 버튼이 보이지 않는다. 양쪽을 같은 규칙으로 정규화해 비교한다.
 */
export function normalizeFacilityKey(name: string | null | undefined): string {
  return (name ?? "").replace(/\s+/g, "");
}

/**
 * 심사표 「데이터」 열 — 이 항목 판정에 실제로 붙은 원천을 칩으로 그린다. 이 앱은
 * 로컬 납품 파일을 무시하고 공개 API 로 교차검증하는 것이 목적이라, 사용자가 「이
 * 항목이 API 로 판정됐는가, 로컬로 판정됐는가」를 한눈에 구분해야 한다.
 *  - api: 제공기관·데이터셋 라벨 + 외부 링크(원천 URL, 새 탭). title 에 전체 URL.
 *  - local: 무채색 "로컬" 칩. 링크 없음. title 에 파일명(있으면).
 *  - demo: "데모" 칩.
 * 미연결·수기 확인·판정제외 항목은 빈 배열이라 셀이 빈다(대시도 넣지 않는다).
 * 순서(API 먼저 → 로컬)는 백엔드가 정해 넘긴 그대로 그린다.
 */
function renderDataSources(sources: ScreeningDataSource[] | undefined) {
  // 계약상 항상 오는 필드지만, 구버전 서버·캐시된 심사 결과 호환을 위해 방어한다.
  const list = sources ?? [];
  if (list.length === 0) {
    return null;
  }
  return (
    <span className="screening-data-sources">
      {list.map((source, index) => {
        const key = `${source.detail || source.label}-${index}`;
        const chipClass = `screening-data-chip is-${source.kind}`;
        if (source.kind === "api" && source.url) {
          return (
            <a
              key={key}
              className={chipClass}
              href={source.url}
              target="_blank"
              rel="noreferrer"
              title={source.url}
            >
              <span>{source.label}</span>
              <ExternalLink size={12} aria-hidden="true" />
            </a>
          );
        }
        return (
          <span key={key} className={chipClass} title={source.detail || undefined}>
            {source.label}
          </span>
        );
      })}
    </span>
  );
}

const SOURCE_KIND_LABELS: Record<ScreeningDataSource["kind"], string> = {
  api: "API",
  local: "로컬",
  demo: "데모",
};

/** 「원천」 열 — 판정에 쓰인 원천의 종류(API·로컬·데모)만 한 단어로. 상세는 「데이터」열. */
function renderSourceKinds(sources: ScreeningDataSource[] | undefined) {
  const kinds = Array.from(new Set((sources ?? []).map((source) => source.kind)));
  if (kinds.length === 0) {
    return null;
  }
  return (
    <span className="screening-source-kinds">
      {kinds.map((kind) => (
        <em key={kind} className={`screening-source-kind is-${kind}`}>
          {SOURCE_KIND_LABELS[kind] ?? kind}
        </em>
      ))}
    </span>
  );
}

/** 「기준점 지정」을 노출하는 시설군(대학·종합병원·역 계열, #8·#9). */
const DESIGNATABLE_GROUPS = new Set([
  "university",
  "hospital",
  "subway",
  "railway",
  "terminal",
  "transfer",
]);
import type {
  HazardApplicationType,
  HazardApplicationTypesResponse,
  HazardHousingType,
} from "../hazard-review/types";

/**
 * 신청유형 라벨 대비책. 평소에는 백엔드 application-types 응답의 라벨을 쓰고,
 * 그 응답을 못 받았을 때만 이 표로 선택지를 그린다(선택 자체가 막히면 안 된다).
 */
const HOUSING_TYPE_FALLBACK: Record<HazardHousingType, string> = {
  house: "주택",
  officetel: "주거용 오피스텔",
};

const APPLICATION_TYPE_FALLBACK: Record<HazardApplicationType, string> = {
  general: "일반",
  multi_child: "다자녀",
  newlywed: "신혼",
  youth: "청년",
  senior: "고령자",
};

interface ScreeningSheetProps {
  result: ScreeningResult | null;
  running: boolean;
  error: string;
  applicationTypes: HazardApplicationTypesResponse | null;
  housingType: HazardHousingType;
  applicationType: HazardApplicationType;
  /** 값을 바꾸면 심사를 다시 돌린다. 판정 기준이 통째로 달라지기 때문이다. */
  onHousingTypeChange: (value: HazardHousingType) => void;
  onApplicationTypeChange: (value: HazardApplicationType) => void;
  /** 지도에서 강조 중인 유해요소 시설. 없으면 강조하지 않는다. */
  selectedFacilityId?: string | null;
  /** 1차 판정 근거의 시설을 지도에 표시한다. */
  onSelectFacility?: (facilityId: string) => void;
  /** 수기 지정된 기준점 목록. 배지·해제가 이 목록을 본다(#8·#9). */
  frontDoors?: FrontDoorView[];
  /** 기준점 지정 모드 대상 시설명. 그 행의 버튼이 진행 상태로 바뀐다. */
  designationTarget?: string | null;
  designationError?: string;
  onEnterDesignation?: (facility: string) => void;
  onCancelDesignation?: () => void;
  onRemoveDesignation?: (facility: string) => void;
  /** 문·출구 후보 선택(#7·#11). 지도 없이도 바꿀 수 있게 한다. */
  onSelectCandidate?: (facility: string, candidate: FrontDoorCandidate) => void;
  /** 지도에 전체 hit 을 펼친 시설군 키. 그 군의 hit 행이 심사표에도 펼쳐진다. */
  expandedGroupKey?: string | null;
  onToggleGroup?: (groupKey: string) => void;
  /** 지도에서 강조 중인 2차 시설명. 후보 문을 펼칠 기준이 된다. */
  selectedHitName?: string | null;
  onSelectHit?: (name: string | null) => void;
  /** 재계산 전후 차이 한 줄. 해당 시설군 행 아래에 보인다. */
  screeningDiff?: ScreeningDiff | null;
  onClose: () => void;
  onRerun: () => void;
}

/** 심사표 본문은 1차와 2차 두 장이다. 한 화면에 쏟지 않고 탭으로 가른다. */
type ScreeningStageTab = "stage-one" | "stage-two";

/** 열림 상태를 키 집합으로 들고 있는다. 여러 줄을 동시에 펼칠 수 있다. */
function useExpandedKeys(resetKey: string | undefined) {
  const [openKeys, setOpenKeys] = useState<ReadonlySet<string>>(
    () => new Set<string>(),
  );

  useEffect(() => {
    setOpenKeys(new Set<string>());
  }, [resetKey]);

  const toggle = useCallback((key: string) => {
    setOpenKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return next;
    });
  }, []);

  return { openKeys, toggle };
}

/**
 * LH 신축매입약정 서류심사표 한 장.
 * 위에서부터 머리말 · 종합 판정 · 1차 매입제외 · 2차 생활편의성 · 고지 순으로,
 * 실제 심사표를 읽는 순서 그대로 늘어놓는다.
 */
/** 1차 종류 행을 규칙(대분류) 순서대로 묶는다. 대분류 결과는 가장 강한 세부 결과를 따른다. */
function groupExclusionItems(items: ScreeningExclusionItem[]) {
  const order: string[] = [];
  const byRule = new Map<string, ScreeningExclusionItem[]>();
  items.forEach((item) => {
    if (!byRule.has(item.rule_id)) {
      byRule.set(item.rule_id, []);
      order.push(item.rule_id);
    }
    byRule.get(item.rule_id)!.push(item);
  });
  const rank: Record<string, number> = { fail: 3, review: 2, pass: 1, na: 0 };
  return order.map((ruleId) => {
    const rows = [...byRule.get(ruleId)!].sort(
      (a, b) => categoryOrderKey(a.label) - categoryOrderKey(b.label),
    );
    const worst = rows.reduce(
      (best, row) => ((rank[row.outcome] ?? 0) > (rank[best.outcome] ?? 0) ? row : best),
      rows[0],
    );
    const allPassthrough = rows.every((row) => row.passthrough);
    const outcome = SCREENING_OUTCOME_CONFIG[worst.outcome];
    return {
      ruleId,
      label: rows[0].rule_label,
      threshold: rows[0].threshold_m,
      items: rows,
      facilityCount: rows.reduce((sum, row) => sum + row.facilities.length, 0),
      tone: allPassthrough ? "muted" : outcome.tone,
      outcomeLabel: allPassthrough ? "미적용" : worst.outcome_label,
    };
  });
}

export function ScreeningSheet({
  result,
  running,
  error,
  applicationTypes,
  housingType,
  applicationType,
  onHousingTypeChange,
  onApplicationTypeChange,
  selectedFacilityId = null,
  onSelectFacility,
  frontDoors = [],
  designationTarget = null,
  designationError = "",
  onEnterDesignation,
  onCancelDesignation,
  onRemoveDesignation,
  onSelectCandidate,
  expandedGroupKey = null,
  onToggleGroup,
  selectedHitName = null,
  onSelectHit,
  screeningDiff = null,
  onClose,
  onRerun,
}: ScreeningSheetProps) {
  const rows = useExpandedKeys(result?.screening_id);
  const tiers = useExpandedKeys(result?.screening_id);
  const [stageTab, setStageTab] = useState<ScreeningStageTab>("stage-one");

  // 새 심사 결과가 오면 1차부터 다시 읽는다.
  useEffect(() => setStageTab("stage-one"), [result?.screening_id]);

  const housingEntries = (
    applicationTypes
      ? (Object.entries(applicationTypes.housing_types) as [
          HazardHousingType,
          string,
        ][])
      : (Object.entries(HOUSING_TYPE_FALLBACK) as [HazardHousingType, string][])
  ).filter(([value]) => value in HOUSING_TYPE_FALLBACK);

  const applicationEntries = (
    applicationTypes
      ? (Object.entries(applicationTypes.application_types) as [
          HazardApplicationType,
          string,
        ][])
      : (Object.entries(APPLICATION_TYPE_FALLBACK) as [
          HazardApplicationType,
          string,
        ][])
  ).filter(([value]) => value in APPLICATION_TYPE_FALLBACK);

  if (!result) {
    return (
      <div className="screening-empty">
        <span>
          <ClipboardCheck size={22} />
        </span>
        <div>
          <small>LH 신축매입약정 서류심사</small>
          <strong>
            {running ? "심사표를 작성하고 있습니다." : "심사를 실행해 주세요."}
          </strong>
          <p>
            주택유형과 신청유형을 고르면 1차 매입제외 판정과 2차 생활편의성
            배점을 심사표 한 장으로 냅니다.
          </p>
          {error && (
            <em>
              <AlertTriangle size={14} />
              {error}
            </em>
          )}
        </div>
      </div>
    );
  }

  const stageOne = result.stage_one;
  const stageTwo = result.stage_two;
  const verdict = SCREENING_VERDICT_CONFIG[result.verdict];
  const VerdictIcon = verdict.icon;

  const renderExclusionRow = (item: ScreeningExclusionItem) => {
    const outcome = SCREENING_OUTCOME_CONFIG[item.outcome];
    const expandable = item.facilities.length > 0;
    const open = rows.openKeys.has(item.key);
    const rowClass = [
      "screening-row",
      "is-sub",
      `tone-${outcome.tone}`,
      item.passthrough ? "is-passthrough" : "",
      open ? "is-open" : "",
    ]
      .filter(Boolean)
      .join(" ");

    // 시설이 없어 통과한 행은 근거 한 줄로 충분하다. 「스냅샷 범위 안에 해당 시설이
    // 없습니다」 같은 부연은 같은 말의 반복이라 숨긴다.
    const passedWithNoFacility =
      item.outcome === "pass" &&
      !item.passthrough &&
      item.facilities.length === 0;
    const hasNote =
      Boolean(item.note) && item.note !== item.reason && !passedWithNoFacility;
    const noteInDataCell = hasNote && (item.data_sources ?? []).length === 0;

    return (
      <Fragment key={item.key}>
        <tr className={rowClass}>
          <th scope="row">
            {expandable ? (
              <button
                type="button"
                className="screening-row-toggle"
                aria-expanded={open}
                onClick={() => rows.toggle(item.key)}
              >
                <ChevronRight size={14} className={open ? "is-open" : ""} />
                <span>{item.label}</span>
                <b>{item.facilities.length}</b>
              </button>
            ) : (
              <span className="screening-row-label">{item.label}</span>
            )}
          </th>
          <td className="is-num">{formatThreshold(item.threshold_m)}</td>
          <td className="is-num">{formatDistance(item.nearest_distance_m)}</td>
          <td>
            <em className={`screening-badge tone-${outcome.tone}`}>
              {item.outcome_label}
            </em>
          </td>
          <td>
            <span className="screening-row-reason">{item.reason}</span>
            {hasNote && !noteInDataCell && (
              <small className="screening-row-note">{item.note}</small>
            )}
          </td>
          <td className="screening-source-cell">
            {renderSourceKinds(item.data_sources)}
          </td>
          <td className="screening-data-cell">
            {renderDataSources(item.data_sources)}
            {/* 원천이 없는 행(미연결·수기 확인)은 데이터 열이 비므로, 좁은 근거 열
                대신 여기에 미연결 사유를 적는다. */}
            {noteInDataCell && (
              <small className="screening-row-note">{item.note}</small>
            )}
          </td>
        </tr>
        {expandable && open && (
          <tr className="screening-row-detail">
            <td colSpan={7}>
              <ul className="screening-facility-list">
                {item.facilities.map((facility) => (
                  <li
                    key={facility.facility_id}
                    className={
                      facility.facility_id === selectedFacilityId
                        ? "is-active"
                        : ""
                    }
                  >
                    {/* 심사표에 「주유소 21m」라고만 적히면 거리를 눈으로
                        확인할 방법이 없다. 눌러서 지도에 세운다. */}
                    <button
                      type="button"
                      disabled={!onSelectFacility}
                      title={
                        onSelectFacility ? "지도에서 이 시설 보기" : undefined
                      }
                      onClick={() => onSelectFacility?.(facility.facility_id)}
                    >
                      <MapPin size={14} aria-hidden="true" />
                      <span>
                        <strong>{facility.name}</strong>
                        <small>{facility.address || "주소 미확보"}</small>
                      </span>
                      <b className="is-num">
                        {formatDistance(facility.distance_m)}
                      </b>
                      <em>{facility.source_label}</em>
                      {facility.zoning_name && (
                        <em className="screening-facility-zoning">
                          용도지역 {facility.zoning_name}
                        </em>
                      )}
                      {(facility.zoning_class === "residential" ||
                        facility.zoning_class === "unknown") && (
                        <em className="screening-badge tone-warning">
                          확인 요청
                        </em>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            </td>
          </tr>
        )}
      </Fragment>
    );
  };

  /** 시설군 안의 근거 시설 한 줄. 측정 기준·문 선택·기준점 지정을 모두 담는다. */
  const renderHit = (
    group: ScreeningGroupStatus,
    hit: ScreeningFacilityHit,
    index: number,
  ) => {
    const designation =
      frontDoors.find(
        (item) =>
          normalizeFacilityKey(item.facility) === normalizeFacilityKey(hit.name),
      ) ?? null;
    const designating = designationTarget === hit.name;
    const candidates = hit.front_door_candidates ?? [];
    const selectedCandidate = candidates.find((item) => item.selected) ?? null;
    const canDesignate = DESIGNATABLE_GROUPS.has(group.key);
    const active = selectedHitName === hit.name;
    return (
      <li
        key={`${group.key}:${hit.name}:${index}`}
        className={`screening-hit${active ? " is-active" : ""}`}
      >
        <button
          type="button"
          className="screening-hit-select"
          disabled={!onSelectHit}
          title={onSelectHit ? "지도에서 이 시설 보기" : undefined}
          onClick={() => onSelectHit?.(active ? null : hit.name)}
        >
          <MapPin size={13} aria-hidden="true" />
          <span>
            <strong>{hit.name || "이름 미확보"}</strong>
            <small>{hit.address || "주소 미확보"}</small>
          </span>
          <b className="is-num">{formatDistance(hit.distance_m)}</b>
        </button>

        <p className="screening-hit-meta">
          측정 {measurementShortLabel(hit)}
          {hit.measurement_method ? ` · ${hit.measurement_method}` : ""}
          {hit.front_door_source ? ` · ${hit.front_door_source}` : ""}
        </p>
        {hit.front_door_notice && (
          <p className="screening-row-note">⚠ {hit.front_door_notice}</p>
        )}

        {designation && (
          <p className="screening-hit-badge-row">
            <em className="screening-badge tone-success">
              수기 지정 ({designation.pnu ? "필지" : "좌표"})
            </em>
            {designation.source_label && <small>{designation.source_label}</small>}
            {onRemoveDesignation && (
              <button
                type="button"
                className="screening-hit-clear"
                disabled={running}
                onClick={() => onRemoveDesignation(hit.name)}
              >
                해제
              </button>
            )}
          </p>
        )}

        <div className="screening-hit-actions">
          {candidates.length >= 2 && (
            <label className="screening-hit-door">
              <DoorOpen size={13} aria-hidden="true" />
              <span>문 {candidates.length}개</span>
              <select
                value={
                  selectedCandidate ? candidates.indexOf(selectedCandidate) : -1
                }
                disabled={!onSelectCandidate || running}
                onChange={(event) => {
                  const next = candidates[Number(event.target.value)];
                  if (next) onSelectCandidate?.(hit.name, next);
                }}
              >
                {selectedCandidate == null && (
                  <option value={-1} disabled>
                    기준점 선택
                  </option>
                )}
                {candidates.map((candidate, ci) => (
                  <option key={`${candidate.label}:${ci}`} value={ci}>
                    {candidate.label} ·{" "}
                    {Math.round(candidate.distance_m).toLocaleString()}m
                  </option>
                ))}
              </select>
            </label>
          )}
          {canDesignate &&
            onEnterDesignation &&
            (designating ? (
              <button
                type="button"
                className="screening-hit-designate is-active"
                onClick={() => onCancelDesignation?.()}
              >
                <Crosshair size={13} /> 지정 취소
              </button>
            ) : (
              <button
                type="button"
                className="screening-hit-designate"
                disabled={running}
                onClick={() => onEnterDesignation(hit.name)}
              >
                <Crosshair size={13} /> 기준점 지정
              </button>
            ))}
        </div>
      </li>
    );
  };

  const renderCriterion = (criterion: ScreeningCriterion, bonus = false) => {
    const open = tiers.openKeys.has(criterion.key);
    return (
      <article
        key={criterion.key}
        className={`screening-criterion ${
          criterion.determined ? "" : "is-undetermined"
        } ${bonus ? "is-bonus" : ""}`}
      >
        <header>
          <div>
            <strong>{criterion.label}</strong>
            {bonus && <em className="screening-badge is-bonus">가점</em>}
            {!criterion.determined && (
              <em className="screening-badge tone-warning">확정 불가</em>
            )}
          </div>
          <b className="is-num">
            {formatPoints(criterion.awarded)} / {criterion.maximum}점
          </b>
        </header>

        {criterion.tier_condition && (
          <p className="screening-criterion-tier">{criterion.tier_condition}</p>
        )}

        {!criterion.determined && (
          <p className="screening-row-note">
            산정 가능 범위 {criterion.awarded_min}~{criterion.awarded_max}점
            {criterion.note ? ` · ${criterion.note}` : ""}
          </p>
        )}

        {criterion.determined && criterion.note && (
          <p className="screening-row-note">{criterion.note}</p>
        )}

        {criterion.tiers.length > 0 && (
          <div className="screening-tier-block">
            <button
              type="button"
              className="screening-tier-toggle"
              aria-expanded={open}
              onClick={() => tiers.toggle(criterion.key)}
            >
              <ChevronRight size={14} className={open ? "is-open" : ""} />
              등급표 {criterion.tiers.length}단계
            </button>
            {open && (
              <ul className="screening-tier-list">
                {criterion.tiers.map((tier) => (
                  <li
                    key={`${criterion.key}:${tier.points}:${tier.condition}`}
                    className={[
                      tier.selected ? "is-selected" : "",
                      tier.achieved ? "is-achieved" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    <b className="is-num">{tier.points}점</b>
                    <span>{tier.condition}</span>
                    {tier.achieved && <em>충족</em>}
                  </li>
                ))}
              </ul>
            )}
            {criterion.basis && (
              <p className="screening-criterion-basis">{criterion.basis}</p>
            )}
          </div>
        )}

        {criterion.groups.length > 0 && (
          <div className="screening-table-scroll">
            <table className="screening-table screening-group-table">
              <thead>
                <tr>
                  <th scope="col">시설군</th>
                  <th scope="col">건수</th>
                  <th scope="col">최근접</th>
                  <th scope="col">결과</th>
                  <th scope="col">근거</th>
                  <th scope="col">원천</th>
                  <th scope="col">데이터</th>
                </tr>
              </thead>
              <tbody>
                {criterion.groups.map((group) => {
                  const state = SCREENING_GROUP_STATE_CONFIG[group.state];
                  const StateIcon = state.icon;
                  const sourceDiffers =
                    group.designated_source !== "" &&
                    group.actual_source !== "" &&
                    group.designated_source !== group.actual_source;
                  const hasHits = group.hits.length > 0;
                  const expanded = expandedGroupKey === group.key;
                  const diffForGroup =
                    screeningDiff && screeningDiff.groupKey === group.key
                      ? screeningDiff
                      : null;
                  const showNote = group.state !== "connected" && group.note;
                  return (
                    <Fragment key={group.key}>
                      <tr
                        className={`screening-row tone-${state.tone}${
                          expanded ? " is-open" : ""
                        }`}
                      >
                        <th scope="row">
                          {hasHits && onToggleGroup ? (
                            <button
                              type="button"
                              className="screening-row-toggle"
                              aria-expanded={expanded}
                              title="지도에 이 시설군 전체 표시"
                              onClick={() => onToggleGroup(group.key)}
                            >
                              <ChevronRight
                                size={14}
                                className={expanded ? "is-open" : ""}
                              />
                              <StateIcon size={14} />
                              <span>{group.label}</span>
                            </button>
                          ) : (
                            <span className="screening-row-label screening-group-label">
                              <StateIcon size={14} />
                              <span>{group.label}</span>
                            </span>
                          )}
                          {sourceDiffers && (
                            <small>
                              지정 원천 {group.designated_source} · 실제 원천{" "}
                              {group.actual_source}
                            </small>
                          )}
                          {group.point_exception && (
                            <small>거리 기준 {group.point_exception}</small>
                          )}
                        </th>
                        <td className="is-num">{group.count.toLocaleString()}건</td>
                        <td className="is-num">
                          {formatDistance(group.nearest_distance_m)}
                        </td>
                        <td>
                          <em className={`screening-badge tone-${state.tone}`}>
                            {group.state_label}
                          </em>
                        </td>
                        <td>
                          {showNote ? (
                            <span className="screening-row-reason">{group.note}</span>
                          ) : (
                            <span className="screening-row-reason">
                              {group.count > 0
                                ? `${group.actual_source || group.designated_source} 조회 결과 ${group.count.toLocaleString()}건.`
                                : "조회 범위 안에 해당 시설이 없습니다."}
                            </span>
                          )}
                          {diffForGroup && (
                            <small className="screening-row-note">
                              <RotateCcw size={12} aria-hidden="true" />{" "}
                              {diffForGroup.text}
                            </small>
                          )}
                        </td>
                        <td className="screening-source-cell">
                          {group.actual_source && group.state !== "missing" && (
                            <span className="screening-source-kinds">
                              <em className="screening-source-kind is-api">API</em>
                            </span>
                          )}
                        </td>
                        <td className="screening-data-cell">
                          {group.actual_source && group.state !== "missing" && (
                            <span className="screening-data-sources">
                              <span
                                className="screening-data-chip is-api"
                                title={
                                  sourceDiffers
                                    ? `지정 원천 ${group.designated_source}`
                                    : undefined
                                }
                              >
                                {group.actual_source}
                              </span>
                            </span>
                          )}
                        </td>
                      </tr>
                      {expanded && hasHits && (
                        <tr className="screening-row-detail">
                          <td colSpan={7}>
                            <ul className="screening-hit-list">
                              {group.hits.map((hit, index) =>
                                renderHit(group, hit, index),
                              )}
                            </ul>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </article>
    );
  };

  return (
    <section className="screening-sheet" aria-label="LH 서류심사표">
      {/* A. 머리말 */}
      <header className="screening-head">
        <div className="screening-head-title">
          <small>LH 신축매입약정 서류심사</small>
          <h2>{result.site.name}</h2>
          <p>{result.site.address || "주소 미확보"}</p>
        </div>
        <div className="screening-head-controls">
          <label className="screening-field">
            <span>주택유형</span>
            <select
              className="screening-select"
              value={housingType}
              disabled={running}
              onChange={(event) =>
                onHousingTypeChange(event.target.value as HazardHousingType)
              }
            >
              {housingEntries.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="screening-field">
            <span>신청유형</span>
            <select
              className="screening-select"
              value={applicationType}
              disabled={running}
              onChange={(event) =>
                onApplicationTypeChange(
                  event.target.value as HazardApplicationType,
                )
              }
            >
              {applicationEntries.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <div className="screening-head-actions">
            <button
              type="button"
              title="심사표 인쇄"
              onClick={() => window.print()}
            >
              <Printer size={17} />
            </button>
            <button type="button" aria-label="심사표 닫기" onClick={onClose}>
              <X size={18} />
            </button>
          </div>
        </div>
      </header>

      <p className="screening-rulepack">
        적용 규칙팩 {result.rule_pack_id || "미지정"} · v
        {result.rule_pack_version || "—"} · 적용 조건 {result.housing_type_label}{" "}
        · {result.application_type_label}
        {result.demo ? " · 데모 데이터" : ""}
      </p>

      {error && (
        <p className="screening-notice tone-danger">
          <AlertTriangle size={14} />
          {error}
        </p>
      )}

      {designationError && (
        <p className="screening-notice tone-danger">
          <AlertTriangle size={14} />
          {designationError}
        </p>
      )}

      {designationTarget && (
        <p className="screening-notice tone-warning">
          <Crosshair size={14} />
          {designationTarget} 기준점 지정 모드입니다. 지도에서 필지 또는 문을
          누르세요.
          <button
            type="button"
            className="screening-notice-action"
            onClick={() => onCancelDesignation?.()}
          >
            취소
          </button>
        </p>
      )}

      {/* B. 종합 판정 */}
      <section className={`screening-verdict tone-${verdict.tone}`}>
        <span>
          <VerdictIcon size={27} />
        </span>
        <div>
          <small>종합 판정</small>
          <strong>{result.verdict_label}</strong>
          <p>{result.verdict_summary}</p>
        </div>
      </section>

      {result.verdict === "fail" && stageOne.reasons.length > 0 && (
        <section className="screening-reasons" aria-label="부적격 사유">
          <header>
            <AlertTriangle size={15} />
            <strong>부적격 사유</strong>
            <b className="is-num">{stageOne.reasons.length}건</b>
          </header>
          <ol>
            {stageOne.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ol>
        </section>
      )}

      {result.verdict === "review" && stageOne.review_reasons.length > 0 && (
        <section
          className="screening-reasons is-review"
          aria-label="검토 필요 사유"
        >
          <header>
            <AlertTriangle size={15} />
            <strong>검토 필요 사유</strong>
            <b className="is-num">{stageOne.review_reasons.length}건</b>
          </header>
          <ol>
            {stageOne.review_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ol>
        </section>
      )}

      {/* C. 본문 — 1차·2차를 탭으로 가른다. 한 장에 다 쏟으면 어느 쪽을
         읽고 있는지 알 수 없고, 인쇄 아닌 화면에서 스크롤만 길어진다. */}
      <nav className="screening-stage-tabs" role="tablist" aria-label="심사 단계">
        <button
          type="button"
          role="tab"
          aria-selected={stageTab === "stage-one"}
          className={stageTab === "stage-one" ? "is-active" : ""}
          onClick={() => setStageTab("stage-one")}
        >
          1차 매입제외 판정
          <b className="is-num">{stageOne.items.length}</b>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={stageTab === "stage-two"}
          className={stageTab === "stage-two" ? "is-active" : ""}
          onClick={() => setStageTab("stage-two")}
        >
          2차 생활편의성 배점
          <b className="is-num">
            {stageTwo.determined
              ? `${formatPoints(stageTwo.living_score)}/${stageTwo.living_maximum}`
              : `${stageTwo.living_score_min}~${stageTwo.living_score_max}`}
          </b>
        </button>
      </nav>

      {/* C-1. 1차 매입제외 — 「주거환경 저해시설」만 보여준다.
         LH 가 요청한 것은 심사표 2항 15개 항목 중 이 한 줄이다. 나머지 14개는
         공간분석 대상이 아니라 담당자가 서류로 확인하는 것이라 화면에 늘어놓지
         않는다. 다만 「자동 판정 통과 ≠ 매입적격」은 감추면 안 되므로 한 줄로
         남긴다. 응답의 stageOne.checklist(15개 항목)는 JSON 증빙에 그대로 둔다. */}
      <section
        className="screening-section"
        role="tabpanel"
        hidden={stageTab !== "stage-one"}
      >
        <div className="screening-section-heading">
          <div>
            <span>1차</span>
            <strong>주거환경 저해시설 판정</strong>
          </div>
          <small>{stageOne.summary}</small>
        </div>

        {stageOne.checklist_note && (
          <p className="screening-checklist-note">{stageOne.checklist_note}</p>
        )}

        <div className="screening-table-scroll">
          <table className="screening-table">
            <caption className="screening-table-caption">
              종류별 판정 내역
            </caption>
            <thead>
              <tr>
                <th scope="col">항목</th>
                <th scope="col">기준거리</th>
                <th scope="col">최근접</th>
                <th scope="col">결과</th>
                <th scope="col">근거</th>
                <th scope="col">원천</th>
                <th scope="col">데이터</th>
              </tr>
            </thead>
            <tbody>
              {groupExclusionItems(stageOne.items).map((group) => (
                <Fragment key={group.ruleId}>
                  <tr className={`screening-rule-head tone-${group.tone}`}>
                    <th scope="rowgroup" colSpan={7}>
                      <span className="screening-rule-title">
                        {group.label}
                      </span>
                      <span className="screening-rule-meta">
                        {group.threshold !== null
                          ? `기준 ${group.threshold}m`
                          : "미적용"}
                        {" · "}
                        세부 {group.items.length}종
                        {group.facilityCount > 0 && ` · 시설 ${group.facilityCount}곳`}
                      </span>
                      <em className={`screening-badge tone-${group.tone}`}>
                        {group.outcomeLabel}
                      </em>
                    </th>
                  </tr>
                  {group.items.map(renderExclusionRow)}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>

        {stageOne.passthrough_notes.length > 0 && (
          <div className="screening-passthrough-notes">
            <strong>판정 미적용 항목</strong>
            <ul>
              {stageOne.passthrough_notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {/* C-2. 2차 생활편의성 */}
      <section
        className="screening-section"
        role="tabpanel"
        hidden={stageTab !== "stage-two"}
      >
        <div className="screening-section-heading">
          <div>
            <span>2차</span>
            <strong>생활편의성 배점</strong>
          </div>
          <small>{stageTwo.sheet_label}</small>
        </div>

        {stageTwo.reference_only && (
          <p className="screening-notice tone-warning">
            <AlertTriangle size={14} />
            1차 매입제외에 해당해 아래 배점은 참고용입니다. 합격 여부를 가르지
            않습니다.
          </p>
        )}

        <div className="screening-score-head">
          <div>
            <small>생활편의성 점수</small>
            {stageTwo.determined ? (
              <strong className="is-num">
                {formatPoints(stageTwo.living_score)} /{" "}
                {stageTwo.living_maximum}점
              </strong>
            ) : (
              <strong className="is-num">
                산정 가능 범위 {stageTwo.living_score_min}~
                {stageTwo.living_score_max}점
              </strong>
            )}
          </div>
          <p>
            심사표 총점 {stageTwo.total_sheet_points}점 · 합격선{" "}
            {stageTwo.pass_threshold}점 · 이 화면은 생활편의성{" "}
            {stageTwo.living_maximum}점만 산정합니다.
          </p>
          {!stageTwo.determined && stageTwo.note && (
            <p className="screening-row-note">{stageTwo.note}</p>
          )}
        </div>

        <div className="screening-criteria">
          {stageTwo.criteria.map((criterion) => renderCriterion(criterion))}
          {stageTwo.bonus && renderCriterion(stageTwo.bonus, true)}
        </div>

        {stageTwo.out_of_scope.length > 0 && (
          <div className="screening-out-of-scope">
            <header>
              <strong>산정 대상 아님</strong>
              <b className="is-num">{stageTwo.out_of_scope_points}점</b>
            </header>
            <ul>
              {stageTwo.out_of_scope.map((item) => (
                <li key={item.label}>
                  <span>{item.label}</span>
                  <b className="is-num">{item.maximum}점</b>
                  <small>{item.reason}</small>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {/* E. 하단 고지 */}
      <footer className="screening-footer">
        {result.calculation_note && <p>{result.calculation_note}</p>}
        {result.disclaimer && <strong>{result.disclaimer}</strong>}
        <button type="button" onClick={onRerun} disabled={running}>
          <RotateCcw size={15} />
          같은 조건으로 다시 심사
        </button>
      </footer>
    </section>
  );
}
