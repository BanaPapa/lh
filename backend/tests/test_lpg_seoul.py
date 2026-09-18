"""서울 열린데이터광장 액화석유가스업(SeoulListLPGSales) 어댑터 검증."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.models import Coordinates
from app.services.lpg_seoul import SEOUL_LPG_DATASET_ID, SeoulLpgClient, seoul_lpg_url
from app.services.kgs import PublicDataAPIError

ROWS = [
    {"REL_TRANS_CGG_CODE": "3220000", "PERM_NT_NO": "2601322000004100001", "WORK_SITE_SNO": "00001",
     "LPG_BSIN_SORT_CODE": "저장소설치", "TRNM_NM": "천마콘크리트", "OFF_TELNO": "", "PERM_NT_YMD": "20260406",
     "SITE_ADDR": "서울특별시 강남구 세곡동  19번지 1호  "},
    {"REL_TRANS_CGG_CODE": "3150000", "PERM_NT_NO": "2022315020404100001", "WORK_SITE_SNO": "00001",
     "LPG_BSIN_SORT_CODE": "판매사업", "TRNM_NM": "강서가스", "OFF_TELNO": "", "PERM_NT_YMD": "20220729",
     "SITE_ADDR": "서울특별시 강서구 등촌동 684번지 2호"},
    {"REL_TRANS_CGG_CODE": "3150000", "PERM_NT_NO": "x", "LPG_BSIN_SORT_CODE": "가스용품제조사업",
     "TRNM_NM": "제조사", "SITE_ADDR": "서울특별시 강서구 어딘가 1"},
]


def _body(rows, total=None, code="INFO-000"):
    return json.dumps({"SeoulListLPGSales": {"list_total_count": total if total is not None else len(rows),
                                               "RESULT": {"CODE": code, "MESSAGE": "정상 처리되었습니다"},
                                               "row": rows}}, ensure_ascii=False).encode()


def _transport(calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        start = int(str(request.url).rstrip("/").split("/")[-2])
        return httpx.Response(200, content=_body(ROWS if start == 1 else []))

    return httpx.MockTransport(handler)


async def geocode(address: str) -> Coordinates | None:
    if "세곡동" in address:
        return Coordinates(lat=37.4667, lng=127.1030)
    if "등촌동" in address:
        return Coordinates(lat=37.5530, lng=126.8590)
    return None


def test_url_shape() -> None:
    assert seoul_lpg_url("KEY", 1, 5) == "http://openapi.seoul.go.kr:8088/KEY/json/SeoulListLPGSales/1/5/"


def test_rows_geocoded_and_kinds_classified() -> None:
    calls: list[str] = []
    client = SeoulLpgClient("KEY", geocode=geocode, transport=_transport(calls))
    rows = asyncio.run(client.all_facilities())
    assert [(r.name, r.kind, r.kind_raw) for r in rows] == [
        ("천마콘크리트", "저장", "저장소설치"), ("강서가스", "판매", "판매사업"),
    ]
    assert rows[0].address == "서울특별시 강남구 세곡동 19번지 1호"
    assert rows[0].dataset_id == SEOUL_LPG_DATASET_ID and rows[0].record_id == "2601322000004100001"
    assert [f.name for f in client.geocode_failures] == ["제조사"]
    assert calls[0].endswith("/json/SeoulListLPGSales/1/1000/")


def test_only_answers_for_seoul_sites() -> None:
    client = SeoulLpgClient("KEY", geocode=geocode, transport=_transport())
    seoul = asyncio.run(client.facilities_for_site("서울특별시 강남구 세곡동 19", Coordinates(lat=37.4667, lng=127.1030), 100))
    assert [f.name for f in seoul.facilities] == ["천마콘크리트"] and seoul.covered
    other = asyncio.run(client.facilities_for_site("전북특별자치도 부안군 부안읍", Coordinates(lat=35.73, lng=126.73), 100))
    assert other.facilities == [] and not other.covered


def test_api_error_is_reported_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당하는 데이터가 없습니다."}}).encode())

    client = SeoulLpgClient("KEY", geocode=geocode, transport=httpx.MockTransport(handler))
    with pytest.raises(PublicDataAPIError, match="INFO-200"):
        asyncio.run(client.all_facilities())
    lookup = asyncio.run(client.facilities_for_site("서울특별시 강남구", Coordinates(lat=37.5, lng=127.0), 100))
    assert SEOUL_LPG_DATASET_ID in lookup.unavailable


def test_disabled_without_key() -> None:
    assert not SeoulLpgClient("", geocode=geocode).enabled
