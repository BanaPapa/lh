"""서류심사 API. hazard_review 라우터와 같은 잡(job) 운영 방식을 그대로 쓴다."""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import get_settings
from app.hazard_review.router import (
    ensure_api_sources_warmup,
    ensure_local_sources_warmup,
    get_hazard_service,
    get_vworld_client,
)
from app.screening.amenities import AmenityCollector
from app.screening.models import (
    ScreeningJobStart,
    ScreeningJobStatus,
    ScreeningProgressItem,
    ScreeningRequest,
    ScreeningResult,
)
from app.screening.scorebook import (
    BONUS_CRITERION,
    FACILITY_GROUPS,
    OUT_OF_SCOPE_ITEMS,
    PASS_THRESHOLD,
    SCORE_SHEET_LABELS,
    TOTAL_SHEET_POINTS,
    Criterion,
    ScoreSheet,
    sheet_for,
)
from app.screening.front_door import FrontDoorRef, FrontDoorStore
from app.screening.service import ScreeningService
from app.services.cadastral_local import CadastralLocalStore
from app.services.facility_store import FacilityStore
from app.services.kakao import KakaoClient
from app.services.naver_search import NaverSearchClient
from app.services.ncmc_hospital import NcmcHospitalClient
from app.services.tago import TagoClient


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/screening", tags=["screening"])

screening_jobs: dict[str, ScreeningJobStatus] = {}
screening_results: dict[str, ScreeningResult] = {}
screening_cancel_events: dict[str, asyncio.Event] = {}
screening_tasks: set[asyncio.Task[None]] = set()

# 진행 단계. 서비스가 올리는 item_id 와 1:1로 맞춘다.
PROGRESS_ITEMS: tuple[tuple[str, str], ...] = (
    ("STAGE1_COLLECT", "1차 유해시설 조회"),
    ("STAGE1_JUDGE", "1차 매입제외 판정"),
    ("STAGE2_COLLECT", "2차 생활편의시설 조회"),
    ("STAGE2_SCORE", "2차 생활편의성 배점"),
)

# 심사표는 3종뿐이므로 목록 응답에서 전부 펼친다.
SHEET_SAMPLE_APPLICATION: dict[ScoreSheet, str] = {
    "common": "general",
    "youth": "youth",
    "senior": "senior",
}


@lru_cache
def get_front_door_store() -> FrontDoorStore:
    """대학 정문 수기 지정 저장소. 판정 엔진과 지정 API 가 같은 인스턴스를 공유해야
    지정이 다음 검토에 반영된다(저장소 리비전이 수집기 캐시 키에 들어간다)."""

    return FrontDoorStore()


@lru_cache
def get_cadastral_store() -> CadastralLocalStore:
    """전북 연속지적도 로컬 조회기. 인덱스가 없으면 정문 필지 기준은 좌표로 폴백한다."""

    return CadastralLocalStore()


@lru_cache
def get_screening_service() -> ScreeningService:
    config = get_settings()
    return ScreeningService(
        hazard=get_hazard_service(),
        amenities=AmenityCollector(
            kakao=KakaoClient(config.kakao_rest_api_key),
            tago=TagoClient(config.tago_service_key),
            cache_ttl_seconds=config.cache_ttl_seconds,
            facility_store=FacilityStore(),
            hospital_client=NcmcHospitalClient(config.public_data_key),
            front_door_store=get_front_door_store(),
            cadastral_store=get_cadastral_store(),
            # 대학 정문 좌표 확보(LH 과업내용서 예외기준: 대학교=정문).
            naver=NaverSearchClient(
                config.naver_search_client_id, config.naver_search_client_secret
            ),
            # 초·중·고·공원·상업·문화·공공·버스정류장은 시설 필지경계에서 잰다
            # (MEASUREMENT.md §3). 1차 판정과 같은 VWorld 클라이언트를 쓴다.
            vworld=get_vworld_client(),
        ),
        demo_mode=config.demo_mode,
    )


# ---------------------------------------------------------------------------
# 심사표 열람 응답 모델 — 프런트가 등급표를 그대로 그릴 수 있게 전부 내보낸다.
# ---------------------------------------------------------------------------
class ScoreSheetTierView(BaseModel):
    points: int
    condition: str


class ScoreSheetCriterionView(BaseModel):
    key: str
    label: str
    maximum: int
    basis: str
    groups: list[str] = Field(default_factory=list)
    tiers: list[ScoreSheetTierView] = Field(default_factory=list)


class ScoreSheetView(BaseModel):
    key: str
    label: str
    living_maximum: int
    criteria: list[ScoreSheetCriterionView] = Field(default_factory=list)
    bonus: ScoreSheetCriterionView | None = None


class FacilityGroupView(BaseModel):
    key: str
    label: str
    designated_source: str
    point_exception: str = ""


class OutOfScopeItemView(BaseModel):
    label: str
    maximum: int
    reason: str


class ScoreSheetsResponse(BaseModel):
    sheets: list[ScoreSheetView] = Field(default_factory=list)
    facility_groups: list[FacilityGroupView] = Field(default_factory=list)
    out_of_scope: list[OutOfScopeItemView] = Field(default_factory=list)
    out_of_scope_points: int = 0
    total_sheet_points: int = TOTAL_SHEET_POINTS
    pass_threshold: int = PASS_THRESHOLD


def _criterion_view(criterion: Criterion) -> ScoreSheetCriterionView:
    return ScoreSheetCriterionView(
        key=criterion.key,
        label=criterion.label,
        maximum=criterion.maximum,
        basis=criterion.basis,
        groups=list(criterion.groups),
        tiers=[
            ScoreSheetTierView(points=tier.points, condition=tier.condition)
            for tier in criterion.tiers
        ],
    )


@router.get("/scoresheets", response_model=ScoreSheetsResponse)
async def scoresheets() -> ScoreSheetsResponse:
    """2차 심사표 3종의 등급표와 시설군 정의를 통째로 돌려준다."""

    sheets: list[ScoreSheetView] = []
    for key in SCORE_SHEET_LABELS:
        sheet = sheet_for(SHEET_SAMPLE_APPLICATION[key])
        sheets.append(
            ScoreSheetView(
                key=sheet.key,
                label=sheet.label,
                living_maximum=sheet.living_maximum,
                criteria=[_criterion_view(criterion) for criterion in sheet.criteria],
                bonus=_criterion_view(BONUS_CRITERION),
            )
        )
    return ScoreSheetsResponse(
        sheets=sheets,
        facility_groups=[
            FacilityGroupView(
                key=group.key,
                label=group.label,
                designated_source=group.designated_source,
                point_exception=group.point_exception,
            )
            for group in FACILITY_GROUPS
        ],
        out_of_scope=[
            OutOfScopeItemView(label=item.label, maximum=item.maximum, reason=item.reason)
            for item in OUT_OF_SCOPE_ITEMS
        ],
        out_of_scope_points=sum(item.maximum for item in OUT_OF_SCOPE_ITEMS),
    )


# ---------------------------------------------------------------------------
# 대학 정문 기준점 지정 — UI(지적도 클릭 지정)가 이 엔드포인트로 저장한다.
# 저장은 다음 검토 실행부터 반영된다(국장님 §3-3). 통필지 안전장치는 판정
# (측정) 시점에 적용하므로 여기서는 지정을 그대로 보존한다.
# ---------------------------------------------------------------------------
class FrontDoorDesignationRequest(BaseModel):
    # 정문을 지정할 시설명(카카오 place_name 과 같은 값이어야 매칭된다). 대학·종합병원
    # 등 임의 시설. 옛 호출 호환을 위해 `university` 도 그대로 받는다(#8 일반화).
    facility: str = ""
    university: str = ""
    # 지적도에서 클릭한 정문 소재 필지 PNU. 비우면 좌표만으로 지정한다.
    pnu: str = ""
    lat: float | None = None
    lng: float | None = None
    source_label: str = ""

    @property
    def facility_name(self) -> str:
        return (self.facility or self.university or "").strip()


class FrontDoorView(BaseModel):
    # 일반화된 시설명 키. 옛 프런트 호환을 위해 `university` 도 같은 값으로 싣는다.
    facility: str
    university: str
    label: str
    pnu: str = ""
    lat: float | None = None
    lng: float | None = None
    # "manual"(수기) | "auto_stop"(정류장 근사) — 화면에서 근사 여부를 드러낸다.
    origin: str = "manual"
    source_label: str = ""
    # 저장소 리비전. 지정이 바뀔 때마다 오르며, 다음 검토 반영 여부를 확인할 수 있다.
    revision: int = 0


def _front_door_view(ref: FrontDoorRef, revision: int) -> FrontDoorView:
    return FrontDoorView(
        facility=ref.key,
        university=ref.key,
        label=ref.label,
        pnu=ref.pnu,
        lat=ref.lat,
        lng=ref.lng,
        origin=ref.origin,
        source_label=ref.source_label,
        revision=revision,
    )


@router.get("/front-doors", response_model=list[FrontDoorView])
async def list_front_doors(
    store: FrontDoorStore = Depends(get_front_door_store),
) -> list[FrontDoorView]:
    revision = store.revision()
    return [_front_door_view(ref, revision) for ref in store.all().values()]


@router.post("/front-doors", response_model=FrontDoorView)
async def designate_front_door(
    payload: FrontDoorDesignationRequest,
    store: FrontDoorStore = Depends(get_front_door_store),
) -> FrontDoorView:
    if not payload.facility_name:
        raise HTTPException(status_code=422, detail="시설명을 입력하세요.")
    if not payload.pnu.strip() and (payload.lat is None or payload.lng is None):
        raise HTTPException(
            status_code=422,
            detail="정문 필지 PNU 또는 좌표(lat·lng) 중 하나는 있어야 합니다.",
        )
    ref = store.designate(
        payload.facility_name,
        pnu=payload.pnu,
        lat=payload.lat,
        lng=payload.lng,
        source_label=payload.source_label,
    )
    return _front_door_view(ref, store.revision())


@router.delete("/front-doors/{facility}", response_model=list[FrontDoorView])
async def remove_front_door(
    facility: str,
    store: FrontDoorStore = Depends(get_front_door_store),
) -> list[FrontDoorView]:
    store.remove(facility)
    revision = store.revision()
    return [_front_door_view(ref, revision) for ref in store.all().values()]


# 로컬 원천 워밍업을 기다리는 상한. 캐시가 있으면 1초 남짓이고, 캐시가 없는
# 최초 1회만 여기 걸린다. 그때는 예전처럼 부분 상태로 진행하고 다음 판정이 온전해진다.
WARMUP_WAIT_SECONDS = 25.0


async def _await_warmup(service: ScreeningService) -> None:
    """로컬 원천 묶음이 채워질 때까지 기다린다(상한 있음)."""

    task = ensure_local_sources_warmup(service.hazard)
    ensure_api_sources_warmup(service.hazard)
    if task is None:
        return
    try:
        # shield: 상한을 넘겨 우리가 포기해도 워밍업 자체는 계속 돌아야 한다.
        await asyncio.wait_for(asyncio.shield(task), timeout=WARMUP_WAIT_SECONDS)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "로컬 원천 워밍업 %.0f초 내 미완료 — 부분 상태로 심사를 진행한다",
            WARMUP_WAIT_SECONDS,
        )


@router.post("", response_model=ScreeningResult)
async def screen(
    payload: ScreeningRequest,
    service: ScreeningService = Depends(get_screening_service),
) -> ScreeningResult:
    await _await_warmup(service)
    try:
        result = await service.screen(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    screening_results[result.screening_id] = result
    return result


@router.post("/jobs", response_model=ScreeningJobStart)
async def start_screening_job(
    payload: ScreeningRequest,
    service: ScreeningService = Depends(get_screening_service),
) -> ScreeningJobStart:
    # 1차 판정은 유해요소 엔진이 맡고, 그 엔진은 로컬 원천 묶음(factoryON 등록공장
    # 등)을 워밍업으로 채운다. 심사가 유일한 실행 경로이므로 여기서 켠다.
    #
    # 켜 놓고 그냥 지나가면 첫 심사가 빈 묶음을 읽어 공장 나·다목이 「원천 미적재」로
    # 나가고, 두 번째 심사부터 정상이 된다. 같은 사업지의 첫 판정과 두 번째 판정이
    # 다르게 나오는 건 검증 도구로서 결격이다. 그래서 기다린다 — 캐시가 있으면
    # 1초 남짓이고, 캐시가 없는 최초 1회(실측 103초)만 상한에서 끊고 진행한다.
    await _await_warmup(service)
    job_id = str(uuid4())
    screening_jobs[job_id] = ScreeningJobStatus(
        job_id=job_id,
        status="queued",
        progress=0,
        stage="심사 준비",
        message="1차 매입제외 판정과 2차 배점을 준비합니다.",
        items=[
            ScreeningProgressItem(id=item_id, label=label)
            for item_id, label in PROGRESS_ITEMS
        ],
    )
    cancel_event = asyncio.Event()
    screening_cancel_events[job_id] = cancel_event

    async def update_progress(
        item_id: str,
        label: str,
        item_status: str,
        item_progress: int,
        count: int | None,
        message: str,
    ) -> None:
        job = screening_jobs.get(job_id)
        if not job or job.status == "cancelled":
            return
        item = next((candidate for candidate in job.items if candidate.id == item_id), None)
        if not item:
            item = ScreeningProgressItem(id=item_id, label=label)
            job.items.append(item)
        item.label = label
        item.status = item_status  # type: ignore[assignment]
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
        job = screening_jobs[job_id]
        job.status = "running"
        job.stage = "1차 유해시설 조회"
        job.message = "사업지 주변 유해시설 후보를 조회합니다."
        try:
            result = await service.screen(payload, update_progress)
            if cancel_event.is_set():
                job.status = "cancelled"
                job.message = "사용자가 심사를 중단했습니다."
                return
            screening_results[result.screening_id] = result
            job.screening_id = result.screening_id
            job.result = result
            job.status = "completed"
            job.progress = 100
            job.stage = "심사 완료"
            job.message = result.verdict_summary
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.message = "사용자가 심사를 중단했습니다."
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            job.message = "서류심사를 완료하지 못했습니다."

    task = asyncio.create_task(run_job())
    screening_tasks.add(task)
    task.add_done_callback(screening_tasks.discard)
    return ScreeningJobStart(job_id=job_id)


@router.get("/jobs/{job_id}", response_model=ScreeningJobStatus)
async def get_screening_job(job_id: str) -> ScreeningJobStatus:
    job = screening_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="서류심사 작업을 찾을 수 없습니다.")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=ScreeningJobStatus)
async def cancel_screening_job(job_id: str) -> ScreeningJobStatus:
    job = screening_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="서류심사 작업을 찾을 수 없습니다.")
    if job.status in {"completed", "failed", "cancelled"}:
        return job
    cancel_event = screening_cancel_events.get(job_id)
    if cancel_event:
        cancel_event.set()
    job.status = "cancelled"
    job.message = "중단 요청을 반영했습니다."
    return job
