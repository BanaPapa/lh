"""여유구간 100m ↔ 150m 실영향을 유형별로 대조한다.

안건 ⑤(BOUNDARY_BUFFER_M 100 vs 150)의 실영향을 조준환 국장 쪽과 같은 두 층위로
센다. 그쪽 LOCUS 엔진이 케이스 단위·시설군 단위로 실측(실영향 0건)했으므로,
같은 층위로 세어야 대조가 성립한다.

  케이스 단위   118건 각각의 종합 상태(매입제외/검토 필요/충돌 없음) 차이 건수
  시설군 단위   케이스 × 시설군별 상태 차이 건수

buffer 값만 다르고 유형(general/multi_child)은 같은 두 스냅샷을 짝지어 센다.
"""

from __future__ import annotations

import collections
import json
import os

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 케이스 종합 상태 라벨. screening verdict → 사람이 읽는 상태.
VERDICT_LABEL = {"fail": "매입제외", "review": "검토 필요", "pass": "충돌 없음"}

# buffer 만 다르고 유형은 같은 스냅샷 짝.
PAIRS = {
    "general": ("ours_118d_general_b100.json", "ours_118d_general_b150.json"),
    "multi_child": ("ours_118d_multichild_b100.json", "ours_118d_multichild_b150.json"),
}


def _load(path: str) -> tuple[dict, dict]:
    """스냅샷을 읽어 (메타, 접수번호→행) 로 돌려준다."""

    data = json.load(open(os.path.join(HERE, path), encoding="utf-8"))
    if isinstance(data, dict):
        meta, rows = data.get("meta", {}), data.get("results", [])
    else:  # 구형(배열만) 하위호환
        meta, rows = {}, data
    return meta, {r["no"]: r for r in rows}


def _label(verdict: str | None) -> str:
    return VERDICT_LABEL.get(verdict or "", verdict or "?")


def _cat_outcome(value: object) -> object:
    """시설군 값에서 outcome 을 뽑는다.

    신형 스냅샷은 {"outcome":..., "status":...}, 구형은 outcome 문자열이다. 여유구간
    실영향은 판정 결과(outcome)의 변화로 센다(버퍼는 거리 임계에만 영향).
    """

    if isinstance(value, dict):
        return value.get("outcome")
    return value


def _cat_status(value: object) -> object:
    """시설군 값에서 원래 상태값(status)을 뽑는다. 구형은 status 가 없어 None."""

    if isinstance(value, dict):
        return value.get("status")
    return None


def compare_type(app_type: str, b100_file: str, b150_file: str) -> dict:
    meta100, rows100 = _load(b100_file)
    meta150, rows150 = _load(b150_file)

    keys = sorted(set(rows100) & set(rows150))

    # 판정 실패(API 오류 등)한 케이스는 대조에서 뺀다.
    errors = sorted(
        k for k in keys if "error" in rows100[k] or "error" in rows150[k]
    )
    usable = [k for k in keys if k not in errors]

    # --- 케이스 단위 -----------------------------------------------------
    case_diffs = []
    for k in usable:
        v100, v150 = rows100[k].get("verdict"), rows150[k].get("verdict")
        if v100 != v150:
            case_diffs.append(
                {"no": k, "b100": _label(v100), "b150": _label(v150)}
            )

    # --- 시설군 단위 -----------------------------------------------------
    facility_diffs = []
    for k in usable:
        c100 = rows100[k].get("categories", {})
        c150 = rows150[k].get("categories", {})
        for cat in sorted(set(c100) | set(c150)):
            o100, o150 = _cat_outcome(c100.get(cat)), _cat_outcome(c150.get(cat))
            s100, s150 = _cat_status(c100.get(cat)), _cat_status(c150.get(cat))
            if o100 != o150 or s100 != s150:
                facility_diffs.append(
                    {
                        "no": k,
                        "category": cat,
                        "b100": o100,
                        "b150": o150,
                        "status_b100": s100,
                        "status_b150": s150,
                    }
                )

    return {
        "application_type": app_type,
        "buffer_b100": meta100.get("boundary_buffer_m"),
        "buffer_b150": meta150.get("boundary_buffer_m"),
        "usable_cases": len(usable),
        "errors": errors,
        "case_diff_count": len(case_diffs),
        "case_diffs": case_diffs,
        "facility_diff_count": len(facility_diffs),
        "facility_diffs": facility_diffs,
    }


def main() -> None:
    report = {}
    print("■ 여유구간 100m ↔ 150m 실영향 (유형별)\n")
    for app_type, (b100, b150) in PAIRS.items():
        try:
            res = compare_type(app_type, b100, b150)
        except FileNotFoundError as exc:
            print(f"  [{app_type}] 스냅샷 없음: {exc.filename}\n")
            continue
        report[app_type] = res

        print(
            f"[{app_type}] buffer {res['buffer_b100']}m → {res['buffer_b150']}m "
            f"· 대조 {res['usable_cases']}건"
        )
        if res["errors"]:
            print(f"  ※ 판정 실패 제외 {len(res['errors'])}건: {res['errors']}")
        print(f"  케이스 단위 차이 : {res['case_diff_count']}건")
        for d in res["case_diffs"]:
            print(f"     {d['no']}  {d['b100']} → {d['b150']}")
        print(f"  시설군 단위 차이 : {res['facility_diff_count']}건")
        for d in res["facility_diffs"]:
            print(f"     {d['no']} · {d['category']}  {d['b100']} → {d['b150']}")
        print()

    out = os.path.join(HERE, "buffer_impact.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=1)
    print(f"저장: {out}")


if __name__ == "__main__":
    main()
