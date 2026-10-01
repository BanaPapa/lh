"""서버 사본 저장소 — 파일 읽고 쓰기와 「오래됨 · 새로 받기 실패」 판단.

식은 캐시는 장애가 아니고, 사본으로 낸 답은 실시간 답처럼 보이면 안 된다. 그 두 규칙이
여기서 갈린다: 하루 넘은 사본은 고지를 달고, 7일 넘은 사본은 새로 받기가 실패했을 때만
장애로 올린다.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.services import snapshot_store
from app.services.snapshot_store import (
    SNAPSHOT_MAX_STALE_SECONDS,
    SNAPSHOT_TTL_SECONDS,
    Snapshot,
    SnapshotGuard,
    SnapshotStore,
    snapshot_as_of,
    snapshot_row_count,
)

DAY = 24 * 3600


class TestSnapshotStore:
    def test_round_trip_keeps_rows_and_fetched_at(self, tmp_path: Path) -> None:
        store = SnapshotStore(tmp_path / "cache.json")
        assert store.save("a", [{"name": "가"}], fetched_at=1_700_000_000.0)

        snapshot = store.load("a")

        assert snapshot is not None
        assert snapshot.rows == [{"name": "가"}]
        assert snapshot.fetched_at == 1_700_000_000.0
        # 임시 파일을 남기지 않는다.
        assert [p.name for p in tmp_path.iterdir()] == ["cache.json"]

    def test_saving_one_key_keeps_the_others(self, tmp_path: Path) -> None:
        store = SnapshotStore(tmp_path / "cache.json")
        store.save("a", [1, 2])
        store.save("b", [3])

        assert store.load("a").rows == [1, 2]  # type: ignore[union-attr]
        assert store.load("b").rows == [3]  # type: ignore[union-attr]

    def test_missing_or_broken_file_is_no_snapshot(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        assert SnapshotStore(path).load("a") is None
        path.write_text("{ 깨진 json", encoding="utf-8")
        assert SnapshotStore(path).load("a") is None

    def test_no_path_never_touches_disk(self, tmp_path: Path) -> None:
        store = SnapshotStore(None)
        assert store.enabled is False
        assert store.save("a", [1]) is False
        assert store.load("a") is None

    def test_file_summary_counts_all_keys_and_reports_oldest_date(self, tmp_path: Path) -> None:
        path = tmp_path / "cache.json"
        path.write_text(
            json.dumps(
                {
                    "a": {"fetched_at": 1_759_276_800.0, "rows": [1, 2, 3]},  # 2025-10-01 KST
                    "b": {"fetched_at": 1_759_363_200.0, "rows": [4]},
                }
            ),
            encoding="utf-8",
        )
        assert snapshot_row_count(path) == 4
        assert snapshot_as_of(path) == "2025-10-01"
        assert snapshot_as_of(tmp_path / "없음.json") == ""


class TestSnapshotGuard:
    def test_live_data_has_no_notice(self, tmp_path: Path) -> None:
        guard = SnapshotGuard(tmp_path / "c.json", "k", "원천")
        guard.mark_live([{"a": 1}])

        assert guard.notice == ""
        assert guard.stale is False
        assert guard.as_of is not None
        # 사본 파일을 함께 썼다(write-through).
        assert SnapshotStore(tmp_path / "c.json").load("k").rows == [{"a": 1}]  # type: ignore[union-attr]

    def test_empty_live_result_does_not_overwrite_snapshot(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        SnapshotStore(path).save("k", [1, 2, 3])
        guard = SnapshotGuard(path, "k", "원천")

        guard.mark_live([])

        assert SnapshotStore(path).load("k").rows == [1, 2, 3]  # type: ignore[union-attr]

    def test_stale_snapshot_is_announced_with_its_date(self, tmp_path: Path) -> None:
        guard = SnapshotGuard(tmp_path / "c.json", "k", "원천")
        guard.adopt(Snapshot([1], time.time() - 2 * DAY))

        assert guard.stale is True
        assert "서버 사본 사용(기준일 " in guard.notice
        assert "받는 중" in guard.notice
        # 새로 받기가 실패했으면 그 사유를 밝힌다.
        guard.mark_failed("응답 오류 (429)")
        assert "실시간 조회 실패: 응답 오류 (429)" in guard.notice

    def test_fresh_snapshot_is_not_announced(self, tmp_path: Path) -> None:
        guard = SnapshotGuard(tmp_path / "c.json", "k", "원천")
        guard.adopt(Snapshot([1], time.time() - SNAPSHOT_TTL_SECONDS / 2))
        assert guard.notice == ""

    def test_outage_only_when_older_than_limit_and_refresh_failed(self, tmp_path: Path) -> None:
        guard = SnapshotGuard(tmp_path / "c.json", "k", "원천")
        guard.adopt(Snapshot([1], time.time() - SNAPSHOT_MAX_STALE_SECONDS - DAY))
        # 오래됐어도 새로 받기를 아직 해 보지 않았으면 장애가 아니다(식은 캐시).
        assert guard.expired_and_failing is False
        guard.mark_failed("429")
        assert guard.expired_and_failing is True
        assert "7일" in guard.outage_message()

        recent = SnapshotGuard(tmp_path / "c.json", "k2", "원천")
        recent.adopt(Snapshot([1], time.time() - 2 * DAY))
        recent.mark_failed("429")
        assert recent.expired_and_failing is False

    def test_disk_is_read_once_per_process(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        SnapshotStore(path).save("k", [1])
        guard = SnapshotGuard(path, "k", "원천")

        assert guard.load() is not None
        assert guard.load() is None

    def test_shrink_reason_rejects_a_list_under_half_the_previous(self, tmp_path: Path) -> None:
        guard = SnapshotGuard(tmp_path / "c.json", "k", "원천")
        assert guard.shrink_reason(40, 100)
        assert guard.shrink_reason(0, 100)
        assert guard.shrink_reason(60, 100) == ""
        # 직전 목록이 없으면 비교하지 않는다.
        assert guard.shrink_reason(0, 0) == ""

    def test_background_refresh_runs_once_and_backs_off_after_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guard = SnapshotGuard(tmp_path / "c.json", "k", "원천")
        calls: list[int] = []

        async def failing() -> None:
            calls.append(1)
            await asyncio.sleep(0)
            raise RuntimeError("429")

        async def scenario() -> None:
            first = guard.refresh_in_background(failing)
            # 도는 중에는 같은 작업을 돌려준다(동시 1회).
            assert guard.refresh_in_background(failing) is first
            assert first is not None
            await first
            assert guard.error == "429"
            # 실패 직후에는 쿨다운 — 다시 걸지 않는다.
            assert guard.refresh_in_background(failing) is None
            # 쿨다운이 지나면 다시 시도한다.
            monkeypatch.setattr(snapshot_store, "REFRESH_RETRY_SECONDS", 0)
            again = guard.refresh_in_background(failing)
            assert again is not None
            await again

        asyncio.run(scenario())
        assert len(calls) == 2

    def test_background_refresh_without_running_loop_is_a_no_op(self, tmp_path: Path) -> None:
        guard = SnapshotGuard(tmp_path / "c.json", "k", "원천")

        async def never() -> None:  # pragma: no cover — 불리지 않는다
            raise AssertionError

        assert guard.refresh_in_background(never) is None
