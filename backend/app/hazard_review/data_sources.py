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
from app.services.crematorium import CREMATORIUM_URL
from app.services.kgs import KGS_LPG_URL
from app.services.localdata import DATASET_BY_KEY, LOCALDATA_BASE, LocalDataSet
from app.services.opinet import OPINET_AROUND_URL
from app.services.safemap import SAFEMAP_IF0033_URL

DataSourceKind = Literal["api", "local", "demo"]


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
    "crematorium": ("공공데이터포털 화장시설", CREMATORIUM_URL),
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
