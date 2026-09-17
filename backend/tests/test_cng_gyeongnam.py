"""경상남도 천연가스 충전소(ODcloud 15055157) 어댑터 검증 — 주소 지오코딩 원천."""

from __future__ import annotations

import asyncio
import json

import httpx

from app.models import Coordinates
from app.services.cng_gyeongnam import CngGyeongnamClient

ROWS = [
    {"도시가스공급사": "경남에너지", "시군명": "김해시", "연번": 1,
     "위치": "경상남도 김해시 한림면 김해대로 1611", "충전소명": "경남에너지(주)김해CNG충전소"},
    {"도시가스공급사": "경동도시가스", "시군명": "양산시", "연번": 3,
     "위치": "경상남도 양산시 웅상대로 1510", "충전소명": "㈜경동도시가스 웅상CNG충전소"},
]


def _transport():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"page": 1, "perPage": 100, "totalCount": len(ROWS), "currentCount": len(ROWS), "data": ROWS}
        return httpx.Response(200, content=json.dumps(payload, ensure_ascii=False).encode())

    return httpx.MockTransport(handler)


async def geocode(address: str) -> Coordinates | None:
    if "김해" in address:
        return Coordinates(lat=35.28, lng=128.83)
    return None  # 양산은 지오코딩 실패로 둔다


def test_geocoded_rows_become_stations_and_failures_are_kept() -> None:
    client = CngGyeongnamClient("key", geocode=geocode, transport=_transport())
    stations = asyncio.run(client.all_stations())
    assert [s.name for s in stations] == ["경남에너지(주)김해CNG충전소"]
    assert stations[0].region == "경남 김해시" and stations[0].branch == "경남에너지"
    assert [f.name for f in client.geocode_failures] == ["㈜경동도시가스 웅상CNG충전소"]


def test_client_without_geocoder_is_disabled() -> None:
    client = CngGyeongnamClient("key", geocode=None)
    assert not client.enabled
    assert asyncio.run(client.all_stations()) == []
