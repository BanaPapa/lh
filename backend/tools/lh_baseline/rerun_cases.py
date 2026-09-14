"""15건 중 지정한 접수번호만 다시 돌린다.

전건 재실행은 카카오·공공데이터 호출을 다시 쏟아붓는다. 한두 건만 확인할 때
(예: 조회 실패로 확정 불가가 난 건이 일시적인 것인지) 쓰라고 둔다.

    python -m tools.lh_baseline.rerun_cases 002 109
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tools" / "lh_baseline"))
os.chdir(BACKEND)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tools.lh_baseline.run_ours import (  # noqa: E402
    _provision_env_from_dotenv,
    _provision_noise_csv,
    one,
    warm_sources,
)
from tools.lh_baseline.run_15_cases import CASES  # noqa: E402
from tools.lh_baseline.lh_replica import load_sites  # noqa: E402
from app.models import Coordinates  # noqa: E402


async def main(targets: list[str]) -> None:
    _provision_env_from_dotenv()
    _provision_noise_csv()

    from app.config import get_settings
    from app.hazard_review.parcels import ParcelResolver
    from app.screening.router import get_screening_service
    from app.services.kakao import KakaoClient
    from app.services.vworld import VWorldClient

    screening = get_screening_service()
    cfg = get_settings()
    resolver = ParcelResolver(
        kakao=KakaoClient(cfg.kakao_rest_api_key),
        vworld=VWorldClient(cfg.vworld_api_key, domain=cfg.vworld_domain),
        cadastral=screening.hazard.cadastral,
    )
    loader = getattr(screening.hazard, "_local_sources_loader", None)
    if loader is not None:
        screening.hazard.local_sources = loader()
        screening.hazard._local_sources_warmup_started = True

    ledger = {s["no"]: s for s in load_sites()}
    picked = [c for c in CASES if c[0] in targets]
    sites = [ledger[c[0]] for c in picked]
    counts, degraded = await warm_sources(
        screening,
        allow_degraded=True,
        centers=[Coordinates(lat=s["lat"], lng=s["lng"]) for s in sites],
    )
    print("예열:", counts, "결손:", [d["source"] for d in degraded], flush=True)

    for no, housing, application, jo, lh_actual, _ in picked:
        site = ledger[no]
        row = await one(site, screening, resolver, housing, application)
        score = row["living_score"]
        rng = f"{row['living_min']}~{row['living_max']}"
        print(
            f"  {no}  {housing:<9}/{application:<9} {row['verdict']:<6} "
            f"우리 {str(score):>4} (범위 {rng}) · 조준환 {jo} · LH실제 {lh_actual}",
            flush=True,
        )


asyncio.run(main(sys.argv[1:] or ["002", "109"]))
