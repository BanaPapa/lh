import { Settings2 } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import type { MapProvider } from "../types";

interface SettingsMenuProps {
  mapProvider: MapProvider;
  onMapProviderChange: (provider: MapProvider) => void;
}

/**
 * 상단 우측 설정 메뉴.
 * 지도 유형처럼 한 번 정해 두고 자주 바꾸지 않는 값을 모은다. API 연결은 상단 바의
 * 전용 버튼(TopSearchBar)이 연다.
 */
export function SettingsMenu({
  mapProvider,
  onMapProviderChange,
}: SettingsMenuProps) {
  const [open, setOpen] = useState(false);
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

        </div>
      )}
    </div>
  );
}
