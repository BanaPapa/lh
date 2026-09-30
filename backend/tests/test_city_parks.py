"""도시공원 표준데이터 — 공원 1순위 원천, 카카오 대체 경고, 관리자 보강 스위치, 지번 필지."""

from __future__ import annotations

import httpx
import pytest

from app.models import Coordinates
from app.rules_config import OPTION_BY_KEY, OPTION_DEFS
from app.screening import amenities
from app.screening import parks as park_source
from app.screening.amenities import AmenityCollector
from app.services.city_parks import CityParkClient, ParkRecord
from app.services.geo import offset_coordinates
from app.services.vworld import ParcelFeature
from tests.test_address_parcels import AddressKakao, PnuVWorld, address_doc
from tests.test_screening_amenities import CENTER, FakeKakao, FakeTago, place, square_ring

PARK_PNU = "5211310700107560004"


def _row(name, kind, point, manage_no=None):
    return {"manageNo": manage_no or name, "parkNm": name, "parkSe": kind,
            "lnmadr": f"{name} 주소", "rdnmadr": "", "parkAr": "2033.7",
            "latitude": str(point.lat), "longitude": str(point.lng)}


@pytest.fixture(autouse=True)
def standard(monkeypatch):
    """기본은 LH 기준(보강 끔). 실제 rule_overrides.json 값에 흔들리지 않게 고정한다."""

    monkeypatch.setattr(amenities, "option_enabled", lambda key: OPTION_BY_KEY[key].default)


@pytest.fixture
def supplement(monkeypatch):
    def fake(key: str) -> bool:
        if key == park_source.PARK_SUPPLEMENT_OPTION:
            return True
        return OPTION_BY_KEY[key].default

    monkeypatch.setattr(amenities, "option_enabled", fake)


class _FakeParks:
    enabled = True

    def __init__(self, records=None, fail=False) -> None:
        self.records = records or []
        self.fail = fail

    async def parks_around(self, center, radius_m):
        if self.fail:
            raise RuntimeError("표준데이터 503")
        return self.records


def _record(name, offset_m, kind="어린이공원", address=""):
    return ParkRecord(name, name, kind, address or f"{name} 주소",
                      offset_coordinates(CENTER, offset_m, 0), 2000.0)


KAKAO_PARKS = {
    "공원": [
        place("금암어린이공원", 150, "여행 > 공원 > 도시근린공원 > 어린이공원"),
        place("덕진공원", 900, "여행 > 공원 > 도시근린공원"),
    ]
}


def _collector(parks, kakao=None, **kwargs):
    return AmenityCollector(
        kakao=kakao or FakeKakao(keywords=KAKAO_PARKS), tago=FakeTago([]),
        park_client=parks, **kwargs,
    )


@pytest.mark.asyncio
async def test_client_filters_by_radius_and_caches() -> None:
    rows = [
        _row("금암공원", "어린이공원", offset_coordinates(CENTER, 140, 0)),
        _row("강당재", "소공원", offset_coordinates(CENTER, 300, 0)),
        _row("먼공원", "근린공원", offset_coordinates(CENTER, 9000, 0)),
        {"parkNm": "좌표없음", "latitude": "", "longitude": ""},
    ]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"},
                              "body": {"items": rows, "totalCount": len(rows)}}})

    client = CityParkClient("key", transport=httpx.MockTransport(handler))
    near = await client.parks_around(CENTER, 3000)
    again = await client.parks_around(CENTER, 3000)

    assert [p.name for p in near] == ["금암공원", "강당재"]
    assert near[1].kind == "소공원"
    assert near[0].area_m2 == pytest.approx(2033.7)
    assert again == near
    assert len(calls) == 1  # 전량은 한 번만 받고 캐시한다


@pytest.mark.asyncio
async def test_client_raises_on_unregistered_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="SERVICE_KEY_IS_NOT_REGISTERED_ERROR")

    client = CityParkClient("key", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await client.parks_around(CENTER, 3000)


@pytest.mark.asyncio
async def test_standard_data_is_the_only_park_source_by_default() -> None:
    # 소공원(카카오 공원 분류 밖)도 LH 처럼 센다. 카카오 「금암어린이공원」은 쓰지 않는다.
    kakao = FakeKakao(keywords=KAKAO_PARKS)
    parks = _FakeParks([_record("금암공원", 140), _record("강당재", 300, kind="소공원")])
    result = await _collector(parks, kakao).collect([], CENTER)

    group = result["park"]
    assert [f.name for f in group.facilities] == ["금암공원", "강당재"]
    assert group.state == "connected"
    assert group.note == park_source.PARK_STANDARD_NOTE
    assert group.actual_source == park_source.PARK_STANDARD_SOURCE
    assert group.source_alert == ""
    assert "공원" not in kakao.calls  # 카카오 공원 검색은 부르지 않는다


@pytest.mark.asyncio
async def test_standard_data_outage_falls_back_to_kakao_with_alert() -> None:
    result = await _collector(_FakeParks(fail=True)).collect([], CENTER)

    group = result["park"]
    assert [f.name for f in group.facilities] == ["금암어린이공원", "덕진공원"]
    assert group.state == "substituted"
    assert group.source_alert == park_source.PARK_STANDARD_ALERT


@pytest.mark.asyncio
async def test_supplement_switch_adds_only_kakao_parks_missing_from_standard(supplement) -> None:
    parks = _FakeParks([_record("금암공원", 140)])
    result = await _collector(parks).collect([], CENTER)

    group = result["park"]
    # 「금암어린이공원」은 이름 열쇠(금암)가 같아 겹친다. 덕진공원만 더한다.
    assert [f.name for f in group.facilities] == ["금암공원", "덕진공원"]
    assert group.state == "connected"
    assert group.note == park_source.PARK_SUPPLEMENT_NOTE
    assert group.source_alert == ""


@pytest.mark.asyncio
async def test_supplement_failure_keeps_standard_and_alerts(supplement) -> None:
    kakao = FakeKakao(keywords=KAKAO_PARKS, fail={"공원"})
    result = await _collector(_FakeParks([_record("금암공원", 140)]), kakao).collect([], CENTER)

    group = result["park"]
    assert [f.name for f in group.facilities] == ["금암공원"]
    assert group.source_alert == park_source.PARK_SUPPLEMENT_ALERT


@pytest.mark.asyncio
async def test_park_is_measured_to_its_address_parcel() -> None:
    site = square_ring(CENTER, 20.0)
    # 공원 대표 좌표(400m 북쪽)는 작은 좌표 필지(반폭 30m)에, 지번 필지는 반폭 80m 다.
    kakao = AddressKakao(
        documents={"금암공원 주소": [address_doc("5211310700", "금암동", "756", "4")]},
    )
    vworld = PnuVWorld(
        by_pnu={
            PARK_PNU: ParcelFeature(
                pnu=PARK_PNU, address="", jibun="756-4공",
                ring=square_ring(offset_coordinates(CENTER, 400, 0), 80.0), area_m2=25600.0,
            )
        },
        half=30.0,
    )
    collector = _collector(_FakeParks([_record("금암공원", 400)]), kakao, vworld=vworld)

    result = await collector.collect([site], CENTER)

    facility = result["park"].facilities[0]
    assert facility.distance_m == pytest.approx(300, abs=3)
    assert facility.measurement_tier == "site_boundary"
    assert "공원 지번주소 필지" in facility.front_door_notice
    assert vworld.pnu_calls == [PARK_PNU]


def test_name_key_matches_lh_and_kakao_names() -> None:
    assert park_source.name_key("금암어린이공원") == park_source.name_key("금암공원")
    assert park_source.name_key("효자제7호공원") == "효자제7호"
    assert park_source.name_key("불무 1") == "불무1"
    assert not park_source.is_duplicate(
        "덕진공원", Coordinates(lat=CENTER.lat + 0.01, lng=CENTER.lng),
        [("금암공원", CENTER)],
    )


def test_supplement_option_defaults_to_lh_standard() -> None:
    option = next(o for o in OPTION_DEFS if o.key == park_source.PARK_SUPPLEMENT_OPTION)
    assert option.default is False


def test_every_option_is_off_by_default() -> None:
    # 전부 끈 상태가 LH 내부망 앱 기준이다(사용자 결정 2026-09-30).
    assert all(o.default is False for o in OPTION_DEFS)
