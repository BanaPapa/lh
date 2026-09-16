"""서울 열린데이터광장 버스정류소 어댑터 — TAGO 미제공(서울) 보완."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.models import Coordinates
from app.screening.amenities import SEOUL_BUS_SOURCE, TAGO_SOURCE, AmenityCollector
from app.services.geo import offset_coordinates
from app.services.seoul_bus import PublicDataAPIError, SeoulBusStopClient, _stop, in_seoul
from tests.test_screening_amenities import FakeKakao, FakeTago

JAMSIL = Coordinates(lat=37.5138, lng=127.1020)


def row(no: str, name: str, lat: float, lng: float, typ: str = "일반차로") -> dict[str, Any]:
    return {"STOPS_NO": no, "STOPS_NM": name, "XCRD": str(lng), "YCRD": str(lat), "NODE_ID": no, "STOPS_TYPE": typ}


def test_row_parsing_drops_ferry_piers_and_out_of_seoul() -> None:
    assert _stop(row("1", "잠실역", 37.5138, 127.1020)).name == "잠실역"
    assert _stop(row("2", "한강버스.잠실선착장", 37.5189, 127.0848, "한강선착장")) is None
    assert _stop(row("3", "전주", 35.85, 127.13)) is None
    assert in_seoul(JAMSIL) and not in_seoul(Coordinates(lat=35.85, lng=127.13))


@pytest.mark.asyncio
async def test_client_pages_and_filters_by_radius() -> None:
    near = offset_coordinates(JAMSIL, 120, 0)
    far = offset_coordinates(JAMSIL, 3000, 0)
    body = {"busStopLocationXyInfo": {"list_total_count": 2, "RESULT": {"CODE": "INFO-000"},
            "row": [row("1", "가까운정류장", near.lat, near.lng), row("2", "먼정류장", far.lat, far.lng)]}}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=body)

    client = SeoulBusStopClient("KEY", transport=httpx.MockTransport(handler))
    found = await client.stops_around(JAMSIL, 500)
    assert [s.name for s in found] == ["가까운정류장"]
    assert "/KEY/json/busStopLocationXyInfo/1/1000/" in seen[0]
    # 서울 밖 사업지는 묻지 않는다.
    assert await client.stops_around(Coordinates(lat=35.85, lng=127.13), 500) == []


@pytest.mark.asyncio
async def test_invalid_key_raises() -> None:
    body = {"RESULT": {"CODE": "INFO-100", "MESSAGE": "인증키가 유효하지 않습니다."}}
    client = SeoulBusStopClient("BAD", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    with pytest.raises(PublicDataAPIError):
        await client.all_stops()


class FakeSeoulBus:
    enabled = True

    def __init__(self, stops):
        self.stops = stops

    async def stops_around(self, center, radius_m):
        from app.services.seoul_bus import SeoulBusStop

        return [SeoulBusStop("1", name, coords, "일반차로") for name, coords in self.stops]


@pytest.mark.asyncio
async def test_bus_stops_fall_back_to_seoul_when_tago_is_empty() -> None:
    stop = offset_coordinates(JAMSIL, 150, 0)
    collector = AmenityCollector(
        kakao=FakeKakao(), tago=FakeTago([]), seoul_bus=FakeSeoulBus([("잠실역.롯데월드", stop)])
    )
    result = await collector.collect([], JAMSIL)
    group = result["bus_stop"]
    assert group.actual_source == SEOUL_BUS_SOURCE
    assert [f.name for f in group.facilities] == ["잠실역.롯데월드"]
    assert group.distances_m[0] == pytest.approx(150, abs=3)


@pytest.mark.asyncio
async def test_tago_rows_take_precedence_over_seoul() -> None:
    stop = offset_coordinates(JAMSIL, 150, 0)
    tago = FakeTago([{"nodenm": "TAGO정류장", "gpslati": stop.lat, "gpslong": stop.lng, "nodeid": "t1"}])
    collector = AmenityCollector(kakao=FakeKakao(), tago=tago, seoul_bus=FakeSeoulBus([("서울정류장", stop)]))
    result = await collector.collect([], JAMSIL)
    assert result["bus_stop"].actual_source == TAGO_SOURCE
