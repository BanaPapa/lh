import { ClipboardCheck, Moon, Printer, Search, Sun } from "lucide-react";
import type { MapProvider } from "../types";
import type { ThemeMode } from "../theme";
import { SettingsMenu } from "./SettingsMenu";

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
  running: boolean;
  canPrint: boolean;
  theme: ThemeMode;
  onToggleTheme: () => void;
}

/**
 * 상단 바 한 줄. 사업지 검색과 곧바로 이어지는 심사 실행을 붙여 놓고,
 * 사업지가 잡히기 전에는 실행 버튼을 잠가 순서를 드러낸다.
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
  running,
  canPrint,
  theme,
  onToggleTheme,
}: TopSearchBarProps) {
  return (
    <header className="solo-topbar">
      <div className="solo-bar-primary">
        <div className="solo-brand">
          <ClipboardCheck size={19} aria-hidden="true" />
          <span>LH 서류심사</span>
        </div>

        <div className="solo-search-shell">
          <div className="solo-search-row">
            <form
              className="solo-search-form"
              onSubmit={(event) => {
                event.preventDefault();
                onSearch();
              }}
            >
              <Search size={17} aria-hidden="true" />
              <input
                aria-label="사업지 주소 또는 장소"
                value={query}
                onChange={(event) => onQueryChange(event.target.value)}
                placeholder="주소, 건물명, 역명으로 사업지 검색"
              />
              <button
                type="submit"
                disabled={searching || query.trim().length < 2}
              >
                {searching ? "검색 중" : "검색"}
              </button>
            </form>

            {/* 검색 다음에 오는 동작이라 검색 옆에 둔다. 사업지가 잡히기
                전에는 누를 수 없다. */}
            <button
              type="button"
              className="solo-run-button"
              disabled={!hasSite || running}
              onClick={onRun}
              title={
                hasSite
                  ? "1차 매입제외 판정과 2차 생활편의성 배점을 실행합니다."
                  : "사업지를 먼저 검색하세요."
              }
            >
              {running ? "심사 중" : "심사 실행"}
            </button>
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
    </header>
  );
}
