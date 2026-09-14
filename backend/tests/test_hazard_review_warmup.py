"""로컬 원천 묶음(local_sources) 백그라운드 워밍업 회귀 테스트.

`ensure_local_sources_warmup` 이 실제로 호출되는지, 요청 스레드를 막지 않는지,
워밍업 전에는 공장 카테고리가 정직하게 dataset_missing 으로 남는지(절대
no_conflict_in_snapshot 으로 둔갑하지 않는지), 중복 호출해도 한 번만 시작되는지를
확인한다. factoryON 원본 PNU 확정(실측 103초, 캐시 미스 기준)을 실제로 기다리면
테스트가 느려지므로, 여기서는 loader 를 이벤트로 제어 가능한 가짜로 바꿔치기한다.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.hazard_review.models import HazardParcel, HazardReviewRequest, HazardSite
from app.hazard_review.router import ensure_local_sources_warmup
from app.hazard_review.service import HazardReviewService, offset_coordinates
from app.models import Coordinates
from app.services.local_wiring import LocalSourcesBundle


SITE_CENTER = Coordinates(lat=37.40111, lng=127.10853)


class FakeKakaoClient:
    enabled = False


def _build_request() -> HazardReviewRequest:
    half = 18.0
    ring = [
        offset_coordinates(SITE_CENTER, -half, -half),
        offset_coordinates(SITE_CENTER, -half, half),
        offset_coordinates(SITE_CENTER, half, half),
        offset_coordinates(SITE_CENTER, half, -half),
        offset_coordinates(SITE_CENTER, -half, -half),
    ]
    return HazardReviewRequest(
        site=HazardSite(
            name="검증 사업지",
            address="경기도 성남시 분당구 판교역로 235",
            coordinates=SITE_CENTER,
            housing_type="house",
            application_type="multi_child",
            parcels=[
                HazardParcel(
                    parcel_id="prototype-parcel",
                    pnu="",
                    geometry=ring,
                    geometry_source="provisional_polygon",
                )
            ],
        ),
        rule_pack_id="lh-rulebook-v1.4",
    )


def _service_with_loader(loader: Any) -> HazardReviewService:
    service = HazardReviewService(kakao=FakeKakaoClient(), demo_mode=False)  # type: ignore[arg-type]
    # router.get_hazard_service 가 배선하는 것과 같은 두 속성. 워밍업 전에는
    # local_sources 가 빈 묶음이라 공장 카테고리는 dataset_missing 으로 남는다.
    service._local_sources_loader = loader  # type: ignore[attr-defined]
    service._local_sources_warmup_started = False  # type: ignore[attr-defined]
    return service


async def _noop_progress(*_args: Any) -> None:
    return None


def _blocking_loader(release: asyncio.Event, calls: list[int]):
    """release 가 설정될 때까지 스레드에서 대기하는 가짜 로더.

    run_in_executor(스레드풀)에서 실행되므로 asyncio.Event.wait() 이 아니라
    is_set() 폴링을 쓴다(스레드에서 이벤트 루프 없는 코루틴 대기는 쓸 수 없다).
    """

    def load() -> LocalSourcesBundle:
        calls.append(1)
        while not release.is_set():
            time.sleep(0.01)
        return LocalSourcesBundle(
            factory_pnus=frozenset({"4511100000-1-00010000"}),
            factory_registry_loaded=True,
            factory_facilities=(),
            prohibit_factory_loaded=True,
        )

    return load


def test_warmup_returns_immediately_without_waiting_for_loader() -> None:
    """워밍업 호출은 로더가 끝나길 기다리지 않고 즉시 반환해야 한다(요청 비차단)."""

    release = asyncio.Event()
    calls: list[int] = []

    async def scenario() -> None:
        service = _service_with_loader(_blocking_loader(release, calls))
        loop = asyncio.get_running_loop()
        start = loop.time()
        ensure_local_sources_warmup(service)
        elapsed = loop.time() - start
        # 로더는 release 가 설정돼야 끝나므로, 즉시 반환됐다면 아직 훨씬 짧다.
        assert elapsed < 1.0
        # 아직 워밍업이 안 끝났으니 local_sources 는 빈 묶음 그대로여야 한다.
        assert service.local_sources.factory_registry_loaded is False

        release.set()
        for _ in range(500):
            if service.local_sources.factory_registry_loaded:
                break
            await asyncio.sleep(0.01)
        assert service.local_sources.factory_registry_loaded is True
        assert len(calls) == 1

    asyncio.run(scenario())


def test_duplicate_warmup_calls_start_loader_only_once() -> None:
    """같은 서비스에 여러 번 호출해도 로더는 한 번만 실행돼야 한다."""

    release = asyncio.Event()
    release.set()  # 즉시 끝나도 되는 로더
    calls: list[int] = []

    async def scenario() -> None:
        service = _service_with_loader(_blocking_loader(release, calls))
        ensure_local_sources_warmup(service)
        ensure_local_sources_warmup(service)
        ensure_local_sources_warmup(service)

        for _ in range(500):
            if service.local_sources.factory_registry_loaded:
                break
            await asyncio.sleep(0.01)
        assert service.local_sources.factory_registry_loaded is True
        assert len(calls) == 1

    asyncio.run(scenario())


def test_review_uses_pinned_snapshot_despite_mid_review_bundle_swap() -> None:
    """review() 도중 local_sources 가 통째로 교체돼도 그 판정은 영향받지 않는다.

    후보 수집(오피넷 조회) 도중 워밍업이 끝나 self.local_sources 가 빈 번들에서
    꽉 찬 번들로 교체돼도, 이미 시작된 판정은 시작 시점에 붙잡은(빈) 번들만 봐야
    한다. 그렇지 않으면 조회하지 않은 원천이 연결된 것처럼 보여 충돌 없음으로
    둔갑할 수 있다(스냅샷 레이스).
    """

    hold = asyncio.Event()
    entered = asyncio.Event()

    class HoldingOpinetClient:
        enabled = True

        async def stations_around(self, center: Coordinates, radius_m: float):
            entered.set()
            await hold.wait()
            return []

        async def station_detail(self, station_id: str) -> None:
            return None

    service = HazardReviewService(
        kakao=FakeKakaoClient(),  # type: ignore[arg-type]
        demo_mode=False,
        opinet=HoldingOpinetClient(),  # type: ignore[arg-type]
    )
    assert service.local_sources.factory_registry_loaded is False

    async def scenario() -> None:
        review_task = asyncio.create_task(
            service.review(_build_request(), _noop_progress, asyncio.Event())
        )
        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "판정이 오피넷 조회 지점까지 도달하지 못했다"

        # 판정이 아직 오피넷 await 에 걸려 있는 도중, 워밍업이 방금 끝난 것처럼
        # 번들을 통째로 교체한다.
        service.local_sources = LocalSourcesBundle(
            factory_pnus=frozenset({"4511100000-1-00010000"}),
            factory_registry_loaded=True,
            factory_facilities=(),
            prohibit_factory_loaded=True,
        )
        # 서비스 레벨에서는 즉시 꽉 찬 번들이 보인다(진행 중인 판정과는 별개로).
        assert service.local_sources.factory_registry_loaded is True

        hold.set()
        result = await review_task

        for key in (
            "factory_registered",
        ):
            category = next(c for c in result.categories if c.key == key)
            # 시작 시점에 빈 번들을 붙잡았으므로, 판정 도중 꽉 찬 번들로 바뀌어도
            # 이 판정 결과는 여전히 registry 미적재로 남아야 한다.
            assert category.status == "dataset_missing", key
            assert category.status != "no_conflict_in_snapshot", key

        # 판정이 끝난 뒤에는 서비스가 다시 최신(꽉 찬) 번들을 정상 반환한다.
        assert service.local_sources.factory_registry_loaded is True

    asyncio.run(scenario())


def test_factory_category_is_dataset_missing_before_warmup_completes() -> None:
    """워밍업이 끝나기 전 진행되는 검토는 factoryON PNU 집합이 비어 있다.

    조회조차 못 한 상태를 '충돌 없음'으로 둔갑시키지 않고 dataset_missing 으로
    정직하게 남아야 한다(절대 no_conflict_in_snapshot 이면 안 된다).
    """

    release = asyncio.Event()
    calls: list[int] = []

    async def scenario() -> None:
        service = _service_with_loader(_blocking_loader(release, calls))
        ensure_local_sources_warmup(service)

        # 워밍업이 아직 안 끝난 상태에서 검토를 돌린다.
        result = await service.review(_build_request(), _noop_progress, asyncio.Event())
        for key in ("factory_registered",):
            category = next(c for c in result.categories if c.key == key)
            assert category.status == "dataset_missing", key
            assert category.status != "no_conflict_in_snapshot", key

        release.set()
        for _ in range(500):
            if service.local_sources.factory_registry_loaded:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 전국 목록 공개 API(KGS LPG · CNG · 화장시설) 예열 — 첫 심사의 콜드스타트 실패 방지
# ---------------------------------------------------------------------------
from app.hazard_review.router import ensure_api_sources_warmup


class _CountingListClient:
    def __init__(self, method: str, enabled: bool = True, fail: bool = False) -> None:
        self.enabled = enabled
        self.calls = 0
        self._fail = fail
        setattr(self, method, self._list)

    async def _list(self) -> list:
        self.calls += 1
        if self._fail:
            raise RuntimeError("boom")
        await asyncio.sleep(0)
        return [1, 2, 3]


def _api_service(**clients: Any) -> HazardReviewService:
    return HazardReviewService(kakao=FakeKakaoClient(), demo_mode=False, **clients)  # type: ignore[arg-type]


def test_api_warmup_lists_every_enabled_source_once() -> None:
    async def scenario() -> None:
        kgs = _CountingListClient("all_stations")
        cng = _CountingListClient("all_stations")
        crem = _CountingListClient("all_crematoriums")
        off = _CountingListClient("all_stations", enabled=False)
        service = _api_service(kgs_lpg=kgs, cng=cng, crematorium=crem, safemap=off)
        task = ensure_api_sources_warmup(service)
        ensure_api_sources_warmup(service)  # 중복 호출은 새 작업을 만들지 않는다
        assert task is not None
        await task
        assert (kgs.calls, cng.calls, crem.calls, off.calls) == (1, 1, 1, 0)

    asyncio.run(scenario())


def test_api_warmup_failure_of_one_source_does_not_stop_others() -> None:
    async def scenario() -> None:
        kgs = _CountingListClient("all_stations", fail=True)
        cng = _CountingListClient("all_stations")
        service = _api_service(kgs_lpg=kgs, cng=cng)
        task = ensure_api_sources_warmup(service)
        assert task is not None
        await task  # 예외가 새어 나오지 않는다
        assert cng.calls == 1

    asyncio.run(scenario())


def test_api_warmup_skips_demo_mode() -> None:
    async def scenario() -> None:
        kgs = _CountingListClient("all_stations")
        service = HazardReviewService(kakao=FakeKakaoClient(), demo_mode=True, kgs_lpg=kgs)  # type: ignore[arg-type]
        assert ensure_api_sources_warmup(service) is None
        assert kgs.calls == 0

    asyncio.run(scenario())
