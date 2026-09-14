import { ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ScreeningJobStatus } from "./types";

/**
 * 심사 진행 모달.
 *
 * 좌: 단계별 진행률 막대 + 상세 로그(접힘) · 우: 라이브 카운트(확인한 건수)와
 * 중지 버튼. 심사가 끝나면 App 이 모달을 내리고 결과 레일이 이어받으므로
 * 완료 화면은 없다.
 */
interface ScreeningProgressModalProps {
  open: boolean;
  progress: ScreeningJobStatus | null;
  siteName?: string;
  onStop: () => void;
}

function unitValue(item: ScreeningJobStatus["items"][number]): string {
  if (item.status === "completed") {
    return item.count !== null ? `${item.count.toLocaleString()}건` : "완료";
  }
  if (item.status === "pending") return "대기";
  if (item.status === "failed") return "실패";
  return `${item.progress}%`;
}

function unitPrefix(status: ScreeningJobStatus["items"][number]["status"]): string {
  if (status === "running") return "▸ ";
  if (status === "completed") return "✓ ";
  if (status === "failed") return "✕ ";
  return "";
}

export function ScreeningProgressModal({
  open,
  progress,
  siteName,
  onStop,
}: ScreeningProgressModalProps) {
  const [logOpen, setLogOpen] = useState(false);
  const logBodyRef = useRef<HTMLDivElement>(null);
  const activeUnitRef = useRef<HTMLLIElement>(null);
  const items = progress?.items ?? [];
  const completedCount = items.filter(
    (item) => item.status === "completed",
  ).length;
  const evidenceCount = items.reduce((sum, item) => sum + (item.count ?? 0), 0);
  const logs = items.filter((item) => item.status !== "pending");

  useEffect(() => {
    if (logOpen && logBodyRef.current) {
      logBodyRef.current.scrollTop = logBodyRef.current.scrollHeight;
    }
  }, [items, logOpen]);

  useEffect(() => {
    activeUnitRef.current?.scrollIntoView({ block: "nearest" });
  }, [progress?.stage]);

  if (!open) return null;

  return createPortal(
    <div className="sp-overlay">
      <section
        className={`sp-card${logOpen ? " is-log-open" : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label="심사 진행"
      >
        <div className="sp-body">
          {/* 좌: 단계별 진행률 */}
          <div className="sp-left">
            <div className="sp-head">
              <b>심사 진행{siteName ? ` · ${siteName}` : ""}</b>
              <span className="sp-frac">
                {completedCount} / {items.length || 3}
              </span>
            </div>

            <ol className="sp-list">
              {items.length === 0 && (
                <li className="sp-unit is-pending">
                  <div className="sp-unit-row">
                    <span className="sp-nm">심사 작업을 준비하고 있습니다</span>
                    <span className="sp-ct">대기</span>
                  </div>
                  <div className="sp-bar">
                    <i style={{ width: "0%" }} />
                  </div>
                </li>
              )}
              {items.map((item) => (
                <li
                  key={item.id}
                  ref={item.status === "running" ? activeUnitRef : undefined}
                  className={`sp-unit is-${item.status}${
                    item.status === "completed" && item.count === 0 ? " is-empty" : ""
                  }`}
                >
                  <div className="sp-unit-row">
                    <span className="sp-nm">
                      {unitPrefix(item.status)}
                      {item.label}
                    </span>
                    <span className="sp-ct">{unitValue(item)}</span>
                  </div>
                  <div className="sp-bar">
                    <i
                      style={{
                        width: `${item.status === "completed" ? 100 : item.progress}%`,
                      }}
                    />
                  </div>
                </li>
              ))}
            </ol>

            <div className="sp-log-wrap">
              <button
                type="button"
                className="sp-log-toggle"
                aria-expanded={logOpen}
                onClick={() => setLogOpen((current) => !current)}
              >
                <ChevronDown size={15} className={logOpen ? "is-open" : ""} />
                상세 로그
                <span className="sp-log-count">{logs.length || ""}</span>
              </button>
              {logOpen && (
                <div className="sp-log-body" ref={logBodyRef}>
                  {logs.length === 0 ? (
                    <p className="sp-log-empty">로그가 없습니다</p>
                  ) : (
                    logs.map((item) => (
                      <p
                        key={`${item.id}-${item.status}`}
                        className={`sp-log-entry is-${item.status}`}
                      >
                        <span className="sp-log-ic" aria-hidden="true">
                          {item.status === "completed"
                            ? "✓"
                            : item.status === "failed"
                              ? "✕"
                              : "●"}
                        </span>
                        <span className="sp-log-msg">
                          <b>{item.label}</b>
                          {item.message && <small>{item.message}</small>}
                        </span>
                      </p>
                    ))
                  )}
                </div>
              )}
            </div>
          </div>

          {/* 우: 라이브 카운트 */}
          <div className="sp-right">
            <div className="sp-live">
              <span className="sp-spin" aria-hidden="true" />
              <div className="sp-live-v">{evidenceCount.toLocaleString()}</div>
              <div className="sp-live-l">건 확인 중…</div>
              <div className="sp-live-frac">
                {progress?.stage || "준비 중"}
              </div>
              <button type="button" className="sp-stop-btn" onClick={onStop}>
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <rect x="6" y="6" width="12" height="12" rx="1.5" />
                </svg>
                심사 중지
              </button>
            </div>
          </div>
        </div>
      </section>
    </div>,
    document.body,
  );
}
