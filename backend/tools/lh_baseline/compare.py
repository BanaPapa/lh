"""LH 기존 모델 ↔ 본 엔진 118건 전수 대조.

차이가 나면 그것이 오류인지 기준 차이인지를 가려야 한다. 원인을 네 갈래로
분류한다.

  RULE      판정 규칙이 다르다 (LH 는 25/50 일괄, 본 엔진은 룰북 v1.4 매트릭스)
  SCOPE     LH 대상 시설이 룰북에서 제외된다 (관광호텔·휴양콘도)
  DATA      시설 데이터 범위가 다르다 (LH 내장 2,956건 vs 실시간 원천)
  GEOMETRY  거리 산정 기준이 다르다 (점↔점 vs 대지경계↔시설경계)
"""

from __future__ import annotations

import collections
import json
import os

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 룰북 v1.4 가 일반숙박시설에서 제외한 관광숙박 업종(LH 8-24 정정회신).
TOURIST_MIDS = {
    "관광호텔",
    "휴양콘도미니엄업",
    "수상관광호텔",
    "한국전통호텔",
    "가족호텔",
    "호스텔",
    "소형호텔",
    "의료관광호텔",
}

# 다자녀 유형에서만 25m 가 걸리는 시설군. LH 는 유형 구분 없이 전 건에 건다.
TYPE_LIMITED = {"숙박시설", "위락시설"}

LH_TO_OURS = {"EXCLUDE": "fail", "CAUTION": None, "PASS": "pass", "NOCOORD": None}


def classify(lh: dict, ours: dict) -> tuple[str, str]:
    """차이 한 건의 원인을 분류한다."""

    near = lh.get("nearest") or {}
    major, mid = near.get("major", ""), near.get("mid", "")
    lh_status, ours_verdict = lh["stage1"], ours.get("verdict")

    # LH 가 잡았는데 우리가 안 잡은 경우
    if lh_status in ("EXCLUDE", "CAUTION") and ours_verdict == "pass":
        if mid in TOURIST_MIDS:
            return "SCOPE", f"{mid} — 룰북 v1.4 관광숙박 제외 대상 (LH 8-24 정정)"
        if lh_status == "CAUTION":
            return "RULE", f"LH 주의(50m) 구간 — 룰북에는 주의 등급이 없음 ({major})"
        if major == "위험시설":
            return "RULE", f"LH 위험시설 25m 일괄 — 룰북은 위험물 50m·주유소3종 25m ({mid})"
        return "DATA", f"LH 내장 데이터에만 있는 시설 ({major}·{mid})"

    # 우리가 잡았는데 LH 가 안 잡은 경우
    if ours_verdict == "fail" and lh_status == "PASS":
        return "GEOMETRY/DATA", "경계 기준 또는 실시간 원천에서만 확인된 시설"

    if ours_verdict == "review" and lh_status in ("PASS", "CAUTION"):
        return "REVIEW", "확정 조건 미충족으로 검토 필요"

    return "OTHER", ""


def _load_our_rows(path: str) -> list[dict]:
    """본 엔진 스냅샷을 읽는다. 실행 조건 메타가 붙은 신형({meta, results})과
    배열만 있던 구형을 모두 받는다(하위호환)."""

    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):
        return data.get("results", [])
    return data


def main() -> None:
    lh_rows = {x["no"]: x for x in json.load(open(os.path.join(HERE, "lh_118.json"), encoding="utf-8"))}
    our_rows = {x["no"]: x for x in _load_our_rows(os.path.join(HERE, "ours_118d.json"))}

    errors = [x for x in our_rows.values() if "error" in x]
    if errors:
        print(f"※ 본 엔진 실패 {len(errors)}건: {[e['no'] for e in errors][:8]}\n")

    pairs = [
        (lh_rows[k], our_rows[k])
        for k in sorted(lh_rows)
        if k in our_rows and "error" not in our_rows[k] and lh_rows[k]["stage1"] != "NOCOORD"
    ]

    # --- 1단계 교차표 ---------------------------------------------------
    print(f"■ 1차 판정 교차표 ({len(pairs)}건)\n")
    cross: collections.Counter = collections.Counter()
    for lh, ours in pairs:
        cross[(lh["stage1_label"], ours["verdict"])] += 1
    ours_cols = ["pass", "review", "fail"]
    ours_lbl = {"pass": "적격", "review": "검토 필요", "fail": "부적격"}
    print(f"{'LH 기존 ↓ / 본 엔진 →':>22s} " + " ".join(f"{ours_lbl[c]:>9s}" for c in ours_cols) + f" {'계':>6s}")
    for lh_label in ("통과", "주의", "제외"):
        row = [cross[(lh_label, c)] for c in ours_cols]
        print(f"{lh_label:>22s} " + " ".join(f"{v:>9d}" for v in row) + f" {sum(row):>6d}")
    totals = [sum(cross[(l, c)] for l in ("통과", "주의", "제외")) for c in ours_cols]
    print(f"{'계':>22s} " + " ".join(f"{v:>9d}" for v in totals) + f" {sum(totals):>6d}")

    same = sum(cross[(a, b)] for a, b in (("통과", "pass"), ("제외", "fail")))
    print(f"\n같은 계열 {same}건 · 갈린 판정 {len(pairs) - same}건")

    # --- 원인 분류 -------------------------------------------------------
    diffs = []
    for lh, ours in pairs:
        expected = LH_TO_OURS[lh["stage1"]]
        if expected is not None and ours["verdict"] == expected:
            continue
        kind, why = classify(lh, ours)
        diffs.append((lh, ours, kind, why))

    print(f"\n■ 차이 원인 분류 ({len(diffs)}건)\n")
    for kind, n in collections.Counter(d[2] for d in diffs).most_common():
        print(f"  {kind:14s} {n:3d}건")

    print("\n■ 갈린 건 전체\n")
    print(f"{'접수':5s} {'소재지':30s} {'LH':4s} {'LH 최근접':28s} {'본엔진':7s} {'근거 시설':30s} 원인")
    for lh, ours, kind, why in diffs:
        n = lh.get("nearest") or {}
        lh_near = f"{n.get('name', '-')[:14]} {n.get('distance_m', 0):.0f}m ({n.get('mid', '')[:6]})"
        f = ours.get("nearest_fail")
        our_near = f"{f['name'][:14]} {f['distance_m']:.0f}m" if f else "—"
        print(
            f"{lh['no']:5s} {lh['address'][:28]:30s} {lh['stage1_label']:4s} {lh_near:28s} "
            f"{ours_lbl[ours['verdict']]:7s} {our_near:30s} {kind}"
        )

    # --- 2단계 점수 ------------------------------------------------------
    scored = [
        (lh, ours)
        for lh, ours in pairs
        if lh["stage2_city_total"] is not None and ours.get("living_determined")
    ]
    print(f"\n■ 2차 생활편의성 점수 대조 (본 엔진 확정건 {len(scored)}건)\n")
    gaps = [ours["living_score"] - lh["stage2_city_total"] for lh, ours in scored]
    if gaps:
        exact = sum(1 for g in gaps if g == 0)
        print(f"  완전 일치 {exact}건 / {len(gaps)}건 ({exact / len(gaps) * 100:.0f}%)")
        print(f"  평균 차이 {sum(gaps) / len(gaps):+.1f}점 · 최대 {max(gaps):+d} · 최소 {min(gaps):+d}")
        print(f"  분포: {dict(sorted(collections.Counter(gaps).items()))}")
    undet = [o for _, o in pairs if not o.get("living_determined")]
    print(f"  본 엔진 미확정 {len(undet)}건 (미확보 시설군으로 등급 확정 불가)")


if __name__ == "__main__":
    main()
