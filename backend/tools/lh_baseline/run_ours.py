"""본 엔진으로 신청 원장 118건을 전수 판정하고 결과를 JSON 으로 남긴다.

LH 원본 모델은 신청유형 개념이 없어 숙박·위락을 전 건에 적용한다. 대조 범위를
가장 넓게 잡기 위해 위락·숙박이 모두 적용되는 house/multi_child 로 돌린다.
좌표는 원장 값을 그대로 쓴다(LH 모델과 같은 입력이어야 대조가 성립한다).

인자를 아무것도 주지 않으면 house/multi_child · data/ours_118d.json 으로
지금까지와 완전히 동일하게 동작한다(하위호환). 여유구간은 코드 인자가 아니라
HAZARD_BOUNDARY_BUFFER_M 환경변수로 바꾸거나, 아래 --buffers 로 한 번의 적재분에
여러 여유구간을 이어서 적용한다(안건 ⑤ 실측 전용 스위치).

원천 예열(2026-08-30):
118건 루프 시작 전에 전국 목록 원천을 먼저 데운다. 예열에 실패한 원천이 하나라도
있으면 판정을 돌리지 않고 즉시 중단한다. 실패한 원천을 안고 118건을 돌리면
캐시 스탬피드로 「검토 필요」에 오염된 쓰레기 스냅샷이 나오기 때문이다. 의도적으로
원천 결손을 재현하려면 --allow-degraded 로 강행한다(결과 meta 에 degraded 목록을 박음).

여유구간 실영향 측정(--buffers):
같은 원천 적재분 위에서 여러 여유구간을 이어서 적용해야 가용성 노이즈가 버퍼
델타를 오염시키지 않는다. --buffers "100,150" 을 주면 예열을 한 번만 하고, 데워진
캐시(single-flight·24h TTL)를 그대로 재사용해 두 버퍼를 이어서 판정한다. 두 판정
사이에 전국 목록 재조회는 일어나지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# backend/ 를 import 경로에 넣는다. `python -m tools.lh_baseline.run_ours`
# 로 실행하면 불필요하지만, 파일을 직접 실행해도 돌아가게 둔다.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Windows 콘솔·파일 리다이렉트는 기본 인코딩이 cp949 라 진행 로그의 em-dash(—)·
# 박스문자(■)에서 UnicodeEncodeError 로 판정 전체가 죽는다. PYTHONUTF8 설정에
# 의존하지 않도록 표준출력을 UTF-8 로 다시 연다(재현성·인계 안정성).
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if callable(_reconfigure):
        try:
            _reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass

from app.config import get_settings
from app.hazard_review import service as hazard_service_module
from app.hazard_review.models import HazardParcelResolveRequest, HazardSite
from app.hazard_review.parcels import MULTI_PARCEL_UNRESOLVED_NOTE, ParcelResolver
from app.hazard_review.wiring import build_hazard_service
from app.models import Coordinates
from app.screening.amenities import AmenityCollector
from app.screening.models import ScreeningRequest
from app.screening.service import ScreeningService
from app.services.kakao import KakaoClient
from app.services.local_sources import HAZARD_DIR, LocalSourcesConfig, _raw_bytes
from app.services.local_wiring import LocalSourcesBundle
from app.services.tago import TagoClient
from app.services.vworld import VWorldClient
from app.settings_api import store

from tools.lh_baseline.lh_replica import LEDGER_PATH, load_sites

def _unresolved_from_note(note: str) -> list[str]:
    """resolve 응답 note 에 섞인 미해석 지번 목록을 구조화해 꺼낸다.

    ParcelResolver 는 다필지(#9) 중 지적도에서 못 찾은 지번을 note 끝에
    `MULTI_PARCEL_UNRESOLVED_NOTE + "a, b, ..."` 로 붙인다. 응답 모델을 건드리지
    않고(회귀 위험 회피) 그 꼬리만 떼어 `unresolved_parcels` 로 결과에 남긴다.
    """

    if not note or MULTI_PARCEL_UNRESOLVED_NOTE not in note:
        return []
    tail = note.split(MULTI_PARCEL_UNRESOLVED_NOTE, 1)[1]
    return [token.strip() for token in tail.split(",") if token.strip()]


DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ours_118d.json")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONCURRENCY = 3

# --buffers 로 여러 버퍼를 돌릴 때 스냅샷 파일명에 쓰는 신청유형 태그.
APP_TAG = {"general": "general", "multi_child": "multichild"}


def _current_buffer() -> int:
    """지금 적용 중인 여유구간(m). 모듈 전역을 매번 읽어 --buffers 반영을 보장한다."""

    return hazard_service_module.BOUNDARY_BUFFER_M


def _set_buffer(value: int) -> None:
    """여유구간을 런타임에 바꾼다. 판정 메서드가 모듈 전역을 직접 읽으므로 여기만 고친다."""

    hazard_service_module.BOUNDARY_BUFFER_M = value


# 전국 목록을 한 번에 받아 캐시하는 원천만 예열 대상이다. (원천속성, 표시명, 조회메서드명).
# opinet 은 사업지별 반경 조회라 전국 예열 대상이 아니다.
WARMUP_SOURCES = [
    ("kgs_lpg", "LPG충전소(KGS)", "all_stations"),
    ("cng", "CNG충전소(KGS ODcloud)", "all_stations"),
    ("safemap", "주유시설(생활안전지도)", "all_stations"),
    ("crematorium", "화장시설", "all_crematoriums"),
    ("noise_emission", "소음배출시설", "all_facilities"),
]


# 원본 zip 안 소음진동배출시설 CSV 멤버(라목 보조 원천). API 미승인(403)이라
# 이 CSV 를 주입해야 factory_noise 가 dataset_missing 을 벗어난다.
_NOISE_CSV_MEMBER = "08_noise_vibration_facilities.csv"


def _provision_env_from_dotenv() -> None:
    """`.env` 의 로컬 원천 경로를 os.environ 으로 올린다(앱과 같은 경로를 쓴다)."""

    store.hydrate_process_env()


def _provision_noise_csv() -> str | None:
    """원본 zip 에서 소음배출시설 CSV 를 추출해 임시 파일 경로를 돌려준다.

    소음배출시설 API 는 미승인(403)이라 CSV 주입으로만 라목이 보조 동작한다. 전북
    CSV 는 원본 zip 안 `유해시설/08_noise_vibration_facilities.csv` 에 있다. NoiseEmission
    클라이언트는 일반 파일 경로만 읽으므로 zip 멤버를 임시 파일로 풀어 주입한다.
    NOISE_EMISSION_CSV_PATH 가 이미 설정돼 있으면 그 값을 존중하고 추출하지 않는다.
    스냅샷 재현성은 CSV 내용에만 달렸고 임시 경로는 메타에 남지 않는다.
    """

    existing = os.environ.get("NOISE_EMISSION_CSV_PATH")
    if existing:
        return existing
    cfg = LocalSourcesConfig.from_env()
    data = _raw_bytes(cfg, HAZARD_DIR, _NOISE_CSV_MEMBER)
    if data is None:
        return None
    import tempfile

    tmp_dir = Path(tempfile.gettempdir()) / "lh_baseline"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    csv_path = tmp_dir / _NOISE_CSV_MEMBER
    csv_path.write_bytes(data)
    os.environ["NOISE_EMISSION_CSV_PATH"] = str(csv_path)
    return str(csv_path)


def build_service() -> tuple[ScreeningService, ParcelResolver, object]:
    """대조 도구 서비스를 실제 앱과 동일한 배선으로 조립한다.

    조립은 app.hazard_review.wiring.build_hazard_service 로 앱(router)과 공유한다.
    반환하는 loader 를 판정 시작 전에 동기로 돌려 완전한 로컬 원천 묶음(factoryON
    원본 PNU 확정 포함)을 주입해야 공장 카테고리가 dataset_missing 으로 남지 않는다.
    """

    s = get_settings()
    kakao = KakaoClient(s.kakao_rest_api_key)
    vworld = VWorldClient(s.vworld_api_key, domain=s.vworld_domain)
    hazard, loader = build_hazard_service(s)
    screening = ScreeningService(
        hazard=hazard,
        amenities=AmenityCollector(
            kakao=kakao,
            tago=TagoClient(s.tago_service_key),
            cache_ttl_seconds=s.cache_ttl_seconds,
        ),
        demo_mode=False,
    )
    # 필지 확보도 앱 라우터와 같은 배선(연속지적도 폴백 포함)으로 만든다.
    resolver = ParcelResolver(kakao=kakao, vworld=vworld, cadastral=hazard.cadastral)
    return screening, resolver, loader


async def one(
    site: dict,
    screening: ScreeningService,
    resolver: ParcelResolver,
    housing_type: str,
    application_type: str,
) -> dict:
    address = f"{site['region']} {site['detail']}"
    coords = Coordinates(lat=site["lat"], lng=site["lng"])
    resolved = await resolver.resolve(
        HazardParcelResolveRequest(name=site["no"], address=address, coordinates=coords)
    )
    request = ScreeningRequest(
        site=HazardSite(
            name=site["no"],
            address=address,
            coordinates=coords,
            housing_type=housing_type,
            application_type=application_type,
            parcels=resolved.parcels,
        ),
        rule_pack_id="",
    )
    result = await screening.screen(request)
    fails = [i for i in result.stage_one.items if i.outcome == "fail"]
    nearest = None
    for item in fails:
        for facility in item.facilities:
            if nearest is None or facility.distance_m < nearest["distance_m"]:
                nearest = {
                    "name": facility.name,
                    "type": facility.facility_type_label,
                    "distance_m": round(facility.distance_m, 1),
                    "address": facility.address,
                }
    two = result.stage_two
    # 시설군별 「원래 상태값」(exclusion_match·review_required·no_conflict_in_snapshot·
    # dataset_missing·geometry_missing·not_applicable)을 함께 남긴다. outcome(pass/
    # fail/review) 만 남기면 dataset_missing 이 pass 로 접혀 「조회 못 함」이 「충돌 없음」
    # 으로 둔갑한다(커밋 f730428 원칙을 스냅샷에도 적용).
    status_by_key = {
        category.key: category.status for category in result.hazard_review.categories
    }
    # 다필지 합집합(#9) 배선 흔적을 결과에 남긴다. parcel_count>1 이면 인접 필지가
    # 합집합에 더해져 최근접 거리가 줄어드는 경로(#9 귀속)가 열린 것이다.
    parcel_pnus = [p.pnu for p in resolved.parcels]
    unresolved_parcels = _unresolved_from_note(resolved.note)
    return {
        "no": site["no"],
        "address": address,
        "geometry_source": resolved.parcels[0].geometry_source if resolved.parcels else "",
        "rule_pack_version": result.rule_pack_version,
        "parcel_count": len(resolved.parcels),
        "parcels": parcel_pnus,
        "unresolved_parcels": unresolved_parcels,
        "verdict": result.verdict,
        "reasons": result.stage_one.reasons,
        "review_reasons": result.stage_one.review_reasons,
        "nearest_fail": nearest,
        "passthrough": len(result.stage_one.passthrough_notes),
        "living_score": two.living_score,
        "living_min": two.living_score_min,
        "living_max": two.living_score_max,
        "living_determined": two.determined,
        "criteria": {c.key: [c.awarded, c.awarded_min, c.awarded_max] for c in two.criteria},
        # 시설군 단위 대조(여유구간 실영향)를 위해 시설군별 상태를 남긴다. outcome 은
        # 심사표 판정, status 는 판정 엔진의 원래 상태값이다.
        "categories": {
            item.key: {
                "outcome": item.outcome,
                "status": status_by_key.get(item.key, ""),
            }
            for item in result.stage_one.items
        },
    }


async def warm_sources(
    screening: ScreeningService,
    allow_degraded: bool,
    centers: list[Coordinates],
) -> tuple[dict[str, int], list[dict]]:
    """118건 루프 시작 전에 전국 목록 원천을 데운다.

    배선된(None 이 아니고 enabled) 원천만 조회한다. 하나라도 실패하면 판정을 돌리지
    않고 즉시 중단한다(allow_degraded 면 강행하되 실패 원천을 degraded 로 남긴다).
    성공한 원천은 적재 건수를 출력해 사람이 눈으로 정상 여부를 본다.

    오피넷은 전국 목록 엔드포인트가 없어 반경검색뿐이라, 사업지들을 덮는 커버 지점을
    한 번 조회해 스냅샷을 데운다(`warm_area`). 이렇게 하면 판정 중 원격 호출이 케이스
    수에 비례해 늘지 않고, 오피넷이 죽어 있으면 여기서 걸려 판정을 시작하지 않는다
    (2026-08-30 양성 대조 오피넷 스로틀 사고 재발 방지).

    반환: (원천명→적재건수, degraded 목록).
    """

    hazard = screening.hazard
    counts: dict[str, int] = {}
    degraded: list[dict] = []

    print("■ 원천 예열")
    for attr, label, method in WARMUP_SOURCES:
        client = getattr(hazard, attr, None)
        if client is None or not getattr(client, "enabled", False):
            print(f"  - {label}: 미배선(건너뜀)")
            continue
        try:
            rows = await getattr(client, method)()
        except Exception as exc:  # 예열 실패는 그대로 붙잡아 중단 판단에 쓴다.
            reason = f"{type(exc).__name__}: {exc}"
            print(f"  [FAIL] {label}: 예열 실패 - {reason}", flush=True)
            degraded.append({"source": attr, "label": label, "reason": reason})
            continue
        counts[attr] = len(rows)
        print(f"  [OK] {label}: {len(rows)}건", flush=True)

    # 오피넷(사업지 커버 지점 예열).
    opinet = getattr(hazard, "opinet", None)
    if opinet is None or not getattr(opinet, "enabled", False):
        print("  - 한국석유공사 오피넷: 미배선(건너뜀)")
    else:
        try:
            n = await opinet.warm_area(centers)
        except Exception as exc:  # 예열 실패는 붙잡아 중단 판단에 쓴다.
            reason = f"{type(exc).__name__}: {exc}"
            print(f"  [FAIL] 한국석유공사 오피넷: 예열 실패 - {reason}", flush=True)
            degraded.append(
                {"source": "opinet", "label": "한국석유공사 오피넷", "reason": reason}
            )
        else:
            counts["opinet"] = n
            print(f"  [OK] 한국석유공사 오피넷: {n}건(커버 조회)", flush=True)

    if degraded and not allow_degraded:
        names = ", ".join(d["label"] for d in degraded)
        print(
            f"\n[중단] 예열 실패 원천이 있어 판정을 중단합니다: {names}\n"
            "  실패한 원천을 안고 118건을 돌리면 「검토 필요」로 오염된 스냅샷이 나옵니다.\n"
            "  원천 상태를 확인하고 다시 실행하세요. 결손 상태를 의도적으로 재현하려면 "
            "--allow-degraded 를 붙이세요.",
            flush=True,
        )
        raise SystemExit(2)

    if degraded:
        print(
            f"\n[경고] --allow-degraded: {len(degraded)}개 원천 결손 상태로 강행합니다.",
            flush=True,
        )
    print()
    return counts, degraded


def inject_local_sources(
    screening: ScreeningService, loader: object
) -> tuple[LocalSourcesBundle, dict]:
    """판정 시작 전에 완전한 로컬 원천 묶음을 동기로 계산해 주입한다.

    앱(router)은 이 계산을 첫 요청에서 백그라운드로 미루지만, CLI 는 백그라운드로
    미룰 이유가 없다. 여기서 동기로 돌려 주입하지 않으면 공장 5개 시설군이 빈
    묶음(dataset_missing)으로 남아, 「조회 안 함」이 「충돌 없음」으로 둔갑한 스냅샷이
    나온다(2026-08-30 미배선 사고). factoryON 원본 PNU 확정(실측 103초·캐시 적중 시
    수초)을 포함한다.
    """

    print("■ 로컬 원천 묶음 적재(factoryON 공장 PNU 확정)")
    bundle: LocalSourcesBundle = loader()  # type: ignore[operator]
    screening.hazard.local_sources = bundle
    summary = {
        "factory_registry_loaded": bundle.factory_registry_loaded,
        "factory_pnus": len(bundle.factory_pnus),
        "factory_registry_resolved_pnus": len(bundle.factory_registry_resolved_pnus),
        "factory_facilities": len(bundle.factory_facilities),
        "cng_facilities": len(bundle.cng_facilities),
        "prohibit_loaded": bundle.prohibit_loaded,
        "prohibit_factory_pnus": len(bundle.prohibit_factory_pnus),
    }
    print(
        f"  factory_pnus={summary['factory_pnus']}"
        f"(원본확정 {summary['factory_registry_resolved_pnus']})"
        f" · 표준본공장 {summary['factory_facilities']}"
        f" · CNG {summary['cng_facilities']}"
        f" · 고시업종공장 {summary['prohibit_factory_pnus']}",
        flush=True,
    )
    for note in bundle.notes:
        print(f"  - {note}", flush=True)
    if not bundle.factory_registry_loaded:
        print(
            "  [경고] factory_registry 미적재 — LH_LOCAL_RAW_PATH 를 확인하세요. "
            "공장 시설군이 dataset_missing 으로 남습니다.",
            flush=True,
        )
    print()
    return bundle, summary


async def preflight_wiring(
    screening: ScreeningService,
    resolver: ParcelResolver,
    probe_site: dict,
    housing_type: str,
    application_type: str,
    allow_degraded: bool,
) -> tuple[dict[str, str], list[str]]:
    """판정 시작 전 시설군별 배선 상태를 실제 엔진으로 한 번 재서 드러낸다.

    소스→시설군 매핑을 손으로 유지하지 않고, 대표 1건을 실제 판정해 각 시설군의
    「원래 상태값」을 읽는다. dataset_missing 은 원천 미배선(조회 자체를 못 함)이라,
    하나라도 있으면 판정을 시작하지 않는다(--allow-degraded 면 강행). 이렇게 하면
    미배선 상태가 pass 로 접혀 보이지 않게 되는 사고가 원천 차단된다.

    반환: (시설군 key→status, dataset_missing 시설군 목록).
    """

    print("■ 시설군 배선 예열(대표 1건 실판정)")
    row = await one(probe_site, screening, resolver, housing_type, application_type)
    if "error" in row:
        print(f"  [중단] 예열 판정 실패: {row['error']}", flush=True)
        raise SystemExit(2)

    categories: dict[str, dict] = row.get("categories", {})
    status_by_key = {key: val.get("status", "") for key, val in categories.items()}

    # not_applicable 은 신청유형 미적용이라 미배선이 아니다. dataset_missing 만 배선
    # 결손으로 본다(geometry_missing 은 경계 미확보라 케이스별이라 여기선 알리기만).
    dataset_missing = sorted(
        key for key, status in status_by_key.items() if status == "dataset_missing"
    )
    geometry_missing = sorted(
        key for key, status in status_by_key.items() if status == "geometry_missing"
    )

    for key in sorted(status_by_key):
        mark = "[MISSING]" if status_by_key[key] == "dataset_missing" else "[OK]"
        print(f"  {mark} {key}: {status_by_key[key]}", flush=True)
    if geometry_missing:
        print(
            f"  (참고) 경계 미확보(geometry_missing) {len(geometry_missing)}종: "
            f"{', '.join(geometry_missing)}",
            flush=True,
        )

    if dataset_missing and not allow_degraded:
        print(
            f"\n[중단] 미배선(dataset_missing) 시설군이 있어 판정을 중단합니다: "
            f"{', '.join(dataset_missing)}\n"
            "  이 상태로 돌리면 「조회 못 함」이 「충돌 없음」으로 둔갑한 스냅샷이 나옵니다.\n"
            "  원천 배선을 확인하고 다시 실행하세요. 결손 상태를 의도적으로 재현하려면 "
            "--allow-degraded 를 붙이세요.",
            flush=True,
        )
        raise SystemExit(2)
    if dataset_missing:
        print(
            f"\n[경고] --allow-degraded: 미배선 시설군 {len(dataset_missing)}종으로 강행합니다.",
            flush=True,
        )
    print()
    return status_by_key, dataset_missing


async def run_pass(
    sites: list[dict],
    screening: ScreeningService,
    resolver: ParcelResolver,
    housing_type: str,
    application_type: str,
) -> list[dict]:
    """현재 여유구간으로 118건을 전수 판정한다."""

    gate = asyncio.Semaphore(CONCURRENCY)
    done = 0

    async def guarded(site: dict) -> dict:
        nonlocal done
        async with gate:
            try:
                row = await one(
                    site, screening, resolver, housing_type, application_type
                )
            except Exception as exc:  # 한 건 실패로 전수 대조를 멈추지 않는다
                row = {"no": site["no"], "error": f"{type(exc).__name__}: {exc}"}
            done += 1
            print(
                f"  [{done}/{len(sites)}] {site['no']} "
                f"{row.get('verdict', row.get('error'))}",
                flush=True,
            )
            return row

    return await asyncio.gather(*(guarded(s) for s in sites))


def _count_source_failures(rows: list[dict]) -> int:
    """판정 중 원천 조회 실패가 사유로 잡힌 케이스 수.

    원천 조회 실패는 '활성 원천 일부(…) 조회가 실패해 …' 문구로 review_reasons 에
    남는다(service.py). 이 문구가 하나라도 있으면 그 케이스는 원천 결손으로 오염된
    것이라 스냅샷에서 걸러 세운다.
    """

    hits = 0
    for row in rows:
        reasons = row.get("review_reasons") or []
        if any("조회가 실패" in str(r) or "조회에 실패" in str(r) for r in reasons):
            hits += 1
    return hits


def _write_snapshot(
    out_path: str,
    rows: list[dict],
    args: argparse.Namespace,
    buffer_m: int,
    warm_counts: dict[str, int],
    degraded: list[dict],
    source_failures: int = 0,
    wiring: dict | None = None,
) -> None:
    # 어떤 조건으로 돌린 스냅샷인지 파일만 보고 알 수 있게 실행 조건을 메타로 박는다.
    # 이 파일은 조준환 국장에게 건네지므로 조건이 파일 안에 남아야 한다.
    meta = {
        "label": args.label,
        "housing_type": args.housing_type,
        "application_type": args.application_type,
        "boundary_buffer_m": buffer_m,
        "generated_at": datetime.now(UTC).isoformat(),
        # 머신 특정 절대경로를 이력에 남기지 않도록 파일명만 남긴다.
        "ledger_path": os.path.basename(str(LEDGER_PATH)),
        "warm_counts": warm_counts,
        # 판정 중 원천 조회 실패가 섞인 케이스 수. 0 이어야 정상 스냅샷이다.
        "source_failures": source_failures,
        "count": len(rows),
    }
    if wiring:
        # 파일만 보고 「이 스냅샷이 무엇을 못 본 상태로 나왔는지」 알 수 있게 배선
        # 요약(적재 원천·건수·미배선 시설군)을 박는다.
        meta["wiring"] = wiring
    if degraded:
        # 결손 상태로 강행했음을 스냅샷에 박아 둔다(정상 스냅샷과 섞이지 않게).
        meta["degraded"] = degraded
    payload = {"meta": meta, "results": rows}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    print(
        f"저장: {out_path} ({len(rows)}건) "
        f"[{args.housing_type}/{args.application_type}, buffer={buffer_m}m]"
    )


def _snapshot_path(application_type: str, buffer_m: int) -> str:
    """--buffers 다중 실행 시 스냅샷 경로. buffer_impact.py 의 PAIRS 명명과 맞춘다."""

    tag = APP_TAG[application_type]
    return os.path.join(DATA_DIR, f"ours_118d_{tag}_b{buffer_m}.json")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="본 엔진 118건 전수 판정 스냅샷 산출")
    p.add_argument(
        "--application-type",
        choices=["general", "multi_child"],
        default="multi_child",
        help="신청유형 (기본 multi_child — 현행 유지)",
    )
    p.add_argument(
        "--housing-type",
        choices=["house", "officetel"],
        default="house",
        help="주택유형 (기본 house)",
    )
    p.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help="출력 경로 (기본 data/ours_118d.json). --buffers 를 주면 무시된다.",
    )
    p.add_argument(
        "--label",
        default="",
        help="결과 JSON 메타에 기록할 실행 라벨",
    )
    p.add_argument(
        "--buffers",
        default="",
        help=(
            "같은 적재분에 이어서 적용할 여유구간(m) 목록. 예: '100,150'. 주면 예열을 "
            "한 번만 하고 각 버퍼로 판정해 data/ours_118d_<유형>_b<버퍼>.json 로 저장한다."
        ),
    )
    p.add_argument(
        "--allow-degraded",
        action="store_true",
        help="원천 예열 실패가 있어도 강행한다(결손 상태 재현 전용). meta 에 degraded 를 박음.",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    # .env 의 로컬 원천 경로를 os.environ 으로 밀어 넣고(LocalSourcesConfig 용),
    # 소음배출시설 CSV(라목 보조)를 원본 zip 에서 추출해 주입한다. get_settings()
    # 첫 호출(build_service) 전에 해야 pydantic 이 CSV 경로를 읽는다.
    _provision_env_from_dotenv()
    _provision_noise_csv()

    sites = [s for s in load_sites() if s["valid"]]
    screening, resolver, loader = build_service()

    # 오피넷 커버 예열에 쓸 사업지 좌표. 원장 좌표를 그대로 쓴다.
    centers = [Coordinates(lat=s["lat"], lng=s["lng"]) for s in sites]

    # 판정 시작 전에 완전한 로컬 원천 묶음을 동기로 주입한다(공장 축 배선).
    bundle, bundle_summary = inject_local_sources(screening, loader)

    # 118건 루프 전에 전국 목록 원천을 예열한다. 실패 시 중단(allow_degraded 예외).
    warm_counts, degraded = await warm_sources(
        screening, args.allow_degraded, centers
    )

    # 시설군 배선 상태를 실제 엔진으로 드러낸다. dataset_missing 이 있으면 중단한다.
    category_status, dataset_missing = await preflight_wiring(
        screening,
        resolver,
        sites[0],
        args.housing_type,
        args.application_type,
        args.allow_degraded,
    )

    # 파일만 보고 무엇을 못 본 스냅샷인지 알 수 있게 배선 요약을 만든다.
    wiring = {
        "local_sources": bundle_summary,
        "warm_counts": warm_counts,
        "category_status": category_status,
        "dataset_missing_categories": dataset_missing,
        "noise_emission_csv_injected": bool(
            os.environ.get("NOISE_EMISSION_CSV_PATH")
        ),
    }
    if degraded:
        wiring["degraded_sources"] = degraded

    buffers = (
        [int(b) for b in args.buffers.split(",") if b.strip()]
        if args.buffers
        else [_current_buffer()]
    )

    for i, buffer_m in enumerate(buffers):
        _set_buffer(buffer_m)
        if len(buffers) > 1:
            print(f"■ 여유구간 {buffer_m}m 판정 ({i + 1}/{len(buffers)})")
        rows = await run_pass(
            sites, screening, resolver, args.housing_type, args.application_type
        )
        # 판정 중 원천 조회 실패가 섞였는지 집계한다. 0 이 아니면 스냅샷을 신뢰하면 안 된다.
        source_failures = _count_source_failures(rows)
        if source_failures:
            print(
                f"  [경고] 원천 조회 실패가 섞인 케이스 {source_failures}건 — 이 스냅샷은 "
                "신뢰하지 말고 원천 상태를 확인한 뒤 재실행하세요.",
                flush=True,
            )
        else:
            print("  원천 조회 실패 0건 — 스냅샷 정상.", flush=True)
        out_path = (
            _snapshot_path(args.application_type, buffer_m)
            if args.buffers
            else args.out
        )
        _write_snapshot(
            out_path,
            rows,
            args,
            buffer_m,
            warm_counts,
            degraded,
            source_failures,
            wiring=wiring,
        )
        print()


if __name__ == "__main__":
    asyncio.run(main())
