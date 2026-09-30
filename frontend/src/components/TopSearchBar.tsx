import {
  AlertTriangle,
  BookOpen,
  ClipboardCheck,
  FileSpreadsheet,
  Moon,
  Plug,
  Printer,
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
import { IS_LOCAL_APP } from "../deployment";

/**
 * API 연결(키 입력·연결 점검) 패널은 이 PC(localhost)에서 띄울 때만 보인다. 배포 사이트는
 * 누구나 들어오므로 브라우저 키 입력칸에 카카오 키가 그대로 보이면 안 된다. 서버 키
 * 설정 API 는 원래 루프백에서만 열려 있어 배포 서버에서는 어차피 쓸 수 없다. 그 창의 탭인
 * 「API 외에 서버에 실은 파일」 목록도 같은 기준으로 배포판에서는 감춘다.
 */
const API_PANEL_ENABLED = IS_LOCAL_APP;
import { ServerWakeNotice } from "./ServerWakeNotice";
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
  /** 카카오 API 한도 초과 안내(전문). 있으면 테마 버튼 왼쪽 상태 줄에 짧게 보인다. */
  kakaoNotice?: string;
  /** 일괄 심사(신청자 엑셀 올리기) 모달을 연다. */
  onOpenBatch: () => void;
  /** 검색 뒤 검색창 바로 아래에 보이는 사업지 요약(필지 수·면적·지번). */
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
  /** 심사가 끝났으면 실행 버튼 오른쪽에 「상세 결과」 버튼을 보인다. */
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
  kakaoNotice = "",
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
      {/* 5열 × 2행 — 1행: 앱 이름 · 주택유형 · 검색창 · 실행 · 설정
                      2행: 판정·점수 · 신청유형 · 사업지 칩(검색창 아래) · 다시 검색 · (비움) */}
      <div className="solo-brand">
        <ClipboardCheck size={19} aria-hidden="true" />
        <span>LH 매입약정 서류심사</span>
      </div>
      <div className="solo-brand-slot">
          {screeningResult && !running && (
            <button
              type="button"
              className={`solo-brand-result ${VERDICT_TONE[screeningResult.verdict] ?? ""}`}
              onClick={onOpenSheet}
              title={
                screeningResult.source_alerts?.length
                  ? `공공 데이터 ${screeningResult.source_alerts.length}건이 응답하지 않았습니다. 상세 결과에서 확인하고 재심사하세요.`
                  : "상세 결과를 엽니다."
              }
            >
              {(screeningResult.source_alerts?.length ?? 0) > 0 && (
                <strong className="solo-brand-alert">
                  <AlertTriangle size={14} aria-hidden="true" />
                  원천 장애
                </strong>
              )}
              <em>{screeningResult.verdict_label}</em>
              <span>
                생활편의성{" "}
                {screeningResult.stage_two.determined
                  ? `${screeningResult.stage_two.living_score ?? "—"}/${screeningResult.stage_two.living_maximum}점`
                  : `${screeningResult.stage_two.living_score_min}~${screeningResult.stage_two.living_score_max}점`}
              </span>
            </button>
          )}
      </div>

      <div className="solo-housing-slot">
          <TypeSelector
            only="housing"
            applicationTypes={applicationTypes}
            housingType={housingType}
            applicationType={applicationType}
            onHousingTypeChange={onHousingTypeChange}
            onApplicationTypeChange={onApplicationTypeChange}
            disabled={running}
          />
      </div>
      <div className="solo-application-slot">
          <TypeSelector
            only="application"
            applicationTypes={applicationTypes}
            housingType={housingType}
            applicationType={applicationType}
            onHousingTypeChange={onHousingTypeChange}
            onApplicationTypeChange={onApplicationTypeChange}
            disabled={running}
          />
      </div>

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

      <div className="solo-run-slot">
        {hasSite ? (
          <>
                {/* 결과가 나오면 같은 자리의 버튼이 「상세 결과」가 된다. 다시 돌리려면 「다시 검색」. */}
                {screeningResult && !running ? (
                  <button
                    type="button"
                    className="solo-run-button"
                    onClick={onOpenSheet}
                    title="1차 매입제외 판정과 2차 배점 근거를 심사표로 봅니다."
                  >
                    상세 결과
                  </button>
                ) : (
                  <button
                    type="button"
                    className={`solo-run-button${runAttention ? " is-attention" : ""}`}
                    disabled={running}
                    onClick={onRun}
                    title="1차 매입제외 판정과 2차 생활편의성 배점을 실행합니다."
                  >
                    {running ? "심사 중" : "심사 실행"}
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
      <div className="solo-reset-slot">
        {hasSite && (
                <button
                  type="button"
                  className="solo-reset-button"
                  disabled={running}
                  onClick={onResetSite}
                  title="사업지를 비우고 다시 검색"
                >
                  다시 검색
                </button>
        )}
      </div>

      <div className="solo-bar-actions">
          <ServerWakeNotice kakaoNotice={kakaoNotice} />
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

          {API_PANEL_ENABLED && (
            <button
              type="button"
              className={`solo-icon-button ${apiOpen ? "is-active" : ""}`}
              onClick={() => setApiOpen(true)}
              aria-label="API 연결"
              title="API 연결 · 서버에 실은 파일"
            >
              <Plug size={18} />
            </button>
          )}

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
            aria-label="관리자 설정"
            title="관리자 설정 — 임계거리 · 2027 완화 기준 · LH 개별 확인 제외"
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
      {/* 좁은 화면에서만 보이는 상태 안내 자리(2행 오른쪽). 넓으면 아이콘 왼쪽에 보인다. */}
      <div className="solo-status-slot">
        <ServerWakeNotice kakaoNotice={kakaoNotice} />
      </div>
      <div className="solo-site-slot">
        {hasSite && siteSummary && (
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
                    <button type="button" onClick={siteSummary.onReset} title="고른 필지를 비우고 대표 필지부터 다시 고릅니다.">
                      필지 초기화
                    </button>
                  )}
                </>
              ) : (
                <span className="is-warning">필지 없음 — 지도에서 필지를 고르세요</span>
              )}
            </div>
        )}
      </div>
      {API_PANEL_ENABLED && (
        <ApiKeysPanel open={apiOpen} onClose={() => setApiOpen(false)} />
      )}
      <RulebookModal open={rulebookOpen} onClose={() => setRulebookOpen(false)} rulePack={rulePack} />
      <RulesPanel open={rulesOpen} onClose={() => setRulesOpen(false)} />
    </header>
  );
}
