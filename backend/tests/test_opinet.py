from __future__ import annotations

import asyncio

import httpx
import pytest

from app.models import Coordinates
from app.services.opinet import (
    OpinetAPIError,
    OpinetClient,
    from_katec,
    to_katec,
)


GANGNAM = Coordinates(lat=37.4979, lng=127.0276)


def around_response(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"RESULT": {"OIL": rows}}


def station_row(
    uni_id: str = "A0009905",
    name: str = "테스트 주유소",
    x: float = 313680.7987,
    y: float = 545015.7859,
) -> dict[str, object]:
    return {
        "UNI_ID": uni_id,
        "OS_NM": name,
        "POLL_DIV_CD": "HDO",
        "GIS_X_COOR": x,
        "GIS_Y_COOR": y,
        "DISTANCE": 120.5,
    }


def client_with(handler) -> OpinetClient:
    return OpinetClient(api_key="test-key", transport=httpx.MockTransport(handler))


class TestKatecProjection:
    """오피넷은 KATEC을 쓴다. 위경도를 그대로 넣으면 엉뚱한 곳이 나온다."""

    def test_round_trip_returns_the_original_point(self) -> None:
        x, y = to_katec(GANGNAM)
        back = from_katec(x, y)

        assert back.lat == pytest.approx(GANGNAM.lat, abs=1e-6)
        assert back.lng == pytest.approx(GANGNAM.lng, abs=1e-6)

    def test_katec_values_are_in_the_expected_range(self) -> None:
        x, y = to_katec(GANGNAM)

        # 서울 강남권은 KATEC 30만대 x, 54만대 y 근처다.
        assert 300_000 < x < 330_000
        assert 530_000 < y < 560_000


class TestAuthParameter:
    """오피넷 명세상 인증 파라미터는 `certkey` 다.

    과거 `code` 로 보내던 버그가 있었다. 파라미터 이름이 틀리면 오피넷은
    200 을 주면서 결과만 0건으로 비워 보낸다 — 예외가 안 나므로 조용히 실패한다.
    MockTransport 는 파라미터 이름을 검사하지 않아 이 버그를 못 잡으므로,
    보낸 파라미터 이름 자체를 회귀로 고정한다.
    """

    def test_around_sends_certkey_not_code(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=around_response([station_row()]))

        asyncio.run(client_with(handler).stations_around(GANGNAM, 1000))

        assert seen, "요청이 전송되지 않았습니다"
        for params in seen:
            assert params.get("certkey") == "test-key"
            assert "code" not in params

    def test_detail_sends_certkey_not_code(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=around_response([station_row()]))

        asyncio.run(client_with(handler).station_detail("A0009905"))

        assert seen, "요청이 전송되지 않았습니다"
        assert seen[0].get("certkey") == "test-key"
        assert "code" not in seen[0]


class TestStationsAround:
    def test_sends_katec_coordinates_and_parses_stations(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=around_response([station_row()]))

        stations = asyncio.run(client_with(handler).stations_around(GANGNAM, 1000))

        assert len(stations) == 1
        assert stations[0].station_id == "A0009905"
        assert stations[0].brand == "HD현대오일뱅크"
        # 위경도를 그대로 보내면 안 된다.
        assert float(seen[0]["x"]) > 100_000
        assert seen[0]["radius"] == "1000"

    def test_queries_both_petrol_and_lpg_products(self) -> None:
        products: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            products.append(request.url.params["prodcd"])
            return httpx.Response(200, json=around_response([]))

        asyncio.run(client_with(handler).stations_around(GANGNAM, 500))

        assert products == ["B027", "K015"]

    def test_a_station_in_both_products_is_marked_as_lpg_once(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=around_response([station_row()]))

        stations = asyncio.run(client_with(handler).stations_around(GANGNAM, 500))

        assert len(stations) == 1
        assert stations[0].lpg is True

    def test_radius_is_capped_at_the_api_limit(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.params["radius"])
            return httpx.Response(200, json=around_response([]))

        asyncio.run(client_with(handler).stations_around(GANGNAM, 99_000))

        assert seen[0] == "5000"

    def test_rows_without_coordinates_are_skipped(self) -> None:
        broken = {"UNI_ID": "X", "OS_NM": "좌표 없음"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=around_response([broken, station_row()]))

        stations = asyncio.run(client_with(handler).stations_around(GANGNAM, 500))

        assert [item.station_id for item in stations] == ["A0009905"]

    def test_disabled_client_returns_empty_without_calling(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=around_response([station_row()]))

        client = OpinetClient(api_key="", transport=httpx.MockTransport(handler))

        assert asyncio.run(client.stations_around(GANGNAM, 500)) == []
        assert called is False

    def test_http_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with pytest.raises(OpinetAPIError):
            asyncio.run(client_with(handler).stations_around(GANGNAM, 500))


class TestStationDetail:
    def test_detail_adds_addresses_and_lpg_flag(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            row = station_row() | {
                "VAN_ADR": "서울 서초구 서초동 1302",
                "NEW_ADR": "서울 서초구 사평대로 350",
                "LPG_YN": "Y",
            }
            return httpx.Response(200, json=around_response([row]))

        station = asyncio.run(client_with(handler).station_detail("A0009905"))

        assert station is not None
        assert station.address == "서울 서초구 서초동 1302"
        assert station.road_address == "서울 서초구 사평대로 350"
        assert station.lpg is True

    def test_missing_station_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=around_response([]))

        assert asyncio.run(client_with(handler).station_detail("nope")) is None

    def test_empty_id_does_not_call_the_api(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=around_response([station_row()]))

        assert asyncio.run(client_with(handler).station_detail("")) is None
        assert called is False

    def test_detail_is_memoized(self) -> None:
        # 같은 station_id 상세는 한 번만 원격을 탄다(판정마다 다시 때리지 않는다).
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            row = station_row() | {"VAN_ADR": "주소", "LPG_YN": "N"}
            return httpx.Response(200, json=around_response([row]))

        client = client_with(handler)

        async def run() -> None:
            await client.station_detail("A0009905")
            await client.station_detail("A0009905")

        asyncio.run(run())
        assert calls == 1


class TestWarmArea:
    """예열: 사업지들을 덮는 커버 지점을 한 번 조회해 스냅샷을 데운다.

    2026-08-30 양성 대조 사고의 재발 방지 테스트. 네트워크를 타지 않고(MockTransport)
    원격 호출 수가 사업지 수에 비례하지 않음을, 데운 뒤에는 원격을 아예 안 탐을 고정한다.
    """

    def _around_counter(self):
        state = {"around": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "aroundAll" in str(request.url):
                state["around"] += 1
            return httpx.Response(200, json=around_response([station_row()]))

        return handler, state

    def test_remote_calls_do_not_scale_with_center_count(self) -> None:
        handler, state = self._around_counter()
        client = client_with(handler)
        # GANGNAM 반경 300m 안에 뭉친 사업지 20곳. 커버 반경 5,000m 안에서 한 점으로
        # 묶여야 한다(케이스 20개여도 원격은 상품 2종 × 커버 1점 = 2회).
        centers = [
            Coordinates(lat=GANGNAM.lat + 0.001 * i, lng=GANGNAM.lng + 0.001 * i)
            for i in range(20)
        ]
        n = asyncio.run(client.warm_area(centers))
        assert n >= 1
        assert state["around"] == 2, f"원격 around 호출이 {state['around']}회 — 케이스 수에 비례하면 안 된다"

    def test_warmed_snapshot_serves_without_remote(self) -> None:
        handler, state = self._around_counter()
        client = client_with(handler)
        asyncio.run(client.warm_area([GANGNAM]))
        before = state["around"]

        # 기본 테스트 시설은 GANGNAM 에서 약 680m 떨어져 있어 반경 2,000m 로 조회한다.
        stations = asyncio.run(client.stations_around(GANGNAM, 2000))
        assert stations, "데운 스냅샷에서 시설이 나와야 한다"
        assert state["around"] == before, "데운 뒤 판정 조회는 원격을 타면 안 된다"

    def test_uncovered_center_falls_back_to_live_fetch(self) -> None:
        handler, state = self._around_counter()
        client = client_with(handler)
        asyncio.run(client.warm_area([GANGNAM]))
        before = state["around"]
        # 커버 지점에서 5,000m 넘게 떨어진 지점(부산권)은 스냅샷이 못 덮어 라이브로 떨어진다.
        busan = Coordinates(lat=35.1796, lng=129.0756)
        asyncio.run(client.stations_around(busan, 500))
        assert state["around"] > before, "커버되지 않은 지점은 라이브 조회로 떨어져야 한다"

    def test_warm_failure_does_not_corrupt_snapshot(self) -> None:
        # 예열 조회가 실패하면 예외를 올리고 스냅샷을 건드리지 않는다(실패를 캐시하지 않는다).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="throttled")

        client = client_with(handler)
        with pytest.raises(OpinetAPIError):
            asyncio.run(client.warm_area([GANGNAM]))
        assert client._snapshot == {}
        assert client._warmed is False
