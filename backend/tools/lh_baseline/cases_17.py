"""LH 9/7판 17건 엑셀에서 접수번호·소재지·건물유형·유형·실제점수를 뽑는다.

엑셀이 이 PC 에 없으면(다른 PC 의 카카오톡 수신 파일) 저장소에 커밋된 0908 실행 결과
`data/ours_17case_20260908.json` 에서 같은 케이스 목록을 복원한다. 실행 결과 행은
(no, housing_type) 단위라 no 로 다시 묶는다(도시형생활주택은 주택·오피스텔 두 유형).
"""
import json, os, sys
from pathlib import Path
P = r'C:\Users\Bana\Documents\카카오톡 받은 파일\2026 매입약정 서류심사_생활편의성 35점 이상 17건_26.09.07.작업.xlsx'
FALLBACK = Path(__file__).resolve().parent / "data" / "ours_17case_20260908.json"


def cases_from_baseline(path: Path) -> list[dict]:
    grouped: dict[str, dict] = {}
    for r in json.loads(path.read_text(encoding="utf-8"))["rows"]:
        entry = grouped.setdefault(r["no"], {
            "no": r["no"], "seq": r["no"].split("_")[0], "address": r["address"],
            "building": r.get("building", ""), "housing_types": [],
            "application_type": r["application_type"],
            "lh_actual": r.get("lh_actual"), "lh_model": r.get("lh_model"),
        })
        if r["housing_type"] not in entry["housing_types"]:
            entry["housing_types"].append(r["housing_type"])
    return list(grouped.values())


if not os.path.exists(P):
    print(f"엑셀 없음 → {FALLBACK.name} 에서 복원", file=sys.stderr)
    cases = cases_from_baseline(FALLBACK)
    print(json.dumps(cases, ensure_ascii=False, indent=1))
    print("건수", len(cases))
    sys.exit(0)

import openpyxl
ws = openpyxl.load_workbook(P, data_only=True)['Sheet1']
rows = list(ws.iter_rows(values_only=True))

# (접수번호행, 소재지행, 건물유형행, 실제점수행, LH모델점수행 or None)
BLOCKS = [(4, 5, 6, 7, None), (27, 28, 29, 30, 45)]

# 건물유형 문자열 → housing_type. 「도시형생활주택 + 주거용오피스텔」은
# 2026-09-07 최기헌 팀장님 확정(도시형생활주택=공동주택=주택)과 같은 날 회신
# (「주택과 주거용오피스텔 모두로 취급」)에 따라 두 유형 모두로 돌린다.
def housing(label: str) -> list[str]:
    if label.startswith("도시형생활주택"):
        return ["house", "officetel"]
    return ["officetel"] if "오피스텔" in label else ["house"]

def application(no: str) -> str:
    tag = no.split("_", 1)[1]
    return {"청년": "youth", "일반": "general", "신혼1": "newlywed",
            "신혼2": "newlywed", "다자녀": "multi_child", "고령자": "senior"}[tag]

cases = []
for nr, ar, br, sr, mr in BLOCKS:
    nos, adr, bld = rows[nr-1], rows[ar-1], rows[br-1]
    sc, mo = rows[sr-1], (rows[mr-1] if mr else None)
    for c in range(3, len(nos)):
        if not nos[c]:
            continue
        no = str(nos[c]).strip()
        cases.append({
            "no": no,
            "seq": no.split("_")[0],
            "address": str(adr[c]).strip(),
            "building": str(bld[c]).strip(),
            "housing_types": housing(str(bld[c]).strip()),
            "application_type": application(no),
            "lh_actual": sc[c],
            "lh_model": (mo[c] if mo else None),
        })
print(json.dumps(cases, ensure_ascii=False, indent=1))
print("건수", len(cases))
