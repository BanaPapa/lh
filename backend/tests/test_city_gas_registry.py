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


def test_every_address_ends_with_a_lot_or_building_number() -> None:
    """지번·건물번호 없는 주소는 지오코더가 동 중심점을 돌려준다 — 시설이 엉뚱한 자리에 놓인다.

    2026-10-01: 「여수시 묘도동」만 적힌 동북아LNG허브터미널이 부지에서 2.8km 떨어진 마을 안
    (묘도동 915-1)에 놓여 있었다.
    """

    import re

    for plant in CITY_GAS_PLANTS:
        assert re.search(r"\d+(-\d+)?$", plant.address.split()[-1]), plant.name


def test_addresses_corrected_against_cadastral_parcels() -> None:
    """지오코딩이 안 되거나 엉뚱한 필지로 가던 세 곳은 지적도 현행 지번을 쓴다(2026-10-01).

    통영: 「안정리 1179」·「안정로 770」은 주소검색에 없어 기지가 명단에서 빠졌다 → 2050(공장용지).
    삼척: 「호산해변길 18」은 정문 옆 주차장 필지(505)로 갔다 → 500(공장용지).
    동북아: 지번이 없어 동 중심점으로 갔다 → 2016(준설토 매립장).
    """

    address = {plant.name: plant.address for plant in CITY_GAS_PLANTS}
    assert address["통영LNG생산기지"] == "경상남도 통영시 광도면 안정리 2050"
    assert address["삼척LNG생산기지"] == "강원특별자치도 삼척시 원덕읍 호산리 500"
    assert address["동북아LNG허브터미널"] == "전라남도 여수시 묘도동 2016"


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
