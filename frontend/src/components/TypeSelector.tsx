import { useState } from "react";
import type {
  HazardApplicationType,
  HazardApplicationTypesResponse,
  HazardHousingType,
} from "../hazard-review/types";

interface TypeSelectorProps {
  applicationTypes: HazardApplicationTypesResponse | null;
  housingType: HazardHousingType;
  applicationType: HazardApplicationType;
  onHousingTypeChange: (value: HazardHousingType) => void;
  onApplicationTypeChange: (value: HazardApplicationType) => void;
  disabled?: boolean;
}

type Hover =
  | { kind: "housing"; value: HazardHousingType }
  | { kind: "application"; value: HazardApplicationType }
  | null;

/**
 * 상단 바의 주택유형·신청유형 선택. 룰북 매트릭스가 이 두 값으로 1차 임계거리를
 * 바꾸므로 심사 전에 반드시 골라야 한다. 임계거리 표는 늘 펼쳐 두지 않고, 유형 위에
 * 마우스를 올렸을 때만 그 조합의 표를 띄운다(라벨·매트릭스는 모두 서버 응답에서 받는다).
 */
export function TypeSelector({
  applicationTypes,
  housingType,
  applicationType,
  onHousingTypeChange,
  onApplicationTypeChange,
  disabled = false,
}: TypeSelectorProps) {
  const [hover, setHover] = useState<Hover>(null);

  if (!applicationTypes) {
    return <div className="solo-type-bar is-loading">신청유형 정보를 불러오는 중…</div>;
  }

  const housingEntries = Object.entries(applicationTypes.housing_types) as [
    HazardHousingType,
    string,
  ][];
  const applicationEntries = Object.entries(applicationTypes.application_types) as [
    HazardApplicationType,
    string,
  ][];

  // 올려 둔 유형을 반영한 조합. 주택유형 위면 현재 신청유형과, 신청유형 위면 현재 주택유형과 짝짓는다.
  const previewHousing = hover?.kind === "housing" ? hover.value : housingType;
  const previewApplication = hover?.kind === "application" ? hover.value : applicationType;
  const combo =
    applicationTypes.combos.find(
      (item) =>
        item.housing_type === previewHousing && item.application_type === previewApplication,
    ) ?? null;

  return (
    <div className="solo-type-bar" onMouseLeave={() => setHover(null)}>
      <div className="solo-type-group" role="group" aria-label="주택유형">
        <span className="solo-type-label">주택유형</span>
        {housingEntries.map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={`solo-type-pill${value === housingType ? " is-active" : ""}`}
            disabled={disabled}
            onClick={() => onHousingTypeChange(value)}
            onMouseEnter={() => setHover({ kind: "housing", value })}
            onFocus={() => setHover({ kind: "housing", value })}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="solo-type-group" role="group" aria-label="신청유형">
        <span className="solo-type-label">신청유형</span>
        {applicationEntries.map(([value, label]) => (
          <button
            key={value}
            type="button"
            className={`solo-type-pill${value === applicationType ? " is-active" : ""}`}
            disabled={disabled}
            onClick={() => onApplicationTypeChange(value)}
            onMouseEnter={() => setHover({ kind: "application", value })}
            onFocus={() => setHover({ kind: "application", value })}
          >
            {label}
          </button>
        ))}
      </div>

      {hover && combo && (
        <div className="solo-type-popover" role="tooltip">
          <header>
            <strong>적용 임계거리</strong>
            <small>
              {applicationTypes.housing_types[previewHousing]} ·{" "}
              {applicationTypes.application_types[previewApplication]}
              {(previewHousing !== housingType || previewApplication !== applicationType) &&
                " (누르면 이 조합으로 바뀝니다)"}
            </small>
          </header>
          <ul>
            {applicationTypes.rules.map((rule) => {
              const threshold = combo.thresholds[rule.rule_id] ?? null;
              return (
                <li key={rule.rule_id} className={threshold === null ? "is-not-applicable" : ""}>
                  <span>{rule.label}</span>
                  {threshold === null ? <em>미적용</em> : <b>{threshold}m</b>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
