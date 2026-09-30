"""LH 개별 맞춤 등록부 — 공공 API 자료를 LH 데이터셋 기준에 일부러 맞춘 특이점.

(1) 저장소 등록부가 규칙(근거·사유·시설군)을 지키는가, (2) 수집기가 항목대로 맞추고
「LH 개별 맞춤」 표시를 남기는가, (3) LH 기준점이 LH 거리를 실제로 재현하는가를 본다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import lh_alignments, rules_config
from app.lh_alignments import (
    LH_ALIGNMENT_BADGE,
    LhAlignment,
    LhAlignmentRegistry,
    lh_excluded_reason,
)
from app.models import Coordinates
from app.screening.amenities import AmenityCollector
from app.screening.scorebook import FACILITY_GROUP_BY_KEY
from app.services.geo import distance_point_to_polygons_m, offset_coordinates
from app.services.vworld import ParcelFeature
from tests.test_address_parcels import PnuVWorld
from tests.test_screening_amenities import (
    CENTER,
    DisabledKakao,
    FakeKakao,
    FakeTago,
    FakeVWorld,
    place,
    square_ring,
)


def entry(**fields) -> LhAlignment:
    base = {
        "id": "test",
        "name": "시험시설",
        "group": "hospital",
        "action": "point",
        "reason": "공공 API 자료 범위를 LH 데이터셋 기준에 맞췄습니다.",
        "source": "facilities.xlsx 시험행",
        "date": "2026-09-30",
    }
    base.update(fields)
    return LhAlignment.model_validate(base)


def lh_at(offset_north_m: float, **extra) -> dict:
    point = offset_coordinates(CENTER, offset_north_m, 0)
    return {"lat": point.lat, "lng": point.lng, **extra}


# ---------------------------------------------------------------------------
# 저장소 등록부
# ---------------------------------------------------------------------------


def test_registry_file_is_valid_and_every_entry_has_lh_evidence() -> None:
    raw = json.loads(lh_alignments.ALIGNMENTS_PATH.read_text(encoding="utf-8"))
    registry = LhAlignmentRegistry.model_validate(raw)

    ids = [e.id for e in registry.entries]
    assert len(ids) == len(set(ids)), "항목 id 는 겹치지 않아야 한다"
    assert registry.entries, "등록부가 비어 있으면 안 된다"
    for item in registry.entries:
        assert item.reason and item.source and item.date
        # 설명은 「LH 데이터셋 기준에 맞췄다」로 쓴다. 앱 오류로 읽히는 표현을 쓰지 않는다.
        assert "LH" in item.reason
        for banned in ("잘못", "오류", "버그"):
            assert banned not in item.reason, f"{item.id}: 사유에 「{banned}」"
        if item.scope == "amenity":
            assert item.group in FACILITY_GROUP_BY_KEY, f"{item.id}: 알 수 없는 시설군"
            # 같은 시설인지 가리는 기준점과 LH 거리 근거가 있어야 한다.
            assert item.lh.coordinates is not None, f"{item.id}: LH 기준점 없음"
            assert item.lh.evidence, f"{item.id}: LH 거리 근거 없음"
        if item.action == "merge":
            assert item.merge_from
            for source in item.merge_from:
                assert source.group in FACILITY_GROUP_BY_KEY


def test_registry_keeps_the_hazard_exclusion_of_the_lh_app() -> None:
    registry = lh_alignments.load_registry()
    hazard = [e for e in registry.entries if e.scope == "hazard"]
    assert any(e.name == "SK신흥주유소" and e.action == "exclude" for e in hazard)


# 17건 표본의 사업지 필지(연속지적도). LH 기준점이 LH앱 거리를 재현하는지 본다.
SITES: dict[str, list[list[tuple[float, float]]]] = {
    "004": [[(35.8611441, 127.114962), (35.8611651, 127.1149148), (35.8613339, 127.1148939), (35.8614168, 127.1149477), (35.861617, 127.1150745), (35.8616655, 127.1151017), (35.8616969, 127.1151216), (35.8616674, 127.1151341), (35.8615196, 127.1151704), (35.861513, 127.1151718), (35.8614109, 127.1152069), (35.8614016, 127.1152095), (35.8613697, 127.1152209), (35.8612487, 127.1152628), (35.8611441, 127.114962)]],
    "006": [[(35.8366315, 127.1315493), (35.8365586, 127.131447), (35.8366897, 127.1313072), (35.8366968, 127.1313171), (35.8367589, 127.1314057), (35.836761, 127.1314087), (35.8368147, 127.1314851), (35.8368454, 127.1315286), (35.8367167, 127.131668), (35.8366315, 127.1315493)]],
    "002": [[(35.8152911, 127.1038497), (35.8152909, 127.1040766), (35.8150471, 127.1040761), (35.8150473, 127.1038494), (35.8152911, 127.1038497)]],
    "077": [[(35.8164855, 127.1041001), (35.8163185, 127.1040999), (35.8163186, 127.1038617), (35.8163499, 127.1038231), (35.8164857, 127.1038233), (35.8164855, 127.1041001)]],
    "094": [
        [(35.9414419, 126.9481214), (35.9414423, 126.9481217), (35.941427, 126.9482175), (35.9413972, 126.9483308), (35.941182, 126.9482392), (35.9412209, 126.9480245), (35.9412317, 126.948028), (35.9412302, 126.9480336), (35.9413067, 126.9480593), (35.9414419, 126.9481214)],
        [(35.9415406, 126.9484256), (35.9413849, 126.9483774), (35.9413972, 126.9483308), (35.941427, 126.9482175), (35.9415794, 126.9482674), (35.9415406, 126.9484256)],
        [(35.9416013, 126.9481783), (35.9415794, 126.9482674), (35.941427, 126.9482175), (35.9414423, 126.9481217), (35.9414717, 126.9481353), (35.9414747, 126.9481368), (35.9415447, 126.9481598), (35.9415737, 126.9481693), (35.9416013, 126.9481783)],
    ],
    "115": [
        [(35.8359065, 127.1298541), (35.8357971, 127.1299743), (35.835754, 127.1300215), (35.8357312, 127.1300468), (35.8356566, 127.1299451), (35.83568, 127.1299203), (35.8358334, 127.129754), (35.8359065, 127.1298541)],
        [(35.8358334, 127.129754), (35.83568, 127.1299203), (35.8356566, 127.1299451), (35.835633, 127.1299131), (35.8356297, 127.1299082), (35.8355915, 127.1298558), (35.8355828, 127.1298441), (35.835606, 127.1298188), (35.8356685, 127.1297518), (35.8357599, 127.1296532), (35.8358334, 127.129754)],
    ],
}


@pytest.mark.parametrize(
    ("entry_id", "site", "lh_distance"),
    [
        ("univ-yesu-school-point", "002", 2807),
        ("univ-yesu-school-point", "077", 2806),
        ("univ-yesu-school-point", "006", 2352),
        ("univ-wonkwang-gate-point", "094", 2848),
        ("univ-jeonju-gate-point", "002", 894),
        ("univ-jeonju-gate-point", "077", 923),
        ("univ-jeonju-vision-gate-point", "002", 1188),
        ("univ-jeonju-vision-gate-point", "077", 1294),
        ("busstop-express-terminal-buddhist-point", "115", 161),
        # 파인트리몰: LH PNU(송천동2가 488-3)가 연속지적도에 없어 LH앱이 점포 좌표로 잰 값.
        ("retail-pinetree-mall-point", "004", 792.3),
    ],
)
def test_lh_points_reproduce_lh_distances(entry_id: str, site: str, lh_distance: float) -> None:
    item = next(e for e in lh_alignments.load_registry().entries if e.id == entry_id)
    rings = [[Coordinates(lat=lat, lng=lng) for lat, lng in ring] for ring in SITES[site]]
    measured = distance_point_to_polygons_m(item.lh.coordinates, rings)
    assert measured == pytest.approx(lh_distance, abs=1.0)


# ---------------------------------------------------------------------------
# 이름 맞추기
# ---------------------------------------------------------------------------


def test_university_matches_by_school_and_campus_distance() -> None:
    gate = offset_coordinates(CENTER, 1000, 0)
    item = entry(name="원광대학교", aliases=["원광디지털대학교"], group="university",
                 lh={"lat": gate.lat, "lng": gate.lng})
    near = offset_coordinates(CENTER, 1200, 0)
    far = offset_coordinates(CENTER, 20_000, 0)
    assert item.matches("원광대학교 한의학전문대학원", near)
    assert item.matches("원광디지털대학교 웰빙문화대학원", near)
    assert not item.matches("원광보건대학교", near)
    # 이름이 같아도 다른 캠퍼스(20km)면 다른 학교다.
    assert not item.matches("원광대학교", far)


def test_other_groups_match_exact_names_only() -> None:
    item = entry(name="익산", group="terminal", lh=lh_at(100))
    assert item.matches("익산", CENTER)
    assert not item.matches("익산시외고속버스터미널", CENTER)
    renamed = entry(name="금암동 주민센터", aliases=["금암1동주민센터"], group="public", lh=lh_at(0))
    assert renamed.matches("금암1동 주민센터", CENTER)


# ---------------------------------------------------------------------------
# 수집기 적용
# ---------------------------------------------------------------------------


def hospital_kakao(name: str = "시험병원", offset_m: float = 800) -> FakeKakao:
    return FakeKakao(categories={"HP8": [place(name, offset_m, "의료,건강 > 병원 > 종합병원")]})


@pytest.mark.asyncio
async def test_point_entry_remeasures_at_the_lh_coordinate_and_marks_the_hit() -> None:
    item = entry(name="시험병원", lh=lh_at(500))
    collector = AmenityCollector(kakao=hospital_kakao(), tago=FakeTago([]), alignments=[item])

    result = await collector.collect([], CENTER)

    group = result["hospital"]
    facility = group.facilities[0]
    assert facility.distance_m == pytest.approx(500, abs=2)
    assert group.distances_m == (facility.distance_m,)
    assert facility.lh_alignment.startswith(LH_ALIGNMENT_BADGE)
    assert "LH 좌표" in facility.measurement_label


@pytest.mark.asyncio
async def test_point_entry_with_pnu_measures_to_the_lh_parcel_even_on_rail_land() -> None:
    parcel_center = offset_coordinates(CENTER, 400, 0)
    pnu = "5214010200100010000"
    vworld = PnuVWorld(
        by_pnu={
            pnu: ParcelFeature(pnu=pnu, address="", jibun="1철",
                               ring=square_ring(parcel_center, 50.0), area_m2=10_000)
        }
    )
    item = entry(name="시험병원", lh=lh_at(400, pnu=pnu))
    collector = AmenityCollector(
        kakao=hospital_kakao(), tago=FakeTago([]), vworld=vworld, alignments=[item]
    )

    result = await collector.collect([], CENTER)

    facility = result["hospital"].facilities[0]
    # 필지 중심 400m − 반폭 50m. 지목 「철」이어도 LH 가 지정한 필지는 그대로 쓴다.
    assert facility.distance_m == pytest.approx(350, abs=3)
    assert facility.measurement_tier == "site_boundary"
    assert pnu in facility.front_door_notice
    assert pnu in vworld.pnu_calls


@pytest.mark.asyncio
async def test_point_entry_without_pnu_overrides_the_coordinate_parcel_of_a_retail_store(
    tmp_path,
) -> None:
    """파인트리몰(004): 점포 좌표를 품는 필지(1422) 경계 대신 LH 좌표에서 잰다.

    LH 데이터셋의 점포 PNU(송천동2가 488-3)는 연속지적도에 없어 LH앱이 점포 좌표로
    쟀다(792.3m). 등록부 항목은 pnu 없이 좌표만 두고, 좌표 필지 측정을 LH 좌표로 바꾼다.
    """

    from app.services.facility_store import FacilityStore
    from app.services.localdata import LocalDataRecord

    store_point = offset_coordinates(CENTER, 800, 0)
    store = FacilityStore(db_path=tmp_path / "facilities.db")
    store.replace_dataset(
        "large_scale_retail_stores",
        [
            LocalDataRecord(
                dataset_key="large_scale_retail_stores",
                record_id="pinetree",
                name="파인트리몰",
                address="전북특별자치도 전주시 덕진구 송천동2가 488-3",
                road_address="전북특별자치도 전주시 덕진구 송천중앙로 225(송천동2가)",
                coordinates=store_point,
                status="영업/정상",
                category="그 밖의 대규모점포",
            )
        ],
    )
    item = entry(
        id="retail-pinetree-mall-point", name="파인트리몰", group="retail", lh=lh_at(800)
    )
    # 좌표 필지(반폭 30m)로 재면 770m 가 된다. LH 좌표로 재면 800m.
    collector = AmenityCollector(
        kakao=FakeKakao(),
        tago=FakeTago([]),
        facility_store=store,
        vworld=FakeVWorld(half=30.0),
        alignments=[item],
    )

    result = await collector.collect([], CENTER, radius_m=3000)

    facility = result["retail"].facilities[0]
    assert facility.name == "파인트리몰"
    assert facility.distance_m == pytest.approx(800, abs=2)
    assert result["retail"].distances_m == (facility.distance_m,)
    assert facility.measurement_tier == "coordinate"
    assert "LH 좌표" in facility.measurement_label
    assert facility.lh_alignment.startswith(LH_ALIGNMENT_BADGE)
    assert facility.facility_ring == ()


@pytest.mark.asyncio
async def test_rename_keeps_distance_and_records_the_api_name() -> None:
    item = entry(name="LH병원", aliases=["시험병원"], action="rename", lh=lh_at(800))
    collector = AmenityCollector(kakao=hospital_kakao(), tago=FakeTago([]), alignments=[item])

    result = await collector.collect([], CENTER)

    facility = result["hospital"].facilities[0]
    assert facility.name == "LH병원"
    assert facility.distance_m == pytest.approx(800, abs=2)
    assert "시험병원" in facility.lh_alignment


@pytest.mark.asyncio
async def test_exclude_drops_the_facility_and_its_distance() -> None:
    item = entry(name="시험병원", action="exclude", lh=lh_at(800))
    collector = AmenityCollector(kakao=hospital_kakao(), tago=FakeTago([]), alignments=[item])

    result = await collector.collect([], CENTER)

    assert result["hospital"].facilities == ()
    assert result["hospital"].distances_m == ()


@pytest.mark.asyncio
async def test_add_puts_in_a_missing_lh_facility_once() -> None:
    item = entry(name="LH병원", action="add", lh=lh_at(300))
    collector = AmenityCollector(kakao=hospital_kakao(), tago=FakeTago([]), alignments=[item])

    result = await collector.collect([], CENTER)

    names = [f.name for f in result["hospital"].facilities]
    assert names == ["LH병원", "시험병원"]
    assert result["hospital"].distances_m[0] == pytest.approx(300, abs=2)

    # 공공 API 가 같은 이름을 돌려주면 새로 더하지 않고 위치만 LH 기준으로 맞춘다.
    same = AmenityCollector(
        kakao=hospital_kakao("LH병원", 2500), tago=FakeTago([]), alignments=[item]
    )
    only = (await same.collect([], CENTER))["hospital"]
    assert [f.name for f in only.facilities] == ["LH병원"]
    assert only.facilities[0].distance_m == pytest.approx(300, abs=2)


@pytest.mark.asyncio
async def test_add_skips_facilities_outside_the_radius_and_missing_groups() -> None:
    far = entry(name="먼병원", action="add", lh=lh_at(3500))
    collector = AmenityCollector(kakao=hospital_kakao(), tago=FakeTago([]), alignments=[far])
    result = await collector.collect([], CENTER)
    assert [f.name for f in result["hospital"].facilities] == ["시험병원"]

    # 원천이 없는(missing) 시설군에는 LH 시설만 끼워 넣지 않는다.
    added = entry(name="LH병원", action="add", lh=lh_at(300))
    missing = AmenityCollector(kakao=DisabledKakao(), tago=FakeTago([]), alignments=[added])
    groups = await missing.collect([], CENTER)
    assert groups["hospital"].state == "missing"
    assert groups["hospital"].facilities == ()


@pytest.mark.asyncio
async def test_merge_folds_the_transfer_hit_into_the_lh_terminal() -> None:
    kakao = FakeKakao(
        keywords={
            "환승센터": [place("익산역환승장", 90, "교통,수송 > 교통시설 > 환승센터")],
            "버스터미널": [place("익산시외버스터미널", 1100, "교통,수송 > 터미널 > 고속,시외버스터미널")],
        }
    )
    item = entry(
        name="익산", group="terminal", action="merge",
        merge_from=[{"group": "transfer", "names": ["익산역환승장"]}],
        lh=lh_at(95),
    )
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]), alignments=[item])

    result = await collector.collect([], CENTER)

    assert [f.name for f in result["terminal"].facilities] == ["익산", "익산시외버스터미널"]
    assert result["terminal"].distances_m[0] == pytest.approx(95, abs=2)
    assert result["transfer"].facilities == ()
    assert result["transfer"].distances_m == ()


def kakao_doc(name: str, lat: float, lng: float, category: str) -> dict:
    return {
        "place_name": name,
        "address_name": f"{name} 주소",
        "road_address_name": "",
        "category_name": category,
        "x": str(lng),
        "y": str(lat),
    }


@pytest.mark.asyncio
async def test_iksan_094_universities_follow_the_registry() -> None:
    """094 익산 창인동1가: 저장소 등록부 그대로 LH 거리·이름이 나온다(실측 좌표)."""

    school = "교육,학문 > 학교 > 대학교"
    kakao = FakeKakao(
        keywords={
            "대학교": [
                kakao_doc("원광대학교", 35.96783, 126.9557171, school),
                kakao_doc("원광디지털대학교", 35.9713179, 126.9562626, school),
                kakao_doc("전북대학교 특성화캠퍼스", 35.9432888, 126.9602009, school),
            ]
        }
    )
    rings = [[Coordinates(lat=lat, lng=lng) for lat, lng in ring] for ring in SITES["094"]]
    center = rings[0][0]
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]))

    result = await collector.collect(rings, center)

    rows = [(f.name, round(f.distance_m)) for f in result["university"].facilities]
    # LH: 전북대학교 수의방역대학원 1,286 · 원광대학교 2,848 · 원광디지털대학교 2,848(같은 정문).
    assert rows[0] == ("전북대학교 수의방역대학원", 1286)
    assert rows[1][1] == 2848 and rows[1][0].startswith("원광")
    # 같은 정문으로 맞춘 원광대·원광디지털대는 한 줄로 합친다.
    assert len(rows) == 2
    assert all(f.lh_alignment for f in result["university"].facilities)


@pytest.mark.asyncio
async def test_far_away_entries_leave_the_collection_untouched() -> None:
    # 저장소 등록부(전북) 항목은 판교 근처 수집을 건드리지 않는다.
    collector = AmenityCollector(kakao=hospital_kakao("전주고려병원"), tago=FakeTago([]))

    result = await collector.collect([], CENTER)

    facility = result["hospital"].facilities[0]
    assert facility.lh_alignment == ""
    assert facility.distance_m == pytest.approx(800, abs=2)


def test_cache_key_changes_with_the_alignments() -> None:
    a = AmenityCollector(kakao=FakeKakao(), tago=FakeTago([]), alignments=[entry(id="a")])
    b = AmenityCollector(kakao=FakeKakao(), tago=FakeTago([]), alignments=[entry(id="b")])
    assert a._cache_key([], CENTER, 3000) != b._cache_key([], CENTER, 3000)


# ---------------------------------------------------------------------------
# 1차 유해시설 제외 — 등록부 + 관리자 편집 목록
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rules_config, "RULES_PATH", tmp_path / "rule_overrides.json")
    monkeypatch.setattr(rules_config, "_cache", None)
    yield tmp_path


def test_hazard_exclusion_from_the_registry_checks_the_legal_dong(isolated_rules: Path) -> None:
    # 관리자 목록이 비어 있어도 등록부의 SK신흥주유소(효자동2가)는 빠진다.
    assert lh_excluded_reason("SK신흥주유소", "5211114100103630002")
    assert lh_excluded_reason("SK 신흥주유소(효자동)")
    # 다른 법정동의 같은 이름은 빼지 않는다.
    assert lh_excluded_reason("SK신흥주유소", "5211310700100010000") is None
    assert lh_excluded_reason("GS칼텍스주유소", "5211114100103630004") is None


def test_hazard_exclusion_still_reads_the_admin_list(isolated_rules: Path) -> None:
    rules_config.save_config(
        rules_config.RulesConfig(
            excluded_facilities=[rules_config.ExcludedFacility(name="철거주유소", reason="철거 확인")]
        )
    )
    assert lh_excluded_reason("철거주유소", "4100000000000000000") == "철거 확인"


# ---------------------------------------------------------------------------
# 설정 API — 읽기 전용 목록은 누구나 본다
# ---------------------------------------------------------------------------


def test_lh_alignments_api_is_public_and_read_only() -> None:
    import os

    os.environ["DEMO_MODE"] = "true"
    from fastapi.testclient import TestClient

    from app.main import app

    outsider = TestClient(app, client=("203.0.113.9", 51000))
    response = outsider.get("/api/settings/lh-alignments")
    assert response.status_code == 200
    body = response.json()
    ids = {e["id"] for e in body["entries"]}
    assert "univ-yesu-school-point" in ids and "hazard-sk-sinheung-exclude" in ids
    yesu = next(e for e in body["entries"] if e["id"] == "univ-yesu-school-point")
    assert yesu["group_label"] == "대학교"
    assert yesu["summary"] and yesu["action_label"]
    # 쓰기 경로는 없다.
    assert outsider.put("/api/settings/lh-alignments", json={}).status_code == 405
