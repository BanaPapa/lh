"""LH 서류심사 엔진 — 1차 매입제외 판정과 2차 생활편의성 배점을 한 장으로 합친다.

두 가지 원칙만 지킨다.
  1) 확인되지 않은 조건으로 부적격을 확정하지 않는다. 원천이 없어 판정하지 못한
     항목은 '통과 처리'하고, 왜 통과 처리했는지 한국어로 남긴다.
  2) 미확보 시설군이 있으면 배점을 하나로 확정하지 않는다. 최악(없다고 가정)과
     최선(있다고 가정)을 함께 내서 확정 불가임을 드러낸다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from uuid import uuid4

from app.hazard_review.models import (
    HazardCategorySummary,
    HazardReviewRequest,
    HazardReviewResult,
)
from app.hazard_review.rulebook import (
    APPLICATION_TYPE_LABELS,
    HOUSING_TYPE_LABELS,
    RULE_PACK_ID,
    RULE_PACK_VERSION,
    RULES,
)
from app.hazard_review.service import HazardReviewService
from app.models import Coordinates
from app.screening.checklist import CHECKLIST_NOTE, EXCLUSION_CHECKLIST
from app.screening.amenities import (
    MAX_RADIUS_M,
    AmenityCollector,
    GroupCollection,
)
from app.screening.models import (
    ScreeningChecklistEntry,
    FrontDoorCandidate,
    GROUP_STATE_LABELS,
    ITEM_OUTCOME_LABELS,
    ScreeningCriterion,
    ScreeningExclusionItem,
    ScreeningFacilityHit,
    ScreeningGroupStatus,
    ScreeningOutOfScopeItem,
    ScreeningRequest,
    ScreeningResult,
    ScreeningStageOne,
    ScreeningStageTwo,
    ScreeningTier,
)
from app.screening.scorebook import (
    BONUS_CRITERION,
    FACILITY_GROUP_BY_KEY,
    OUT_OF_SCOPE_ITEMS,
    PASS_THRESHOLD,
    TOTAL_SHEET_POINTS,
    Criterion,
    Facts,
    evaluate,
    sheet_for,
)


ScreeningProgressCallback = Callable[
    [str, str, str, int, int | None, str],
    Awaitable[None],
]

VERDICT_LABELS: dict[str, str] = {
    "fail": "부적격 (매입제외)",
    "review": "검토 필요",
    "pass": "적격",
}

PASS_SUMMARY = "1차 매입제외 항목에서 확정 저촉이 없습니다."

DISCLAIMER = (
    "생활편의성 40점 외 나머지 60점(공가율·세대수 증가율·재무영향도·접면도로·"
    "주차대수·정책사업)은 공간분석 대상이 아니므로 서류심사 합격선(70점) 판정은 "
    "이 화면에서 내리지 않습니다."
)

# 원천이 없어 판정을 못 했는데 고지 문구까지 비어 있으면 사용자는 '통과'만 읽는다.
DEFAULT_PASSTHROUGH_NOTE = "필요 원천이 아직 연결되지 않았습니다."

BOUNDARY_MEASUREMENT = "사업지 대지경계 ↔ 시설 기준점 직선거리"
POINT_MEASUREMENT = "사업지 주소점 ↔ 시설 기준점 직선거리"

# 실제 필지 경계로 인정하는 geometry_source. 임시 사각형은 경계가 아니다.
CADASTRAL_SOURCES = frozenset({"official_polygon", "parcel_polygon"})


class ScreeningService:
    """유해요소 판정 결과를 심사표 한 장으로 옮기고, 생활편의성 배점을 얹는다."""

    def __init__(
        self,
        hazard: HazardReviewService,
        amenities: AmenityCollector,
        demo_mode: bool = False,
    ) -> None:
        self.hazard = hazard
        self.amenities = amenities
        self.demo_mode = demo_mode

    async def screen(
        self,
        request: ScreeningRequest,
        progress: ScreeningProgressCallback | None = None,
    ) -> ScreeningResult:
        site = request.site
        report = _progress_reporter(progress)

        # --- 1차: 유해시설 조회 → 매입제외 판정 -----------------------------
        await report(
            "STAGE1_COLLECT", "1차 유해시설 조회", "running", 5, None,
            "사업지 주변 유해시설 후보를 조회합니다.",
        )
        review = await self._review(request, report)
        stage_one = _build_stage_one(review)
        facility_count = sum(len(item.facilities) for item in stage_one.items)
        await report(
            "STAGE1_COLLECT", "1차 유해시설 조회", "completed", 100, facility_count,
            f"항목 {len(stage_one.items)}종에서 시설 {facility_count}곳을 확인했습니다.",
        )
        await report(
            "STAGE1_JUDGE", "1차 매입제외 판정", "completed", 100,
            len(stage_one.items), stage_one.summary,
        )

        # --- 2차: 생활편의시설 조회 → 생활편의성 배점 ------------------------
        await report(
            "STAGE2_COLLECT", "2차 생활편의시설 조회", "running", 20, None,
            "시설군을 병렬 조회합니다.",
        )
        rings = _site_rings(site.parcels)
        collections = await self.amenities.collect(rings, site.coordinates, MAX_RADIUS_M)
        collected = sum(1 for item in collections.values() if item.state != "missing")
        await report(
            "STAGE2_COLLECT", "2차 생활편의시설 조회", "completed", 100, collected,
            f"시설군 {len(collections)}종 중 {collected}종을 확보했습니다.",
        )

        await report(
            "STAGE2_SCORE", "2차 생활편의성 배점", "running", 40, None,
            "심사표 등급을 대조합니다.",
        )
        stage_two = _build_stage_two(
            site.application_type,
            collections,
            measurement=BOUNDARY_MEASUREMENT if rings else POINT_MEASUREMENT,
            reference_only=stage_one.verdict == "fail",
        )
        await report(
            "STAGE2_SCORE", "2차 생활편의성 배점", "completed", 100,
            stage_two.living_score_max, _score_message(stage_two),
        )

        return ScreeningResult(
            screening_id=str(uuid4()),
            created_at=datetime.now(UTC),
            demo=review.demo,
            site=site,
            housing_type=site.housing_type,
            housing_type_label=HOUSING_TYPE_LABELS[site.housing_type],
            application_type=site.application_type,
            application_type_label=APPLICATION_TYPE_LABELS[site.application_type],
            rule_pack_id=review.rule_pack.id,
            rule_pack_version=review.rule_pack.version,
            verdict=stage_one.verdict,
            verdict_label=VERDICT_LABELS[stage_one.verdict],
            verdict_summary=_verdict_summary(stage_one),
            stage_one=stage_one,
            stage_two=stage_two,
            hazard_review=review,
            calculation_note=_calculation_note(bool(rings)),
            disclaimer=DISCLAIMER,
        )

    async def _review(
        self,
        request: ScreeningRequest,
        report: ScreeningProgressCallback,
    ) -> HazardReviewResult:
        """유해요소 판정 엔진을 그대로 호출한다. 판정 로직은 여기서 다시 쓰지 않는다."""

        async def forward(
            item_id: str,
            label: str,
            status: str,
            item_progress: int,
            count: int | None,
            message: str,
        ) -> None:
            # 유해요소 엔진의 항목별 진행률을 「1차 유해시설 조회」 한 줄로 접어 올린다.
            await report(
                "STAGE1_COLLECT",
                "1차 유해시설 조회",
                "running",
                max(5, min(95, item_progress)),
                count,
                f"{label} — {message}" if message else label,
            )

        payload = HazardReviewRequest(
            site=request.site,
            rule_pack_id=request.rule_pack_id or RULE_PACK_ID,
            requested_by=request.requested_by,
        )
        return await self.hazard.review(payload, forward, asyncio.Event())


# ---------------------------------------------------------------------------
# 1차 매입제외 — 통과 처리 정책
# ---------------------------------------------------------------------------
_LEGAL_REFERENCE_BY_RULE: dict[str, str] = {rule.rule_id: rule.legal_reference for rule in RULES}


def _build_checklist(verdict: str, items: list[ScreeningExclusionItem]) -> list[ScreeningChecklistEntry]:
    """심사표 2항 15개 항목을 원문 순서대로 만든다.

    자동 판정 항목(주거환경 저해시설) 한 줄만 실제 판정 결과를 싣고, 나머지는
    「별도 확인」으로 둔다. 자동 판정이 통과여도 나머지 14개가 남아 있음을
    화면에서 감추지 않기 위한 것이다.
    """

    entries: list[ScreeningChecklistEntry] = []
    for spec in EXCLUSION_CHECKLIST:
        if spec.mode == "automatic":
            outcome = {"fail": "fail", "review": "review"}.get(verdict, "pass")
            label = ITEM_OUTCOME_LABELS[outcome]
            if outcome == "pass":
                passthrough = sum(1 for item in items if item.passthrough)
                if passthrough:
                    label = f"통과 (판정 미적용 {passthrough}종 포함)"
            entries.append(
                ScreeningChecklistEntry(
                    key=spec.key,
                    label=spec.label,
                    mode=spec.mode,
                    note=spec.note,
                    outcome=outcome,  # type: ignore[arg-type]
                    outcome_label=label,
                )
            )
            continue
        entries.append(
            ScreeningChecklistEntry(
                key=spec.key,
                label=spec.label,
                mode=spec.mode,
                note=spec.note,
                outcome=None,
                outcome_label="별도 확인",
            )
        )
    return entries


def _build_stage_one(review: HazardReviewResult) -> ScreeningStageOne:
    measurement_by_rule = {
        finding.rule_id: finding.measurement_method for finding in review.findings
    }
    items = [
        _exclusion_item(category, measurement_by_rule.get(category.rule_id, ""))
        for category in review.categories
    ]

    if any(item.outcome == "fail" for item in items):
        verdict = "fail"
    elif any(item.outcome == "review" for item in items):
        verdict = "review"
    else:
        verdict = "pass"

    counts: dict[str, int] = {}
    for item in items:
        counts[item.outcome] = counts.get(item.outcome, 0) + 1

    return ScreeningStageOne(
        verdict=verdict,  # type: ignore[arg-type]
        verdict_label=VERDICT_LABELS[verdict],
        summary=_stage_one_summary(verdict, items),
        reasons=[item.reason for item in items if item.outcome == "fail"],
        review_reasons=[item.reason for item in items if item.outcome == "review"],
        passthrough_notes=[
            f"{item.label} — {item.note}" for item in items if item.passthrough
        ],
        items=items,
        counts=counts,
        checklist=_build_checklist(verdict, items),
        checklist_note=CHECKLIST_NOTE,
    )


def _exclusion_item(
    category: HazardCategorySummary,
    measurement_method: str,
) -> ScreeningExclusionItem:
    effect, outcome, passthrough, reason = _decide(category)
    note = category.implementation_note or category.note
    if passthrough and not note:
        note = DEFAULT_PASSTHROUGH_NOTE
    return ScreeningExclusionItem(
        key=category.key,
        label=category.label,
        rule_id=category.rule_id,
        rule_label=category.rule_label,
        threshold_m=category.threshold_m,
        effect=effect,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        outcome_label=ITEM_OUTCOME_LABELS[outcome],
        reason=reason,
        note=note,
        passthrough=passthrough,
        candidate_count=category.candidate_count,
        inside_threshold_count=category.inside_threshold_count,
        nearest_distance_m=category.nearest_distance_m,
        measurement_method=measurement_method,
        legal_reference=_LEGAL_REFERENCE_BY_RULE.get(category.rule_id, ""),
        source_label=category.source_label,
        # 원천 칩은 hazard_review 가 이미 판정 붙임 여부로 정해 채웠다. 여기서는
        # 다시 계산하지 않고 그대로 통과시킨다(의존 방향: screening → hazard_review).
        data_sources=list(category.data_sources),
        facilities=list(category.facilities),
    )


def _decide(category: HazardCategorySummary) -> tuple[str, str, bool, str]:
    """(effect, outcome, passthrough, reason).

    협의로 판정에서 뺀 종류와 수기 확인 대상은 상태보다 먼저 본다. 이 두 가지는
    '판정하지 않기로 한' 항목이라 상태값이 무엇이든 부적격 사유가 될 수 없다.
    """

    if category.judgment_excluded:
        return "advisory", "pass", True, "협의에 따라 판정 제외 — 통과 처리"
    if category.manual_check_required and category.status != "exclusion_match":
        return "advisory", "pass", True, "별도 수기 확인 대상 — 통과 처리"

    status = category.status
    if status == "exclusion_match":
        return (
            "blocking",
            "fail",
            False,
            f"{category.label} {_meters(category.nearest_distance_m)} — "
            f"기준거리 {_threshold(category.threshold_m)} 이내",
        )
    if status == "review_required":
        return "blocking", "review", False, category.note or "조건 확인 필요"
    if status == "no_conflict_in_snapshot":
        return (
            "blocking",
            "pass",
            False,
            f"기준거리 {_threshold(category.threshold_m)} 이내 확정 시설 없음",
        )
    if status == "dataset_missing":
        return "advisory", "pass", True, "판정 미적용 — 통과 처리"
    if status == "geometry_missing":
        return "advisory", "pass", True, "경계 미확보 — 통과 처리"
    return (
        "not_applicable",
        "not_applicable",
        False,
        category.not_applicable_reason or "이 신청유형에는 적용되지 않습니다",
    )


def _stage_one_summary(verdict: str, items: Sequence[ScreeningExclusionItem]) -> str:
    if verdict == "fail":
        fails = [item.label for item in items if item.outcome == "fail"]
        return f"매입제외 사유 {len(fails)}건 — {'·'.join(fails)}"
    if verdict == "review":
        reviews = [item.label for item in items if item.outcome == "review"]
        return f"검토 필요 {len(reviews)}건 — {'·'.join(reviews)}"
    passthrough = sum(1 for item in items if item.passthrough)
    return f"{PASS_SUMMARY} 통과 처리 {passthrough}건은 근거를 함께 확인해 주세요."


def _verdict_summary(stage_one: ScreeningStageOne) -> str:
    fails = [item for item in stage_one.items if item.outcome == "fail"]
    if fails:
        first = fails[0]
        return (
            f"{first.label}{_subject_particle(first.label)} 기준거리 "
            f"{_threshold(first.threshold_m)} 이내에 있어 매입제외 대상입니다."
        )
    reviews = [item for item in stage_one.items if item.outcome == "review"]
    if reviews:
        return (
            f"{reviews[0].label}{_subject_particle(reviews[0].label)} 확인되지 않아 "
            f"검토가 필요합니다(총 {len(reviews)}건)."
        )
    return PASS_SUMMARY


# ---------------------------------------------------------------------------
# 2차 생활편의성 배점
# ---------------------------------------------------------------------------
def _build_stage_two(
    application_type: str,
    collections: dict[str, GroupCollection],
    *,
    measurement: str,
    reference_only: bool,
) -> ScreeningStageTwo:
    sheet = sheet_for(application_type)
    distances = {key: list(item.distances_m) for key, item in collections.items()}
    missing = {key for key, item in collections.items() if item.state == "missing"}

    pessimistic = Facts(distances, missing, assume_missing_present=False)
    optimistic = Facts(distances, missing, assume_missing_present=True)

    criteria = [
        _build_criterion(criterion, pessimistic, optimistic, collections, measurement)
        for criterion in sheet.criteria
    ]
    bonus = _build_criterion(
        BONUS_CRITERION, pessimistic, optimistic, collections, measurement
    )

    score_min = sum(item.awarded_min for item in criteria)
    score_max = sum(item.awarded_max for item in criteria)
    determined = all(item.determined for item in criteria)

    return ScreeningStageTwo(
        sheet_key=sheet.key,
        sheet_label=sheet.label,
        living_maximum=sheet.living_maximum,
        living_score=score_min if determined else None,
        living_score_min=score_min,
        living_score_max=score_max,
        determined=determined,
        criteria=criteria,
        bonus=bonus,
        out_of_scope=[
            ScreeningOutOfScopeItem(label=item.label, maximum=item.maximum, reason=item.reason)
            for item in OUT_OF_SCOPE_ITEMS
        ],
        out_of_scope_points=sum(item.maximum for item in OUT_OF_SCOPE_ITEMS),
        total_sheet_points=TOTAL_SHEET_POINTS,
        pass_threshold=PASS_THRESHOLD,
        reference_only=reference_only,
        note=_stage_two_note(collections, reference_only),
    )


def _build_criterion(
    criterion: Criterion,
    pessimistic: Facts,
    optimistic: Facts,
    collections: dict[str, GroupCollection],
    measurement: str,
) -> ScreeningCriterion:
    awarded_min, min_tier = evaluate(criterion, pessimistic)
    awarded_max, max_tier = evaluate(criterion, optimistic)
    determined = awarded_min == awarded_max

    tiers = [
        ScreeningTier(
            points=tier.points,
            condition=tier.condition,
            achieved=tier.check(pessimistic),
            selected=determined and tier is min_tier,
        )
        for tier in criterion.tiers
    ]
    if determined:
        tier_condition = min_tier.condition
    else:
        tier_condition = (
            f"{awarded_min}점 조건 ~ {awarded_max}점 조건 사이에서 확정 불가"
        )

    missing_labels = [
        FACILITY_GROUP_BY_KEY[key].label
        for key in criterion.groups
        if collections.get(key) is not None and collections[key].state == "missing"
    ]
    note = ""
    if not determined and missing_labels:
        note = f"{'·'.join(missing_labels)} 원천 미확보로 등급을 확정할 수 없습니다."
    elif not determined:
        note = "필요 원천이 확보되지 않아 등급을 확정할 수 없습니다."

    return ScreeningCriterion(
        key=criterion.key,
        label=criterion.label,
        maximum=criterion.maximum,
        awarded=awarded_min if determined else None,
        awarded_min=awarded_min,
        awarded_max=awarded_max,
        determined=determined,
        tier_condition=tier_condition,
        tiers=tiers,
        groups=[
            _group_status(key, collections.get(key), measurement) for key in criterion.groups
        ],
        basis=criterion.basis,
        note=note,
    )


def _group_status(
    key: str,
    collection: GroupCollection | None,
    measurement: str,
) -> ScreeningGroupStatus:
    group = FACILITY_GROUP_BY_KEY[key]
    if collection is None:
        return ScreeningGroupStatus(
            key=key,
            label=group.label,
            state="missing",
            state_label=GROUP_STATE_LABELS["missing"],
            designated_source=group.designated_source,
            note="수집 대상에 포함되지 않았습니다.",
            point_exception=group.point_exception,
        )
    # 출입구·정문 예외 시설은 대표점으로 쟀다는 사실을 시설 한 줄마다 남긴다.
    hit_measurement = (
        f"{measurement} (기준점 예외 — {group.point_exception})"
        if group.point_exception
        else measurement
    )
    return ScreeningGroupStatus(
        key=key,
        label=group.label,
        state=collection.state,  # type: ignore[arg-type]
        state_label=GROUP_STATE_LABELS[collection.state],
        designated_source=group.designated_source,
        actual_source=collection.actual_source,
        note=collection.note,
        count=len(collection.distances_m),
        nearest_distance_m=min(collection.distances_m) if collection.distances_m else None,
        point_exception=group.point_exception,
        front_door_notice=collection.front_door_notice,
        hits=[
            ScreeningFacilityHit(
                name=facility.name,
                address=facility.address,
                distance_m=facility.distance_m,
                group=key,
                group_label=group.label,
                source_label=facility.source_label,
                point_basis=group.point_exception,
                # 대학교는 정문 3단으로 잰 표기(국장님 §3-2)를 그대로 싣는다. 배점에
                # 쓴 거리(facility.distance_m)와 같은 계산에서 나온 문구다.
                measurement_method=(
                    facility.measurement_label
                    if facility.measurement_label
                    else hit_measurement
                ),
                measurement_tier=facility.measurement_tier,
                front_door_source=facility.front_door_source,
                front_door_notice=facility.front_door_notice,
                front_door_candidates=[
                    FrontDoorCandidate(
                        label=candidate.label,
                        coordinates=candidate.coordinates,
                        distance_m=candidate.distance_m,
                        selected=candidate.selected,
                    )
                    for candidate in facility.front_door_candidates
                ],
                coordinates=facility.coordinates,
                nearest_boundary_point=facility.nearest_boundary_point,
                nearest_facility_point=facility.nearest_facility_point,
                facility_ring=list(facility.facility_ring),
            )
            for facility in collection.facilities
        ],
    )


def _stage_two_note(collections: dict[str, GroupCollection], reference_only: bool) -> str:
    substituted = [
        FACILITY_GROUP_BY_KEY[key].label
        for key, item in collections.items()
        if item.state == "substituted"
    ]
    missing = [
        FACILITY_GROUP_BY_KEY[key].label
        for key, item in collections.items()
        if item.state == "missing"
    ]
    parts: list[str] = []
    if reference_only:
        parts.append("1차 매입제외 대상이므로 아래 배점은 참고값입니다.")
    if substituted:
        parts.append(f"대체 원천으로 근사한 시설군 — {'·'.join(substituted)}")
    if missing:
        parts.append(f"원천 미확보 시설군 — {'·'.join(missing)}")
    return " / ".join(parts)


def _score_message(stage_two: ScreeningStageTwo) -> str:
    if stage_two.determined:
        return f"생활편의성 {stage_two.living_score}점 / {stage_two.living_maximum}점"
    return (
        f"생활편의성 {stage_two.living_score_min}~{stage_two.living_score_max}점 "
        f"(확정 불가) / {stage_two.living_maximum}점"
    )


# ---------------------------------------------------------------------------
# 공용 도구
# ---------------------------------------------------------------------------
def _site_rings(parcels: Sequence[object]) -> list[list[Coordinates]]:
    """지적도에서 받은 신청 필지 경계. 임시 사각형은 경계로 인정하지 않는다."""

    rings: list[list[Coordinates]] = []
    for parcel in parcels:
        geometry = getattr(parcel, "geometry", None) or []
        source = getattr(parcel, "geometry_source", "")
        if source in CADASTRAL_SOURCES and len(geometry) >= 4:
            rings.append(list(geometry))
    return rings


def _calculation_note(has_rings: bool) -> str:
    basis = (
        "거리는 사업지 대지경계에서 시설 기준점까지의 직선거리로 잰다."
        if has_rings
        else "필지 경계를 확보하지 못해 사업지 주소점에서 시설 기준점까지 재었습니다. "
        "경계 기준보다 멀게 나올 수 있습니다."
    )
    return (
        f"{basis} 지하철역·철도역은 최단거리 출입구가 아닌 역 대표점, 대학교는 공식 "
        "정문이 아닌 학교 대표점을 Point 기준으로 사용했습니다(평가기준 2항 예외 "
        "항목의 근사). 나머지 시설군도 시설 경계가 아닌 대표점 기준입니다."
    )


def _meters(value: float | None) -> str:
    return f"{value:.0f}m" if value is not None else "거리 미상"


def _threshold(value: int | None) -> str:
    return f"{value}m" if value is not None else "미적용"


def _subject_particle(word: str) -> str:
    """받침 유무로 주격조사를 고른다. '시설이', '학교가' 처럼 읽히게 한다."""

    if not word:
        return "이"
    last = word[-1]
    if not "가" <= last <= "힣":
        return "이"
    return "이" if (ord(last) - 0xAC00) % 28 else "가"


def _progress_reporter(
    progress: ScreeningProgressCallback | None,
) -> ScreeningProgressCallback:
    async def report(
        item_id: str,
        label: str,
        status: str,
        item_progress: int,
        count: int | None,
        message: str,
    ) -> None:
        if progress is None:
            return
        await progress(item_id, label, status, item_progress, count, message)

    return report
