"""카지노영업소 명단(문체부 허가 18곳 · 한국카지노업관광협회 회원사) 어댑터 검증."""

from __future__ import annotations

import asyncio

from app.models import Coordinates
from app.services.casino_registry import (
    CASINOS,
    CASINO_REGISTRY_URL,
    CasinoRegistryClient,
)


async def geocode(address: str) -> Coordinates | None:
    if "정선군" in address:
        return Coordinates(lat=37.2127, lng=128.8216)
    if "평창군" in address:
        return None  # 알펜시아는 지오코딩 실패로 둔다
    return Coordinates(lat=37.0, lng=127.0)


def test_registry_has_18_licensed_casinos_with_addresses() -> None:
    assert len(CASINOS) == 18
    assert len({c.name for c in CASINOS}) == 18
    assert all(c.address and c.region and c.venue for c in CASINOS)
    regions = {c.region for c in CASINOS}
    assert regions == {"서울", "부산", "인천", "강원", "대구", "제주"}
    assert sum(1 for c in CASINOS if c.region == "제주") == 8
    assert CASINO_REGISTRY_URL.startswith("http")


def test_geocoded_entries_become_casinos_and_failures_are_kept() -> None:
    client = CasinoRegistryClient(geocode=geocode)
    casinos = asyncio.run(client.all_casinos())
    names = [c.name for c in casinos]
    assert "강원랜드카지노" in names
    assert "알펜시아카지노" not in names
    assert [f.name for f in client.geocode_failures] == ["알펜시아카지노"]
    kangwon = next(c for c in casinos if c.name == "강원랜드카지노")
    assert kangwon.coordinates == Coordinates(lat=37.2127, lng=128.8216)
    assert kangwon.venue == "강원랜드호텔"
    assert kangwon.status == "영업"


def test_dormant_casino_keeps_status_label() -> None:
    async def always(address: str) -> Coordinates | None:
        return Coordinates(lat=37.0, lng=127.0)

    client = CasinoRegistryClient(geocode=always)
    casinos = asyncio.run(client.all_casinos())
    alpensia = next(c for c in casinos if c.name == "알펜시아카지노")
    assert alpensia.status == "휴업"


def test_radius_query_and_cache() -> None:
    calls = 0

    async def counting(address: str) -> Coordinates | None:
        nonlocal calls
        calls += 1
        return await geocode(address)

    client = CasinoRegistryClient(geocode=counting)
    near = asyncio.run(client.casinos_around(Coordinates(lat=37.2127, lng=128.8216), 100))
    assert [c.name for c in near] == ["강원랜드카지노"]
    asyncio.run(client.casinos_around(Coordinates(lat=37.0, lng=127.0), 10))
    assert calls == 18


def test_disabled_without_geocoder() -> None:
    client = CasinoRegistryClient(geocode=None)
    assert not client.enabled
    assert asyncio.run(client.all_casinos()) == []
