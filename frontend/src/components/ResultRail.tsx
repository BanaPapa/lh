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
import type { ScreeningResult } from "../screening/types";

type SectionTone = "neutral" | "ready" | "warning" | "danger";

/** 심사 종합 판정 → 결과 레일 섹션 헤더 톤. */
function screeningSectionTone(verdict: ScreeningResult["verdict"]): SectionTone {
  if (verdict === "fail") return "danger";
  if (verdict === "review") return "warning";
  return "ready";
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

interface ResultRailProps {
  site: GeocodeCandidate;
  onCollapse: () => void;
  screeningResult: ScreeningResult | null;
  screeningRunning: boolean;
  screeningError: string;
  onOpenSheet: () => void;
  onRerun: () => void;
}

/**
 * 화면 우측 결과 레일. 심사가 돌기 시작한 뒤에만 뜬다 — 사업지·필지 요약은 상단 바의
 * 주소 옆 칩이, 신청유형·임계거리는 상단 바의 유형 선택이 맡는다. 지도를 보면서 결과를
 * 읽어야 하는 도구라 결과만 옆에 두고, 지도는 남은 폭을 그대로 쓴다.
 */
export function ResultRail({
  site,
  onCollapse,
  screeningResult,
  screeningRunning,
  screeningError,
  onOpenSheet,
  onRerun,
}: ResultRailProps) {
  const [open, setOpen] = useState(true);
  // 새 심사가 끝나면 결과 섹션을 펼쳐 준다.
  useEffect(() => {
    setOpen(true);
  }, [screeningResult?.screening_id]);

  return (
    <aside className="result-rail" aria-label="심사 결과">
      <header>
        <div>
          <h2>심사 결과</h2>
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
            screeningResult ? screeningSectionTone(screeningResult.verdict) : "neutral"
          }
          open={open}
          onToggle={() => setOpen((current) => !current)}
        >
          {screeningResult ? (
            <>
              <p className="result-note">{screeningResult.verdict_summary}</p>
              <p className="result-footnote">
                생활편의성{" "}
                {screeningResult.stage_two.determined
                  ? `${screeningResult.stage_two.living_score ?? "—"} / ${
                      screeningResult.stage_two.living_maximum
                    }점`
                  : `${screeningResult.stage_two.living_score_min}~${screeningResult.stage_two.living_score_max}점 (확정 불가)`}
              </p>
              <button type="button" className="result-detail-button" onClick={onOpenSheet}>
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
      </div>
    </aside>
  );
}
