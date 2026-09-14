from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app.hazard_review.models import (
    HazardFacility,
    HazardParcel,
    HazardReviewRequest,
    HazardReviewResult,
    HazardSite,
)
from app.hazard_review.rulebook import (
    CATEGORIES,
    CATEGORY_BY_KEY,
    MATRIX,
    RULES,
    RULE_BY_ID,
    threshold_for,
)
from app.hazard_review.service import (
    HazardReviewService,
    SEARCH_SLACK_M,
    is_self_use_gas,
    offset_coordinates,
)
from app.models import Coordinates
from app.services.facility_store import StoredFacility
from app.services.local_wiring import LocalSourceFile, LocalSourcesBundle
from app.services.local_sources import LocalSourceRecord
from app.services.opinet import OpinetAPIError, OpinetStation
from app.services.vworld import ParcelFeature, VWorldAPIError, ZoningInfo


SITE_CENTER = Coordinates(lat=37.40111, lng=127.10853)


# ---------------------------------------------------------------------------
# 공용 도구
# ---------------------------------------------------------------------------
def square_ring(half_size_m: float, center: Coordinates = SITE_CENTER) -> list[Coordinates]:
    return [
        offset_coordinates(center, -half_size_m, -half_size_m),
        offset_coordinates(center, -half_size_m, half_size_m),
        offset_coordinates(center, half_size_m, half_size_m),
        offset_coordinates(center, half_size_m, -half_size_m),
        offset_coordinates(center, -half_size_m, -half_size_m),
    ]


def build_request(
    housing: str = "house",
    application: str = "multi_child",
    geometry_source: str = "provisional_polygon",
    half_size_m: float = 18,
    pnu: str = "",
) -> HazardReviewRequest:
    return HazardReviewRequest(
        site=HazardSite(
            name="검증 사업지",
            address="경기도 성남시 분당구 판교역로 235",
            coordinates=SITE_CENTER,
            housing_type=housing,  # type: ignore[arg-type]
            application_type=application,  # type: ignore[arg-type]
            parcels=[
                HazardParcel(
                    parcel_id="prototype-parcel",
                    pnu=pnu,
                    geometry=square_ring(half_size_m),
                    geometry_source=geometry_source,  # type: ignore[arg-type]
                )
            ],
        ),
        rule_pack_id="lh-rulebook-v1.4",
    )


class FakeKakaoClient:
    enabled = False


class FakeFacilityStore:
    """로컬 인허가 캐시를 흉내낸다. SQLite 를 타지 않는다."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.available = bool(self.rows)

    def facilities_around(
        self, center: Coordinates, radius_m: float, dataset_keys: Any
    ) -> list[Any]:
        keys = set(dataset_keys)
        return [row for row in self.rows if row.dataset_key in keys]

    def ready_datasets(self) -> set[str]:
        # 적재된 행이 있는 데이터셋만 준비된 것으로 본다(데이터셋별 판정).
        return {row.dataset_key for row in self.rows}

    def latest_sync(self) -> None:
        return None


def stored_facility(
    dataset_key: str,
    name: str,
    north_m: float,
    east_m: float,
    status: str = "영업/정상",
    category: str = "일반숙박업",
    extra: dict[str, str] | None = None,
) -> StoredFacility:
    coordinates = offset_coordinates(SITE_CENTER, north_m, east_m)
    return StoredFacility(
        dataset_key=dataset_key,
        record_id=f"rec-{name}",
        name=name,
        address="테스트 지번주소",
        road_address="테스트 도로명주소",
        coordinates=coordinates,
        status=status,
        category=category,
        distance_m=abs(north_m) + abs(east_m),
        extra=extra or {},
    )


class FakeVWorldForFacilities:
    """시설 좌표를 감싸는 정사각형 필지를 돌려준다.

    zoning_name 이 지정되면 용도지역 조회에 그 이름을 돌려준다(석유대체연료 판정용).
    미지정이면 미확인(None)으로 본다. zoning_calls 로 실제 조회 횟수를 센다.
    """

    def __init__(
        self,
        half_size_m: float = 10,
        fail: bool = False,
        zoning_name: str | None = None,
    ) -> None:
        self.half_size_m = half_size_m
        self.fail = fail
        self.enabled = True
        self.zoning_name = zoning_name
        self.zoning_calls = 0

    async def parcel_at(self, lat: float, lng: float):
        if self.fail:
            raise VWorldAPIError("일시 오류")
        centre = Coordinates(lat=lat, lng=lng)
        return ParcelFeature(
            pnu="4146125628108640000",
            address="시설 필지 주소",
            jibun="864",
            ring=square_ring(self.half_size_m, centre),
            area_m2=(2 * self.half_size_m) ** 2,
        )

    async def zoning_at(self, lat: float, lng: float):
        self.zoning_calls += 1
        if self.fail:
            raise VWorldAPIError("일시 오류")
        return ZoningInfo(name=self.zoning_name) if self.zoning_name else None


class FakeOpinetClient:
    def __init__(self, stations: list[OpinetStation] | None = None) -> None:
        self.stations = stations or []
        self.enabled = True

    async def stations_around(self, center: Coordinates, radius_m: float):
        return list(self.stations)

    async def station_detail(self, station_id: str):
        return next((s for s in self.stations if s.station_id == station_id), None)


class FakeCngClient:
    """가스안전공사 CNG 충전소 API(ODcloud 15001508) 대역."""

    def __init__(self, stations: list[Any] | None = None, enabled: bool = True) -> None:
        self.stations = stations or []
        self.enabled = enabled

    async def stations_around(self, center: Coordinates, radius_m: float):
        return list(self.stations)


class RaisingCngClient:
    enabled = True

    async def stations_around(self, center: Coordinates, radius_m: float):
        from app.services.cng import PublicDataAPIError

        raise PublicDataAPIError("CNG 조회 실패", 500)


def cng_station(name: str, north_m: float, east_m: float) -> Any:
    from app.services.cng import CngStation

    return CngStation(
        name=name,
        address="전북 전주시 덕진구 1",
        region="전북",
        coordinates=offset_coordinates(SITE_CENTER, north_m, east_m),
        branch="전북지역본부",
    )


def opinet_station(name: str, north_m: float, east_m: float) -> OpinetStation:
    return OpinetStation(
        station_id=f"op-{name}",
        name=name,
        brand="SK에너지",
        coordinates=offset_coordinates(SITE_CENTER, north_m, east_m),
        address="테스트 지번",
        road_address="테스트 도로명",
        lpg=False,
    )


def run_review(
    request: HazardReviewRequest,
    demo_mode: bool = False,
    facility_store: Any = None,
    vworld: Any = None,
    opinet: Any = None,
    kgs_lpg: Any = None,
    cadastral: Any = None,
    local_sources: Any = None,
    building_register: Any = None,
    safemap: Any = None,
    noise_emission: Any = None,
    cng: Any = None,
) -> HazardReviewResult:
    service = HazardReviewService(
        kakao=FakeKakaoClient(),  # type: ignore[arg-type]
        demo_mode=demo_mode,
        facility_store=facility_store,
        vworld=vworld,
        opinet=opinet,
        kgs_lpg=kgs_lpg,
        cadastral=cadastral,
        local_sources=local_sources,
        building_register=building_register,
        safemap=safemap,
        noise_emission=noise_emission,
        cng=cng,
    )

    async def progress(*_args: Any) -> None:
        return None

    return asyncio.run(service.review(request, progress, asyncio.Event()))


def category_for(result: HazardReviewResult, key: str):
    return next(c for c in result.categories if c.key == key)


def make_facility(
    distance_m: float,
    geometry_type: str = "point",
    facility_type: str = "crematorium",
    business_status: str = "정상영업",
    metadata: dict[str, Any] | None = None,
) -> HazardFacility:
    return HazardFacility(
        facility_id="f",
        facility_type=facility_type,
        facility_type_label="시설",
        name="테스트 시설",
        coordinates=offset_coordinates(SITE_CENTER, distance_m, 0),
        distance_m=distance_m,
        business_status=business_status,
        provider="test",
        source_label="테스트",
        source_as_of=datetime.now(UTC),
        geometry_type=geometry_type,  # type: ignore[arg-type]
        geometry_note="테스트 경계",
        classification_note="테스트",
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# §2 매트릭스
# ---------------------------------------------------------------------------
class TestMatrix:
    """룰북 §2 매트릭스와 threshold_for 가 한 칸도 어긋나면 안 된다."""

    EXPECTED = {
        ("house", "general"): (50, 50, 25, None, None, 500),
        ("house", "multi_child"): (50, 50, 25, 25, 25, 500),
        ("house", "newlywed"): (50, 50, 25, None, None, 500),
        ("house", "youth"): (50, 50, 25, None, None, 500),
        ("house", "senior"): (50, 50, 25, None, None, 500),
        ("officetel", "general"): (None, None, None, None, None, 500),
        ("officetel", "multi_child"): (None, None, None, None, 25, 500),
        ("officetel", "newlywed"): (None, None, None, None, None, 500),
        ("officetel", "youth"): (None, None, None, None, None, 500),
        ("officetel", "senior"): (None, None, None, None, None, 500),
    }
    ORDER = [rule.rule_id for rule in RULES]

    def test_every_combo_matches_the_contract(self) -> None:
        assert len(self.EXPECTED) == 10
        assert set(MATRIX) == set(self.EXPECTED)
        for (housing, application), values in self.EXPECTED.items():
            got = tuple(
                threshold_for(rule_id, housing, application) for rule_id in self.ORDER
            )
            assert got == values, (housing, application, got)

    def test_officetel_multi_child_keeps_lodging_but_drops_amusement(self) -> None:
        # 표에서 가장 틀리기 쉬운 칸: lodging 25, amusement None.
        assert threshold_for("RB14-LODGING", "officetel", "multi_child") == 25
        assert threshold_for("RB14-AMUSEMENT", "officetel", "multi_child") is None


# ---------------------------------------------------------------------------
# 오피스텔 미적용 / 화장장 500m
# ---------------------------------------------------------------------------
class TestOfficetelApplicability:
    def test_officetel_disables_factory_hazmat_fuel(self) -> None:
        result = run_review(build_request("officetel", "general"), demo_mode=True)
        for key in ("factory_registered", "high_pressure_gas", "gas_station"):
            assert category_for(result, key).status == "not_applicable"
            assert category_for(result, key).threshold_m is None

    def test_officetel_still_applies_cremation_military_500(self) -> None:
        result = run_review(build_request("officetel", "senior"), demo_mode=True)
        crematorium = category_for(result, "crematorium")
        assert crematorium.threshold_m == 500
        assert crematorium.status != "not_applicable"


# ---------------------------------------------------------------------------
# §3 거리 판정 · §공통 경계 미확보
# ---------------------------------------------------------------------------
class TestDistanceDecision:
    def _service(self) -> HazardReviewService:
        return HazardReviewService(kakao=FakeKakaoClient(), demo_mode=False)  # type: ignore[arg-type]

    def test_distance_equal_to_threshold_is_included_as_exclusion(self) -> None:
        # 임계값과 정확히 같은 거리(50.0m)는 이내로 포함되어야 한다.
        service = self._service()
        category = CATEGORY_BY_KEY["crematorium"]
        facility = make_facility(500.0, geometry_type="polygon")
        status, _ = service._decide_status(
            category, 500, [facility], [], site_boundary_resolved=True
        )
        assert status == "exclusion_match"

    def test_point_only_candidate_inside_threshold_is_geometry_missing(self) -> None:
        # 시설 경계가 없어 점 좌표로만 잰 건은 임계 이내여도 확정하지 않는다.
        service = self._service()
        category = CATEGORY_BY_KEY["crematorium"]
        facility = make_facility(400.0, geometry_type="point")
        status, _ = service._decide_status(
            category, 500, [facility], [], site_boundary_resolved=True
        )
        assert status == "geometry_missing"

    def test_cadastral_site_with_facility_polygon_reaches_exclusion(self) -> None:
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "가까운석유", 0, 20, category="석유")]
        )
        result = run_review(
            request, facility_store=store, vworld=FakeVWorldForFacilities(half_size_m=5)
        )
        oil = category_for(result, "oil_retailer")
        assert oil.status == "exclusion_match"
        assert oil.facilities[0].geometry_type == "polygon"

    def test_context_zero_still_fetches_slack_for_boundary_candidates(
        self, monkeypatch
    ) -> None:
        """참고 반경(HAZARD_CONTEXT_RADIUS_M)이 0 이어도 예비검색 슬랙은 확보한다.

        회귀: fetch_limit_m = max(search_limit_m, 0) 이면 점 거리가 판정창 밖인 큰
        폴리곤 시설(경계 거리는 안)을 조회조차 못 해 누락됐다. 점 거리 ~182m·경계
        ~2m 인 시설이 threshold 25m 에서도 판정 후보(exclusion)로 남아야 한다.
        """

        monkeypatch.setattr(
            "app.hazard_review.service.HAZARD_CONTEXT_RADIUS_M", 0
        )
        # 점 좌표는 북쪽 200m, 사업지 경계(half 30m)까지 점거리 ~170m > search_limit 125m.
        # 큰 폴리곤(half 150m)의 남쪽 변이 사업지 경계 가까이(경계거리 ~20m ≤ 25m)까지 온다.
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "먼좌표석유", 200, 0, category="석유")]
        )
        result = run_review(
            build_request(
                "house", "general", geometry_source="parcel_polygon", half_size_m=30
            ),
            facility_store=store,
            vworld=FakeVWorldForFacilities(half_size_m=150),
        )
        oil = category_for(result, "oil_retailer")
        assert oil.status == "exclusion_match"
        assert oil.facilities[0].geometry_type == "polygon"
        assert oil.facilities[0].distance_m <= 25

    def test_context_zero_drops_reference_pins(self, monkeypatch) -> None:
        """참고 반경이 0 이면 참고 핀(nearby)은 비어야 한다.

        조회는 슬랙까지 넓혔지만, 판정 후보로 승격되지 못한 시설이 참고 핀으로
        새어 나오면 안 된다. 점 거리 ~182m·경계도 먼(작은 폴리곤) 시설로 확인한다.
        """

        monkeypatch.setattr(
            "app.hazard_review.service.HAZARD_CONTEXT_RADIUS_M", 0
        )
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "먼석유", 200, 0, category="석유")]
        )
        result = run_review(
            build_request(
                "house", "general", geometry_source="parcel_polygon", half_size_m=30
            ),
            facility_store=store,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        oil = category_for(result, "oil_retailer")
        # 판정 후보 없음(경계거리도 멀다).
        assert oil.status == "no_conflict_in_snapshot"
        # 참고 핀도 비어야 한다(컨텍스트 0). nearby 는 Finding 에 실린다.
        finding = next(f for f in result.findings if f.rule_id == "RB14-FUEL25")
        assert finding.nearby_facilities == []


# ---------------------------------------------------------------------------
# §6.1 공장 — AND 미성립은 review, 절대 exclusion 아님
# ---------------------------------------------------------------------------
class TestFactoryLogic:
    """LH 확정 2026-09-11 (안건 ①): 등록공장 소재만 「공장 있음」 검토 표시.
    유해공장(가~라목) 매칭 판정·자동 제외는 하지 않고, 대기배출·소음 원장은
    판정 근거가 아니라 등록공장 주석(부가 정보) 전용이다.
    """

    def test_factory_dataset_missing_without_registry(self) -> None:
        # 대기배출 원천만 적재되고 factoryON 등록공장 원천은 미적재인 상태.
        # '공장 있음'을 확정할 수 없으므로 dataset_missing 이어야 한다(절대 exclusion·
        # no_conflict 아님).
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        store = FakeFacilityStore(
            [stored_facility("air_pollution", "대기1종공장", 0, 25, category="대기1종")]
        )
        result = run_review(
            request, facility_store=store, vworld=FakeVWorldForFacilities(half_size_m=5)
        )
        factory = category_for(result, "factory_registered")
        assert factory.status == "dataset_missing"
        assert factory.status not in ("exclusion_match", "no_conflict_in_snapshot")
        assert "factoryON" in factory.note
        # 대기배출 사업장만으로는 공장 후보를 만들지 않는다(오검출 방지).
        assert factory.candidate_count == 0

    def test_factory_dataset_missing_without_coordinate_records(self) -> None:
        # factoryON PNU 는 적재됐지만 좌표를 해석할 수 있는 공장 레코드가 0건이면,
        # 조회할 스냅샷이 없는 것이므로 dataset_missing 이어야 한다
        # (파일 존재 ≠ 쓸 수 있는 레코드 존재).
        from app.services.local_wiring import LocalSourcesBundle

        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        bundle = LocalSourcesBundle(
            factory_pnus=frozenset({"4511100000-1-00010000"}),
            factory_registry_loaded=True,
            factory_facilities=(),  # 좌표 보유 공장 레코드 0건
        )
        result = run_review(request, local_sources=bundle)
        factory = category_for(result, "factory_registered")
        assert factory.status == "dataset_missing"
        assert factory.status != "no_conflict_in_snapshot"

    def test_registered_factory_within_50m_is_present_review(self) -> None:
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        factory = category_for(result, "factory_registered")
        assert factory.status == "review_required"
        assert "공장 있음" in factory.note
        # 공장 rule 어디에서도 매입제외로 올라오면 안 된다.
        factory_categories = [
            c for c in result.categories if c.rule_id == "RB14-FACTORY"
        ]
        assert all(c.status != "exclusion_match" for c in factory_categories)

    def test_factory_rule_has_single_category_no_duplicate(self) -> None:
        # 가~라·인접 5종을 한 종류로 합쳤다 — 화면·CSV 중복 방지(LH 확정 2026-09-11).
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        factory_categories = [
            c for c in result.categories if c.rule_id == "RB14-FACTORY"
        ]
        assert [c.key for c in factory_categories] == ["factory_registered"]


# ---------------------------------------------------------------------------
# §6.5 일반숙박 필터
# ---------------------------------------------------------------------------
class TestLodgingFilter:
    """H-05 §5·§6-1 — 관광숙박 8종은 업태(BZSTAT_SE_NM)로 가른다."""

    def test_tourist_hotel_business_type_is_excluded(self) -> None:
        request = build_request("house", "multi_child")
        store = FakeFacilityStore(
            [
                stored_facility("lodgings", "행복모텔", 0, 10, category="여관업"),
                stored_facility("lodgings", "스카이", 0, -8, category="관광호텔"),
                stored_facility("lodgings", "해변콘도", 0, -9, category="휴양콘도미니엄업"),
            ]
        )
        result = run_review(request, facility_store=store)
        lodging = category_for(result, "general_lodging")
        names = {f.name for f in lodging.facilities}
        assert names == {"행복모텔"}

    def test_business_type_beats_trade_name(self) -> None:
        # 상호에 「호텔」이 들어가도 업태가 여관업이면 대상이고, 상호가 평범해도 업태가
        # 관광호텔이면 제외한다. 숙박은 상호가 아니라 분류로 판정한다.
        request = build_request("house", "multi_child")
        store = FakeFacilityStore(
            [
                stored_facility("lodgings", "관광호텔 스카이", 0, 10, category="여관업"),
                stored_facility("lodgings", "평범여관", 0, -8, category="관광호텔"),
            ]
        )
        result = run_review(request, facility_store=store)
        names = {f.name for f in category_for(result, "general_lodging").facilities}
        assert names == {"관광호텔 스카이"}

    def test_demo_row_without_business_type_falls_back_to_keywords(self) -> None:
        # 업태가 없는 원천(데모)에서만 상호·라벨 키워드로 대신 가른다.
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        names = {f.name for f in category_for(result, "general_lodging").facilities}
        assert "검증용 행복모텔" in names
        assert "검증용 관광호텔 스카이" not in names

    def test_living_accommodation_is_excluded(self) -> None:
        # LH 확정 2026-09-11 (안건 ⑥): 생활숙박업(업태 「숙박업(생활)」)은 제외한다.
        # 일반숙박시설만 판정 대상. 원장 실측 표기 「숙박업(생활)」(괄호 포함)을 놓치지
        # 않고 가른다.
        request = build_request("house", "multi_child")
        store = FakeFacilityStore(
            [
                stored_facility("lodgings", "바다생활숙박", 0, 10, category="숙박업(생활)"),
                stored_facility("lodgings", "행복모텔", 0, -8, category="여관업"),
            ]
        )
        result = run_review(request, facility_store=store)
        lodging = category_for(result, "general_lodging")
        names = {f.name for f in lodging.facilities}
        assert names == {"행복모텔"}
        assert "바다생활숙박" not in names


# ---------------------------------------------------------------------------
# §7 ③ 운영상태 필터
# ---------------------------------------------------------------------------
class TestOperatingStatusFilter:
    def test_closed_facility_is_dropped(self) -> None:
        request = build_request("house", "multi_child")
        store = FakeFacilityStore(
            [stored_facility("lodgings", "폐업모텔", 0, 10, status="폐업")]
        )
        result = run_review(request, facility_store=store)
        lodging = category_for(result, "general_lodging")
        assert lodging.candidate_count == 0
        assert lodging.status == "no_conflict_in_snapshot"

    def test_suspended_facility_is_judged_as_active(self) -> None:
        # LH 확정 2026-09-11 (안건 ②): 휴업은 판정에 포함한다. 영업과 같이 판정 대상이며
        # 별도 「휴업」 검토로 내리지 않는다. 상태 표기(「휴업」)는 후보에 그대로 남는다.
        request = build_request(
            "house", "multi_child", geometry_source="parcel_polygon", half_size_m=30
        )
        store = FakeFacilityStore(
            [stored_facility("lodgings", "휴업모텔", 0, 10, status="휴업", category="여관업")]
        )
        result = run_review(
            request, facility_store=store, vworld=FakeVWorldForFacilities(half_size_m=5)
        )
        lodging = category_for(result, "general_lodging")
        assert lodging.candidate_count == 1
        assert lodging.facilities[0].business_status == "휴업"
        # 경계 확인 시 매입제외까지 간다(영업과 동일 취급 · 휴업이라고 보류하지 않는다).
        assert lodging.status == "exclusion_match"


# ---------------------------------------------------------------------------
# §6 data_state=missing
# ---------------------------------------------------------------------------
class TestMissingDatasets:
    def test_missing_category_reports_dataset_missing(self) -> None:
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        # 군부대·사격장은 별도로 판정 제외 처리하므로 여기서 뺀다.
        # LH 회신문 §7 미확보 6개 목은 [요청 2] 승인에 따라 「판정 미적용 — 별도
        # 수기 확인」으로 표시한다. 자료가 없다는 뜻이지 시설이 없다는 뜻이 아니다.
        for key in (
            "hazmat_facility", "toxic_substance",
            "explosive_storage", "lpg_storage", "casino",
        ):
            category = category_for(result, key)
            assert category.status == "dataset_missing"
            assert category.data_state == "manual"
            assert category.manual_check_required is True
            assert "판정 미적용 — 별도 수기 확인" in category.note
            assert category.doc_ref.startswith("H-")

    def test_negotiate_category_is_not_judged(self) -> None:
        # 차목(그 밖에 비슷한 것)은 개별 협의 항목이라 자동 판정하지 않는다(H-02-차).
        result = run_review(build_request("house", "general"), demo_mode=True)
        category = category_for(result, "hazmat_other_similar")
        assert category.status == "dataset_missing"
        assert category.data_state == "negotiate"
        assert category.manual_check_required is True
        assert category.candidate_count == 0
        assert category.doc_ref == "H-02-차"

    def test_every_category_carries_doc_ref(self) -> None:
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        assert result.categories
        assert all(c.doc_ref for c in result.categories)


# ---------------------------------------------------------------------------
# 판정 제외(군부대·사격장) — LH 과업협의 2026-08-14 (회의록_0814 §4 · H-07)
# ---------------------------------------------------------------------------
class TestJudgmentExcluded:
    EXCLUDED_KEYS = ("military_base", "shooting_range")

    def test_excluded_categories_are_visible_but_not_applicable(self) -> None:
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        for key in self.EXCLUDED_KEYS:
            category = category_for(result, key)
            # 리스트에는 보인다.
            assert category is not None
            # 판정에서 빠지므로 not_applicable + 판정제외 플래그.
            assert category.status == "not_applicable"
            assert category.judgment_excluded is True
            assert "판정 대상 제외" in category.not_applicable_reason
            assert "2026-08-14" in category.not_applicable_reason
            assert category.doc_ref == "H-07"
            # 상태값은 룰북 §4 6종을 벗어나지 않는다.
            assert category.status in {
                "exclusion_match",
                "no_conflict_in_snapshot",
                "review_required",
                "dataset_missing",
                "geometry_missing",
                "not_applicable",
            }

    def test_excluded_categories_do_not_affect_status_counts(self) -> None:
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        judged = [c for c in result.categories if not c.judgment_excluded]
        # status_counts 는 판정 제외 종류를 세지 않는다.
        for status, count in result.status_counts.items():
            assert count == sum(1 for c in judged if c.status == status)
        # 판정 제외 두 종류는 어떤 status_counts 버킷에도 잡히지 않는다.
        total_counted = sum(result.status_counts.values())
        assert total_counted == len(judged)

    def test_excluded_categories_do_not_affect_overall_or_finding(self) -> None:
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        cremation_finding = next(
            f for f in result.findings if f.rule_id == "RB14-CREMATION-MILITARY"
        )
        # 화장장만 판정하므로 finding facilities 에 군부대·사격장은 없다.
        finding_keys = {
            f.facility_id for f in cremation_finding.facilities
        }
        for key in self.EXCLUDED_KEYS:
            assert key not in finding_keys
        # 종합상태 승격은 판정 제외 종류의 not_applicable 에 영향받지 않는다.
        judged = [c for c in result.categories if not c.judgment_excluded]
        expected = HazardReviewService._promote([c.status for c in judged])
        assert result.overall_status == expected


# ---------------------------------------------------------------------------
# 미구현 / 미확정 / 검증필요 표기 (구현/확정/검증 축)
# ---------------------------------------------------------------------------
class TestImplementationState:
    def test_building_register_categories_are_not_implemented(self) -> None:
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        for key in (
            "singing_bar",
            "theme_park_comprehensive",
            "theme_park_general",
            "theme_park_other",
        ):
            category = category_for(result, key)
            assert category.implementation_state == "not_implemented"
            assert category.implementation_note

    def test_factory_category_flags_factoryon_not_implemented(self) -> None:
        # demo_mode 는 _category_connected 가 True 를 주지만 구현 축은 별개라,
        # factoryON 등록공장 원천 미적재는 not_implemented 로 드러난다.
        result = run_review(build_request("house", "multi_child"))
        category = category_for(result, "factory_registered")
        assert category.implementation_state == "not_implemented"
        # 로컬 표준본도 산단공 API 도 없을 때의 사유(2026-09-15: API 배선 후 문구 변경).
        assert "등록공장 원천 없음" in category.implementation_note

    def test_dance_academy_adapter_is_ready(self) -> None:
        # 무도학원 엔드포인트는 2026-08-27 실호출로 존재가 확인됐다(`/info` 필요).
        # 어댑터에 빠진 것이 없으므로 구현 축은 ready 다. 남은 것은 포털
        # 활용신청뿐이고, 그것은 data_state(dataset_missing)로 드러난다.
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        academy = category_for(result, "dance_academy")
        assert academy.implementation_state == "ready"
        assert academy.implementation_note == ""

    def test_ready_category_has_ready_state(self) -> None:
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        lodging = category_for(result, "general_lodging")
        assert lodging.implementation_state == "ready"
        assert lodging.implementation_note == ""

    def test_implementation_state_is_separate_axis_from_data_state(self) -> None:
        # data_state 와 implementation_state 는 별개 축이다.
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        entertainment = category_for(result, "entertainment_bar")
        assert entertainment.data_state == "applied"
        assert entertainment.implementation_state == "ready"


# ---------------------------------------------------------------------------
# 전체 결과 골격
# ---------------------------------------------------------------------------
class TestResultShape:
    def test_result_carries_types_and_pending_items(self) -> None:
        result = run_review(build_request("house", "multi_child"), demo_mode=True)
        assert result.housing_type == "house"
        assert result.application_type == "multi_child"
        # 공장 5종을 한 종류로 합쳐 30 → 26 (LH 확정 2026-09-11).
        assert len(result.categories) == 26
        assert len(result.findings) == 6
        # 미결은 INDEX §10-1 LH 판정 기준 결정 항목 위주로 싣는다. 건수보다 각
        # 항목이 근거 문서(H-번호)를 가리키는지가 중요하다. 2026-09-11 확정분은 내렸다.
        assert len(result.pending_items) >= 10
        assert all("H-" in item.status or "TIMELINE" in item.status
                   or "MEASUREMENT" in item.status
                   for item in result.pending_items)
        assert result.rule_pack.id == "lh-rulebook-v1.4"
        assert result.rule_pack.version == "1.6"

    def test_rule_bands_only_for_applicable_thresholds(self) -> None:
        # 오피스텔 일반은 화장장(500m)만 적용되므로 밴드도 500m 만 나온다.
        request = build_request(
            "officetel", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        result = run_review(request, demo_mode=True)
        thresholds = {band.threshold_m for band in result.rule_bands}
        assert thresholds == {500}


# ---------------------------------------------------------------------------
# 원천 조회 실패는 no_conflict 로 둔갑하지 않는다
# ---------------------------------------------------------------------------
class RaisingOpinetClient:
    """조회 때마다 예외를 던지는 오피넷 스텁."""

    enabled = True

    async def stations_around(self, center: Coordinates, radius_m: float):
        raise OpinetAPIError("일시 오류")

    async def station_detail(self, station_id: str):
        return None


class TestSourceFailureNotSnapshot:
    def test_provider_failure_is_dataset_missing_not_no_conflict(self) -> None:
        # 주유소 원천(오피넷)만 붙어 있고 그 조회가 실패하면, 스냅샷 자체가
        # 없으므로 no_conflict_in_snapshot 으로 둔갑하면 안 된다.
        request = build_request("house", "general")
        result = run_review(
            request, facility_store=None, opinet=RaisingOpinetClient()
        )
        gas = category_for(result, "gas_station")
        assert gas.status != "no_conflict_in_snapshot"
        assert gas.status == "dataset_missing"
        assert "실패" in gas.note

    def test_failed_source_is_marked_failed_in_status(self) -> None:
        request = build_request("house", "general")
        result = run_review(
            request, facility_store=None, opinet=RaisingOpinetClient()
        )
        opinet_source = next(
            source for source in result.sources if source.source_id == "opinet-fuel"
        )
        assert opinet_source.state == "failed"

    def test_partial_source_failure_within_category_is_not_no_conflict(self) -> None:
        # gas_station 은 오피넷·생활안전지도(safemap) 두 원천을 함께 쓴다. 오피넷은
        # 정상 조회되고 0건(0건도 정상 결과)이지만 safemap 은 조회 자체가 실패하면,
        # safemap 범위의 스냅샷이 없다. 오피넷 0건만으로 「충돌 없음」을 확정하면
        # 조회하지 못한 safemap 범위까지 충돌 없음으로 둔갑한다 — 그러면 안 된다.
        from app.services.safemap import SafemapAPIError

        class RaisingSafemapClient:
            enabled = True

            async def stations_around_cached(
                self, center: Coordinates, radius_m: float
            ) -> list[Any]:
                raise SafemapAPIError("일시 오류")

        request = build_request("house", "general")
        result = run_review(
            request,
            facility_store=None,
            opinet=FakeOpinetClient([]),  # 정상 조회, 0건
            safemap=RaisingSafemapClient(),
        )
        gas = category_for(result, "gas_station")
        assert gas.status != "no_conflict_in_snapshot"
        assert gas.status == "review_required", gas.note
        assert "safemap" in gas.note or "생활안전지도" in gas.note

    def test_partial_source_failure_does_not_hide_positive_match(self) -> None:
        # 양성 매치(임계거리 이내 후보를 실제로 찾음)는 다른 원천이 실패해도
        # 그대로 유지돼야 한다(찾은 것은 찾은 것). 일부 실패를 이유로 양성 결과를
        # 검토대상으로 낮추지 않는다.
        from app.services.safemap import SafemapAPIError

        class RaisingSafemapClient:
            enabled = True

            async def stations_around_cached(
                self, center: Coordinates, radius_m: float
            ) -> list[Any]:
                raise SafemapAPIError("일시 오류")

        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        result = run_review(
            request,
            facility_store=None,
            opinet=FakeOpinetClient([opinet_station("근접주유소", 0, 20)]),
            safemap=RaisingSafemapClient(),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        gas = category_for(result, "gas_station")
        assert gas.status == "exclusion_match", gas.note


# ---------------------------------------------------------------------------
# 점유 필지 열거 원천이 없을 때 「단일 필지로 판정했다」는 사실을 감추지 않는다
# ---------------------------------------------------------------------------
class TestSingleParcelOnlyDisclosure:
    def _service(self) -> HazardReviewService:
        return HazardReviewService(kakao=FakeKakaoClient(), demo_mode=False)  # type: ignore[arg-type]

    def test_enumerated_false_marks_single_parcel_only(self) -> None:
        # 점유 필지 열거 원천이 없어 좌표가 포함된 필지 1개만 잡은 경우
        # (enumerated=False), 다필지를 다 본 것처럼 보이면 안 된다.
        service = self._service()
        facility = make_facility(10, geometry_type="polygon")
        boundaries = [square_ring(20)]
        parcels = [(square_ring(2, offset_coordinates(SITE_CENTER, 10, 0)), "PNU1", "", "", 100.0)]
        service._apply_parcel_distances(facility, boundaries, parcels, enumerated=False)
        assert facility.metadata.get("single_parcel_only") is True
        assert "단일 필지" in facility.geometry_note

    def test_enumerated_true_does_not_mark_single_parcel_only(self) -> None:
        # 점유 필지를 실제로 열거한 경우(enumerated=True, 기본값)는 이 표기가
        # 붙으면 안 된다 — 다필지를 실제로 다 본 정상 경로다.
        service = self._service()
        facility = make_facility(10, geometry_type="polygon")
        boundaries = [square_ring(20)]
        parcels = [
            (square_ring(2, offset_coordinates(SITE_CENTER, 10, 0)), "PNU1", "", "", 100.0),
            (square_ring(2, offset_coordinates(SITE_CENTER, -10, 0)), "PNU2", "", "", 100.0),
        ]
        service._apply_parcel_distances(facility, boundaries, parcels, enumerated=True)
        assert "single_parcel_only" not in facility.metadata


# ---------------------------------------------------------------------------
# §6.1 공장 — 등록공장 「공장 있음」 검토 표시 (LH 확정 2026-09-11 안건 ①)
# ---------------------------------------------------------------------------
class TestFactoryDecision:
    def _service(self) -> HazardReviewService:
        return HazardReviewService(kakao=FakeKakaoClient(), demo_mode=False)  # type: ignore[arg-type]

    def test_registered_factory_inside_is_review_not_exclusion(self) -> None:
        # 등록공장이 기준거리 이내면 「공장 있음」 검토 표시만 한다. 경계가 확인돼도
        # 매입제외로 올리지 않는다(유해공장 매칭 판정 없음).
        service = self._service()
        factory = make_facility(
            30, geometry_type="polygon", facility_type="factory",
            metadata={"factory_registered": True},
        )
        status, note = service._decide_status(
            CATEGORY_BY_KEY["factory_registered"], 50, [factory], [],
            site_boundary_resolved=True,
        )
        assert status == "review_required"
        assert status != "exclusion_match"
        assert "공장 있음" in note

    def test_no_registered_factory_is_no_conflict(self) -> None:
        service = self._service()
        status, _ = service._decide_status(
            CATEGORY_BY_KEY["factory_registered"], 50, [], [],
            site_boundary_resolved=True,
        )
        assert status == "no_conflict_in_snapshot"


# ---------------------------------------------------------------------------
# §3 고압가스 자가설비(기관 자체 사용) 제외 — LH 확정 2026-09-11 (안건 ③)
# ---------------------------------------------------------------------------
class TestSelfUseGas:
    def test_fire_station_air_filling_is_self_use(self) -> None:
        # 소방서 공기충전(제조구분 충전) → 자가설비로 제외.
        assert is_self_use_gas("완주소방서", "제조", "충전") is True

    def test_hospital_storage_is_self_use(self) -> None:
        # 병원 저장소(업태 저장소) → 자가설비로 제외.
        assert is_self_use_gas("전북대학교병원", "저장소", "일반") is True

    def test_general_filling_station_is_kept(self) -> None:
        # 기관 명칭이 아닌 일반 충전소 → 유지.
        assert is_self_use_gas("행복엘피지충전소", "제조", "충전") is False

    def test_hospital_specific_gas_use_report_is_self_use(self) -> None:
        # 특정고압가스 사용신고 행은 업태·제조구분이 비어 있다. 기관 명칭 + 사용목적
        # (의료용)으로 자가설비로 본다(2026-09-14 건국대학교병원 사례).
        assert is_self_use_gas("건국대학교병원", "", "", "의료용") is True

    def test_medical_purpose_is_self_use_regardless_of_name(self) -> None:
        assert is_self_use_gas("혜민의원", "", "", "의료용(병실의 환자 호흡용 등)") is True

    def test_plant_specific_gas_use_report_is_not_self_use(self) -> None:
        # 기관 명칭이 아닌 공장의 사용신고는 종전대로 검토 후보로 남는다(§7-3 미확정).
        assert is_self_use_gas("삼성전자 화성사업장", "", "", "반도체 공정용") is False

    def test_refrigeration_non_institution_is_not_self_use_here(self) -> None:
        # 냉동(냉방설비)은 이 함수가 제외하지 않는다 — 기존 _classify_gas_facilities 의
        # 「냉동=판정 미적용」 처리를 그대로 둔다.
        assert is_self_use_gas("행복마트", "제조", "냉동") is False

    def test_self_use_gas_row_is_not_a_candidate(self) -> None:
        # 자가설비 행은 판정·검토·참고 핀 어디에도 올라오지 않는다(후보에서 완전 제외).
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        store = FakeFacilityStore(
            [
                stored_facility(
                    "high_pressure_gas", "완주소방서", 0, 20,
                    category="저장소", extra={"MNFTR_SE_NM": "충전"},
                ),
                stored_facility(
                    "high_pressure_gas", "행복충전소", 0, -20,
                    category="제조", extra={"MNFTR_SE_NM": "충전"},
                ),
            ]
        )
        result = run_review(
            request, facility_store=store, vworld=FakeVWorldForFacilities(half_size_m=5)
        )
        gas = category_for(result, "high_pressure_gas")
        names = {f.name for f in gas.facilities}
        assert "완주소방서" not in names  # 자가설비 → 완전 제외
        assert "행복충전소" in names  # 일반 충전소 → 유지(검토)


# ---------------------------------------------------------------------------
# §6.3 주유소·CNG 원천 정합성 (체크리스트 정본: CSV 원천, 추정 슬러그 제거)
# ---------------------------------------------------------------------------
class TestFuelSources:
    def test_removed_guessed_slugs_are_absent(self) -> None:
        # 추정 슬러그는 레지스트리에서 사라져야 한다.
        from app.services.localdata import DATASET_BY_KEY

        assert "gas_stations" not in DATASET_BY_KEY
        assert "cng_stations" not in DATASET_BY_KEY

    def test_gas_station_resolves_from_opinet_when_csv_absent(self) -> None:
        # 주유소 정본은 산업통상부 CSV(적재대기). 그 전까지는 오피넷 보조로 판정.
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        opinet = FakeOpinetClient([opinet_station("보조주유소", 0, 20)])
        result = run_review(
            request, opinet=opinet, vworld=FakeVWorldForFacilities(half_size_m=5)
        )
        gas = category_for(result, "gas_station")
        assert any(f.name == "보조주유소" for f in gas.facilities)
        assert gas.status == "exclusion_match"

    def test_unclassified_safemap_station_is_review_not_exclusion(self) -> None:
        # 상표·lpg_yn 이 전부 공란인 생활안전지도 시설(미분류)은 임계거리 이내이고
        # 시설 필지 폴리곤이 붙어도 exclusion_match 로 승격되지 않는다. 구분이 확인
        # 안 됐으므로 review_required 로만 남긴다(룰북 §3).
        from app.services.safemap import SafemapStation

        class FakeSafemapClient:
            def __init__(self, stations: list[SafemapStation]) -> None:
                self._stations = stations
                self.enabled = True

            async def stations_around_cached(
                self, center: Coordinates, radius_m: float
            ) -> list[SafemapStation]:
                return list(self._stations)

        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        unclassified = SafemapStation(
            station_id="U1",
            name="구분미상주유시설",
            coordinates=offset_coordinates(SITE_CENTER, 10, 0),
            is_gas_station=False,
            is_lpg_station=False,
            is_unclassified=True,
        )
        result = run_review(
            request,
            safemap=FakeSafemapClient([unclassified]),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        gas = category_for(result, "gas_station")
        assert gas.status == "review_required", gas.note
        assert gas.status != "exclusion_match"
        assert "구분 정보 없음" in gas.note
        # LPG 종류도 미분류 시설을 조회 안 함으로 두지 않고 review 로 받는다.
        lpg = category_for(result, "lpg_station")
        assert lpg.status == "review_required", lpg.note

    def test_safemap_stations_around_cached_called_once_per_review(self) -> None:
        # 성능 회귀 방지(2026-08-29): 생활안전지도 원천은 판정 1회당 한 번만 받아
        # 공유한다. review() 시작 시점에 한 번만 받으므로 호출 횟수가 늘지 않아야 한다.
        # LH 확정 2026-09-11: 주유소·자동차용 LPG 충전소 모두 FUEL25(25m)로 본다.
        # 두 시설은 OFFICIAL_DEDUPE_M(40m) 안에 있으면 한 시설로 묶이므로, 서로 다른
        # 시설임을 확인하려고 44m(>40m) 떨어뜨려 배치한다.
        from app.services.safemap import SafemapStation

        class CountingSafemapClient:
            def __init__(self, stations: list[SafemapStation]) -> None:
                self._stations = stations
                self.enabled = True
                self.calls = 0

            async def stations_around_cached(
                self, center: Coordinates, radius_m: float
            ) -> list[SafemapStation]:
                self.calls += 1
                return list(self._stations)

        gas_station = SafemapStation(
            station_id="G1",
            name="생활안전지도주유소",
            coordinates=offset_coordinates(SITE_CENTER, 0, 22),
            oil_brand="SK에너지",
            is_gas_station=True,
        )
        lpg_station = SafemapStation(
            station_id="L1",
            name="생활안전지도LPG",
            coordinates=offset_coordinates(SITE_CENTER, 0, -22),
            lpg_yn=True,
            is_lpg_station=True,
        )
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        safemap = CountingSafemapClient([gas_station, lpg_station])
        result = run_review(
            request,
            safemap=safemap,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )

        assert safemap.calls == 1, (
            f"stations_around_cached 는 판정 1회에 1번만 불려야 한다 "
            f"(실제 {safemap.calls}회)"
        )
        # 호출을 나눈 뒤에도 두 rule 이 각자 맞는 종류로 정확히 갈라야 한다.
        gas = category_for(result, "gas_station")
        lpg = category_for(result, "lpg_station")
        assert any(f.name == "생활안전지도주유소" for f in gas.facilities)
        assert any(f.name == "생활안전지도LPG" for f in lpg.facilities)

    def test_gas_and_lpg_within_dedupe_radius_both_survive(self) -> None:
        # LH 확정 2026-09-11: 주유소와 LPG 충전소는 facility_type 이 달라, 40m
        # (OFFICIAL_DEDUPE_M) 안에 있어도 한 건으로 병합하지 않는다. 30m 떨어진
        # 주유소+LPG 충전소가 둘 다 남아야 한다(같은 타입끼리만 병합).
        from app.services.safemap import SafemapStation

        class FakeSafemapClient:
            def __init__(self, stations: list[SafemapStation]) -> None:
                self._stations = stations
                self.enabled = True

            async def stations_around_cached(
                self, center: Coordinates, radius_m: float
            ) -> list[SafemapStation]:
                return list(self._stations)

        # 중심에서 동/서로 각 15m → 둘 사이 30m(< 40m).
        gas_station = SafemapStation(
            station_id="G1",
            name="가까운주유소",
            coordinates=offset_coordinates(SITE_CENTER, 0, 15),
            oil_brand="SK에너지",
            is_gas_station=True,
        )
        lpg_station = SafemapStation(
            station_id="L1",
            name="가까운LPG충전소",
            coordinates=offset_coordinates(SITE_CENTER, 0, -15),
            lpg_yn=True,
            is_lpg_station=True,
        )
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        result = run_review(
            request,
            safemap=FakeSafemapClient([gas_station, lpg_station]),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        gas = category_for(result, "gas_station")
        lpg = category_for(result, "lpg_station")
        assert any(f.name == "가까운주유소" for f in gas.facilities), gas.note
        assert any(f.name == "가까운LPG충전소" for f in lpg.facilities), lpg.note

    def test_cng_is_dataset_missing_without_api_or_csv(self) -> None:
        # CNG 는 가스안전공사 API(ODcloud 15001508) 또는 로컬 CSV 둘 중 하나라도
        # 붙어야 판정한다. 둘 다 없으면 dataset_missing 이 정상.
        request = build_request("house", "general")
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "석유", 0, 20, category="석유")]
        )
        result = run_review(request, facility_store=store)
        cng = category_for(result, "cng_station")
        assert cng.status == "dataset_missing"

    def test_cng_station_from_api_is_exclusion_match(self) -> None:
        # 2026-09-14 조사: CNG 전국 자료(15001508)에 위경도가 있다. API 후보가
        # 25m(FUEL25) 안이면 로컬 CSV 없이도 저촉으로 잡혀야 한다.
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        result = run_review(
            request,
            facility_store=FakeFacilityStore([]),
            cng=FakeCngClient([cng_station("전주CNG충전소", 0, 40)]),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        cng = category_for(result, "cng_station")
        assert cng.status == "exclusion_match", cng.note
        hit = next(f for f in cng.facilities if f.name == "전주CNG충전소")
        assert hit.provider == "cng"
        assert hit.facility_type == "cng_station"

    def test_cng_api_failure_is_not_disguised_as_no_conflict(self) -> None:
        # 유일한 활성 원천이 실패하면(활용신청 전 401 등) 스냅샷이 없다. 다른 API 와
        # 같은 규칙으로 dataset_missing + 실패 원천 표기이지 「충돌 없음」이 아니다.
        request = build_request("house", "general")
        result = run_review(
            request, facility_store=FakeFacilityStore([]), cng=RaisingCngClient()
        )
        cng = category_for(result, "cng_station")
        assert cng.status == "dataset_missing", cng.note
        assert "원천 조회 실패" in cng.note
        assert "CNG" in cng.note

    def test_local_cng_duplicate_of_api_station_is_dropped(self) -> None:
        # API 가 먼저다. 같은 자리(40m 이내)의 로컬 CSV 행은 중복으로 버린다.
        from app.services.local_sources import LocalSourceRecord
        from app.services.local_wiring import LocalSourcesBundle

        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        local = LocalSourceRecord(
            source_id="cng_stations", record_id="C1", name="로컬CNG",
            coordinates=offset_coordinates(SITE_CENTER, 0, 45),
            status_class="active", origin="raw",
        )
        result = run_review(
            request,
            facility_store=FakeFacilityStore([]),
            cng=FakeCngClient([cng_station("전주CNG충전소", 0, 40)]),
            local_sources=LocalSourcesBundle(cng_facilities=(local,)),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        cng = category_for(result, "cng_station")
        names = [f.name for f in cng.facilities]
        assert "전주CNG충전소" in names
        assert "로컬CNG" not in names

    def test_oil_retailer_resolves_from_petroleum_alt(self) -> None:
        # 석유대체연료판매업(petroleum_alt)도 석유판매 카테고리에 잡혀야 한다.
        # 용도지역이 비주거(공업)로 확인되면 25m 그대로 확정(LH 확정 2026-09-11 #5).
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        store = FakeFacilityStore(
            [
                stored_facility(
                    "petroleum_alt_fuel_retailers", "대체연료판매", 0, 20,
                    category="석유대체",
                )
            ]
        )
        result = run_review(
            request, facility_store=store,
            vworld=FakeVWorldForFacilities(half_size_m=5, zoning_name="일반공업지역"),
        )
        oil = category_for(result, "oil_retailer")
        assert any(f.name == "대체연료판매" for f in oil.facilities)
        assert oil.status == "exclusion_match"


# ---------------------------------------------------------------------------
# 석유대체연료 판매업 용도지역(지적편집도) — LH 확정 2026-09-11 #5
# ---------------------------------------------------------------------------
class TestPetroleumAltZoning:
    def _run(self, zoning_name, fail=False):
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        store = FakeFacilityStore(
            [
                stored_facility(
                    "petroleum_alt_fuel_retailers", "대체연료판매", 0, 20,
                    category="석유대체",
                )
            ]
        )
        vworld = FakeVWorldForFacilities(half_size_m=5, zoning_name=zoning_name, fail=fail)
        result = run_review(request, facility_store=store, vworld=vworld)
        return category_for(result, "oil_retailer"), vworld

    def test_residential_within_25m_is_review_required(self) -> None:
        oil, _ = self._run("제2종일반주거지역")
        assert oil.status == "review_required"
        fac = next(f for f in oil.facilities if f.name == "대체연료판매")
        assert fac.zoning_name == "제2종일반주거지역"
        assert fac.zoning_class == "residential"
        assert "확인 요청" in oil.note

    def test_industrial_within_25m_is_exclusion_with_zoning_in_reason(self) -> None:
        oil, _ = self._run("일반공업지역")
        assert oil.status == "exclusion_match"
        fac = next(f for f in oil.facilities if f.name == "대체연료판매")
        assert fac.zoning_class == "non_residential"
        assert "일반공업지역" in oil.note

    def test_unknown_zoning_within_25m_is_review_required(self) -> None:
        oil, _ = self._run(None)
        assert oil.status == "review_required"
        fac = next(f for f in oil.facilities if f.name == "대체연료판매")
        assert fac.zoning_class == "unknown"

    def test_vworld_failure_is_treated_as_unknown_review(self) -> None:
        oil, _ = self._run(None, fail=True)
        assert oil.status == "review_required"
        fac = next(f for f in oil.facilities if f.name == "대체연료판매")
        assert fac.zoning_class == "unknown"

    def test_gas_station_is_not_zoning_queried(self) -> None:
        # 주유소는 용도지역 조회 대상이 아니다(VWorld zoning 호출 0회).
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        vworld = FakeVWorldForFacilities(half_size_m=5, zoning_name="일반공업지역")
        opinet = FakeOpinetClient([opinet_station("가까운주유소", 0, 15)])
        result = run_review(request, opinet=opinet, vworld=vworld)
        assert vworld.zoning_calls == 0
        gas = category_for(result, "gas_station")
        assert gas.status == "exclusion_match"


# ---------------------------------------------------------------------------
# §6.2/§6.4 체크리스트 신규·분리 카테고리
# ---------------------------------------------------------------------------
class TestChecklistCategories:
    def test_theme_park_split_into_three(self) -> None:
        for key in (
            "theme_park_comprehensive",
            "theme_park_general",
            "theme_park_other",
        ):
            assert key in CATEGORY_BY_KEY
        assert "theme_park" not in CATEGORY_BY_KEY

    def test_dance_split_into_two(self) -> None:
        assert "dance_hall" in CATEGORY_BY_KEY
        assert "dance_academy" in CATEGORY_BY_KEY

    def test_paint_retailer_added_to_hazmat(self) -> None:
        paint = CATEGORY_BY_KEY["paint_retailer"]
        assert paint.rule_id == "RB14-HAZMAT"
        assert "petroleum_alt_fuel_retailers" in paint.datasets

    def test_general_theme_park_resolves_from_localdata(self) -> None:
        request = build_request(
            "house", "multi_child",
            geometry_source="parcel_polygon", half_size_m=30,
        )
        store = FakeFacilityStore(
            [
                stored_facility(
                    "general_amusement_facilities", "일반테마", 0, 20,
                    category="테마파크",
                )
            ]
        )
        result = run_review(
            request, facility_store=store,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        general = category_for(result, "theme_park_general")
        comprehensive = category_for(result, "theme_park_comprehensive")
        assert any(f.name == "일반테마" for f in general.facilities)
        # §6.4: 테마파크는 건축물대장 교차확인이 AND 조건이라, 25m 이내여도
        # exclusion 으로 확정하지 않고 review_required 로 남긴다(어댑터 미구현).
        assert general.status == "review_required"
        assert general.status != "exclusion_match"
        assert "건축물대장" in general.note
        # 다른 테마파크 종류로 새어 들어가면 안 된다.
        assert all(f.name != "일반테마" for f in comprehensive.facilities)

    def test_dance_academy_resolves_from_localdata(self) -> None:
        request = build_request(
            "house", "multi_child",
            geometry_source="parcel_polygon", half_size_m=30,
        )
        store = FakeFacilityStore(
            [
                stored_facility(
                    "dance_academies", "무도학원A", 0, 20, category="무도학원"
                )
            ]
        )
        result = run_review(
            request, facility_store=store,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        academy = category_for(result, "dance_academy")
        assert any(f.name == "무도학원A" for f in academy.facilities)


# ---------------------------------------------------------------------------
# §6.4 위락 건축물대장 AND — 교차확인 전에는 exclusion 으로 확정하지 않는다
# ---------------------------------------------------------------------------
class TestBuildingRegisterAnd:
    def test_singing_bar_within_25m_is_review_not_exclusion(self) -> None:
        request = build_request(
            "house", "multi_child",
            geometry_source="parcel_polygon", half_size_m=30,
        )
        store = FakeFacilityStore(
            [stored_facility("singing_bars", "단란주점A", 0, 20, category="단란주점")]
        )
        result = run_review(
            request, facility_store=store,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        singing = category_for(result, "singing_bar")
        # 시설 경계(polygon)로 25m 이내로 확인돼도 건축물대장 AND 미확인이라
        # exclusion 으로 올리지 않는다.
        assert singing.status == "review_required"
        assert singing.status != "exclusion_match"
        assert "건축물대장" in singing.note

    def test_entertainment_bar_stays_exclusion_without_building_register(self) -> None:
        # 유흥주점(나목)은 건축물대장 AND 조건이 없으므로 그대로 exclusion.
        request = build_request(
            "house", "multi_child",
            geometry_source="parcel_polygon", half_size_m=30,
        )
        store = FakeFacilityStore(
            [
                stored_facility(
                    "entertainment_bars", "유흥주점A", 0, 20, category="유흥주점"
                )
            ]
        )
        result = run_review(
            request, facility_store=store,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        bar = category_for(result, "entertainment_bar")
        assert bar.status == "exclusion_match"


# ---------------------------------------------------------------------------
# §11 원천 준비상태는 데이터셋별이다 (전역 아님)
# ---------------------------------------------------------------------------
class TestPerDatasetReadiness:
    def test_other_dataset_loaded_does_not_make_category_no_conflict(self) -> None:
        # lodgings 만 적재된 상태. 조회조차 안 한 유흥주점·테마파크가
        # no_conflict_in_snapshot 으로 둔갑하면 안 된다(dataset_missing 이어야 한다).
        request = build_request("house", "multi_child")
        store = FakeFacilityStore(
            [stored_facility("lodgings", "행복모텔", 0, 10)]
        )
        result = run_review(request, facility_store=store)
        lodging = category_for(result, "general_lodging")
        # 적재된 lodgings 는 정상 판정된다.
        assert lodging.status != "dataset_missing"
        for key in ("entertainment_bar", "dance_hall", "high_pressure_gas"):
            category = category_for(result, key)
            assert category.status != "no_conflict_in_snapshot", key
            assert category.status == "dataset_missing", key

    def test_unloaded_applied_category_is_dataset_missing_with_note(self) -> None:
        # 테마파크·무도학원은 2026-08-27 실호출로 엔드포인트 존재가 확인됐고
        # (`/info` 필요), 막고 있는 것은 포털 활용신청뿐이다. 적재 전까지는
        # dataset_missing 이되 '시설 없음'이 아니라 '원천 미연결'임을 밝힌다.
        request = build_request("house", "multi_child")
        store = FakeFacilityStore(
            [stored_facility("lodgings", "행복모텔", 0, 10)]
        )
        result = run_review(request, facility_store=store)
        theme = category_for(result, "theme_park_general")
        assert theme.status == "dataset_missing"
        assert theme.note
        assert "미연결" in theme.note


# ---------------------------------------------------------------------------
# Rule 후보 정렬 — 판정 기준이 된 시설이 목록 맨 앞이어야 한다
# ---------------------------------------------------------------------------
class TestFindingFacilityOrder:
    """`finding.facilities[0]` 은 반드시 최근접 시설이어야 한다.

    지도는 이 첫 후보에 최단거리선과 거리 라벨을 그린다. 카테고리별로 정렬한
    목록을 그대로 이어붙이면 앞선 카테고리의 먼 시설이 맨 앞에 오고,
    `measured_distance_m`(전체 최소값)과 지도에 찍히는 숫자가 어긋난다.
    """

    def _fuel_result(self) -> HazardReviewResult:
        # RB14-FUEL25 는 gas_station(오피넷) → oil_retailer(인허가) 순서로
        # 카테고리가 붙는다. 앞 카테고리에 먼 시설, 뒤 카테고리에 가까운 시설을
        # 두어 이어붙이기 순서와 거리 순서가 어긋나게 만든다.
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        return run_review(
            request,
            opinet=FakeOpinetClient([opinet_station("먼주유소", 0, 60)]),
            facility_store=FakeFacilityStore(
                [stored_facility("oil_retailers", "가까운석유", 0, -35, category="석유")]
            ),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )

    def _fuel_finding(self, result: HazardReviewResult):
        return next(f for f in result.findings if f.rule_id == "RB14-FUEL25")

    def test_nearest_facility_is_first(self) -> None:
        finding = self._fuel_finding(self._fuel_result())
        assert [f.name for f in finding.facilities] == ["가까운석유", "먼주유소"]

    def test_first_facility_matches_measured_distance(self) -> None:
        finding = self._fuel_finding(self._fuel_result())
        assert finding.measured_distance_m is not None
        assert finding.facilities[0].distance_m == pytest.approx(
            finding.measured_distance_m
        )


# ---------------------------------------------------------------------------
# 여유구간 검토는 '경계를 확인하지 못한' 후보에만 걸려야 한다
# ---------------------------------------------------------------------------
class TestBoundaryBufferReview:
    """기준거리 밖 후보를 검토 필요로 남기는 것은 경계 미확보를 메우기 위해서다.

    시설 폴리곤이 이미 붙어 distance_m 이 경계 대 경계 값이면 더 확인할 것이
    남아 있지 않다. 그런 후보까지 검토로 남기면 판정이 끝없이 보류된다.
    """

    def _lodging_category(self, result: HazardReviewResult):
        return category_for(result, "general_lodging")

    def test_confirmed_boundary_outside_threshold_is_no_conflict(self) -> None:
        # 사업지 경계에서 60m 떨어진 숙박시설. 시설 필지(반경 5m)까지 확보되면
        # 경계 대 경계로도 25m 밖이므로 확인할 것이 남지 않는다.
        request = build_request(
            "house", "multi_child", geometry_source="parcel_polygon", half_size_m=20
        )
        result = run_review(
            request,
            facility_store=FakeFacilityStore(
                [stored_facility("lodgings", "여유구간모텔", 0, 80)]
            ),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        category = self._lodging_category(result)
        assert category.status == "no_conflict_in_snapshot", category.note

    def test_unconfirmed_boundary_in_buffer_stays_review(self) -> None:
        # 같은 배치인데 시설 필지를 못 받아오면 점 좌표로만 잰 값이라,
        # 시설 경계가 기준 안으로 들어올 여지가 남는다. 이때는 검토로 남긴다.
        request = build_request(
            "house", "multi_child", geometry_source="parcel_polygon", half_size_m=20
        )
        result = run_review(
            request,
            facility_store=FakeFacilityStore(
                [stored_facility("lodgings", "여유구간모텔", 0, 80)]
            ),
            vworld=FakeVWorldForFacilities(half_size_m=5, fail=True),
        )
        category = self._lodging_category(result)
        assert category.status == "review_required", category.note


# ---------------------------------------------------------------------------
# §4 자동차용 LPG 충전소 25m — LH 확정 2026-09-11 (안건 ④)
# ---------------------------------------------------------------------------
class TestLpgStationFuel25:
    def test_lpg_station_category_is_fuel25_25m(self) -> None:
        # LPG 충전소는 RB14-FUEL25(25m) 로 옮겼다. LPG 판매소·저장소는 HAZMAT 50m 유지.
        assert CATEGORY_BY_KEY["lpg_station"].rule_id == "RB14-FUEL25"
        assert CATEGORY_BY_KEY["lpg_retailer"].rule_id == "RB14-HAZMAT"
        assert CATEGORY_BY_KEY["lpg_storage"].rule_id == "RB14-HAZMAT"
        result = run_review(build_request("house", "general"), demo_mode=True)
        assert category_for(result, "lpg_station").threshold_m == 25


# ---------------------------------------------------------------------------
# §G 예비검색 슬랙 — 점 거리는 판정창 밖이어도 경계 거리는 안이면 후보에 남긴다
# ---------------------------------------------------------------------------
class TestSearchSlack:
    def test_slack_keeps_far_point_but_near_boundary_candidate(self) -> None:
        # FUEL25 는 25m → search_limit 125m. 시설 점 좌표는 사업지 경계에서 ~170m
        # (판정창 밖)지만, 큰 시설 폴리곤의 경계는 ~50m(판정창 안)다. 점 거리로 먼저
        # 자르면 빠지지만, 예비검색 슬랙(150m)까지 넓혀 경계를 붙이면 후보로 남는다.
        assert SEARCH_SLACK_M == 150
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        service = HazardReviewService(
            kakao=FakeKakaoClient(),  # type: ignore[arg-type]
            demo_mode=False,
            opinet=FakeOpinetClient([opinet_station("먼주유소", 0, 200)]),
            vworld=FakeVWorldForFacilities(half_size_m=120),
        )
        rule = RULE_BY_ID["RB14-FUEL25"]
        cats = [c for c in CATEGORIES if c.rule_id == "RB14-FUEL25"]
        facilities, nearby, _failed = asyncio.run(
            service._find_rule_facilities(request, rule, cats, 25)
        )
        # 경계 부착 후 거리가 판정창(125m) 안으로 들어와 판정 후보로 남는다.
        assert any(f.name == "먼주유소" for f in facilities), (
            [round(f.distance_m, 1) for f in facilities],
            [round(f.distance_m, 1) for f in nearby],
        )
        kept = next(f for f in facilities if f.name == "먼주유소")
        assert kept.distance_m <= 125


# ---------------------------------------------------------------------------
# 공장 — 등록공장 주석(소음배출 신고) 배선
# ---------------------------------------------------------------------------
class _FakeNoiseClient:
    """NoiseEmissionClient 대역. enabled 와 facilities_around 만 흉내낸다."""

    def __init__(self, facilities: list[Any] | None = None, *, enabled: bool = True,
                 raises: bool = False) -> None:
        self._facilities = facilities or []
        self.enabled = enabled
        self._raises = raises

    async def facilities_around(self, center: Any, radius_m: float) -> list[Any]:
        if self._raises:
            from app.services.noise_emission import NoiseEmissionAPIError

            raise NoiseEmissionAPIError("조회 실패")
        return list(self._facilities)


def _noise_facility(distance_m: float, se_name: str = "소음") -> Any:
    from app.services.noise_emission import NoiseEmissionFacility

    return NoiseEmissionFacility(
        facility_id=f"noise-{distance_m}",
        name="검증용 소음배출시설",
        coordinates=offset_coordinates(SITE_CENTER, distance_m, 0),
        region="전북특별자치도",
        sigungu="전주시",
        road_address="검증용 도로명",
        lotno_address="전주시 검증동 1-1",
        se_name=se_name,
        main_content="",
    )


def _factory_registry_bundle() -> LocalSourcesBundle:
    """factoryON 등록공장 PNU 가 적재된 상태의 로컬 원천 묶음."""

    return LocalSourcesBundle(
        factory_pnus=frozenset({"4511100000-1-00010000"}),
        factory_registry_loaded=True,
    )


class TestFactoryNoiseAnnotation:
    """LH 확정 2026-09-11: 소음배출 원장은 판정 근거가 아니라 등록공장 주석 전용.
    소음 신고만으로는 공장 후보를 만들지 않고, 등록공장과 같은 좌표일 때만 주석한다.
    """

    def _request(self) -> HazardReviewRequest:
        return build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=20
        )

    def _bundle_with_factory(self) -> LocalSourcesBundle:
        rec = LocalSourceRecord(
            source_id="factory_standard", record_id="F1", name="등록공장",
            coordinates=offset_coordinates(SITE_CENTER, 0, 10),
            status_class="active", pnu="4146125628108640000", origin="standard",
        )
        return LocalSourcesBundle(
            factory_registry_loaded=True,
            factory_pnus=frozenset({"4146125628108640000"}),
            factory_facilities=(rec,),
        )

    def test_noise_report_annotates_registered_factory(self) -> None:
        client = _FakeNoiseClient([_noise_facility(0)])  # 등록공장과 ~10m
        result = run_review(
            self._request(),
            noise_emission=client,
            local_sources=self._bundle_with_factory(),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        factory = category_for(result, "factory_registered")
        assert factory.status == "review_required", factory.note
        assert any(
            "소음배출 신고 있음" in f.classification_note for f in factory.facilities
        )

    def test_noise_only_is_not_a_factory_candidate(self) -> None:
        # 소음 신고만 있고 좌표 보유 등록공장이 없으면 공장 후보를 만들지 않는다.
        client = _FakeNoiseClient([_noise_facility(10)])
        result = run_review(
            self._request(),
            noise_emission=client,
            local_sources=_factory_registry_bundle(),
        )
        factory = category_for(result, "factory_registered")
        assert factory.candidate_count == 0
        assert factory.status == "dataset_missing", factory.note

    def test_vibration_only_record_is_not_annotated(self) -> None:
        # 진동-only 레코드는 소음배출시설이 아니므로 주석하지 않는다.
        client = _FakeNoiseClient([_noise_facility(0, se_name="진동")])
        result = run_review(
            self._request(),
            noise_emission=client,
            local_sources=self._bundle_with_factory(),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        factory = category_for(result, "factory_registered")
        assert not any(
            "소음배출 신고 있음" in f.classification_note for f in factory.facilities
        )






# ---------------------------------------------------------------------------
# 고압가스(라·바목) 자가설비·업태 필터 — docs/hazards H-02-라바 §7-1·§7-2
# ---------------------------------------------------------------------------
class TestHighPressureGasFilter:
    def _run(self, rows: list[Any]) -> Any:
        request = build_request("house", "general")
        result = run_review(request, facility_store=FakeFacilityStore(rows))
        return category_for(result, "high_pressure_gas")

    def test_refrigeration_self_use_is_not_judged(self) -> None:
        # 전주우체국 냉방기 같은 「제조/냉동」 설비는 제2호 공통 전제(자가설비 제외)로
        # 판정 미적용이다. 후보에서 빠지고 note 에 건수를 남긴다.
        category = self._run([
            stored_facility(
                "high_pressure_gas", "전주우체국", 0, 20, category="제조",
                extra={"MNFTR_SE_NM": "냉동", "BPLC_SIE_USG_SE_NM": "상업업무용"},
            )
        ])
        assert category.candidate_count == 0
        assert category.status == "no_conflict_in_snapshot", category.note
        assert "자가용 냉동설비 1건" in category.note

    def test_storage_and_retail_are_confirmable(self) -> None:
        # 업태 「저장소」·「판매」는 바목 문언에 해당해 확정 가능(점 좌표라 경계 미확보).
        category = self._run([
            stored_facility("high_pressure_gas", "가스저장소", 0, 20, category="저장소"),
        ])
        assert category.candidate_count == 1
        assert category.facilities[0].metadata["gas_type_confirmed"] is True
        assert category.status == "geometry_missing", category.note

    def test_manufacture_without_refrigeration_stays_review(self) -> None:
        # 「제조/일반·충전·공란」은 바목(충전소·판매소·저장소) 해당 여부가 미확정이라
        # 확정하지 않고 검토로 남긴다.
        category = self._run([
            stored_facility(
                "high_pressure_gas", "일반제조업소", 0, 20, category="제조",
                extra={"MNFTR_SE_NM": "일반"},
            ),
        ])
        assert category.candidate_count == 1
        assert category.status == "review_required", category.note
        assert "H-02-라바" in category.note

    def test_city_gas_company_is_confirmable(self) -> None:
        category = self._run([
            stored_facility("city_gas_companies", "전북도시가스", 0, 20, category="일반도시가스"),
        ])
        assert category.facilities[0].metadata["gas_type_confirmed"] is True

    def test_specific_high_pressure_gas_stays_review(self) -> None:
        # 특정고압가스는 사용신고 성격이라 목 배정이 미확정(§7-3) — 확정하지 않는다.
        category = self._run([
            stored_facility("specific_high_pressure_gas", "사용신고업체", 0, 20, category=""),
        ])
        assert category.status == "review_required", category.note

    def test_legacy_rows_without_extra_are_not_treated_as_self_use(self) -> None:
        # 재적재 전 구 스키마 행(extra 없음)은 제조구분을 알 수 없으므로 빼지 않는다.
        category = self._run([
            stored_facility("high_pressure_gas", "구적재", 0, 20, category="제조"),
        ])
        assert category.candidate_count == 1


# ---------------------------------------------------------------------------
# 테마파크(다목) 건축물용도 필터 — docs/hazards H-04-다 §6-1
# ---------------------------------------------------------------------------
class TestThemeParkBuildingUse:
    def test_sports_facility_use_is_dropped(self) -> None:
        request = build_request("house", "multi_child")
        store = FakeFacilityStore([
            stored_facility(
                "amusement_facilities_other", "볼링장테마", 0, 10, category="기타테마파크업",
                extra={"BLDG_USG_NM": "체육시설"},
            ),
            stored_facility(
                "amusement_facilities_other", "빈칸테마", 0, -10, category="기타테마파크업",
            ),
        ])
        result = run_review(request, facility_store=store)
        category = category_for(result, "theme_park_other")
        names = {f.name for f in category.facilities}
        assert names == {"빈칸테마"}
        assert "체육시설" in category.note
        # 건축물대장 미연결이라 남은 후보는 검토 상한이다.
        assert category.status == "review_required"

    def test_neighborhood_use_is_marked_not_dropped(self) -> None:
        # 「근린생활시설」은 제2종 여부를 따로 확인해야 하므로 빼지 않고 표시만 한다.
        request = build_request("house", "multi_child")
        store = FakeFacilityStore([
            stored_facility(
                "amusement_facilities_other", "근생테마", 0, 10, category="기타테마파크업",
                extra={"BLDG_USG_NM": "근린생활시설"},
            ),
        ])
        result = run_review(request, facility_store=store)
        category = category_for(result, "theme_park_other")
        assert category.candidate_count == 1
        assert category.facilities[0].metadata["bldg_use_neighborhood"] == "근린생활시설"


# ---------------------------------------------------------------------------
# 무도장·무도학원 이중 등록 병합 — docs/hazards H-04-마 §6-5
# ---------------------------------------------------------------------------
class TestDanceDuplicateMerge:
    def test_same_location_registrations_collapse_to_one(self) -> None:
        request = build_request("house", "multi_child")
        store = FakeFacilityStore([
            stored_facility("dance_halls", "전주 중앙댄스 아카데미", 0, 10, category="무도장업"),
            stored_facility("dance_academies", "전주 중앙 댄스아카데미", 0, 10, category="무도학원업"),
            stored_facility("dance_academies", "다른무도학원", 0, -12, category="무도학원업"),
        ])
        result = run_review(request, facility_store=store)
        hall = category_for(result, "dance_hall")
        academy = category_for(result, "dance_academy")
        assert hall.candidate_count == 1
        assert hall.facilities[0].metadata["merged_registration"] == "전주 중앙 댄스아카데미"
        assert "이중 등록" in hall.facilities[0].facility_type_label
        assert {f.name for f in academy.facilities} == {"다른무도학원"}

    def test_distinct_locations_are_kept_separately(self) -> None:
        request = build_request("house", "multi_child")
        store = FakeFacilityStore([
            stored_facility("dance_halls", "무도장A", 0, 10, category="무도장업"),
            stored_facility("dance_academies", "무도학원B", 0, 15, category="무도학원업"),
        ])
        result = run_review(request, facility_store=store)
        assert category_for(result, "dance_hall").candidate_count == 1
        assert category_for(result, "dance_academy").candidate_count == 1




# 참고 시설(nearby_facilities) — 판정창 밖·참고 반경 이내 지도 표시 전용
# ---------------------------------------------------------------------------
class TestNearbyFacilities:
    """판정창(임계+버퍼) 밖·참고 반경 이내 시설은 nearby_facilities 에만 실린다.

    핵심 계약: status·candidate_count·measured_distance_m·finding.facilities 는
    참고 시설을 얹기 전과 완전히 같아야 한다(판정 결과 불변). 참고 반경 밖 시설은
    어디에도 실리지 않고, 참고 시설은 경계 조회를 하지 않아 geometry 가 비어 있다.
    """

    def _lodging_finding(self, result: HazardReviewResult):
        return next(f for f in result.findings if f.rule_id == "RB14-LODGING")

    def _run(self, rows: list[Any]) -> HazardReviewResult:
        # 다자녀 일반숙박 임계 25m → 판정창(search_limit) 125m, 참고 반경 1000m.
        request = build_request(
            "house", "multi_child", geometry_source="parcel_polygon", half_size_m=18
        )
        return run_review(
            request,
            facility_store=FakeFacilityStore(rows),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )

    def test_nearby_facility_does_not_change_verdict(self) -> None:
        # 판정 후보 1건만 있는 기준 판정과, 여기에 참고 시설·반경 밖 시설을 더한
        # 판정을 비교한다. 판정 결과(status·집계·근거 시설)는 한 톨도 달라지면 안 된다.
        candidate_only = self._run(
            [stored_facility("lodgings", "판정후보모텔", 0, 60)]
        )
        with_nearby = self._run(
            [
                stored_facility("lodgings", "판정후보모텔", 0, 60),
                stored_facility("lodgings", "참고반경모텔", 0, 300),
                stored_facility("lodgings", "반경밖모텔", 0, 1300),
            ]
        )
        base = category_for(candidate_only, "general_lodging")
        mixed = category_for(with_nearby, "general_lodging")
        assert mixed.status == base.status
        assert mixed.candidate_count == base.candidate_count
        assert mixed.inside_threshold_count == base.inside_threshold_count
        assert mixed.nearest_distance_m == base.nearest_distance_m

        base_finding = self._lodging_finding(candidate_only)
        mixed_finding = self._lodging_finding(with_nearby)
        assert mixed_finding.measured_distance_m == base_finding.measured_distance_m
        assert [f.facility_id for f in mixed_finding.facilities] == [
            f.facility_id for f in base_finding.facilities
        ]
        # 판정 후보에는 참고 시설이 섞이지 않는다.
        candidate_names = {f.name for f in mixed_finding.facilities}
        assert "참고반경모텔" not in candidate_names
        assert "반경밖모텔" not in candidate_names

    def test_nearby_only_holds_context_radius_facility(self) -> None:
        result = self._run(
            [
                stored_facility("lodgings", "판정후보모텔", 0, 60),
                stored_facility("lodgings", "참고반경모텔", 0, 300),
                stored_facility("lodgings", "반경밖모텔", 0, 1300),
            ]
        )
        finding = self._lodging_finding(result)
        nearby_names = {f.name for f in finding.nearby_facilities}
        # 참고 반경 이내는 참고 시설로, 반경 밖은 어디에도 없다.
        assert "참고반경모텔" in nearby_names
        assert "반경밖모텔" not in nearby_names
        # 반경 밖 시설은 판정 후보에도 없다.
        assert "반경밖모텔" not in {f.name for f in finding.facilities}

    def test_nearby_facility_gets_boundary_too(self) -> None:
        # 참고 시설도 경계를 붙여 경계↔경계 거리로 보여 준다(2026-09-14). 종전엔
        # 점 좌표로 뒀는데 지도의 선이 시설 영역 안 점까지 들어가 오해를 낳았다.
        # 판정은 바뀌지 않는다(위 test_nearby_does_not_change_verdict).
        result = self._run(
            [
                stored_facility("lodgings", "판정후보모텔", 0, 60),
                stored_facility("lodgings", "참고반경모텔", 0, 300),
            ]
        )
        finding = self._lodging_finding(result)
        nearby = next(f for f in finding.nearby_facilities if f.name == "참고반경모텔")
        assert len(nearby.geometry) >= 4
        assert nearby.geometry_type == "polygon"
        # 경계까지 재므로 점 거리(300m)보다 짧아진다(가짜 필지 반폭 5m).
        assert nearby.distance_m < 300


# ---------------------------------------------------------------------------
# 심사표 「데이터」 열 — 판정에 실제로 붙은 원천 칩 (API/로컬 구분)
# ---------------------------------------------------------------------------
# 이 앱의 목적은 로컬 납품 파일을 무시하고 공개 API 로 교차검증하는 것이라
# (docs/COORDINATES_PRIMER_2026-09-13 A-1·A-2), 「이 항목이 API 로 판정됐는가,
# 로컬로 판정됐는가」가 한눈에 구분돼야 한다. data_sources 가 그 근거다.
class _FakeEnabledClient:
    """opinet·safemap·kgs·crematorium 준비 술어가 보는 것은 .enabled 뿐이다."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled


def _data_source_service(demo_mode: bool = False, **kwargs: Any) -> HazardReviewService:
    return HazardReviewService(
        kakao=FakeKakaoClient(), demo_mode=demo_mode, **kwargs  # type: ignore[arg-type]
    )


def _factory_record() -> Any:
    from app.services.local_sources import LocalSourceRecord

    return LocalSourceRecord(
        source_id="factory_standard", record_id="F1", name="등록공장",
        coordinates=SITE_CENTER, status_class="active", pnu="x", origin="standard",
    )


def _cng_record() -> Any:
    from app.services.local_sources import LocalSourceRecord

    return LocalSourceRecord(
        source_id="cng_stations", record_id="C1", name="CNG충전소",
        coordinates=SITE_CENTER, status_class="active", origin="raw",
    )


class TestCategoryDataSources:
    def test_localdata_category_emits_api_source_with_endpoint_url(self) -> None:
        # (a) LOCALDATA 연결 카테고리는 데이터셋 라벨·원천 URL 을 가진 api 칩을 낸다.
        store = FakeFacilityStore(
            [stored_facility("high_pressure_gas", "행복충전소", 0, 20)]
        )
        service = _data_source_service(facility_store=store)
        sources = service._category_data_sources(CATEGORY_BY_KEY["high_pressure_gas"])
        assert [s.kind for s in sources] == ["api"]
        chip = sources[0]
        assert chip.detail == "high_pressure_gas"
        assert chip.url == "https://apis.data.go.kr/1741000/high_pressure_gas/info"
        assert "고압가스업" in chip.label

    def test_gas_station_lists_localdata_then_opinet_then_safemap(self) -> None:
        # 주유소는 석유판매업 원장(LOCALDATA) + 오피넷 + 생활안전지도. API 먼저.
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "가나주유소", 0, 10)]
        )
        service = _data_source_service(
            facility_store=store,
            opinet=_FakeEnabledClient(),
            safemap=_FakeEnabledClient(),
        )
        sources = service._category_data_sources(CATEGORY_BY_KEY["gas_station"])
        assert [s.kind for s in sources] == ["api", "api", "api"]
        assert [s.detail for s in sources] == ["oil_retailers", "opinet", "safemap"]

    def test_crematorium_emits_public_api_chip(self) -> None:
        service = _data_source_service(crematorium=_FakeEnabledClient())
        sources = service._category_data_sources(CATEGORY_BY_KEY["crematorium"])
        assert [s.kind for s in sources] == ["api"]
        assert sources[0].detail == "crematorium"

    def test_factory_ledger_chip_uses_standard_filename_when_loaded_from_standard(
        self,
    ) -> None:
        # (b-표준) 표준본으로 적재됐으면 칩 detail 은 표준본 파일명(+짧은 구분)이다.
        # 원본 파일명(03_06_09_…)을 박지 않는다(Codex 리뷰).
        bundle = LocalSourcesBundle(
            factory_registry_loaded=True,
            factory_facilities=(_factory_record(),),
            source_files={
                "factory_registry": LocalSourceFile(
                    filename="facilities.xlsx",
                    origin="standard",
                    qualifier="표준 공장(PNU 보유)",
                )
            },
        )
        service = _data_source_service(local_sources=bundle)
        sources = service._category_data_sources(CATEGORY_BY_KEY["factory_registered"])
        assert [s.kind for s in sources] == ["local"]
        assert sources[0].label == "로컬"
        assert sources[0].url == ""
        assert sources[0].detail == "facilities.xlsx · 표준 공장(PNU 보유)"

    def test_factory_ledger_chip_uses_raw_filename_when_loaded_from_raw(self) -> None:
        # (b-원본) 원본에서 적재됐으면 칩 detail 은 원본 파일명이다(구분 없음).
        bundle = LocalSourcesBundle(
            factory_registry_loaded=True,
            factory_facilities=(_factory_record(),),
            source_files={
                "factory_registry": LocalSourceFile(
                    filename="03_06_09_factory_registry.xlsx",
                    origin="raw",
                )
            },
        )
        service = _data_source_service(local_sources=bundle)
        sources = service._category_data_sources(CATEGORY_BY_KEY["factory_registered"])
        assert sources[0].detail == "03_06_09_factory_registry.xlsx"

    def test_factory_ledger_chip_detail_empty_when_source_file_unknown(self) -> None:
        # 실제 적재 파일명을 모르면 하드코딩 대신 빈 detail → 칩은 "로컬"만.
        bundle = LocalSourcesBundle(
            factory_registry_loaded=True,
            factory_facilities=(_factory_record(),),
        )
        service = _data_source_service(local_sources=bundle)
        sources = service._category_data_sources(CATEGORY_BY_KEY["factory_registered"])
        assert [s.kind for s in sources] == ["local"]
        assert sources[0].detail == ""

    def test_cng_ledger_category_emits_local_chip(self) -> None:
        bundle = LocalSourcesBundle(
            cng_facilities=(_cng_record(),),
            source_files={
                "cng_stations": LocalSourceFile(
                    filename="25_cng_stations.csv", origin="raw"
                )
            },
        )
        service = _data_source_service(local_sources=bundle)
        sources = service._category_data_sources(CATEGORY_BY_KEY["cng_station"])
        assert [s.kind for s in sources] == ["local"]
        assert sources[0].detail == "25_cng_stations.csv"

    def test_cng_api_chip_precedes_local_chip(self) -> None:
        # API 먼저 규칙: 가스안전공사 CNG API 가 붙어 있으면 api 칩이 앞에, 로컬은 뒤에.
        bundle = LocalSourcesBundle(
            cng_facilities=(_cng_record(),),
            source_files={
                "cng_stations": LocalSourceFile(
                    filename="25_cng_stations.csv", origin="raw"
                )
            },
        )
        service = _data_source_service(
            local_sources=bundle, cng=_FakeEnabledClient()
        )
        sources = service._category_data_sources(CATEGORY_BY_KEY["cng_station"])
        assert [s.kind for s in sources] == ["api", "local"]
        assert "CNG" in sources[0].label
        assert sources[0].url.startswith("https://api.odcloud.kr/api/15001508/")

    def test_cng_api_chip_dropped_when_request_failed(self) -> None:
        service = _data_source_service(cng=_FakeEnabledClient())
        sources = service._category_data_sources(
            CATEGORY_BY_KEY["cng_station"], failed_sources={"cng"}
        )
        assert sources == []

    def test_cng_api_alone_counts_as_connected(self) -> None:
        service = _data_source_service(cng=_FakeEnabledClient())
        assert service._category_connected(CATEGORY_BY_KEY["cng_station"]) is True
        assert service._category_active_sources(CATEGORY_BY_KEY["cng_station"]) == {"cng"}

    def test_disconnected_category_emits_no_sources(self) -> None:
        # (c) 원천이 안 붙은 카테고리는 빈 리스트다(「데이터」 열 칸이 빈다).
        service = _data_source_service(facility_store=FakeFacilityStore([]))
        sources = service._category_data_sources(CATEGORY_BY_KEY["high_pressure_gas"])
        assert sources == []

    def test_demo_mode_emits_demo_chip(self) -> None:
        service = _data_source_service(demo_mode=True)
        sources = service._category_data_sources(CATEGORY_BY_KEY["high_pressure_gas"])
        assert [s.kind for s in sources] == ["demo"]
        assert sources[0].label == "데모"

    def test_full_review_passes_api_sources_through_when_connected(self) -> None:
        # 통합: 연결된 종류는 판정 결과에도 api 원천이 실려 나온다(URL 포함).
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        store = FakeFacilityStore(
            [stored_facility("high_pressure_gas", "행복충전소", 0, 20)]
        )
        result = run_review(
            request, facility_store=store, vworld=FakeVWorldForFacilities(half_size_m=5)
        )
        gas = category_for(result, "high_pressure_gas")
        assert gas.data_sources
        assert all(s.kind == "api" for s in gas.data_sources)
        assert all(s.url for s in gas.data_sources)

    def test_full_review_leaves_data_sources_empty_when_unconnected(self) -> None:
        # 통합: 미연결(dataset_missing) 종류는 빈 리스트로 나간다.
        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        result = run_review(request, facility_store=FakeFacilityStore([]))
        gas = category_for(result, "high_pressure_gas")
        assert gas.status == "dataset_missing"
        assert gas.data_sources == []

    def test_failed_provider_chip_is_dropped(self) -> None:
        # (a) 오피넷 성공 + 생활안전지도(safemap) 실패 시, 주유소 「데이터」 열에는
        # 실패한 safemap 칩이 빠지고 오피넷·석유판매업 원장(localdata) 칩은 남는다.
        # 「데이터」 열은 이 판정에 실제로 붙은 원천만 보이므로 실패 원천은 뺀다.
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "가나주유소", 0, 10)]
        )
        service = _data_source_service(
            facility_store=store,
            opinet=_FakeEnabledClient(),
            safemap=_FakeEnabledClient(),
        )
        sources = service._category_data_sources(
            CATEGORY_BY_KEY["gas_station"], failed_sources={"safemap"}
        )
        assert [s.detail for s in sources] == ["oil_retailers", "opinet"]
        assert all(s.detail != "safemap" for s in sources)

    def test_failed_localdata_drops_all_localdata_chips_provider_unit(self) -> None:
        # (b) 실패 추적 단위는 데이터셋이 아니라 provider('localdata') 전체다.
        # 그래서 localdata 조회가 실패하면 데이터셋별 칩이 통째로 빠지고, 실패와
        # 무관한 오피넷·safemap 칩만 남는다(provider 단위 제외를 검증).
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "가나주유소", 0, 10)]
        )
        service = _data_source_service(
            facility_store=store,
            opinet=_FakeEnabledClient(),
            safemap=_FakeEnabledClient(),
        )
        sources = service._category_data_sources(
            CATEGORY_BY_KEY["gas_station"], failed_sources={"localdata"}
        )
        assert [s.detail for s in sources] == ["opinet", "safemap"]
        assert all(s.detail != "oil_retailers" for s in sources)

    def test_single_provider_failure_empties_data_sources(self) -> None:
        # 단일 원천(localdata) 종류가 그 원천 조회에 실패하면 목록이 빈다.
        # 실패는 상태 배지로 이미 드러나므로 여기서 '실패' 칩을 새로 만들지 않는다.
        store = FakeFacilityStore(
            [stored_facility("high_pressure_gas", "행복충전소", 0, 20)]
        )
        service = _data_source_service(facility_store=store)
        sources = service._category_data_sources(
            CATEGORY_BY_KEY["high_pressure_gas"], failed_sources={"localdata"}
        )
        assert sources == []

    def test_full_review_drops_failed_provider_chip_but_keeps_others(self) -> None:
        # 통합: 오피넷 양성 매치 + safemap 실패면 판정은 유지(양성)되지만
        # gas_station.data_sources 에는 safemap 칩이 빠지고 오피넷·석유판매업
        # 원장 칩은 남는다.
        from app.services.safemap import SafemapAPIError

        class RaisingSafemapClient:
            enabled = True

            async def stations_around_cached(
                self, center: Coordinates, radius_m: float
            ) -> list[Any]:
                raise SafemapAPIError("일시 오류")

        request = build_request(
            "house", "general", geometry_source="parcel_polygon", half_size_m=30
        )
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "가나주유소", 0, 10)]
        )
        result = run_review(
            request,
            facility_store=store,
            opinet=FakeOpinetClient([opinet_station("근접주유소", 0, 20)]),
            safemap=RaisingSafemapClient(),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        gas = category_for(result, "gas_station")
        assert gas.status == "exclusion_match", gas.note
        details = [s.detail for s in gas.data_sources]
        assert "safemap" not in details
        assert "opinet" in details
        assert "oil_retailers" in details
