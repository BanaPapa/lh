"""전량 목록 원천의 서버 사본 — 식은 캐시는 장애가 아니고, 사본으로 낸 답은 드러난다.

2026-10-01 검수: 서버가 켜진 직후(Cloud Run 은 켜질 때마다) 첫 주택 심사가 주유소·LPG
종류에 「생활안전지도 전국 주유시설 현황 조회 실패 — 복구 후 재심사 필요」를 띄웠다. 전국
목록을 뒤에서 받는 중이었을 뿐 장애가 아니었다. 그리고 가스안전공사 LPG API 가 하루 종일
429 를 낸 날 7개 사업지가 검토 필요에 묶였다.

여기서 고정하는 것:
  · 사본이 있으면 켜지자마자 그것으로 답한다(원격 호출 없음 · 경고 없음).
  · 하루 넘은 사본은 그대로 쓰면서 뒤에서 새로 받아 파일을 갈아 쓴다.
  · 새로 받기가 실패하면 사본을 계속 쓰되 「서버 사본 사용(기준일 …)」으로 드러낸다.
  · 사본이 없는데 API 도 실패하거나, 7일 넘은 사본인데 API 가 실패하면 종전대로 장애다.
  · 생성자 기본값은 사본 경로 None — 테스트·도구가 실제 data 폴더에 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.models import Coordinates
from app.services import crematorium as crematorium_source
from app.services import kgs as kgs_source
from app.services import lpg_municipal as lpg_municipal_source
from app.services import lpg_seoul as lpg_seoul_source
from app.services import safemap as safemap_source
from app.services import safemap_facilities
from app.services.crematorium import Crematorium, CrematoriumAPIError, CrematoriumClient
from app.services.kgs import KgsLpgClient, PublicDataAPIError
from app.services.lpg_municipal import (
    LPG_MUNICIPAL_DATASETS,
    LpgMunicipalClient,
    MunicipalFacility,
)
from app.services.lpg_seoul import SEOUL_LPG_DATASET_ID, SeoulLpgClient
from app.services.safemap import SafemapAPIError, SafemapFuelClient, _station
from app.services.safemap_facilities import SafemapFacility, SafemapFacilityFeed
from tests.test_hazard_wiring import build_request, category_for, run_review

DAY = 24 * 3600
INCHEON = Coordinates(lat=37.45, lng=126.74)
SEOUL = Coordinates(lat=37.5, lng=127.0)

FUEL_ROW = {
    "uni_cd": "A0008251",
    "os_nm": "동남주유소",
    "poll_div_co": "SOL",
    "gpoll_div_co": "",
    "lpg_yn": "N",
    "van_adr": "인천광역시 남동구 만수동 1099",
    "new_adr": "인천광역시 남동구 담방로 64 (만수동)",
    "tel": "032-468-8851",
    "x": "14108535",
    "y": "4501386.0",
}
LPG_ROW = {
    "BSES_NM": "한빛충전소",
    "ADDR": "서울특별시 강남구 역삼동 1",
    "SECT_NM": "서울",
    "LAT": "37.5",
    "LOT": "127.0",
    "MGT_NM": "자동차",
}


def write_snapshot(path: Path, key: str, rows: list[Any], age_seconds: float) -> None:
    path.write_text(
        json.dumps({key: {"fetched_at": time.time() - age_seconds, "rows": rows}}, ensure_ascii=False),
        encoding="utf-8",
    )


def snapshot_entry(path: Path, key: str) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))[key]


def fuel_body(rows: list[dict]) -> dict:
    return {
        "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
        "body": {"totalCount": len(rows), "items": {"item": rows}},
    }


def fuel_snapshot_rows(count: int = 1) -> list[dict]:
    station = _station(FUEL_ROW)
    assert station is not None
    return [safemap_source.snapshot_row(station) for _ in range(count)]


def kgs_body(rows: list[dict]) -> dict:
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL"},
            "body": {"totalCount": 1, "items": {"item": rows}},
        }
    }


def counting(handler):
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return calls, httpx.MockTransport(wrapped)


# ---------------------------------------------------------------------------
# 생활안전지도 전국 주유시설(IF_0033)
# ---------------------------------------------------------------------------
class TestSafemapFuelSnapshot:
    def test_fresh_snapshot_answers_at_once_without_remote_call(self, tmp_path: Path) -> None:
        path = tmp_path / "fuel.json"
        write_snapshot(path, safemap_source.SNAPSHOT_KEY, fuel_snapshot_rows(), 3600)
        calls, transport = counting(lambda request: httpx.Response(500))
        client = SafemapFuelClient("k", transport=transport, snapshot_path=path)

        # 켜진 직후의 비차단 조회 — 콜드 신호 없이 곧바로 답한다.
        near = asyncio.run(client.stations_around_cached(INCHEON, 5000))

        assert [s.name for s in near] == ["동남주유소"]
        assert near[0].is_gas_station and not near[0].is_lpg_station
        assert calls == []
        # 하루 안의 사본은 종전 메모리 캐시와 같은 신선도다 — 고지를 달지 않는다.
        assert client.snapshot_notice == ""
        assert client.data_as_of is not None

    def test_cold_signal_only_without_any_snapshot(self, tmp_path: Path) -> None:
        calls, transport = counting(lambda request: httpx.Response(200, json=fuel_body([FUEL_ROW])))
        client = SafemapFuelClient("k", transport=transport, snapshot_path=tmp_path / "fuel.json")

        async def scenario() -> None:
            with pytest.raises(SafemapAPIError):
                await client.stations_around_cached(INCHEON, 5000)
            assert client._prefetch_task is not None
            await client._prefetch_task
            assert len(await client.stations_around_cached(INCHEON, 5000)) == 1

        asyncio.run(scenario())
        # 받은 목록을 사본으로 남겼다 — 다음에 켜지는 서버는 식지 않는다.
        entry = snapshot_entry(tmp_path / "fuel.json", safemap_source.SNAPSHOT_KEY)
        assert [row["name"] for row in entry["rows"]] == ["동남주유소"]

    def test_stale_snapshot_is_served_and_refreshed_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "fuel.json"
        write_snapshot(path, safemap_source.SNAPSHOT_KEY, fuel_snapshot_rows(), 2 * DAY)
        renamed = dict(FUEL_ROW, os_nm="새이름주유소")
        calls, transport = counting(lambda request: httpx.Response(200, json=fuel_body([renamed])))
        client = SafemapFuelClient("k", transport=transport, snapshot_path=path)

        async def scenario() -> None:
            # 하루 넘은 사본 — 기다리지 않고 그 사본으로 답한다.
            near = await client.stations_around_cached(INCHEON, 5000)
            assert [s.name for s in near] == ["동남주유소"]
            assert "서버 사본 사용(기준일 " in client.snapshot_notice
            assert client._prefetch_task is not None
            await client._prefetch_task
            # 뒤에서 새로 받았다 — 이제 실시간 목록이고 고지가 사라진다.
            near = await client.stations_around_cached(INCHEON, 5000)
            assert [s.name for s in near] == ["새이름주유소"]
            assert client.snapshot_notice == ""

        asyncio.run(scenario())
        entry = snapshot_entry(path, safemap_source.SNAPSHOT_KEY)
        assert entry["rows"][0]["name"] == "새이름주유소"
        assert time.time() - entry["fetched_at"] < 60

    def test_failing_api_keeps_serving_snapshot_with_notice(self, tmp_path: Path) -> None:
        path = tmp_path / "fuel.json"
        write_snapshot(path, safemap_source.SNAPSHOT_KEY, fuel_snapshot_rows(), 2 * DAY)
        calls, transport = counting(lambda request: httpx.Response(503))
        client = SafemapFuelClient("k", transport=transport, snapshot_path=path)

        async def scenario() -> None:
            assert len(await client.stations_around_cached(INCHEON, 5000)) == 1
            await client._prefetch_task  # type: ignore[misc]
            attempts = len(calls)
            # 새로 받기가 실패해도 사본으로 계속 답하고, 그 사실을 고지에 남긴다.
            assert len(await client.stations_around_cached(INCHEON, 5000)) == 1
            assert "실시간 조회 실패" in client.snapshot_notice
            assert "503" in client.snapshot_notice
            # 실패 직후에는 쿨다운 — 심사마다 전량 조회를 다시 걸지 않는다.
            await asyncio.sleep(0)
            assert len(calls) == attempts

        asyncio.run(scenario())
        # 실패한 갱신은 사본을 건드리지 않는다.
        assert snapshot_entry(path, safemap_source.SNAPSHOT_KEY)["rows"][0]["name"] == "동남주유소"

    def test_snapshot_older_than_a_week_with_failing_api_is_an_outage(self, tmp_path: Path) -> None:
        path = tmp_path / "fuel.json"
        write_snapshot(path, safemap_source.SNAPSHOT_KEY, fuel_snapshot_rows(), 8 * DAY)
        calls, transport = counting(lambda request: httpx.Response(503))
        client = SafemapFuelClient("k", transport=transport, snapshot_path=path)

        async def scenario() -> None:
            # 아직 새로 받기를 해 보지 않았다 — 식은 캐시는 장애가 아니므로 사본으로 답한다.
            assert len(await client.stations_around_cached(INCHEON, 5000)) == 1
            await client._prefetch_task  # type: ignore[misc]
            # 7일 넘은 사본인데 API 도 실패했다 — 더는 사본으로 답하지 않는다.
            with pytest.raises(SafemapAPIError, match="7일"):
                await client.stations_around_cached(INCHEON, 5000)

        asyncio.run(scenario())

    def test_refresh_rejects_a_list_that_shrank_by_more_than_half(self, tmp_path: Path) -> None:
        path = tmp_path / "fuel.json"
        write_snapshot(path, safemap_source.SNAPSHOT_KEY, fuel_snapshot_rows(10), 2 * DAY)
        calls, transport = counting(lambda request: httpx.Response(200, json=fuel_body([FUEL_ROW])))
        client = SafemapFuelClient("k", transport=transport, snapshot_path=path)

        with pytest.raises(SafemapAPIError, match="응답 이상"):
            asyncio.run(client.refresh(force=True))

        assert len(snapshot_entry(path, safemap_source.SNAPSHOT_KEY)["rows"]) == 10

    def test_default_constructor_never_writes_a_snapshot(self, tmp_path: Path, monkeypatch) -> None:
        # 사본 경로는 앱 배선만 준다. 기본 생성자는 받아도 파일을 쓰지 않는다.
        monkeypatch.chdir(tmp_path)
        calls, transport = counting(lambda request: httpx.Response(200, json=fuel_body([FUEL_ROW])))
        client = SafemapFuelClient("k", transport=transport)

        assert len(asyncio.run(client.all_stations())) == 1
        assert client.snapshot.store.path is None
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 가스안전공사 전국 LPG 충전소
# ---------------------------------------------------------------------------
class TestKgsLpgSnapshot:
    def test_429_day_keeps_yesterdays_list(self, tmp_path: Path) -> None:
        path = tmp_path / "kgs.json"
        write_snapshot(path, kgs_source.SNAPSHOT_KEY, [LPG_ROW], 2 * DAY)
        calls, transport = counting(lambda request: httpx.Response(429))
        client = KgsLpgClient("k", transport=transport, snapshot_path=path)

        async def scenario() -> None:
            # 판정 경로(stations_around)가 실패하지 않고 받아 둔 목록으로 답한다.
            near = await client.stations_around(SEOUL, 1000)
            assert [s.name for s in near] == ["한빛충전소"]
            await client.snapshot._task  # type: ignore[misc]
            near = await client.stations_around(SEOUL, 1000)
            assert [s.name for s in near] == ["한빛충전소"]
            assert "서버 사본 사용(기준일 " in client.snapshot_notice
            assert "429" in client.snapshot_notice

        asyncio.run(scenario())

    def test_no_snapshot_and_failing_api_is_still_an_outage(self, tmp_path: Path) -> None:
        calls, transport = counting(lambda request: httpx.Response(429))
        client = KgsLpgClient("k", transport=transport, snapshot_path=tmp_path / "kgs.json")

        with pytest.raises(PublicDataAPIError):
            asyncio.run(client.stations_around(SEOUL, 1000))
        assert not (tmp_path / "kgs.json").exists()

    def test_week_old_snapshot_with_failing_api_is_an_outage(self, tmp_path: Path) -> None:
        path = tmp_path / "kgs.json"
        write_snapshot(path, kgs_source.SNAPSHOT_KEY, [LPG_ROW], 8 * DAY)
        calls, transport = counting(lambda request: httpx.Response(429))
        client = KgsLpgClient("k", transport=transport, snapshot_path=path)

        async def scenario() -> None:
            assert len(await client.stations_around(SEOUL, 1000)) == 1
            await client.snapshot._task  # type: ignore[misc]
            with pytest.raises(PublicDataAPIError, match="7일"):
                await client.stations_around(SEOUL, 1000)

        asyncio.run(scenario())

    def test_live_fetch_writes_trimmed_snapshot_and_fresh_one_skips_remote(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "kgs.json"
        row = dict(LPG_ROW, UNUSED_COLUMN="버릴 값")
        calls, transport = counting(lambda request: httpx.Response(200, json=kgs_body([row])))

        first = KgsLpgClient("k", transport=transport, snapshot_path=path)
        assert len(asyncio.run(first.all_stations())) == 1
        stored = snapshot_entry(path, kgs_source.SNAPSHOT_KEY)["rows"]
        assert stored == [{column: LPG_ROW.get(column) for column in kgs_source.SNAPSHOT_COLUMNS}]

        # 새로 켜진 서버 — 사본으로 곧바로 답하고 원격 호출은 하지 않는다.
        attempts = len(calls)
        second = KgsLpgClient("k", transport=transport, snapshot_path=path)
        assert [s.name for s in asyncio.run(second.all_stations())] == ["한빛충전소"]
        assert len(calls) == attempts
        assert second.snapshot_notice == ""

    def test_quarantine_still_applies_to_snapshot_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "kgs.json"
        bad = dict(LPG_ROW, BSES_NM="서울시청좌표", LAT="37.5665", LOT="126.978")
        write_snapshot(path, kgs_source.SNAPSHOT_KEY, [LPG_ROW, bad], 3600)
        client = KgsLpgClient("k", transport=httpx.MockTransport(lambda r: httpx.Response(500)),
                              snapshot_path=path)

        stations = asyncio.run(client.all_stations())

        assert [s.name for s in stations] == ["한빛충전소"]
        assert [q.name for q in client.quarantined] == ["서울시청좌표"]


# ---------------------------------------------------------------------------
# 생활안전지도 시설 레이어
# ---------------------------------------------------------------------------
OFFICE_FACILITY = SafemapFacility(
    "IF_0031", "obj-1", "용봉동행정복지센터", "광주광역시 북구 용봉동 1", "주민센터",
    Coordinates(lat=35.1554, lng=126.9082),
)


class TestLayerFeedSnapshot:
    def test_snapshot_gives_fast_path_without_remote_call(self, tmp_path: Path) -> None:
        path = tmp_path / "layer.json"
        write_snapshot(path, "IF_0031", [safemap_facilities.snapshot_row(OFFICE_FACILITY)], 3600)
        calls, transport = counting(lambda request: httpx.Response(500))
        feed = SafemapFacilityFeed("key", "IF_0031", transport=transport, snapshot_path=path)

        # 판정 경로가 건너뛰지 않는다(has_fast_path) — 켜진 직후 참고 핀·주석이 빠지지 않는다.
        assert feed.has_fast_path is True
        near = asyncio.run(feed.facilities_around(OFFICE_FACILITY.coordinates, 500))

        assert near == [OFFICE_FACILITY]
        assert calls == []

    def test_cold_feed_without_snapshot_has_no_fast_path(self, tmp_path: Path) -> None:
        feed = SafemapFacilityFeed("key", "IF_0031", snapshot_path=tmp_path / "layer.json")
        assert feed.has_fast_path is False

    def test_stale_snapshot_is_served_then_replaced(self, tmp_path: Path) -> None:
        path = tmp_path / "layer.json"
        write_snapshot(path, "IF_0031", [safemap_facilities.snapshot_row(OFFICE_FACILITY)], 2 * DAY)
        row = {"objt_id": "obj-2", "fclty_nm": "새청사", "fclty_ty": "본청",
               "rn_adres": "광주광역시 북구 1", "x": "14127365.0", "y": "4183000.0"}
        payload = {"header": {"resultCode": "00"}, "body": {"totalCount": 1, "items": [row]}}
        calls, transport = counting(lambda request: httpx.Response(200, json=payload))
        feed = SafemapFacilityFeed("key", "IF_0031", transport=transport, snapshot_path=path)

        async def scenario() -> None:
            assert [f.name for f in await feed.all_facilities()] == ["용봉동행정복지센터"]
            assert "서버 사본 사용" in feed.snapshot_notice
            await feed.snapshot._task  # type: ignore[misc]
            assert [f.name for f in await feed.all_facilities()] == ["새청사"]
            assert feed.snapshot_notice == ""

        asyncio.run(scenario())
        assert snapshot_entry(path, "IF_0031")["rows"][0]["name"] == "새청사"


# ---------------------------------------------------------------------------
# 좌표를 지오코딩으로 붙이는 원천 — 사본의 좌표를 물려받는다
# ---------------------------------------------------------------------------
def crematorium_body(rows: list[dict]) -> dict:
    return {"resultCode": "00", "resultMsg": "NORMAL SERVICE", "totalCount": len(rows), "items": rows}


CREMATORIUM_ROW = {
    "fcltNm": "천안추모공원", "addr": "충청남도 천안시 1", "ctpv": "충청남도",
    "sigungu": "천안시", "gubun": "공설", "brzCnt": "9",
}
KNOWN_CREMATORIUM = Crematorium(
    facility_id="천안추모공원:충청남도 천안시 1", name="천안추모공원",
    coordinates=Coordinates(lat=36.75, lng=127.10), address="충청남도 천안시 1",
    region="충청남도", sigungu="천안시", gubun="공설", brazier_count="9",
)


class TestCrematoriumSnapshot:
    def test_snapshot_skips_fetch_and_geocoding(self, tmp_path: Path) -> None:
        path = tmp_path / "crematorium.json"
        write_snapshot(
            path, crematorium_source.SNAPSHOT_KEY,
            [crematorium_source.snapshot_row(KNOWN_CREMATORIUM)], 3600,
        )
        geocoded: list[str] = []

        async def geocode(address: str) -> Coordinates | None:
            geocoded.append(address)
            return None

        calls, transport = counting(lambda request: httpx.Response(500))
        client = CrematoriumClient("k", geocode=geocode, transport=transport, snapshot_path=path)

        assert asyncio.run(client.all_crematoriums()) == [KNOWN_CREMATORIUM]
        assert calls == [] and geocoded == []

    def test_refresh_geocodes_new_addresses_only(self, tmp_path: Path) -> None:
        path = tmp_path / "crematorium.json"
        write_snapshot(
            path, crematorium_source.SNAPSHOT_KEY,
            [crematorium_source.snapshot_row(KNOWN_CREMATORIUM)], 2 * DAY,
        )
        new_row = dict(CREMATORIUM_ROW, fcltNm="새화장장", addr="전북특별자치도 익산시 1")
        geocoded: list[str] = []

        async def geocode(address: str) -> Coordinates | None:
            geocoded.append(address)
            return Coordinates(lat=35.95, lng=126.95)

        calls, transport = counting(
            lambda request: httpx.Response(200, json=crematorium_body([CREMATORIUM_ROW, new_row]))
        )
        client = CrematoriumClient("k", geocode=geocode, transport=transport, snapshot_path=path)

        facilities = asyncio.run(client.refresh(force=True))

        # 이름·주소가 그대로인 시설은 사본의 좌표를 다시 쓰고, 새 주소만 지오코딩한다.
        assert geocoded == ["전북특별자치도 익산시 1"]
        assert facilities[0].coordinates == KNOWN_CREMATORIUM.coordinates
        assert len(snapshot_entry(path, crematorium_source.SNAPSHOT_KEY)["rows"]) == 2

    def test_week_old_snapshot_with_failing_api_is_an_outage(self, tmp_path: Path) -> None:
        path = tmp_path / "crematorium.json"
        write_snapshot(
            path, crematorium_source.SNAPSHOT_KEY,
            [crematorium_source.snapshot_row(KNOWN_CREMATORIUM)], 8 * DAY,
        )

        async def geocode(address: str) -> Coordinates | None:
            return None

        client = CrematoriumClient(
            "k", geocode=geocode, snapshot_path=path,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        )

        async def scenario() -> None:
            assert len(await client.all_crematoriums()) == 1
            await client.snapshot._task  # type: ignore[misc]
            with pytest.raises(CrematoriumAPIError, match="7일"):
                await client.all_crematoriums()

        asyncio.run(scenario())


SEOUL_FACILITY = MunicipalFacility(
    SEOUL_LPG_DATASET_ID, "서울시 액화석유가스업 현황(열린데이터광장)", "p-1", "강서가스",
    "서울특별시 강서구 등촌동 684번지 2호", "판매", "판매사업", "20220729",
    Coordinates(lat=37.5530, lng=126.8590),
)


class TestSeoulLpgSnapshot:
    def test_snapshot_makes_source_ready_without_geocoding(self, tmp_path: Path) -> None:
        path = tmp_path / "seoul.json"
        write_snapshot(
            path, lpg_seoul_source.SNAPSHOT_KEY,
            [lpg_municipal_source.snapshot_row(SEOUL_FACILITY)], 2 * DAY,
        )
        geocoded: list[str] = []

        async def geocode(address: str) -> Coordinates | None:
            geocoded.append(address)
            return None

        client = SeoulLpgClient(
            "KEY", geocode=geocode, snapshot_path=path,
            transport=httpx.MockTransport(lambda request: httpx.Response(500)),
        )

        async def scenario() -> None:
            # 켜진 직후에도 준비(has_fast_path) — 520건 지오코딩을 기다리지 않는다.
            assert client.has_fast_path is True
            lookup = await client.facilities_for_site(
                "서울특별시 강서구 등촌동 1", SEOUL_FACILITY.coordinates, 500
            )
            assert [f.name for f in lookup.facilities] == ["강서가스"]
            assert lookup.covered
            # 하루 넘은 사본이라 고지가 따라온다.
            assert "서버 사본 사용" in lookup.snapshot_notice
            await client.snapshot._task  # type: ignore[misc]

        asyncio.run(scenario())
        assert geocoded == []


class TestMunicipalLpgSnapshot:
    def test_dataset_snapshot_answers_first_screening_and_refresh_reuses_coordinates(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "municipal.json"
        dataset = next(d for d in LPG_MUNICIPAL_DATASETS if d.dataset_id == "15064201")
        known = MunicipalFacility(
            dataset.dataset_id, dataset.title, "15064201-0", "부안가스",
            "전북특별자치도 부안군 행안면 신기리 186", "판매", "판매사업", "신규",
            Coordinates(lat=35.72, lng=126.72),
        )
        write_snapshot(path, dataset.dataset_id, [lpg_municipal_source.snapshot_row(known)], 3600)
        rows = [
            {"사업소주소": "전북특별자치도 부안군 행안면 신기리 186", "사업종류": "판매사업",
             "상호": "부안가스", "영업구분": "신규"},
            {"사업소주소": "전북특별자치도 부안군 부안읍 당산로 91", "사업종류": "저장소",
             "상호": "부안저장", "영업구분": "정상"},
        ]
        geocoded: list[str] = []

        async def geocode(address: str) -> Coordinates | None:
            geocoded.append(address)
            return Coordinates(lat=35.73, lng=126.73)

        calls, transport = counting(
            lambda request: httpx.Response(200, json={"data": rows, "totalCount": len(rows)})
        )
        client = LpgMunicipalClient("k", geocode=geocode, transport=transport, snapshot_path=path)

        lookup = asyncio.run(
            client.facilities_for_site("전북특별자치도 부안군 부안읍 당산로 91", known.coordinates, 500)
        )
        # 사본으로 곧바로 답했다(파일 수신·지오코딩 없음 · 최신이라 고지 없음).
        assert [f.name for f in lookup.facilities] == ["부안가스"]
        assert calls == [] and geocoded == [] and lookup.snapshot_notice == ""

        facilities, _failures = asyncio.run(client.refresh_dataset(dataset, force=True))
        # 새로 받을 때 상호·주소가 그대로인 행은 사본 좌표를 물려받는다.
        assert [f.name for f in facilities] == ["부안가스", "부안저장"]
        assert facilities[0].coordinates == known.coordinates
        assert len(geocoded) >= 1 and all("당산로" in address for address in geocoded)
        assert len(snapshot_entry(path, dataset.dataset_id)["rows"]) == 2


# ---------------------------------------------------------------------------
# 판정 결과에 드러나는 모습 — 거짓 경고는 없애고, 사본으로 낸 답은 밝힌다
# ---------------------------------------------------------------------------
class TestSnapshotInReview:
    def test_fuel_snapshot_removes_the_cold_start_false_alert(self, tmp_path: Path) -> None:
        # 서버가 막 켜졌고 전국 목록은 아직 받지 못했다. 사본이 있으면 「조회 실패」가 아니다.
        path = tmp_path / "fuel.json"
        write_snapshot(path, safemap_source.SNAPSHOT_KEY, fuel_snapshot_rows(), 3600)
        safemap = SafemapFuelClient(
            "k", snapshot_path=path,
            transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        )

        result = run_review(
            build_request("house", "general", geometry_source="provisional_polygon"),
            safemap=safemap,
        )

        gas = category_for(result, "gas_station")
        assert gas.status == "no_conflict_in_snapshot", gas.note
        assert "조회 실패" not in gas.note and "서버 사본" not in gas.note
        assert [s for s in result.sources if s.state == "failed"] == []
        assert [chip.label for chip in gas.data_sources if chip.detail == "safemap"] == [
            "생활안전지도 주유·가스 IF_0033"
        ]

    def test_kgs_429_with_snapshot_is_a_dated_note_not_review_required(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "kgs.json"
        write_snapshot(path, kgs_source.SNAPSHOT_KEY, [LPG_ROW], 2 * DAY)
        kgs = KgsLpgClient(
            "k", snapshot_path=path,
            transport=httpx.MockTransport(lambda request: httpx.Response(429)),
        )

        async def refresh_fails_once() -> None:
            await kgs.all_stations()
            await kgs.snapshot._task  # type: ignore[misc]

        asyncio.run(refresh_fails_once())
        result = run_review(build_request("house", "general"), kgs_lpg=kgs)

        lpg = category_for(result, "lpg_station")
        # 어제 받아 둔 목록으로 판정했다 — 검토 필요로 묶이지 않는다.
        assert lpg.status == "no_conflict_in_snapshot", lpg.note
        # 그러나 실시간 답처럼 보이지 않는다: 비고·칩·원천 상태에 기준일이 드러난다.
        assert "서버 사본 사용(기준일 " in lpg.note and "429" in lpg.note
        chip = next(chip for chip in lpg.data_sources if chip.detail == "kgs")
        assert chip.label.endswith("서버 사본")
        row = next(source for source in result.sources if source.source_id == "kgs-lpg")
        assert row.state == "connected"
        assert "서버 사본 사용" in row.coverage_note
        assert row.as_of is not None and (result.created_at - row.as_of).days >= 1
        assert [s for s in result.sources if s.state == "failed"] == []

    def test_kgs_429_without_snapshot_is_still_an_outage(self, tmp_path: Path) -> None:
        kgs = KgsLpgClient(
            "k", snapshot_path=tmp_path / "kgs.json",
            transport=httpx.MockTransport(lambda request: httpx.Response(429)),
        )

        result = run_review(build_request("house", "general"), kgs_lpg=kgs)

        lpg = category_for(result, "lpg_station")
        assert lpg.status == "review_required"
        assert "조회 실패 — 복구 후 재심사 필요" in lpg.note
        assert any(s.source_id == "kgs-lpg" and s.state == "failed" for s in result.sources)

    def test_week_old_kgs_snapshot_with_failing_api_is_an_outage(self, tmp_path: Path) -> None:
        path = tmp_path / "kgs.json"
        write_snapshot(path, kgs_source.SNAPSHOT_KEY, [LPG_ROW], 8 * DAY)
        kgs = KgsLpgClient(
            "k", snapshot_path=path,
            transport=httpx.MockTransport(lambda request: httpx.Response(429)),
        )

        async def refresh_fails_once() -> None:
            await kgs.all_stations()
            await kgs.snapshot._task  # type: ignore[misc]

        asyncio.run(refresh_fails_once())
        result = run_review(build_request("house", "general"), kgs_lpg=kgs)

        lpg = category_for(result, "lpg_station")
        assert lpg.status == "review_required"
        assert "조회 실패 — 복구 후 재심사 필요" in lpg.note

    def test_stale_fuel_snapshot_shows_in_source_list_without_moving_scores(
        self, tmp_path: Path
    ) -> None:
        fresh_path, stale_path = tmp_path / "fresh.json", tmp_path / "stale.json"
        write_snapshot(fresh_path, safemap_source.SNAPSHOT_KEY, fuel_snapshot_rows(), 3600)
        write_snapshot(stale_path, safemap_source.SNAPSHOT_KEY, fuel_snapshot_rows(), 2 * DAY)
        failing = httpx.MockTransport(lambda request: httpx.Response(503))
        request = build_request("house", "general", geometry_source="provisional_polygon")

        fresh = run_review(
            request, safemap=SafemapFuelClient("k", transport=failing, snapshot_path=fresh_path)
        )
        stale = run_review(
            request, safemap=SafemapFuelClient("k", transport=failing, snapshot_path=stale_path)
        )

        gas = category_for(stale, "gas_station")
        assert gas.status == "no_conflict_in_snapshot"
        assert "서버 사본 사용(기준일 " in gas.note
        row = next(s for s in stale.sources if s.source_id == "snapshot-safemap")
        assert row.state == "partial" and "서버 사본 사용" in row.coverage_note
        # 고지 줄은 연결·신선도 점수를 움직이지 않는다.
        assert stale.data_completeness == fresh.data_completeness
        assert stale.source_freshness == fresh.source_freshness
