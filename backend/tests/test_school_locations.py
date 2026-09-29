"""학교 위치 표준데이터 — 초·중·고 1순위 원천과 대체 경고."""

from __future__ import annotations

import httpx
import pytest

from app.models import Coordinates
from app.services.geo import offset_coordinates
from app.services.school_locations import SchoolLocationClient, SchoolRecord

# 가짜 레이어(FakeLayerFeed)가 쓰는 기준점과 같아야 반경 안에 든다.
from tests.test_screening_amenities import CENTER  # noqa: E402


def _row(name, kind, point, status="운영"):
    return {"schoolId": name, "schoolNm": name, "schoolSe": kind, "operSttus": status,
            "bnhhSe": "본교", "lnmadr": f"{name} 주소",
            "latitude": str(point.lat), "longitude": str(point.lng)}


@pytest.mark.asyncio
async def test_client_filters_by_radius_and_status() -> None:
    rows = [
        _row("전주서신중학교", "중학교", offset_coordinates(CENTER, 700, 0)),
        _row("전라중학교", "중학교", offset_coordinates(CENTER, 6000, 0)),  # 송천동으로 이전
        _row("폐교중학교", "중학교", offset_coordinates(CENTER, 300, 0), status="폐교"),
    ]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"},
                              "body": {"items": rows, "totalCount": len(rows)}}})

    client = SchoolLocationClient("key", transport=httpx.MockTransport(handler))
    near = await client.schools_around(CENTER, 3000)
    again = await client.schools_around(CENTER, 3000)

    assert [s.name for s in near] == ["전주서신중학교"]
    assert again == near
    assert len(calls) == 1  # 전량은 한 번만 받고 캐시한다


class _FakeSchools:
    enabled = True

    def __init__(self, records=None, fail=False) -> None:
        self.records = records or []
        self.fail = fail

    async def schools_around(self, center, radius_m):
        if self.fail:
            raise RuntimeError("표준데이터 503")
        return self.records


def _collector(schools, layer=None):
    from tests.test_screening_amenities import FakeKakao, FakeTago
    from app.screening.amenities import AmenityCollector

    return AmenityCollector(
        kakao=FakeKakao(), tago=FakeTago([]), school_client=schools, safemap_schools=layer
    )


@pytest.mark.asyncio
async def test_standard_data_is_first_school_source() -> None:
    from tests.test_screening_amenities import FakeLayerFeed

    # 레이어는 옛 부지(306m)를 주지만 표준데이터가 우선이라 쓰이지 않는다.
    layer = FakeLayerFeed([("전라중학교", 306, "중학교")])
    schools = _FakeSchools([SchoolRecord("s1", "전주서신중학교", "중학교", "",
                                         offset_coordinates(CENTER, 713, 0))])
    result = await _collector(schools, layer).collect([], CENTER)

    group = result["school_middle"]
    assert [f.name for f in group.facilities] == ["전주서신중학교"]
    assert group.state == "connected"
    assert group.source_alert == ""


@pytest.mark.asyncio
async def test_standard_data_outage_falls_back_with_alert() -> None:
    from tests.test_screening_amenities import FakeLayerFeed

    layer = FakeLayerFeed([("전라중학교", 306, "중학교")])
    result = await _collector(_FakeSchools(fail=True), layer).collect([], CENTER)

    group = result["school_middle"]
    assert [f.name for f in group.facilities] == ["전라중학교"]
    assert "학교위치표준데이터" in group.source_alert
