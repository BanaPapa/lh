"""서버 사본(스냅샷) — 전량 목록 공개 API 를 파일로 받아 두고 곧바로 쓴다.

배경(2026-10-01): 배포 서버(Cloud Run, 최소 인스턴스 0)는 켜질 때마다 메모리 캐시가
비어 있다. 전국 목록을 받는 원천(생활안전지도 주유시설 14,400건 · 가스안전공사 LPG
충전소 등)은 첫 심사가 그 목록을 기다리거나(지연), 아직 받는 중이라는 신호를 「조회
실패 — 재심사 필요」로 띄웠다(거짓 경고). 식은 캐시는 장애가 아니다. 등록공장 목록
사본(factory_registry.DEFAULT_ROWS_CACHE)과 같은 방식을 한 곳에 모은다.

    읽기   사본이 있으면 곧바로 쓴다(첫 심사도 기다리지 않는다).
    갱신   받은 지 하루(SNAPSHOT_TTL_SECONDS)가 지났으면 그대로 쓰면서 뒤에서 새로 받아
           파일을 갈아 쓴다.
    장애   새로 받기가 실패하면 사본을 계속 쓰되 「서버 사본 사용(기준일 …)」으로 드러낸다
           — 실시간 답처럼 보이지 않게. 사본이 7일(SNAPSHOT_MAX_STALE_SECONDS)을 넘겼는데
           새로 받기도 실패하면 종전대로 원천 장애(조회 실패)로 올린다.
    없음   사본도 없고 API 도 실패하면 종전대로 원천 장애다.

파일 모양은 `{키: {"fetched_at": epoch 초, "rows": [...]}}` 로 등록공장 사본과 같다.
파일은 git 에 두지 않고(.gitignore) 배포 이미지에만 실린다 — `python -m app.refresh_snapshots`
가 한 번에 다시 만든다. 테스트가 실제 data 폴더에 쓰지 않도록, 클라이언트 생성자의 사본
경로 기본값은 None 이고 앱 배선(wiring)만 경로를 준다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from app.kst import KST

logger = logging.getLogger(__name__)

# 사본 파일이 놓이는 곳(backend/data). 코드 위치 기준이라 로컬·컨테이너(/app/data)가 같다.
DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# 받은 지 이만큼 지나면 「오래된 사본」 — 그대로 쓰면서 뒤에서 새로 받는다.
SNAPSHOT_TTL_SECONDS = 24 * 60 * 60
# 이만큼 넘긴 사본은 새로 받기가 실패했을 때 더 믿지 않는다(원천 장애로 올린다).
SNAPSHOT_MAX_STALE_SECONDS = 7 * 24 * 60 * 60
# 뒤에서 새로 받기가 실패한 뒤 다시 시도하기까지 쉬는 시간. 429 가 뜬 날 심사마다
# 전량 조회를 다시 걸면 쿼터만 더 깎는다.
REFRESH_RETRY_SECONDS = 10 * 60
# 새로 받은 목록이 직전 사본의 이 비율에 못 미치면 응답 이상으로 보고 갈아 쓰지 않는다.
MIN_KEEP_RATIO = 0.5


class Snapshot(NamedTuple):
    rows: list[Any]
    fetched_at: float  # epoch 초

    @property
    def age_seconds(self) -> float:
        return max(time.time() - self.fetched_at, 0.0)


def read_snapshot_file(path: Path) -> dict[str, Any]:
    """사본 파일 전체. 없거나 깨졌으면 빈 dict."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("서버 사본을 읽지 못했습니다: %s", path)
        return {}
    return raw if isinstance(raw, dict) else {}


class SnapshotStore:
    """사본 파일 하나. 키마다 받은 시각(fetched_at)과 행 목록을 둔다.

    파일 내용을 메모리에 붙잡아 두지 않는다 — 클라이언트가 이미 파싱한 목록을 들고
    있으므로, 여기서 또 들면 큰 목록(환경배출시설 4만7천 곳)이 두 벌이 된다.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def load(self, key: str) -> Snapshot | None:
        if self.path is None:
            return None
        entry = read_snapshot_file(self.path).get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("rows"), list):
            return None
        try:
            fetched_at = float(entry.get("fetched_at") or 0.0)
        except (TypeError, ValueError):
            fetched_at = 0.0
        return Snapshot(entry["rows"], fetched_at)

    def save(self, key: str, rows: list[Any], fetched_at: float | None = None) -> bool:
        """키 하나를 갈아 쓴다. 임시 파일에 쓰고 바꿔치기해 쓰다 만 파일을 남기지 않는다."""

        if self.path is None:
            return False
        data = read_snapshot_file(self.path)
        data[key] = {"fetched_at": time.time() if fetched_at is None else fetched_at, "rows": rows}
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError:
            logger.warning("서버 사본을 쓰지 못했습니다: %s", self.path)
            return False
        return True


def as_of_date(fetched_at: float) -> str:
    """받은 시각 → 기준일(YYYY-MM-DD · 한국 표준시)."""

    return datetime.fromtimestamp(fetched_at, KST).strftime("%Y-%m-%d")


class SnapshotGuard:
    """전량 목록 하나의 사본 읽고 쓰기 + 「받은 시각 · 새로 받기 실패」 기록.

    목록(메모리 캐시)은 클라이언트가 들고, 이 객체는 그 목록이 언제 받은 것인지와
    새로 받기가 실패했는지를 기억한다. 심사 결과의 「서버 사본 사용(기준일 …)」 고지와
    7일 넘은 사본의 장애 승격이 여기서 나온다.
    """

    def __init__(
        self,
        path: Path | None,
        key: str,
        label: str,
        ttl_seconds: float = SNAPSHOT_TTL_SECONDS,
        max_stale_seconds: float = SNAPSHOT_MAX_STALE_SECONDS,
    ) -> None:
        self.store = SnapshotStore(path)
        self.key = key
        self.label = label
        self.ttl_seconds = ttl_seconds
        self.max_stale_seconds = max_stale_seconds
        # 지금 메모리에 든 목록을 받은 시각(epoch 초). 아직 없으면 None.
        self.fetched_at: float | None = None
        # 마지막 새로 받기 실패 사유. 성공하면 비운다.
        self.error = ""
        self._failed_at = 0.0
        self._disk_checked = False
        self._task: asyncio.Task[None] | None = None

    # -- 디스크 ---------------------------------------------------------------
    def load(self) -> Snapshot | None:
        """디스크 사본. 프로세스에서 한 번만 읽는다(없었으면 다음부터는 묻지 않는다)."""

        if self._disk_checked:
            return None
        self._disk_checked = True
        return self.store.load(self.key)

    def adopt(self, snapshot: Snapshot) -> None:
        """디스크 사본을 메모리에 올렸다 — 받은 시각을 그 사본의 것으로 둔다."""

        self.fetched_at = snapshot.fetched_at

    def mark_live(self, rows: list[Any]) -> None:
        """방금 API 에서 새로 받았다. 사본을 갈아 쓰고 실패 기록을 지운다."""

        self.fetched_at = time.time()
        self.error = ""
        self._disk_checked = True
        # 빈 목록으로 멀쩡한 사본을 덮지 않는다.
        if rows:
            self.store.save(self.key, rows, self.fetched_at)

    def mark_failed(self, exc: BaseException | str) -> None:
        self.error = str(exc) or exc.__class__.__name__
        self._failed_at = time.monotonic()

    # -- 상태 -----------------------------------------------------------------
    @property
    def age_seconds(self) -> float | None:
        return None if self.fetched_at is None else max(time.time() - self.fetched_at, 0.0)

    @property
    def stale(self) -> bool:
        """메모리의 목록이 받은 지 하루를 넘겼는가."""

        age = self.age_seconds
        return age is not None and age >= self.ttl_seconds

    @property
    def expired_and_failing(self) -> bool:
        """7일 넘은 사본인데 새로 받기도 실패했다 — 더는 사본으로 답하지 않는다."""

        age = self.age_seconds
        return bool(self.error) and age is not None and age > self.max_stale_seconds

    def can_retry(self) -> bool:
        return not self.error or time.monotonic() - self._failed_at >= REFRESH_RETRY_SECONDS

    @property
    def as_of(self) -> datetime | None:
        """메모리의 목록을 받은 시각(UTC). 결과의 원천 기준일로 쓴다."""

        return None if self.fetched_at is None else datetime.fromtimestamp(self.fetched_at, UTC)

    @property
    def notice(self) -> str:
        """받은 지 하루 넘은 목록으로 답하고 있으면 그 고지. 최신이면 빈 문자열."""

        if self.fetched_at is None or not self.stale:
            return ""
        date = as_of_date(self.fetched_at)
        if self.error:
            return f"서버 사본 사용(기준일 {date} · 실시간 조회 실패: {self.error})"
        return f"서버 사본 사용(기준일 {date} · 새 목록을 받는 중)"

    def outage_message(self) -> str:
        date = as_of_date(self.fetched_at) if self.fetched_at is not None else "없음"
        days = int(self.max_stale_seconds // 86400)
        return (
            f"{self.label} 조회 실패 — 서버 사본(기준일 {date})이 {days}일을 넘겨 쓰지 않습니다: "
            f"{self.error}"
        )

    def shrink_reason(self, new_count: int, previous_count: int) -> str:
        """새 목록이 직전 목록보다 크게 줄었으면 그 사유(갈아 쓰지 않는다). 정상이면 빈 문자열."""

        if previous_count <= 0 or new_count >= previous_count * MIN_KEEP_RATIO:
            return ""
        return (
            f"{self.label} 응답 이상 — 새 목록 {new_count:,}건이 직전 {previous_count:,}건의 "
            f"{int(MIN_KEEP_RATIO * 100)}%에 못 미쳐 사본을 유지합니다"
        )

    # -- 뒤에서 새로 받기 -------------------------------------------------------
    def refresh_in_background(
        self,
        refresh: Callable[[], Awaitable[Any]],
        cooldown: bool = True,
    ) -> asyncio.Task[None] | None:
        """새로 받기를 뒤에서 한 번만 건다. 이미 도는 중이면 그 작업을 돌려준다.

        실패는 삼키고 기록만 한다(사본을 계속 쓴다). 직전 실패 뒤 쿨다운 동안은 다시
        걸지 않는다. 러닝 루프가 없으면 아무 것도 하지 않는다.
        """

        if self._task is not None and not self._task.done():
            return self._task
        if cooldown and not self.can_retry():
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        async def run() -> None:
            try:
                await refresh()
            except Exception as exc:  # noqa: BLE001 — 갱신 실패는 사본을 그대로 쓴다
                self.mark_failed(exc)
                logger.warning("%s 새로 받기 실패(서버 사본 유지): %s", self.label, exc)

        self._task = loop.create_task(run())
        return self._task


# -- bundled_files · refresh_snapshots 가 쓰는 파일 요약 ---------------------------
def snapshot_row_count(path: Path) -> int:
    """사본 파일의 행 수(모든 키 합)."""

    return sum(
        len(entry.get("rows") or [])
        for entry in read_snapshot_file(path).values()
        if isinstance(entry, dict)
    )


def snapshot_as_of(path: Path) -> str:
    """사본 파일에서 가장 오래된 키의 받은 날짜(YYYY-MM-DD). 읽지 못하면 빈 문자열."""

    stamps: list[float] = []
    for entry in read_snapshot_file(path).values():
        if not isinstance(entry, dict):
            continue
        try:
            stamps.append(float(entry.get("fetched_at") or 0.0))
        except (TypeError, ValueError):
            continue
    stamps = [stamp for stamp in stamps if stamp > 0]
    return as_of_date(min(stamps)) if stamps else ""
