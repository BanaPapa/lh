import { BookOpen, X } from "lucide-react";
import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  APPLY_MODE_LABEL,
  RULEBOOK_GROUPS,
  type RulebookItem,
} from "./rulebookContent";
import "../rulebook.css";

interface RulebookModalProps {
  open: boolean;
  onClose: () => void;
}

const FIRST_ITEM_ID = RULEBOOK_GROUPS[0].items[0].id;

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
export function RulebookModal({ open, onClose }: RulebookModalProps) {
  const [selectedId, setSelectedId] = useState(FIRST_ITEM_ID);

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

        <div className="rulebook-body">
          <nav className="rulebook-nav" aria-label="판단 항목">
            {RULEBOOK_GROUPS.map((group) => (
              <div className="rulebook-nav-group" key={group.id}>
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
            ))}
          </nav>

          <article className="rulebook-content" key={item.id}>
            <header className="rulebook-content-head">
              <h3>{item.title}</h3>
              <span className="rulebook-tag">{item.tag}</span>
            </header>

            <section className="rulebook-section">
              <h4>LH 기준</h4>
              {item.lhCriteria.map((paragraph, index) => (
                <p key={index}>{paragraph}</p>
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
                <p key={index}>{paragraph}</p>
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
