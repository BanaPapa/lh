import { useEffect, useState } from "react";
import { getLhAlignments, type LhAlignmentEntry } from "../api";

function formatMeters(value: number | null): string {
  if (value === null || value === undefined) return "—";
  return `${value.toLocaleString("ko-KR", { maximumFractionDigits: 1 })}m`;
}

/**
 * 관리자 설정 「LH 개별 확인」 — 공공 API 자료를 LH 데이터셋 기준에 일부러 맞춘 시설 목록.
 *
 * 저장소에 커밋하는 등록부(backend/data/lh_alignments.json)를 그대로 보여 준다. 항목을
 * 고치는 곳은 등록부 파일이라 여기서는 읽기만 한다. 맞춘 시설은 심사 결과에도
 * 「LH 개별 맞춤」 표시가 붙는다.
 */
export function LhAlignmentList({ open }: { open: boolean }) {
  const [entries, setEntries] = useState<LhAlignmentEntry[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setError("");
    getLhAlignments()
      .then((response) => {
        if (alive) setEntries(response.entries);
      })
      .catch((cause: unknown) => {
        if (alive) setError(cause instanceof Error ? cause.message : "목록을 불러오지 못했습니다.");
      });
    return () => {
      alive = false;
    };
  }, [open]);

  return (
    <section className="rules-section rules-alignments">
      <header>
        <div>
          <h3>LH 개별 맞춤 — 공공 API 자료를 LH 데이터셋 기준에 맞춘 시설</h3>
          <p>
            이 앱은 공공 API 로 시설을 모읍니다. 아래 시설은 공공 API 가 주는 위치·이름·범위가 LH
            데이터셋과 달라, 시설 한 곳씩 LH 데이터셋 기준에 맞췄습니다. 항목마다 LH 데이터셋 근거와
            LH 거리를 함께 적었고, 심사 결과에는 「LH 개별 맞춤」 표시가 붙습니다. 이 목록은 저장소
            등록부(backend/data/lh_alignments.json)에서 관리하며 여기서는 볼 수만 있습니다.
          </p>
        </div>
      </header>
      {error && <p className="rules-message is-error">{error}</p>}
      {!error && entries === null && <p className="rules-empty">불러오는 중…</p>}
      {entries !== null && entries.length === 0 && <p className="rules-empty">등록된 항목이 없습니다.</p>}
      {entries !== null && entries.length > 0 && (
        <div className="rules-table-scroll">
          <table className="rules-table rules-alignment-table">
            <thead>
              <tr>
                <th className="is-left">시설</th>
                <th className="is-left">시설군</th>
                <th className="is-left">맞춘 내용</th>
                <th className="is-left">LH 거리 근거</th>
                <th className="is-left">사유</th>
                <th className="is-left">출처</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <td className="is-left">
                    <strong>{entry.name}</strong>
                    {entry.lh_name && entry.lh_name !== entry.name && <small>LH 표기 {entry.lh_name}</small>}
                    {entry.aliases.length > 0 && <small>다른 이름 {entry.aliases.join(" · ")}</small>}
                  </td>
                  <td className="is-left">{entry.group_label}</td>
                  <td className="is-left">
                    <b className="rules-alignment-action">{entry.action_label}</b>
                    <small>{entry.summary}</small>
                  </td>
                  <td className="is-left">
                    {entry.evidence.length === 0 ? (
                      "—"
                    ) : (
                      <ul className="rules-alignment-evidence">
                        {entry.evidence.map((item, index) => (
                          <li key={index}>
                            {item.site}
                            {item.lh_distance_m !== null && ` · LH ${formatMeters(item.lh_distance_m)}`}
                            {item.before_m !== null && ` · 맞추기 전 ${formatMeters(item.before_m)}`}
                            {item.after_m !== null && ` → ${formatMeters(item.after_m)}`}
                            {item.note && <small>{item.note}</small>}
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                  <td className="is-left">{entry.reason}</td>
                  <td className="is-left">
                    <small>{entry.source}</small>
                    <small>{entry.date}</small>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
