"""완성 어댑터 3종(연속지적도·로컬 원천·건축물대장)과 safemap 콜드스타트 배선 회귀.

핵심 불변식을 고정한다.
- 확인불가(건축물대장)는 비해당으로 취급해 매입제외로 승격하지 않는다.
- 캐시·인덱스 미준비가 '충돌 없음'으로 둔갑하지 않는다.
- 로컬 지적도 미적재 시 시설 경계 폴백이 점 좌표로 정직하게 남는다.
- factoryON PNU 집합으로 공장 AND 가 실제로 성립/미성립한다.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime
from typing import Any

import httpx

import app.hazard_review.service as hazard_service_module

from app.hazard_review.models import (
    HazardParcel,
    HazardReviewRequest,
    HazardReviewResult,
    HazardSite,
)
from app.hazard_review.service import HazardReviewService, offset_coordinates
from app.models import Coordinates
from app.services.building_register import (
    BuildingUse,
    BuildingUseResult,
    UseVerdict,
    parse_pnu,
)
from app.services.facility_store import StoredFacility
from app.services.local_sources import LocalSourceRecord
from app.services.local_wiring import LocalSourcesBundle
from app.services.safemap import SafemapFuelClient
from app.services.vworld import ParcelFeature


SITE_CENTER = Coordinates(lat=37.40111, lng=127.10853)
FACILITY_PNU = "4146125628108640000"  # FakeVWorld/Cadastral 이 돌려주는 PNU


# ---------------------------------------------------------------------------
# 공용 도구 (self-contained)
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
    application: str = "general",
    geometry_source: str = "parcel_polygon",
    half_size_m: float = 30,
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
                    pnu="",
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
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.available = bool(self.rows)

    def facilities_around(self, center, radius_m, dataset_keys):
        keys = set(dataset_keys)
        return [row for row in self.rows if row.dataset_key in keys]

    def ready_datasets(self) -> set[str]:
        return {row.dataset_key for row in self.rows}

    def latest_sync(self) -> None:
        return None


def stored_facility(
    dataset_key: str, name: str, north_m: float, east_m: float,
    status: str = "영업/정상", category: str = "",
) -> StoredFacility:
    coordinates = offset_coordinates(SITE_CENTER, north_m, east_m)
    return StoredFacility(
        dataset_key=dataset_key, record_id=f"rec-{name}", name=name,
        address="지번주소", road_address="도로명주소", coordinates=coordinates,
        status=status, category=category, distance_m=abs(north_m) + abs(east_m),
    )


class FakeVWorldForFacilities:
    def __init__(self, half_size_m: float = 5) -> None:
        self.half_size_m = half_size_m
        self.enabled = True

    async def parcel_at(self, lat: float, lng: float):
        centre = Coordinates(lat=lat, lng=lng)
        return ParcelFeature(
            pnu=FACILITY_PNU, address="시설 필지", jibun="864",
            ring=square_ring(self.half_size_m, centre),
            area_m2=(2 * self.half_size_m) ** 2,
        )

    async def zoning_at(self, lat: float, lng: float):
        # 용도지역은 석유대체연료 판매업에만 붙는다. 배선 테스트에는 해당 후보가
        # 없어 호출되지 않지만, 인터페이스를 맞춰 둔다(미확인 → None).
        return None


class FakeCadastralStore:
    """연속지적도 로컬 인덱스 스텁. status().available·parcel_at 만 쓴다."""

    def __init__(self, half_size_m: float = 5, available: bool = True) -> None:
        self.half_size_m = half_size_m
        self._available = available

    def status(self) -> Any:
        available = self._available

        class _Status:
            pass

        s = _Status()
        s.available = available
        return s

    def parcel_at(self, lat: float, lng: float) -> Any | None:
        if not self._available:
            return None

        class _Local:
            pass

        local = _Local()
        local.pnu = FACILITY_PNU
        local.jibun = "864"
        local.address = ""
        local.ring = square_ring(self.half_size_m, Coordinates(lat=lat, lng=lng))
        local.area_m2 = (2 * self.half_size_m) ** 2
        return local


class FakeBuildingRegister:
    """건축물대장 표제부 교차확인 스텁. pnu→(제2종근생·운동시설·주용도·기타용도)."""

    def __init__(self, verdicts: dict[str, tuple], fail: bool = False) -> None:
        self.verdicts = verdicts
        self.fail = fail
        self.looked_up: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def lookup_many(self, pnus: list[str]) -> dict[str, Any]:
        from app.services.building_register import BuildingRegisterAPIError

        self.looked_up = list(pnus)
        if self.fail:
            raise BuildingRegisterAPIError("조회 실패")
        out: dict[str, Any] = {}
        for pnu in pnus:
            spec = self.verdicts.get(pnu)
            if spec is None:
                continue
            second, sports, main, etc = spec
            uses = (BuildingUse(dong_name="", main_purpose=main, etc_purpose=etc),)
            out[pnu] = BuildingUseResult(
                pnu=pnu, params=parse_pnu(pnu), uses=uses,
                second_class_neighborhood=second, sports_facility=sports,
            )
        return out


def run_review(request: HazardReviewRequest, **kwargs: Any) -> HazardReviewResult:
    service = HazardReviewService(
        kakao=FakeKakaoClient(),  # type: ignore[arg-type]
        demo_mode=False,
        **kwargs,
    )

    async def progress(*_args: Any) -> None:
        return None

    return asyncio.run(service.review(request, progress, asyncio.Event()))


def category_for(result: HazardReviewResult, key: str):
    return next(c for c in result.categories if c.key == key)


def factory_record(pnu: str, north_m: float, east_m: float) -> LocalSourceRecord:
    return LocalSourceRecord(
        source_id="factory_standard", record_id=f"F-{north_m}-{east_m}",
        name="등록공장", coordinates=offset_coordinates(SITE_CENTER, north_m, east_m),
        status_class="active", pnu=pnu, origin="standard",
    )


def cng_record(north_m: float, east_m: float) -> LocalSourceRecord:
    return LocalSourceRecord(
        source_id="cng_stations", record_id=f"C-{north_m}-{east_m}",
        name="CNG충전소", coordinates=offset_coordinates(SITE_CENTER, north_m, east_m),
        status_class="active", origin="raw",
    )


# ---------------------------------------------------------------------------
# factoryON PNU AND / 공장 인접 / CNG
# ---------------------------------------------------------------------------
class TestFactoryRegistryWiring:
    """LH 확정 2026-09-11 (안건 ①): 등록공장 소재만 「공장 있음」 검토 표시.
    대기배출·소음 원장은 판정 근거가 아니라 부가 정보(주석) 전용이다.
    """

    def test_registered_factory_within_50m_is_present_review(self) -> None:
        bundle = LocalSourcesBundle(
            factory_registry_loaded=True,
            factory_pnus=frozenset({FACILITY_PNU}),
            factory_facilities=(factory_record(FACILITY_PNU, 0, 20),),
        )
        result = run_review(
            build_request(), local_sources=bundle,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        factory = category_for(result, "factory_registered")
        assert factory.candidate_count >= 1
        assert factory.status == "review_required", factory.note
        assert "공장 있음" in factory.note
        factory_categories = [
            c for c in result.categories if c.rule_id == "RB14-FACTORY"
        ]
        assert all(c.status != "exclusion_match" for c in factory_categories)

    def test_air_emission_alone_is_not_a_factory_candidate(self) -> None:
        # 대기배출 사업장만 있고 좌표 보유 등록공장이 없으면 공장 후보를 만들지 않는다
        # (우체국 냉방기·홈플러스 보일러 오검출 방지, LH 확정 2026-09-11).
        store = FakeFacilityStore(
            [stored_facility("air_pollution", "대기1종공장", 0, 25, category="대기1종")]
        )
        bundle = LocalSourcesBundle(
            factory_pnus=frozenset({FACILITY_PNU}), factory_registry_loaded=True,
        )
        result = run_review(
            build_request(), facility_store=store, local_sources=bundle,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        factory = category_for(result, "factory_registered")
        assert factory.status == "dataset_missing", factory.note
        assert factory.candidate_count == 0

    def test_factory_registry_missing_is_dataset_missing(self) -> None:
        result = run_review(
            build_request(), local_sources=LocalSourcesBundle(),
        )
        factory = category_for(result, "factory_registered")
        assert factory.status == "dataset_missing"
        assert "등록공장 원천 없음" in factory.note

    def test_registered_factory_gets_air_emission_annotation(self) -> None:
        # 등록공장과 같은 좌표(40m 이내)에 대기배출 신고가 있으면 부가 정보로 주석한다.
        # 판정은 여전히 「공장 있음」 검토이며 매입제외로 올리지 않는다.
        store = FakeFacilityStore(
            [stored_facility("air_pollution", "대기2종공장", 0, 20, category="대기2종")]
        )
        bundle = LocalSourcesBundle(
            factory_registry_loaded=True,
            factory_pnus=frozenset({FACILITY_PNU}),
            factory_facilities=(factory_record(FACILITY_PNU, 0, 20),),
        )
        result = run_review(
            build_request(), facility_store=store, local_sources=bundle,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        factory = category_for(result, "factory_registered")
        assert factory.status == "review_required", factory.note
        assert any(
            "대기배출 신고 있음" in f.classification_note for f in factory.facilities
        )

    def test_cng_from_local_source_is_wired(self) -> None:
        bundle = LocalSourcesBundle(cng_facilities=(cng_record(0, 20),))
        result = run_review(
            build_request(), local_sources=bundle,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        cng = category_for(result, "cng_station")
        assert cng.candidate_count >= 1
        assert cng.status in ("exclusion_match", "geometry_missing")

    def test_cng_without_local_source_stays_dataset_missing(self) -> None:
        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(),
        )
        cng = category_for(result, "cng_station")
        assert cng.status == "dataset_missing"


# ---------------------------------------------------------------------------
# 건축물대장 AND (§6.4 단란주점·테마파크)
# ---------------------------------------------------------------------------
def _singing_request_store():
    request = build_request("house", "multi_child")
    store = FakeFacilityStore(
        [stored_facility("singing_bars", "단란주점A", 0, 20, category="단란주점")]
    )
    return request, store


class TestBuildingRegisterWiring:
    def test_singing_bar_not_second_class_reaches_exclusion(self) -> None:
        request, store = _singing_request_store()
        br = FakeBuildingRegister(
            {FACILITY_PNU: (UseVerdict.NOT_APPLICABLE, UseVerdict.UNKNOWN,
                            "위락시설", "단란주점")}
        )
        result = run_review(
            request, facility_store=store, building_register=br,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        singing = category_for(result, "singing_bar")
        assert singing.status == "exclusion_match", singing.note
        assert br.looked_up == [FACILITY_PNU]

    def test_singing_bar_unknown_stays_review_not_exclusion(self) -> None:
        request, store = _singing_request_store()
        br = FakeBuildingRegister(
            {FACILITY_PNU: (UseVerdict.UNKNOWN, UseVerdict.UNKNOWN, "", "")}
        )
        result = run_review(
            request, facility_store=store, building_register=br,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        singing = category_for(result, "singing_bar")
        assert singing.status == "review_required", singing.note
        assert singing.status != "exclusion_match"

    def test_singing_bar_second_class_is_no_conflict(self) -> None:
        request, store = _singing_request_store()
        br = FakeBuildingRegister(
            {FACILITY_PNU: (UseVerdict.APPLICABLE, UseVerdict.UNKNOWN,
                            "제2종근린생활시설", "")}
        )
        result = run_review(
            request, facility_store=store, building_register=br,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        singing = category_for(result, "singing_bar")
        assert singing.status == "no_conflict_in_snapshot", singing.note

    def test_building_register_error_stays_review(self) -> None:
        request, store = _singing_request_store()
        br = FakeBuildingRegister({}, fail=True)
        result = run_review(
            request, facility_store=store, building_register=br,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        singing = category_for(result, "singing_bar")
        assert singing.status == "review_required", singing.note
        assert singing.status != "exclusion_match"

    def test_theme_park_both_not_applicable_reaches_exclusion(self) -> None:
        request = build_request("house", "multi_child")
        store = FakeFacilityStore(
            [stored_facility("general_amusement_facilities", "일반테마", 0, 20)]
        )
        br = FakeBuildingRegister(
            {FACILITY_PNU: (UseVerdict.NOT_APPLICABLE, UseVerdict.NOT_APPLICABLE,
                            "위락시설", "")}
        )
        result = run_review(
            request, facility_store=store, building_register=br,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        general = category_for(result, "theme_park_general")
        assert general.status == "exclusion_match", general.note

    def test_theme_park_sports_unknown_stays_review(self) -> None:
        request = build_request("house", "multi_child")
        store = FakeFacilityStore(
            [stored_facility("general_amusement_facilities", "일반테마", 0, 20)]
        )
        br = FakeBuildingRegister(
            {FACILITY_PNU: (UseVerdict.NOT_APPLICABLE, UseVerdict.UNKNOWN,
                            "위락시설", "")}
        )
        result = run_review(
            request, facility_store=store, building_register=br,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        general = category_for(result, "theme_park_general")
        assert general.status == "review_required", general.note
        assert general.status != "exclusion_match"

    def test_no_building_register_client_keeps_review(self) -> None:
        request, store = _singing_request_store()
        result = run_review(
            request, facility_store=store,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        singing = category_for(result, "singing_bar")
        assert singing.status == "review_required"
        assert "건축물대장" in singing.note


# ---------------------------------------------------------------------------
# 시설 필지 경계 로컬 지적도 폴백
# ---------------------------------------------------------------------------
class TestCadastralFacilityFallback:
    def test_cadastral_attaches_facility_polygon_when_no_vworld(self) -> None:
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "가까운석유", 0, 20, category="석유")]
        )
        result = run_review(
            build_request(), facility_store=store, vworld=None,
            cadastral=FakeCadastralStore(half_size_m=5),
        )
        oil = category_for(result, "oil_retailer")
        assert oil.facilities and oil.facilities[0].geometry_type == "polygon"
        assert oil.status == "exclusion_match", oil.note

    def test_missing_cadastral_index_keeps_point_fallback(self) -> None:
        store = FakeFacilityStore(
            [stored_facility("oil_retailers", "가까운석유", 0, 20, category="석유")]
        )
        result = run_review(
            build_request(), facility_store=store, vworld=None,
            cadastral=FakeCadastralStore(available=False),
        )
        oil = category_for(result, "oil_retailer")
        assert oil.status == "geometry_missing", oil.note
        assert all(f.geometry_type == "point" for f in oil.facilities)


# ---------------------------------------------------------------------------
# safemap 콜드스타트는 '충돌 없음'으로 둔갑하지 않는다
# ---------------------------------------------------------------------------
class TestSafemapColdStartInReview:
    def test_cold_safemap_only_source_is_dataset_missing(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                    "body": {"totalCount": 0, "items": {"item": []}},
                },
            )

        safemap = SafemapFuelClient(
            service_key="k", transport=httpx.MockTransport(handler)
        )
        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            safemap=safemap,
        )
        gas = category_for(result, "gas_station")
        assert gas.status == "dataset_missing", gas.note
        assert gas.status != "no_conflict_in_snapshot"


# ---------------------------------------------------------------------------
# 여유구간(BOUNDARY_BUFFER_M) 양성 대조 — 네트워크를 타지 않는 결정적 테스트
# ---------------------------------------------------------------------------
class TestBoundaryBufferPositiveControl:
    """여유구간 배관이 살아 있음을 증명한다(팀 안건 ⑤ 선행조건).

    기준거리(FUEL25 일반 25m) 밖·여유구간 안에 놓은 가짜 주유소가, 여유구간을
    바꿨을 때 판정을 review_required ↔ no_conflict_in_snapshot 로 실제로 뒤집는지
    본다. 시설 경계-대-점 거리 약 89.7m 지점이라 여유구간 100m 면 잡히고 50m 면
    빠진다. 라이브 5,000m 양성 대조가 오피넷 노이즈에 묻혀 폐기됐으므로 이 테스트가
    배관 생존을 대신 증명한다(2026-08-30).
    """

    def _store(self) -> FakeFacilityStore:
        # 사업지 경계에서 약 89.7m 떨어진 여유구간 주유소 한 곳.
        return FakeFacilityStore(
            [stored_facility("oil_retailers", "여유구간주유소", 120, 0)]
        )

    def test_module_global_reassignment_flips_verdict(self) -> None:
        # --buffers 가 쓰는 경로: 모듈 전역 BOUNDARY_BUFFER_M 을 런타임에 재대입한다.
        store = self._store()
        original = hazard_service_module.BOUNDARY_BUFFER_M
        try:
            hazard_service_module.BOUNDARY_BUFFER_M = 100
            wide = run_review(build_request(), facility_store=store)
            assert category_for(wide, "oil_retailer").status == "review_required"

            hazard_service_module.BOUNDARY_BUFFER_M = 50
            narrow = run_review(build_request(), facility_store=store)
            assert (
                category_for(narrow, "oil_retailer").status
                == "no_conflict_in_snapshot"
            )
        finally:
            hazard_service_module.BOUNDARY_BUFFER_M = original

    def test_env_var_path_flips_verdict(self, monkeypatch) -> None:
        # 환경변수 경로: HAZARD_BOUNDARY_BUFFER_M 을 임포트 시점에 읽어 전역에 넣는다.
        # 두 스냅샷이 완전히 같게 나오는 「영향 0」과 「플래그 미반영」을 이 테스트가 가른다.
        store = self._store()
        original = hazard_service_module.BOUNDARY_BUFFER_M
        try:
            monkeypatch.setenv("HAZARD_BOUNDARY_BUFFER_M", "100")
            importlib.reload(hazard_service_module)
            assert hazard_service_module.BOUNDARY_BUFFER_M == 100

            async def prog(*_args: Any) -> None:
                return None

            wide_service = hazard_service_module.HazardReviewService(
                kakao=FakeKakaoClient(), demo_mode=False, facility_store=store,
            )
            wide = asyncio.run(
                wide_service.review(build_request(), prog, asyncio.Event())
            )
            assert category_for(wide, "oil_retailer").status == "review_required"

            monkeypatch.setenv("HAZARD_BOUNDARY_BUFFER_M", "50")
            importlib.reload(hazard_service_module)
            assert hazard_service_module.BOUNDARY_BUFFER_M == 50
            narrow_service = hazard_service_module.HazardReviewService(
                kakao=FakeKakaoClient(), demo_mode=False, facility_store=store,
            )
            narrow = asyncio.run(
                narrow_service.review(build_request(), prog, asyncio.Event())
            )
            assert (
                category_for(narrow, "oil_retailer").status
                == "no_conflict_in_snapshot"
            )
        finally:
            monkeypatch.delenv("HAZARD_BOUNDARY_BUFFER_M", raising=False)
            importlib.reload(hazard_service_module)
            hazard_service_module.BOUNDARY_BUFFER_M = original


def test_cli_and_app_build_service_with_identical_wiring():
    """대조 도구(run_ours)와 앱(router)이 같은 인자로 서비스를 조립하는지 고정한다.

    run_ours 가 서비스를 자체 조립하면서 앱이 넘기는 인자 7종(cadastral·safemap·
    crematorium·noise_emission·building_register·pnu_resolver·local_sources loader)을
    빠뜨려 공장·화장장·소음·시설경계 폴백이 통째로 빠진 스냅샷이 나온 사고가 있었다.
    조립 경로가 두 벌이면 재발하므로, 두 진입점이 만든 서비스의 배선이 동일함을
    회귀로 못 박는다.
    """

    from app.hazard_review import router as hazard_router
    import tools.lh_baseline.run_ours as run_ours

    hazard_router.get_hazard_service.cache_clear()
    app_service = hazard_router.get_hazard_service()
    cli_screening, _resolver, loader = run_ours.build_service()
    cli_service = cli_screening.hazard

    collaborators = [
        "opinet",
        "kgs_lpg",
        "facility_store",
        "vworld",
        "safemap",
        "crematorium",
        "cadastral",
        "building_register",
        "noise_emission",
        "pnu_resolver",
    ]
    app_wired = {name for name in collaborators if getattr(app_service, name) is not None}
    cli_wired = {name for name in collaborators if getattr(cli_service, name) is not None}
    assert app_wired == cli_wired

    # 과거에 누락됐던 7종은 두 경로 모두에서 반드시 배선돼야 한다.
    for name in (
        "cadastral",
        "safemap",
        "crematorium",
        "noise_emission",
        "building_register",
        "pnu_resolver",
    ):
        assert getattr(cli_service, name) is not None, name

    # 로컬 원천 묶음 loader 가 두 경로 모두에 있어야 공장 축을 채울 수 있다.
    assert getattr(app_service, "_local_sources_loader", None) is not None
    assert callable(loader)

    hazard_router.get_hazard_service.cache_clear()


# ---------------------------------------------------------------------------
# 생활안전지도 레이어(2026-09-17 승인) — 참고 핀·주석 전용, 판정은 바꾸지 않는다
# ---------------------------------------------------------------------------
class FakeLayerFeed:
    def __init__(self, rows, enabled: bool = True) -> None:
        from app.services.safemap_facilities import SafemapFacility

        self.enabled = enabled
        self.rows = [
            SafemapFacility("IF_TEST", f"{name}-{n}", name, "주소", kind,
                            offset_coordinates(SITE_CENTER, n, e))
            for name, n, e, kind in rows
        ]

    async def facilities_around(self, center, radius_m):
        return list(self.rows)


class TestSafemapReferenceLayers:
    def test_chemical_layer_becomes_reference_pin_without_changing_manual_status(self) -> None:
        feed = FakeLayerFeed([("연호전자", 0, 20, "화학제품 제조업")])
        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), chemical_feed=feed,
        )
        toxic = category_for(result, "toxic_substance")
        # LH [요청 2] 승인 상태 그대로 — 판정 미적용.
        assert toxic.status == "dataset_missing"
        assert toxic.manual_check_required
        assert toxic.candidate_count == 1
        pin = toxic.facilities[0]
        assert pin.facility_type == "chemical_handling" and pin.metadata["reference"]
        assert "참고 핀 1건" in toxic.note

    def test_gg_chemical_becomes_reference_pin_with_business_type_and_chip(self) -> None:
        # 경기데이터드림 유해화학물질 취급사업장 — 판매업 핀도 판정은 바꾸지 않는다.
        feed = FakeLayerFeed([("○○케미칼 판매점", 0, 20, "판매업")])
        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), gg_chemical=feed,
        )
        toxic = category_for(result, "toxic_substance")
        assert toxic.status == "dataset_missing"
        assert toxic.manual_check_required
        pin = toxic.facilities[0]
        assert pin.provider == "gg_chemical" and pin.metadata["reference"]
        assert pin.facility_type == "chemical_handling"
        assert "업종구분 판매업" in pin.classification_note
        assert "경기데이터드림" in pin.geometry_note
        assert "참고 핀 1건" in toxic.note and "경기데이터드림" in toxic.note
        # 참고 핀 원천은 「API 일부연결」(partial) 칩으로 판정 원천(api)과 구분해 낸다.
        assert [(s.kind, s.detail) for s in toxic.data_sources] == [("partial", "gg_chemical")]

    def test_gg_chemical_failure_is_recorded_without_changing_status(self) -> None:
        class FailingFeed:
            enabled = True

            async def facilities_around(self, center, radius_m):
                raise RuntimeError("WAF")

        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), gg_chemical=FailingFeed(),
        )
        toxic = category_for(result, "toxic_substance")
        assert toxic.status == "dataset_missing"
        assert toxic.facilities == []
        assert toxic.data_sources == []

    def test_logistics_warehouse_is_reference_pin_for_toxic(self) -> None:
        feed = FakeLayerFeed([("(주)경기화학물류", 0, 20, "보관·저장업(환경부 등록 창고)")])
        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), logistics_warehouse=feed,
        )
        toxic = category_for(result, "toxic_substance")
        assert toxic.status == "dataset_missing" and toxic.manual_check_required
        pin = toxic.facilities[0]
        assert pin.provider == "logistics_chem_warehouse" and pin.metadata["reference"]
        assert "보관·저장업" in pin.classification_note
        assert "물류창고업" in toxic.note

    def test_waste_layer_is_reference_pin_for_other_similar(self) -> None:
        feed = FakeLayerFeed([("○○환경 소각시설", 0, 20, "소각")])
        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), waste_feed=feed,
        )
        other = category_for(result, "hazmat_other_similar")
        assert other.status == "dataset_missing"
        assert [f.facility_type for f in other.facilities] == ["waste_treatment"]

    def test_emission_layer_annotates_registered_factory(self) -> None:
        bundle = LocalSourcesBundle(
            factory_registry_loaded=True,
            factory_pnus=frozenset({FACILITY_PNU}),
            factory_facilities=(factory_record(FACILITY_PNU, 0, 20),),
        )
        feed = FakeLayerFeed([("등록공장", 0, 20, "대기"), ("등록공장", 0, 25, "수질")])
        result = run_review(
            build_request(), local_sources=bundle, emission_feed=feed,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        factory = category_for(result, "factory_registered")
        assert factory.status == "review_required"
        note = factory.facilities[0].classification_note
        assert "대기배출 신고 있음(생활안전지도" in note and "수질배출 신고 있음" in note


# ---------------------------------------------------------------------------
# 카지노영업소 — 문체부 허가 18곳 명단(H-04-바 §8) · 바목 판정 원천
# ---------------------------------------------------------------------------
class FakeCasinoRegistry:
    def __init__(self, rows, enabled: bool = True) -> None:
        from app.services.casino_registry import Casino

        self.enabled = enabled
        self.rows = [
            Casino(f"C-{n}", name, "서울", "호텔", "주소", status,
                   offset_coordinates(SITE_CENTER, n, e))
            for name, n, e, status in rows
        ]

    async def casinos_around(self, center, radius_m):
        return list(self.rows)


class FakeLpgRetailerFile:
    def __init__(self, rows, enabled: bool = True) -> None:
        from app.services.lpg_retailer_file import LpgRetailer

        self.enabled = enabled
        self.rows = [
            LpgRetailer(f"R-{n}", name, "주소", offset_coordinates(SITE_CENTER, n, e))
            for name, n, e in rows
        ]

    async def retailers_around(self, center, radius_m):
        return list(self.rows)


class FakeLpgMunicipal:
    def __init__(self, rows, enabled: bool = True) -> None:
        from app.services.lpg_municipal import MunicipalFacility

        self.enabled = enabled
        self.rows = [
            MunicipalFacility("15064201", "전북특별자치도 부안군_액화석유가스업", f"M-{n}", name,
                              "주소", kind, kind_raw, "신규", offset_coordinates(SITE_CENTER, n, e))
            for name, n, e, kind, kind_raw in rows
        ]

    async def facilities_for_site(self, address, center, radius_m):
        from app.services.lpg_municipal import MunicipalLookup

        return MunicipalLookup(list(self.rows), ["15064201"], {}, [])


class TestLpgRetailerWiring:
    def test_national_file_retailer_within_50m_is_exclusion(self) -> None:
        result = run_review(
            build_request("house", "general"),
            local_sources=LocalSourcesBundle(),
            lpg_retailer_file=FakeLpgRetailerFile([("대륙가스", 0, 20)]),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        retailer = category_for(result, "lpg_retailer")
        assert retailer.status == "exclusion_match", retailer.note
        assert retailer.facilities[0].provider == "lpg_retailer_file"
        assert [s.detail for s in retailer.data_sources] == ["lpg_retailer_file"]

    def test_municipal_sales_row_judged_and_storage_row_is_reference_pin(self) -> None:
        municipal = FakeLpgMunicipal([
            ("부안가스", 0, 20, "판매", "판매사업"),
            ("부안저장", 0, 30, "저장", "저장소"),
            ("집단", 0, 25, "기타", "집단공급사업"),
        ])
        result = run_review(
            build_request("house", "general"),
            local_sources=LocalSourcesBundle(), lpg_municipal=municipal,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        retailer = category_for(result, "lpg_retailer")
        assert retailer.status == "exclusion_match", retailer.note
        assert [f.name for f in retailer.facilities] == ["부안가스"]
        storage = category_for(result, "lpg_storage")
        assert storage.status == "dataset_missing" and storage.manual_check_required
        assert [f.name for f in storage.facilities] == ["부안저장"]
        assert storage.facilities[0].metadata["reference"]
        assert "참고 핀 1건" in storage.note and storage.note.startswith("가스안전공사 정기검사")

    def test_municipal_duplicate_of_national_row_is_dropped(self) -> None:
        result = run_review(
            build_request("house", "general"),
            local_sources=LocalSourcesBundle(),
            lpg_retailer_file=FakeLpgRetailerFile([("대륙가스", 0, 20)]),
            lpg_municipal=FakeLpgMunicipal([("대륙가스(시군구)", 0, 25, "판매", "판매")]),
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        retailer = category_for(result, "lpg_retailer")
        assert [f.provider for f in retailer.facilities] == ["lpg_retailer_file"]

    def test_without_sources_note_says_api_not_connected(self) -> None:
        result = run_review(build_request("house", "general"), local_sources=LocalSourcesBundle())
        retailer = category_for(result, "lpg_retailer")
        assert retailer.status == "dataset_missing"
        assert retailer.note == "" and retailer.data_sources == []


class TestCasinoRegistryWiring:
    def test_casino_within_25m_is_exclusion_for_multi_child(self) -> None:
        registry = FakeCasinoRegistry([("파라다이스카지노 워커힐점", 0, 20, "영업")])
        result = run_review(
            build_request("house", "multi_child"),
            local_sources=LocalSourcesBundle(), casino_registry=registry,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        casino = category_for(result, "casino")
        assert casino.status == "exclusion_match", casino.note
        assert casino.facilities[0].provider == "casino_registry"
        assert casino.facilities[0].facility_type == "casino"
        assert [s.detail for s in casino.data_sources] == ["casino_registry"]

    def test_casino_point_only_stays_geometry_missing(self) -> None:
        # 시설 필지 경계를 못 붙이면 점 좌표만으로 확정하지 않는다(공통 규칙).
        registry = FakeCasinoRegistry([("파라다이스카지노 워커힐점", 0, 20, "영업")])
        result = run_review(
            build_request("house", "multi_child", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), casino_registry=registry,
        )
        assert category_for(result, "casino").status == "geometry_missing"

    def test_casino_far_away_is_no_conflict(self) -> None:
        registry = FakeCasinoRegistry([("강원랜드카지노", 0, 400, "영업")])
        result = run_review(
            build_request("house", "multi_child", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), casino_registry=registry,
        )
        casino = category_for(result, "casino")
        assert casino.status == "no_conflict_in_snapshot", casino.note
        assert not casino.manual_check_required

    def test_casino_without_registry_stays_dataset_missing(self) -> None:
        result = run_review(
            build_request("house", "multi_child", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(),
        )
        casino = category_for(result, "casino")
        assert casino.status == "dataset_missing"


class TestNotApplicableKeepsSources:
    def test_not_applicable_rule_still_shows_connected_sources(self) -> None:
        # 일반 유형에는 위락 Rule 이 미적용이지만 원천(인허가 원장)은 연결돼 있다.
        store = FakeFacilityStore([stored_facility("entertainment_bars", "유흥", 0, 20)])
        result = run_review(build_request("house", "general"), facility_store=store,
                            local_sources=LocalSourcesBundle())
        bar = category_for(result, "entertainment_bar")
        assert bar.status == "not_applicable"
        assert [s.detail for s in bar.data_sources] == ["entertainment_bars"]


class FakeBuildingScan:
    enabled = True

    def __init__(self, rows, failed=()) -> None:
        from app.services.building_use_scan import ScannedBuilding, ScanResult

        ring = [offset_coordinates(SITE_CENTER, 0, 20), offset_coordinates(SITE_CENTER, 0, 30),
                offset_coordinates(SITE_CENTER, 10, 30), offset_coordinates(SITE_CENTER, 10, 20),
                offset_coordinates(SITE_CENTER, 0, 20)]
        self.result = ScanResult(
            [ScannedBuilding(f"4111{n:015d}", "주소", ring, offset_coordinates(SITE_CENTER, 5, 25),
                             "1동", "위험물저장및처리시설", etc, kind) for n, (etc, kind) in enumerate(rows)],
            parcels_seen=len(rows), lookups=len(rows), failed_pnus=list(failed),
        )

    async def scan(self, center, radius_m):
        return self.result


class TestBuildingUseScanWiring:
    def test_scan_pins_land_on_their_categories_as_partial_sources(self) -> None:
        scan = FakeBuildingScan([
            ("액화석유가스 저장소", "LPG저장"),
            ("위험물 옥외탱크저장소", "위험물"),
            ("주유소", "주유소"),      # 연결된 원천이 덮는 종류 → 뺀다
            ("", "미분류"),
            ("", "화약류"),
        ])
        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), building_scan=scan,
        )
        storage = category_for(result, "lpg_storage")
        assert storage.status == "dataset_missing"
        assert [f.provider for f in storage.facilities] == ["building_use_scan"]
        assert storage.facilities[0].geometry_type == "polygon" and storage.facilities[0].metadata["reference"]
        # 빠짐없이 끝난 스캔은 완전 연결과 같은 범위 → 「우회 연결」
        assert [(s.kind, s.detail) for s in storage.data_sources] == [("bypass", "building_use_scan")]
        assert "참고 핀 1건" in storage.note

        hazmat = category_for(result, "hazmat_facility")
        assert [f.metadata["kind"] for f in hazmat.facilities] == ["위험물"]
        other = category_for(result, "hazmat_other_similar")
        assert [f.metadata["kind"] for f in other.facilities] == ["미분류"]
        explosives = category_for(result, "explosive_storage")
        assert [f.metadata["kind"] for f in explosives.facilities] == ["화약류"]
        assert explosives.status == "dataset_missing"
        assert [(s.kind, s.detail) for s in explosives.data_sources] == [("bypass", "building_use_scan")]
        # 근거(basis)가 핀 설명에 드러난다
        assert "기타용도 문자열 「액화석유가스 저장소」 추정" in storage.facilities[0].classification_note
        assert storage.facilities[0].metadata["basis"] == "text"
        # 주유소는 어느 종류에도 핀으로 오르지 않는다
        all_kinds = [f.metadata.get("kind") for c in result.categories for f in c.facilities]
        assert "주유소" not in all_kinds
        # 도시가스 제조시설(applied·미연결)도 스캔이 붙어 있으면 일부 연결 칩을 받는다
        city_gas = category_for(result, "city_gas_plant")
        assert [(s.kind, s.detail) for s in city_gas.data_sources] == [("bypass", "building_use_scan")]

    def test_incomplete_scan_is_only_partially_connected(self) -> None:
        scan = FakeBuildingScan([("액화석유가스 저장소", "LPG저장")], failed=["4111000000000000009"])
        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), building_scan=scan,
        )
        storage = category_for(result, "lpg_storage")
        assert [(s.kind, s.detail) for s in storage.data_sources] == [("partial", "building_use_scan")]

    def test_scan_failure_only_drops_pins(self) -> None:
        class Failing:
            enabled = True

            async def scan(self, center, radius_m):
                raise RuntimeError("vworld down")

        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), building_scan=Failing(),
        )
        storage = category_for(result, "lpg_storage")
        assert storage.status == "dataset_missing" and storage.facilities == []
        assert storage.data_sources == []


class TestMunicipalCoverageChips:
    def test_uncovered_regional_provider_gets_no_chip(self) -> None:
        class NotCovering:
            enabled = True

            async def facilities_for_site(self, address, center, radius_m):
                from app.services.lpg_municipal import MunicipalLookup

                return MunicipalLookup([], [], {}, [])

        result = run_review(
            build_request("house", "general"),
            local_sources=LocalSourcesBundle(),
            lpg_retailer_file=FakeLpgRetailerFile([]),
            lpg_seoul=NotCovering(),
        )
        retailer = category_for(result, "lpg_retailer")
        assert [s.detail for s in retailer.data_sources] == ["lpg_retailer_file"]
        storage = category_for(result, "lpg_storage")
        assert "lpg_seoul" not in [s.detail for s in storage.data_sources]


# ---------------------------------------------------------------------------
# 도시가스 제조시설 — LNG 생산기지·터미널·바이오가스 명단(H-02-아 §8) · 아목 판정 원천
# ---------------------------------------------------------------------------
class FakeCityGasRegistry:
    def __init__(self, rows, enabled: bool = True) -> None:
        from app.services.city_gas_registry import CityGasPlant

        self.enabled = enabled
        self.rows = [
            CityGasPlant(f"G-{n}", name, "한국가스공사", "경기", "주소", "LNG생산기지", status,
                         "https://example.test", offset_coordinates(SITE_CENTER, n, e))
            for name, n, e, status in rows
        ]

    async def plants_around(self, center, radius_m):
        return list(self.rows)


class TestCityGasRegistryWiring:
    def test_plant_within_50m_is_exclusion(self) -> None:
        registry = FakeCityGasRegistry([("평택LNG생산기지", 0, 30, "운영")])
        result = run_review(
            build_request("house", "general"),
            local_sources=LocalSourcesBundle(), city_gas_registry=registry,
            vworld=FakeVWorldForFacilities(half_size_m=5),
        )
        plant = category_for(result, "city_gas_plant")
        assert plant.status == "exclusion_match", plant.note
        assert plant.facilities[0].provider == "city_gas_registry"
        assert plant.facilities[0].facility_type == "city_gas_plant"
        assert plant.facilities[0].metadata["kind"] == "LNG생산기지"
        assert [s.detail for s in plant.data_sources] == ["city_gas_registry"]

    def test_plant_far_away_is_no_conflict_and_registry_absent_is_missing(self) -> None:
        registry = FakeCityGasRegistry([("삼척LNG생산기지", 0, 400, "운영")])
        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(), city_gas_registry=registry,
        )
        plant = category_for(result, "city_gas_plant")
        assert plant.status == "no_conflict_in_snapshot", plant.note
        without = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            local_sources=LocalSourcesBundle(),
        )
        assert category_for(without, "city_gas_plant").status == "dataset_missing"


class TestLpgRetailerZoningDisplay:
    def test_lpg_retailer_gets_zoning_name_without_changing_verdict(self) -> None:
        # 나목 LPG 판매소는 소재 용도지역을 표기만 한다(석유대체연료의 확인 요청 규칙은 적용 안 함).
        class ZonedVWorld(FakeVWorldForFacilities):
            def __init__(self) -> None:
                super().__init__(half_size_m=5)
                self.zoning_calls = 0

            async def zoning_at(self, lat: float, lng: float):
                from app.services.vworld import ZoningInfo

                self.zoning_calls += 1
                return ZoningInfo(name="제2종일반주거지역")

        vworld = ZonedVWorld()
        result = run_review(
            build_request("house", "general"),
            local_sources=LocalSourcesBundle(),
            lpg_retailer_file=FakeLpgRetailerFile([("동네가스", 0, 20)]),
            vworld=vworld,
        )
        retailer = category_for(result, "lpg_retailer")
        fac = next(f for f in retailer.facilities if f.name == "동네가스")
        assert fac.zoning_name == "제2종일반주거지역" and fac.zoning_class == "residential"
        assert vworld.zoning_calls >= 1
        # 판정은 용도지역과 무관하게 경계 확인 결과 그대로다.
        assert retailer.status == "exclusion_match", retailer.note
