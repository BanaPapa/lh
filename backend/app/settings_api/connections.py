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
from app.services.gg_chemical import GG_CHEMICAL_DATASET_PAGE_URL
from app.services.casino_registry import CASINO_REGISTRY_URL
from app.services.city_gas_registry import CITY_GAS_REGISTRY_URL
from app.services.logistics_warehouse import WAREHOUSE_DATASET_PAGE_URL
from app.services.lpg_municipal import LPG_MUNICIPAL_DATASETS
from app.services.lpg_retailer_file import LPG_RETAILER_DATASET_PAGE_URL
from app.services.lpg_seoul import SEOUL_LPG_PAGE_URL
from app.services.safemap_layers import SAFEMAP_LAYERS, SafemapLayerClient

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


async def _probe_lpg_file(hazard: Any, screening: Any) -> str:
    rows = await hazard.lpg_file.all_stations()
    return f"전국 {len(rows):,}건"


async def _probe_cng_gyeongnam(hazard: Any, screening: Any) -> str:
    rows = await hazard.cng_gyeongnam.all_stations()
    failed = len(hazard.cng_gyeongnam.geocode_failures)
    return f"경남 {len(rows)}건" + (f" · 지오코딩 실패 {failed}건" if failed else "")


async def _probe_gg_chemical(hazard: Any, screening: Any) -> str:
    rows = await hazard.gg_chemical.all_facilities()
    return f"경기 {len(rows):,}건"


async def _probe_logistics_warehouse(hazard: Any, screening: Any) -> str:
    client = hazard.logistics_warehouse
    if not client.is_warm:
        # 전량 예열(상세 286건 · 초당 1건)은 기동 시 백그라운드로 돈다. 여기서는
        # 목록 1건으로 키·엔드포인트만 확인하고 예열 중임을 알린다.
        total = await client.probe_total()
        return f"원천 응답 정상(등록 창고 {total:,}건) · 환경부 창고 예열 중(약 6분, 백그라운드)"
    rows = await client.all_facilities()
    failed = len(client.geocode_failures)
    origin = ""
    if client.loaded_from == "store" and client.synced_at is not None:
        stamp = client.synced_at.astimezone().strftime("%m-%d %H:%M")
        origin = f" · 저장분 재사용({stamp} 수집)"
    return (
        f"환경부 창고 {len(rows)}건"
        + (f" · 지오코딩 실패 {failed}건" if failed else "")
        + origin
    )


async def _probe_casino(hazard: Any, screening: Any) -> str:
    rows = await hazard.casino_registry.all_casinos()
    failed = len(hazard.casino_registry.geocode_failures)
    return f"카지노 {len(rows)}곳" + (f" · 지오코딩 실패 {failed}곳" if failed else "")


async def _probe_city_gas(hazard: Any, screening: Any) -> str:
    rows = await hazard.city_gas_registry.all_plants()
    failed = hazard.city_gas_registry.geocode_failures
    building = sum(1 for p in rows if p.status == "건설중")
    names = ", ".join(f.name for f in failed)
    return f"제조시설 {len(rows)}곳(건설중 {building})" + (
        f" · 지오코딩 실패 {len(failed)}곳: {names}" if failed else ""
    )


async def _probe_lpg_retailer_file(hazard: Any, screening: Any) -> str:
    client = hazard.lpg_retailer_file
    if not client.is_warm:
        total = await client.probe_total()
        return f"원천 응답 정상(판매소 {total:,}건) · 지오코딩 예열 중(백그라운드)"
    rows = await client.all_retailers()
    failed = len(client.geocode_failures)
    origin = ""
    if client.loaded_from == "store" and client.synced_at is not None:
        stamp = client.synced_at.astimezone().strftime("%m-%d %H:%M")
        origin = f" · 저장분 재사용({stamp} 수집)"
    return f"판매소 {len(rows):,}건" + (f" · 지오코딩 실패 {failed}건" if failed else "") + origin


async def _probe_lpg_municipal(hazard: Any, screening: Any) -> str:
    """레지스트리 51종을 1건씩 찔러 승인·미승인을 센다(데이터셋별 활용신청 안내용)."""

    client = hazard.lpg_municipal
    approved: list[str] = []
    pending: list[str] = []
    stale: list[str] = []
    for dataset in LPG_MUNICIPAL_DATASETS:
        try:
            await client.probe(dataset)
            approved.append(dataset.dataset_id)
        except Exception as exc:  # noqa: BLE001 — 개별 실패는 집계로만
            code = getattr(exc, "status_code", None)
            (stale if code == 404 else pending).append(dataset.dataset_id)
    summary = f"승인 {len(approved)} / 활용신청 필요 {len(pending)} / 버전 재확인 {len(stale)} (총 {len(LPG_MUNICIPAL_DATASETS)})"
    if pending:
        summary += " · 미승인: " + ", ".join(pending[:8]) + (" …" if len(pending) > 8 else "")
    return summary


async def _probe_lpg_seoul(hazard: Any, screening: Any) -> str:
    client = hazard.lpg_seoul
    total = await client.probe_total()
    rows = await client.all_facilities()
    kinds = {}
    for row in rows:
        kinds[row.kind] = kinds.get(row.kind, 0) + 1
    detail = " · ".join(f"{k} {v}" for k, v in sorted(kinds.items()))
    return f"서울 {total}건(좌표 {len(rows)}건: {detail})"


async def _probe_building_scan(hazard: Any, screening: Any) -> str:
    """점검 지점 반경 50m 를 실제로 스캔해 방식이 동작하는지 보인다."""

    result = await hazard.building_scan.scan(PROBE_POINT, 50)
    kinds = {}
    for b in result.buildings:
        kinds[b.kind] = kinds.get(b.kind, 0) + 1
    detail = ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) or "해당 건물 없음"
    return (
        f"건물 {result.parcels_seen} · 대장 조회 {result.lookups} · "
        f"위험물저장및처리시설 {len(result.buildings)}동({detail})"
        + (f" · 조회 실패 {len(result.failed_pnus)}" if result.failed_pnus else "")
    )


async def _probe_crematorium(hazard: Any, screening: Any) -> str:
    rows = await hazard.crematorium.all_crematoriums()
    return f"전국 {len(rows):,}건"


async def _probe_ncmc(hazard: Any, screening: Any) -> str:
    rows = await _amenities(screening).hospital_client.general_hospitals_in_sido("서울")
    return f"서울 종합병원 {len(rows)}건"


async def _probe_seoul_bus(hazard: Any, screening: Any) -> str:
    rows = await _amenities(screening).seoul_bus.all_stops()
    return f"서울 정류소 {len(rows):,}건"


async def _probe_transfer_center(hazard: Any, screening: Any) -> str:
    rows = await _amenities(screening).transfer_client.all_centers()
    return f"환승센터 {len(rows):,}건"


async def _probe_factory_registry(hazard: Any, screening: Any) -> str:
    rows = await hazard.factory_registry.factories_in_sigungu("11680")  # 서울 강남구
    return f"강남구 등록공장 {len(rows):,}건"


def _safemap_layer_probe(layer_id: str) -> Probe:
    async def probe(hazard: Any, screening: Any) -> str:
        key = getattr(getattr(hazard, "safemap", None), "service_key", "")
        total, columns = await SafemapLayerClient(key, layer_id).probe()
        return f"전국 {total:,}건 · 컬럼 {', '.join(columns[:6])}"

    return probe


def _safemap_layer_client(layer_id: str):
    def client(hazard: Any, screening: Any) -> Any:
        key = getattr(getattr(hazard, "safemap", None), "service_key", "")
        return SafemapLayerClient(key, layer_id) if key else None

    return client


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
        "seoul_bus", "서울 열린데이터광장 버스정류소",
        "2차 버스정류장 — 서울 사업지(국토부 TAGO 가 서울을 제공하지 않음)",
        "SEOUL_OPEN_DATA_KEY", lambda h, s: getattr(_amenities(s), "seoul_bus", None), _probe_seoul_bus,
        "서울 열린데이터광장", "https://data.seoul.go.kr/together/mypage/actKeyPage.do",
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
        "1차 자동차용 천연가스 충전소 후보(전국 190곳 · 위경도)",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: h.cng, _probe_cng,
        "공공데이터포털", "https://www.data.go.kr/data/15001508/fileData.do",
    ),
    ConnectionSpec(
        "lpg_file", "가스안전공사 LPG 충전소 현황 파일(ODcloud 15001643)",
        "1차 LPG 충전소 보조 — 조회 API 와 같은 명부의 최신 파일(관리구분·위경도), 40m 중복 제거",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: getattr(h, "lpg_file", None), _probe_lpg_file,
        "공공데이터포털", "https://www.data.go.kr/data/15001643/fileData.do",
    ),
    ConnectionSpec(
        "cng_gyeongnam", "경상남도 천연가스 충전소(ODcloud 15055157)",
        "1차 CNG 충전소 지역 보조 — 경남 12곳, 주소를 카카오로 지오코딩",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: getattr(h, "cng_gyeongnam", None),
        _probe_cng_gyeongnam,
        "공공데이터포털", "https://www.data.go.kr/data/15055157/fileData.do",
    ),
    ConnectionSpec(
        "gg_chemical", "경기데이터드림 유해화학물질 취급사업장(ChmstryMttrBizplc)",
        "1차 마목 유독물 참고 핀(경기 한정 · 업종구분 · WGS84) — 판정 아님",
        "GG_OPEN_API_KEY", lambda h, s: getattr(h, "gg_chemical", None), _probe_gg_chemical,
        "경기데이터드림", GG_CHEMICAL_DATASET_PAGE_URL,
    ),
    ConnectionSpec(
        "logistics_warehouse", "국토교통부 물류창고업 등록정보(3048029)",
        "1차 마목 유독물 참고 핀 — 환경부 등록 보관·저장 창고(전국 286곳), 주소를 카카오로 지오코딩",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: getattr(h, "logistics_warehouse", None),
        _probe_logistics_warehouse,
        "공공데이터포털", WAREHOUSE_DATASET_PAGE_URL,
    ),
    ConnectionSpec(
        "city_gas_registry", "도시가스 제조시설 명단(LNG 생산기지·터미널·바이오가스 12곳)",
        "1차 아목 도시가스 제조시설(50m) — 도시가스사업법 가스제조시설 명단을 카카오로 지오코딩",
        "KAKAO_REST_API_KEY", lambda h, s: getattr(h, "city_gas_registry", None), _probe_city_gas,
        "민간LNG산업협회·한국가스공사", CITY_GAS_REGISTRY_URL,
    ),
    ConnectionSpec(
        "casino_registry", "카지노영업소 명단(문체부 허가 18곳)",
        "1차 바목 카지노영업소(25m · 다자녀) — 코드 명단을 카카오로 지오코딩",
        "KAKAO_REST_API_KEY", lambda h, s: getattr(h, "casino_registry", None), _probe_casino,
        "한국카지노업관광협회", CASINO_REGISTRY_URL,
    ),
    ConnectionSpec(
        "lpg_retailer_file", "가스안전공사 전국 LPG 판매소 현황(ODcloud 15091481)",
        "1차 나목 LPG 판매소 원천 — 전국 4,542건(2024-03 일회성), 주소를 카카오로 지오코딩",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: getattr(h, "lpg_retailer_file", None),
        _probe_lpg_retailer_file,
        "공공데이터포털", LPG_RETAILER_DATASET_PAGE_URL,
    ),
    ConnectionSpec(
        "lpg_municipal", "시군구 액화석유가스업 인허가 파일(ODcloud 51종)",
        "1차 나목 LPG 판매소 보강·저장소 참고 핀 — 사업지 시군구 파일만 조회. 데이터셋별 활용신청 필요",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: getattr(h, "lpg_municipal", None),
        _probe_lpg_municipal,
        "공공데이터포털", "https://www.data.go.kr/",
    ),
    ConnectionSpec(
        "lpg_seoul", "서울 열린데이터광장 액화석유가스업 현황(SeoulListLPGSales)",
        "1차 나목 — 서울 사업지의 LPG 판매소 판정·저장소 참고 핀(실시간 인허가, 주소 지오코딩)",
        "SEOUL_OPEN_DATA_KEY", lambda h, s: getattr(h, "lpg_seoul", None), _probe_lpg_seoul,
        "서울 열린데이터광장", SEOUL_LPG_PAGE_URL,
    ),
    ConnectionSpec(
        "building_use_scan", "건축물대장 용도 스캔(브이월드 건물통합정보 + 국토부 표제부·층별개요)",
        "1차 우회 원천(일부 연결) — 반경 안 필지의 표제부 주용도 「위험물저장및처리시설」 건물을 "
        "기타용도 문자열로 LPG 저장·위험물·도시가스·유독물·미분류로 가르고, 주유소·LPG 충전·판매·"
        "고압가스는 연결된 원천이 덮으므로 뺀다. 참고 핀 전용(판정 아님). 필지당 표제부 1회 호출",
        "PUBLIC_DATA_SERVICE_KEY", lambda h, s: getattr(h, "building_scan", None), _probe_building_scan,
        "국토교통부 건축HUB", "https://www.data.go.kr/data/15134735/openapi.do",
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
    *(
        ConnectionSpec(
            f"safemap_{layer.layer_id.lower()}",
            f"생활안전지도 {layer.label}({layer.agency}, {layer.layer_id})",
            f"{layer.purpose} (레이어별 데이터 사용신청 · 2026-09-17 승인)",
            "SAFEMAP_API_KEY", _safemap_layer_client(layer.layer_id), _safemap_layer_probe(layer.layer_id),
            "생활안전지도 오픈API", "https://www.safemap.go.kr/opna/data/dataList.do",
        )
        for layer in SAFEMAP_LAYERS
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
    # 네트워크 단계 실패는 원문(영문 httpx 문구)보다 원인이 읽히게 바꾼다. 키·앱 문제가
    # 아니라 원천 서버 쪽 장애임을 드러내야 담당자가 엉뚱한 곳을 손대지 않는다.
    lowered = text.lower()
    if "all connection attempts failed" in lowered or "connecterror" in lowered:
        return "원천 서버에 연결할 수 없습니다(응답 없음) — 키 문제가 아니라 제공처 서버 장애·차단 가능성. 잠시 뒤 다시 점검"
    if "timed out" in lowered or "timeout" in lowered:
        return "원천 서버 응답 시간 초과 — 제공처 서버 지연 가능성. 잠시 뒤 다시 점검"
    cleaned = []
    for token in text.split():
        cleaned.append(token.split("?", 1)[0] if "serviceKey=" in token or "key=" in token else token)
    return " ".join(cleaned)[:200]
