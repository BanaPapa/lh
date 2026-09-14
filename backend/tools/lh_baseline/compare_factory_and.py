"""공장 나·다목 AND 성립 건수를 독립 계산하고 박진주 표준셋 577건과 대조한다.

우리 계산
  나목  대기배출 1~3종 사업장의 필지 PNU ∈ 등록공장 PNU 집합
  다목  대기배출 4~5종 사업장의 필지 PNU ∈ 고시업종 등록공장 PNU 집합
등록공장 PNU 집합은 factoryON 원장 주소를 카카오·VWorld 로 확정한 것이다
(박진주 표준셋을 쓰지 않는다).

대조 대상
  0908-standardized/facilities.xlsx 의 rule_key_passs
    FACTORY_B:PASS 185 (나목) · FACTORY_C:PASS 392 (다목)
"""
from __future__ import annotations
import asyncio, collections, io, json, os, sqlite3, sys, zipfile
from pathlib import Path

# backend/ 를 import 경로에 넣는다. 이 파일은 backend/tools/lh_baseline/ 에 있다.
BACKEND = Path(__file__).resolve().parents[2]
SCRATCH = Path(__file__).resolve().parent / "data"
sys.path.insert(0, str(BACKEND)); sys.path.insert(0, str(BACKEND / "tools" / "lh_baseline"))
os.chdir(BACKEND)
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

from tools.lh_baseline.run_ours import _provision_env_from_dotenv
_provision_env_from_dotenv()
from app.screening.router import get_screening_service
from app.services.facility_store import DEFAULT_DB_PATH
from app.services.address_pnu_kakao import KakaoAddressPnu
from app.config import get_settings

# 박진주 대표님 표준셋(검증 대상). 판정에는 쓰지 않고 대조에만 쓴다.
STD_ZIP = os.environ.get("LH_STANDARD_ZIP", r"C:\Users\Bana\Documents\카카오톡 받은 파일\0908-standardized.zip")

# 대기배출 원장의 종별 → 목. 「1종」~「5종」 문자열이 category 에 들어온다.
def band_of(category: str) -> str:
    c = category or ""
    if any(k in c for k in ("1종", "2종", "3종")):
        return "air_1_3"
    if any(k in c for k in ("4종", "5종")):
        return "air_4_5"
    return ""


def main() -> None:
    hz = get_screening_service().hazard
    bundle = hz._local_sources_loader()
    registered = set(bundle.factory_pnus) | set(bundle.factory_registry_resolved_pnus)
    prohibited = set(bundle.prohibit_factory_pnus)
    print(f"등록공장 PNU {len(registered)} · 고시업종 공장 PNU {len(prohibited)}")

    con = sqlite3.connect(str(DEFAULT_DB_PATH)); con.row_factory = sqlite3.Row
    rows = [
        dict(r) for r in con.execute(
            "select name,address,category,lat,lng from facilities "
            "where dataset_key='air_pollution' and address like '%전북%'"
        )
    ]
    bands = collections.Counter(band_of(r["category"]) for r in rows)
    print(f"전북 대기배출사업장 {len(rows)}건 — 1~3종 {bands['air_1_3']} · "
          f"4~5종 {bands['air_4_5']} · 종별 불명 {bands['']}")

    # 배출사업장 주소 → PNU (등록공장과 같은 방식·같은 원천)
    cfg = get_settings()
    asm = KakaoAddressPnu(cfg.kakao_rest_api_key)
    hits = {"air_1_3": 0, "air_4_5": 0}
    resolved = 0
    matched_rows = {"air_1_3": [], "air_4_5": []}
    for r in rows:
        band = band_of(r["category"])
        if not band:
            continue
        a = asm(r["address"])
        if a is None:
            continue
        resolved += 1
        if band == "air_1_3" and a.pnu in registered:
            hits["air_1_3"] += 1
            matched_rows["air_1_3"].append((r["name"], r["address"], a.pnu))
        elif band == "air_4_5" and a.pnu in prohibited:
            hits["air_4_5"] += 1
            matched_rows["air_4_5"].append((r["name"], r["address"], a.pnu))
    asm.close()
    print(f"배출사업장 PNU 조립 {resolved} · {asm.stats.summary()}")

    # 박진주 표준셋 대조값
    z = zipfile.ZipFile(STD_ZIP)
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(z.read("standardized/facilities.xlsx")),
                                read_only=True, data_only=True)
    ws = wb.worksheets[0]; it = ws.iter_rows(values_only=True)
    hdr = [str(c or "") for c in next(it)]
    I = {h: i for i, h in enumerate(hdr)}
    std = collections.Counter()
    for r in it:
        k = r[I["rule_key_passs"]]
        if k and str(k).endswith(":PASS"):
            std[str(k)] += 1

    print("\n===== 대조 =====")
    print(f"  나목  우리 {hits['air_1_3']:4}  ↔  박진주 FACTORY_B:PASS {std['FACTORY_B:PASS']:4}")
    print(f"  다목  우리 {hits['air_4_5']:4}  ↔  박진주 FACTORY_C:PASS {std['FACTORY_C:PASS']:4}")
    park_factory = std["FACTORY_B:PASS"] + std["FACTORY_C:PASS"]
    print(f"  합계  우리 {sum(hits.values()):4}  ↔  박진주 {park_factory:4}  "
          f"(박진주 나머지 {sum(std.values()) - park_factory}건은 위락시설 ENT_*)")

    out = {
        "registered_pnus": len(registered),
        "prohibited_pnus": len(prohibited),
        "jeonbuk_emission_rows": len(rows),
        "bands": dict(bands),
        "ours": hits,
        "park": dict(std),
        "samples": {k: v[:15] for k, v in matched_rows.items()},
    }
    (SCRATCH / "factory_and_20260908.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n저장:", SCRATCH / "factory_and_20260908.json")


main()
