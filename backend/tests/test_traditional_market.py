"""전국전통시장표준데이터 — 상업시설(대규모점포 + 전통시장)의 전통시장 원천과 장애 경고."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.models import Coordinates
from app.services.geo import offset_coordinates
from app.services.traditional_market import (
    MARKET_OUTAGE_ALERT,
    MARKET_SOURCE,
    MarketRecord,
    TraditionalMarketClient,
    drop_duplicate_markets,
)
from tests import test_screening_amenities as amenity_tests
from tests.test_screening_amenities import CENTER, FakeKakao, FakeTago

# 클래스를 직접 import 하면 pytest 가 그 테스트를 이 파일에서도 다시 모은다.
Retail = amenity_tests.TestRetailLocalData


def _row(name, point, lnmadr="전북특별차치도 익산시 창인동1가 137-1", kind="상설장"):
    return {
        "mrktNm": name,
        "mrktType": kind,
        "rdnmadr": "",
        "lnmadr": lnmadr,
        "latitude": "" if point is None else str(point.lat),
        "longitude": "" if point is None else str(point.lng),
    }


@pytest.mark.asyncio
async def test_client_pages_filters_radius_and_caches() -> None:
    # 응답은 response 감싸개 없이 header/body 로 온다(실호출 2026-09-30).
    page1 = [_row(f"먼시장{i}", offset_coordinates(CENTER, 9000, 0)) for i in range(1000)]
    page2 = [
        _row("중앙시장", offset_coordinates(CENTER, 300, 0)),
        _row("좌표없는시장", None),
    ]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        rows = page1 if request.url.params["pageNo"] == "1" else page2
        return httpx.Response(200, json={"header": {"resultCode": "00"},
                                         "body": {"items": rows, "totalCount": 1002}})

    client = TraditionalMarketClient("key", transport=httpx.MockTransport(handler))
    near = await client.markets_around(CENTER, 3000)
    again = await client.markets_around(CENTER, 3000)

    assert [m.name for m in near] == ["중앙시장"]
    # 원천 오타 「전북특별차치도」는 주소 필지 조회가 빗나가지 않게 고쳐 둔다.
    assert near[0].address == "전북특별자치도 익산시 창인동1가 137-1"
    assert again == near
    assert len(calls) == 2  # 두 페이지를 한 번만 받고 캐시한다


@pytest.mark.asyncio
async def test_client_raises_on_unregistered_key() -> None:
    # 활용신청 전 403 은 0곳이 아니라 장애로 올린다.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="SERVICE_KEY_IS_NOT_REGISTERED_ERROR")

    client = TraditionalMarketClient("key", transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await client.markets_around(CENTER, 3000)


def test_drop_duplicate_markets_near_store_and_repeated_rows() -> None:
    here = offset_coordinates(CENTER, 500, 0)
    store = offset_coordinates(CENTER, 1000, 0)
    markets = [
        MarketRecord("중앙시장", "상설장", "", "", here),
        MarketRecord("중앙시장", "5일장", "", "", offset_coordinates(here, 10, 0)),  # 같은 시장
        MarketRecord("매일시장", "상설장", "", "", offset_coordinates(here, 20, 0)),   # 이름이 달라 남긴다
        MarketRecord("시장상가", "상설장", "", "", offset_coordinates(store, 30, 0)),  # 대규모점포와 같은 자리
    ]

    kept = drop_duplicate_markets(markets, [store])

    assert [m.name for m in kept] == ["중앙시장", "매일시장"]


class _FakeMarkets:
    enabled = True

    def __init__(self, records=None, fail=False) -> None:
        self.records = records or []
        self.fail = fail

    async def markets_around(self, center: Coordinates, radius_m: float):
        if self.fail:
            raise RuntimeError("전통시장 표준데이터 503")
        return self.records


def _collector(tmp_path, markets, stores):
    from app.screening.amenities import AmenityCollector

    store = Retail._store(tmp_path, stores)
    return AmenityCollector(
        kakao=FakeKakao(), tago=FakeTago([]), facility_store=store, market_client=markets
    )


def test_retail_counts_stores_plus_markets(tmp_path) -> None:
    from app.screening.amenities import RETAIL_CONNECTED_NOTE

    stores = [Retail._record("행복대형마트", 400)]
    markets = _FakeMarkets([
        MarketRecord("중앙시장", "상설장", "전북특별자치도 익산시 창인동1가 137-1", "",
                     offset_coordinates(CENTER, 200, 0)),
        # 대규모점포(400m)와 같은 자리 — 한 번만 센다.
        MarketRecord("대형마트안시장", "상설장", "", "", offset_coordinates(CENTER, 420, 0)),
    ])
    result = asyncio.run(_collector(tmp_path, markets, stores).collect([], CENTER, radius_m=3000))

    retail = result["retail"]
    assert retail.state == "connected"
    assert [f.name for f in retail.facilities] == ["중앙시장", "행복대형마트"]
    assert retail.facilities[0].source_label == MARKET_SOURCE
    assert len(retail.distances_m) == 2
    assert MARKET_SOURCE in retail.actual_source
    assert retail.note == RETAIL_CONNECTED_NOTE
    assert retail.source_alert == ""


def test_market_outage_falls_back_to_stores_with_alert(tmp_path) -> None:
    stores = [Retail._record("행복대형마트", 400)]
    collector = _collector(tmp_path, _FakeMarkets(fail=True), stores)
    result = asyncio.run(collector.collect([], CENTER, radius_m=3000))

    retail = result["retail"]
    assert [f.name for f in retail.facilities] == ["행복대형마트"]
    assert retail.state == "connected"
    assert "대규모점포만" in retail.note
    # 조용히 넘어가지 않는다 — 결과 상단 경고로 올리고 캐시하지 않는다.
    assert retail.source_alert == MARKET_OUTAGE_ALERT
    assert collector._cache == {}


def test_no_market_client_keeps_stores_only_note(tmp_path) -> None:
    stores = [Retail._record("행복대형마트", 400)]
    result = asyncio.run(_collector(tmp_path, None, stores).collect([], CENTER, radius_m=3000))

    retail = result["retail"]
    assert [f.name for f in retail.facilities] == ["행복대형마트"]
    assert "대규모점포만" in retail.note
    assert retail.source_alert == ""


@pytest.mark.asyncio
async def test_market_is_measured_to_its_address_parcel_but_store_is_not(tmp_path) -> None:
    # LH앱은 전통시장을 시장 지번주소 PNU 필지 경계로 잰다(location_basis=PNU).
    # 대규모점포는 기존대로 좌표 필지 — 주소 필지 조회는 시장에만 건다.
    from app.screening.amenities import AmenityCollector
    from app.services.vworld import ParcelFeature
    from tests.test_address_parcels import AddressKakao, PnuVWorld, address_doc
    from tests.test_screening_amenities import square_ring

    pnu = "5214010100101370001"
    address = "전북특별자치도 익산시 창인동1가 137-1"
    market_point = offset_coordinates(CENTER, 400, 0)
    kakao = AddressKakao(documents={address: [address_doc("5214010100", "창인동1가", "137", "1")]})
    vworld = PnuVWorld(
        by_pnu={pnu: ParcelFeature(pnu=pnu, address="", jibun="137-1대",
                                   ring=square_ring(market_point, 80.0), area_m2=25600)},
        half=30.0,
    )
    store = Retail._store(tmp_path, [Retail._record("행복대형마트", 1000)])
    collector = AmenityCollector(
        kakao=kakao, tago=FakeTago([]), facility_store=store, vworld=vworld,
        market_client=_FakeMarkets([MarketRecord("중앙시장", "상설장", address, "", market_point)]),
    )

    result = await collector.collect([square_ring(CENTER, 20.0)], CENTER)

    market, mart = result["retail"].facilities
    # 400 − 사업지 반폭 20 − 주소 필지 반폭 80 = 300 (좌표 필지였다면 350).
    assert market.name == "중앙시장"
    assert market.distance_m == pytest.approx(300, abs=3)
    assert "시장 지번주소 필지" in market.front_door_notice
    assert "시장 지번주소 필지" not in mart.front_door_notice
    assert kakao.address_calls == [address]
    assert vworld.pnu_calls == [pnu]
