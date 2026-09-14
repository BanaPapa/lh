import { ChevronRight, KeyRound, Settings2 } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { MapProvider } from "../types";
import { ApiKeysPanel } from "./ApiKeysPanel";

interface SettingsMenuProps {
  mapProvider: MapProvider;
  onMapProviderChange: (provider: MapProvider) => void;
}

/**
 * 상단 우측 설정 메뉴.
 * 지도 유형처럼 한 번 정해 두고 자주 바꾸지 않는 값과 API 키 설정을 모은다.
 */
export function SettingsMenu({
  mapProvider,
  onMapProviderChange,
}: SettingsMenuProps) {
  const [open, setOpen] = useState(false);
  const [apiKeysOpen, setApiKeysOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const panelId = useId();

  // 바깥을 누르거나 Esc 를 누르면 닫는다.
  useEffect(() => {
    if (!open) return;

    const handlePointerDown = (event: MouseEvent) => {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  return (
    <div className="solo-settings" ref={wrapperRef}>
      <button
        type="button"
        className={`solo-icon-button ${open ? "is-active" : ""}`}
        onClick={() => setOpen((current) => !current)}
        aria-expanded={open}
        aria-controls={panelId}
        aria-label="설정"
        title="설정"
      >
        <Settings2 size={18} />
      </button>

      {open && (
        <div className="solo-settings-panel" id={panelId} role="dialog" aria-label="설정">
          <section className="solo-settings-section">
            <header>
              <h3>지도</h3>
            </header>
            <div className="solo-segmented" role="group" aria-label="지도 유형">
              <button
                type="button"
                className={mapProvider === "kakao" ? "is-active" : ""}
                aria-pressed={mapProvider === "kakao"}
                onClick={() => onMapProviderChange("kakao")}
              >
                카카오
              </button>
              <button
                type="button"
                className={mapProvider === "naver" ? "is-active" : ""}
                aria-pressed={mapProvider === "naver"}
                onClick={() => onMapProviderChange("naver")}
              >
                네이버
              </button>
            </div>
          </section>

          <section className="solo-settings-section">
            <header>
              <h3>API 연결</h3>
              <p>지도·데이터 키를 넣습니다. 로컬에서만 씁니다.</p>
            </header>
            <button
              type="button"
              className="solo-settings-link"
              onClick={() => {
                setApiKeysOpen(true);
                setOpen(false);
              }}
            >
              <KeyRound size={16} aria-hidden="true" />
              <span>
                <strong>API 키 설정</strong>
                <small>브라우저 키·서버 키 관리</small>
              </span>
              <ChevronRight size={15} aria-hidden="true" />
            </button>
          </section>
        </div>
      )}

      <ApiKeysPanel open={apiKeysOpen} onClose={() => setApiKeysOpen(false)} />
    </div>
  );
}
