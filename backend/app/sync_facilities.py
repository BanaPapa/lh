"""인허가 시설 로컬 캐시 적재.

    python -m app.sync_facilities              # 전체
    python -m app.sync_facilities lodgings     # 일부만

인허가 API는 지역 필터가 없어 전량을 받아야 한다. 데이터가 하루 한 번 갱신되므로
이 명령도 하루 한 번이면 충분하다. 앱 요청 경로에서는 절대 호출하지 않는다.
"""

from __future__ import annotations

import asyncio
import sys
import time

from app.config import get_settings
from app.services.facility_store import FacilityStore
from app.services.localdata import DATASETS, DATASET_BY_KEY, LocalDataClient


async def sync(dataset_keys: list[str]) -> int:
    settings = get_settings()
    if not settings.public_data_key:
        print("공공데이터포털 인증키가 없습니다. .env 의 TAGO_SERVICE_KEY 또는 "
              "PUBLIC_DATA_SERVICE_KEY 를 확인하세요.")
        return 1

    client = LocalDataClient(settings.public_data_key)
    store = FacilityStore()
    datasets = [DATASET_BY_KEY[key] for key in dataset_keys]

    total_stored = 0
    for dataset in datasets:
        started = time.monotonic()
        try:
            records = await client.fetch_all(dataset)
        except Exception as exc:  # 한 데이터셋 실패가 나머지를 막지 않는다.
            print(f"  {dataset.label:<24} 실패: {exc}")
            continue
        stored = store.replace_dataset(dataset.key, records)
        total_stored += stored
        elapsed = time.monotonic() - started
        print(f"  {dataset.label:<24} {stored:>7,}건 적재 · {elapsed:5.1f}초")

    print(f"\n합계 {total_stored:,}건 · {store.db_path}")
    return 0


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
