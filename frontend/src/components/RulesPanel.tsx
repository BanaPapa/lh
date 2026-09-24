import { Plus, RotateCcw, SlidersHorizontal, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import {
  getRules,
  updateRules,
  type ExcludedFacility,
  type RulesResponse,
} from "../api";
import "../rules.css";

interface RulesPanelProps {
  open: boolean;
  onClose: () => void;
}

type RulesTab = "stage1" | "relaxed" | "excluded";

const TAB_LABELS: Record<RulesTab, string> = {
  stage1: "1차 임계거리",
  relaxed: "2027 완화 기준",
  excluded: "LH 개별 확인 제외",
};

/** 입력칸 문자열 ↔ 값. 빈 칸은 「미적용」(null). */
function parseCell(text: string): number | null | undefined {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const number = Number(trimmed);
  if (!Number.isInteger(number) || number < 0 || number > 5000) return undefined;
  return number;
}

/**
 * 관리자 「기준 편집」 — 09/22 회의 결정 6.
 * 거리 숫자는 여기서 고치고 저장한다(정본 기본값은 늘 보관·복원 가능). 조건(AND/OR)
 * 편집 UI 는 두지 않고 2027 완화 기준은 토글 하나로 켠다. LH 가 개별 확인해 판정에서
 * 뺀 시설 목록도 여기서 관리한다. 설정 API 와 같이 로컬(루프백)에서만 동작한다.
 */
export function RulesPanel({ open, onClose }: RulesPanelProps) {
  const [data, setData] = useState<RulesResponse | null>(null);
  const [tab, setTab] = useState<RulesTab>("stage1");
  // 편집 중인 값(칸 키 → 입력 문자열). 저장 전까지는 화면에만 있다.
  const [cells, setCells] = useState<Record<string, string>>({});
  const [relaxed, setRelaxed] = useState(false);
  const [excluded, setExcluded] = useState<ExcludedFacility[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const next = await getRules();
      setData(next);
      setCells(
        Object.fromEntries(next.cells.map((cell) => [cell.key, cell.value === null ? "" : String(cell.value)])),
      );
      setRelaxed(next.config.relaxed_2027);
      setExcluded(next.config.excluded_facilities.map((item) => ({ ...item })));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "기준을 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setNotice("");
    void load();
  }, [open, load]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const cellByKey = useMemo(
    () => new Map((data?.cells ?? []).map((cell) => [cell.key, cell])),
    [data],
  );

  const invalidKeys = useMemo(
    () => Object.entries(cells).filter(([, text]) => parseCell(text) === undefined).map(([key]) => key),
    [cells],
  );

  const changedCount = useMemo(
    () =>
      Object.entries(cells).filter(([key, text]) => {
        const cell = cellByKey.get(key);
        if (!cell) return false;
        const value = parseCell(text);
        return value !== undefined && value !== cell.default;
      }).length,
    [cells, cellByKey],
  );

  if (!open) return null;

  const handleSave = async () => {
    if (!data || invalidKeys.length) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const thresholds: Record<string, number | null> = {};
      for (const [key, text] of Object.entries(cells)) {
        const cell = cellByKey.get(key);
        const value = parseCell(text);
        if (!cell || value === undefined || value === cell.default) continue;
        thresholds[key] = value;
      }
      const next = await updateRules({
        stage1_thresholds: thresholds,
        relaxed_2027: relaxed,
        excluded_facilities: excluded.filter((item) => item.name.trim()),
      });
      setData(next);
      setCells(
        Object.fromEntries(next.cells.map((cell) => [cell.key, cell.value === null ? "" : String(cell.value)])),
      );
      setExcluded(next.config.excluded_facilities.map((item) => ({ ...item })));
      setNotice("저장했습니다. 다음 심사부터 반영됩니다.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "저장하지 못했습니다.");
    } finally {
      setSaving(false);
    }
  };

  const resetAllCells = () => {
    if (!data) return;
    setCells(
      Object.fromEntries(data.cells.map((cell) => [cell.key, cell.default === null ? "" : String(cell.default)])),
    );
  };

  return createPortal(
    <div
      className="api-keys-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section className="rules-modal" role="dialog" aria-modal="true" aria-label="기준 편집">
        <header className="rules-head">
          <div>
            <h2>
              <SlidersHorizontal size={22} aria-hidden="true" />
              기준 편집
            </h2>
            <p>
              거리 숫자와 완화 기준을 여기서 고칩니다. 룰북 정본 값은 늘 보관되며 「기본값」으로
              되돌릴 수 있습니다. 저장한 값은 다음 심사부터 적용됩니다(로컬 전용).
            </p>
          </div>
          <button type="button" className="api-keys-close" onClick={onClose} aria-label="닫기">
            <X size={18} />
          </button>
        </header>

        <nav className="rules-tabs" role="tablist">
          {(Object.keys(TAB_LABELS) as RulesTab[]).map((key) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              className={tab === key ? "is-active" : ""}
              onClick={() => setTab(key)}
            >
              {TAB_LABELS[key]}
              {key === "stage1" && changedCount > 0 && <b>{changedCount}</b>}
              {key === "relaxed" && relaxed && <b>ON</b>}
              {key === "excluded" && excluded.length > 0 && <b>{excluded.length}</b>}
            </button>
          ))}
        </nav>

        {(error || notice) && (
          <p className={`rules-message ${error ? "is-error" : "is-notice"}`} role="status">
            {error || notice}
          </p>
        )}

        <div className="rules-body">
          {loading && <p className="rules-empty">불러오는 중…</p>}

          {!loading && data && tab === "stage1" && (
            <section className="rules-section">
              <header>
                <div>
                  <h3>1차 매입제외 임계거리 (m)</h3>
                  <p>
                    주택유형·신청유형 조합마다 Rule 별 기준거리입니다. 빈 칸은 「미적용」, 정본과 다른
                    칸은 색으로 표시됩니다. 판정 여유구간(100m)은 여기서 바꾸지 않습니다.
                  </p>
                </div>
                <button type="button" className="rules-ghost" onClick={resetAllCells}>
                  <RotateCcw size={14} aria-hidden="true" />
                  전부 기본값
                </button>
              </header>
              <div className="rules-table-scroll">
                <table className="rules-table">
                  <thead>
                    <tr>
                      <th className="is-left">조합</th>
                      {data.rules.map((rule) => (
                        <th key={rule.rule_id}>
                          {rule.label}
                          <small>{rule.column}</small>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.combos.map((combo) => (
                      <tr key={`${combo.housing_type}:${combo.application_type}`}>
                        <th scope="row" className="is-left">
                          {combo.housing_label}
                          <small>{combo.application_label}</small>
                        </th>
                        {data.rules.map((rule) => {
                          const key = `${combo.housing_type}:${combo.application_type}:${rule.column}`;
                          const cell = cellByKey.get(key);
                          const text = cells[key] ?? "";
                          const value = parseCell(text);
                          const invalid = value === undefined;
                          const changed = !invalid && cell !== undefined && value !== cell.default;
                          return (
                            <td key={key} className={`${changed ? "is-changed" : ""} ${invalid ? "is-invalid" : ""}`}>
                              <input
                                inputMode="numeric"
                                value={text}
                                placeholder="미적용"
                                aria-label={`${combo.housing_label} ${combo.application_label} ${rule.label}`}
                                onChange={(event) => setCells((prev) => ({ ...prev, [key]: event.target.value }))}
                              />
                              {changed && cell && (
                                <button
                                  type="button"
                                  className="rules-cell-reset"
                                  title={`기본값 ${cell.default === null ? "미적용" : `${cell.default}m`}`}
                                  onClick={() =>
                                    setCells((prev) => ({
                                      ...prev,
                                      [key]: cell.default === null ? "" : String(cell.default),
                                    }))
                                  }
                                >
                                  기본 {cell.default === null ? "—" : cell.default}
                                </button>
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {!loading && data && tab === "relaxed" && (
            <section className="rules-section">
              <header>
                <div>
                  <h3>2027 서류심사 기준 완화안</h3>
                  <p>LH 「서류심사 기준 완화」(09/22 전달) 세 가지를 한 번에 켜고 끕니다.</p>
                </div>
              </header>
              <label className="rules-toggle">
                <input type="checkbox" checked={relaxed} onChange={(event) => setRelaxed(event.target.checked)} />
                <span>{relaxed ? "완화 기준 적용 중" : "현행 기준 적용 중"}</span>
              </label>
              <ul className="rules-list">
                <li>
                  <b>❸ 통과기준</b> 100점 만점 기준 {data.default_pass_threshold}점 → {data.relaxed_pass_threshold}점
                  (매입심의 140/200점은 그대로).
                </li>
                <li>
                  <b>ㅇ 교육여건</b> 100% 등급 「1km 초·중·고 모두 <em>하고</em> 500m 초·중 모두」 → 「…<em>하거나</em>…」,
                  80% 등급 「1.5km 초·중·고 모두 <em>하고</em> 1km 초등」 → 「…<em>하거나</em>…」. 60% 이하는 현행과 같음.
                </li>
                <li>
                  <b>❹ 가산점 확대</b> 역세권 가점(5점)을 대학교 정문 500m 이내까지 확대. 청년·기숙사형은 대학교 인근
                  가산 10점. 생활편의 40점은 그대로 두고 가점만 따로 표시.
                </li>
              </ul>
            </section>
          )}

          {!loading && data && tab === "excluded" && (
            <section className="rules-section">
              <header>
                <div>
                  <h3>LH 개별 확인으로 판정에서 뺀 시설</h3>
                  <p>
                    LH 가 현장·서류로 확인해 판정 대상이 아니라고 회신한 시설(예: 철거된 주유소)입니다.
                    이름이 일치하는 시설은 1차 판정 후보에서 빠지고, 뺀 사실은 심사표 근거에 남습니다.
                  </p>
                </div>
                <button
                  type="button"
                  className="rules-ghost"
                  onClick={() => setExcluded((prev) => [...prev, { name: "", reason: "" }])}
                >
                  <Plus size={14} aria-hidden="true" />
                  시설 추가
                </button>
              </header>
              {excluded.length === 0 ? (
                <p className="rules-empty">등록된 시설이 없습니다.</p>
              ) : (
                <table className="rules-table rules-excluded">
                  <thead>
                    <tr>
                      <th className="is-left">시설명</th>
                      <th className="is-left">사유(LH 회신)</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {excluded.map((item, index) => (
                      <tr key={index}>
                        <td>
                          <input
                            value={item.name}
                            placeholder="예: SK신흥주유소"
                            onChange={(event) =>
                              setExcluded((prev) =>
                                prev.map((row, i) => (i === index ? { ...row, name: event.target.value } : row)),
                              )
                            }
                          />
                        </td>
                        <td>
                          <input
                            value={item.reason}
                            placeholder="예: LH 09/11 회의 — 주유소 철거 확인(신청 082)"
                            onChange={(event) =>
                              setExcluded((prev) =>
                                prev.map((row, i) => (i === index ? { ...row, reason: event.target.value } : row)),
                              )
                            }
                          />
                        </td>
                        <td>
                          <button
                            type="button"
                            className="rules-ghost is-danger"
                            aria-label="삭제"
                            onClick={() => setExcluded((prev) => prev.filter((_, i) => i !== index))}
                          >
                            <Trash2 size={14} aria-hidden="true" />
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          )}
        </div>

        <footer className="rules-foot">
          <small>
            {data?.config.updated_at
              ? `마지막 저장 ${new Date(data.config.updated_at).toLocaleString("ko-KR")}`
              : "저장된 편집값 없음 — 룰북 정본 그대로"}
            {invalidKeys.length > 0 && ` · 잘못된 값 ${invalidKeys.length}칸(0~5000 정수)`}
          </small>
          <div>
            <button type="button" className="rules-ghost" onClick={() => void load()} disabled={loading || saving}>
              되돌리기
            </button>
            <button
              type="button"
              className="rules-save"
              onClick={() => void handleSave()}
              disabled={!data || saving || invalidKeys.length > 0}
            >
              {saving ? "저장 중…" : "저장"}
            </button>
          </div>
        </footer>
      </section>
    </div>,
    document.body,
  );
}
