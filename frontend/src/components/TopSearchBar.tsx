import {
  BookOpen,
  ClipboardCheck,
  FileSpreadsheet,
  Moon,
  Plug,
  Printer,
  RotateCcw,
  Search,
  SlidersHorizontal,
  Sun,
} from "lucide-react";
import { useState } from "react";
import type { MapProvider } from "../types";
import type { ScreeningResult } from "../screening/types";
import type {
  HazardApplicationType,
  HazardApplicationTypesResponse,
  HazardHousingType,
  HazardRulePack,
} from "../hazard-review/types";
import { TypeSelector } from "./TypeSelector";
import type { ThemeMode } from "../theme";
import { ApiKeysPanel } from "./ApiKeysPanel";
import { SettingsMenu } from "./SettingsMenu";
import { RulebookModal } from "../rulebook/RulebookModal";
import { RulesPanel } from "./RulesPanel";

interface TopSearchBarProps {
  query: string;
  onQueryChange: (query: string) => void;
  onSearch: () => void;
  searching: boolean;
  searchError: string;
  searchNotice?: string;
  hasSite: boolean;
  mapProvider: MapProvider;
  onMapProviderChange: (provider: MapProvider) => void;
  onRun: () => void;
  /** 사업지·결과를 비우고 검색 상태로 되돌린다. */
  onResetSite: () => void;
  running: boolean;
  /** 필지가 바뀌어 다시 심사해야 할 때 실행 버튼을 반짝인다. */
  runAttention?: boolean;
  canPrint: boolean;
  theme: ThemeMode;
  onToggleTheme: () => void;
  /** 일괄 심사(신청자 엑셀 올리기) 모달을 연다. */
  onOpenBatch: () => void;
  /** 검색 뒤 주소 옆에 보이는 사업지 요약(필지 수·면적). */
  siteSummary?: {
    parcelCount: number;
    areaM2: number;
    /** 선택된 필지 지번(대표가 첫 항목). 칩에 그대로 나열한다. */
    parcels: { pnu: string; label: string; areaM2: number | null }[];
    /** 「외 N필지」 지번을 다 풀지 못했을 때만 채운다. */
    unresolvedNote: string;
    locked: boolean;
    onReset: () => void;
  } | null;
  applicationTypes: HazardApplicationTypesResponse | null;
  housingType: HazardHousingType;
  applicationType: HazardApplicationType;
  onHousingTypeChange: (value: HazardHousingType) => void;
  onApplicationTypeChange: (value: HazardApplicationType) => void;
  rulePack: HazardRulePack | null;
  /** 심사가 끝났으면 실행 버튼 오른쪽에 「심사 결과 상세」 버튼을 보인다. */
  screeningResult: ScreeningResult | null;
  screeningError: string;
  onOpenSheet: () => void;
}

const VERDICT_TONE: Record<string, string> = {
  pass: "tone-success",
  review: "tone-warning",
  fail: "tone-danger",
};

function formatArea(areaM2: number): string {
  return `${Math.round(areaM2).toLocaleString("ko-KR")}㎡`;
}

/**
 * 상단 바 한 줄. 입력창 옆 버튼 하나가 흐름을 끌고 간다 — 사업지가 없으면
 * 「검색」, 검색이 끝나면 같은 자리가 「심사 실행」으로 바뀐다. 다시 검색하려면
 * 실행 버튼 옆 되돌리기 아이콘으로 사업지를 비우고 검색 상태로 돌아간다.
 */
export function TopSearchBar({
  query,
  onQueryChange,
  onSearch,
  searching,
  searchError,
  searchNotice,
  hasSite,
  mapProvider,
  onMapProviderChange,
  onRun,
  onResetSite,
  running,
  runAttention = false,
  canPrint,
  theme,
  onToggleTheme,
  onOpenBatch,
  siteSummary = null,
  applicationTypes,
  housingType,
  applicationType,
  onHousingTypeChange,
  onApplicationTypeChange,
  rulePack,
  screeningResult,
  screeningError,
  onOpenSheet,
}: TopSearchBarProps) {
  // API 연결 패널(상단 바 전용 버튼이 연다).
  const [apiOpen, setApiOpen] = useState(false);
  // 심사 룰북 모달(API 연결 옆 버튼이 연다).
  const [rulebookOpen, setRulebookOpen] = useState(false);
  // 관리자 「기준 편집」(임계거리·완화 기준·LH 개별 확인 제외).
  const [rulesOpen, setRulesOpen] = useState(false);
  return (
    <header className="solo-topbar">
      {/* 1행: 브랜드(왼쪽) · 화면 설정(오른쪽) */}
      <div className="solo-bar-primary">
        <div className="solo-brand">
          <ClipboardCheck size={19} aria-hidden="true" />
          <span>LH 매입약정 서류심사</span>
        </div>

        <div className="solo-bar-actions">
          <button
            type="button"
            className="solo-icon-button"
            onClick={onToggleTheme}
            aria-label={theme === "dark" ? "라이트 테마로 전환" : "다크 테마로 전환"}
            title={theme === "dark" ? "라이트 테마" : "다크 테마"}
          >
            {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
          </button>

          <button
            type="button"
            className="solo-icon-button"
            onClick={onOpenBatch}
            aria-label="일괄 심사"
            title="일괄 심사 — 신청자 엑셀 올리기"
          >
            <FileSpreadsheet size={18} />
          </button>

          <button
            type="button"
            className={`solo-icon-button ${apiOpen ? "is-active" : ""}`}
            onClick={() => setApiOpen(true)}
            aria-label="API 연결"
            title="API 연결"
          >
            <Plug size={18} />
          </button>

          <button
            type="button"
            className={`solo-icon-button ${rulebookOpen ? "is-active" : ""}`}
            onClick={() => setRulebookOpen(true)}
            aria-label="심사 룰북"
            title="심사 룰북"
          >
            <BookOpen size={18} />
          </button>

          <button
            type="button"
            className={`solo-icon-button ${rulesOpen ? "is-active" : ""}`}
            onClick={() => setRulesOpen(true)}
            aria-label="기준 편집"
            title="기준 편집 — 임계거리 · 2027 완화 기준 · LH 개별 확인 제외"
          >
            <SlidersHorizontal size={18} />
          </button>

          <SettingsMenu
            mapProvider={mapProvider}
            onMapProviderChange={onMapProviderChange}
          />

          <button
            type="button"
            className="solo-icon-button"
            onClick={() => window.print()}
            disabled={!canPrint}
            aria-label="심사표 인쇄"
            title="인쇄"
          >
            <Printer size={18} />
          </button>
        </div>
      </div>

      {/* 2행: 검색·실행·결과 상세(왼쪽) · 주택유형·신청유형(오른쪽) */}
      <div className="solo-bar-secondary">
        <div className="solo-search-shell">
          <div className="solo-search-row">
            <form
              className="solo-search-form"
              onSubmit={(event) => {
                event.preventDefault();
                if (!searching && query.trim().length >= 2) onSearch();
              }}
            >
              <Search size={17} aria-hidden="true" />
              <input
                aria-label="사업지 주소 또는 장소"
                value={query}
                onChange={(event) => onQueryChange(event.target.value)}
                placeholder="주소·건물명·역명 검색 — 여러 필지는 363-2, -4, 364-1 처럼 쉼표로"
              />
            </form>

            {hasSite ? (
              <>
                <button
                  type="button"
                  className={`solo-run-button${runAttention ? " is-attention" : ""}`}
                  disabled={running}
                  onClick={onRun}
                  title="1차 매입제외 판정과 2차 생활편의성 배점을 실행합니다."
                >
                  {running ? "심사 중" : "심사 실행"}
                </button>
                <button
                  type="button"
                  className="solo-icon-button solo-reset-button"
                  disabled={running}
                  onClick={onResetSite}
                  aria-label="다시 검색"
                  title="사업지를 비우고 다시 검색"
                >
                  <RotateCcw size={17} />
                </button>
                {screeningResult && !running && (
                  <button
                    type="button"
                    className={`solo-detail-button ${VERDICT_TONE[screeningResult.verdict] ?? ""}`}
                    onClick={onOpenSheet}
                    title="1차 매입제외 판정과 2차 배점 근거를 심사표로 봅니다."
                  >
                    <em>{screeningResult.verdict_label}</em>
                    <span>
                      {screeningResult.stage_two.determined
                        ? `${screeningResult.stage_two.living_score ?? "—"}/${screeningResult.stage_two.living_maximum}점`
                        : `${screeningResult.stage_two.living_score_min}~${screeningResult.stage_two.living_score_max}점`}
                    </span>
                    심사 결과 상세
                  </button>
                )}
              </>
            ) : (
              <button
                type="button"
                className="solo-run-button"
                disabled={searching || query.trim().length < 2}
                onClick={onSearch}
                title="주소나 장소명으로 사업지를 찾습니다."
              >
                {searching ? "검색 중" : "검색"}
              </button>
            )}
          </div>
          {searchError && (
            <p className="solo-search-error" role="alert">
              {searchError}
            </p>
          )}
          {!searchError && searchNotice && (
            <p className="solo-search-notice" role="status">
              {searchNotice}
            </p>
          )}
          {!searchError && !searchNotice && screeningError && (
            <p className="solo-search-error" role="alert">
              {screeningError}
            </p>
          )}
        </div>
        <TypeSelector
          applicationTypes={applicationTypes}
          housingType={housingType}
          applicationType={applicationType}
          onHousingTypeChange={onHousingTypeChange}
          onApplicationTypeChange={onApplicationTypeChange}
          disabled={running}
        />
      </div>

      {/* 3행: 사업지(선택 필지) 요약 — 검색 뒤에만 */}
      {hasSite && siteSummary && (
        <div className="solo-bar-tertiary">
            <div
              className="solo-site-chip"
              title={
                siteSummary.locked
                  ? "심사 결과가 있는 동안에는 필지를 바꿀 수 없습니다. 필지를 다시 고르려면 주소를 새로 검색하세요."
                  : "지도에서 필지를 누르면 사업지에 더하고, 선택된 필지를 다시 누르면 뺍니다."
              }
            >
              {siteSummary.parcelCount > 0 ? (
                <>
                  <b>{siteSummary.parcelCount}필지</b>
                  <span>합계 {formatArea(siteSummary.areaM2)}</span>
                  <ul className="solo-site-parcels" aria-label="선택된 필지">
                    {siteSummary.parcels.map((parcel, index) => (
                      <li
                        key={parcel.pnu || `${parcel.label}-${index}`}
                        className={index === 0 ? "is-representative" : ""}
                        title={
                          parcel.areaM2 === null
                            ? parcel.label
                            : `${parcel.label} · ${formatArea(parcel.areaM2)}`
                        }
                      >
                        {parcel.label}
                        {index === 0 && <i>대표</i>}
                      </li>
                    ))}
                  </ul>
                  {siteSummary.unresolvedNote && (
                    <em title={siteSummary.unresolvedNote}>외 N필지 일부 미확정</em>
                  )}
                  {!siteSummary.locked && (
                    <button type="button" onClick={siteSummary.onReset}>
                      초기화
                    </button>
                  )}
                </>
              ) : (
                <span className="is-warning">필지 없음 — 지도에서 필지를 고르세요</span>
              )}
            </div>
        </div>
      )}
      <ApiKeysPanel open={apiOpen} onClose={() => setApiOpen(false)} />
      <RulebookModal open={rulebookOpen} onClose={() => setRulebookOpen(false)} rulePack={rulePack} />
      <RulesPanel open={rulesOpen} onClose={() => setRulesOpen(false)} />
    </header>
  );
}
