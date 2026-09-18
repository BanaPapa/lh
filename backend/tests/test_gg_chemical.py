"""경기데이터드림 유해화학물질 취급사업장 현황(ChmstryMttrBizplc) 어댑터 검증.

마목 유독물의 참고 핀 원천이다(판정 아님). 경기 한정 · WGS84 좌표 직접 제공.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.models import Coordinates
from app.services.gg_chemical import (
    EXCLUDED_BUSINESS_TYPES,
    GG_CHEMICAL_URL,
    GgChemicalClient,
    PublicDataAPIError,
)

ROWS = [
    {"SIGUN_NM": "안산시", "SIGUN_CD": "41270", "ENTRPS_NM": "㈜영신금속",
     "LOCPLC_ROADNM_ADDR": "경기도 안산시 성곡동 정왕천동로94번길 6", "LOCPLC_LOTNO_ADDR": None,
     "INDUTYPE_NM": "사용업", "CHMSTRY_ACDNT_OCCUR_YN_STD_DE": None, "FYR_UPLMT_TRTMNT_QTY": None,
     "REFINE_ROADNM_ADDR": "경기도 안산시 단원구 정왕천동로94번길 6",
     "REFINE_LOTNO_ADDR": "경기도 안산시 단원구 성곡동 698-11번지",
     "REFINE_WGS84_LAT": 37.3206569, "REFINE_WGS84_LOGT": 126.7310874,
     "REFINE_ZIPNO": "15415", "BIZREGNO": "1348142184"},
    {"SIGUN_NM": "안산시", "SIGUN_CD": "41270", "ENTRPS_NM": "○○케미칼 판매점",
     "LOCPLC_ROADNM_ADDR": None, "LOCPLC_LOTNO_ADDR": "경기도 안산시 원시동 1-1",
     "INDUTYPE_NM": "판매업", "CHMSTRY_ACDNT_OCCUR_YN_STD_DE": None, "FYR_UPLMT_TRTMNT_QTY": None,
     "REFINE_ROADNM_ADDR": None, "REFINE_LOTNO_ADDR": "경기도 안산시 단원구 원시동 1-1번지",
     "REFINE_WGS84_LAT": 37.31, "REFINE_WGS84_LOGT": 126.73,
     "REFINE_ZIPNO": "15600", "BIZREGNO": "1348142184"},
    # 좌표 없음 → 뺀다
    {"SIGUN_NM": "안성시", "ENTRPS_NM": "좌표없음상사", "INDUTYPE_NM": "보관저장업",
     "REFINE_ROADNM_ADDR": "경기도 안성시 어디로 1", "REFINE_WGS84_LAT": None, "REFINE_WGS84_LOGT": None},
    # 알선판매업·운반업은 취급시설이 그 자리에 없으므로 참고 핀에서 뺀다
    {"SIGUN_NM": "화성시", "ENTRPS_NM": "알선상사", "INDUTYPE_NM": "알선판매업",
     "REFINE_ROADNM_ADDR": "경기도 화성시 어디로 2", "REFINE_WGS84_LAT": 37.2, "REFINE_WGS84_LOGT": 126.8},
    {"SIGUN_NM": "화성시", "ENTRPS_NM": "운반상사", "INDUTYPE_NM": "운반업",
     "REFINE_ROADNM_ADDR": "경기도 화성시 어디로 3", "REFINE_WGS84_LAT": 37.2, "REFINE_WGS84_LOGT": 126.8},
    # 한반도 밖 좌표는 원천 오류로 뺀다
    {"SIGUN_NM": "화성시", "ENTRPS_NM": "좌표오류상사", "INDUTYPE_NM": "제조업",
     "REFINE_ROADNM_ADDR": "경기도 화성시 어디로 4", "REFINE_WGS84_LAT": 0.0, "REFINE_WGS84_LOGT": 0.0},
]


def _payload(rows: list[dict], total: int | None = None, code: str = "INFO-000") -> bytes:
    body = {
        "ChmstryMttrBizplc": [
            {"head": [
                {"list_total_count": len(rows) if total is None else total},
                {"RESULT": {"CODE": code, "MESSAGE": "정상 처리되었습니다."}},
                {"api_version": "1.0"},
            ]},
            {"row": rows},
        ]
    }
    return json.dumps(body, ensure_ascii=False).encode()


def _transport(handler):
    return httpx.MockTransport(handler)


def test_rows_become_facilities_with_business_type_and_exclusions() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_payload(ROWS))

    client = GgChemicalClient("key", transport=_transport(handler))
    facilities = asyncio.run(client.all_facilities())

    assert [f.name for f in facilities] == ["㈜영신금속", "○○케미칼 판매점"]
    assert [f.kind for f in facilities] == ["사용업", "판매업"]
    # 도로명주소 우선, 없으면 지번주소
    assert facilities[0].address == "경기도 안산시 단원구 정왕천동로94번길 6"
    assert facilities[1].address == "경기도 안산시 단원구 원시동 1-1번지"
    assert facilities[0].coordinates == Coordinates(lat=37.3206569, lng=126.7310874)
    # 같은 사업자번호(다공장)라도 record_id 는 서로 달라야 한다
    assert facilities[0].record_id != facilities[1].record_id
    assert {"알선판매업", "운반업"} == set(EXCLUDED_BUSINESS_TYPES)

    request = seen[0]
    assert str(request.url).startswith(GG_CHEMICAL_URL)
    assert request.url.params["KEY"] == "key"
    assert request.url.params["Type"] == "json"
    # WAF 가 기본 UA 를 막으므로 브라우저형 UA 를 보낸다
    assert "Mozilla" in request.headers["user-agent"]


def test_pages_until_total_reached() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params["pIndex"]
        calls.append(page)
        row = dict(ROWS[0])
        row["ENTRPS_NM"] = f"업체{page}"
        return httpx.Response(200, content=_payload([row], total=2))

    client = GgChemicalClient("key", transport=_transport(handler))
    facilities = asyncio.run(client.all_facilities())
    assert [f.name for f in facilities] == ["업체1", "업체2"]
    assert calls == ["1", "2"]


def test_radius_filter_uses_cache() -> None:
    hits = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, content=_payload(ROWS[:2]))

    client = GgChemicalClient("key", transport=_transport(handler))
    near = asyncio.run(client.facilities_around(Coordinates(lat=37.3206569, lng=126.7310874), 50))
    assert [f.name for f in near] == ["㈜영신금속"]
    asyncio.run(client.facilities_around(Coordinates(lat=37.31, lng=126.73), 50))
    assert hits == 1


def test_disabled_without_key() -> None:
    client = GgChemicalClient("")
    assert not client.enabled
    assert asyncio.run(client.all_facilities()) == []


def test_service_error_code_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"RESULT": {"CODE": "ERROR-310", "MESSAGE": "해당하는 서비스를 찾을 수 없습니다."}}
        return httpx.Response(200, content=json.dumps(body, ensure_ascii=False).encode())

    client = GgChemicalClient("key", transport=_transport(handler))
    with pytest.raises(PublicDataAPIError, match="ERROR-310"):
        asyncio.run(client.all_facilities())


def test_waf_html_block_raises_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        html = "<html><body>- 보안 정책에 의해 차단 되었습니다 -</body></html>".encode("euc-kr")
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    client = GgChemicalClient("key", transport=_transport(handler))
    with pytest.raises(PublicDataAPIError, match="해석하지 못했습니다"):
        asyncio.run(client.all_facilities())


def test_http_error_status_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"")

    client = GgChemicalClient("key", transport=_transport(handler))
    with pytest.raises(PublicDataAPIError) as exc_info:
        asyncio.run(client.all_facilities())
    assert exc_info.value.status_code == 500
