"""서버 사본 일괄 갱신 — 야간 작업이 한 번 돌려 사본·원장을 전부 새로 만든다.

    python -m app.refresh_snapshots                     # 전부
    python -m app.refresh_snapshots --only kgs_lpg,safemap_fuel
    python -m app.refresh_snapshots --skip facilities,lpg_retailer
    python -m app.refresh_snapshots --list-steps         # 단계 이름
    python -m app.refresh_snapshots --list-files         # 사본·캐시 파일 이름(backend/data 기준)

배포 서버(Cloud Run)는 켜질 때마다 식어 있어, 전량 목록 원천을 파일 사본으로 실어 두고
곧바로 쓴다(services/snapshot_store). 이 명령이 그 사본들과 인허가 원장(facilities.db)을
한 번에 새로 만든다. 서버가 쓰는 것과 같은 배선(get_screening_service)의 클라이언트로
받으므로 경로·지오코더가 앱과 어긋나지 않는다.

- 단계는 서로 격리된다. 하나가 실패해도 나머지는 계속 돌고, 실패한 단계의 기존 사본은
  그대로 남는다(새 목록이 직전의 절반에 못 미치는 응답 이상도 실패로 치고 갈아 쓰지 않는다).
- 단계마다 한 줄 요약(상태 · 건수 · 초)을 찍고, 마지막 줄에 `REFRESH_RESULT ok=<n> failed=<m>`
  을 찍는다(파이프라인이 읽는다). 키가 없거나 사용신청 승인 전인 원천은 skip 으로 찍고 세지
  않는다. 종료 코드는 성공한 단계가 하나도 없고 실패가 있을 때만 0 이 아니다.
- 경로는 전부 코드 위치 기준(backend/data → 컨테이너에서는 /app/data)이고 대화형 입력이 없다.
  키는 서버와 같이 backend/.env 또는 환경변수에서 읽는다.
- 지오코딩은 새 주소에만 쓴다 — 인허가 원장은 localdata_geocode_cache.json, 등록공장은
  factory_geocode_cache.json, 화장시설·액화석유가스업·창고·판매소는 직전 사본의 좌표를 물려받는다.

스케줄러·워크플로는 이 모듈 밖에서 정한다(이 명령은 한 번 돌고 끝난다).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NamedTuple

# 단계 하나가 이 시간을 넘기면 실패로 치고 다음 단계로 간다(걸린 채 밤을 새지 않게).
DEFAULT_STEP_TIMEOUT_SECONDS = 30 * 60
# 저장소(facilities.db)에 좌표째 넣어 두는 원천은 보관 기한의 이 비율을 넘겼을 때만 다시 모은다.
STORE_REFRESH_AGE_RATIO = 0.5
# 인허가 원장에서 실패한 데이터셋을 다시 받는 횟수(1분 · 2분 쉬고).
FACILITY_RETRY_PASSES = 2
# 생활안전지도 레이어 단계 묶음 이름(--only/--skip 에서 전 레이어를 한 번에 가리킨다).
LAYER_GROUP = "safemap_layers"


class StepSkipped(Exception):
    """키가 없거나 승인 전이라 돌릴 수 없는 단계. 실패로 세지 않는다."""


class StepResult(NamedTuple):
    name: str
    status: str  # "ok" | "fail" | "skip"
    rows: int | None
    seconds: float
    detail: str


@dataclass(frozen=True)
class Step:
    name: str
    label: str
    run: Callable[["Context"], Awaitable[tuple[int | None, str]]]
    group: str = ""


class Context:
    """단계들이 나눠 쓰는 서비스. 처음 필요할 때 앱과 같은 배선으로 조립한다."""

    def __init__(self) -> None:
        self._screening: Any = None

    @property
    def screening(self) -> Any:
        if self._screening is None:
            from app.screening.router import get_screening_service

            self._screening = get_screening_service()
        return self._screening

    @property
    def hazard(self) -> Any:
        return self.screening.hazard

    @property
    def amenities(self) -> Any:
        return self.screening.amenities


def _require(client: Any, what: str) -> Any:
    if client is None or not getattr(client, "enabled", False):
        raise StepSkipped(f"{what} 키가 없어 건너뜁니다")
    return client


# -- 단계 ------------------------------------------------------------------------
async def _safemap_fuel(context: Context) -> tuple[int | None, str]:
    client = _require(context.hazard.safemap, "생활안전지도(SAFEMAP_API_KEY)")
    return len(await client.refresh(force=True)), ""


async def _kgs_lpg(context: Context) -> tuple[int | None, str]:
    client = _require(context.hazard.kgs_lpg, "공공데이터포털")
    stations = await client.refresh(force=True)
    quarantined = len(client.quarantined)
    return len(stations), f"좌표 격리 {quarantined}건" if quarantined else ""


async def _crematorium(context: Context) -> tuple[int | None, str]:
    client = _require(context.hazard.crematorium, "공공데이터포털·지오코더")
    facilities = await client.refresh(force=True)
    failures = len(client.geocode_failures)
    return len(facilities), f"지오코딩 실패 {failures}건" if failures else ""


async def _lpg_seoul(context: Context) -> tuple[int | None, str]:
    client = _require(context.hazard.lpg_seoul, "서울 열린데이터광장(SEOUL_OPEN_DATA_KEY)")
    facilities = await client.refresh(force=True)
    failures = len(client.geocode_failures)
    return len(facilities), f"지오코딩 실패 {failures}건" if failures else ""


async def _lpg_municipal(context: Context) -> tuple[int | None, str]:
    """전북 시군구 파일 + 이미 사본에 든 다른 시군구 파일."""

    from app.services.lpg_municipal import LPG_MUNICIPAL_DATASETS
    from app.services.snapshot_store import read_snapshot_file

    client = _require(context.hazard.lpg_municipal, "공공데이터포털·지오코더")
    path = client._snapshot_path  # noqa: SLF001 — 사본에 이미 든 데이터셋을 함께 갱신한다
    stored = set(read_snapshot_file(path)) if path is not None else set()
    targets = [d for d in LPG_MUNICIPAL_DATASETS if d.sido == "전북" or d.dataset_id in stored]
    total = 0
    notes: list[str] = []
    failed: list[str] = []
    for dataset in targets:
        try:
            facilities, _failures = await client.refresh_dataset(dataset, force=True)
        except Exception as exc:  # noqa: BLE001 — 데이터셋 하나의 실패가 나머지를 막지 않는다
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                notes.append(f"{dataset.sigungu or dataset.sido} 활용신청 전")
            else:
                failed.append(f"{dataset.sigungu or dataset.sido}({exc})")
            continue
        total += len(facilities)
    if failed:
        raise RuntimeError(
            f"{len(targets)}개 파일 중 {len(failed)}개 실패(기존 사본 유지): " + " · ".join(failed)
        )
    return total, " · ".join([f"파일 {len(targets)}개", *notes])


def _layer_feeds(context: Context) -> dict[str, Any]:
    hazard, amenities = context.hazard, context.amenities
    feeds = [
        hazard.chemical_feed, hazard.waste_feed, hazard.emission_feed,
        amenities.safemap_schools, amenities.safemap_universities, amenities.safemap_offices,
        amenities.safemap_hospitals, amenities.safemap_fire,
    ]
    return {feed.layer_id: feed for feed in feeds if feed is not None}


def _layer_step(layer_id: str) -> Callable[[Context], Awaitable[tuple[int | None, str]]]:
    async def run(context: Context) -> tuple[int | None, str]:
        feed = _layer_feeds(context).get(layer_id)
        if feed is None:
            raise StepSkipped("앱에 배선되지 않은 레이어")
        _require(feed, "생활안전지도(SAFEMAP_API_KEY)")
        try:
            return len(await feed.refresh(force=True)), ""
        except Exception as exc:
            if getattr(exc, "unapproved", False):
                raise StepSkipped("생활안전지도 데이터 사용신청 승인 전") from exc
            raise

    return run


async def _factory_rows(context: Context) -> tuple[int | None, str]:
    """전북 15개 시군구 등록공장 목록(산단공 API · 큰 시군구는 504 뒤 작은 쪽으로 다시 받는다)."""

    from app.services.factory_registry import JEONBUK_SIGUNGU_CODES

    client = _require(context.hazard.factory_registry, "공공데이터포털")
    total = 0
    failed: list[str] = []
    for code in JEONBUK_SIGUNGU_CODES:
        try:
            total += len(await client.refresh_sigungu(code))
        except Exception as exc:  # noqa: BLE001 — 시군구 하나의 실패가 나머지를 막지 않는다
            failed.append(f"{code}({exc})")
    if failed:
        raise RuntimeError(
            f"{len(JEONBUK_SIGUNGU_CODES)}개 시군구 중 {len(failed)}개 실패(기존 사본 유지): "
            + " · ".join(failed)
        )
    return total, f"시군구 {len(JEONBUK_SIGUNGU_CODES)}개"


async def _factory_geocode(context: Context) -> tuple[int | None, str]:
    """전북 등록공장 주소 중 캐시에 없는 것만 지오코딩한다(factory_geocode_cache.json)."""

    from app.services.factory_registry import JEONBUK_SIGUNGU_CODES

    client = _require(context.hazard.factory_registry, "공공데이터포털")
    if client.geocoder is None:
        raise StepSkipped("지오코더가 없어 건너뜁니다")
    new_addresses = 0
    outages = 0
    for code in JEONBUK_SIGUNGU_CODES:
        new_addresses += await client.warm_geocodes(code)
        outages += client.last_geocode_outages
    cached = len(client._load_geocache())  # noqa: SLF001 — 캐시 크기만 읽는다
    detail = f"새 주소 {new_addresses:,}건"
    if outages:
        # 지오코더 장애로 못 붙인 주소는 캐시에 굳히지 않았다. 다음 갱신이 다시 묻는다.
        raise RuntimeError(f"{detail} 중 {outages:,}건은 지오코더 오류로 미확정(캐시 {cached:,}건)")
    return cached, detail


async def _facilities(context: Context) -> tuple[int | None, str]:
    """인허가 원장(facilities.db) 18종. 좌표 없는 행은 캐시에 없는 주소만 지오코딩한다."""

    from app.config import get_settings
    from app.services.facility_store import FacilityStore
    from app.services.localdata import DATASETS
    from app.sync_facilities import RETRY_FAILED_AFTER_SECONDS, localdata_client, refresh

    settings = get_settings()
    if not settings.public_data_key:
        raise StepSkipped("공공데이터포털 키가 없어 건너뜁니다")
    store = FacilityStore()
    client = localdata_client(settings)
    failed = await refresh(client, store, DATASETS, report=_say)
    # 포털이 연달아 받으면 429·504 를 낸다. 쉬었다가 실패분만 다시 받는다(두 번까지 —
    # 2026-10-01 실측: 한 번 쉬고도 특정고압가스업이 429 로 남았다).
    for attempt in range(1, FACILITY_RETRY_PASSES + 1):
        if not failed:
            break
        await asyncio.sleep(RETRY_FAILED_AFTER_SECONDS * attempt)
        failed = await refresh(client, store, failed, report=_say)
    keys = {dataset.key for dataset in DATASETS}
    total = sum(s.record_count for s in store.sync_states() if s.dataset_key in keys)
    if failed:
        raise RuntimeError(
            f"{len(DATASETS)}종 중 {len(failed)}종 실패(기존 적재 유지 · 합계 {total:,}건): "
            + " · ".join(dataset.label for dataset in failed)
        )
    return total, f"데이터셋 {len(DATASETS)}종"


def _store_age_days(store: Any, dataset_key: str) -> float | None:
    state = store.sync_state_for(dataset_key) if store is not None else None
    if state is None or state.record_count == 0:
        return None
    synced = state.synced_at if state.synced_at.tzinfo else state.synced_at.replace(tzinfo=UTC)
    return (datetime.now(UTC) - synced).total_seconds() / 86400


def _stored_step(
    attr: str, what: str, module_name: str
) -> Callable[[Context], Awaitable[tuple[int | None, str]]]:
    """저장소(facilities.db)에 좌표째 넣어 두는 원천. 보관 기한의 절반을 넘겼을 때만 다시 모은다."""

    async def run(context: Context) -> tuple[int | None, str]:
        import importlib

        module = importlib.import_module(module_name)
        client = _require(getattr(context.hazard, attr), what)
        store = client._store  # noqa: SLF001 — 저장분 나이만 읽는다
        age = _store_age_days(store, module.STORE_DATASET_KEY)
        limit = module.STORE_MAX_AGE.total_seconds() / 86400
        if age is not None and age < limit * STORE_REFRESH_AGE_RATIO:
            state = store.sync_state_for(module.STORE_DATASET_KEY)
            return state.record_count, f"최신({age:.1f}일 전 수집 · 보관 {limit:.0f}일) — 다시 받지 않음"
        items = await client.refresh()
        failures = len(client.geocode_failures)
        return len(items), f"지오코딩 실패 {failures}건" if failures else ""

    return run


def build_steps() -> list[Step]:
    from app.services.safemap_facilities import SNAPSHOT_LAYER_IDS
    from app.services.safemap_layers import LAYER_BY_ID

    steps = [
        Step("safemap_fuel", "생활안전지도 전국 주유시설(IF_0033)", _safemap_fuel),
        Step("kgs_lpg", "가스안전공사 전국 LPG 충전소", _kgs_lpg),
        Step("crematorium", "보건복지부 전국 화장시설(좌표 포함)", _crematorium),
    ]
    steps += [
        Step(
            f"safemap_layer_{layer_id}",
            f"생활안전지도 {LAYER_BY_ID[layer_id].label}({layer_id})",
            _layer_step(layer_id),
            group=LAYER_GROUP,
        )
        for layer_id in SNAPSHOT_LAYER_IDS
    ]
    steps += [
        Step("lpg_municipal", "시군구 액화석유가스업 파일(전북 + 사본에 든 시군구)", _lpg_municipal),
        Step("lpg_seoul", "서울 액화석유가스업(열린데이터광장)", _lpg_seoul),
        Step("factory_rows", "산단공 등록공장 목록(전북 15개 시군구)", _factory_rows),
        Step("factory_geocode", "등록공장 주소 지오코딩 캐시(새 주소만)", _factory_geocode),
        Step("facilities", "인허가 원장 facilities.db(18종)", _facilities),
        Step(
            "logistics_warehouse",
            "환경부 보관·저장 창고(국토부 물류창고업 · facilities.db)",
            _stored_step("logistics_warehouse", "공공데이터포털·지오코더", "app.services.logistics_warehouse"),
        ),
        Step(
            "lpg_retailer",
            "LPG 판매소 파일 15091481(facilities.db)",
            _stored_step("lpg_retailer_file", "공공데이터포털·지오코더", "app.services.lpg_retailer_file"),
        ),
    ]
    return steps


# -- 실행 ------------------------------------------------------------------------
def _say(message: str) -> None:
    print(message, flush=True)


def _names(values: list[str] | None) -> set[str]:
    """`--only a,b --only c` · `--only "a b"` 를 모두 받는다."""

    names: set[str] = set()
    for value in values or []:
        names.update(part for part in value.replace(",", " ").split() if part)
    return names


def select_steps(steps: list[Step], only: set[str], skip: set[str]) -> list[Step]:
    def named(step: Step, names: set[str]) -> bool:
        return step.name in names or (bool(step.group) and step.group in names)

    return [
        step for step in steps
        if (not only or named(step, only)) and not named(step, skip)
    ]


def format_result(result: StepResult, label: str) -> str:
    rows = f"{result.rows:>9,}건" if result.rows is not None else f"{'-':>10}"
    tail = f" · {result.detail}" if result.detail else ""
    return (
        f"[{result.status:<4}] {result.name:<26} {rows} · {result.seconds:7.1f}초 · {label}{tail}"
    )


async def run_step(step: Step, context: Context, timeout: float) -> StepResult:
    started = time.monotonic()
    try:
        rows, detail = await asyncio.wait_for(step.run(context), timeout=timeout)
        status = "ok"
    except StepSkipped as exc:
        rows, detail, status = None, str(exc), "skip"
    except (TimeoutError, asyncio.TimeoutError):
        rows, detail, status = None, f"{timeout:.0f}초 안에 끝나지 않았습니다(기존 사본 유지)", "fail"
    except Exception as exc:  # noqa: BLE001 — 단계 하나의 실패가 나머지를 막지 않는다
        rows, detail, status = None, str(exc) or exc.__class__.__name__, "fail"
    return StepResult(step.name, status, rows, time.monotonic() - started, detail)


async def run(steps: list[Step], timeout: float) -> list[StepResult]:
    context = Context()
    results: list[StepResult] = []
    for step in steps:
        result = await run_step(step, context, timeout)
        results.append(result)
        _say(format_result(result, step.label))
    return results


def result_line(results: list[StepResult]) -> str:
    ok = sum(1 for result in results if result.status == "ok")
    failed = sum(1 for result in results if result.status == "fail")
    return f"REFRESH_RESULT ok={ok} failed={failed}"


def exit_code(results: list[StepResult]) -> int:
    """성공한 단계가 하나도 없고 실패가 있을 때만 1."""

    ok = any(result.status == "ok" for result in results)
    failed = any(result.status == "fail" for result in results)
    return 1 if failed and not ok else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.refresh_snapshots",
        description="서버 사본(전량 목록 API 사본·인허가 원장)을 한 번에 새로 만든다.",
    )
    parser.add_argument("--only", action="append", metavar="이름", help="이 단계만(쉼표로 여럿)")
    parser.add_argument("--skip", action="append", metavar="이름", help="이 단계는 빼고(쉼표로 여럿)")
    parser.add_argument("--list-steps", action="store_true", help="단계 이름을 찍고 끝낸다")
    parser.add_argument(
        "--list-files", action="store_true",
        help="사본·캐시 파일 이름(backend/data 기준 상대 경로)을 한 줄에 하나씩 찍고 끝낸다",
    )
    parser.add_argument(
        "--step-timeout", type=float, default=DEFAULT_STEP_TIMEOUT_SECONDS, metavar="초",
        help=f"단계 하나의 제한 시간(기본 {DEFAULT_STEP_TIMEOUT_SECONDS}초)",
    )
    args = parser.parse_args(argv)

    # 콘솔 인코딩이 한글을 못 실어도(컨테이너 C 로캘 등) 죽지 않게 한다.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")

    if args.list_files:
        from app.bundled_files import snapshot_file_names

        for name in snapshot_file_names():
            _say(name)
        return 0

    steps = build_steps()
    if args.list_steps:
        for step in steps:
            group = f" (묶음 {step.group})" if step.group else ""
            _say(f"{step.name:<26} {step.label}{group}")
        return 0

    only, skip = _names(args.only), _names(args.skip)
    known = {step.name for step in steps} | {step.group for step in steps if step.group}
    unknown = sorted((only | skip) - known)
    if unknown:
        _say(f"알 수 없는 단계: {', '.join(unknown)}")
        _say(f"사용 가능: {', '.join(sorted(known))}")
        return 2
    selected = select_steps(steps, only, skip)
    if not selected:
        _say("돌릴 단계가 없습니다.")
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    started = time.monotonic()
    _say(f"서버 사본 갱신 시작 — {len(selected)}단계 · {datetime.now(UTC):%Y-%m-%d %H:%M:%S} UTC")
    results = asyncio.run(run(selected, args.step_timeout))
    _say(f"끝 — {time.monotonic() - started:.1f}초")
    _say(result_line(results))
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
