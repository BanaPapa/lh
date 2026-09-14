import { ChevronDown, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { ScreeningJobStatus } from "./types";

/**
 * 심사 진행 화면.
 *
 * 예전에는 유해시설·심사 두 모듈이 같은 모달을 나눠 썼고, 그래서 제목이
 * 「유해시설 검토 진행 중」으로 고정돼 심사를 돌려도 유해시설이라 적혔다.
 * 실행 경로가 심사 하나로 줄면서 이 모달도 심사 것이 되었다.
 *
 * 표현은 리포트(report.css)와 같은 규격을 쓴다. 대문자 영문 눈썹
 * 문구, 주황 진행바, 원형 스피너를 걷어내고 단계 목록과 로그만 남긴다.
 */
interface ScreeningProgressModalProps {
  open: boolean;
  progress: ScreeningJobStatus | null;
  siteName?: string;
  onStop: () => void;
}

export function ScreeningProgressModal({
  open,
  progress,
  siteName,
  onStop,
}: ScreeningProgressModalProps) {
  const [logOpen, setLogOpen] = useState(true);
  const logBodyRef = useRef<HTMLDivElement>(null);
  const activeUnitRef = useRef<HTMLLIElement>(null);
  const items = progress?.items ?? [];
  const completedCount = items.filter(
    (item) => item.status === "completed",
  ).length;
  const evidenceCount = items.reduce((sum, item) => sum + (item.count ?? 0), 0);
  const percent = Math.max(progress?.progress ?? 2, 2);

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
    <div className="analysis-modal-backdrop screening-progress-backdrop">
      <section
        className="analysis-modal is-loading screening-progress-modal"
        role="dialog"
        aria-modal="true"
        aria-label="서류심사 진행"
      >
        <header className="analysis-modal-header">
          <div className="analysis-modal-title">
            <span>LH 신축매입약정 서류심사</span>
            <strong>{siteName || "심사 진행 중"}</strong>
          </div>
          <div className="analysis-modal-actions">
            <button
              type="button"
              className="modal-close"
              aria-label="심사 중단"
              onClick={onStop}
            >
              <X size={20} />
            </button>
          </div>
        </header>

        <div className="progress-body">
          <div className="progress-headline">
            <p className="progress-stage">
              {progress?.stage || "심사 작업을 준비하고 있습니다."}
            </p>
            <p className="progress-message">
              {progress?.message ||
                "신청필지와 규칙팩을 확인한 뒤 1차 판정과 2차 배점을 차례로 계산합니다."}
            </p>
            <div
              className="progress-meter"
              role="progressbar"
              aria-valuenow={percent}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="전체 진행률"
            >
              <i style={{ width: `${percent}%` }} />
            </div>
            <dl className="progress-facts">
              <div>
                <dt>전체 진행률</dt>
                <dd>{percent}%</dd>
              </div>
              <div>
                <dt>완료 단계</dt>
                <dd>
                  {completedCount} / {items.length || 1}
                </dd>
              </div>
              <div>
                <dt>확인한 건수</dt>
                <dd>{evidenceCount.toLocaleString()}</dd>
              </div>
            </dl>
          </div>

          <ol className="progress-steps">
            {items.map((item) => (
              <li
                key={item.id}
                ref={
                  item.status === "running" && item.label === progress?.stage
                    ? activeUnitRef
                    : undefined
                }
                className={`is-${item.status}`}
              >
                <span className="progress-step-label">{item.label}</span>
                <span className="progress-step-value">
                  {item.status === "completed" && item.count !== null
                    ? `${item.count.toLocaleString()}건`
                    : item.status === "pending"
                      ? "대기"
                      : item.status === "failed"
                        ? "실패"
                        : `${item.progress}%`}
                </span>
                <i>
                  <span style={{ width: `${item.progress}%` }} />
                </i>
              </li>
            ))}
          </ol>

          <div className="progress-log">
            <button
              type="button"
              className="progress-log-toggle"
              aria-expanded={logOpen}
              onClick={() => setLogOpen((current) => !current)}
            >
              <ChevronDown size={14} className={logOpen ? "is-open" : ""} />
              상세 로그
              <b>{items.filter((item) => item.status !== "pending").length}</b>
            </button>
            {logOpen && (
              <div className="progress-log-body" ref={logBodyRef}>
                {items
                  .filter((item) => item.status !== "pending")
                  .map((item) => (
                    <p
                      key={`${item.id}-${item.status}`}
                      className={`log-${item.status}`}
                    >
                      <span>{item.label}</span>
                      {item.message && <small>{item.message}</small>}
                    </p>
                  ))}
              </div>
            )}
          </div>

          <footer className="progress-actions">
            <button type="button" onClick={onStop}>
              <Square size={11} fill="currentColor" aria-hidden="true" />
              심사 중지
            </button>
          </footer>
        </div>
      </section>
    </div>,
    document.body,
  );
}
