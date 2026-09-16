"""API 연결 현황 — 앱이 쓰는 외부 API 를 한 표로 보여 주고 점검한다.

설정 패널의 「API 연결」 탭이 쓴다. 키가 있는지(configured)와 실제로 응답하는지
(probe)를 구분해 보여 준다. 키 원문은 어디에도 싣지 않는다.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from app.models import Coordinates

# 점검용 고정 지점(강남역). 정류장·주유소·필지 모두 이 지점 근처에 결과가 있다.
PROBE_POINT = Coordinates(lat=37.4979, lng=127.0276)
PROBE_RADIUS_M = 1500
# 건축물대장 점검용 PNU(서울특별시 중구 태평로1가 31, 서울시청).
PROBE_PNU = "1114010300100310000"
# 원천 하나의 점검 상한(초). 전량 조회형(가스안전공사 등)은 캐시가 없으면 오래 걸린다.
PROBE_TIMEOUT_SECONDS = 60.0


class ConnectionStatus(BaseModel):
    id: str
    label: str
    # 앱 안에서 이 API 가 하는 일. 사용자가 「이게 왜 필요한가」를 바로 알게.
    purpose: str
    # 의존하는 서버 키 이름(.env 키). 비어 있으면 키가 필요 없는 원천이다.
    key_name: str
    configured: bool
    # "ready" | "missing_key" | "ok" | "failed" | "unchecked"
    state: str
    detail: str = ""
    checked_at: float | None = None
    # 발급처. 프런트가 링크로 보여 준다.
    issuer_name: str = ""
    issuer_url: str = ""


class ConnectionsResponse(BaseModel):
    connections: list[ConnectionStatus]
    demo_mode: bool


Probe = Callable[[Any, Any], Awaitable[str]]


@dataclass(frozen=True)
class ConnectionSpec:
    id: str
    label: str
    purpose: str
    key_name: str
    # (hazard_service, screening_service) → 클라이언트. None 이면 미배선.
    client: Callable[[Any, Any], Any]
    # 실제 호출. 성공하면 사람이 읽을 요약("12건")을 돌려준다.
    probe: Probe
    issuer_name: str = ""
    issuer_url: str = ""


@dataclass
class ProbeResult:
    ok: bool
    detail: str
    at: float = field(default_factory=time.time)


# 마지막 점검 결과. 프로세스 안에만 둔다(재시작하면 「미점검」).
_last_results: dict[str, ProbeResult] = {}


def _amenities(screening: Any) -> Any:
    return getattr(screening, "amenities", None)


async def _probe_kakao(hazard: Any, screening: Any) -> str:
    rows = await hazard.kakao.geocode("서울특별시청")
    return f"주소 검색 {len(rows)}건"


async def _probe_tago(hazard: Any, screening: Any) -> str:
    rows = await _amenities(screening).tago.nearby_stops(PROBE_POINT.lat, PROBE_POINT.lng)
    return f"정류장 {len(rows)}건"


async def _probe_naver(hazard: Any, screening: Any) -> str:
    rows = await _amenities(screening).naver.local("서울시청")
    return f"지역검색 {len(rows)}건"


async def _probe_vworld(hazard: Any, screening: Any) -> str:
    parcel = await hazard.vworld.parcel_at(PROBE_POINT.lat, PROBE_POINT.lng)
    return f"필지 {parcel.jibun or parcel.pnu}" if parcel else "필지 응답 없음"


async def _probe_opinet(hazard: Any, screening: Any) -> str:
    rows = await hazard.opinet.stations_around(PROBE_POINT, PROBE_RADIUS_M)
    return f"주유소 {len(rows)}건"


async def _probe_safemap(hazard: Any, screening: Any) -> str:
    rows = await hazard.safemap.stations_around(PROBE_POINT, PROBE_RADIUS_M)
    return f"주유시설 {len(rows)}건"


async def _probe_kgs(hazard: Any, screening: Any) -> str:
    rows = await hazard.kgs_lpg.all_stations()
    return f"전국 {len(rows):,}건"


async def _probe_cng(hazard: Any, screening: Any) -> str:
    rows = await hazard.cng.all_stations()
    return f"전국 {len(rows):,}건"


async def _probe_crematorium(hazard: Any, screening: Any) -> str:
    rows = await hazard.crematorium.all_crematoriums()
    return f"전국 {len(rows):,}건"


async def _probe_ncmc(hazard: Any, screening: Any) -> str:
    rows = await _amenities(screening).hospital_client.general_hospitals_in_sido("서울")
    return f"서울 종합병원 {len(rows)}건"


async def _probe_transfer_center(hazard: Any, screening: Any) -> str:
    rows = await _amenities(screening).transfer_client.all_centers()
    return f"환승센터 {len(rows):,}건"


async def _probe_factory_registry(hazard: Any, screening: Any) -> str:
    rows = await hazard.factory_registry.factories_in_sigungu("11680")  # 서울 강남구
    return f"강남구 등록공장 {len(rows):,}건"


async def _probe_building_register(hazard: Any, screening: Any) -> str:
    result = await hazard.building_register.lookup(PROBE_PNU)
    uses = getattr(result, "uses", None)
    return f"표제부 {len(uses)}건" if uses is not None else "응답 확인"


CONNECTION_SPECS: tuple[ConnectionSpec, ...] = (
    ConnectionSpec(
        "kakao_local", "카카오 로컬(주소·장소 검색)",
        "사업지 주소 검색·좌표 변환, 2차 생활편의시설(학교·공원·역 등) 조회",
        "KAKAO_REST_API_KEY", lambda h, s: h.kakao, _probe_kakao,
        "카카오 개발자", "https://developers.kakao.com/",
    ),
    ConnectionSpec(
        "vworld", "브이월드(국토부 지적도)",
        "사업지 필지 경계, 유해시설·편의시설 필지 경계, 용도지역",
        "VWORLD_API_KEY", lambda h, s: h.vworld, _probe_vworld,
        "브이월드", "https://www.vworld.kr/",
    ),
    ConnectionSpec(
        "tago", "TAGO 국토교통 대중교통",
        "2차 배점의 버스정류장 조회",
        "TAGO_SERVICE_KEY", lambda h, s: getattr(_amenities(s), "tago", None), _probe_tago,
        "공공데이터포털", "https://www.data.go.kr/",
    ),
    ConnectionSpec(
        "naver_search", "네이버 지역검색",
        "대학·종합병원 정문, 역 출입구 보충 조회",
        "NAVER_SEARCH_CLIENT_ID", lambda h, s: getattr(_amenities(s), "naver", None), _probe_naver,
        "네이버 개발자", "https://developers.naver.com/",
    ),
    ConnectionSpec(
        "opinet", "한국석유공사 오피넷",
        "1차 주유소·LPG 충전소(25m) 후보",
        "OPINET_API_KEY", lambda h, s: h.opinet, _probe_opinet,
        "오피넷", "https://www.opinet.co.kr/user/custapi/custApiInfo.do",
    ),
    ConnectionSpec(
        "safemap", "생활안전지도 주유시설",
        "1차 주유소·LPG 충전소 후보 보충(전국 주유시설 현황)",
        "SAFEMAP_API_KEY", lambda h, s: h.safemap, _probe_safemap,
        "생활안전지도", "https://www.safemap.go.kr/",
    ),
    ConnectionSpec(
        "kgs_lpg", "가스안전공사 LPG 충전소",
        "1차 LPG 충전소 후보(전국 목록, 기동 시 예열)",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: h.kgs_lpg, _probe_kgs,
        "공공데이터포털", "https://www.data.go.kr/",
    ),
    ConnectionSpec(
        "cng", "가스안전공사 CNG 충전소(ODcloud 15001508)",
        "1차 자동차용 천연가스 충전소 후보 — 활용신청 승인 전에는 401",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: h.cng, _probe_cng,
        "공공데이터포털", "https://www.data.go.kr/data/15001508/fileData.do",
    ),
    ConnectionSpec(
        "crematorium", "보건복지부 화장시설",
        "1차 화장장(500m) 후보(전국 목록, 기동 시 예열)",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: h.crematorium, _probe_crematorium,
        "공공데이터포털", "https://www.data.go.kr/",
    ),
    ConnectionSpec(
        "ncmc_hospital", "국립중앙의료원 병·의원",
        "2차 의료시설(종합병원) 지정 원천",
        "PUBLIC_DATA_SERVICE_KEY",
        lambda h, s: getattr(_amenities(s), "hospital_client", None), _probe_ncmc,
        "공공데이터포털", "https://www.data.go.kr/",
    ),
    ConnectionSpec(
        "transfer_center", "국토부 환승센터 표준데이터(15034541)",
        "2차 환승시설 지정 원천 — 활용신청 승인 전에는 403(지도 검색으로 근사)",
        "PUBLIC_DATA_SERVICE_KEY",
        lambda h, s: getattr(_amenities(s), "transfer_client", None), _probe_transfer_center,
        "공공데이터포털", "https://www.data.go.kr/data/15034541/standard.do",
    ),
    ConnectionSpec(
        "factory_registry", "산단공 공장등록 필지정보(15087615)",
        "1차 「공장 있음」 검토 표시 — 사업지 시군구 등록공장(도로명주소 → 카카오 지오코딩)",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: h.factory_registry, _probe_factory_registry,
        "공공데이터포털", "https://www.data.go.kr/data/15087615/openapi.do",
    ),
    ConnectionSpec(
        "building_register", "국토부 건축물대장",
        "단란주점·테마파크 AND 조건(근린생활·운동시설) 표제부 용도 확인",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: h.building_register, _probe_building_register,
        "공공데이터포털", "https://www.data.go.kr/",
    ),
)


def _client_enabled(client: Any) -> bool:
    return client is not None and bool(getattr(client, "enabled", False))


def build_connections(hazard: Any, screening: Any, demo_mode: bool) -> ConnectionsResponse:
    """키 유무 + 마지막 점검 결과로 현황을 만든다(원격 호출 없음)."""

    rows: list[ConnectionStatus] = []
    for spec in CONNECTION_SPECS:
        client = spec.client(hazard, screening)
        configured = _client_enabled(client)
        last = _last_results.get(spec.id)
        if not configured:
            state, detail, at = "missing_key", "키가 없어 이 원천은 쓰지 않습니다.", None
        elif last is None:
            state, detail, at = "ready", "키 있음 · 미점검", None
        else:
            state, detail, at = ("ok" if last.ok else "failed"), last.detail, last.at
        rows.append(
            ConnectionStatus(
                id=spec.id,
                label=spec.label,
                purpose=spec.purpose,
                key_name=spec.key_name,
                configured=configured,
                state=state,
                detail=detail,
                checked_at=at,
                issuer_name=spec.issuer_name,
                issuer_url=spec.issuer_url,
            )
        )
    return ConnectionsResponse(connections=rows, demo_mode=demo_mode)


async def check_connections(
    hazard: Any, screening: Any, demo_mode: bool, only: set[str] | None = None
) -> ConnectionsResponse:
    """키가 있는 원천을 실제로 호출해 본다. 실패 사유는 키를 빼고 기록한다."""

    async def one(spec: ConnectionSpec) -> None:
        client = spec.client(hazard, screening)
        if not _client_enabled(client):
            _last_results.pop(spec.id, None)
            return
        try:
            detail = await asyncio.wait_for(
                spec.probe(hazard, screening), timeout=PROBE_TIMEOUT_SECONDS
            )
            _last_results[spec.id] = ProbeResult(True, detail)
        except asyncio.TimeoutError:
            _last_results[spec.id] = ProbeResult(
                False, f"{PROBE_TIMEOUT_SECONDS:.0f}초 안에 응답이 없습니다."
            )
        except Exception as exc:  # noqa: BLE001 — 원천별 실패를 격리해 표에 적는다
            _last_results[spec.id] = ProbeResult(False, _safe_error(exc))

    targets = [s for s in CONNECTION_SPECS if only is None or s.id in only]
    await asyncio.gather(*(one(s) for s in targets), return_exceptions=True)
    return build_connections(hazard, screening, demo_mode)


def _safe_error(exc: BaseException) -> str:
    """예외 문구에서 URL 쿼리(키가 실릴 수 있음)를 지우고 200자로 자른다."""

    text = str(exc) or exc.__class__.__name__
    cleaned = []
    for token in text.split():
        cleaned.append(token.split("?", 1)[0] if "serviceKey=" in token or "key=" in token else token)
    return " ".join(cleaned)[:200]
