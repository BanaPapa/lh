"""한국가스안전공사 전국 LPG 판매소 현황(ODcloud 15091481) 어댑터 검증 — 주소 지오코딩 · 저장소 캐시."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import app.services.lpg_retailer_file as mod
from app.models import Coordinates
from app.services.facility_store import FacilityStore
from app.services.lpg_retailer_file import (
    LPG_RETAILER_URL,
    LpgRetailerFileClient,
    PublicDataAPIError,
)

ROWS = [
    {"업소명": "(주)태성산업가스경기충전소", "일련번호": None, "주소": "경기 용인시 기흥구 중부대로 14-9 (영덕동)"},
    {"업소명": "대륙가스", "일련번호": 12, "주소": "충북 청주시 상당구 1순환로 1498-2"},
    {"업소명": "주소없음", "일련번호": 13, "주소": ""},
    {"업소명": "지오코딩실패", "일련번호": 14, "주소": "전북 어딘가 모르는곳 1"},
]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(mod, "RETRY_BASE_SECONDS", 0.0)


def _payload(rows: list[dict], total: int | None = None) -> bytes:
    body = {"page": 1, "perPage": 1000, "totalCount": len(rows) if total is None else total,
            "currentCount": len(rows), "matchCount": len(rows), "data": rows}
    return json.dumps(body, ensure_ascii=False).encode()


def _transport(calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        page = int(request.url.params["page"])
        return httpx.Response(200, content=_payload(ROWS if page == 1 else []))

    return httpx.MockTransport(handler)


async def geocode(address: str) -> Coordinates | None:
    if "용인시" in address:
        return Coordinates(lat=37.27, lng=127.12)
    if "청주시" in address:
        return Coordinates(lat=36.62, lng=127.51)
    return None


def test_rows_geocode_and_failures_are_kept() -> None:
    calls: list[str] = []
    client = LpgRetailerFileClient("key", geocode=geocode, transport=_transport(calls))
    retailers = asyncio.run(client.all_retailers())
    assert [r.name for r in retailers] == ["(주)태성산업가스경기충전소", "대륙가스"]
    assert retailers[0].coordinates == Coordinates(lat=37.27, lng=127.12)
    assert retailers[1].record_id == "12"
    assert retailers[0].record_id  # 일련번호가 없어도 유일한 id 를 만든다
    assert [f.name for f in client.geocode_failures] == ["주소없음", "지오코딩실패"]
    assert calls[0].startswith(LPG_RETAILER_URL)
    assert "serviceKey=key" in calls[0]


def test_radius_query() -> None:
    client = LpgRetailerFileClient("key", geocode=geocode, transport=_transport())
    near = asyncio.run(client.retailers_around(Coordinates(lat=37.27, lng=127.12), 50))
    assert [r.name for r in near] == ["(주)태성산업가스경기충전소"]


def test_disabled_without_key_or_geocoder() -> None:
    assert not LpgRetailerFileClient("", geocode=geocode).enabled
    assert not LpgRetailerFileClient("key", geocode=None).enabled


def test_odcloud_error_envelope_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"code": -3, "msg": "등록되지 않은 서비스 입니다."}).encode())

    client = LpgRetailerFileClient("key", geocode=geocode, transport=httpx.MockTransport(handler))
    with pytest.raises(PublicDataAPIError, match="등록되지 않은 서비스"):
        asyncio.run(client.all_retailers())


def test_unauthorized_reports_application_needed() -> None:
    client = LpgRetailerFileClient(
        "key", geocode=geocode, transport=httpx.MockTransport(lambda r: httpx.Response(401))
    )
    with pytest.raises(PublicDataAPIError, match="활용신청") as info:
        asyncio.run(client.all_retailers())
    assert info.value.status_code == 401


def test_store_backed_cache(tmp_path) -> None:
    store = FacilityStore(tmp_path / "f.db")
    calls: list[str] = []
    first = LpgRetailerFileClient("key", geocode=geocode, transport=_transport(calls), store=store)
    rows = asyncio.run(first.all_retailers())
    assert first.loaded_from == "api" and len(rows) == 2
    assert store.sync_state_for(mod.STORE_DATASET_KEY).record_count == 2

    calls.clear()
    second = LpgRetailerFileClient("key", geocode=geocode, transport=_transport(calls), store=store)
    rows2 = asyncio.run(second.all_retailers())
    assert second.loaded_from == "store" and calls == []
    assert {r.record_id for r in rows2} == {r.record_id for r in rows}


def test_refresh_recollects_fresh_store_and_reuses_stored_coordinates(tmp_path) -> None:
    # 야간 사본 갱신: 저장분이 신선해도 새로 받되, 상호·주소가 그대로인 판매소는 저장분의
    # 좌표를 다시 써서 지오코딩(4,542건)은 새 주소에만 쓴다.
    store = FacilityStore(tmp_path / "f.db")
    asyncio.run(
        LpgRetailerFileClient("key", geocode=geocode, transport=_transport(), store=store)
        .all_retailers()
    )
    asked: list[str] = []

    async def counting_geocode(address: str) -> Coordinates | None:
        asked.append(address)
        return None

    calls: list[str] = []
    client = LpgRetailerFileClient(
        "key", geocode=counting_geocode, transport=_transport(calls), store=store
    )

    retailers = asyncio.run(client.refresh())

    assert calls and client.loaded_from == "api"
    assert [r.name for r in retailers] == ["(주)태성산업가스경기충전소", "대륙가스"]
    assert retailers[0].coordinates == Coordinates(lat=37.27, lng=127.12)
    # 저장분에 없던 주소(지오코딩 실패분)만 다시 물었다.
    assert asked and all("모르는곳" in address or "전북" in address for address in asked)
    assert store.sync_state_for(mod.STORE_DATASET_KEY).record_count == 2


def test_refresh_keeps_store_when_new_list_shrinks_abnormally(tmp_path) -> None:
    store = FacilityStore(tmp_path / "f.db")
    asyncio.run(
        LpgRetailerFileClient("key", geocode=geocode, transport=_transport(), store=store)
        .all_retailers()
    )
    empty = httpx.MockTransport(lambda request: httpx.Response(200, content=_payload([])))
    client = LpgRetailerFileClient("key", geocode=geocode, transport=empty, store=store)

    with pytest.raises(PublicDataAPIError, match="저장분을 유지"):
        asyncio.run(client.refresh())

    assert store.sync_state_for(mod.STORE_DATASET_KEY).record_count == 2
