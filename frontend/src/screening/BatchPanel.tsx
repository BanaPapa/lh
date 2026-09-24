import { AlertTriangle, Download, FileSpreadsheet, Square, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type {
  HazardApplicationType,
  HazardApplicationTypesResponse,
  HazardHousingType,
} from "../hazard-review/types";
import {
  batchExportUrl,
  batchTemplateUrl,
  cancelBatch,
  getBatch,
  getBatchRowResult,
  parseBatchFile,
  startBatch,
  type BatchParseResponse,
  type BatchRowInput,
  type BatchRowStatus,
  type BatchStatus,
} from "./batchApi";
import type { ScreeningResult } from "./types";
import "../batch.css";

interface BatchPanelProps {
  open: boolean;
  onClose: () => void;
  applicationTypes: HazardApplicationTypesResponse | null;
  defaultHousingType: HazardHousingType;
  defaultApplicationType: HazardApplicationType;
  /** 결과 한 건을 본 화면(지도·심사표)으로 연다. */
  onOpenResult: (result: ScreeningResult) => void;
  /** 화면 확인용 — 파일을 올리지 않고 읽은 결과를 바로 넣어 확인 창을 띄운다. */
  initialParse?: BatchParseResponse | null;
}

const POLL_MS = 1500;
const EXTRA_KEYS = ["차수", "접수일자", "매도자명"] as const;

const VERDICT_TONE: Record<string, string> = {
  pass: "tone-success",
  review: "tone-warning",
  fail: "tone-danger",
};

function scoreText(row: BatchRowStatus): string {
  if (row.status !== "completed") return "—";
  if (row.determined && row.living_score !== null) return `${row.living_score}`;
  if (row.living_score_min !== null && row.living_score_max !== null) {
    return `${row.living_score_min}~${row.living_score_max}`;
  }
  return "—";
}

function criterionText(row: BatchRowStatus, key: string): string {
  const item = key === "station_area" ? row.bonus : row.criteria.find((c) => c.key === key);
  if (!item) return row.status === "completed" ? "·" : "";
  if (item.awarded !== null) return `${item.awarded}`;
  return `${item.awarded_min}~${item.awarded_max}`;
}

/**
 * 일괄 심사 — LH 「심사지 리스트 양식」(분류·신청유형·지역·상세주소 …)을 올려 여러
 * 사업지를 차례로 심사한다. 올린 목록은 실행 전에 확인 창으로 정리해 보여 주고,
 * 담당자가 행마다 분류·신청유형·주소를 고쳐 확정한 뒤에야 서버가 돌린다.
 */
export function BatchPanel({
  open,
  onClose,
  applicationTypes,
  defaultHousingType,
  defaultApplicationType,
  onOpenResult,
  initialParse = null,
}: BatchPanelProps) {
  const [housingType, setHousingType] = useState<HazardHousingType>(defaultHousingType);
  const [applicationType, setApplicationType] =
    useState<HazardApplicationType>(defaultApplicationType);
  const [parsed, setParsed] = useState<BatchParseResponse | null>(initialParse);
  // 확인 창에서 담당자가 고친 행. 확정 전까지는 이 목록만 바뀐다.
  const [draft, setDraft] = useState<BatchRowInput[]>(() =>
    initialParse ? initialParse.rows.map((row) => ({ ...row })) : [],
  );
  const [confirming, setConfirming] = useState(Boolean(initialParse));
  const [batch, setBatch] = useState<BatchStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [openingRow, setOpeningRow] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);

  // 열 때마다 본 화면의 유형을 기본값으로 따라간다.
  useEffect(() => {
    if (!open) return;
    setHousingType(defaultHousingType);
    setApplicationType(defaultApplicationType);
  }, [open, defaultHousingType, defaultApplicationType]);

  // 진행 중이면 서버 상태를 주기적으로 받아온다.
  const running = batch !== null && (batch.status === "queued" || batch.status === "running");
  useEffect(() => {
    if (!running || !batch) return;
    let cancelled = false;
    const timer = window.setInterval(async () => {
      try {
        const next = await getBatch(batch.batch_id);
        if (!cancelled) setBatch(next);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : "진행 상태를 받지 못했습니다.");
      }
    }, POLL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [running, batch]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (confirming) setConfirming(false);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, confirming]);

  const housingEntries = useMemo(
    () => Object.entries(applicationTypes?.housing_types ?? {}) as [HazardHousingType, string][],
    [applicationTypes],
  );
  const applicationEntries = useMemo(
    () =>
      Object.entries(applicationTypes?.application_types ?? {}) as [
        HazardApplicationType,
        string,
      ][],
    [applicationTypes],
  );
  const housingLabel = (key: HazardHousingType) => applicationTypes?.housing_types[key] ?? key;
  const applicationLabel = (key: HazardApplicationType) =>
    applicationTypes?.application_types[key] ?? key;

  if (!open) return null;

  const handleFile = async (file: File | null) => {
    if (!file) return;
    setError("");
    setBatch(null);
    setBusy(true);
    try {
      const next = await parseBatchFile(file);
      setParsed(next);
      setDraft(next.rows.map((row) => ({ ...row })));
      setConfirming(true);
    } catch (cause) {
      setParsed(null);
      setDraft([]);
      setError(cause instanceof Error ? cause.message : "파일을 읽지 못했습니다.");
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const updateDraft = (index: number, patch: Partial<BatchRowInput>) => {
    setDraft((rows) => rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  };

  const handleRun = async () => {
    if (draft.length === 0) return;
    setError("");
    setBusy(true);
    try {
      const started = await startBatch({
        rows: draft,
        housing_type: housingType,
        application_type: applicationType,
        rule_pack_id: "",
      });
      setConfirming(false);
      setBatch(await getBatch(started.batch_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "일괄 심사를 시작하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  };

  const handleStop = async () => {
    if (!batch) return;
    try {
      setBatch(await cancelBatch(batch.batch_id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "중단 요청에 실패했습니다.");
    }
  };

  const handleOpenRow = async (row: BatchRowStatus) => {
    if (!batch || row.status !== "completed") return;
    setOpeningRow(row.id);
    try {
      onOpenResult(await getBatchRowResult(batch.batch_id, row.id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "이 건의 심사표를 열지 못했습니다.");
    } finally {
      setOpeningRow(null);
    }
  };

  const completed = batch?.rows.filter((r) => r.status === "completed").length ?? 0;
  const failed = batch?.rows.filter((r) => r.status === "failed").length ?? 0;
  const hasExtras = (rows: { extras: Record<string, string> }[]) =>
    rows.some((r) => EXTRA_KEYS.some((k) => r.extras[k]));

  // 확인 창 요약 — 담당자가 실행 전에 무엇이 들어왔는지 한눈에 본다.
  const draftSummary = (() => {
    const byHousing = new Map<string, number>();
    const byApplication = new Map<string, number>();
    let unresolvedHousing = 0;
    let unresolvedApplication = 0;
    let warned = 0;
    let withCoords = 0;
    for (const row of draft) {
      const h = row.housing_type ?? housingType;
      const a = row.application_type ?? applicationType;
      byHousing.set(h, (byHousing.get(h) ?? 0) + 1);
      byApplication.set(a, (byApplication.get(a) ?? 0) + 1);
      if (!row.housing_type) unresolvedHousing += 1;
      if (!row.application_type) unresolvedApplication += 1;
      if (row.warnings.length) warned += 1;
      if (row.lat !== null && row.lng !== null) withCoords += 1;
    }
    return { byHousing, byApplication, unresolvedHousing, unresolvedApplication, warned, withCoords };
  })();

  return createPortal(
    <div
      className="api-keys-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section className="batch-modal" role="dialog" aria-modal="true" aria-label="일괄 심사">
        <header className="batch-head">
          <div>
            <h2>
              <FileSpreadsheet size={22} aria-hidden="true" />
              일괄 심사
            </h2>
            <p>
              LH 「심사지 리스트 양식」(.xlsx·.csv)을 올리면 소재지마다 1차 매입제외 판정과 2차
              배점을 차례로 냅니다. 「분류」「신청유형」 열이 있으면 행마다 그 값을 쓰고, 비어
              있으면 아래 기본값을 씁니다. 실행 전에 올린 목록을 확인 창에서 고칠 수 있습니다.
            </p>
          </div>
          <button type="button" className="api-keys-close" onClick={onClose} aria-label="닫기">
            <X size={18} />
          </button>
        </header>

        <div className="batch-toolbar">
          <label className="batch-field">
            <span>기본 분류</span>
            <select
              value={housingType}
              disabled={running}
              onChange={(event) => setHousingType(event.target.value as HazardHousingType)}
            >
              {housingEntries.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="batch-field">
            <span>기본 신청유형</span>
            <select
              value={applicationType}
              disabled={running}
              onChange={(event) => setApplicationType(event.target.value as HazardApplicationType)}
            >
              {applicationEntries.map(([key, label]) => (
                <option key={key} value={key}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className={`batch-upload${running ? " is-disabled" : ""}`}>
            <input
              ref={fileInput}
              type="file"
              accept=".xlsx,.xlsm,.csv"
              disabled={running || busy}
              onChange={(event) => void handleFile(event.target.files?.[0] ?? null)}
            />
            <FileSpreadsheet size={16} aria-hidden="true" />
            심사지 리스트 올리기
          </label>
          <a className="batch-link" href={batchTemplateUrl()} download>
            <Download size={14} aria-hidden="true" />
            양식 내려받기
          </a>
          {parsed && !batch && !running && (
            <button
              type="button"
              className="batch-run"
              disabled={draft.length === 0 || busy}
              onClick={() => setConfirming(true)}
            >
              목록 확인 · {draft.length.toLocaleString()}건
            </button>
          )}
          {running && (
            <button type="button" className="batch-stop" onClick={() => void handleStop()}>
              <Square size={14} aria-hidden="true" />
              중단
            </button>
          )}
          {batch && !running && (
            <a className="batch-export" href={batchExportUrl(batch.batch_id)} download>
              <Download size={16} aria-hidden="true" />
              결과 내려받기(CSV 3종)
            </a>
          )}
        </div>

        {parsed && (
          <p className="batch-file-line">
            <strong>{parsed.file_name}</strong>
            {parsed.sheet && <span>· 시트 {parsed.sheet}</span>}
            <span>· 소재지 {draft.length.toLocaleString()}건</span>
            {parsed.skipped > 0 && <span>· 소재지 없는 행 {parsed.skipped}건 제외</span>}
            <span>· 읽은 열 {Object.values(parsed.columns).join(" · ")}</span>
          </p>
        )}
        {error && (
          <p className="batch-error" role="alert">
            {error}
          </p>
        )}

        {batch && (
          <div className="batch-progress" aria-live="polite">
            <div className="batch-progress-bar">
              <span style={{ width: `${(batch.done / Math.max(batch.total, 1)) * 100}%` }} />
            </div>
            <small>
              {running ? "검토 중" : batch.status === "cancelled" ? "중단됨" : "완료"} ·{" "}
              {batch.done}/{batch.total}건 · 완료 {completed} · 실패 {failed}
            </small>
          </div>
        )}

        <div className="batch-body">
          {batch ? (
            <table className="batch-table">
              <thead>
                <tr>
                  <th>접수번호</th>
                  {hasExtras(batch.rows) && <th>차수·접수일자·매도자</th>}
                  <th className="is-left">소재지</th>
                  <th>분류 · 신청유형</th>
                  <th>1차 판정</th>
                  <th>2차 점수</th>
                  <th>교통</th>
                  <th>주거</th>
                  <th>교육</th>
                  <th>가점</th>
                  <th>필지</th>
                  <th>상태</th>
                  <th>심사표</th>
                </tr>
              </thead>
              <tbody>
                {batch.rows.map((row) => (
                  <tr key={row.id} className={`is-${row.status}`}>
                    <td className="is-num">{row.id}</td>
                    {hasExtras(batch.rows) && (
                      <td>
                        <small>
                          {EXTRA_KEYS.map((k) => row.extras[k])
                            .filter(Boolean)
                            .join(" · ") || "—"}
                        </small>
                      </td>
                    )}
                    <td className="is-left">
                      <strong>{row.address}</strong>
                      {row.site_address && row.site_address !== row.address && (
                        <small>대표 필지 {row.site_address}</small>
                      )}
                      {row.error && <small className="is-error">{row.error}</small>}
                    </td>
                    <td>
                      <small>
                        {housingLabel(row.housing_type)} · {applicationLabel(row.application_type)}
                      </small>
                    </td>
                    <td>
                      {row.verdict_label ? (
                        <em className={`batch-badge ${VERDICT_TONE[row.verdict] ?? ""}`}>
                          {row.verdict_label}
                        </em>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="is-num is-score">
                      {scoreText(row)}
                      {row.status === "completed" && row.living_maximum !== null && (
                        <small>/{row.living_maximum}</small>
                      )}
                    </td>
                    <td className="is-num">{criterionText(row, "transit")}</td>
                    <td className="is-num">{criterionText(row, "living")}</td>
                    <td className="is-num">{criterionText(row, "education")}</td>
                    <td className="is-num">{criterionText(row, "station_area")}</td>
                    <td className="is-num">{row.parcel_count || ""}</td>
                    <td>
                      <small className={`batch-state is-${row.status}`}>{row.message || "대기"}</small>
                    </td>
                    <td>
                      {row.status === "completed" && (
                        <button
                          type="button"
                          className="batch-open"
                          disabled={openingRow === row.id}
                          onClick={() => void handleOpenRow(row)}
                        >
                          열기
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : parsed ? (
            <div className="batch-empty">
              <FileSpreadsheet size={28} aria-hidden="true" />
              <p>{draft.length.toLocaleString()}건을 읽었습니다.</p>
              <small>「목록 확인」을 눌러 분류·신청유형·주소를 확인하고 심사를 시작하세요.</small>
            </div>
          ) : (
            <div className="batch-empty">
              <FileSpreadsheet size={28} aria-hidden="true" />
              <p>LH 심사지 리스트 양식을 올려 주세요.</p>
              <small>
                열 이름 — 순번 · 차수 · 접수번호 · 접수일자 · 매도자명 · 분류(주택/주거용
                오피스텔) · 신청유형(청년/일반/신혼/고령자) · 지역 · 상세주소 · 경도 · 위도.
                판정에는 분류·신청유형·지역·상세주소만 쓰고 나머지는 결과에 그대로 싣습니다.
                다필지는 상세주소에 지번을 쉼표로 적습니다.
              </small>
            </div>
          )}
        </div>

        {confirming && (
          <div
            className="batch-confirm-backdrop"
            onMouseDown={(event) => {
              if (event.target === event.currentTarget) setConfirming(false);
            }}
          >
            <section
              className="batch-confirm"
              role="dialog"
              aria-modal="true"
              aria-label="올린 목록 확인"
            >
              <header className="batch-confirm-head">
                <div>
                  <h3>올린 목록 확인 · {draft.length.toLocaleString()}건</h3>
                  <p>
                    행마다 분류·신청유형·상세주소를 고칠 수 있습니다. 비어 있는 분류·신청유형은
                    기본값({housingLabel(housingType)} · {applicationLabel(applicationType)})으로
                    심사합니다.
                  </p>
                </div>
                <button
                  type="button"
                  className="api-keys-close"
                  onClick={() => setConfirming(false)}
                  aria-label="확인 창 닫기"
                >
                  <X size={18} />
                </button>
              </header>

              <ul className="batch-summary">
                <li>
                  <b>분류</b>
                  {[...draftSummary.byHousing].map(([key, count]) => (
                    <span key={key}>
                      {housingLabel(key as HazardHousingType)} {count}
                    </span>
                  ))}
                  {draftSummary.unresolvedHousing > 0 && (
                    <small>(기본값 적용 {draftSummary.unresolvedHousing}건)</small>
                  )}
                </li>
                <li>
                  <b>신청유형</b>
                  {[...draftSummary.byApplication].map(([key, count]) => (
                    <span key={key}>
                      {applicationLabel(key as HazardApplicationType)} {count}
                    </span>
                  ))}
                  {draftSummary.unresolvedApplication > 0 && (
                    <small>(기본값 적용 {draftSummary.unresolvedApplication}건)</small>
                  )}
                </li>
                <li>
                  <b>좌표</b>
                  <span>파일 좌표 {draftSummary.withCoords}건</span>
                  <span>주소로 찾음 {draft.length - draftSummary.withCoords}건</span>
                </li>
                {draftSummary.warned > 0 && (
                  <li className="is-warning">
                    <AlertTriangle size={14} aria-hidden="true" />
                    <span>확인할 점이 있는 행 {draftSummary.warned}건</span>
                  </li>
                )}
              </ul>

              <div className="batch-confirm-body">
                <table className="batch-table batch-confirm-table">
                  <thead>
                    <tr>
                      <th>접수번호</th>
                      {hasExtras(draft) && <th>차수 · 접수일자 · 매도자</th>}
                      <th className="is-left">상세주소</th>
                      <th>분류</th>
                      <th>신청유형</th>
                      <th>좌표</th>
                    </tr>
                  </thead>
                  <tbody>
                    {draft.map((row, index) => (
                      <tr key={`${row.id}:${index}`} className={row.warnings.length ? "is-warned" : ""}>
                        <td className="is-num">{row.id}</td>
                        {hasExtras(draft) && (
                          <td>
                            <small>
                              {EXTRA_KEYS.map((k) => row.extras[k])
                                .filter(Boolean)
                                .join(" · ") || "—"}
                            </small>
                          </td>
                        )}
                        <td className="is-left">
                          <input
                            className="batch-input"
                            value={row.address}
                            onChange={(event) => updateDraft(index, { address: event.target.value })}
                          />
                          {row.warnings.map((warning) => (
                            <small key={warning} className="is-warning">
                              <AlertTriangle size={12} aria-hidden="true" /> {warning}
                            </small>
                          ))}
                        </td>
                        <td>
                          <select
                            className={`batch-select${row.housing_type ? "" : " is-default"}`}
                            value={row.housing_type ?? ""}
                            onChange={(event) =>
                              updateDraft(index, {
                                housing_type: (event.target.value || null) as HazardHousingType | null,
                              })
                            }
                          >
                            <option value="">기본값 · {housingLabel(housingType)}</option>
                            {housingEntries.map(([key, label]) => (
                              <option key={key} value={key}>
                                {label}
                              </option>
                            ))}
                          </select>
                          {row.housing_label && !row.housing_type && (
                            <small>파일 값 「{row.housing_label}」</small>
                          )}
                        </td>
                        <td>
                          <select
                            className={`batch-select${row.application_type ? "" : " is-default"}`}
                            value={row.application_type ?? ""}
                            onChange={(event) =>
                              updateDraft(index, {
                                application_type: (event.target.value ||
                                  null) as HazardApplicationType | null,
                              })
                            }
                          >
                            <option value="">기본값 · {applicationLabel(applicationType)}</option>
                            {applicationEntries.map(([key, label]) => (
                              <option key={key} value={key}>
                                {label}
                              </option>
                            ))}
                          </select>
                          {row.application_label && !row.application_type && (
                            <small>파일 값 「{row.application_label}」</small>
                          )}
                        </td>
                        <td className="is-num">
                          <small>
                            {row.lat !== null && row.lng !== null
                              ? `${row.lat.toFixed(5)}, ${row.lng.toFixed(5)}`
                              : "주소로 찾음"}
                          </small>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <footer className="batch-confirm-foot">
                <button type="button" className="batch-upload" onClick={() => setConfirming(false)}>
                  돌아가기
                </button>
                <button
                  type="button"
                  className="batch-run"
                  disabled={draft.length === 0 || busy}
                  onClick={() => void handleRun()}
                >
                  이대로 {draft.length.toLocaleString()}건 심사 시작
                </button>
              </footer>
            </section>
          </div>
        )}
      </section>
    </div>,
    document.body,
  );
}
