"""도시가스 제조시설 명단(LNG 생산기지·터미널·바이오가스 제조소) 어댑터 검증."""

from __future__ import annotations

import asyncio

from app.models import Coordinates
from app.services.city_gas_registry import (
    CITY_GAS_PLANTS,
    CITY_GAS_REGISTRY_URL,
    CityGasRegistryClient,
)


async def geocode(address: str) -> Coordinates | None:
    if "평택시" in address:
        return Coordinates(lat=36.9647, lng=126.8383)
    if "묘도동" in address:
        return None  # 도로명 미부여 부지는 지오코딩 실패로 둔다
    return Coordinates(lat=37.0, lng=127.0)


def test_registry_lists_12_plants_with_kind_status_and_source() -> None:
    assert len(CITY_GAS_PLANTS) == 12
    assert len({p.name for p in CITY_GAS_PLANTS}) == 12
    assert all(p.address and p.operator and p.region and p.source_url for p in CITY_GAS_PLANTS)
    assert {p.kind for p in CITY_GAS_PLANTS} == {"LNG생산기지", "민간LNG터미널", "바이오가스제조"}
    assert {p.status for p in CITY_GAS_PLANTS} == {"운영", "건설중"}
    assert sum(1 for p in CITY_GAS_PLANTS if p.operator == "한국가스공사") == 6
    assert CITY_GAS_REGISTRY_URL.startswith("http")


def test_geocoded_entries_become_plants_and_failures_are_kept() -> None:
    client = CityGasRegistryClient(geocode=geocode)
    plants = asyncio.run(client.all_plants())
    names = [p.name for p in plants]
    assert "평택LNG생산기지" in names and "동북아LNG허브터미널" not in names
    assert [f.name for f in client.geocode_failures] == ["동북아LNG허브터미널"]
    pyeongtaek = next(p for p in plants if p.name == "평택LNG생산기지")
    assert pyeongtaek.kind == "LNG생산기지" and pyeongtaek.status == "운영"
    assert pyeongtaek.facility_id == "평택LNG생산기지"


def test_plants_around_filters_by_radius_and_caches_geocoding() -> None:
    calls = 0

    async def counting(address: str) -> Coordinates | None:
        nonlocal calls
        calls += 1
        return await geocode(address)

    client = CityGasRegistryClient(geocode=counting)
    near = asyncio.run(client.plants_around(Coordinates(lat=36.9650, lng=126.8380), 100))
    assert [p.name for p in near] == ["평택LNG생산기지"]
    asyncio.run(client.plants_around(Coordinates(lat=36.9650, lng=126.8380), 100))
    assert calls == len(CITY_GAS_PLANTS)


def test_disabled_without_geocoder() -> None:
    client = CityGasRegistryClient()
    assert not client.enabled
    assert asyncio.run(client.all_plants()) == []
