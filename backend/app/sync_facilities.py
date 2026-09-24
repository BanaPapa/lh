"""인허가 시설 로컬 캐시 적재.

    python -m app.sync_facilities              # 전체
    python -m app.sync_facilities lodgings     # 일부만

판정은 전국 원장을 로컬에 받아 두고 반경으로 거른다. 데이터가 하루 한 번 갱신되므로
적재도 하루 한 번이면 충분하다. 백엔드가 켜질 때 `ensure_facility_sync` 가 받은 지
하루가 넘은 데이터셋만 백그라운드로 다시 받는다. 요청 경로에서는 절대 호출하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from app.config import Settings, get_settings
from app.services.facility_store import FacilityStore
from app.services.localdata import DATASETS, DATASET_BY_KEY, LocalDataClient, LocalDataSet

# uvicorn 이 출력 설정을 걸어 둔 로거로 내보내야 서버 콘솔에 갱신 진행이 보인다.
logger = logging.getLogger("uvicorn.error")

# 원장 갱신 주기. 포털 원장이 하루 한 번 바뀌므로 그보다 자주 받을 이유가 없다.
MAX_AGE = timedelta(hours=24)

# 데이터셋을 연달아 받으면 포털이 429 를 낸다. 사이에 쉬고, 실패분은 한 번 더 받는다.
PAUSE_BETWEEN_DATASETS_SECONDS = 3.0
RETRY_FAILED_AFTER_SECONDS = 60.0


def stale_datasets(
    store: FacilityStore,
    now: datetime | None = None,
    max_age: timedelta = MAX_AGE,
) -> list[LocalDataSet]:
    """한 번도 안 받았거나 받은 지 max_age 가 지난 데이터셋."""

    now = now or datetime.now(UTC)
    synced_at = {state.dataset_key: state.synced_at for state in store.sync_states()}
    stale: list[LocalDataSet] = []
    for dataset in DATASETS:
        last = synced_at.get(dataset.key)
        if last is None or now - last >= max_age:
            stale.append(dataset)
    return stale


async def refresh(
    client: LocalDataClient,
    store: FacilityStore,
    datasets: Sequence[LocalDataSet],
    report: Callable[[str], None] = logger.info,
    pause_seconds: float | None = None,
) -> list[LocalDataSet]:
    """데이터셋을 차례로 받아 갈아끼운다. 실패한 데이터셋을 돌려준다.

    한 데이터셋 실패가 나머지를 막지 않는다. 실패분은 기존 적재를 그대로 둔다 —
    어제 원장이 비어 있는 원장보다 낫다.
    """

    pause = PAUSE_BETWEEN_DATASETS_SECONDS if pause_seconds is None else pause_seconds
    failed: list[LocalDataSet] = []
    for index, dataset in enumerate(datasets):
        if index and pause:
            await asyncio.sleep(pause)
        started = time.monotonic()
        try:
            records = await client.fetch_all(dataset)
        except Exception as exc:
            report(f"  {dataset.label:<24} 실패: {exc}")
            failed.append(dataset)
            continue
        # 수만 행 적재는 동기 sqlite 쓰기라 이벤트 루프를 막지 않게 스레드로 뺀다.
        stored = await asyncio.to_thread(store.replace_dataset, dataset.key, records)
        elapsed = time.monotonic() - started
        report(f"  {dataset.label:<24} {stored:>7,}건 적재 · {elapsed:5.1f}초")
    return failed


async def sync(dataset_keys: list[str]) -> int:
    settings = get_settings()
    if not settings.public_data_key:
        print("공공데이터포털 인증키가 없습니다. .env 의 TAGO_SERVICE_KEY 또는 "
              "PUBLIC_DATA_SERVICE_KEY 를 확인하세요.")
        return 1

    store = FacilityStore()
    datasets = [DATASET_BY_KEY[key] for key in dataset_keys]
    await refresh(LocalDataClient(settings.public_data_key), store, datasets, report=print)
    total = sum(state.record_count for state in store.sync_states() if state.dataset_key in dataset_keys)
    print(f"\n합계 {total:,}건 · {store.db_path}")
    return 0


async def refresh_stale(
    settings: Settings,
    store: FacilityStore | None = None,
    client: LocalDataClient | None = None,
    retry_after_seconds: float = RETRY_FAILED_AFTER_SECONDS,
) -> None:
    """기동 시 자동 갱신 본체. 오래된 데이터셋만 받고, 실패분은 잠시 뒤 한 번 더 받는다."""

    store = store or FacilityStore()
    stale = stale_datasets(store)
    if not stale:
        logger.info("인허가 원장 최신: 자동 갱신 건너뜀")
        return
    client = client or LocalDataClient(settings.public_data_key)
    logger.info(
        "인허가 원장 자동 갱신 시작: %d종(%s)",
        len(stale),
        "·".join(dataset.label for dataset in stale),
    )
    failed = await refresh(client, store, stale)
    if failed:
        await asyncio.sleep(retry_after_seconds)
        failed = await refresh(client, store, failed)
    if failed:
        logger.warning(
            "인허가 원장 자동 갱신 일부 실패: %s (기존 적재 유지, 다음 기동 때 다시 받음)",
            "·".join(dataset.label for dataset in failed),
        )
    else:
        logger.info("인허가 원장 자동 갱신 완료: %d종", len(stale))


# 자동 갱신 태스크. GC 로 취소되지 않게 참조를 붙잡아 둔다.
_refresh_tasks: set[asyncio.Task[None]] = set()


def ensure_facility_sync(settings: Settings) -> asyncio.Task[None] | None:
    """백엔드 기동 때 오래된 인허가 원장을 백그라운드로 다시 받는다.

    기동을 막지 않는다. 갱신 중 심사는 기존 적재분으로 돌고, 데이터셋 하나가
    다 받아진 순간 통째로 교체된다. 데모 모드이거나 키가 없으면 아무 것도 하지 않는다.
    """

    if settings.demo_mode or not settings.public_data_key:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    async def _run() -> None:
        try:
            await refresh_stale(settings)
        except Exception:
            logger.exception("인허가 원장 자동 갱신 실패")

    task = loop.create_task(_run())
    _refresh_tasks.add(task)
    task.add_done_callback(_refresh_tasks.discard)
    return task


def main() -> int:
    keys = sys.argv[1:]
    unknown = [key for key in keys if key not in DATASET_BY_KEY]
    if unknown:
        print(f"알 수 없는 데이터셋: {', '.join(unknown)}")
        print(f"사용 가능: {', '.join(dataset.key for dataset in DATASETS)}")
        return 2
    return asyncio.run(sync(keys or [dataset.key for dataset in DATASETS]))


if __name__ == "__main__":
    raise SystemExit(main())
