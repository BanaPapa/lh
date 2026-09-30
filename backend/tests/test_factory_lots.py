"""등록공장 지번 파일(전주시 공장등록현황 3069076) — API 행을 등록 지번 필지에 붙인다.

공장등록 API 는 도로명만 준다. 대진식품 「추천로 217-14」는 도로명으로 재면 팔복동1가 10-5
(사업지 송천동1가 626-74 에서 약 578m)인데, LH 앱은 등록 지번 팔복동1가 711-2(404.2m)로 잰다.
"""

from __future__ import annotations

import asyncio
import csv

from fastapi.testclient import TestClient

from app.models import Coordinates
from app.services.factory_lots import (
    FACTORY_LOTS_CSV,
    FactoryLot,
    FactoryLotIndex,
    normalize_name,
    normalize_road,
    read_lots,
)
from app.services.factory_registry import FactoryRegistryClient
from tests.test_factory_registry import Recorder, factory_row, xml_page

DAEJIN_PNU = "5211310800107110002"
DAEJIN_ROAD = "전북특별자치도 전주시 덕진구 추천로 217-14, 대진식품 (팔복동1가)"
DAEJIN_POINT = Coordinates(lat=35.8560666, lng=127.1146636)


def lot(name: str, road: str, pnu: str, point: Coordinates | None = DAEJIN_POINT) -> FactoryLot:
    return FactoryLot("1", name, road, f"{name} 지번", pnu, 0, point)


def test_normalizers_ignore_corporate_marks_and_building_tails() -> None:
    assert normalize_name("(주) 대진식품") == normalize_name("주식회사대진식품") == "대진식품"
    assert normalize_road(DAEJIN_ROAD) == "전주시덕진구추천로217-14"
    assert normalize_road("전북특별자치도 전주시 덕진구 추천로 217-14(팔복동1가)") == "전주시덕진구추천로217-14"


def test_index_matches_name_and_road_then_unique_name_only() -> None:
    index = FactoryLotIndex(
        [
            lot("대진식품", DAEJIN_ROAD, DAEJIN_PNU),
            lot("같은이름", "전주시 덕진구 가로 1", "5211310800100010000"),
            lot("같은이름", "전주시 덕진구 나로 2", "5211310800100020000"),
        ]
    )
    assert index.match("대진식품", "전북특별자치도 전주시 덕진구 추천로 217-14 (팔복동1가)").pnu == DAEJIN_PNU
    # 법인 표기·건물명·층 표기는 떼고 맞춘다.
    assert index.match("(주)대진식품", "전주시 덕진구 추천로 217-14, 2층").pnu == DAEJIN_PNU
    # 주소가 다르면 같은 회사의 다른 공장일 수 있어 맞추지 않는다. 주소가 비었으면 한 곳뿐일 때만.
    assert index.match("대진식품", "전주시 덕진구 다른로 9") is None
    assert index.match("대진식품", "").pnu == DAEJIN_PNU
    # API 가 도로명 칸에 지번을 적은 행은 파일의 지번주소와 맞춘다(「번지」「외 N필지」는 뗀다).
    assert index.match("대진식품", "대진식품 지번 외 1필지").pnu == DAEJIN_PNU
    # 같은 이름이 여럿이면 도로명이 같을 때만.
    assert index.match("같은이름", "전주시 덕진구 나로 2").pnu.endswith("00020000")
    assert index.match("같은이름", "전주시 덕진구 다로 3") is None
    assert index.match("없는공장", DAEJIN_ROAD) is None


def test_matched_row_uses_registered_lot_instead_of_geocoding(tmp_path) -> None:
    calls: list[str] = []

    async def geocoder(address: str) -> Coordinates | None:
        calls.append(address)
        return Coordinates(lat=35.86, lng=127.10)  # 도로명 지점(팔복동1가 10-5 근처)

    pages = [xml_page([factory_row("000002", "대진식품", DAEJIN_ROAD)], total=1)]
    client = FactoryRegistryClient(
        "key", geocoder=geocoder, cache_path=tmp_path / "geo.json",
        transport=Recorder(pages).transport(),
        lot_index=FactoryLotIndex([lot("대진식품", DAEJIN_ROAD, DAEJIN_PNU)]),
    )
    found = asyncio.run(client.factories_near(DAEJIN_POINT, 100, "52113"))
    assert len(found) == 1
    record = found[0]
    assert record.pnu == DAEJIN_PNU and record.coordinates == DAEJIN_POINT
    assert record.lot is not None and record.address == "대진식품 지번"
    assert record.road_address == DAEJIN_ROAD
    assert calls == []  # 도로명 지오코딩을 하지 않는다


def test_lot_without_cadastral_point_falls_back_to_geocoding(tmp_path) -> None:
    near = Coordinates(lat=35.86, lng=127.10)

    async def geocoder(address: str) -> Coordinates | None:
        return near

    pages = [xml_page([factory_row("000002", "대진식품", DAEJIN_ROAD)], total=1)]
    client = FactoryRegistryClient(
        "key", geocoder=geocoder, cache_path=tmp_path / "geo.json",
        transport=Recorder(pages).transport(),
        lot_index=FactoryLotIndex([lot("대진식품", DAEJIN_ROAD, DAEJIN_PNU, point=None)]),
    )
    found = asyncio.run(client.factories_near(near, 100, "52113"))
    assert found[0].coordinates == near and found[0].pnu == "" and found[0].lot is None


def test_bundled_file_carries_daejin_registered_lot() -> None:
    """서버에 실은 파일: 대진식품 → 팔복동1가 711-2(PNU 5211310800107110002) · 지적도 좌표 있음."""

    assert FACTORY_LOTS_CSV.is_file()
    with FACTORY_LOTS_CSV.open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert header == [
        "seq", "name", "road_address", "jibun_address", "pnu", "extra_lots", "lat", "lng", "as_of",
    ]
    lots = read_lots()
    assert len(lots) > 1000
    index = FactoryLotIndex(lots)
    daejin = index.match("대진식품", DAEJIN_ROAD)
    assert daejin is not None
    assert daejin.pnu == DAEJIN_PNU
    assert "팔복동1가 711-2" in daejin.jibun_address
    assert daejin.coordinates is not None
    assert all(l.pnu == "" or (len(l.pnu) == 19 and l.pnu.startswith("5211")) for l in lots)


def test_bundled_files_endpoint_is_public_and_lists_required_files() -> None:
    from app.main import app

    outsider = TestClient(app, client=("203.0.113.9", 51000))
    response = outsider.get("/api/settings/bundled-files")
    assert response.status_code == 200
    files = {f["id"]: f for f in response.json()["files"]}
    for required in (
        "factory_lots", "gas_product_source", "gas_product_geocoded",
        "university_gates", "station_exits", "lh_alignments",
    ):
        assert required in files, required
        entry = files[required]
        assert entry["present"] is True
        assert entry["name"] and entry["purpose"] and entry["source"] and entry["as_of"]
        assert entry["count"] and entry["count"] > 0
    assert files["gas_product_source"]["as_of"] == "2025-09-30"
    assert files["factory_lots"]["as_of"] == "2026-07-10"
    assert "3069076" in files["factory_lots"]["source_id"]
    # 키·비밀이 새지 않는다(경로·출처 문자열만).
    assert "KEY" not in response.text
    assert outsider.put("/api/settings/bundled-files", json={}).status_code == 405
