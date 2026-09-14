"""조준환 국장 3자 비교보고(2026-09-07)의 표본 5건을 이 앱으로 재판정한다.

조준환 보고는 5건 전부를 주택·일반으로 통일해 돌렸다. 같은 조건(A)으로 먼저 돌려
대조 가능성을 확보하고, LH 9/7판 17건 엑셀에 적힌 실제 건물유형(B)으로 한 번 더
돌려 유형 적용이 판정을 뒤집는지 본다.
"""
from __future__ import annotations

import asyncio, json, os, sys
from pathlib import Path

# backend/ 를 import 경로에 넣는다. 이 파일은 backend/tools/lh_baseline/ 에 있다.
BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tools" / "lh_baseline"))
os.chdir(BACKEND)

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass

from tools.lh_baseline.run_ours import (
    _provision_env_from_dotenv, _provision_noise_csv, one, warm_sources,
)
from tools.lh_baseline.lh_replica import load_sites
from app.models import Coordinates

# 조준환 보고 §3 표본 5건. 값은 그 문서에 적힌 3자 결과.
TARGET = ["001", "004", "006", "041", "105"]

# LH 9/7판 17건 엑셀의 실제 건물유형·신청유형. 5건 중 표에 있는 것만.
ACTUAL = {
    "004": ("officetel", "general"),   # 004_일반 · 주거용오피스텔
    "006": ("officetel", "youth"),     # 006_청년 · 주거용오피스텔
}
# 나머지 3건은 17건 표에 없다 → 조준환과 같은 주택·일반 유지.
DEFAULT = ("house", "general")


async def main() -> None:
    _provision_env_from_dotenv()
    print("소음 CSV:", _provision_noise_csv())
    sites = {s["no"]: s for s in load_sites()}
    picked = [sites[n] for n in TARGET]
    for s in picked:
        print(f"  {s['no']} {s['region']} {s['detail']} ({s['lat']:.6f},{s['lng']:.6f})")

    # 앱(FastAPI 라우터)과 완전히 같은 배선으로 조립한다. tools/lh_baseline 의
    # build_service() 는 facility_store·hospital_client·front_door·cadastral 을
    # 넘기지 않아 대규모점포(상업시설)·종합병원이 빠진 축소 배선이다. 이 보고는
    # 「이 앱을 그대로 돌린 결과」여야 하므로 라우터 팩토리를 그대로 쓴다.
    from app.screening.router import get_screening_service
    from app.hazard_review.parcels import ParcelResolver
    from app.services.kakao import KakaoClient
    from app.services.vworld import VWorldClient
    from app.config import get_settings

    screening = get_screening_service()
    s_cfg = get_settings()
    resolver = ParcelResolver(
        kakao=KakaoClient(s_cfg.kakao_rest_api_key),
        vworld=VWorldClient(s_cfg.vworld_api_key, domain=s_cfg.vworld_domain),
        cadastral=screening.hazard.cadastral,
    )
    loader = getattr(screening.hazard, "_local_sources_loader", None)
    if loader is not None:
        bundle = loader()
        screening.hazard.local_sources = bundle
        screening.hazard._local_sources_warmup_started = True
        print("로컬 원천 로더:", type(bundle).__name__)

    centers = [Coordinates(lat=s["lat"], lng=s["lng"]) for s in picked]
    counts, degraded = await warm_sources(screening, allow_degraded=True, centers=centers)
    print("예열:", counts, "결손:", [d["source"] for d in degraded])

    out = {"meta": {"targets": TARGET, "warm": counts,
                    "degraded": [d["source"] for d in degraded]}, "runs": {}}

    for label, picker in (("A_주택일반", lambda n: DEFAULT),
                          ("B_실제유형", lambda n: ACTUAL.get(n, DEFAULT))):
        rows = []
        for s in picked:
            ht, at = picker(s["no"])
            print(f"\n▶ [{label}] {s['no']} {ht}/{at}", flush=True)
            try:
                r = await one(s, screening, resolver, ht, at)
            except Exception as exc:
                print(f"  [ERR] {type(exc).__name__}: {exc}", flush=True)
                rows.append({"no": s["no"], "error": f"{type(exc).__name__}: {exc}"})
                continue
            r["housing_type"], r["application_type"] = ht, at
            print(f"  판정={r['verdict']} 점수={r['living_score']} "
                  f"기하={r['geometry_source']} 최근접={r['nearest_fail']}", flush=True)
            rows.append(r)
        out["runs"][label] = rows

    dest = Path(__file__).resolve().parent / "data" / "ours_5site_20260908.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n저장:", dest)


asyncio.run(main())
