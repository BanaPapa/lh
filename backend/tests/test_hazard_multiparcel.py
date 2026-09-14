"""복수 필지 점유 시설의 판정 거리·근거 회귀 테스트 (정민재 이사 2026-08-28 확정).

- 판정 거리는 점유 필지 중 최소값(최근접 필지)인지
- 판정에 쓴 필지가 결과에 남는지(judgment_parcel_pnu · is_judgment_parcel)
- 점유 필지 전체가 실리는지(「외 N필지」로 뭉뚱그리지 않음)
- 단일 필지 시설의 기존 동작이 그대로인지
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.hazard_review.models import (
    HazardFacility,
    HazardOccupiedParcel,
    HazardParcel,
    HazardReviewRequest,
    HazardSite,
)
from app.hazard_review.service import HazardReviewService
from app.models import Coordinates


class _FakeKakao:
    enabled = False


def _service() -> HazardReviewService:
    return HazardReviewService(kakao=_FakeKakao(), demo_mode=False)  # type: ignore[arg-type]


def _square(lat0: float, lng0: float, size: float = 0.0002) -> list[Coordinates]:
    return [
        Coordinates(lat=lat0, lng=lng0),
        Coordinates(lat=lat0, lng=lng0 + size),
        Coordinates(lat=lat0 + size, lng=lng0 + size),
        Coordinates(lat=lat0 + size, lng=lng0),
        Coordinates(lat=lat0, lng=lng0),
    ]


SITE_LAT, SITE_LNG = 35.5000, 127.0000


def _request() -> HazardReviewRequest:
    return HazardReviewRequest(
        site=HazardSite(
            name="검증 사업지",
            coordinates=Coordinates(lat=SITE_LAT + 0.0001, lng=SITE_LNG + 0.0001),
            housing_type="house",  # type: ignore[arg-type]
            application_type="multi_child",  # type: ignore[arg-type]
            parcels=[
                HazardParcel(
                    parcel_id="site-1",
                    geometry=_square(SITE_LAT, SITE_LNG),
                    geometry_source="parcel_polygon",  # type: ignore[arg-type]
                )
            ],
        ),
        rule_pack_id="lh-rulebook-v1.4",
    )


def _facility(occupied: list[HazardOccupiedParcel]) -> HazardFacility:
    return HazardFacility(
        facility_id="f1",
        facility_type="factory",
        facility_type_label="공장",
        name="복수필지 공장",
        coordinates=Coordinates(lat=SITE_LAT + 0.01, lng=SITE_LNG),
        distance_m=999.0,
        provider="test",
        source_label="테스트",
        source_as_of=datetime.now(timezone.utc),
        geometry_note="점 좌표",
        classification_note="테스트",
        occupied_parcels=occupied,
    )


@pytest.mark.asyncio
async def test_multiparcel_uses_nearest_distance():
    service = _service()
    request = _request()
    # 필지 A: 사업지 바로 북쪽(가까움). 필지 B: 훨씬 북쪽(멈).
    near = HazardOccupiedParcel(pnu="A" * 19, jibun="100-1", geometry=_square(35.5003, 127.0000))
    far = HazardOccupiedParcel(pnu="B" * 19, jibun="100-2", geometry=_square(35.5030, 127.0000))
    facility = _facility([near, far])

    await service._attach_facility_boundaries(request, [facility])

    # 판정거리는 최근접(필지 A)까지의 경계거리 = 두 필지 거리 중 최소.
    dists = {p.pnu: p.distance_m for p in facility.occupied_parcels}
    assert dists["A" * 19] < dists["B" * 19]
    assert facility.distance_m == pytest.approx(dists["A" * 19])
    # 판정에 쓴 필지가 결과에 남는다.
    assert facility.judgment_parcel_pnu == "A" * 19
    assert facility.parcel_pnu == "A" * 19
    judged = [p for p in facility.occupied_parcels if p.is_judgment_parcel]
    assert len(judged) == 1 and judged[0].pnu == "A" * 19


@pytest.mark.asyncio
async def test_multiparcel_carries_all_parcels():
    service = _service()
    request = _request()
    parcels = [
        HazardOccupiedParcel(pnu="A" * 19, jibun="100-1", geometry=_square(35.5003, 127.0000)),
        HazardOccupiedParcel(pnu="B" * 19, jibun="100-2", geometry=_square(35.5030, 127.0000)),
        HazardOccupiedParcel(pnu="C" * 19, jibun="100-3", geometry=_square(35.5050, 127.0000)),
    ]
    facility = _facility(parcels)

    await service._attach_facility_boundaries(request, [facility])

    # 점유 필지 전체가 실린다(「외 N필지」 아님). 각 필지에 PNU·지번·폴리곤·거리.
    assert len(facility.occupied_parcels) == 3
    pnus = {p.pnu for p in facility.occupied_parcels}
    assert pnus == {"A" * 19, "B" * 19, "C" * 19}
    for p in facility.occupied_parcels:
        assert p.distance_m is not None
        assert len(p.geometry) >= 4
        assert p.jibun


@pytest.mark.asyncio
async def test_single_parcel_still_populates_judgment():
    service = _service()
    request = _request()
    only = HazardOccupiedParcel(pnu="A" * 19, jibun="100-1", geometry=_square(35.5003, 127.0000))
    facility = _facility([only])

    await service._attach_facility_boundaries(request, [facility])

    assert facility.geometry_type == "polygon"
    assert len(facility.occupied_parcels) == 1
    assert facility.occupied_parcels[0].is_judgment_parcel is True
    assert facility.judgment_parcel_pnu == "A" * 19


@pytest.mark.asyncio
async def test_no_parcels_and_no_sources_stays_point():
    # 점유 필지도 없고 조회 원천도 없으면 점 좌표로 남는다(없는 걸 만들지 않는다).
    service = _service()
    request = _request()
    facility = _facility([])
    facility.distance_m = 42.0

    await service._attach_facility_boundaries(request, [facility])

    assert facility.geometry_type == "point"
    assert facility.occupied_parcels == []
    assert facility.distance_m == 42.0
