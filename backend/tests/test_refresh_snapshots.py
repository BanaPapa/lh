"""서버 사본 일괄 갱신 명령(python -m app.refresh_snapshots)과 사본 파일 목록.

야간 작업이 이 명령 하나로 사본·원장을 새로 만든다. 그래서
  · 단계 하나의 실패가 나머지를 막지 않고,
  · 마지막 줄이 `REFRESH_RESULT ok=<n> failed=<m>` 이며(파이프라인이 읽는다),
  · 성공이 하나라도 있으면 종료 코드가 0 이고,
  · `--list-files` 가 버킷과 주고받을 파일 이름(backend/data 기준)을 준다.
실제 원천은 부르지 않는다 — 단계는 가짜로 바꿔 끼운다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import bundled_files, refresh_snapshots
from app.refresh_snapshots import Step, StepResult, StepSkipped
from app.services import crematorium, kgs, lpg_municipal, lpg_seoul, safemap
from app.services.safemap_facilities import SNAPSHOT_LAYER_IDS, layer_snapshot_path
from app.services.snapshot_store import DATA_DIR


def _step(name: str, outcome, group: str = "") -> Step:
    async def run(_context) -> tuple[int | None, str]:
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "hang":
            await asyncio.sleep(60)
        return outcome, ""

    return Step(name, f"{name} 라벨", run, group=group)


class TestStepSelection:
    def test_only_and_skip_accept_commas_spaces_and_repeats(self) -> None:
        assert refresh_snapshots._names(["a,b", "c d", ""]) == {"a", "b", "c", "d"}
        assert refresh_snapshots._names(None) == set()

    def test_group_name_selects_every_layer_step(self) -> None:
        steps = [_step("kgs_lpg", 1), _step("l1", 1, group="safemap_layers"),
                 _step("l2", 1, group="safemap_layers")]

        only_layers = refresh_snapshots.select_steps(steps, {"safemap_layers"}, set())
        without_layers = refresh_snapshots.select_steps(steps, set(), {"safemap_layers", "nothing"})
        mixed = refresh_snapshots.select_steps(steps, {"kgs_lpg", "l2"}, {"l2"})

        assert [s.name for s in only_layers] == ["l1", "l2"]
        assert [s.name for s in without_layers] == ["kgs_lpg"]
        assert [s.name for s in mixed] == ["kgs_lpg"]

    def test_real_steps_cover_every_snapshot_and_have_unique_names(self) -> None:
        steps = refresh_snapshots.build_steps()
        names = [step.name for step in steps]

        assert len(names) == len(set(names))
        assert {"facilities", "factory_rows", "factory_geocode", "safemap_fuel", "kgs_lpg",
                "crematorium", "lpg_municipal", "lpg_seoul", "logistics_warehouse",
                "lpg_retailer"} <= set(names)
        # 2차 표준데이터·1차 소형 목록(2026-10-01 추가) — 사본 파일마다 단계가 있다.
        assert {"city_parks", "school_locations", "traditional_markets", "public_libraries",
                "korail_stations", "transfer_centers", "seoul_bus_stops", "cng_stations",
                "lpg_station_file", "cng_gyeongnam", "gg_chemical", "casino_registry",
                "city_gas_registry"} <= set(names)
        layer_steps = [s for s in steps if s.group == refresh_snapshots.LAYER_GROUP]
        assert [s.name for s in layer_steps] == [f"safemap_layer_{i}" for i in SNAPSHOT_LAYER_IDS]


class TestRun:
    def test_one_failing_step_does_not_stop_the_rest(self, capsys) -> None:
        steps = [
            _step("first", RuntimeError("429 Too Many Requests")),
            _step("second", 1234),
            _step("third", StepSkipped("키가 없어 건너뜁니다")),
            _step("fourth", "hang"),
            _step("fifth", 7),
        ]

        results = asyncio.run(refresh_snapshots.run(steps, timeout=0.05))

        assert [(r.name, r.status) for r in results] == [
            ("first", "fail"), ("second", "ok"), ("third", "skip"), ("fourth", "fail"),
            ("fifth", "ok"),
        ]
        assert results[0].detail == "429 Too Many Requests"
        assert results[1].rows == 1234
        lines = capsys.readouterr().out.strip().splitlines()
        # 단계마다 한 줄(상태 · 건수 · 초).
        assert len(lines) == 5
        assert lines[1].startswith("[ok  ] second") and "1,234건" in lines[1] and "초" in lines[1]
        assert lines[0].startswith("[fail] first") and "429" in lines[0]
        assert lines[2].startswith("[skip] third")
        # 건너뛴 단계는 성공·실패 어느 쪽에도 세지 않는다.
        assert refresh_snapshots.result_line(results) == "REFRESH_RESULT ok=2 failed=2"

    def test_exit_code_is_nonzero_only_when_everything_failed(self) -> None:
        ok = StepResult("a", "ok", 1, 0.1, "")
        fail = StepResult("b", "fail", None, 0.1, "x")
        skip = StepResult("c", "skip", None, 0.0, "y")

        assert refresh_snapshots.exit_code([ok, fail]) == 0
        assert refresh_snapshots.exit_code([fail, fail, skip]) == 1
        assert refresh_snapshots.exit_code([skip]) == 0
        assert refresh_snapshots.exit_code([ok]) == 0


class TestMain:
    def test_last_line_is_the_result_line(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr(
            refresh_snapshots, "build_steps",
            lambda: [_step("alpha", 3), _step("beta", RuntimeError("장애"))],
        )

        code = refresh_snapshots.main([])

        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[-1] == "REFRESH_RESULT ok=1 failed=1"
        assert code == 0

    def test_everything_failed_exits_nonzero(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr(
            refresh_snapshots, "build_steps", lambda: [_step("alpha", RuntimeError("장애"))]
        )

        assert refresh_snapshots.main([]) == 1
        assert capsys.readouterr().out.strip().splitlines()[-1] == "REFRESH_RESULT ok=0 failed=1"

    def test_only_and_skip_filter_steps(self, capsys, monkeypatch) -> None:
        ran: list[str] = []

        def tracked(name: str) -> Step:
            async def run(_context) -> tuple[int | None, str]:
                ran.append(name)
                return 1, ""

            return Step(name, name, run)

        monkeypatch.setattr(
            refresh_snapshots, "build_steps", lambda: [tracked("a"), tracked("b"), tracked("c")]
        )

        assert refresh_snapshots.main(["--only", "a,b", "--skip", "b"]) == 0
        assert ran == ["a"]
        capsys.readouterr()

    def test_unknown_step_name_is_rejected_before_running(self, capsys, monkeypatch) -> None:
        monkeypatch.setattr(refresh_snapshots, "build_steps", lambda: [_step("alpha", 1)])

        assert refresh_snapshots.main(["--only", "nope"]) == 2
        assert "알 수 없는 단계: nope" in capsys.readouterr().out

    def test_list_files_prints_snapshot_file_names_one_per_line(self, capsys) -> None:
        assert refresh_snapshots.main(["--list-files"]) == 0

        lines = capsys.readouterr().out.strip().splitlines()
        assert lines == bundled_files.snapshot_file_names()


class TestSnapshotFileList:
    def test_lists_api_copies_and_caches_relative_to_data_dir(self) -> None:
        names = bundled_files.snapshot_file_names()

        assert {
            "facilities.db", "factory_rows_cache.json", "factory_geocode_cache.json",
            "localdata_geocode_cache.json", "safemap_fuel_cache.json", "kgs_lpg_cache.json",
            "crematorium_cache.json", "lpg_municipal_cache.json", "lpg_seoul_cache.json",
        } <= set(names)
        assert {f"safemap_layer_{layer_id}.json" for layer_id in SNAPSHOT_LAYER_IDS} <= set(names)
        assert {
            "city_parks_cache.json", "school_locations_cache.json",
            "traditional_markets_cache.json", "public_libraries_cache.json",
            "korail_stations_cache.json", "transfer_centers_cache.json",
            "seoul_bus_stops_cache.json", "cng_stations_cache.json",
            "lpg_station_file_cache.json", "cng_gyeongnam_cache.json", "gg_chemical_cache.json",
            "casino_registry_cache.json", "city_gas_registry_cache.json",
        } <= set(names)
        assert len(names) == len(set(names))
        # 경로 구분자·상위 폴더 없이 data 폴더 기준 이름만.
        assert all("/" not in name and "\\" not in name for name in names)

    def test_git_tracked_files_are_not_listed(self) -> None:
        names = set(bundled_files.snapshot_file_names())

        tracked = {entry.path.name for entry in bundled_files.BUNDLED_FILES if not entry.api_snapshot}
        assert "lh_alignments.json" in tracked and "station_exits_jeonbuk.csv" in tracked
        assert names.isdisjoint(tracked)
        assert "rule_overrides.json" not in names

    def test_every_snapshot_is_registered_with_source_and_as_of_reader(self) -> None:
        entries = {entry.path: entry for entry in bundled_files.BUNDLED_FILES if entry.api_snapshot}
        expected = [
            safemap.DEFAULT_SNAPSHOT_PATH, kgs.DEFAULT_SNAPSHOT_PATH,
            crematorium.DEFAULT_SNAPSHOT_PATH, lpg_municipal.DEFAULT_SNAPSHOT_PATH,
            lpg_seoul.DEFAULT_SNAPSHOT_PATH,
            *(layer_snapshot_path(layer_id) for layer_id in SNAPSHOT_LAYER_IDS),
        ]

        for path in expected:
            entry = entries[path]
            assert path.parent == DATA_DIR
            assert entry.name and entry.purpose and entry.source
            # 기준일은 사본의 fetched_at 에서 읽는다.
            assert entry.as_of_reader is not None and entry.counter is not None

    def test_view_reads_count_and_as_of_from_the_snapshot(self, tmp_path: Path) -> None:
        import json

        path = tmp_path / "kgs_lpg_cache.json"
        path.write_text(
            json.dumps({"lpg_station": {"fetched_at": 1_759_276_800.0, "rows": [{}, {}, {}]}}),
            encoding="utf-8",
        )
        template = next(e for e in bundled_files.BUNDLED_FILES if e.id == "kgs_lpg")
        entry = bundled_files.BundledFile(
            id=template.id, name=template.name, path=path, purpose=template.purpose,
            source=template.source, as_of_reader=template.as_of_reader,
            counter=template.counter, api_snapshot=True,
        )

        view = entry.view()

        assert view.present and view.count == 3 and view.as_of == "2025-10-01"
        # 파일이 없으면 숨기지 않고 present=False 로 드러낸다.
        missing = bundled_files.BundledFile(
            id="x", name="x", path=tmp_path / "없음.json", purpose="p", source="s",
            as_of_reader=template.as_of_reader, counter=template.counter, api_snapshot=True,
        ).view()
        assert missing.present is False and missing.count is None and missing.as_of == ""


class TestWiring:
    def test_only_app_wiring_gives_snapshot_paths(self) -> None:
        from app.hazard_review import router as hazard_router
        from app.screening.router import _layer_feed

        hazard_router.get_hazard_service.cache_clear()
        try:
            service = hazard_router.get_hazard_service()
            assert service.safemap.snapshot.store.path == safemap.DEFAULT_SNAPSHOT_PATH
            assert service.kgs_lpg.snapshot.store.path == kgs.DEFAULT_SNAPSHOT_PATH
            assert service.crematorium.snapshot.store.path == crematorium.DEFAULT_SNAPSHOT_PATH
            assert service.lpg_seoul.snapshot.store.path == lpg_seoul.DEFAULT_SNAPSHOT_PATH
            assert service.lpg_municipal._snapshot_path == lpg_municipal.DEFAULT_SNAPSHOT_PATH
            for feed in (service.chemical_feed, service.emission_feed, service.waste_feed):
                assert feed.layer_id in SNAPSHOT_LAYER_IDS
                assert feed.snapshot.store.path == layer_snapshot_path(feed.layer_id)
        finally:
            hazard_router.get_hazard_service.cache_clear()

        for layer_id in ("IF_0022", "IF_0031", "IF_0034", "IF_0035", "IF_0038"):
            assert layer_id in SNAPSHOT_LAYER_IDS
            assert _layer_feed("k", layer_id).snapshot.store.path == layer_snapshot_path(layer_id)

    def test_every_whole_list_client_in_the_app_has_a_registered_snapshot(self) -> None:
        """앱이 조립한 전량 목록 클라이언트마다 사본 경로가 있고, 그 파일이 등록부·단계에 있다.

        사본으로 옮기지 않은 전량 목록 원천이 하나라도 남으면 켜진 직후 그 원천만 시간이
        넘어 대체 + 경고가 난다(2026-10-01 공원). 새 원천을 붙일 때 이 시험이 잡는다.
        """

        from app.hazard_review import router as hazard_router
        from app.screening import router as screening_router

        hazard_router.get_hazard_service.cache_clear()
        screening_router.get_screening_service.cache_clear()
        try:
            screening = screening_router.get_screening_service()
            hazard, amenities = screening.hazard, screening.amenities
            lists = {
                "cng": hazard.cng, "lpg_file": hazard.lpg_file,
                "cng_gyeongnam": hazard.cng_gyeongnam, "gg_chemical": hazard.gg_chemical,
                "school": amenities.school_client, "market": amenities.market_client,
                "park": amenities.park_client, "library": amenities.library_client,
                "korail": amenities.korail_client, "transfer": amenities.transfer_client,
                "seoul_bus": amenities.seoul_bus,
            }
            paths = {name: client._list.guard.store.path for name, client in lists.items()}
            paths["casino"] = hazard.casino_registry._coordinates.store.path
            paths["city_gas"] = hazard.city_gas_registry._coordinates.store.path
        finally:
            screening_router.get_screening_service.cache_clear()
            hazard_router.get_hazard_service.cache_clear()

        registered = set(bundled_files.snapshot_file_names())
        for name, path in paths.items():
            assert path is not None, name
            assert path.parent == DATA_DIR, name
            assert path.name in registered, name

    @pytest.mark.parametrize(
        "client",
        [
            safemap.SafemapFuelClient("k"),
            kgs.KgsLpgClient("k"),
            crematorium.CrematoriumClient("k"),
            lpg_seoul.SeoulLpgClient("k"),
        ],
    )
    def test_default_constructors_have_no_snapshot_path(self, client) -> None:
        # 테스트·도구가 만든 클라이언트는 실제 data 폴더에 쓰지 않는다.
        assert client.snapshot.store.path is None
