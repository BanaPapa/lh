"""국토교통부 물류창고업 등록정보(whsinfoview2) — 환경부 유해화학물질 보관·저장 창고(C 그룹) 어댑터 검증."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.models import Coordinates
import app.services.logistics_warehouse as warehouse_module
from app.services.logistics_warehouse import (
    CHEMICAL_GROUP_CODE,
    WAREHOUSE_DETAIL_URL,
    WAREHOUSE_LIST_URL,
    LogisticsWarehouseClient,
    PublicDataAPIError,
    is_chemical_warehouse,
)
from app.services.address_candidates import address_candidates

LIST_ROWS = [
    {"STORAGE_ITEM": "일반", "RNUM": 1, "COMPANY_NAME": "태공보세창고", "WARE_NO": "1967M62600000049"},
    {"STORAGE_ITEM": "", "RNUM": 2, "COMPANY_NAME": "(주)경기화학물류", "WARE_NO": "2001C64100000224"},
    {"STORAGE_ITEM": "", "RNUM": 3, "COMPANY_NAME": "(주)씨티화학", "WARE_NO": "2010C64300000213"},
    {"STORAGE_ITEM": "", "RNUM": 4, "COMPANY_NAME": "주소없음창고", "WARE_NO": "2011C64100000999"},
]
DETAILS = {
    "2001C64100000224": {"COMPANY_NAME": "(주)경기화학물류", "COMPANY_ADDRESS": "경기도 화성시 비봉면 양노리 641-1 ",
                          "STORAGE_ITEM": "", "WARE_NO": "2001C64100000224", "PRESIDENT_NAME": "홍길동"},
    "2010C64300000213": {"COMPANY_NAME": "(주)씨티화학", "COMPANY_ADDRESS": "충청북도 음성군 생극면 오생리 50-15",
                          "STORAGE_ITEM": "", "WARE_NO": "2010C64300000213"},
    "2011C64100000999": {"COMPANY_NAME": "주소없음창고", "COMPANY_ADDRESS": "", "WARE_NO": "2011C64100000999"},
}


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(warehouse_module, "DETAIL_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(warehouse_module, "RETRY_BASE_SECONDS", 0.0)


def _ok(items: list[dict], total: int) -> bytes:
    body = {"items": items, "header": {"resultMsg": "정상", "title": "t", "TotalCount": total,
                                       "RequestDate": "2026-09-17", "pageNo": "1", "ResultCode": 0,
                                       "TotalPages": 1, "numOfRows": "500"}}
    return json.dumps(body, ensure_ascii=False).encode()


def _transport(detail_calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(WAREHOUSE_LIST_URL):
            page = int(request.url.params["pageNo"])
            return httpx.Response(200, content=_ok(LIST_ROWS if page == 1 else [], len(LIST_ROWS)))
        if url.startswith(WAREHOUSE_DETAIL_URL):
            ware_no = request.url.params["wareNo"]
            if detail_calls is not None:
                detail_calls.append(ware_no)
            return httpx.Response(200, content=_ok([DETAILS[ware_no]], 1))
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def geocode(address: str) -> Coordinates | None:
    if "화성시" in address:
        return Coordinates(lat=37.22, lng=126.85)
    if "음성군" in address:
        return Coordinates(lat=37.05, lng=127.55)
    return None


def test_chemical_group_is_fifth_character_c() -> None:
    assert CHEMICAL_GROUP_CODE == "C"
    assert is_chemical_warehouse("2001C64100000224")
    assert not is_chemical_warehouse("1967M62600000049")
    assert not is_chemical_warehouse("")


def test_only_chemical_group_gets_detail_and_geocoding() -> None:
    detail_calls: list[str] = []
    geocoded: list[str] = []

    async def tracking(address: str) -> Coordinates | None:
        geocoded.append(address)
        return await geocode(address)

    client = LogisticsWarehouseClient("key", geocode=tracking, transport=_transport(detail_calls))
    facilities = asyncio.run(client.all_facilities())

    # 보세창고(M)는 상세조회조차 하지 않는다
    assert sorted(detail_calls) == ["2001C64100000224", "2010C64300000213", "2011C64100000999"]
    assert [f.name for f in facilities] == ["(주)경기화학물류", "(주)씨티화학"]
    assert facilities[0].address == "경기도 화성시 비봉면 양노리 641-1"
    assert facilities[0].record_id == "2001C64100000224"
    assert facilities[0].kind == "보관·저장업(환경부 등록 창고)"
    assert facilities[0].coordinates == Coordinates(lat=37.22, lng=126.85)
    # 주소 없는 창고는 지오코딩하지 않고 실패 목록에 남는다
    assert [f.name for f in client.geocode_failures] == ["주소없음창고"]
    assert "양노리" in geocoded[0]


def test_radius_query_uses_cache() -> None:
    detail_calls: list[str] = []
    client = LogisticsWarehouseClient("key", geocode=geocode, transport=_transport(detail_calls))
    near = asyncio.run(client.facilities_around(Coordinates(lat=37.22, lng=126.85), 50))
    assert [f.name for f in near] == ["(주)경기화학물류"]
    asyncio.run(client.facilities_around(Coordinates(lat=37.05, lng=127.55), 50))
    assert len(detail_calls) == 3


def test_disabled_without_key_or_geocoder() -> None:
    assert not LogisticsWarehouseClient("", geocode=geocode).enabled
    assert not LogisticsWarehouseClient("key", geocode=None).enabled
    assert asyncio.run(LogisticsWarehouseClient("", geocode=geocode).all_facilities()) == []


def test_portal_error_envelope_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {
            "errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "returnAuthMsg": "등록되지 않은 서비스키",
            "returnReasonCode": "30"}}}
        return httpx.Response(200, content=json.dumps(body, ensure_ascii=False).encode())

    client = LogisticsWarehouseClient("key", geocode=geocode, transport=httpx.MockTransport(handler))
    with pytest.raises(PublicDataAPIError, match="SERVICE_KEY_IS_NOT_REGISTERED_ERROR"):
        asyncio.run(client.all_facilities())


def test_http_error_raises_with_status() -> None:
    client = LogisticsWarehouseClient(
        "key", geocode=geocode, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    with pytest.raises(PublicDataAPIError) as info:
        asyncio.run(client.all_facilities())
    assert info.value.status_code == 500


def test_rate_limit_is_retried(monkeypatch) -> None:
    import app.services.logistics_warehouse as mod

    monkeypatch.setattr(mod, "RETRY_BASE_SECONDS", 0.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(WAREHOUSE_LIST_URL):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(429)
            return httpx.Response(200, content=_ok([], 0))
        return httpx.Response(404)

    client = LogisticsWarehouseClient("key", geocode=geocode, transport=httpx.MockTransport(handler))
    assert asyncio.run(client.all_facilities()) == []
    assert attempts["n"] == 3


def test_address_candidates_strip_trailing_business_names() -> None:
    got = address_candidates("충청남도 당진시 신평면 당진항만로 73 영진티앤엠(주)")
    assert got[0] == "충청남도 당진시 신평면 당진항만로 73 영진티앤엠(주)"
    assert got[1] == "충청남도 당진시 신평면 당진항만로 73 영진티앤엠"
    assert "충청남도 당진시 신평면 당진항만로 73" in got
    assert got[-1] == "충청남도 당진시 신평면"
    assert address_candidates("  ") == []


def test_geocoding_retries_with_trimmed_address() -> None:
    tried: list[str] = []

    async def picky(address: str) -> Coordinates | None:
        tried.append(address)
        return Coordinates(lat=37.0, lng=127.0) if address == "경기도 화성시 비봉면 양노리 641-1" else None

    rows = [{"STORAGE_ITEM": "", "RNUM": 1, "COMPANY_NAME": "케미칼", "WARE_NO": "2001C64100000224"}]
    detail = {"COMPANY_NAME": "케미칼", "COMPANY_ADDRESS": "경기도 화성시 비봉면 양노리 641-1 (주)케미칼", "WARE_NO": "2001C64100000224"}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(WAREHOUSE_LIST_URL):
            return httpx.Response(200, content=_ok(rows if request.url.params["pageNo"] == "1" else [], 1))
        return httpx.Response(200, content=_ok([detail], 1))

    client = LogisticsWarehouseClient("key", geocode=picky, transport=httpx.MockTransport(handler))
    facilities = asyncio.run(client.all_facilities())
    assert [f.name for f in facilities] == ["케미칼"]
    assert tried[0].endswith("(주)케미칼") and tried[1] == "경기도 화성시 비봉면 양노리 641-1"


def test_probe_total_reads_only_first_list_row() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.url.params["numOfRows"] == "1"
        return httpx.Response(200, content=_ok([LIST_ROWS[0]], 5977))

    client = LogisticsWarehouseClient("key", geocode=geocode, transport=httpx.MockTransport(handler))
    assert not client.is_warm
    assert asyncio.run(client.probe_total()) == 5977
    assert len(calls) == 1 and calls[0].startswith(WAREHOUSE_LIST_URL)


def test_store_backed_cache_skips_api_when_fresh(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.facility_store import FacilityStore
    import app.services.logistics_warehouse as mod

    store = FacilityStore(tmp_path / "f.db")
    api_calls = {"n": 0}

    def counting_transport():
        base = _transport()

        def handler(request: httpx.Request) -> httpx.Response:
            api_calls["n"] += 1
            return base.handler(request)

        return httpx.MockTransport(handler)

    # 1) 저장분 없음 → API 전량 수집 후 저장
    first = LogisticsWarehouseClient("key", geocode=geocode, transport=counting_transport(), store=store)
    rows = asyncio.run(first.all_facilities())
    assert [r.name for r in rows] == ["(주)경기화학물류", "(주)씨티화학"]
    assert first.loaded_from == "api" and api_calls["n"] > 0
    state = store.sync_state_for(mod.STORE_DATASET_KEY)
    assert state is not None and state.record_count == 2

    # 2) 새 프로세스(새 클라이언트) → 저장분 재사용, API 호출 0
    api_calls["n"] = 0
    second = LogisticsWarehouseClient("key", geocode=geocode, transport=counting_transport(), store=store)
    rows2 = asyncio.run(second.all_facilities())
    assert [r.record_id for r in rows2] == [r.record_id for r in rows]
    assert rows2[0].kind == "보관·저장업(환경부 등록 창고)"
    assert second.loaded_from == "store" and api_calls["n"] == 0

    # 3) 저장분이 오래되면 다시 API
    old = (datetime.now(UTC) - mod.STORE_MAX_AGE - timedelta(days=1)).isoformat()
    with store._connect() as connection:
        connection.execute(
            "UPDATE sync_state SET synced_at = ? WHERE dataset_key = ?", (old, mod.STORE_DATASET_KEY)
        )
    third = LogisticsWarehouseClient("key", geocode=geocode, transport=counting_transport(), store=store)
    asyncio.run(third.all_facilities())
    assert third.loaded_from == "api" and api_calls["n"] > 0
