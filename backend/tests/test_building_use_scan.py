"""건축물대장 용도 스캔(건물통합정보 + 표제부·층별개요) 검증."""

from __future__ import annotations

import asyncio

from app.models import Coordinates
from app.services.building_register import (
    BuildingUse,
    BuildingUseResult,
    FloorUse,
    UseVerdict,
    parse_pnu,
)
from app.services.building_use_scan import (
    COVERED_KINDS,
    BuildingUseScanner,
    bbox_around,
    classify_floors,
    classify_use,
    is_hazmat_use,
)
from app.services.vworld import BuildingFeature, ParcelFeature

CENTER = Coordinates(lat=35.8242, lng=127.1480)


def _ring(lat: float, lng: float, d: float = 0.0002) -> list[Coordinates]:
    return [Coordinates(lat=lat - d, lng=lng - d), Coordinates(lat=lat - d, lng=lng + d),
            Coordinates(lat=lat + d, lng=lng + d), Coordinates(lat=lat + d, lng=lng - d),
            Coordinates(lat=lat - d, lng=lng - d)]


def _building(lat: float, lng: float, use_code: str, name: str = "") -> BuildingFeature:
    return BuildingFeature(name, "", use_code, _ring(lat, lng, 0.00008), 1)


def _floor(main: str, etc: str = "", code: str = "19999") -> FloorUse:
    return FloorUse("", "지상 1", code, main, etc, 10.0)


def test_classify_use_reads_etc_purpose_first() -> None:
    assert classify_use("위험물저장및처리시설", "액화석유가스 저장소") == "LPG저장"
    assert classify_use("위험물저장및처리시설", "LPG충전소") == "LPG충전"
    assert classify_use("위험물저장및처리시설", "주유소") == "주유소"
    assert classify_use("위험물저장및처리시설", "고압가스 저장소") == "고압가스"
    assert classify_use("위험물저장및처리시설", "위험물 옥외탱크저장소") == "위험물"
    assert classify_use("위험물저장및처리시설", "도시가스 정압기실") == "도시가스"
    assert classify_use("위험물저장및처리시설", "유독물 보관창고") == "유독물"
    assert classify_use("위험물저장및처리시설", "") == "미분류"
    assert classify_use("위험물저장및처리시설(주유소)", "") == "주유소"
    assert {"주유소", "LPG충전", "LPG판매", "고압가스", "액화가스", "도료류"} == set(COVERED_KINDS)


def test_is_hazmat_use_normalizes_spacing() -> None:
    assert is_hazmat_use("위험물 저장 및 처리시설")
    assert is_hazmat_use("공장", "위험물저장및처리시설")
    assert not is_hazmat_use("단독주택", "")


def test_bbox_around_is_symmetric() -> None:
    s, w, n, e = bbox_around(CENTER, 100)
    assert n - CENTER.lat > 0 and CENTER.lat - s > 0 and abs((n - CENTER.lat) - (CENTER.lat - s)) < 1e-9
    assert e - CENTER.lng > n - CENTER.lat  # 경도 1도가 짧으므로 더 넓게


def test_classify_floors_maps_standard_code_names_to_items() -> None:
    assert classify_floors([_floor("사무소", code="04402"), _floor("액화석유가스저장소")]) == ("LPG저장", "액화석유가스저장소")
    assert classify_floors([_floor("위험물저장소")]) == ("위험물", "위험물저장소")
    assert classify_floors([_floor("위험물취급소")])[0] == "위험물"
    assert classify_floors([_floor("유독물보관저장소")]) == ("유독물", "유독물보관저장소")
    assert classify_floors([_floor("유독물판매소")])[0] == "유독물"
    assert classify_floors([_floor("도시가스제조시설")]) == ("도시가스", "도시가스제조시설")
    assert classify_floors([_floor("화약류저장소")]) == ("화약류", "화약류저장소")
    # 연결된 원천이 덮는 종류도 이름 그대로 가른다(상위에서 뺀다)
    assert classify_floors([_floor("액화석유가스충전소", "가스충전소")])[0] == "LPG충전"
    assert classify_floors([_floor("주유소")])[0] == "주유소"
    assert classify_floors([_floor("고압가스저장소")])[0] == "고압가스"
    assert classify_floors([_floor("액화가스취급소")])[0] == "액화가스"
    assert classify_floors([_floor("도료류판매소")])[0] == "도료류"


def test_classify_floors_falls_back_to_etc_text_for_generic_code() -> None:
    # 「기타위험물저장처리시설」은 종류를 말해 주지 않으므로 기타용도 문자열로 가른다
    assert classify_floors([_floor("기타위험물저장처리시설", "LPG 벌크 저장탱크")]) == ("LPG저장", "LPG 벌크 저장탱크")
    assert classify_floors([_floor("기타위험물저장처리시설", "기계실")]) == ("미분류", "")
    assert classify_floors([]) == ("미분류", "")


def test_classify_floors_prefers_uncovered_hazard_over_covered_use() -> None:
    # 한 건물에 충전소(덮임)와 저장소(미연결)가 같이 있으면 미연결 종류가 이긴다
    floors = [_floor("액화석유가스충전소"), _floor("액화석유가스저장소")]
    assert classify_floors(floors)[0] == "LPG저장"


class FakeVWorld:
    enabled = True

    def __init__(self, buildings, parcels=()):
        self.buildings = list(buildings)
        self.parcels = list(parcels)
        self.building_calls = []
        self.parcel_calls = 0

    async def buildings_in_box(self, south, west, north, east, limit=1000):
        self.building_calls.append((south, west, north, east, limit))
        return self.buildings

    async def parcels_in_box(self, south, west, north, east, limit=1000):
        self.parcel_calls += 1
        return self.parcels


class FakeRegister:
    enabled = True

    def __init__(self, uses_by_pnu, floors_by_pnu=None, fail=(), fail_floors=()):
        self.uses_by_pnu = uses_by_pnu
        self.floors_by_pnu = floors_by_pnu or {}
        self.fail = set(fail)
        self.fail_floors = set(fail_floors)
        self.lookups = []
        self.floor_lookups = []

    async def lookup(self, pnu):
        self.lookups.append(pnu)
        if pnu in self.fail:
            raise RuntimeError("boom")
        uses = tuple(BuildingUse("동", m, e) for m, e in self.uses_by_pnu.get(pnu, []))
        return BuildingUseResult(pnu, parse_pnu(pnu), uses, UseVerdict.UNKNOWN, UseVerdict.UNKNOWN)

    async def lookup_floors(self, pnu):
        self.floor_lookups.append(pnu)
        if pnu in self.fail_floors:
            raise RuntimeError("floor boom")
        return tuple(self.floors_by_pnu.get(pnu, ()))


P1 = ParcelFeature("4511310100100010000", "전주 A", "1", _ring(35.8243, 127.1481, 0.00004), 100)
P2 = ParcelFeature("4511310100100020000", "전주 B", "2", _ring(35.8241, 127.1479, 0.00004), 100)
P3 = ParcelFeature("4511310100100030000", "전주 C", "3", _ring(35.8240, 127.1478, 0.00004), 100)


def test_scan_uses_building_use_codes_and_floors_for_hazmat_only() -> None:
    hazmat = _building(35.8243, 127.1481, "19000", "가스")     # 코드 있음 → 층별개요
    house = _building(35.8241, 127.1479, "02000", "빌라")      # 공동주택 → 건너뜀
    blank = _building(35.8240, 127.1478, "", "미연계")          # 공란 → 표제부 보완
    vworld = FakeVWorld([hazmat, house, blank], parcels=[P1, P2, P3])
    register = FakeRegister(
        {P3.pnu: [("위험물저장및처리시설", "액화석유가스 저장소")]},
        floors_by_pnu={P1.pnu: [_floor("화약류저장소")]},
    )
    result = asyncio.run(BuildingUseScanner(vworld, register).scan(CENTER, 60))
    # 표제부는 공란 건물의 필지만, 층별개요는 위험물 건물의 필지만 부른다
    assert register.lookups == [P3.pnu]
    # 건물이 걸친 필지를 겹침 순으로 더 보므로(옆 필지 살짝 겹침) 두 필지는 반드시 포함된다
    assert {P1.pnu, P3.pnu} <= set(register.floor_lookups)
    by_pnu = {b.pnu: b for b in result.buildings}
    assert (by_pnu[P1.pnu].kind, by_pnu[P1.pnu].basis, by_pnu[P1.pnu].evidence) == ("화약류", "code", "화약류저장소")
    # 층별개요에 세부 코드명이 없는 건물은 표제부 문자열 추정으로 내려간다
    assert (by_pnu[P3.pnu].kind, by_pnu[P3.pnu].basis) == ("LPG저장", "text")
    assert by_pnu[P1.pnu].address == "전주 A" and len(by_pnu[P1.pnu].ring) == 5
    assert result.parcels_seen == 3 and result.lookups >= 3 and result.failed_pnus == []
    assert result.complete


def test_scan_marks_failed_fallback_and_is_incomplete() -> None:
    blank = _building(35.8240, 127.1478, "", "미연계")
    vworld = FakeVWorld([blank], parcels=[P3])
    register = FakeRegister({}, fail=[P3.pnu])
    result = asyncio.run(BuildingUseScanner(vworld, register).scan(CENTER, 60))
    assert result.failed_pnus == [P3.pnu] and not result.complete and result.buildings == []


def test_scan_without_hazmat_makes_no_register_calls() -> None:
    vworld = FakeVWorld([_building(35.8243, 127.1481, "02000"), _building(35.8241, 127.1479, "04000")])
    register = FakeRegister({})
    result = asyncio.run(BuildingUseScanner(vworld, register).scan(CENTER, 60))
    assert register.lookups == [] and register.floor_lookups == [] and vworld.parcel_calls == 0
    assert result.complete and result.buildings == []


def test_scan_clips_to_radius_and_caps_radius(monkeypatch) -> None:
    import app.services.building_use_scan as mod

    monkeypatch.setattr(mod, "MAX_BUILDINGS", 2)
    near = _building(35.8243, 127.1481, "19000")
    far = _building(35.8300, 127.1600, "19000")  # ~1km
    vworld = FakeVWorld([near, far], parcels=[P1])
    register = FakeRegister({}, floors_by_pnu={P1.pnu: [_floor("위험물저장소")]})
    result = asyncio.run(BuildingUseScanner(vworld, register).scan(CENTER, 1_200))
    assert vworld.building_calls[0][3] - vworld.building_calls[0][1] < 0.01  # 반경 상한 200m
    assert [b.kind for b in result.buildings] == ["위험물"]
    # 받은 건물 수가 상한에 닿으면 빠진 건물이 있을 수 있어 미완료
    assert result.parcels_seen == 2 and not result.complete


def test_scanner_disabled_when_a_dependency_is_missing() -> None:
    class Off:
        enabled = False

    assert not BuildingUseScanner(Off(), FakeRegister({})).enabled
    assert not BuildingUseScanner(FakeVWorld([]), None).enabled
    assert asyncio.run(BuildingUseScanner(Off(), FakeRegister({})).scan(CENTER, 50)).buildings == []
