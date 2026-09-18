"""판정에 실제로 붙은 원천을 화면 칩으로 옮기기 위한 모델과 매핑.

이 앱의 목적은 박진주 님이 모은 로컬 납품 파일을 무시하고 공개 API 로
교차검증하는 것이라(docs/COORDINATES_PRIMER_2026-09-13 A-1·A-2), 사용자가
"이 항목이 API 로 판정됐는가, 로컬로 판정됐는가"를 한눈에 구분해야 한다.
그래서 원천 URL·제공기관 라벨 정본을 여기 한 곳에 모아, 서비스 곳곳에서
같은 문자열을 다시 적는 하드코딩 중복을 막는다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.services.cng import CNG_STATION_URL
from app.services.cng_gyeongnam import CNG_GYEONGNAM_URL
from app.services.gg_chemical import GG_CHEMICAL_URL
from app.services.casino_registry import CASINO_REGISTRY_URL
from app.services.city_gas_registry import CITY_GAS_REGISTRY_URL
from app.services.logistics_warehouse import WAREHOUSE_DETAIL_URL
from app.services.lpg_municipal import ODCLOUD_BASE
from app.services.lpg_retailer_file import LPG_RETAILER_URL
from app.services.lpg_seoul import SEOUL_LPG_PAGE_URL
from app.services.safemap_layers import LAYER_BY_ID
from app.services.lpg_station_file import LPG_FILE_URL
from app.services.crematorium import CREMATORIUM_URL
from app.services.factory_registry import FACTORY_PARCEL_URL
from app.services.kgs import KGS_LPG_URL
from app.services.localdata import DATASET_BY_KEY, LOCALDATA_BASE, LocalDataSet
from app.services.opinet import OPINET_AROUND_URL
from app.services.safemap import SAFEMAP_IF0033_URL

# partial: 판정 원천은 아니지만 참고 핀·우회 스캔으로 「일부 연결」된 공개 API.
# bypass: 있는 API 를 겹쳐(브이월드 필지 + 건축물대장 표제부·층별개요 용도코드) 완전 연결과 같은
#         범위를 훑어 낸 우회 원천 — 이번 사업지 스캔이 빠짐없이 끝났을 때만 「우회 연결」.
DataSourceKind = Literal["api", "local", "demo", "partial", "bypass"]


class HazardDataSource(BaseModel):
    """한 항목의 판정에 붙은 원천 하나.

    - api: 제공기관·데이터셋 라벨과 엔드포인트 URL(원천 링크).
    - local: 박진주 님 납품 로컬 파일. 라벨은 "로컬", detail 에 파일명.
    - demo: 데모 데이터.
    """

    kind: DataSourceKind
    label: str
    url: str = ""
    # api 는 데이터셋 슬러그·원천 식별자, local 은 파일명 등 부연.
    detail: str = ""


# 공개 API 원천 식별자 → (제공기관·데이터셋 라벨, 엔드포인트 URL). LOCALDATA(행안부
# 인허가)는 데이터셋마다 라벨·slug 가 달라 아래 localdata_source() 로 따로 만든다.
API_SOURCE_REGISTRY: dict[str, tuple[str, str]] = {
    "opinet": ("오피넷 주유소 정보", OPINET_AROUND_URL),
    "safemap": ("생활안전지도 주유·가스 IF_0033", SAFEMAP_IF0033_URL),
    "kgs": ("가스안전공사 LPG 충전소", KGS_LPG_URL),
    "cng": ("가스안전공사 CNG 충전소", CNG_STATION_URL),
    "lpg_file": ("가스안전공사 LPG 충전소 현황(파일)", LPG_FILE_URL),
    "cng_gyeongnam": ("경상남도 천연가스 충전소", CNG_GYEONGNAM_URL),
    "crematorium": ("공공데이터포털 화장시설", CREMATORIUM_URL),
    "factory_registry": ("산단공 공장등록 필지정보(15087615)", FACTORY_PARCEL_URL),
    "gg_chemical": ("경기데이터드림 유해화학물질 취급사업장", GG_CHEMICAL_URL),
    "logistics_chem_warehouse": ("국토부 물류창고업 · 환경부 보관저장 창고", WAREHOUSE_DETAIL_URL),
    # 카지노는 API 가 아니라 코드에 둔 허가 명단(H-04-바 §8)이지만 원천 링크는 협회 회원사 페이지다.
    "casino_registry": ("한국카지노업관광협회 회원사 · 문체부 허가 18곳", CASINO_REGISTRY_URL),
    # 도시가스 제조시설도 코드 명단(H-02-아 §8). 원천 링크는 민간LNG산업협회 터미널 현황.
    "city_gas_registry": ("도시가스 제조시설 명단 · LNG 생산기지·터미널·바이오가스 12곳", CITY_GAS_REGISTRY_URL),
    "lpg_retailer_file": ("가스안전공사 전국 LPG 판매소 현황(파일 · 2024-03)", LPG_RETAILER_URL),
    "lpg_municipal": ("시군구 액화석유가스업 인허가 파일", ODCLOUD_BASE),
    "lpg_seoul": ("서울 열린데이터광장 액화석유가스업 현황", SEOUL_LPG_PAGE_URL),
    # 원천 링크는 사람이 읽는 데이터셋 페이지로. 엔드포인트를 그대로 열면 키가 없어 접근 거부가 뜬다.
    "building_use_scan": ("건축물대장 용도 스캔(표제부·층별개요 용도코드)", "https://www.data.go.kr/data/15134735/openapi.do"),
    "safemap_chemical": ("생활안전지도 화학물취급시설 IF_0049", LAYER_BY_ID["IF_0049"].page_url if hasattr(LAYER_BY_ID["IF_0049"], "page_url") else "https://www.safemap.go.kr/"),
    "safemap_waste": ("생활안전지도 폐기물처리시설 IF_0051", LAYER_BY_ID["IF_0051"].page_url if hasattr(LAYER_BY_ID["IF_0051"], "page_url") else "https://www.safemap.go.kr/"),
}


def localdata_url(dataset: LocalDataSet) -> str:
    """행안부 지방행정 인허가 데이터셋의 엔드포인트 URL(원천 링크)."""

    suffix = "/info" if dataset.info_suffix else ""
    return f"{LOCALDATA_BASE}/{dataset.slug}{suffix}"


def api_source(identifier: str) -> HazardDataSource | None:
    """공개 API 원천 식별자 하나를 칩으로. 등록되지 않은 식별자면 None."""

    entry = API_SOURCE_REGISTRY.get(identifier)
    if entry is None:
        return None
    label, url = entry
    return HazardDataSource(kind="api", label=label, url=url, detail=identifier)


def partial_source(identifier: str) -> HazardDataSource | None:
    """참고 핀·우회 스캔 원천을 「일부 연결」 칩으로. 판정 원천(api)과 구분해 그린다."""

    entry = API_SOURCE_REGISTRY.get(identifier)
    if entry is None:
        return None
    label, url = entry
    return HazardDataSource(kind="partial", label=label, url=url, detail=identifier)


def bypass_source(identifier: str) -> HazardDataSource | None:
    """우회 원천을 「우회 연결」 칩으로. 스캔이 사업지 주변을 빠짐없이 끝냈을 때 쓴다."""

    entry = API_SOURCE_REGISTRY.get(identifier)
    if entry is None:
        return None
    label, url = entry
    return HazardDataSource(kind="bypass", label=label, url=url, detail=identifier)


def localdata_source(dataset_key: str) -> HazardDataSource | None:
    """행안부 인허가 데이터셋 하나를 칩으로(예: "행안부 인허가 · 고압가스업")."""

    dataset = DATASET_BY_KEY.get(dataset_key)
    if dataset is None:
        return None
    return HazardDataSource(
        kind="api",
        label=f"행안부 인허가 · {dataset.label}",
        url=localdata_url(dataset),
        detail=dataset.slug,
    )


def demo_source() -> HazardDataSource:
    return HazardDataSource(kind="demo", label="데모")


def local_source(detail: str) -> HazardDataSource:
    """박진주 님 로컬 납품 파일 원천. 라벨은 "로컬", 실제 적재 파일명을 detail 로 노출.

    detail 은 번들이 보존한 실제 로드 파일명이다(표준본이면 짧은 구분이 붙기도 한다).
    파일명을 알 수 없으면 빈 문자열 → 칩은 "로컬"만 보인다. 원본 파일명을 하드코딩하면
    표준본으로 적재됐을 때 표시가 어긋나므로(Codex 리뷰) 여기선 받은 값을 그대로 쓴다.
    """

    return HazardDataSource(kind="local", label="로컬", detail=detail)
