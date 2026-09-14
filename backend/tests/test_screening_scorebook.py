"""2차 생활편의성 배점과 1차 '통과 처리' 정책 검증.

심사표 등급은 한 칸만 틀려도 점수가 통째로 달라지므로, 공고문 예시 조건을
그대로 세워 놓고 점수를 못 박는다.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.hazard_review.models import (
    HazardCategorySummary,
    HazardParcel,
    HazardReviewRequest,
    HazardReviewResult,
    HazardSite,
)
from app.hazard_review.rulebook import RULE_PACK_ID, STATUS_LABELS
from app.hazard_review.service import get_rule_pack
from app.models import Coordinates
from app.screening.amenities import CollectedFacility, GroupCollection
from app.screening.models import ScreeningRequest
from app.screening.scorebook import FACILITY_GROUPS, sheet_for
from app.screening.service import ScreeningService, _build_stage_two


SITE_CENTER = Coordinates(lat=37.40111, lng=127.10853)


# ---------------------------------------------------------------------------
# 공용 도구
# ---------------------------------------------------------------------------
def group(
    key: str,
    distances: list[float],
    state: str = "connected",
    note: str = "",
) -> GroupCollection:
    facilities = tuple(
        CollectedFacility(
            name=f"{key}-{index}",
            address="테스트 주소",
            coordinates=SITE_CENTER,
            distance_m=distance,
            source_label="테스트 원천",
        )
        for index, distance in enumerate(distances)
    )
    return GroupCollection(
        key=key,
        state=state,
        note=note,
        actual_source="테스트 원천",
        distances_m=tuple(distances),
        facilities=facilities,
    )


def collections(**overrides: GroupCollection) -> dict[str, GroupCollection]:
    """모든 시설군을 '조회했으나 없음'으로 깔고 필요한 것만 덮어쓴다."""

    base = {item.key: group(item.key, []) for item in FACILITY_GROUPS}
    base.update(overrides)
    return base


def stage_two(application_type: str, **overrides: GroupCollection):
    return _build_stage_two(
        application_type,
        collections(**overrides),
        measurement="테스트 측정",
        reference_only=False,
    )


def criterion(stage, key: str):
    return next(item for item in stage.criteria if item.key == key)


# 대중교통 최고 등급 조건 — 반경 0.5km 이내 교통시설 둘 이상.
TWO_TRANSIT = {
    "subway": group("subway", [400.0]),
    "bus_stop": group("bus_stop", [300.0], state="substituted", note="정류장 근사"),
}


def site(application_type: str = "general") -> HazardSite:
    return HazardSite(
        name="검증 사업지",
        address="경기도 성남시 분당구 판교역로 235",
        coordinates=SITE_CENTER,
        housing_type="house",
        application_type=application_type,  # type: ignore[arg-type]
        parcels=[HazardParcel(parcel_id="prototype-parcel")],
    )


# ---------------------------------------------------------------------------
# 심사표 배점
# ---------------------------------------------------------------------------
def test_common_sheet_scores_full_forty_points() -> None:
    stage = stage_two(
        "general",
        **TWO_TRANSIT,
        retail=group("retail", [1500.0]),
        hospital=group("hospital", [1800.0]),
        park=group("park", [1000.0]),
        culture=group("culture", [1200.0]),
        school_elementary=group("school_elementary", [400.0]),
        school_middle=group("school_middle", [450.0]),
        school_high=group("school_high", [900.0]),
    )

    assert criterion(stage, "transit").awarded == 15
    assert criterion(stage, "living").awarded == 15
    assert criterion(stage, "education").awarded == 10
    assert stage.determined is True
    assert stage.living_score == 40
    assert stage.living_maximum == 40


def test_common_sheet_marks_selected_tier() -> None:
    stage = stage_two("general", **TWO_TRANSIT)
    transit = criterion(stage, "transit")

    selected = [tier for tier in transit.tiers if tier.selected]
    assert [tier.points for tier in selected] == [15]
    assert transit.tier_condition == "반경 0.5km 이내 교통시설 둘 이상"


def test_youth_sheet_transit_twenty_and_university_four() -> None:
    stage = stage_two(
        "youth",
        **TWO_TRANSIT,
        university=group("university", [1200.0], state="substituted", note="대표점 근사"),
    )

    assert stage.sheet_key == "youth"
    assert criterion(stage, "transit").awarded == 20
    # 1.2km 는 1km 등급(5점)에 못 미치고 1.5km 등급(4점)에 걸린다.
    assert criterion(stage, "education").awarded == 4


def test_senior_sheet_has_no_education_criterion() -> None:
    sheet = sheet_for("senior")

    assert [item.key for item in sheet.criteria] == ["transit", "living"]
    assert len(sheet.criteria) == 2
    assert sheet.living_maximum == 40

    stage = stage_two("senior", **TWO_TRANSIT)
    assert [item.key for item in stage.criteria] == ["transit", "living"]
    assert criterion(stage, "transit").awarded == 20


def test_station_area_bonus_uses_bonus_criterion() -> None:
    stage = stage_two("general", **TWO_TRANSIT)

    assert stage.bonus is not None
    assert stage.bonus.key == "station_area"
    # 버스정류장은 역세권 가점 대상이 아니지만 지하철역 400m 가 걸린다.
    assert stage.bonus.awarded == 5
    # 가점은 생활편의성 40점 합계에 들어가지 않는다.
    # 교통 15점 + 주거여건 최하 3점 + 교육여건 최하 2점 = 20점.
    assert stage.living_score == 20


def test_out_of_scope_items_are_reported() -> None:
    stage = stage_two("general", **TWO_TRANSIT)

    assert stage.out_of_scope_points == 65
    assert stage.total_sheet_points == 100
    assert stage.pass_threshold == 70
    assert any(item.label == "공가율" for item in stage.out_of_scope)


# ---------------------------------------------------------------------------
# 미확보 시설군 — 확정 불가
# ---------------------------------------------------------------------------
def test_missing_group_leaves_criterion_undetermined() -> None:
    stage = stage_two(
        "general",
        **TWO_TRANSIT,
        retail=group("retail", [1500.0]),
        hospital=group("hospital", [1800.0]),
        culture=group("culture", [1200.0]),
        park=group("park", [], state="missing", note="도시공원정보 미연결"),
    )
    living = criterion(stage, "living")

    assert living.determined is False
    assert living.awarded is None
    assert living.awarded_min == 12
    assert living.awarded_max == 15
    assert living.awarded_min < living.awarded_max
    assert "공원" in living.note
    assert stage.determined is False
    assert stage.living_score is None
    assert stage.living_score_min < stage.living_score_max


def test_missing_group_status_is_visible_on_criterion() -> None:
    stage = stage_two(
        "general",
        park=group("park", [], state="missing", note="도시공원정보 미연결"),
    )
    living = criterion(stage, "living")
    park = next(item for item in living.groups if item.key == "park")

    assert park.state == "missing"
    assert park.state_label == "데이터 미확보"
    assert park.note == "도시공원정보 미연결"
    assert park.designated_source == "도시공원정보 표준데이터"


def test_substituted_group_keeps_designated_source_visible() -> None:
    stage = stage_two("general", **TWO_TRANSIT)
    transit = criterion(stage, "transit")
    bus = next(item for item in transit.groups if item.key == "bus_stop")

    assert bus.state == "substituted"
    assert bus.state_label == "대체 원천"
    assert bus.note == "정류장 근사"
    assert bus.designated_source == "운행주기 15분 이내인 정류장만 인정"
    assert bus.count == 1
    assert bus.nearest_distance_m == 300.0
    assert bus.hits[0].measurement_method == "테스트 측정"

    subway = next(item for item in transit.groups if item.key == "subway")
    # 출입구가 아니라 역 대표점으로 잰 사실이 시설 한 줄마다 남아야 한다.
    assert subway.point_exception == "최단거리 출입구"
    assert "최단거리 출입구" in subway.hits[0].measurement_method


# ---------------------------------------------------------------------------
# 1차 매입제외 — 통과 처리 정책
# ---------------------------------------------------------------------------
def category(
    key: str,
    status: str,
    *,
    label: str | None = None,
    threshold_m: int | None = 50,
    nearest: float | None = None,
    note: str = "",
    implementation_note: str = "",
    judgment_excluded: bool = False,
    manual_check_required: bool = False,
    not_applicable_reason: str = "",
) -> HazardCategorySummary:
    return HazardCategorySummary(
        key=key,
        label=label or key,
        rule_id="R-HAZMAT",
        rule_label="위험물 저장·처리시설",
        threshold_m=threshold_m,
        status=status,  # type: ignore[arg-type]
        status_label=STATUS_LABELS[status],
        data_state="applied",
        note=note,
        implementation_note=implementation_note,
        judgment_excluded=judgment_excluded,
        manual_check_required=manual_check_required,
        not_applicable_reason=not_applicable_reason,
        nearest_distance_m=nearest,
    )


class FakeHazardService:
    """유해요소 판정 엔진 대역. 카테고리 목록만 그대로 돌려준다."""

    def __init__(self, categories: list[HazardCategorySummary]) -> None:
        self.categories = categories
        self.calls = 0

    async def review(self, request: HazardReviewRequest, progress, cancel_event):
        self.calls += 1
        await progress("HAZMAT", "위험물", "running", 50, None, "후보를 조회합니다.")
        pack = get_rule_pack(request.rule_pack_id)
        assert pack is not None
        return HazardReviewResult(
            review_id="test-review",
            created_at=datetime.now(UTC),
            demo=False,
            site=request.site,
            housing_type=request.site.housing_type,
            application_type=request.site.application_type,
            rule_pack=pack,
            overall_status="no_conflict_in_snapshot",
            overall_label="스냅샷 내 충돌 없음",
            overall_summary="테스트 결과",
            status_counts={},
            data_completeness=50,
            boundary_coverage=50,
            source_freshness=50,
            findings=[],
            categories=self.categories,
            sources=[],
            source_snapshot_id="test-snapshot",
            calculation_note="테스트",
            disclaimer="테스트",
        )


class FakeAmenityCollector:
    def __init__(self, items: dict[str, GroupCollection]) -> None:
        self.items = items

    async def collect(self, rings, center, radius_m=3000):
        return self.items


def build_service(categories: list[HazardCategorySummary]) -> ScreeningService:
    return ScreeningService(
        hazard=FakeHazardService(categories),  # type: ignore[arg-type]
        amenities=FakeAmenityCollector(collections(**TWO_TRANSIT)),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_dataset_missing_category_passes_with_reason() -> None:
    service = build_service([category("hazmat", "dataset_missing", label="위험물 저장·처리시설")])

    result = await service.screen(ScreeningRequest(site=site()))
    item = result.stage_one.items[0]

    assert item.outcome == "pass"
    assert item.outcome_label == "통과"
    assert item.passthrough is True
    assert item.effect == "advisory"
    assert item.reason == "판정 미적용 — 통과 처리"
    # 왜 통과 처리했는지 비어 있으면 사용자는 '통과'만 읽는다.
    assert item.note != ""
    assert result.verdict == "pass"
    assert result.stage_one.passthrough_notes == [f"위험물 저장·처리시설 — {item.note}"]


@pytest.mark.asyncio
async def test_passthrough_variants_all_pass_with_notes() -> None:
    service = build_service(
        [
            category("a", "dataset_missing", note="원천 미연결"),
            category("b", "geometry_missing", implementation_note="경계 미확보"),
            category("c", "no_conflict_in_snapshot", judgment_excluded=True),
            category("d", "review_required", manual_check_required=True, note="수기"),
        ]
    )

    result = await service.screen(ScreeningRequest(site=site()))
    by_key = {item.key: item for item in result.stage_one.items}

    assert [item.outcome for item in result.stage_one.items] == ["pass"] * 4
    assert all(item.passthrough for item in result.stage_one.items)
    assert all(item.note for item in result.stage_one.items)
    assert by_key["b"].reason == "경계 미확보 — 통과 처리"
    assert by_key["c"].reason == "협의에 따라 판정 제외 — 통과 처리"
    assert by_key["d"].reason == "별도 수기 확인 대상 — 통과 처리"
    assert result.verdict == "pass"
    assert result.verdict_summary == "1차 매입제외 항목에서 확정 저촉이 없습니다."


@pytest.mark.asyncio
async def test_exclusion_match_fails_and_names_the_reason() -> None:
    service = build_service(
        [
            category(
                "hazmat",
                "exclusion_match",
                label="위험물 저장·처리시설",
                nearest=32.4,
                threshold_m=50,
            ),
            category("other", "dataset_missing"),
        ]
    )

    result = await service.screen(ScreeningRequest(site=site()))

    assert result.verdict == "fail"
    assert result.verdict_label == "부적격 (매입제외)"
    assert result.stage_one.reasons == ["위험물 저장·처리시설 32m — 기준거리 50m 이내"]
    assert result.verdict_summary == (
        "위험물 저장·처리시설이 기준거리 50m 이내에 있어 매입제외 대상입니다."
    )
    # 부적격이어도 2차는 참고값으로 계속 낸다.
    assert result.stage_two.reference_only is True
    assert result.stage_two.living_score_min > 0
    assert result.stage_one.counts == {"fail": 1, "pass": 1}


@pytest.mark.asyncio
async def test_review_required_and_not_applicable_are_not_failures() -> None:
    service = build_service(
        [
            category("review", "review_required", note="조건 확인"),
            category("na", "not_applicable", threshold_m=None, not_applicable_reason="미적용"),
        ]
    )

    result = await service.screen(ScreeningRequest(site=site()))
    by_key = {item.key: item for item in result.stage_one.items}

    assert result.verdict == "review"
    assert result.stage_one.review_reasons == ["조건 확인"]
    assert by_key["na"].outcome == "not_applicable"
    assert by_key["na"].effect == "not_applicable"
    assert by_key["na"].passthrough is False


@pytest.mark.asyncio
async def test_screen_reports_progress_for_four_stages() -> None:
    # 1차 조회 → 1차 판정 → 2차 조회 → 2차 배점. 단계 이름이 1차·2차 모두
    # 「조회 / 판정·배점」 짝으로 맞아야 진행 화면에서 한눈에 읽힌다.
    service = build_service([category("a", "dataset_missing")])
    seen: list[tuple[str, str, str]] = []

    async def progress(item_id, label, status, item_progress, count, message):
        seen.append((item_id, label, status))

    await service.screen(ScreeningRequest(site=site()), progress)

    completed = [(i, l) for i, l, s in seen if s == "completed"]
    assert completed == [
        ("STAGE1_COLLECT", "1차 유해시설 조회"),
        ("STAGE1_JUDGE", "1차 매입제외 판정"),
        ("STAGE2_COLLECT", "2차 생활편의시설 조회"),
        ("STAGE2_SCORE", "2차 생활편의성 배점"),
    ]


@pytest.mark.asyncio
async def test_screen_carries_rule_pack_and_disclaimer() -> None:
    service = build_service([category("a", "dataset_missing")])

    result = await service.screen(ScreeningRequest(site=site()))

    assert result.rule_pack_id == RULE_PACK_ID
    assert "70점" in result.disclaimer
    assert "대지경계" in result.calculation_note or "주소점" in result.calculation_note
    assert result.hazard_review.review_id == "test-review"
