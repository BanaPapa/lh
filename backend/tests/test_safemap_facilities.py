"""생활안전지도 시설 레이어 파서·피드 검증(2026-09-17 실측 컬럼 기준)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.safemap_facilities import (
    SafemapFacilityFeed,
    mercator_to_wgs84,
    parse_chemical,
    parse_emission,
    parse_hospital,
    parse_office,
    parse_school,
)

OFFICE_ROW = {
    "ctprvn_cd": "12", "objt_id": "135629", "fclty_cd": "501010", "emd_cd": "12300104",
    "fclty_nm": "광주고용복지플러스센터", "telno": "062-609-8500",
    "rn_adres": "전남광주통합특별시 북구 금남로 121", "sgg_cd": "12300",
    "fclty_ty": "관공서", "x": "14127352.37", "y": "4185018.582", "adres": "",
}
SCHOOL_ROW = {
    "fcltynm": "가거도초등학교", "secd": "03", "fcltycd": "SH00010417",
    "latitude": 34.0531192, "lnmadr": "전남광주통합특별시 신안군 흑산면 가거도리 266",
    "x": "13929473.9956", "y": "4035936.86898", "fcltyty": "01", "longitude": 125.1305939,
}
HOSPITAL_ROW = {
    "hpid": "A2500005", "dutyname": "전주병원", "dutydivname": "종합병원",
    "dutyaddr": "전북특별자치도 전주시 완산구 한두평3길 13", "lat": 35.815, "lon": 127.1248,
}
EMISSION_NO_XY = {"objt_id": 45315, "x": "0", "y": "0", "fclty_se": "수질",
                  "adres": "인천광역시남동구고잔동737-8", "fclty_nm": "태영산업"}
CHEMICAL_ROW = {
    "sgg_cd": 29170, "objt_id": 3213, "induty_nm": "전자 부품 제조업", "entrps_nm": "(주)연호전자",
    "x": "14121814.97628", "y": "4195828.89717", "adres": "-",
    "rn_adres": "광주광역시 북구 첨단벤처로 140",
}


def test_mercator_conversion_lands_in_gwangju() -> None:
    point = mercator_to_wgs84(14127352.37, 4185018.582)
    assert abs(point.lat - 35.1554) < 0.001 and abs(point.lng - 126.9082) < 0.001


def test_office_parser_reads_mercator_and_road_address() -> None:
    facility = parse_office("IF_0031", OFFICE_ROW)
    assert facility is not None
    assert facility.kind == "관공서" and facility.address.startswith("전남광주")
    assert abs(facility.coordinates.lat - 35.1554) < 0.001


def test_school_parser_uses_code_then_name_token() -> None:
    assert parse_school("IF_0035", SCHOOL_ROW).kind == "초등학교"
    branch = dict(SCHOOL_ROW, fcltyty="06", fcltynm="가곡초등학교대곡분교장")
    assert parse_school("IF_0035", branch).kind == "초등학교"


def test_hospital_parser_reads_latlon() -> None:
    facility = parse_hospital("IF_0022", HOSPITAL_ROW)
    assert facility is not None and facility.record_id == "A2500005"
    assert facility.kind == "종합병원"


def test_emission_without_coordinates_is_dropped() -> None:
    assert parse_emission("IF_0040", EMISSION_NO_XY) is None


def test_chemical_parser_prefers_road_address_over_dash() -> None:
    facility = parse_chemical("IF_0049", CHEMICAL_ROW)
    assert facility is not None
    assert facility.address == "광주광역시 북구 첨단벤처로 140"
    assert facility.kind == "전자 부품 제조업"


def _transport(rows: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {"totalCount": len(rows), "items": rows},
        }
        return httpx.Response(200, content=json.dumps(payload).encode())

    return httpx.MockTransport(handler)


def test_feed_filters_by_radius_and_caches() -> None:
    feed = SafemapFacilityFeed("key", "IF_0031", transport=_transport([OFFICE_ROW]))
    center = Coordinates(lat=35.1554, lng=126.9082)
    near = asyncio.run(feed.facilities_around(center, 500))
    far = asyncio.run(feed.facilities_around(Coordinates(lat=37.5, lng=127.0), 500))
    assert len(near) == 1 and far == []
    assert haversine_meters(center, near[0].coordinates) < 100


def test_feed_without_key_is_disabled() -> None:
    feed = SafemapFacilityFeed("", "IF_0035")
    assert not feed.enabled
    assert asyncio.run(feed.all_facilities()) == []


def test_unknown_layer_is_rejected() -> None:
    with pytest.raises(ValueError):
        SafemapFacilityFeed("key", "IF_0033")
