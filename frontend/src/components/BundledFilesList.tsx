import { ExternalLink, FileText, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { getBundledFiles, type BundledFilesResponse } from "../api";

function formatCount(count: number | null, unit: string): string {
  if (count === null || count === undefined) return "—";
  return `${count.toLocaleString("ko-KR")}${unit ? ` ${unit}` : ""}`;
}

function formatSize(bytes: number | null): string {
  if (!bytes) return "";
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
  return `${Math.max(1, Math.round(bytes / 1024))}KB`;
}

/**
 * 「API 외에 서버에 실은 파일」 — 공공 API 로 온전히 받을 수 없어 파일로 서버에 실은 자료.
 *
 * 백엔드 목록(backend/app/bundled_files.py · GET /api/settings/bundled-files)을 그대로 보인다.
 * 엔드포인트는 비밀이 없는 공개 읽기 전용이지만, 화면은 API 연결 창의 탭이라 그 창처럼
 * 로컬 앱(IS_LOCAL_APP)에서만 보인다.
 */
export function BundledFilesList({ open }: { open: boolean }) {
  const [data, setData] = useState<BundledFilesResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(await getBundledFiles());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "목록을 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void load();
  }, [open, load]);

  return (
    <section className="api-keys-group is-status">
      <header className="api-keys-group-head">
        <span className="api-keys-badge is-server">
          <FileText size={13} aria-hidden="true" /> API 외에 서버에 실은 파일
        </span>
        <div className="api-conn-actions">
          <button
            type="button"
            className="api-keys-refresh"
            onClick={() => void load()}
            disabled={loading}
          >
            <RotateCcw size={13} aria-hidden="true" /> 새로고침
          </button>
        </div>
      </header>
      {data?.description && <p className="api-file-intro">{data.description}</p>}
      {error && <p className="api-conn-empty">{error}</p>}
      {!error && !data && (
        <p className="api-conn-empty">{loading ? "목록을 불러오는 중…" : "목록이 없습니다."}</p>
      )}
      {data && (
        <table className="api-conn-table api-file-table">
          <thead>
            <tr>
              <th>이름</th>
              <th>무엇에 쓰는지</th>
              <th>출처</th>
              <th>기준일</th>
              <th>건수</th>
            </tr>
          </thead>
          <tbody>
            {data.files.map((file) => (
              <tr key={file.id} className={file.present ? "" : "is-missing"}>
                <td className="api-conn-label">
                  <strong>{file.name}</strong>
                  <code title={file.path}>{file.path.split("/").pop()}</code>
                  {!file.present && <span className="api-keys-state is-off">서버에 없음</span>}
                </td>
                <td className="api-conn-purpose">
                  {file.purpose}
                  {file.note && <small>{file.note}</small>}
                </td>
                <td className="api-conn-meta">
                  <span>{file.source}</span>
                  {file.source_id && <small>{file.source_id}</small>}
                  {file.source_url && (
                    <a
                      href={file.source_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="api-keys-issuer"
                    >
                      <ExternalLink size={13} aria-hidden="true" /> 원천
                    </a>
                  )}
                </td>
                <td>{file.as_of || "—"}</td>
                <td>
                  {formatCount(file.count, file.count_unit)}
                  {file.size_bytes ? <small>{formatSize(file.size_bytes)}</small> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
