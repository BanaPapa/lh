"""시군구 액화석유가스업 인허가 파일(ODcloud) 레지스트리 어댑터 검증."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.models import Coordinates
from app.services.lpg_municipal import (
    LPG_MUNICIPAL_DATASETS,
    LpgMunicipalClient,
    MunicipalDataset,
    classify_kind,
    datasets_for_address,
    parse_row,
)


def test_registry_has_unique_ids_and_regions() -> None:
    ids = [d.dataset_id for d in LPG_MUNICIPAL_DATASETS]
    assert len(ids) == len(set(ids)) and len(ids) >= 44
    assert all(d.sido and d.uddi.startswith("uddi:") for d in LPG_MUNICIPAL_DATASETS)
    buan = next(d for d in LPG_MUNICIPAL_DATASETS if d.dataset_id == "15064201")
    assert buan.sido == "전북" and buan.sigungu == "부안군"


def test_datasets_for_address_matches_sido_and_sigungu() -> None:
    hits = datasets_for_address("전북특별자치도 부안군 부안읍 당산로 91")
    assert [d.dataset_id for d in hits] == ["15064201"]
    # 시도 통합본은 시군구가 비어 있어 시도만 맞으면 걸린다
    gangwon = datasets_for_address("강원특별자치도 원주시 시청로 1")
    assert {d.dataset_id for d in gangwon} >= {"3044402", "15033688"}
    assert datasets_for_address("세종특별자치시 한누리대로 2130") == []
    assert datasets_for_address("") == []


def test_classify_kind() -> None:
    assert classify_kind("판매") == "판매"
    assert classify_kind("액화석유가스 판매사업") == "판매"
    assert classify_kind("저장소") == "저장"
    assert classify_kind("LPG 충전사업") == "충전"
    assert classify_kind("집단공급사업") == "기타"
    assert classify_kind("고압가스판매") == "기타"  # 고법 행은 LPG 판매소가 아니다
    assert classify_kind("") == "미상"


def test_parse_row_uses_column_heuristics() -> None:
    row = {"데이터 기준일자": "2026-08-26", "사무소주소": "전주 어딘가", "사업소주소": "전북특별자치도 부안군 행안면 신기리 186",
           "사업종류": "판매사업", "상호": "부안가스", "영업구분": "신규", "인허가번호": "2026-1", "허가일자": "2026-03-13"}
    parsed = parse_row(row)
    assert parsed is not None
    assert parsed.name == "부안가스" and parsed.address == "전북특별자치도 부안군 행안면 신기리 186"
    assert parsed.kind == "판매" and parsed.kind_raw == "판매사업" and parsed.status == "신규"
    # 업체명·소재지 열 이름이 달라도 잡는다
    other = {"업체명": "영도가스", "소재지": "부산광역시 영도구 태종로 1", "구분": "LPG저장소"}
    assert parse_row(other).kind == "저장"
    # 폐업은 뺀다 / 이름·주소 없으면 None
    assert parse_row({"상호": "x", "소재지": "y", "영업상태": "폐업"}) is None
    assert parse_row({"상호": "x"}) is None


def _dataset(dataset_id: str = "15064201") -> MunicipalDataset:
    return next(d for d in LPG_MUNICIPAL_DATASETS if d.dataset_id == dataset_id)


ROWS = [
    {"사업소주소": "전북특별자치도 부안군 행안면 신기리 186", "사업종류": "판매사업", "상호": "부안가스", "영업구분": "신규"},
    {"사업소주소": "전북특별자치도 부안군 부안읍 당산로 91", "사업종류": "저장소", "상호": "부안저장", "영업구분": "정상"},
    {"사업소주소": "전북특별자치도 부안군 위도면 중선넘길 51", "사업종류": "집단공급사업", "상호": "집단", "영업구분": "신규"},
]


def _transport(status_by_id: dict[str, int] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        dataset_id = str(request.url).split("/api/")[1].split("/")[0]
        status = (status_by_id or {}).get(dataset_id, 200)
        if status != 200:
            return httpx.Response(status)
        page = int(request.url.params["page"])
        body = {"totalCount": len(ROWS), "currentCount": len(ROWS), "data": ROWS if page == 1 else []}
        return httpx.Response(200, content=json.dumps(body, ensure_ascii=False).encode())

    return httpx.MockTransport(handler)


async def geocode(address: str) -> Coordinates | None:
    if "행안면" in address:
        return Coordinates(lat=35.72, lng=126.73)
    if "당산로" in address:
        return Coordinates(lat=35.73, lng=126.73)
    return None


def test_facilities_for_site_geocodes_matching_dataset_only() -> None:
    client = LpgMunicipalClient("key", geocode=geocode, transport=_transport())
    result = asyncio.run(
        client.facilities_for_site("전북특별자치도 부안군 부안읍 당산로 91", Coordinates(lat=35.73, lng=126.73), 2000)
    )
    names = sorted((f.name, f.kind) for f in result.facilities)
    assert names == [("부안가스", "판매"), ("부안저장", "저장")]
    assert result.datasets_used == ["15064201"]
    assert result.unavailable == {}
    assert [f.name for f in result.geocode_failures] == ["집단"]
    assert result.facilities[0].dataset_title.startswith("전북특별자치도 부안군")


def test_unauthorized_dataset_is_reported_not_raised() -> None:
    client = LpgMunicipalClient("key", geocode=geocode, transport=_transport({"15064201": 401}))
    result = asyncio.run(
        client.facilities_for_site("전북특별자치도 부안군 부안읍 당산로 91", Coordinates(lat=35.73, lng=126.73), 2000)
    )
    assert result.facilities == [] and result.datasets_used == []
    assert "활용신청" in result.unavailable["15064201"]


def test_no_dataset_for_region() -> None:
    client = LpgMunicipalClient("key", geocode=geocode, transport=_transport())
    result = asyncio.run(
        client.facilities_for_site("세종특별자치시 한누리대로 2130", Coordinates(lat=36.48, lng=127.29), 2000)
    )
    assert result.facilities == [] and result.datasets_used == [] and not result.covered


def test_disabled_without_key() -> None:
    client = LpgMunicipalClient("", geocode=geocode)
    assert not client.enabled
