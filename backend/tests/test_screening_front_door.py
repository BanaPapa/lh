"""대학 캠퍼스 정문 기준점(국장님 「LOCUS 판정 로직 명세서 v1」 §3-2/§3-3) 회귀.

여기서 지키는 것은 다섯 가지다.
  1) 시설 측 3단 우선순위 — 정문 필지 > 정문 좌표 > 좌표 폴백.
  2) 캠퍼스 통필지(5만㎡ 이상)는 필지경계가 아니라 좌표로 폴백한다(축소 왜곡 방지).
  3) 정문 미지정 대학은 좌표로 폴백하되 그 사실을 결과에 남긴다.
  4) 자동 채택(정류장 근사)은 출처 라벨 없이 나가지 않는다.
  5) 수기 지정은 다음 검토(collect)부터 반영된다.

배점에 쓰는 거리(distances_m)와 화면 표기가 같은 계산에서 나오는지도 함께 본다.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.models import Coordinates
from app.screening.amenities import (
    FRONT_DOOR_PENDING_NOTICE,
    AmenityCollector,
    _distance_m,
)
from app.services.naver_search import NaverLocalPlace
from app.screening.front_door import (
    CAMPUS_PARCEL_MAX_AREA_M2,
    FrontDoorStore,
    auto_front_door,
    normalize_key,
)
from app.services.cadastral_local import LocalParcel
from app.services.geo import offset_coordinates


CENTER = Coordinates(lat=37.40111, lng=127.10853)


# ---------------------------------------------------------------------------
# 대역
# ---------------------------------------------------------------------------
def _square_ring(center: Coordinates, half_m: float) -> list[Coordinates]:
    """center 를 감싸는 2*half_m 변의 닫힌 사각 링."""

    return [
        offset_coordinates(center, -half_m, -half_m),
        offset_coordinates(center, -half_m, half_m),
        offset_coordinates(center, half_m, half_m),
        offset_coordinates(center, half_m, -half_m),
        offset_coordinates(center, -half_m, -half_m),
    ]


SITE_RING = _square_ring(CENTER, 20.0)


# 대학교 판별은 이름이 아니라 카카오 분류로 한다(amenities._is_university).
# 실제 응답 형식 그대로 쓴다 — 분류를 비워 두면 대학으로 잡히지 않는다.
UNIVERSITY_CATEGORY = "교육,학문 > 학교 > 대학교"


def _place(
    name: str,
    offset_m: float,
    category_name: str = UNIVERSITY_CATEGORY,
) -> dict[str, Any]:
    point = offset_coordinates(CENTER, offset_m, 0)
    return {
        "place_name": name,
        "address_name": f"{name} 주소",
        "road_address_name": "",
        "category_name": category_name,
        "x": str(point.lng),
        "y": str(point.lat),
    }


class FakeKakao:
    enabled = True

    def __init__(
        self,
        categories: dict[str, list[dict[str, Any]]] | None = None,
        keywords: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.categories = categories or {}
        self.keywords = keywords or {}

    async def search_category(self, code, lat, lng, radius_m, max_pages=3):
        return list(self.categories.get(code, []))

    async def search_keyword(self, keyword, lat, lng, radius_m, max_pages=3):
        return list(self.keywords.get(keyword, []))


class FakeTago:
    """정류장 원장 대역. rows 의 nodenm 이 정문 자동 채택 후보가 된다."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.enabled = True

    async def nearby_stops(self, lat, lng):
        return list(self.rows)


class FakeCadastral:
    """PNU → LocalParcel 조회 대역. 실제 지적도 인덱스 없이 필지 3단을 검증한다."""

    def __init__(self, parcels: dict[str, LocalParcel]) -> None:
        self.parcels = parcels

    def parcel_by_pnu(self, pnu: str) -> LocalParcel | None:
        return self.parcels.get(pnu)

    def representative_point(self, parcel: LocalParcel) -> Coordinates | None:
        lat = sum(c.lat for c in parcel.ring) / len(parcel.ring)
        lng = sum(c.lng for c in parcel.ring) / len(parcel.ring)
        return Coordinates(lat=lat, lng=lng)


def _parcel(pnu: str, center: Coordinates, half_m: float, area_m2: float) -> LocalParcel:
    return LocalParcel(
        pnu=pnu,
        jibun="완산구 서노송동 100",
        address="",
        ring=_square_ring(center, half_m),
        area_m2=area_m2,
        in_jeonbuk=True,
    )


UNIVERSITY_NAME = "전주대학교"
# 대학 대표점(카카오 place). 중심에서 북쪽 1km.
UNI_POINT_OFFSET = 1000.0


def _university_kakao() -> FakeKakao:
    return FakeKakao(keywords={"대학교": [_place(UNIVERSITY_NAME, UNI_POINT_OFFSET)]})


def _collect(collector: AmenityCollector) -> Any:
    return asyncio.run(collector.collect([SITE_RING], CENTER))


# ---------------------------------------------------------------------------
# front_door.py 단위 — 자동 채택과 저장소
# ---------------------------------------------------------------------------
class TestAutoFrontDoor:
    def test_matches_gate_stop_and_labels_the_source(self) -> None:
        gate = offset_coordinates(CENTER, 950, 0)
        ref = auto_front_door(
            UNIVERSITY_NAME,
            [("전주대학교정문", gate), ("시청앞", CENTER)],
        )
        assert ref is not None
        assert ref.origin == "auto_stop"
        # 근사값이므로 출처 라벨 없이 나가면 안 된다(§3-3).
        assert ref.source_label
        assert "정류장 원장 근사" in ref.source_label
        assert ref.coordinates == gate

    def test_short_form_token_matches(self) -> None:
        # '전주대학교' 의 축약형 '전주대' 로도 정류장명을 잡는다.
        ref = auto_front_door(UNIVERSITY_NAME, [("전주대입구", CENTER)])
        assert ref is not None

    def test_hospital_annex_stop_is_excluded(self) -> None:
        # 부속 병원 정류장이 대학 토큰에 걸려 캠퍼스 정문으로 오인되면 안 된다.
        ref = auto_front_door("전북대학교", [("전북대학병원입구", CENTER)])
        assert ref is None

    def test_no_gate_stop_returns_none(self) -> None:
        assert auto_front_door(UNIVERSITY_NAME, [("어딘가정류장", CENTER)]) is None


class TestFrontDoorStore:
    def test_normalize_key_folds_whitespace(self) -> None:
        assert normalize_key("전주 대학교") == normalize_key("전주대학교")

    def test_designation_persists_across_reloads(self, tmp_path) -> None:
        path = tmp_path / "front_doors.json"
        store = FrontDoorStore(path=path)
        store.designate(UNIVERSITY_NAME, pnu="P1", source_label="지적도 클릭")

        reopened = FrontDoorStore(path=path)
        ref = reopened.get(UNIVERSITY_NAME)
        assert ref is not None
        assert ref.pnu == "P1"
        assert ref.origin == "manual"

    def test_revision_increments_on_designate_and_remove(self, tmp_path) -> None:
        store = FrontDoorStore(path=tmp_path / "fd.json")
        base = store.revision()
        store.designate(UNIVERSITY_NAME, pnu="P1")
        assert store.revision() == base + 1
        assert store.remove(UNIVERSITY_NAME) is True
        assert store.revision() == base + 2
        assert store.remove(UNIVERSITY_NAME) is False

    def test_corrupt_file_is_treated_as_empty(self, tmp_path) -> None:
        path = tmp_path / "fd.json"
        path.write_text("{ not json", encoding="utf-8")
        store = FrontDoorStore(path=path)
        assert store.all() == {}

    def test_save_failure_does_not_break_designation(self, tmp_path) -> None:
        # 경로가 디렉터리면 파일 쓰기가 실패한다. 그래도 판정은 죽지 않아야 하므로
        # 지정은 메모리에 반영되고 예외가 올라오지 않는다(§3-3 저장 안전장치).
        as_dir = tmp_path / "front_doors.json"
        as_dir.mkdir()
        store = FrontDoorStore(path=as_dir)
        ref = store.designate(UNIVERSITY_NAME, pnu="P1")
        assert ref.pnu == "P1"
        assert store.get(UNIVERSITY_NAME) is not None


# ---------------------------------------------------------------------------
# 시설 측 3단 — collect 를 통과한 실제 측정
# ---------------------------------------------------------------------------
class TestThreeTierMeasurement:
    def test_unassigned_campus_falls_back_to_coordinate_and_says_so(self) -> None:
        # 정문 미지정: 좌표(대표점)로 폴백하되 지정 대기 사실을 감추지 않는다(§3-3).
        collector = AmenityCollector(
            kakao=_university_kakao(),
            tago=FakeTago([]),
            front_door_store=None,
        )
        result = _collect(collector)
        uni = result["university"]
        facility = uni.facilities[0]
        assert facility.measurement_tier == "coordinate"
        assert facility.front_door_notice == FRONT_DOOR_PENDING_NOTICE
        # 시설군 헤더에도 폴백 사실이 드러난다.
        assert FRONT_DOOR_PENDING_NOTICE in uni.front_door_notice

    def test_auto_stop_adoption_carries_source_label(self, tmp_path) -> None:
        gate = offset_coordinates(CENTER, 950, 0)
        collector = AmenityCollector(
            kakao=_university_kakao(),
            tago=FakeTago([{"gpslati": gate.lat, "gpslong": gate.lng, "nodenm": "전주대학교정문"}]),
            front_door_store=FrontDoorStore(path=tmp_path / "fd.json"),
        )
        result = _collect(collector)
        facility = result["university"].facilities[0]
        # 좌표만 있는 자동 채택은 정문 좌표 단(front_door_point)으로 잰다.
        assert facility.measurement_tier == "front_door_point"
        # 근사 출처 라벨이 반드시 실린다.
        assert "정류장 원장 근사" in facility.front_door_source

    def test_manual_parcel_designation_is_measured_from_parcel_boundary(self, tmp_path) -> None:
        # 1순위: 정문 필지 지정 → 대지경계 ↔ 정문 필지경계.
        gate_center = offset_coordinates(CENTER, 500, 0)
        cadastral = FakeCadastral({"P1": _parcel("P1", gate_center, 15.0, area_m2=900.0)})
        store = FrontDoorStore(path=tmp_path / "fd.json")
        store.designate(UNIVERSITY_NAME, pnu="P1")
        collector = AmenityCollector(
            kakao=_university_kakao(),
            tago=FakeTago([]),
            front_door_store=store,
            cadastral_store=cadastral,
        )
        result = _collect(collector)
        facility = result["university"].facilities[0]
        assert facility.measurement_tier == "front_door_parcel"
        assert "정문 필지경계" in facility.measurement_label
        assert "P1" in facility.front_door_notice
        # 배점 거리와 표기가 같은 계산: 이 시설의 거리가 시설군 거리목록에 그대로 있다.
        assert facility.distance_m in result["university"].distances_m

    def test_manual_designation_wins_over_auto_stop(self, tmp_path) -> None:
        # 수기 지정(필지)이 자동 채택(정류장)보다 우선한다(§3-3).
        gate_center = offset_coordinates(CENTER, 500, 0)
        cadastral = FakeCadastral({"P1": _parcel("P1", gate_center, 15.0, area_m2=900.0)})
        store = FrontDoorStore(path=tmp_path / "fd.json")
        store.designate(UNIVERSITY_NAME, pnu="P1", source_label="운영자 수기 지정")
        collector = AmenityCollector(
            kakao=_university_kakao(),
            tago=FakeTago(
                [{"gpslati": CENTER.lat, "gpslong": CENTER.lng, "nodenm": "전주대학교정문"}]
            ),
            front_door_store=store,
            cadastral_store=cadastral,
        )
        result = _collect(collector)
        facility = result["university"].facilities[0]
        assert facility.measurement_tier == "front_door_parcel"
        assert facility.front_door_source == "운영자 수기 지정"


class TestCampusParcelSafeguard:
    def test_large_parcel_falls_back_to_coordinate(self, tmp_path) -> None:
        # 5만㎡ 이상 통필지는 필지경계가 아니라 좌표로 폴백한다(축소 왜곡 방지).
        gate_center = offset_coordinates(CENTER, 800, 0)
        big = _parcel("BIG", gate_center, 150.0, area_m2=CAMPUS_PARCEL_MAX_AREA_M2 + 10_000)
        cadastral = FakeCadastral({"BIG": big})
        store = FrontDoorStore(path=tmp_path / "fd.json")
        # 정문 클릭 좌표를 함께 지정 → 폴백은 이 점을 쓴다.
        store.designate(
            UNIVERSITY_NAME, pnu="BIG", lat=gate_center.lat, lng=gate_center.lng
        )
        collector = AmenityCollector(
            kakao=_university_kakao(),
            tago=FakeTago([]),
            front_door_store=store,
            cadastral_store=cadastral,
        )
        result = _collect(collector)
        facility = result["university"].facilities[0]
        assert facility.measurement_tier == "front_door_point"
        assert "통필지" in facility.front_door_notice

    def test_threshold_value_is_the_directors_50000(self) -> None:
        # 국장님 §3-3 값(5만㎡)을 그대로 쓴다. 임의로 낮춰 판정을 느슨하게 하지 않는다.
        assert CAMPUS_PARCEL_MAX_AREA_M2 == 50_000.0

    def test_large_parcel_without_gate_coordinate_is_not_confirmed_via_representative_point(
        self, tmp_path
    ) -> None:
        # 회귀: 5만㎡ 이상 통필지에 PNU 만 지정되고 검증된 정문 좌표가 없으면,
        # 필지 내부 임의 대표점으로 거리를 확정해서는 안 된다(정문이 반대편이면
        # 거리가 실제보다 짧아지는 왜곡이 재발한다). 시설 좌표로 보수적으로
        # 폴백하고 대기 상태임을 고지해야 한다.
        gate_center = offset_coordinates(CENTER, 800, 0)
        big = _parcel("BIG", gate_center, 150.0, area_m2=CAMPUS_PARCEL_MAX_AREA_M2 + 10_000)
        cadastral = FakeCadastral({"BIG": big})
        store = FrontDoorStore(path=tmp_path / "fd.json")
        # 좌표 없이 PNU 만 지정 — 정문 클릭 좌표가 없다.
        store.designate(UNIVERSITY_NAME, pnu="BIG")
        collector = AmenityCollector(
            kakao=_university_kakao(),
            tago=FakeTago([]),
            front_door_store=store,
            cadastral_store=cadastral,
        )
        result = _collect(collector)
        facility = result["university"].facilities[0]
        # 정문 필지경계도, 필지 대표점(front_door_point)도 아니어야 한다.
        assert facility.measurement_tier == "coordinate"
        assert facility.measurement_tier != "front_door_parcel"
        # 대기 상태와 사유가 화면에 드러나야 한다.
        assert "정문 좌표" in facility.front_door_notice
        assert "통필지" in facility.front_door_notice
        # 거리는 대표점이 아니라 시설(카카오 place) 좌표 기준으로, 확정하지 않은
        # 보수적 폴백이다.
        place_coords = offset_coordinates(CENTER, UNI_POINT_OFFSET, 0)
        assert facility.coordinates == place_coords
        assert facility.distance_m == _distance_m(place_coords, [SITE_RING], CENTER)


class TestDesignationReflectedInNextReview:
    def test_new_designation_changes_the_next_collect(self, tmp_path) -> None:
        gate_center = offset_coordinates(CENTER, 500, 0)
        cadastral = FakeCadastral({"P1": _parcel("P1", gate_center, 15.0, area_m2=900.0)})
        store = FrontDoorStore(path=tmp_path / "fd.json")
        collector = AmenityCollector(
            kakao=_university_kakao(),
            tago=FakeTago([]),
            front_door_store=store,
            cadastral_store=cadastral,
        )

        before = _collect(collector)["university"].facilities[0]
        assert before.measurement_tier == "coordinate"

        # 운영자가 정문 필지를 지정한다 → 저장소 리비전이 오르고 캐시 키가 바뀐다.
        store.designate(UNIVERSITY_NAME, pnu="P1")

        after = _collect(collector)["university"].facilities[0]
        assert after.measurement_tier == "front_door_parcel"


# ---------------------------------------------------------------------------
# 네이버 문 후보(#7·#11) · 종합병원 정문(#8) — 2026-09-11 LH 결정
# ---------------------------------------------------------------------------
HOSPITAL_CATEGORY = "의료,건강 > 병원 > 종합병원"


def _hospital_place(name: str, offset_m: float) -> dict[str, Any]:
    point = offset_coordinates(CENTER, offset_m, 0)
    return {
        "place_name": name,
        "address_name": f"{name} 주소",
        "road_address_name": "",
        "category_name": HOSPITAL_CATEGORY,
        "x": str(point.lng),
        "y": str(point.lat),
    }


class FakeNaver:
    """네이버 지역검색 대역. 질의 문자열 → NaverLocalPlace 목록."""

    def __init__(self, responses: dict[str, list[NaverLocalPlace]]) -> None:
        self.responses = responses
        self.enabled = True
        self.queries: list[str] = []

    async def local(self, query: str, display: int = 5) -> list[NaverLocalPlace]:
        self.queries.append(query)
        return list(self.responses.get(query, []))


def _local(name: str, offset_m: float) -> NaverLocalPlace:
    return NaverLocalPlace(
        name=name,
        category="",
        address="",
        road_address="",
        coordinates=offset_coordinates(CENTER, offset_m, 0),
    )


class TestNaverDoorCandidates:
    def test_university_collects_all_doors_and_selects_nearest(self) -> None:
        # 정문·후문을 전부 후보로 모으고, 기본은 사업지에 가장 가까운 문을 쓴다(#7).
        naver = FakeNaver(
            {
                "전주대학교 정문": [
                    _local("전주대학교 정문", 300.0),
                    _local("전주대학교 후문", 700.0),
                ]
            }
        )
        collector = AmenityCollector(
            kakao=_university_kakao(),
            tago=FakeTago([]),
            naver=naver,
        )
        facility = _collect(collector)["university"].facilities[0]
        assert facility.measurement_tier == "front_door_point"
        assert "네이버 지역검색" in facility.front_door_source
        labels = {c.label for c in facility.front_door_candidates}
        assert labels == {"전주대학교 정문", "전주대학교 후문"}
        selected = [c for c in facility.front_door_candidates if c.selected]
        assert len(selected) == 1
        assert selected[0].label == "전주대학교 정문"  # 더 가까운 문
        # 최단거리선(#5): 시설 측 기준점이 실린다.
        assert facility.nearest_facility_point is not None
        # 사업지 rings 가 있으면 경계 위 점도 실린다.
        assert facility.nearest_boundary_point is not None

    def test_single_door_has_one_candidate(self) -> None:
        naver = FakeNaver({"전주대학교 정문": [_local("전주대학교 정문", 300.0)]})
        collector = AmenityCollector(
            kakao=_university_kakao(), tago=FakeTago([]), naver=naver
        )
        facility = _collect(collector)["university"].facilities[0]
        assert len(facility.front_door_candidates) == 1
        assert facility.front_door_candidates[0].selected is True


class TestHospitalFrontDoor:
    def _hospital_kakao(self) -> FakeKakao:
        return FakeKakao(categories={"HP8": [_hospital_place("전주사랑종합병원", 500.0)]})

    def test_hospital_ignores_front_doors_and_measures_by_parcel_boundary(self) -> None:
        # LH 09/22 결정 2: 종합병원은 필지 경계 기준. 네이버에 정문이 있어도 묻지 않는다
        # (경계 원천이 없는 테스트 환경에서는 좌표로 남고, 정문 후보는 비어 있다).
        naver = FakeNaver(
            {"전주사랑종합병원 정문": [_local("전주사랑종합병원 정문", 400.0)]}
        )
        collector = AmenityCollector(
            kakao=self._hospital_kakao(), tago=FakeTago([]), naver=naver
        )
        hospital = _collect(collector)["hospital"]
        facility = hospital.facilities[0]
        assert facility.measurement_tier == "coordinate"
        assert facility.front_door_candidates == ()
        assert facility.front_door_source == ""
        assert hospital.front_door_notice == ""


class TestStationExitCandidates:
    def test_railway_exits_become_candidates(self) -> None:
        # #11: 역 출구가 여럿이면 전부 후보로 싣고 최단 출구를 기본으로 쓴다.
        kakao = FakeKakao(keywords={"기차역": [_place("성남역", 400.0, "")]})
        naver = FakeNaver(
            {
                "성남역 출구": [
                    _local("성남역 1번출구", 380.0),
                    _local("성남역 2번출구", 420.0),
                ]
            }
        )
        collector = AmenityCollector(kakao=kakao, tago=FakeTago([]), naver=naver)
        facility = _collect(collector)["railway"].facilities[0]
        labels = {c.label for c in facility.front_door_candidates}
        assert labels == {"성남역 1번출구", "성남역 2번출구"}
        selected = [c for c in facility.front_door_candidates if c.selected]
        assert len(selected) == 1
        assert selected[0].label == "성남역 1번출구"


class TestStationManualDesignation:
    """역·터미널 수기 기준점 지정(#11 「출구 여럿이면 담당자 선택」)."""

    def test_manual_coordinate_designation_moves_the_station_distance(
        self, tmp_path
    ) -> None:
        # 담당자가 좌표를 지정하면 역 거리는 그 기준점으로 다시 잰다.
        kakao = FakeKakao(keywords={"기차역": [_place("성남역", 400.0, "")]})
        store = FrontDoorStore(path=tmp_path / "fd.json")
        # 사업지에서 역 대표점(북쪽 400m)보다 먼 지점(북쪽 900m)을 기준점으로 지정.
        gate = offset_coordinates(CENTER, 900.0, 0)
        store.designate(
            "성남역", lat=gate.lat, lng=gate.lng, source_label="운영자 수기 지정"
        )
        collector = AmenityCollector(
            kakao=kakao, tago=FakeTago([]), front_door_store=store
        )

        facility = _collect(collector)["railway"].facilities[0]

        assert facility.distance_m == pytest.approx(
            _distance_m(gate, [SITE_RING], CENTER)
        )
        assert facility.measurement_tier == "front_door_point"
        assert "수기 지정" in facility.front_door_source
        assert facility.nearest_facility_point == gate

    def test_without_designation_station_uses_the_representative_point(
        self, tmp_path
    ) -> None:
        kakao = FakeKakao(keywords={"기차역": [_place("성남역", 400.0, "")]})
        store = FrontDoorStore(path=tmp_path / "fd.json")
        collector = AmenityCollector(
            kakao=kakao, tago=FakeTago([]), front_door_store=store
        )

        facility = _collect(collector)["railway"].facilities[0]

        rep = offset_coordinates(CENTER, 400.0, 0)
        assert facility.distance_m == pytest.approx(
            _distance_m(rep, [SITE_RING], CENTER)
        )
        assert facility.measurement_tier == "coordinate"

    def test_terminal_also_honours_manual_designation(self, tmp_path) -> None:
        # 터미널은 출구 후보가 없지만 수기 지정은 동일하게 반영된다.
        kakao = FakeKakao(keywords={"버스터미널": [_place("전주시외버스터미널", 300.0, "")]})
        store = FrontDoorStore(path=tmp_path / "fd.json")
        gate = offset_coordinates(CENTER, 800.0, 0)
        store.designate(
            "전주시외버스터미널",
            lat=gate.lat,
            lng=gate.lng,
            source_label="운영자 수기 지정",
        )
        collector = AmenityCollector(
            kakao=kakao, tago=FakeTago([]), front_door_store=store
        )

        facility = _collect(collector)["terminal"].facilities[0]

        assert facility.distance_m == pytest.approx(
            _distance_m(gate, [SITE_RING], CENTER)
        )
        assert facility.measurement_tier == "front_door_point"
        assert "수기 지정" in facility.front_door_source


class TestHospitalFrontDoorCacheKey:
    """종합병원 정문 후보 캐시 키는 이름 그대로여야 한다(대학 키로 오인 대체 금지)."""

    def test_hospital_is_not_matched_to_a_university_front_door(self) -> None:
        # 「전북대학교병원」이 「전북대학교」 정문 후보로 대체되면 안 된다.
        kakao = FakeKakao(
            categories={"HP8": [_hospital_place("전북대학교병원", 500.0)]}
        )
        collector = AmenityCollector(kakao=kakao, tago=FakeTago([]))  # naver 없음
        # 같은 반경에 대학 「전북대학교」 정문 후보가 캐시돼 있다고 가정한다.
        gate = offset_coordinates(CENTER, 100.0, 0)
        collector._front_door_candidates[normalize_key("전북대학교")] = (
            ("전북대학교 정문", gate),
        )

        facility = _collect(collector)["hospital"].facilities[0]

        # 병원은 이름 그대로(정규화만) 찾으므로 후보가 없어 좌표로 폴백한다.
        assert facility.front_door_candidates == ()
        assert facility.measurement_tier == "coordinate"
