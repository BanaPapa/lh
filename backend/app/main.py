import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from app.config import Settings, get_settings
from app.hazard_review.multi_parcel import representative_address
from app.hazard_review.router import router as hazard_review_router
from app.models import GeocodeResponse
from app.screening.batch import router as screening_batch_router
from app.screening.router import router as screening_router
from app.services.demo import demo_geocode
from app.services.address_geocoder import (
    GeocodeUnavailable,
    ProviderOutage,
    current_outages,
    mark_outage,
    search_address_candidates,
)
from app.services.kakao import KakaoAPIError, KakaoClient
from app.services.naver_geocode import NaverGeocodeClient
from app.services.vworld import VWorldClient
from app.rate_limit import rate_limit_middleware
from app.settings_api.router import router as settings_router


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """기동 직후 전국 목록 공개 API(KGS LPG·CNG·화장시설)와 로컬 원천 묶음을 예열하고,
    오래된 인허가 원장을 다시 받는다.

    첫 심사가 콜드스타트(전량 조회 수십 페이지)를 심사 도중 겪어 「조회 실패 → 검토」로
    떨어지던 것을 막는다(2026-09-14 검수). 데모 모드면 둘 다 아무 것도 하지 않는다.
    """

    from app.hazard_review.router import (
        ensure_api_sources_warmup,
        ensure_local_sources_warmup,
        get_hazard_service,
    )

    from app.sync_facilities import ensure_facility_sync

    service = get_hazard_service()
    ensure_api_sources_warmup(service)
    ensure_local_sources_warmup(service)
    # 받은 지 하루가 넘은 인허가 원장(숙박·위락·대규모점포 등)을 백그라운드로 다시 받는다.
    # 배포(Cloud Run)에서는 끈다 — 이미지에 구운 원장을 쓴다(FACILITY_SYNC_ON_STARTUP).
    if get_settings().facility_sync_on_startup:
        ensure_facility_sync(get_settings())
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

# 누구나 접속하는 배포에서 심사 시작 요청을 접속자별로 제한한다(설정 0 이면 통과).
app.middleware("http")(rate_limit_middleware)

app.include_router(hazard_review_router)
app.include_router(screening_router)
app.include_router(screening_batch_router)
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
        "naver_geocode_configured": config.naver_geocode_configured,
    }


# 카카오 상태 확인은 10분에 한 번만 실제로 묻는다(확인 자체가 쿼터를 쓰지 않게).
KAKAO_PROBE_INTERVAL_SECONDS = 600
KAKAO_PROBE_ADDRESS = "서울특별시 중구 세종대로 110"
_kakao_probe: dict[str, float] = {"at": float("-inf")}


@app.get("/api/status/providers")
async def provider_status(config: Settings = Depends(get_settings)) -> dict[str, object]:
    """카카오 API 가 지금 막혔는가(일일 쿼터 초과 등). 화면이 안내를 띄우고 지도를 네이버로 돌린다."""

    kakao = KakaoClient(config.kakao_rest_api_key)
    reason = current_outages().get("카카오", "")
    now = time.monotonic()
    if not reason and kakao.enabled and now - _kakao_probe["at"] >= KAKAO_PROBE_INTERVAL_SECONDS:
        _kakao_probe["at"] = now
        try:
            await kakao.geocode(KAKAO_PROBE_ADDRESS)
        except (KakaoAPIError, httpx.HTTPStatusError) as exc:
            mark_outage("카카오", kakao, exc)
            reason = current_outages().get("카카오", str(exc))
        except httpx.TransportError:
            pass  # 일시 통신 오류는 한도 초과로 보지 않는다
    limited = bool(reason)
    return {
        "kakao_limited": limited,
        "kakao_reason": reason,
        "notice": (
            "카카오 API 일일 사용 한도를 넘어 지도를 네이버 지도로 바꿨습니다. 주소 검색은 "
            "네이버·VWorld 로 대신하고, 편의시설 판정은 공공 원천을 써서 결과가 같습니다. "
            "한도는 매일 자정(KST)에 풀립니다."
            if limited
            else ""
        ),
    }


# 배포 서버에서 외부 원천까지 연결되는지 잰다(원천 장애 진단용). 고정된 주소만
# 부르고 키·응답 본문은 싣지 않는다.
EGRESS_PROBE_HOSTS = (
    "https://apis.data.go.kr",
    "https://api.data.go.kr",
    "https://api.odcloud.kr",
    "https://www.safemap.go.kr",
    "https://api.vworld.kr",
    "https://maps.apigw.ntruss.com",
    "https://dapi.kakao.com",
    "https://openapi.naver.com",
)


@app.get("/api/status/egress")
async def egress_status() -> list[dict[str, object]]:
    async def probe(url: str) -> dict[str, object]:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=6.0)) as client:
                response = await client.get(url)
            outcome = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            outcome = exc.__class__.__name__
        return {"host": url, "result": outcome, "ms": round((time.monotonic() - started) * 1000)}

    return list(await asyncio.gather(*(probe(url) for url in EGRESS_PROBE_HOSTS)))


@app.get("/api/geocode", response_model=GeocodeResponse)
async def geocode(
    query: str = Query(min_length=2, max_length=120),
    # 화면에서 고른 지도 세트(kakao·naver). 검색도 같은 세트의 원천을 먼저 쓴다.
    provider: str = Query(default="kakao", pattern="^(kakao|naver)$"),
    config: Settings = Depends(get_settings),
) -> GeocodeResponse:
    if config.demo_mode:
        return GeocodeResponse(query=query, candidates=demo_geocode(query), demo=True)

    # 「363-2, -4, 364-1」·「764-10 외 3필지」처럼 여러 지번이 든 검색어는 대표필지로
    # 지오코딩한다. 필지 합집합은 이어지는 필지 확보(parcels/resolve)가 원문으로 푼다.
    # 카카오가 막히면(일일 쿼터 초과 등) 네이버 지오코딩 → 브이월드 주소검색으로 찾고,
    # 그 사실을 notice 로 알린다.
    lookup = representative_address(query)
    kakao, naver, vworld = geocode_clients(config)
    try:
        candidates, notice = await search_address_candidates(
            lookup, kakao=kakao, naver=naver, vworld=vworld, prefer=provider
        )
        if not candidates and lookup != query:
            candidates, notice = await search_address_candidates(
                query, kakao=kakao, naver=naver, vworld=vworld, prefer=provider
            )
    except GeocodeUnavailable as exc:
        raise _geocode_http_error(exc) from exc

    return GeocodeResponse(query=query, candidates=candidates, demo=False, notice=notice)


def geocode_clients(config: Settings) -> tuple[KakaoClient, NaverGeocodeClient, VWorldClient]:
    return (
        KakaoClient(config.kakao_rest_api_key),
        NaverGeocodeClient(config.naver_map_client_id, config.naver_map_client_secret),
        VWorldClient(config.vworld_api_key, domain=config.vworld_domain),
    )


def _geocode_http_error(exc: GeocodeUnavailable) -> HTTPException:
    """모든 원천이 못 찾았고 오류가 섞였을 때 사용자에게 줄 응답. 카카오 사유를 앞세운다."""

    kakao_error = next(
        (e for e in exc.errors if isinstance(e, (KakaoAPIError, ProviderOutage))), None
    )
    fallback = " 대체 검색(네이버·브이월드)에서도 찾지 못했습니다."
    if isinstance(kakao_error, KakaoAPIError):
        if kakao_error.status_code in {401, 403}:
            return HTTPException(
                status_code=503,
                detail="카카오 REST API 키 또는 호출 허용 IP 설정을 확인해 주세요." + fallback,
            )
        if kakao_error.status_code == 429 or "limit" in str(kakao_error).lower():
            return HTTPException(status_code=429, detail="카카오 API 쿼터를 초과했습니다." + fallback)
        return HTTPException(status_code=502, detail=f"{kakao_error}.{fallback}")
    if isinstance(kakao_error, ProviderOutage):
        return HTTPException(
            status_code=429,
            detail="카카오 주소 검색이 일시 중단 상태입니다(쿼터 초과 등)." + fallback,
        )
    return HTTPException(
        status_code=502, detail="주소 검색 원천에 연결하지 못했습니다. 잠시 후 다시 검색해 주세요."
    )
