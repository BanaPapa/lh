import { ShieldCheck, SlidersHorizontal } from "lucide-react";
import type {
  HazardApplicationType,
  HazardApplicationTypesResponse,
  HazardHousingType,
  HazardRulePack,
} from "./types";

interface HazardCriteriaPanelProps {
  applicationTypes: HazardApplicationTypesResponse | null;
  rulePack: HazardRulePack | null;
  housingType: HazardHousingType;
  applicationType: HazardApplicationType;
  onHousingTypeChange: (value: HazardHousingType) => void;
  onApplicationTypeChange: (value: HazardApplicationType) => void;
}

/**
 * 심사 실행 전 주택유형·신청유형을 고르는 패널.
 * 룰북 매트릭스가 이 두 값으로 임계거리를 바꾸므로 없으면 판정 자체가 불가능하다.
 * 라벨과 매트릭스는 모두 application-types 응답에서 받는다(하드코딩 금지).
 */
export function HazardCriteriaPanel({
  applicationTypes,
  rulePack,
  housingType,
  applicationType,
  onHousingTypeChange,
  onApplicationTypeChange,
}: HazardCriteriaPanelProps) {
  if (!applicationTypes) {
    return (
      <article className="hazard-criteria-block">
        <p className="hazard-criteria-loading">신청유형 정보를 불러오는 중…</p>
      </article>
    );
  }

  const housingEntries = Object.entries(applicationTypes.housing_types) as [
    HazardHousingType,
    string,
  ][];
  const applicationEntries = Object.entries(
    applicationTypes.application_types,
  ) as [HazardApplicationType, string][];

  const combo =
    applicationTypes.combos.find(
      (item) =>
        item.housing_type === housingType &&
        item.application_type === applicationType,
    ) ?? null;

  return (
    <article className="hazard-criteria-block">
      <header className="hazard-criteria-head">
        <SlidersHorizontal size={15} />
        <div>
          <strong>신청유형</strong>
          <small>주택유형과 신청유형에 따라 임계거리가 달라집니다.</small>
        </div>
      </header>

      <div className="hazard-criteria-field">
        <span className="hazard-field-label">주택유형</span>
        <div className="hazard-segmented-grid">
          {housingEntries.map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={value === housingType ? "is-active" : ""}
              onClick={() => onHousingTypeChange(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="hazard-criteria-field">
        <span className="hazard-field-label">신청유형</span>
        <div className="hazard-segmented-grid">
          {applicationEntries.map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={value === applicationType ? "is-active" : ""}
              onClick={() => onApplicationTypeChange(value)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="hazard-matrix">
        <div className="hazard-matrix-caption">
          <span>적용 임계거리</span>
          <small>
            {applicationTypes.housing_types[housingType]} ·{" "}
            {applicationTypes.application_types[applicationType]}
          </small>
        </div>
        <ul className="hazard-matrix-list">
          {applicationTypes.rules.map((rule) => {
            const threshold = combo?.thresholds[rule.rule_id] ?? null;
            const applied = threshold !== null;
            return (
              <li
                key={rule.rule_id}
                className={applied ? "" : "is-not-applicable"}
              >
                <span>{rule.label}</span>
                {applied ? (
                  <b>{threshold}m</b>
                ) : (
                  <em className="hazard-matrix-na">미적용</em>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      {rulePack && (
        <div className="hazard-rulepack-active">
          <ShieldCheck size={14} />
          <div>
            <strong>{rulePack.title}</strong>
            <small>
              v{rulePack.version} · {rulePack.effective_from} 적용
            </small>
          </div>
          <span>현재 규칙팩</span>
        </div>
      )}
    </article>
  );
}
