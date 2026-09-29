"""학교 필지를 좌표가 아니라 지번주소(PNU)로 먼저 찾는지.

LH앱은 학교 지번주소로 PNU 를 만들어 필지를 받는다. 좌표 필지는 옆 필지·도로에 떨어져
50~150m 길게 나왔다(2026-09-30 전주완산서초: 좌표 필지 219m, 주소 필지 169m = LH).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models import Coordinates
from app.screening.address_parcels import AddressParcelResolver, _candidate_pnus
from app.screening.amenities import AmenityCollector
from app.services.geo import offset_coordinates
from app.services.vworld import ParcelFeature
from tests.test_screening_amenities import (
    CENTER,
    FakeKakao,
    FakeTago,
    FakeVWorld,
    place,
    square_ring,
)

SCHOOL_PNU = "5211112300102270001"


def address_doc(b_code: str, dong: str, main: str, sub: str = "") -> dict[str, Any]:
    return {
        "address_name": f"전북특별자치도 전주시 {dong} {main}-{sub}",
        "address": {
            "b_code": b_code,
            "region_3depth_name": dong,
            "mountain_yn": "N",
            "main_address_no": main,
            "sub_address_no": sub,
        },
    }


class AddressKakao(FakeKakao):
    def __init__(self, documents: dict[str, list[dict[str, Any]]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.documents = documents
        self.address_calls: list[str] = []

    async def address_documents(self, query: str) -> list[dict[str, Any]]:
        self.address_calls.append(query)
        return list(self.documents.get(query, []))


class PnuVWorld(FakeVWorld):
    """좌표 필지는 반폭 30m, PNU 필지는 정해 둔 도형을 돌려준다."""

    def __init__(self, by_pnu: dict[str, ParcelFeature], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.by_pnu = by_pnu
        self.pnu_calls: list[str] = []

    async def parcel_by_pnu(self, pnu: str):
        self.pnu_calls.append(pnu)
        return self.by_pnu.get(pnu)


def school_collector(parcel_center: Coordinates, half: float) -> tuple[AmenityCollector, PnuVWorld]:
    kakao = AddressKakao(
        documents={"전주초등학교 주소": [address_doc("5211112300", "서완산동1가", "227", "1")]},
        categories={"SC4": [place("전주초등학교", 400, "교육,학문 > 학교 > 초등학교")]},
    )
    vworld = PnuVWorld(
        by_pnu={
            SCHOOL_PNU: ParcelFeature(
                pnu=SCHOOL_PNU,
                address="",
                jibun="227-1학",
                ring=square_ring(parcel_center, half),
                area_m2=(2 * half) ** 2,
            )
        },
        half=30.0,
    )
    return AmenityCollector(kakao=kakao, tago=FakeTago([]), vworld=vworld), vworld


@pytest.mark.asyncio
async def test_school_is_measured_to_its_address_parcel_first() -> None:
    site = square_ring(CENTER, 20.0)
    # 학교 좌표(400m 북쪽)를 품는, 좌표 필지보다 사업지 쪽으로 큰 주소 필지.
    collector, vworld = school_collector(offset_coordinates(CENTER, 400, 0), 80.0)

    result = await collector.collect([site], CENTER)

    facility = result["school_elementary"].facilities[0]
    # 400 − 사업지 반폭 20 − 주소 필지 반폭 80 = 300 (좌표 필지였다면 350).
    assert facility.distance_m == pytest.approx(300, abs=3)
    assert result["school_elementary"].distances_m[0] == pytest.approx(facility.distance_m)
    assert facility.measurement_tier == "site_boundary"
    assert "학교 지번주소 필지" in facility.front_door_notice
    assert "227-1" in facility.front_door_notice
    assert vworld.pnu_calls == [SCHOOL_PNU]
    # 주소 필지를 찾았으면 좌표 필지는 조회하지 않는다.
    school_point = offset_coordinates(CENTER, 400, 0)
    assert all(abs(lat - school_point.lat) > 1e-7 for lat, _ in vworld.calls)


@pytest.mark.asyncio
async def test_address_parcel_far_from_school_falls_back_to_coordinate_parcel() -> None:
    site = square_ring(CENTER, 20.0)
    # 주소가 틀려 학교 좌표에서 1km 떨어진 필지가 나오면 쓰지 않는다.
    collector, _ = school_collector(offset_coordinates(CENTER, 400, 1000), 80.0)

    result = await collector.collect([site], CENTER)

    facility = result["school_elementary"].facilities[0]
    assert facility.distance_m == pytest.approx(350, abs=3)
    assert "학교 지번주소 필지" not in facility.front_door_notice
    assert "100-1" in facility.front_door_notice


def test_candidate_pnus_prefer_the_legal_dong_named_in_the_address() -> None:
    # 카카오는 행정동(농소동)으로 찾은 지번을 여러 법정동으로 돌려준다.
    documents = [
        address_doc("5218012400", "용계동", "14", "1"),
        address_doc("5218010600", "농소동", "14", "1"),
        address_doc("5218012300", "흑암동", "14", "1"),
    ]

    pnus = _candidate_pnus("전북특별자치도 정읍시 농소동 14-1", documents, None)

    assert pnus[0] == "5218010600100140001"
    assert len(pnus) == 3


@pytest.mark.asyncio
async def test_resolver_caches_found_parcels_but_retries_empty_lookups() -> None:
    near = CENTER
    feature = ParcelFeature(
        pnu=SCHOOL_PNU, address="", jibun="227-1학", ring=square_ring(near, 50), area_m2=1e4
    )
    responses: list[ParcelFeature | None] = [None, feature]

    async def fetch(pnu: str) -> ParcelFeature | None:
        return responses.pop(0)

    kakao = AddressKakao(documents={"학교 주소": [address_doc("5211112300", "서완산동1가", "227", "1")]})
    resolver = AddressParcelResolver(kakao, fetch)

    # 지적도가 비어 오면(장애일 수 있다) 기억하지 않고 다음에 다시 찾는다.
    assert await resolver.parcel_for("학교 주소", near) is None
    assert await resolver.parcel_for("학교 주소", near) is feature
    assert await resolver.parcel_for("학교 주소", near) is feature
    assert kakao.address_calls == ["학교 주소", "학교 주소"]


@pytest.mark.asyncio
async def test_resolver_rejects_road_parcels() -> None:
    road = ParcelFeature(
        pnu=SCHOOL_PNU, address="", jibun="501-2 도", ring=square_ring(CENTER, 50), area_m2=1e4
    )

    async def fetch(pnu: str) -> ParcelFeature | None:
        return road

    kakao = AddressKakao(documents={"학교 주소": [address_doc("5211112300", "서완산동1가", "227", "1")]})
    resolver = AddressParcelResolver(kakao, fetch)

    assert await resolver.parcel_for("학교 주소", CENTER) is None
