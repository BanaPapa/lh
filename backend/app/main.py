from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.hazard_review.router import router as hazard_review_router
from app.models import GeocodeResponse
from app.screening.router import router as screening_router
from app.services.demo import demo_geocode
from app.services.kakao import KakaoAPIError, KakaoClient
from app.settings_api.router import router as settings_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """기동 직후 전국 목록 공개 API(KGS LPG·CNG·화장시설)와 로컬 원천 묶음을 예열한다.

    첫 심사가 콜드스타트(전량 조회 수십 페이지)를 심사 도중 겪어 「조회 실패 → 검토」로
    떨어지던 것을 막는다(2026-09-14 검수). 데모 모드면 둘 다 아무 것도 하지 않는다.
    """

    from app.hazard_review.router import (
        ensure_api_sources_warmup,
        ensure_local_sources_warmup,
        get_hazard_service,
    )

    service = get_hazard_service()
    ensure_api_sources_warmup(service)
    ensure_local_sources_warmup(service)
    yield


app = FastAPI(
    title="LH 서류심사 API",
    version="0.1.0",
    description="LH 신축매입약정 서류심사 — 1차 매입제외 판정과 2차 생활편의성 배점",
    lifespan=lifespan,
)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)

app.include_router(hazard_review_router)
app.include_router(screening_router)
app.include_router(settings_router)


@app.get("/api/health")
async def health(config: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "status": "ok",
        "demo_mode": config.demo_mode,
        "kakao_configured": bool(config.kakao_rest_api_key),
        "tago_configured": bool(config.tago_service_key),
        "public_data_configured": bool(config.public_data_key),
        "naver_search_configured": config.naver_search_configured,
    }


@app.get("/api/geocode", response_model=GeocodeResponse)
async def geocode(
    query: str = Query(min_length=2, max_length=120),
    config: Settings = Depends(get_settings),
) -> GeocodeResponse:
    if config.demo_mode:
        return GeocodeResponse(query=query, candidates=demo_geocode(query), demo=True)

    try:
        candidates = await KakaoClient(config.kakao_rest_api_key).geocode(query)
    except KakaoAPIError as exc:
        if exc.status_code in {401, 403}:
            raise HTTPException(
                status_code=503,
                detail="카카오 REST API 키 또는 호출 허용 IP 설정을 확인해 주세요.",
            ) from exc
        if exc.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="카카오 API 쿼터를 초과했습니다.",
            ) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return GeocodeResponse(query=query, candidates=candidates, demo=False)
