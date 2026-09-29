"""심평원 병원정보서비스 — 종합·상급종합 반경 조회와 2차 의료시설 원천 순서."""

from __future__ import annotations

import httpx
import pytest

from app.models import Coordinates
from app.services.hira_hospital import HiraHospitalClient
from app.services.ncmc_hospital import HospitalRecord

CENTER = Coordinates(lat=35.5695, lng=126.8535)


def _payload(items: list[dict]) -> dict:
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
            "body": {"items": {"item": items}, "totalCount": len(items)},
        }
    }


@pytest.mark.asyncio
async def test_client_reads_general_and_tertiary_hospitals() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        code = request.url.params["clCd"]
        assert request.url.params["radius"] == "3000"
        if code == "11":
            return httpx.Response(200, json=_payload([
                {"yadmNm": "정읍한국병원", "addr": "정읍시 서부산업도로 425-12",
                 "XPos": "126.8515587", "YPos": "35.5843414", "ykiho": "k1"},
            ]))
        return httpx.Response(200, json=_payload([]))

    client = HiraHospitalClient("key", transport=httpx.MockTransport(handler))
    records = await client.hospitals_around(CENTER, 3000)

    assert [(r.name, r.div_name) for r in records] == [("정읍한국병원", "종합병원")]


@pytest.mark.asyncio
async def test_client_raises_on_unregistered_key() -> None:
    client = HiraHospitalClient(
        "key", transport=httpx.MockTransport(lambda _r: httpx.Response(403, text="등록되지 않은 서비스키"))
    )
    with pytest.raises(Exception):
        await client.hospitals_around(CENTER, 3000)


class _FakeHira:
    enabled = True

    def __init__(self, records=None, fail=False) -> None:
        self.records = records or []
        self.fail = fail

    async def hospitals_around(self, center, radius_m):
        if self.fail:
            raise RuntimeError("심평원 503")
        return self.records


class _FakeNcmc:
    enabled = True

    async def hospitals_around(self, center, sido, radius_m):
        return [HospitalRecord("n1", "정읍아산병원", Coordinates(lat=35.5888, lng=126.8234), "종합병원")]

    async def general_hospitals_in_sido(self, sido):
        return []


def _collector(hira):
    from tests.test_screening_amenities import FakeKakao, FakeTago
    from app.screening.amenities import AmenityCollector

    kakao = FakeKakao()
    kakao.region_info = lambda lat, lng: _sido()  # type: ignore[attr-defined]
    return AmenityCollector(kakao=kakao, tago=FakeTago([]), hospital_client=_FakeNcmc(), hira_client=hira)


async def _sido():
    from types import SimpleNamespace

    return SimpleNamespace(legal_name="전북특별자치도 정읍시 연지동")


@pytest.mark.asyncio
async def test_hira_is_first_hospital_source() -> None:
    hira = _FakeHira([HospitalRecord("k1", "정읍한국병원", Coordinates(lat=35.5843, lng=126.8516), "종합병원")])
    result = await _collector(hira).collect([], CENTER)

    group = result["hospital"]
    assert [f.name for f in group.facilities] == ["정읍한국병원"]
    assert group.state == "connected"
    assert "건강보험심사평가원" in group.note
    assert group.source_alert == ""


@pytest.mark.asyncio
async def test_hira_outage_falls_back_with_alert() -> None:
    result = await _collector(_FakeHira(fail=True)).collect([], CENTER)

    group = result["hospital"]
    # 국립중앙의료원으로 대체해 병원은 잡되, 대체 사실을 경고로 올린다.
    assert [f.name for f in group.facilities] == ["정읍아산병원"]
    assert "심사평가원" in group.source_alert
