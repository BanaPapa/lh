"""생활안전지도 IF_0033 주유시설 어댑터 검증.

두 가지가 조용히 실패하는 종류라 회귀로 고정한다.
(1) http → https 302 리다이렉트를 안 따라가면 302 HTML 을 받아 0건이 된다.
(2) x·y 를 EPSG:3857 로 변환하지 않으면 좌표가 엉뚱해져 반경 조회가 0건이 된다.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.models import Coordinates
from app.services.safemap import (
    SafemapAPIError,
    SafemapFuelClient,
    _station,
)


# 실측 1건(인천 남동구 만수동 동남주유소).
SAMPLE_ROW = {
    "uni_cd": "A0008251",
    "os_nm": "동남주유소",
    "poll_div_co": "SOL",
    "gpoll_div_co": "",
    "lpg_yn": "N",
    "van_adr": "인천광역시 남동구 만수동 1099",
    "new_adr": "인천광역시 남동구 담방로 64 (만수동)",
    "tel": "032-468-8851",
    "x": "14108535",
    "y": "4501386.0",
    "gis_x_coor": "538892.0",
    "gis_y_coor": "288624.0",
}

INCHEON = Coordinates(lat=37.45, lng=126.74)


def body(rows: list[dict], total: int | None = None) -> dict:
    return {
        "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
        "body": {
            "totalCount": total if total is not None else len(rows),
            "items": {"item": rows},
        },
    }


def client_with(handler) -> SafemapFuelClient:
    return SafemapFuelClient(service_key="test-key", transport=httpx.MockTransport(handler))


class TestCoordinateConversion:
    def test_epsg_3857_is_converted_to_wgs84(self) -> None:
        station = _station(SAMPLE_ROW)

        assert station is not None
        # 역산: x=14108535 → 경도 126.74°, y=4501386 → 위도 37.45°.
        assert station.coordinates.lng == pytest.approx(126.74, abs=0.01)
        assert station.coordinates.lat == pytest.approx(37.45, abs=0.01)

    def test_out_of_korea_coordinate_is_dropped(self) -> None:
        # 좌표를 WGS84 로 착각해 그대로 넣으면 한반도 밖이 된다 → 버린다.
        assert _station({**SAMPLE_ROW, "x": "0", "y": "0"}) is None


class TestClassification:
    def test_oil_brand_marks_gas_station(self) -> None:
        station = _station(SAMPLE_ROW)
        assert station is not None
        assert station.is_gas_station is True
        assert station.is_lpg_station is False

    def test_gas_brand_marks_lpg_station(self) -> None:
        row = {**SAMPLE_ROW, "poll_div_co": "", "gpoll_div_co": "E1G", "lpg_yn": "Y"}
        station = _station(row)
        assert station is not None
        assert station.is_lpg_station is True
        assert station.is_gas_station is False

    def test_combined_station_is_both(self) -> None:
        row = {**SAMPLE_ROW, "gpoll_div_co": "SKG", "lpg_yn": "Y"}
        station = _station(row)
        assert station is not None
        assert station.is_gas_station is True
        assert station.is_lpg_station is True

    def test_blank_brand_and_lpg_is_unclassified_not_gas(self) -> None:
        # 상표·gpoll·lpg_yn 이 전부 공란인 행을 주유소로 강제 분류하면 임계거리
        # 이내일 때 매입제외로 승격된다. 미분류로 남겨야 한다(is_gas·is_lpg 둘 다 False).
        row = {**SAMPLE_ROW, "poll_div_co": "", "gpoll_div_co": "", "lpg_yn": ""}
        station = _station(row)
        assert station is not None
        assert station.is_gas_station is False
        assert station.is_lpg_station is False
        assert station.is_unclassified is True


class TestRedirect:
    def test_follows_http_to_https_redirect(self) -> None:
        """http 로 부르면 https 로 302 된다. 안 따라가면 조용히 0건이 된다."""

        seen_schemes: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_schemes.append(request.url.scheme)
            if request.url.scheme == "http":
                return httpx.Response(
                    302, headers={"Location": str(request.url.copy_with(scheme="https"))}
                )
            return httpx.Response(200, json=body([SAMPLE_ROW]))

        stations = asyncio.run(client_with(handler).all_stations())

        assert "http" in seen_schemes and "https" in seen_schemes
        assert len(stations) == 1
        assert stations[0].name == "동남주유소"


class TestPaging:
    def test_stops_when_total_reached(self) -> None:
        pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            pages.append(request.url.params["pageNo"])
            return httpx.Response(200, json=body([SAMPLE_ROW], total=1))

        stations = asyncio.run(client_with(handler).all_stations())
        assert stations and pages == ["1"]

    def test_around_filters_by_radius(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body([SAMPLE_ROW], total=1))

        near = asyncio.run(client_with(handler).stations_around(INCHEON, 5000))
        assert len(near) == 1

        far = Coordinates(lat=35.0, lng=129.0)
        # 캐시가 남으니 새 클라이언트로 조회한다.
        result = asyncio.run(client_with(handler).stations_around(far, 1000))
        assert result == []


class TestColdStart:
    """콜드스타트 조치(2026-08-28): 첫 요청이 전량 조회로 스레드를 막지 않는다.

    캐시가 안 데워졌으면 조용히 빈 결과를 내지 않고 콜드 신호(SafemapAPIError)를
    올려 판정이 '충돌 없음'으로 둔갑하지 않게 한다. 데워지면 거리로 걸러 돌려준다.
    """

    def test_cached_query_raises_when_cold_and_starts_background_warm(self) -> None:
        pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            pages.append(request.url.params["pageNo"])
            return httpx.Response(200, json=body([SAMPLE_ROW], total=1))

        async def scenario() -> None:
            client = client_with(handler)
            # 콜드: 즉시 신호를 올리고 요청 스레드를 막지 않는다.
            with pytest.raises(SafemapAPIError):
                await client.stations_around_cached(INCHEON, 5000)
            # 백그라운드 프리페치가 걸렸다. 데워질 때까지 기다린다.
            assert client._prefetch_task is not None
            await asyncio.gather(client._prefetch_task, return_exceptions=True)
            # 데워진 뒤에는 거리로 걸러 정상 반환한다.
            near = await client.stations_around_cached(INCHEON, 5000)
            assert len(near) == 1

        asyncio.run(scenario())
        assert pages  # 백그라운드에서 실제로 조회했다.

    def test_disabled_cached_query_returns_empty(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body([SAMPLE_ROW]))

        client = SafemapFuelClient(
            service_key="", transport=httpx.MockTransport(handler)
        )
        assert asyncio.run(client.stations_around_cached(INCHEON, 5000)) == []


class TestErrors:
    def test_error_result_code_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "header": {
                        "resultCode": "30",
                        "resultMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
                    },
                    "body": {},
                },
            )

        with pytest.raises(SafemapAPIError):
            asyncio.run(client_with(handler).all_stations())

    def test_disabled_client_returns_empty(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=body([SAMPLE_ROW]))

        client = SafemapFuelClient(service_key="", transport=httpx.MockTransport(handler))
        assert asyncio.run(client.all_stations()) == []
        assert called is False
