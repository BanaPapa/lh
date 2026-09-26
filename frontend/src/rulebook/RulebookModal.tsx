import { BookOpen, ShieldCheck, X } from "lucide-react";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  APPLY_MODE_LABEL,
  RULEBOOK_GROUPS,
  type RulebookItem,
  type RulebookParagraph,
} from "./rulebookContent";
import type { HazardRulePack } from "../hazard-review/types";
import "../rulebook.css";

interface RulebookModalProps {
  open: boolean;
  onClose: () => void;
  /** 지금 판정에 쓰는 규칙팩. 머리글에 버전·적용일을 보인다. */
  rulePack?: HazardRulePack | null;
}

const FIRST_ITEM_ID = RULEBOOK_GROUPS[0].items[0].id;

/** 상단 탭 = 그룹 하나(1차 · 2차 · 수기). 왼쪽 목록은 고른 탭의 항목만 보인다. */
const TAB_LABELS: Record<string, string> = {
  stage1: "1차 매입제외",
  stage2: "2차 생활편의성",
  manual: "자동 판정 아닌 항목",
};

/** 문장 끝(마침표 + 공백)에서 잘라 문장마다 한 줄로 만든다. 괄호 안 마침표는 건드리지 않는다. */
function splitSentences(text: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let current = "";
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === "(") depth += 1;
    if (ch === ")") depth = Math.max(0, depth - 1);
    current += ch;
    if (ch === "." && depth === 0 && (text[i + 1] === " " || i === text.length - 1)) {
      out.push(current.trim());
      current = "";
      i += 1; // 뒤따르는 공백을 건너뛴다
    }
  }
  if (current.trim()) out.push(current.trim());
  return out;
}

function Paragraph({ value }: { value: RulebookParagraph }) {
  if (typeof value === "string") {
    return (
      <div className="rulebook-para">
        {splitSentences(value).map((sentence, index) => (
          <p key={index}>{sentence}</p>
        ))}
      </div>
    );
  }
  return (
    <div className="rulebook-para">
      {value.lead && <p>{value.lead}</p>}
      <ul className="rulebook-list">
        {value.items.map((entry, index) => (
          <li key={index}>{entry}</li>
        ))}
      </ul>
    </div>
  );
}

function findItem(id: string): RulebookItem {
  for (const group of RULEBOOK_GROUPS) {
    const hit = group.items.find((item) => item.id === id);
    if (hit) return hit;
  }
  return RULEBOOK_GROUPS[0].items[0];
}

/**
 * 룰북 모달 — 왼쪽은 LH 가 판단하는 항목 목록, 오른쪽은 고른 항목의
 * ① LH 기준 ② 이 앱의 적용 ③ 데이터 원천. 문단마다 행간을 넓혀 구분한다.
 */
export function RulebookModal({ open, onClose, rulePack = null }: RulebookModalProps) {
  const [selectedId, setSelectedId] = useState(FIRST_ITEM_ID);
  const [groupId, setGroupId] = useState(RULEBOOK_GROUPS[0].id);
  const group =
    RULEBOOK_GROUPS.find((entry) => entry.id === groupId) ?? RULEBOOK_GROUPS[0];

  const selectGroup = (id: string) => {
    const next = RULEBOOK_GROUPS.find((entry) => entry.id === id);
    if (!next) return;
    setGroupId(id);
    setSelectedId(next.items[0].id);
  };

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const item = findItem(selectedId);

  return createPortal(
    <div
      className="api-keys-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className="rulebook-modal"
        role="dialog"
        aria-modal="true"
        aria-label="심사 룰북"
      >
        <header className="rulebook-head">
          <div>
            <h2>
              <BookOpen size={18} aria-hidden="true" /> 심사 룰북
            </h2>
            <p>LH 가 판단하는 항목마다 LH 기준 · 이 앱의 적용 · 데이터 원천을 적었습니다.</p>
            {rulePack && (
              <p className="rulebook-pack">
                <ShieldCheck size={14} aria-hidden="true" />
                <strong>{rulePack.title}</strong>
                <span>v{rulePack.version}</span>
                <span>{rulePack.effective_from} 적용</span>
                <span>{rulePack.status === "approved" ? "승인본" : rulePack.status}</span>
                <em>현재 규칙팩</em>
              </p>
            )}
          </div>
          <button
            type="button"
            className="api-keys-close"
            onClick={onClose}
            aria-label="닫기"
          >
            <X size={18} />
          </button>
        </header>
        <div className="api-keys-tabs" role="tablist" aria-label="심사 단계">
          {RULEBOOK_GROUPS.map((entry) => (
            <button
              key={entry.id}
              type="button"
              role="tab"
              aria-selected={entry.id === groupId}
              className={entry.id === groupId ? "is-active" : ""}
              onClick={() => selectGroup(entry.id)}
            >
              {TAB_LABELS[entry.id] ?? entry.title}
            </button>
          ))}
        </div>

        <div className="rulebook-body">
          <nav className="rulebook-nav" aria-label="판단 항목">
            <div className="rulebook-nav-group">
              <h3>{group.title}</h3>
              <ul>
                {group.items.map((entry) => (
                  <li key={entry.id}>
                    <button
                      type="button"
                      className={entry.id === selectedId ? "is-active" : ""}
                      aria-current={entry.id === selectedId ? "true" : undefined}
                      onClick={() => setSelectedId(entry.id)}
                    >
                      <span>{entry.title}</span>
                      <small>{entry.tag}</small>
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          </nav>

          <article className="rulebook-content" key={item.id}>
            <header className="rulebook-content-head">
              <h3>{item.title}</h3>
              <span className="rulebook-tag">{item.tag}</span>
            </header>

            <section className="rulebook-section">
              <h4>LH 기준</h4>
              {item.lhCriteria.map((paragraph, index) => (
                <Paragraph key={index} value={paragraph} />
              ))}
            </section>

            <section className="rulebook-section">
              <h4>
                이 앱의 적용
                <span className={`rulebook-mode is-${item.apply.mode}`}>
                  {APPLY_MODE_LABEL[item.apply.mode]}
                </span>
              </h4>
              {item.apply.paragraphs.map((paragraph, index) => (
                <Paragraph key={index} value={paragraph} />
              ))}
            </section>

            <section className="rulebook-section">
              <h4>데이터 원천</h4>
              <ul className="rulebook-sources">
                {item.sources.map((source) => (
                  <li key={source.name}>
                    <div className="rulebook-source-head">
                      <strong>{source.name}</strong>
                      <span className="rulebook-kind">{source.kind}</span>
                    </div>
                    <p>{source.note}</p>
                  </li>
                ))}
              </ul>
            </section>
          </article>
        </div>
      </section>
    </div>,
    document.body,
  );
}
