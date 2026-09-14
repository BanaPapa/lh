"""LH 9/7판 17건을 이 앱(실시간 공개 API)으로 전수 판정한다.

좌표는 118건 원장에 있는 접수번호 좌표를 우선 쓰고(같은 입력이어야 LH 모델·
납품본과 대조가 성립한다), 원장에 없으면 카카오 주소검색으로 확보한다.
"""
from __future__ import annotations
import asyncio, json, os, subprocess, sys
from datetime import datetime
from pathlib import Path

# backend/ 를 import 경로에 넣는다. 이 파일은 backend/tools/lh_baseline/ 에 있다.
BACKEND = Path(__file__).resolve().parents[2]
SCRATCH = Path(__file__).resolve().parent / "data"
sys.path.insert(0, str(BACKEND)); sys.path.insert(0, str(BACKEND / "tools" / "lh_baseline"))
os.chdir(BACKEND)
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass

from tools.lh_baseline.run_ours import _provision_env_from_dotenv, _provision_noise_csv, one, warm_sources
from tools.lh_baseline.lh_replica import load_sites
from app.models import Coordinates

CASES = json.loads(subprocess.run(
    [sys.executable, str(Path(__file__).resolve().parent / "cases_17.py")], capture_output=True, text=True,
    encoding="utf-8").stdout.rsplit("]", 1)[0] + "]")


async def main() -> None:
    _provision_env_from_dotenv(); _provision_noise_csv()
    from app.screening.router import get_screening_service
    from app.hazard_review.parcels import ParcelResolver
    from app.services.kakao import KakaoClient
    from app.services.vworld import VWorldClient
    from app.config import get_settings

    cfg = get_settings()
    kakao = KakaoClient(cfg.kakao_rest_api_key)
    screening = get_screening_service()
    resolver = ParcelResolver(kakao=kakao,
                              vworld=VWorldClient(cfg.vworld_api_key, domain=cfg.vworld_domain),
                              cadastral=screening.hazard.cadastral)
    loader = getattr(screening.hazard, "_local_sources_loader", None)
    if loader:
        screening.hazard.local_sources = loader()
        screening.hazard._local_sources_warmup_started = True

    ledger = {s["no"]: s for s in load_sites()}

    # 좌표 확보 — 원장 우선, 없으면 카카오 주소검색.
    for c in CASES:
        led = ledger.get(c["seq"])
        if led and led.get("valid"):
            c["lat"], c["lng"], c["coord_src"] = led["lat"], led["lng"], "LH 원장"
            c["region"], c["detail"] = led["region"], led["detail"]
            continue
        docs = await kakao.address_documents(c["address"])
        if not docs:
            c["lat"] = c["lng"] = None; c["coord_src"] = "확보 실패"; continue
        d = docs[0]
        c["lat"], c["lng"] = float(d["y"]), float(d["x"])
        c["coord_src"] = "카카오 주소검색"
        c["region"], c["detail"] = "", c["address"]
        print(f"  [지오코딩] {c['no']} {c['address']} → {c['lat']:.6f},{c['lng']:.6f}")

    usable = [c for c in CASES if c.get("lat")]
    print(f"좌표 확보 {len(usable)}/{len(CASES)}")

    centers = [Coordinates(lat=c["lat"], lng=c["lng"]) for c in usable]
    counts, degraded = await warm_sources(screening, allow_degraded=True, centers=centers)
    print("예열:", counts, "결손:", [d["source"] for d in degraded])

    results = []
    for c in usable:
        site = {"no": c["seq"], "region": c.get("region", ""),
                "detail": c.get("detail") or c["address"],
                "lat": c["lat"], "lng": c["lng"]}
        for ht in c["housing_types"]:
            at = c["application_type"]
            print(f"\n▶ {c['no']} {c['building']} → {ht}/{at}", flush=True)
            try:
                r = await one(site, screening, resolver, ht, at)
            except Exception as exc:
                print(f"  [ERR] {type(exc).__name__}: {exc}", flush=True)
                results.append({**c, "housing_type": ht, "error": f"{type(exc).__name__}: {exc}"})
                continue
            r.update({k: c[k] for k in ("no", "building", "lh_actual", "lh_model", "coord_src")})
            r["housing_type"], r["application_type"] = ht, at
            print(f"  판정={r['verdict']} 점수={r['living_score']}"
                  f"(LH실제 {c['lh_actual']} / LH모델 {c['lh_model']}) "
                  f"기하={r['geometry_source']} 최근접={r['nearest_fail']}", flush=True)
            results.append(r)

    dest = SCRATCH / f"ours_17case_{datetime.now().strftime('%Y%m%d')}.json"
    dest.write_text(json.dumps({"meta": {"warm": counts,
                                         "degraded": [d["source"] for d in degraded]},
                                "rows": results}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print("\n저장:", dest)


asyncio.run(main())
