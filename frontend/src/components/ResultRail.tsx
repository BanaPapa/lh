import {
  AlertTriangle,
  ChevronDown,
  ClipboardCheck,
  Maximize2,
  PanelRightClose,
  RotateCcw,
} from "lucide-react";
import { useEffect, useState } from "react";
import type { GeocodeCandidate } from "../types";
import type {
  HazardApplicationType,
  HazardApplicationTypesResponse,
  HazardHousingType,
  HazardParcel,
  HazardRulePack,
} from "../hazard-review/types";
import { HazardCriteriaPanel } from "../hazard-review/HazardCriteriaPanel";
import type { ScreeningResult } from "../screening/types";

type SectionTone = "neutral" | "ready" | "warning" | "danger";

/** 심사 종합 판정 → 결과 레일 섹션 헤더 톤. */
function screeningSectionTone(verdict: ScreeningResult["verdict"]): SectionTone {
  if (verdict === "fail") return "danger";
  if (verdict === "review") return "warning";
  return "ready";
}

/** 필지 면적 표기. 1000㎡ 이상은 읽기 쉽게 반올림한다. */
function formatArea(area_m2: number | null): string {
  if (area_m2 === null) return "면적 미상";
  return `${Math.round(area_m2).toLocaleString("ko-KR")}㎡`;
}

interface SectionProps {
  label: string;
  icon: typeof ClipboardCheck;
  badge: string;
  tone: SectionTone;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}

function Section({
  label,
  icon: Icon,
  badge,
  tone,
  open,
  onToggle,
  children,
}: SectionProps) {
  return (
    <article className={`result-section is-${tone} ${open ? "is-open" : ""}`}>
      <button type="button" onClick={onToggle} aria-expanded={open}>
        <span className="result-section-icon">
          <Icon size={17} />
        </span>
        <strong>{label}</strong>
        <span className="result-section-badge">{badge}</span>
        <ChevronDown size={15} className="result-section-chevron" />
      </button>
      {open && <div className="result-section-body">{children}</div>}
    </article>
  );
}

interface SiteCardProps {
  site: GeocodeCandidate;
  parcels: HazardParcel[];
  /** 연속지적도가 「외 N필지」를 다 풀지 못했을 때의 안내. */
  parcelNote?: string;
  /** 심사가 돌았거나 결과가 있어 필지를 바꿀 수 없는 상태. */
  locked?: boolean;
  onResetParcels: () => void;
}

/** 사업지와 선택된 필지. */
function SiteCard({
  site,
  parcels,
  parcelNote = "",
  locked = false,
  onResetParcels,
}: SiteCardProps) {
  const totalArea = parcels.reduce(
    (sum, parcel) => sum + (parcel.area_m2 ?? 0),
    0,
  );

  return (
    <article className="rail-site-card">
      <div className="rail-site-heading">
        <div>
          <small>사업지</small>
          <strong>{site.name}</strong>
        </div>
        {parcels.length > 0 && !locked && (
          <button type="button" className="parcel-reset-button" onClick={onResetParcels}>
            초기화
          </button>
        )}
      </div>
      <p className="rail-site-address">{site.road_address || site.address}</p>

      {parcels.length === 0 ? (
        <p className="site-parcel-hint">
          지적도 필지를 찾지 못했습니다. 지도를 눌러 필지를 직접 고르세요.
        </p>
      ) : (
        <>
          <p className="site-parcel-total">
            {parcels.length}필지 · 합계 {formatArea(totalArea)}
          </p>
          <ul className="site-parcel-list">
            {parcels.map((parcel, index) => (
              <li
                key={parcel.pnu}
                className={index === 0 ? "is-representative" : ""}
              >
                <span>
                  {parcel.address || parcel.pnu}
                  {index === 0 && <em className="parcel-rep-badge">대표</em>}
                </span>
                <span>{formatArea(parcel.area_m2)}</span>
              </li>
            ))}
          </ul>
        </>
      )}
      {parcelNote && <p className="site-parcel-note">{parcelNote}</p>}
      {locked ? (
        <p className="site-parcel-hint">
          심사 결과가 있는 동안에는 필지를 바꿀 수 없습니다. 필지를 다시 고르려면
          주소를 새로 검색하세요.
        </p>
      ) : (
        <p className="site-parcel-hint">
          지도에서 필지를 누르면 사업지에 더하고, 선택된 필지를 다시 누르면 뺍니다.
          {parcelNote ? " 빠진 필지는 지도에서 눌러 더하세요." : ""}
        </p>
      )}
    </article>
  );
}

interface ResultRailProps {
  site: GeocodeCandidate;
  siteParcels: HazardParcel[];
  parcelNote?: string;
  parcelsLocked?: boolean;
  onResetParcels: () => void;
  onCollapse: () => void;
  screeningResult: ScreeningResult | null;
  screeningRunning: boolean;
  screeningError: string;
  hazardApplicationTypes: HazardApplicationTypesResponse | null;
  hazardRulePack: HazardRulePack | null;
  hazardHousingType: HazardHousingType;
  hazardApplicationType: HazardApplicationType;
  onHazardHousingTypeChange: (value: HazardHousingType) => void;
  onHazardApplicationTypeChange: (value: HazardApplicationType) => void;
  onOpenSheet: () => void;
  onRerun: () => void;
}

/**
 * 화면 우측 결과 레일. 지도를 보면서 결과를 읽어야 하는 도구라 결과를 옆에
 * 두고, 지도는 남은 폭을 그대로 쓴다.
 */
export function ResultRail({
  site,
  siteParcels,
  parcelNote = "",
  parcelsLocked = false,
  onResetParcels,
  onCollapse,
  screeningResult,
  screeningRunning,
  screeningError,
  hazardApplicationTypes,
  hazardRulePack,
  hazardHousingType,
  hazardApplicationType,
  onHazardHousingTypeChange,
  onHazardApplicationTypeChange,
  onOpenSheet,
  onRerun,
}: ResultRailProps) {
  const [open, setOpen] = useState(true);
  // 새 심사가 끝나면 결과 섹션을 펼쳐 준다.
  useEffect(() => {
    setOpen(true);
  }, [screeningResult?.screening_id]);

  const hasResult = Boolean(screeningResult) || screeningRunning;

  return (
    <aside
      className="result-rail"
      aria-label={hasResult ? "심사 결과" : "심사 대상지역"}
    >
      <header>
        <div>
          {/* 심사 전에는 아직 결과가 아니라 무엇을 심사할지 고른 상태다. */}
          <h2>{hasResult ? "심사 결과" : "심사 대상지역"}</h2>
          <p>{site.name}</p>
        </div>
        <div className="result-drawer-actions">
          <button
            type="button"
            onClick={onRerun}
            aria-label="다시 심사"
            title="다시 심사"
          >
            <RotateCcw size={15} />
          </button>
          <button
            type="button"
            onClick={onCollapse}
            aria-label="결과 패널 접기"
            title="결과 패널 접기"
          >
            <PanelRightClose size={17} />
          </button>
        </div>
      </header>

      <div className="result-section-list">
        <SiteCard
          site={site}
          parcels={siteParcels}
          parcelNote={parcelNote}
          locked={parcelsLocked}
          onResetParcels={onResetParcels}
        />

        <HazardCriteriaPanel
          applicationTypes={hazardApplicationTypes}
          rulePack={hazardRulePack}
          housingType={hazardHousingType}
          applicationType={hazardApplicationType}
          onHousingTypeChange={onHazardHousingTypeChange}
          onApplicationTypeChange={onHazardApplicationTypeChange}
        />

        {!hasResult && (
          <p className="rail-empty-note">
            신청유형을 고르고 <b>심사 실행</b>을 누르면 결과가 여기에 쌓입니다.
          </p>
        )}

        {hasResult && (
          <Section
            label="심사"
            icon={ClipboardCheck}
            badge={
              screeningResult
                ? screeningResult.verdict_label
                : screeningRunning
                  ? "심사 중"
                  : "대기"
            }
            tone={
              screeningResult
                ? screeningSectionTone(screeningResult.verdict)
                : "neutral"
            }
            open={open}
            onToggle={() => setOpen((current) => !current)}
          >
            {screeningResult ? (
              <>
                <p className="result-note">
                  {screeningResult.verdict_summary}
                </p>
                <p className="result-footnote">
                  생활편의성{" "}
                  {screeningResult.stage_two.determined
                    ? `${screeningResult.stage_two.living_score ?? "—"} / ${
                        screeningResult.stage_two.living_maximum
                      }점`
                    : `${screeningResult.stage_two.living_score_min}~${screeningResult.stage_two.living_score_max}점 (확정 불가)`}
                </p>
                <button
                  type="button"
                  className="result-detail-button"
                  onClick={onOpenSheet}
                >
                  <Maximize2 size={13} /> 심사표 열기
                </button>
              </>
            ) : screeningRunning ? (
              <p className="result-note">심사표를 작성하는 중입니다.</p>
            ) : (
              <p className="result-note">
                <AlertTriangle size={13} /> {screeningError || "결과가 없습니다."}
              </p>
            )}
          </Section>
        )}
      </div>
    </aside>
  );
}
