"""환승센터 표준데이터(15034541) 어댑터와 2차 환승시설 시설군 배선."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.models import Coordinates
from app.screening.amenities import (
    TRANSFER_STANDARD_SOURCE,
    TRANSFER_SUBSTITUTED_NOTE,
    AmenityCollector,
)
from app.services.geo import offset_coordinates
from app.services.transfer_center import PublicDataAPIError, TransferCenterClient, _center
from tests.test_screening_amenities import CENTER, FakeKakao, FakeTago, place


def row(name: str, lat: float, lng: float, oper: str = "Y") -> dict[str, Any]:
    return {
        "trnsitlcCnterNm": name, "rdnmadr": "전북 전주시 1", "lnmadr": "",
        "latitude": str(lat), "longitude": str(lng), "trnsitlcFclty": "버스+도시철도",
        "operYn": oper,
    }


def test_row_parsing_and_coordinate_sanity() -> None:
    assert _center(row("전주역 환승센터", 35.85, 127.16)).name == "전주역 환승센터"
    assert _center(row("좌표없음", "", "")) is None if False else _center({"trnsitlcCnterNm": "x", "latitude": "", "longitude": ""}) is None
    assert _center(row("해외좌표", 10.0, 10.0)) is None


@pytest.mark.asyncio
async def test_client_pages_and_filters_by_radius() -> None:
    near = offset_coordinates(CENTER, 200, 0)
    far = offset_coordinates(CENTER, 5000, 0)
    body = {"response": {"header": {"resultCode": "00"}, "body": {
        "items": [row("가까운센터", near.lat, near.lng), row("먼센터", far.lat, far.lng), row("폐쇄센터", near.lat, near.lng, "N")],
        "totalCount": 3}}}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=body))
    client = TransferCenterClient("key", transport=transport)
    found = await client.centers_around(CENTER, 1000)
    assert [c.name for c in found] == ["가까운센터"]


@pytest.mark.asyncio
async def test_unregistered_key_raises() -> None:
    body = {"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"returnReasonCode": "30", "returnAuthMsg": "등록되지 않은 서비스키"}}}
    transport = httpx.MockTransport(lambda req: httpx.Response(403, json=body))
    with pytest.raises(PublicDataAPIError):
        await TransferCenterClient("key", transport=transport).all_centers()


class FakeTransfer:
    enabled = True

    def __init__(self, centers=None, fail=False):
        self.centers = centers or []
        self.fail = fail

    async def centers_around(self, center, radius_m):
        if self.fail:
            raise PublicDataAPIError("403")
        return list(self.centers)


@pytest.mark.asyncio
async def test_transfer_group_uses_standard_data_when_available() -> None:
    from app.services.transfer_center import TransferCenter

    center = TransferCenter("전주역 환승센터", "주소", offset_coordinates(CENTER, 300, 0), "버스", True)
    collector = AmenityCollector(kakao=FakeKakao(), tago=FakeTago([]), transfer_client=FakeTransfer([center]))
    result = await collector.collect([], CENTER)
    group = result["transfer"]
    assert group.state == "connected"
    assert group.actual_source == TRANSFER_STANDARD_SOURCE
    assert group.facilities[0].name == "전주역 환승센터"


@pytest.mark.asyncio
async def test_transfer_group_falls_back_to_kakao_when_unapproved() -> None:
    kakao = FakeKakao(keywords={
        "환승센터": [
            place("전주 환승센터", 400, "교통,수송 > 교통시설"),
            place("환승센터약국", 100, "의료,건강 > 약국"),
            place("전주 환승센터 2-1출입구", 380, "교통,수송 > 입출구"),
        ],
        "환승정류장": [place("덕진 환승정류장", 700, "교통,수송 > 버스정류장")],
    })
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]), transfer_client=FakeTransfer(fail=True))
    result = await collector.collect([], CENTER)
    group = result["transfer"]
    assert group.state == "substituted"
    assert group.note == TRANSFER_SUBSTITUTED_NOTE
    assert [f.name for f in group.facilities] == ["전주 환승센터", "덕진 환승정류장"]


@pytest.mark.asyncio
async def test_transfer_group_is_missing_without_any_source() -> None:
    from tests.test_screening_amenities import DisabledKakao

    collector = AmenityCollector(kakao=DisabledKakao(), tago=FakeTago([]))
    result = await collector.collect([], CENTER)
    assert result["transfer"].state == "missing"
