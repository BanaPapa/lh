"""조준환님 LOCUS 앱 검증 표(2026-09-08)의 15건을 같은 조건으로 판정한다.

표본·유형의 출처
----------------
조준환님 「조준환 앱 이식 완료」(2026-09-08) §2-4 검증 페이지 캡처에 15건의
접수번호·유형·분류·소재지가 그대로 찍혀 있다. 그 15건은 LH 신청 원장(118건)의
상세주소와 전건 일치하며, 앞서 쓰던 LH 9/7판 17건에서 111·115 두 건이 빠진 것이다.

주소는 원장 값을 그대로 쓴다(좌표도 원장 값). 같은 입력이어야 대조가 성립한다.

대표필지 전제
-------------
이 앱은 사업지를 **대표필지 하나**로 잡는다(원장 좌표가 가리키는 필지). 조준환님은
「외N필지」를 합집합으로 넣는다. 최근접 거리는 어느 쪽이든 결국 **한 필지에서**
찍히지만, 필지를 더하면 그 최솟값이 작아질 수는 있어도 커지지 않는다. 따라서
대표필지 방식은 1차에서 과소검출·2차에서 과소평가 쪽으로 치우칠 수 있다.
이 앱은 담당자가 지도에서 인접 필지를 눌러 사업지에 더할 수 있게 두는 쪽을 택했다.
"""
from __future__ import annotations
import asyncio, json, os, sys
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND)); sys.path.insert(0, str(BACKEND / "tools" / "lh_baseline"))
os.chdir(BACKEND)
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass

from tools.lh_baseline.run_ours import (
    _provision_env_from_dotenv, _provision_noise_csv, one, warm_sources,
)
from tools.lh_baseline.lh_replica import load_sites
from app.models import Coordinates

# (접수번호, housing_type, application_type, 조준환님 엔진 2차, LH 실제, LH 자체모델)
# 조준환님 값은 그분 검증 페이지 캡처에서 읽은 것이다. 「제외」는 1차 제외라 2차 미산출.
CASES = [
    ("002", "officetel", "youth",      40, 40, None),
    ("006", "officetel", "youth",      40, 36, None),
    ("060", "house",     "youth",      40, 36, None),
    ("077", "officetel", "youth",      40, 40, None),
    ("094", "house",     "youth",      36, 37, None),
    ("004", "officetel", "general",    34, 37, 28),
    ("005", "officetel", "general",    38, 35, 35),
    ("035", "officetel", "newlywed",   38, 35, 24),
    ("058", "house",     "newlywed",   40, 40, 38),
    ("079", "house",     "general",    40, 40, 26),
    ("082", "house",     "newlywed", None, 37, 37),   # 조준환님도 1차 제외
    ("091", "officetel", "newlywed",   38, 35, 38),
    ("095", "officetel", "newlywed",   38, 38, 38),
    ("108", "officetel", "general",    35, 35, 35),
    ("109", "house",     "general",    38, 38, 38),
]


async def main() -> None:
    _provision_env_from_dotenv(); _provision_noise_csv()
    from app.screening.router import get_screening_service
    from app.hazard_review.parcels import ParcelResolver
    from app.services.kakao import KakaoClient
    from app.services.vworld import VWorldClient
    from app.config import get_settings

    cfg = get_settings()
    screening = get_screening_service()
    resolver = ParcelResolver(
        kakao=KakaoClient(cfg.kakao_rest_api_key),
        vworld=VWorldClient(cfg.vworld_api_key, domain=cfg.vworld_domain),
        cadastral=screening.hazard.cadastral,
    )
    loader = getattr(screening.hazard, "_local_sources_loader", None)
    if loader:
        screening.hazard.local_sources = loader()
        screening.hazard._local_sources_warmup_started = True

    ledger = {s["no"]: s for s in load_sites()}
    picked = [(ledger[no], ht, at, jo, lh, mdl) for no, ht, at, jo, lh, mdl in CASES]
    centers = [Coordinates(lat=s["lat"], lng=s["lng"]) for s, *_ in picked]
    counts, degraded = await warm_sources(screening, allow_degraded=True, centers=centers)
    print("예열:", counts, "결손:", [d["source"] for d in degraded], "\n")

    rows = []
    for site, ht, at, jo, lh, mdl in picked:
        r = await one(site, screening, resolver, ht, at)
        r.update({"housing_type": ht, "application_type": at,
                  "jo": jo, "lh_actual": lh, "lh_model": mdl})
        ours = None if r["verdict"] == "fail" else r["living_score"]
        mark = "=" if ours == lh else ("제외" if r["verdict"] == "fail" else f"{(ours or 0)-lh:+d}")
        near = r["nearest_fail"]
        print(f"  {site['no']}  {ht:9}/{at:9} {r['verdict']:5} "
              f"우리 {str(ours):>4} · 조준환 {str(jo):>4} · LH실제 {lh:>3}  [{mark}]"
              + (f"  ← {near['name']} {near['distance_m']}m" if near else ""))
        rows.append(r)

    # 파일명은 실행일(YYYYMMDD)로 찍고 기존 스냅샷을 덮지 않는다(날짜 하드코딩 금지).
    stamp = datetime.now().strftime("%Y%m%d")
    dest = Path(__file__).resolve().parent / "data" / f"ours_15case_{stamp}.json"
    dest.write_text(json.dumps({"meta": {"warm": counts}, "rows": rows},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n저장:", dest)


asyncio.run(main())
