"""국립중앙의료원 전국 병·의원 찾기 어댑터 검증.

심사표는 '종류=종합병원'만 인정한다. 병·의원 전체를 그대로 세면 배점이
부풀어 오른다. 종합병원·상급종합병원만 남기는지, 상급종합을 구분하는지 고정한다.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.models import Coordinates
from app.services.ncmc_hospital import (
    NcmcHospitalAPIError,
    NcmcHospitalClient,
)


def item_xml(name: str, div: str, lat: str, lng: str, hpid: str = "A0000001") -> str:
    return (
        "<item>"
        f"<dutyName>{name}</dutyName>"
        f"<dutyDiv>X</dutyDiv>"
        f"<dutyDivNam>{div}</dutyDivNam>"
        f"<dutyAddr>서울특별시 어딘가</dutyAddr>"
        f"<hpid>{hpid}</hpid>"
        f"<dutyTel1>02-000-0000</dutyTel1>"
        f"<wgs84Lat>{lat}</wgs84Lat>"
        f"<wgs84Lon>{lng}</wgs84Lon>"
        "</item>"
    )


def response_xml(items: list[str], total: int | None = None) -> str:
    total = total if total is not None else len(items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<response><header><resultCode>00</resultCode>"
        "<resultMsg>NORMAL SERVICE.</resultMsg></header>"
        f"<body><items>{''.join(items)}</items>"
        f"<numOfRows>200</numOfRows><pageNo>1</pageNo>"
        f"<totalCount>{total}</totalCount></body></response>"
    )


def client_with(handler) -> NcmcHospitalClient:
    return NcmcHospitalClient(service_key="test-key", transport=httpx.MockTransport(handler))


SEOUL = Coordinates(lat=37.5665, lng=126.9780)


class TestGeneralHospitalFilter:
    def test_keeps_only_general_and_tertiary_hospitals(self) -> None:
        rows = [
            item_xml("행복의원", "의원", "37.56", "126.97", "H1"),
            item_xml("가톨릭대여의도성모병원", "종합병원", "37.5182", "126.9367", "H2"),
            item_xml("서울대학교병원", "상급종합병원", "37.5799", "126.9989", "H3"),
            item_xml("사랑요양병원", "요양병원", "37.55", "126.98", "H4"),
            item_xml("미소치과의원", "치과의원", "37.54", "126.99", "H5"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=response_xml(rows, total=len(rows)))

        hospitals = asyncio.run(
            client_with(handler).general_hospitals_in_sido("서울특별시")
        )

        names = [h.name for h in hospitals]
        assert names == ["가톨릭대여의도성모병원", "서울대학교병원"]

    def test_tertiary_is_distinguished(self) -> None:
        rows = [
            item_xml("여의도성모병원", "종합병원", "37.5182", "126.9367", "H2"),
            item_xml("서울대학교병원", "상급종합병원", "37.5799", "126.9989", "H3"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=response_xml(rows))

        hospitals = asyncio.run(
            client_with(handler).general_hospitals_in_sido("서울특별시")
        )
        by_name = {h.name: h for h in hospitals}
        assert by_name["여의도성모병원"].is_tertiary is False
        assert by_name["서울대학교병원"].is_tertiary is True


class TestCoordinatesAndRadius:
    def test_out_of_korea_coordinate_is_dropped(self) -> None:
        rows = [item_xml("좌표이상병원", "종합병원", "0", "0")]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=response_xml(rows))

        assert asyncio.run(
            client_with(handler).general_hospitals_in_sido("서울특별시")
        ) == []

    def test_hospitals_around_filters_by_radius(self) -> None:
        rows = [
            item_xml("가까운종합병원", "종합병원", "37.5670", "126.9785", "N1"),
            item_xml("먼종합병원", "종합병원", "35.1796", "129.0756", "N2"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=response_xml(rows))

        near = asyncio.run(
            client_with(handler).hospitals_around(SEOUL, "서울특별시", 3000)
        )
        assert [h.name for h in near] == ["가까운종합병원"]


class TestRequestAndErrors:
    def test_sends_sido_as_q0(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, text=response_xml([]))

        asyncio.run(client_with(handler).general_hospitals_in_sido("전북특별자치도"))
        assert seen[0]["Q0"] == "전북특별자치도"

    def test_error_result_code_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = (
                '<?xml version="1.0"?><response><header>'
                "<resultCode>30</resultCode>"
                "<resultMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</resultMsg>"
                "</header><body/></response>"
            )
            return httpx.Response(200, text=body)

        with pytest.raises(NcmcHospitalAPIError):
            asyncio.run(client_with(handler).general_hospitals_in_sido("서울특별시"))

    def test_disabled_client_returns_empty(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, text=response_xml([]))

        client = NcmcHospitalClient(service_key="", transport=httpx.MockTransport(handler))
        assert asyncio.run(client.general_hospitals_in_sido("서울특별시")) == []
        assert called is False
