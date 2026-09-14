"""전국 화장시설 API 어댑터 검증.

두 가지가 조용히 실패하는 종류라 회귀로 고정한다.
(1) 허용 파라미터가 넷뿐이라 type 등을 더 붙이면 400 이 난다 — 파라미터를 고정.
(2) 좌표가 없어 지오코딩이 필요하다. 실패 건을 삭제하지 않고 격리하는지 확인.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.models import Coordinates
from app.services.crematorium import (
    CrematoriumAPIError,
    CrematoriumClient,
)


def row(name: str, addr: str, gubun: str = "공설", brz: str = "9") -> dict:
    return {
        "fcltNm": name,
        "addr": addr,
        "ctpv": "충청남도",
        "sigungu": "천안시",
        "gubun": gubun,
        "brzCnt": brz,
    }


def response(rows: list[dict], total: int | None = None) -> dict:
    return {
        "pageNo": "1",
        "resultCode": "00",
        "resultMsg": "NORMAL SERVICE",
        "numOfRows": "100",
        "totalCount": total if total is not None else len(rows),
        "items": rows,
    }


CHEONAN = Coordinates(lat=36.75, lng=127.10)


def geocoder(mapping: dict[str, Coordinates | None]):
    async def _geocode(address: str) -> Coordinates | None:
        return mapping.get(address)

    return _geocode


def client_with(handler, geocode) -> CrematoriumClient:
    return CrematoriumClient(
        service_key="test-key",
        geocode=geocode,
        transport=httpx.MockTransport(handler),
    )


class TestParameters:
    def test_sends_only_the_four_allowed_params(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=response([]))

        asyncio.run(client_with(handler, geocoder({})).all_crematoriums())

        assert seen, "요청이 전송되지 않았습니다"
        params = seen[0]
        assert set(params) == {"serviceKey", "pageNo", "numOfRows", "apiType"}
        assert params["apiType"] == "JSON"
        # type 을 붙이면 이 API 는 400 이 난다 — 절대 넣지 않는다.
        assert "type" not in params


class TestGeocoding:
    def test_geocodes_addresses_and_isolates_failures(self) -> None:
        rows = [
            row("천안추모공원", "충청남도 천안시 A"),
            row("좌표없는화장장", "충청남도 어딘가 B"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response(rows, total=2))

        client = client_with(
            handler,
            geocoder({"충청남도 천안시 A": CHEONAN, "충청남도 어딘가 B": None}),
        )
        located = asyncio.run(client.all_crematoriums())

        assert [c.name for c in located] == ["천안추모공원"]
        # 지오코딩 실패 건은 삭제하지 않고 사유와 함께 격리한다.
        failures = client.geocode_failures
        assert [f.name for f in failures] == ["좌표없는화장장"]
        assert "결과 없음" in failures[0].reason

    def test_around_filters_by_radius(self) -> None:
        rows = [
            row("가까운화장장", "가까움"),
            row("먼화장장", "멈"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response(rows, total=2))

        client = client_with(
            handler,
            geocoder(
                {
                    "가까움": CHEONAN,
                    "멈": Coordinates(lat=35.1796, lng=129.0756),
                }
            ),
        )
        near = asyncio.run(client.crematoriums_around(CHEONAN, 3000))
        assert [c.name for c in near] == ["가까운화장장"]


class TestErrorsAndEnablement:
    def test_error_result_code_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "resultCode": "30",
                    "resultMsg": "INVALID_REQUEST_PARAMETER_ERROR",
                    "items": [],
                },
            )

        with pytest.raises(CrematoriumAPIError):
            asyncio.run(client_with(handler, geocoder({})).all_crematoriums())

    def test_without_geocoder_is_disabled(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, json=response([row("x", "y")]))

        client = CrematoriumClient(
            service_key="test-key",
            geocode=None,
            transport=httpx.MockTransport(handler),
        )
        assert client.enabled is False
        assert asyncio.run(client.all_crematoriums()) == []
        assert called is False


# ---------------------------------------------------------------------------
# 검증 좌표 고정 — docs/hazards H-06 §6-3
# ---------------------------------------------------------------------------
class TestVerifiedCoordinates:
    """전주시승화원은 「효자동3가 3」 지오코딩값이 실제(산 170-1)와 1,931m 어긋난다.

    500m 기준 항목이라 이 오차는 판정을 완전히 뒤집는다. 지오코더가 어떤 값을
    돌려주든 VWorld·카카오 키워드 교차확인으로 확정한 좌표로 고정한다.
    """

    WRONG = Coordinates(lat=35.8231807, lng=127.1103465)  # 효자동3가 3
    RIGHT = Coordinates(lat=35.824386, lng=127.088977)     # 콩쥐팥쥐로 1705-138

    def test_jeonju_crematorium_uses_verified_coordinate_over_geocoder(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response([
                row("전주시승화원", "전북특별자치도 전주시 완산구 효자동3가 3"),
            ]))

        located = asyncio.run(client_with(
            handler,
            geocoder({"전북특별자치도 전주시 완산구 효자동3가 3": self.WRONG}),
        ).all_crematoriums())
        assert len(located) == 1
        assert abs(located[0].coordinates.lat - self.RIGHT.lat) < 1e-6
        assert abs(located[0].coordinates.lng - self.RIGHT.lng) < 1e-6

    def test_verified_coordinate_rescues_geocode_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response([
                row("전주시승화원", "전북 전주시 완산구 콩쥐팥쥐로 1705-138"),
            ]))

        client = client_with(handler, geocoder({}))
        located = asyncio.run(client.all_crematoriums())
        assert len(located) == 1
        assert not client.geocode_failures

    def test_other_facility_still_uses_geocoder(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response([row("천안추모공원", "천안 주소")]))

        located = asyncio.run(client_with(
            handler, geocoder({"천안 주소": CHEONAN}),
        ).all_crematoriums())
        assert located[0].coordinates == CHEONAN
