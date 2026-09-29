from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import get_settings
from app.hazard_review.models import (
    CadastralParcel,
    CadastralParcelsResponse,
    HazardApplicationCombo,
    HazardApplicationTypesResponse,
    HazardJobStart,
    HazardJobStatus,
    HazardParcelResolveRequest,
    HazardParcelResolveResponse,
    HazardProgressItem,
    HazardReviewRequest,
    HazardReviewResult,
    HazardRulePack,
)
from app.hazard_review.parcels import ParcelResolver
from app.hazard_review.rulebook import (
    APPLICATION_TYPE_LABELS,
    HOUSING_TYPE_LABELS,
    RULES,
    thresholds_for,
)
from app.hazard_review.service import HazardReviewService, get_rule_pack, get_rule_packs
from app.hazard_review.wiring import build_hazard_service
from app.services.kakao import KakaoClient
from app.services.cadastral_local import CadastralLocalStore
from app.services.vworld import VWorldAPIError, VWorldClient


# 지도 한 타일에 그릴 필지 상한. 프런트는 사업지 반경 3km 를 0.008°(약 890m)
# 타일로 나눠 요청한다. 도심 타일(0.008°≈0.6km²)은 수서역 250m 박스(284필지)
# 밀도로 환산하면 필지가 수천 건이라 600 이면 자주 잘렸다. 800 으로 올려 도심
# 타일의 절단 빈도를 낮춘다(여전히 넘치면 응답 truncated 로 "일부만 표시" 처리).
MAX_MAP_PARCELS = 800

# 요청당 조회 폭 상한. 약 1.1km. 타일 폭 0.008° 는 위·경도 모두 이 안에 든다.
MAX_BOUNDS_SPAN_DEG = 0.01

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hazard-review", tags=["hazard-review"])

hazard_jobs: dict[str, HazardJobStatus] = {}
hazard_reviews: dict[str, HazardReviewResult] = {}
hazard_cancel_events: dict[str, asyncio.Event] = {}
hazard_tasks: set[asyncio.Task[None]] = set()

# 로컬 원천 묶음 워밍업 태스크. GC 로 취소되지 않게 참조를 붙잡아 둔다.
_local_sources_warmup_tasks: set[asyncio.Task[None]] = set()


def ensure_local_sources_warmup(service: HazardReviewService) -> asyncio.Task | None:
    """로컬 원천 묶음 워밍업을 한 번만 백그라운드로 시작한다.

    safemap 콜드스타트 조치와 같은 원칙이다. 첫 검토 요청이 factoryON 확정(실측
    103초)을 동기로 기다리지 않게 하고, 계산이 끝나면 service.local_sources 를
    통째로 교체한다. 워밍업 전에는 빈 묶음이라 공장 카테고리가 dataset_missing 으로
    남는다(조회 안 함이 충돌 없음으로 둔갑하지 않는다). 러닝 루프가 없으면(비요청
    경로) 아무 것도 하지 않는다 — 그 경우 빈 묶음을 그대로 두는 편이 안전하다.
    """

    running = getattr(service, "_local_sources_warmup_task", None)
    loader = getattr(service, "_local_sources_loader", None)
    if loader is None or getattr(service, "_local_sources_warmup_started", False):
        # 이미 도는 중이면 그 작업을 돌려준다. 호출자가 기다릴 수 있게.
        return running if running is not None and not running.done() else None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    service._local_sources_warmup_started = True  # type: ignore[attr-defined]

    async def _warm() -> None:
        try:
            bundle = await loop.run_in_executor(None, loader)
            logger.info(
                "로컬 원천 워밍업 완료 — factoryON %s · PNU %s · 고시업종 %s",
                bundle.factory_registry_loaded,
                len(bundle.factory_pnus),
                len(bundle.prohibit_factory_pnus),
            )
        except Exception:
            logger.exception("로컬 원천 워밍업 실패")
            # 워밍업 실패는 치명적이지 않다. 빈 묶음을 유지하면 공장 카테고리는
            # dataset_missing 으로 남는다. 플래그를 되돌려 다음 요청이 재시도한다.
            service._local_sources_warmup_started = False  # type: ignore[attr-defined]
            return
        # 속성 교체는 원자적이다(GIL). 서비스는 매번 self.local_sources 를 새로 읽어
        # 빈→완전 묶음 교체가 즉시 반영된다. 교체는 단조적(정보가 늘기만)이라
        # 진행 중 검토가 부분 상태를 봐도 충돌을 놓치는 방향으로 틀어지지 않는다.
        service.local_sources = bundle

    task = loop.create_task(_warm())
    _local_sources_warmup_tasks.add(task)
    task.add_done_callback(_local_sources_warmup_tasks.discard)
    service._local_sources_warmup_task = task  # type: ignore[attr-defined]
    return task


# 전국 목록 공개 API 예열 태스크. GC 로 취소되지 않게 참조를 붙잡아 둔다.
_api_sources_warmup_tasks: set[asyncio.Task[None]] = set()

# (서비스 속성, 라벨, 전량 조회 메서드). 전국 목록을 캐시하는 원천만 — 반경 조회형
# (오피넷·LOCALDATA 캐시)은 예열 대상이 아니다. tools/lh_baseline/run_ours.py 의
# WARMUP_SOURCES 와 같은 목록을 유지한다.
API_WARMUP_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("kgs_lpg", "LPG 충전소(가스안전공사)", "all_stations"),
    ("cng", "CNG 충전소(가스안전공사 ODcloud)", "all_stations"),
    ("crematorium", "화장시설(복지부)", "all_crematoriums"),
    ("logistics_warehouse", "환경부 보관·저장 창고(국토부 물류창고업)", "all_facilities"),
    ("casino_registry", "카지노영업소 명단(문체부 허가 18곳)", "all_casinos"),
    ("city_gas_registry", "도시가스 제조시설 명단(LNG 생산기지·터미널 12곳)", "all_plants"),
    ("lpg_retailer_file", "LPG 판매소(가스안전공사 파일 15091481)", "all_retailers"),
    ("gg_chemical", "경기 유해화학물질 취급사업장(경기데이터드림)", "all_facilities"),
    ("lpg_seoul", "서울 액화석유가스업(열린데이터광장)", "all_facilities"),
    # 생활안전지도 참고 핀·주석 레이어. 콜드 로드가 20초씩이라 심사 중에 일어나지 않게 예열.
    ("chemical_feed", "생활안전지도 화학물취급시설(IF_0049)", "all_facilities"),
    ("waste_feed", "생활안전지도 폐기물처리시설(IF_0051)", "all_facilities"),
    ("emission_feed", "생활안전지도 환경배출시설(IF_0040)", "all_facilities"),
)


def ensure_api_sources_warmup(service: HazardReviewService) -> asyncio.Task | None:
    """전국 목록 공개 API 캐시를 한 번만 백그라운드로 데운다.

    2026-09-14 검수: 서버 기동 후 첫 심사에서 KGS LPG 전량 조회(37페이지)가 심사 중에
    처음 일어나 「조회 실패 → 검토 필요」로 떨어졌고, 두 번째 심사부터 정상이었다.
    기동 직후 미리 채워 두면 첫 심사가 콜드스타트를 겪지 않는다. 실패는 로그만 남기고
    삼킨다 — 예열은 편의이고, 판정 시점 조회가 실패하면 그때 failed_sources 로 드러난다.
    데모 모드·러닝 루프 없음이면 아무 것도 하지 않는다.
    """

    if service.demo_mode or getattr(service, "_api_sources_warmup_started", False):
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    service._api_sources_warmup_started = True  # type: ignore[attr-defined]

    async def _warm_one(attr: str, label: str, method: str) -> None:
        client = getattr(service, attr, None)
        if client is None or not getattr(client, "enabled", False):
            return
        try:
            rows = await getattr(client, method)()
            logger.info("공개 API 예열 완료 — %s %d건", label, len(rows))
        except Exception as exc:  # noqa: BLE001 — 원천별 실패를 격리한다
            logger.warning("공개 API 예열 실패 — %s: %s", label, exc)

    async def _warm() -> None:
        await asyncio.gather(
            *(_warm_one(*spec) for spec in API_WARMUP_SOURCES),
            return_exceptions=True,
        )

    task = loop.create_task(_warm())
    _api_sources_warmup_tasks.add(task)
    task.add_done_callback(_api_sources_warmup_tasks.discard)
    return task


@lru_cache
def get_hazard_service() -> HazardReviewService:
    # 서비스 조립은 대조 도구(tools/lh_baseline)와 공유한다(app.hazard_review.wiring).
    # 조립 경로가 두 벌이 되면 인자 누락 사고가 재발하므로 단일 출처로 모은다.
    service, loader = build_hazard_service(get_settings())
    # 로컬 원천 묶음 워밍업 배선. loader 는 factoryON 원본 PNU 확정(실측 103초·캐시
    # 적중 시 수초)을 포함한 완전한 묶음을 계산한다. 첫 검토 요청에서 스레드풀로
    # 시작되고(요청 경로를 막지 않는다) 끝나면 service.local_sources 를 교체한다.
    service._local_sources_loader = loader  # type: ignore[attr-defined]
    service._local_sources_warmup_started = False  # type: ignore[attr-defined]
    return service


@router.get("/rule-packs", response_model=list[HazardRulePack])
async def rule_packs() -> list[HazardRulePack]:
    return get_rule_packs()


@router.get("/rule-packs/{rule_pack_id}", response_model=HazardRulePack)
async def rule_pack(rule_pack_id: str) -> HazardRulePack:
    pack = get_rule_pack(rule_pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="유해시설 규칙팩을 찾을 수 없습니다.")
    return pack


@router.get("/application-types", response_model=HazardApplicationTypesResponse)
async def application_types() -> HazardApplicationTypesResponse:
    """신청유형 조합과 조합별 적용 임계거리 매트릭스를 돌려준다.

    프런트가 주택유형·신청유형을 고른 뒤 어떤 Rule 이 몇 m 로 적용되는지 미리
    보여줄 수 있게 매트릭스를 그대로 내보낸다.
    """

    combos: list[HazardApplicationCombo] = []
    for housing in ("house", "officetel"):
        for application in (
            "general",
            "multi_child",
            "newlywed",
            "youth",
            "senior",
        ):
            combos.append(
                HazardApplicationCombo(
                    housing_type=housing,  # type: ignore[arg-type]
                    housing_type_label=HOUSING_TYPE_LABELS[housing],
                    application_type=application,  # type: ignore[arg-type]
                    application_type_label=APPLICATION_TYPE_LABELS[application],
                    thresholds=thresholds_for(housing, application),  # type: ignore[arg-type]
                )
            )
    return HazardApplicationTypesResponse(
        housing_types=HOUSING_TYPE_LABELS,
        application_types=APPLICATION_TYPE_LABELS,
        rules=[
            {
                "rule_id": rule.rule_id,
                "label": rule.label,
                "matrix_column": rule.column,
            }
            for rule in RULES
        ],
        combos=combos,
    )


@lru_cache
def get_parcel_resolver() -> ParcelResolver:
    config = get_settings()
    return ParcelResolver(
        kakao=KakaoClient(config.kakao_rest_api_key),
        vworld=VWorldClient(config.vworld_api_key, domain=config.vworld_domain),
        # 연속지적도 로컬 인덱스가 있으면 VWorld 실패 시에도 실제 지적경계를 확보해
        # 임시 필지(provisional)로 떨어지지 않는다. 없으면 조용히 폴백한다.
        cadastral=CadastralLocalStore(),
    )


@router.post("/parcels/resolve", response_model=HazardParcelResolveResponse)
async def resolve_parcels(
    payload: HazardParcelResolveRequest,
    resolver: ParcelResolver = Depends(get_parcel_resolver),
) -> HazardParcelResolveResponse:
    return await resolver.resolve(payload)


@lru_cache
def get_vworld_client() -> VWorldClient:
    config = get_settings()
    return VWorldClient(config.vworld_api_key, domain=config.vworld_domain)


@router.get("/parcels/in-bounds", response_model=CadastralParcelsResponse)
async def parcels_in_bounds(
    south: float = Query(ge=-90, le=90),
    west: float = Query(ge=-180, le=180),
    north: float = Query(ge=-90, le=90),
    east: float = Query(ge=-180, le=180),
    limit: int = Query(default=MAX_MAP_PARCELS, ge=1, le=MAX_MAP_PARCELS),
    vworld: VWorldClient = Depends(get_vworld_client),
) -> CadastralParcelsResponse:
    """지도 화면에 보이는 필지 경계를 돌려준다.

    사용자가 어느 필지를 누르는지 눈으로 보고 고를 수 있어야 한다.
    """

    if north <= south or east <= west:
        raise HTTPException(status_code=422, detail="조회 영역이 올바르지 않습니다.")
    if north - south > MAX_BOUNDS_SPAN_DEG or east - west > MAX_BOUNDS_SPAN_DEG:
        raise HTTPException(
            status_code=422,
            detail="조회 영역이 너무 넓습니다. 지도를 더 확대해 주세요.",
        )
    if not vworld.enabled:
        return CadastralParcelsResponse(
            parcels=[],
            note="VWorld 지적도 키가 설정되지 않아 필지 경계를 표시할 수 없습니다.",
        )

    try:
        features = await vworld.parcels_in_box(south, west, north, east, limit)
    except VWorldAPIError as exc:
        # 배경 레이어일 뿐이므로 실패해도 지도 자체는 계속 쓸 수 있어야 한다.
        return CadastralParcelsResponse(parcels=[], note=str(exc))

    return CadastralParcelsResponse(
        parcels=[
            CadastralParcel(
                pnu=feature.pnu,
                jibun=feature.jibun,
                address=feature.address,
                area_m2=feature.area_m2,
                geometry=feature.ring,
            )
            for feature in features
        ],
        truncated=len(features) >= limit,
    )


@router.post("/jobs", response_model=HazardJobStart)
async def start_hazard_job(
    payload: HazardReviewRequest,
    service: HazardReviewService = Depends(get_hazard_service),
) -> HazardJobStart:
    if not get_rule_pack(payload.rule_pack_id):
        raise HTTPException(status_code=422, detail="선택한 규칙팩을 찾을 수 없습니다.")
    # 여기(async 핸들러)는 러닝 루프가 보장돼 워밍업을 안전하게 시작할 수 있다.
    # 첫 요청은 확정을 기다리지 않고 즉시 작업을 큐잉한다. 워밍업이 끝나기 전
    # 진행되는 검토는 공장 카테고리가 dataset_missing 으로 정직하게 남는다.
    ensure_local_sources_warmup(service)
    ensure_api_sources_warmup(service)
    job_id = str(uuid4())
    item_specs = [
        ("PARCEL", "신청필지 확보"),
        ("RULES", "규칙팩 검증"),
        ("FACTORY", "공장"),
        ("HAZMAT", "위험물 저장·처리시설"),
        ("FUEL", "주유·석유·천연가스"),
        ("AMUSEMENT", "위락시설"),
        ("LODGING", "일반숙박시설"),
        ("CREMATION", "화장장·군부대·사격장"),
        ("GEOMETRY", "시설 필지·경계 확인"),
        ("DECISION", "규칙 판정"),
        ("REPORT", "결과·증빙 생성"),
    ]
    hazard_jobs[job_id] = HazardJobStatus(
        job_id=job_id,
        status="queued",
        progress=0,
        stage="검토 준비",
        message="규칙과 자료 조회 작업을 준비합니다.",
        items=[
            HazardProgressItem(id=item_id, label=label)
            for item_id, label in item_specs
        ],
    )
    cancel_event = asyncio.Event()
    hazard_cancel_events[job_id] = cancel_event

    async def update_progress(
        item_id: str,
        label: str,
        item_status: str,
        item_progress: int,
        count: int | None,
        message: str,
    ) -> None:
        job = hazard_jobs.get(job_id)
        if not job or job.status == "cancelled":
            return
        item = next((candidate for candidate in job.items if candidate.id == item_id), None)
        if not item:
            item = HazardProgressItem(id=item_id, label=label)
            job.items.append(item)
        item.label = label
        item.status = item_status
        item.progress = item_progress
        item.count = count
        item.message = message
        job.status = "running"
        job.stage = label
        job.message = message
        job.progress = round(
            sum(progress_item.progress for progress_item in job.items)
            / max(len(job.items), 1)
        )

    async def run_job() -> None:
        job = hazard_jobs[job_id]
        job.status = "running"
        job.stage = "신청필지 확보"
        job.message = "선택 필지와 적용 규칙을 확인합니다."
        try:
            result = await service.review(payload, update_progress, cancel_event)
            if cancel_event.is_set():
                job.status = "cancelled"
                job.message = "사용자가 검토를 중단했습니다."
                return
            hazard_reviews[result.review_id] = result
            job.review_id = result.review_id
            job.result = result
            job.status = "completed"
            job.progress = 100
            job.stage = "검토 완료"
            job.message = result.overall_summary
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.message = "사용자가 검토를 중단했습니다."
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.message = "유해시설 검토를 완료하지 못했습니다."

    task = asyncio.create_task(run_job())
    hazard_tasks.add(task)
    task.add_done_callback(hazard_tasks.discard)
    return HazardJobStart(job_id=job_id)


@router.get("/jobs/{job_id}", response_model=HazardJobStatus)
async def get_hazard_job(job_id: str) -> HazardJobStatus:
    job = hazard_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="유해시설 검토 작업을 찾을 수 없습니다.")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=HazardJobStatus)
async def cancel_hazard_job(job_id: str) -> HazardJobStatus:
    job = hazard_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="유해시설 검토 작업을 찾을 수 없습니다.")
    if job.status in {"completed", "failed", "cancelled"}:
        return job
    cancel_event = hazard_cancel_events.get(job_id)
    if cancel_event:
        cancel_event.set()
    job.status = "cancelled"
    job.message = "중단 요청을 반영했습니다."
    return job


@router.get("/reviews/{review_id}", response_model=HazardReviewResult)
async def get_review(review_id: str) -> HazardReviewResult:
    review = hazard_reviews.get(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="유해시설 검토 결과를 찾을 수 없습니다.")
    return review


@router.get("/reviews/{review_id}/evidence.json")
async def get_review_evidence(review_id: str) -> dict[str, object]:
    review = hazard_reviews.get(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="유해시설 검토 결과를 찾을 수 없습니다.")
    return review.model_dump(mode="json")

