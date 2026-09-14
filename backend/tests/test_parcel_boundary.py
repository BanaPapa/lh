from __future__ import annotations

import asyncio

import httpx
import pytest

from app.models import Coordinates
from app.services.geo import (
    buffer_ring,
    buffer_rings,
    distance_point_to_polygons_m,
    max_extent_multi,
    distance_point_to_polygon_m,
    distance_polygon_to_polygon_m,
    offset_coordinates,
    polygon_area_m2,
    tm_epsg_for,
)
from app.services.pnu import build_pnu, pnu_from_address_document
from app.services.vworld import VWorldAPIError, VWorldClient


CENTER = Coordinates(lat=37.5, lng=127.1)


def square(center: Coordinates, half_size_m: float) -> list[Coordinates]:
    """중심에서 한 변이 2*half_size_m 인 정사각형 링을 만든다."""

    return [
        offset_coordinates(center, -half_size_m, -half_size_m),
        offset_coordinates(center, -half_size_m, half_size_m),
        offset_coordinates(center, half_size_m, half_size_m),
        offset_coordinates(center, half_size_m, -half_size_m),
        offset_coordinates(center, -half_size_m, -half_size_m),
    ]


class TestPnuAssembly:
    def test_general_land_pads_bonbun_and_bubun(self) -> None:
        assert build_pnu("4145010900", "N", "1034", "") == "4145010900110340000"

    def test_mountain_land_uses_flag_two(self) -> None:
        # 4145010900 + 산(2) + 본번 0012 + 부번 0003
        assert build_pnu("4145010900", "Y", "12", "3") == "4145010900200120003"

    def test_reads_a_kakao_address_document(self) -> None:
        document = {
            "address": {
                "b_code": "4145010800",
                "mountain_yn": "N",
                "main_address_no": "318",
                "sub_address_no": "29",
            }
        }

        assert pnu_from_address_document(document) == "4145010800103180029"

    def test_missing_legal_code_returns_none(self) -> None:
        # 카카오 역지오코딩 응답에는 b_code 가 없다.
        document = {
            "address": {
                "mountain_yn": "N",
                "main_address_no": "858",
                "sub_address_no": "",
            }
        }

        assert pnu_from_address_document(document) is None


class TestProjection:
    def test_picks_the_tm_origin_from_longitude(self) -> None:
        assert tm_epsg_for(125.5) == 5185  # 서부
        assert tm_epsg_for(127.1) == 5186  # 중부
        assert tm_epsg_for(129.0) == 5187  # 동부
        assert tm_epsg_for(131.0) == 5188  # 동해

    def test_area_of_a_100m_square_is_about_10000_m2(self) -> None:
        area = polygon_area_m2(square(CENTER, 50))

        assert area == pytest.approx(10_000, rel=0.01)


class TestBoundaryDistance:
    def test_polygon_distance_measures_edge_to_edge_not_center_to_center(self) -> None:
        a = square(CENTER, 50)
        b = square(offset_coordinates(CENTER, 0, 150), 50)

        result = distance_polygon_to_polygon_m(a, b)

        # 중심 간격 150m, 각 변의 절반 50m씩 -> 경계 간격 50m
        assert result.distance_m == pytest.approx(50, abs=1.0)
        assert result.overlaps is False

    def test_touching_polygons_report_zero(self) -> None:
        a = square(CENTER, 50)
        b = square(offset_coordinates(CENTER, 0, 60), 50)

        result = distance_polygon_to_polygon_m(a, b)

        assert result.distance_m == pytest.approx(0, abs=0.5)
        assert result.overlaps is True

    def test_nearest_points_sit_on_the_facing_edges(self) -> None:
        a = square(CENTER, 50)
        b = square(offset_coordinates(CENTER, 0, 150), 50)

        result = distance_polygon_to_polygon_m(a, b)

        # 마주 보는 두 변 위의 점이므로 경도는 각각 중심 +50m, +100m 근처
        assert result.nearest_a.lng < result.nearest_b.lng
        assert result.nearest_a.lat == pytest.approx(CENTER.lat, abs=0.001)

    def test_point_inside_polygon_is_zero(self) -> None:
        assert distance_point_to_polygon_m(CENTER, square(CENTER, 50)) == 0.0

    def test_point_outside_polygon_measures_to_the_edge(self) -> None:
        point = offset_coordinates(CENTER, 0, 80)

        distance = distance_point_to_polygon_m(point, square(CENTER, 50))

        assert distance == pytest.approx(30, abs=1.0)

    def test_empty_ring_raises(self) -> None:
        with pytest.raises(ValueError):
            polygon_area_m2([])

    def test_buffer_ring_grows_the_polygon_by_the_given_distance(self) -> None:
        base = square(CENTER, 50)

        expanded = buffer_ring(base, 50)

        assert len(expanded) >= 4
        # 100m 사각형을 50m 확장하면 모서리가 둥근 200m 사방 도형이 된다.
        area = polygon_area_m2(expanded)
        assert area > polygon_area_m2(base)
        assert area == pytest.approx(200 * 200, rel=0.06)

    def test_buffer_ring_keeps_every_point_outside_the_original(self) -> None:
        base = square(CENTER, 50)

        for point in buffer_ring(base, 30):
            assert distance_point_to_polygon_m(point, base) == pytest.approx(
                30, abs=1.5
            )


def vworld_response(features: list[dict[str, object]]) -> dict[str, object]:
    return {
        "response": {
            "status": "OK",
            "result": {"featureCollection": {"features": features}},
        }
    }


PARCEL_FEATURE = {
    "geometry": {
        "type": "MultiPolygon",
        "coordinates": [
            [
                [
                    [127.1000, 37.5000],
                    [127.1005, 37.5000],
                    [127.1005, 37.5004],
                    [127.1000, 37.5004],
                    [127.1000, 37.5000],
                ]
            ]
        ],
    },
    "properties": {
        "pnu": "4145010900110340000",
        "addr": "경기도 하남시 망월동 1034",
        "jibun": "1034대",
    },
}


def client_with(handler) -> VWorldClient:
    transport = httpx.MockTransport(handler)
    return VWorldClient(api_key="test-key", transport=transport)


class TestVWorldClient:
    def test_parses_a_multipolygon_parcel(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["attrFilter"] == "pnu:=:4145010900110340000"
            assert request.url.params["data"] == "LP_PA_CBND_BUBUN"
            return httpx.Response(200, json=vworld_response([PARCEL_FEATURE]))

        parcel = asyncio.run(
            client_with(handler).parcel_by_pnu("4145010900110340000")
        )

        assert parcel is not None
        assert parcel.pnu == "4145010900110340000"
        assert parcel.address == "경기도 하남시 망월동 1034"
        assert len(parcel.ring) >= 4
        assert parcel.area_m2 > 0

    def test_missing_parcel_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=vworld_response([]))

        assert asyncio.run(client_with(handler).parcel_by_pnu("0" * 19)) is None

    def test_not_found_status_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"response": {"status": "NOT_FOUND"}},
            )

        assert asyncio.run(client_with(handler).parcel_by_pnu("0" * 19)) is None

    def test_api_error_status_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "response": {
                        "status": "ERROR",
                        "error": {"code": "INVALID_KEY", "text": "인증키 오류"},
                    }
                },
            )

        with pytest.raises(VWorldAPIError):
            asyncio.run(client_with(handler).parcel_by_pnu("0" * 19))

    def test_http_failure_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with pytest.raises(VWorldAPIError):
            asyncio.run(client_with(handler).parcel_by_pnu("0" * 19))

    def test_disabled_client_returns_none_without_calling(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=vworld_response([PARCEL_FEATURE]))

        client = VWorldClient(
            api_key="",
            transport=httpx.MockTransport(handler),
        )

        assert asyncio.run(client.parcel_by_pnu("0" * 19)) is None
        assert called is False


class TestMultipleParcels:
    """사업지는 합필 예정 필지처럼 여러 개일 수 있다."""

    def test_distance_uses_the_nearest_parcel(self) -> None:
        near = square(CENTER, 50)
        far = square(offset_coordinates(CENTER, 0, 500), 50)
        point = offset_coordinates(CENTER, 0, 80)

        assert distance_point_to_polygons_m(point, [far, near]) == pytest.approx(
            30, abs=1.0
        )

    def test_single_parcel_matches_the_single_polygon_helper(self) -> None:
        ring = square(CENTER, 50)
        point = offset_coordinates(CENTER, 0, 80)

        assert distance_point_to_polygons_m(
            point, [ring]
        ) == pytest.approx(distance_point_to_polygon_m(point, ring), abs=0.01)

    def test_touching_parcels_buffer_into_one_band(self) -> None:
        left = square(CENTER, 50)
        right = square(offset_coordinates(CENTER, 0, 100), 50)

        rings = buffer_rings([left, right], 30)

        # 두 필지가 맞닿아 있으므로 확장 결과도 하나로 합쳐진다.
        assert len(rings) == 1
        for point in rings[0]:
            assert distance_point_to_polygons_m(point, [left, right]) == pytest.approx(
                30, abs=2
            )

    def test_separate_parcels_buffer_into_separate_bands(self) -> None:
        near = square(CENTER, 50)
        far = square(offset_coordinates(CENTER, 0, 600), 50)

        rings = buffer_rings([near, far], 30)

        assert len(rings) == 2

    def test_extent_covers_the_farthest_parcel(self) -> None:
        near = square(CENTER, 50)
        far = square(offset_coordinates(CENTER, 0, 500), 50)

        extent = max_extent_multi(CENTER, [near, far])

        assert extent == pytest.approx(550, abs=15)

    def test_empty_input_is_safe(self) -> None:
        assert buffer_rings([], 30) == []
        assert max_extent_multi(CENTER, []) == 0.0


class TestBoxQuery:
    """지도에 지적도를 깔려면 영역 안의 필지를 한 번에 받아야 한다."""

    def test_requests_a_box_filter_and_parses_every_feature(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(request.url.params)
            return httpx.Response(
                200,
                json=vworld_response([PARCEL_FEATURE, PARCEL_FEATURE]),
            )

        parcels = asyncio.run(
            client_with(handler).parcels_in_box(37.50, 127.10, 37.51, 127.11)
        )

        assert captured["geomFilter"] == "BOX(127.1,37.5,127.11,37.51)"
        assert captured["crs"] == "EPSG:4326"
        assert len(parcels) == 2

    def test_limit_caps_the_result_and_the_page_size(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(request.url.params)
            return httpx.Response(
                200,
                json=vworld_response([PARCEL_FEATURE] * 5),
            )

        parcels = asyncio.run(
            client_with(handler).parcels_in_box(37.50, 127.10, 37.51, 127.11, limit=2)
        )

        assert captured["size"] == "2"
        assert len(parcels) == 2

    def test_empty_area_returns_an_empty_list(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"response": {"status": "NOT_FOUND"}})

        assert (
            asyncio.run(
                client_with(handler).parcels_in_box(37.50, 127.10, 37.51, 127.11)
            )
            == []
        )

    def test_one_broken_geometry_does_not_drop_the_rest(self) -> None:
        broken = {"geometry": {"type": "Point", "coordinates": [127.1, 37.5]},
                  "properties": {"pnu": "x"}}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=vworld_response([broken, PARCEL_FEATURE]),
            )

        parcels = asyncio.run(
            client_with(handler).parcels_in_box(37.50, 127.10, 37.51, 127.11)
        )

        assert len(parcels) == 1
        assert parcels[0].pnu == "4145010900110340000"

    def test_disabled_client_returns_empty_without_calling(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=vworld_response([PARCEL_FEATURE]))

        client = VWorldClient(api_key="", transport=httpx.MockTransport(handler))

        assert asyncio.run(client.parcels_in_box(37.5, 127.1, 37.51, 127.11)) == []
        assert called is False
