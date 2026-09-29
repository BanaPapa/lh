"""2차 주거여건 문화시설 — LH 기준(인허가 3종)과 관리자 확장(카카오 CT1) 검증.

LH 최종 보고서의 문화시설은 행정안전부 인허가 공연장·박물관미술관·영화상영관뿐이다.
기본은 그 세 원장만 세고, culture_extended 를 켜면 카카오 CT1 을 겹치지 않게 더한다.
원장이 빠지면 카카오로 메우되 근사·경고로 드러낸다(조용히 넘어가지 않는다).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_config import OPTION_BY_KEY  # noqa: E402
from app.screening import amenities  # noqa: E402
from app.screening.amenities import BOUNDARY_GROUPS, AmenityCollector  # noqa: E402
from app.screening.culture import (  # noqa: E402
    CULTURE_DATASETS,
    CULTURE_EXTENDED_NOTE,
    CULTURE_KAKAO_COUNT_NOTE,
    CULTURE_LOCALDATA_SOURCE,
    CULTURE_STANDARD_NOTE,
    CULTURE_SUSPENDED_COUNT_NOTE,
    is_duplicate,
    plan_culture,
    venue_name,
)
from app.services.geo import offset_coordinates  # noqa: E402
from app.services.localdata import DATASET_BY_KEY, parse_record  # noqa: E402
from tests.test_screening_amenities import (  # noqa: E402
    CENTER,
    DisabledKakao,
    FakeKakao,
    FakeTago,
    culture_record,
    culture_store,
    place,
)


@pytest.fixture
def extended(monkeypatch):
    """관리자 설정 「문화시설 넓게 보기」를 켠다. 나머지 옵션은 기본값."""

    def fake(key: str) -> bool:
        if key == "culture_extended":
            return True
        return OPTION_BY_KEY[key].default

    monkeypatch.setattr(amenities, "option_enabled", fake)


@pytest.fixture(autouse=True)
def standard(monkeypatch):
    """기본은 LH 기준(끔). 실제 rule_overrides.json 값에 흔들리지 않게 고정한다."""

    monkeypatch.setattr(
        amenities, "option_enabled", lambda key: OPTION_BY_KEY[key].default
    )


def _collect(collector: AmenityCollector, rings=None):
    return asyncio.run(collector.collect(rings or [], CENTER, radius_m=3000))


def _records():
    return [
        culture_record("performance_halls", "서학예술극장", 290),
        culture_record("museums_and_art_galleries", "일제강점기 군산역사관", 493),
        # 영화상영관 원장은 관마다 한 행이다 — 한 극장으로 센다.
        culture_record("movie_theaters", "CGV 전주고사 1관", 800),
        culture_record("movie_theaters", "CGV 전주고사 2관", 800),
        culture_record("movie_theaters", "휴업극장", 1200, status="휴업"),
    ]


def _kakao():
    return FakeKakao(
        categories={
            "CT1": [
                # 인허가 서학예술극장과 같은 건물(10m) — 확장해도 중복으로 뺀다.
                place("서학 예술 극장 소극장", 300),
                # 좌표는 멀어도 이름이 같으면 같은 시설이다.
                place("일제강점기군산역사관", 2446),
                # LH 기준 밖 갤러리 — 확장 때만 더한다.
                place("동네갤러리", 150),
            ]
        }
    )


def test_culture_datasets_are_registered_localdata_sets() -> None:
    for key in CULTURE_DATASETS:
        assert key in DATASET_BY_KEY
    assert DATASET_BY_KEY["museums_and_art_galleries"].slug == "museums_and_art_galleries"
    assert DATASET_BY_KEY["movie_theaters"].slug == "movie_theaters"
    assert DATASET_BY_KEY["performance_halls"].slug == "performance_halls"
    # 경계(필지)에서 잰다.
    assert "culture" in BOUNDARY_GROUPS


def test_parse_record_takes_category_from_culture_business_type() -> None:
    row = {
        "BPLC_NM": "정읍시립미술관",
        "CRD_INFO_X": "187378.63038098",
        "CRD_INFO_Y": "228078.360118437",
        "SALS_STTS_CD": "01",
        "SALS_STTS_NM": "영업/정상",
        "CULTR_SPTS_TPBIZ_NM": "미술관",
        "MNG_NO": "B04022022000001",
    }
    record = parse_record(DATASET_BY_KEY["museums_and_art_galleries"], row)
    assert record is not None
    assert record.category == "미술관"
    # 폐업은 적재하지 않는다.
    closed = parse_record(
        DATASET_BY_KEY["movie_theaters"], {**row, "SALS_STTS_CD": "03"}
    )
    assert closed is None


def test_default_counts_only_lh_localdata_and_skips_kakao(tmp_path) -> None:
    kakao = _kakao()
    collector = AmenityCollector(
        kakao=kakao, tago=FakeTago([]), facility_store=culture_store(tmp_path, _records())
    )
    culture = _collect(collector)["culture"]

    assert culture.state == "connected"
    assert culture.note == CULTURE_STANDARD_NOTE
    assert culture.source_alert == ""
    assert culture.actual_source == CULTURE_LOCALDATA_SOURCE
    # LH 기준에서는 카카오 CT1 을 부르지도 않는다.
    assert "CT1" not in kakao.calls
    names = [f.name for f in culture.facilities]
    assert names == ["서학예술극장", "일제강점기 군산역사관", "CGV 전주고사", "휴업극장"]
    assert len(culture.distances_m) == 4
    # 휴업은 LH 데이터셋처럼 세되, 그 사실을 적는다.
    assert culture.facilities[-1].count_note == CULTURE_SUSPENDED_COUNT_NOTE
    assert culture.facilities[-1].counted


def test_extended_adds_kakao_without_duplicates(tmp_path, extended) -> None:
    kakao = _kakao()
    collector = AmenityCollector(
        kakao=kakao, tago=FakeTago([]), facility_store=culture_store(tmp_path, _records())
    )
    culture = _collect(collector)["culture"]

    assert "CT1" in kakao.calls
    assert culture.state == "connected"
    assert culture.note == CULTURE_EXTENDED_NOTE
    assert culture.source_alert == ""
    names = [f.name for f in culture.facilities]
    # 같은 건물·같은 이름의 카카오 결과는 빠지고 LH 기준 밖 갤러리만 더해진다.
    assert names == [
        "동네갤러리",
        "서학예술극장",
        "일제강점기 군산역사관",
        "CGV 전주고사",
        "휴업극장",
    ]
    assert culture.facilities[0].count_note == CULTURE_KAKAO_COUNT_NOTE
    assert "카카오" in culture.actual_source


def test_missing_datasets_fall_back_to_kakao_with_alert(tmp_path) -> None:
    from app.services.facility_store import FacilityStore

    kakao = _kakao()
    store = FacilityStore(db_path=tmp_path / "facilities.db")
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]), facility_store=store)
    culture = _collect(collector)["culture"]

    assert culture.state == "substituted"
    assert "카카오" in culture.note
    assert "재심사" in culture.source_alert
    assert "공연장" in culture.source_alert
    assert [f.name for f in culture.facilities][0] == "동네갤러리"


def test_partial_datasets_fill_the_gap_from_kakao(tmp_path) -> None:
    kakao = _kakao()
    store = culture_store(
        tmp_path,
        _records(),
        datasets=("performance_halls", "museums_and_art_galleries"),
    )
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]), facility_store=store)
    culture = _collect(collector)["culture"]

    assert culture.state == "substituted"
    # 빠진 원장(영화상영관)만 경고에 적는다.
    assert "영화상영관" in culture.source_alert
    assert "공연장" not in culture.source_alert
    names = [f.name for f in culture.facilities]
    assert "서학예술극장" in names and "동네갤러리" in names
    # 인허가와 겹치는 카카오 결과는 여전히 뺀다.
    assert "서학 예술 극장 소극장" not in names


def test_missing_datasets_without_kakao_is_missing_not_zero(tmp_path) -> None:
    from app.services.facility_store import FacilityStore

    store = FacilityStore(db_path=tmp_path / "facilities.db")
    collector = AmenityCollector(
        kakao=DisabledKakao(), tago=FakeTago([]), facility_store=store
    )
    culture = _collect(collector)["culture"]

    assert culture.state == "missing"
    assert culture.distances_m == ()
    assert culture.source_alert


def test_kakao_failure_in_extended_mode_keeps_localdata_and_alerts(
    tmp_path, extended
) -> None:
    kakao = FakeKakao(fail={"CT1"})
    collector = AmenityCollector(
        kakao=kakao, tago=FakeTago([]), facility_store=culture_store(tmp_path, _records())
    )
    culture = _collect(collector)["culture"]

    assert culture.state == "connected"
    assert [f.name for f in culture.facilities][0] == "서학예술극장"
    assert "실패" in culture.source_alert


def test_localdata_hits_are_measured_to_facility_boundary_line(tmp_path) -> None:
    half = 20.0
    ring = [
        offset_coordinates(CENTER, -half, -half),
        offset_coordinates(CENTER, -half, half),
        offset_coordinates(CENTER, half, half),
        offset_coordinates(CENTER, half, -half),
        offset_coordinates(CENTER, -half, -half),
    ]
    collector = AmenityCollector(
        kakao=FakeKakao(), tago=FakeTago([]), facility_store=culture_store(tmp_path, _records())
    )
    hit = _collect(collector, [ring])["culture"].facilities[0]
    assert hit.nearest_boundary_point is not None
    assert hit.distance_m == pytest.approx(270, abs=2)


def test_plan_and_name_helpers() -> None:
    assert plan_culture(CULTURE_DATASETS, False).use_kakao is False
    assert plan_culture(CULTURE_DATASETS, True).use_kakao is True
    assert plan_culture((), False).use_kakao is True
    assert plan_culture((), False).state == "substituted"
    assert venue_name("CGV 전주고사 3관") == "CGV 전주고사"
    assert venue_name("메가박스 전주(객사) 제2관") == "메가박스 전주(객사)"
    assert venue_name("5관") == "5관"
    point = offset_coordinates(CENTER, 0, 0)
    far = offset_coordinates(CENTER, 500, 0)
    assert is_duplicate("아무이름", point, [("다른이름", offset_coordinates(CENTER, 30, 0))])
    assert is_duplicate("전주 로이재즈 아트홀", far, [("전주로이재즈아트홀", point)])
    assert not is_duplicate("동네갤러리", far, [("전주로이재즈아트홀", point)])
