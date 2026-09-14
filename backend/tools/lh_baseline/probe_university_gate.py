# -*- coding: utf-8 -*-
"""청년 5건에서 대학교를 무엇을 기준으로 쟀는지 확인한다."""
from __future__ import annotations
import asyncio, os, sys
from pathlib import Path

BACKEND = Path(r"C:\Users\Bana\orca\lh_mvp\backend")
sys.path.insert(0, str(BACKEND)); sys.path.insert(0, str(BACKEND / "tools" / "lh_baseline"))
os.chdir(BACKEND)
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass
import logging
logging.getLogger("httpx").setLevel(logging.WARNING)

from tools.lh_baseline.run_ours import _provision_env_from_dotenv, _provision_noise_csv
from tools.lh_baseline.lh_replica import load_sites
_provision_env_from_dotenv(); _provision_noise_csv()

from app.screening.router import get_screening_service
from app.hazard_review.parcels import ParcelResolver
from app.hazard_review.models import HazardParcelResolveRequest, HazardSite
from app.screening.models import ScreeningRequest
from app.services.kakao import KakaoClient
from app.services.vworld import VWorldClient
from app.config import get_settings
from app.models import Coordinates

YOUTH = ["002", "006", "060", "077", "094"]


async def main() -> None:
    cfg = get_settings()
    svc = get_screening_service()
    resolver = ParcelResolver(
        kakao=KakaoClient(cfg.kakao_rest_api_key),
        vworld=VWorldClient(cfg.vworld_api_key, domain=cfg.vworld_domain),
        cadastral=svc.hazard.cadastral,
    )
    loader = getattr(svc.hazard, "_local_sources_loader", None)
    if loader:
        svc.hazard.local_sources = loader()
        svc.hazard._local_sources_warmup_started = True

    store = getattr(svc.amenities, "front_door_store", None)
    print("정문 저장소:", type(store).__name__ if store else "없음")
    if store is not None:
        try:
            print("  수기 지정 건수:", len(store.all()))
        except Exception as exc:
            print("  건수 조회 실패:", type(exc).__name__)

    ledger = {s["no"]: s for s in load_sites()}
    for no in YOUTH:
        site = ledger[no]
        addr = f"{site['region']} {site['detail']}"
        coords = Coordinates(lat=site["lat"], lng=site["lng"])
        resolved = await resolver.resolve(
            HazardParcelResolveRequest(name=no, address=addr, coordinates=coords))
        req = ScreeningRequest(site=HazardSite(
            name=no, address=addr, coordinates=coords,
            housing_type="officetel" if no in ("002", "006", "077") else "house",
            application_type="youth", parcels=resolved.parcels), rule_pack_id="")
        r = await svc.screen(req)
        edu = next((c for c in r.stage_two.criteria if c.key == "education"), None)
        print("")
        print(f"■ {no} {addr}")
        if edu is None:
            print("   교육여건 항목 없음"); continue
        print(f"   교육여건 {edu.awarded}점 / 최대 {edu.maximum} · 조건: {edu.tier_condition}")
        if edu.basis:
            print(f"   근거: {edu.basis}")
        for g in edu.groups:
            if g.key not in ("university", "school"):
                continue
            print(f"   [{g.key}] " + " · ".join(
                f"{k}={v}" for k, v in g.model_dump().items()
                if k not in ("facilities",) and v not in (None, "", [], 0)))
            for attr in ("note", "front_door_notice", "basis"):
                v = getattr(g, attr, "")
                if v:
                    print(f"      {attr}: {v}")
            for f in (getattr(g, "facilities", None) or [])[:3]:
                print(f"      - {f.name} {getattr(f,'distance_m',None)}m"
                      f" · 기준={getattr(f,'basis','')}"
                      f" · 정문출처={getattr(f,'front_door_source','')}")


asyncio.run(main())
