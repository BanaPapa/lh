"""나머지 전량 목록 원천의 서버 사본 — 공원·학교·전통시장·도서관·역·환승센터·CNG·LPG 파일 등.

2026-10-01 배포 뒤 검수: 주유소·LPG 거짓 경고는 사라졌지만, 서버가 켜진 직후 첫 심사에서
2차 「공원」이 전량 19쪽을 받다가(동시 예열로 붐빌 때 27초) 시간이 넘어 카카오 대체 + 원천
경고가 한 번 떴다. 사본으로 옮기지 않은 전량 목록 원천이 모두 같은 틈을 갖고 있었다.

같은 규칙을 모든 전량 목록 원천에 고정한다(SnapshotList 한 곳에서 한다):
  · 신선한 사본 → 원격 호출 없이 곧바로 답한다.
  · 하루 넘은 사본 → 그대로 답하고 뒤에서 새로 받는다. 실패하면 사본 + 기준일 고지.
  · 7일 넘은 사본 + API 실패, 또는 사본 없음 + API 실패 → 종전대로 예외(대체·경고).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest

from app.models import Coordinates
from app.rules_config import OPTION_BY_KEY
from app.screening import amenities
from app.screening import parks as park_rules
from app.screening.amenities import AmenityCollector
from app.services import (
    casino_registry,
    city_gas_registry,
    city_parks,
    cng,
    cng_gyeongnam,
    gg_chemical,
    lpg_station_file,
    public_library,
    rail_stations,
    school_locations,
    seoul_bus,
    traditional_market,
    transfer_center,
)
from app.services.snapshot_store import (
    CoordinateSnapshot,
    SnapshotList,
    SnapshotStore,
    trim_rows,
)
from tests.test_screening_amenities import CENTER, FakeKakao, FakeTago, place

DAY = 24 * 3600
JEONJU = Coordinates(lat=35.83, lng=127.13)
SEOUL = Coordinates(lat=37.55, lng=126.97)


def rewind(path: Path, seconds: float) -> None:
    """사본의 받은 시각을 seconds 만큼 과거로 돌린다."""

    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data.values():
        entry["fetched_at"] = time.time() - seconds
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# SnapshotList — 수명 규칙 한 곳
# ---------------------------------------------------------------------------
class ListError(RuntimeError):
    pass


def make_list(path: Path, fetch, **kwargs) -> SnapshotList[str]:
    return SnapshotList(
        path, "k", "시험 원천", fetch=fetch,
        parse=lambda rows: [str(row["name"]) for row in rows if isinstance(row, dict)],
        error=ListError, **kwargs,
    )


class TestSnapshotList:
    def test_first_use_fetches_once_under_concurrency_and_writes_snapshot(
        self, tmp_path: Path
    ) -> None:
        calls: list[int] = []

        async def fetch(_previous: list[str]) -> list[dict]:
            calls.append(1)
            await asyncio.sleep(0)
            return [{"name": "가"}, {"name": "나"}]

        items = make_list(tmp_path / "c.json", fetch)

        async def hammer() -> list[list[str]]:
            return await asyncio.gather(*(items.get() for _ in range(10)))

        results = asyncio.run(hammer())

        assert all(result == ["가", "나"] for result in results)
        assert len(calls) == 1
        assert SnapshotStore(tmp_path / "c.json").load("k").rows == [  # type: ignore[union-attr]
            {"name": "가"}, {"name": "나"},
        ]

    def test_fresh_snapshot_needs_no_fetch(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        SnapshotStore(path).save("k", [{"name": "가"}], fetched_at=time.time() - 3600)

        async def fetch(_previous: list[str]) -> list[dict]:
            raise AssertionError("신선한 사본이 있으면 받지 않는다")

        items = make_list(path, fetch)

        assert asyncio.run(items.get()) == ["가"]
        assert items.notice == "" and items.as_of is not None and items.has_data

    def test_stale_snapshot_is_served_then_refreshed_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        SnapshotStore(path).save("k", [{"name": "옛"}], fetched_at=time.time() - 2 * DAY)
        seen_previous: list[list[str]] = []

        async def fetch(previous: list[str]) -> list[dict]:
            seen_previous.append(list(previous))
            return [{"name": "새"}]

        items = make_list(path, fetch)

        async def scenario() -> None:
            assert await items.get() == ["옛"]
            assert "서버 사본 사용(기준일 " in items.notice
            await items.guard._task  # type: ignore[misc]
            assert await items.get() == ["새"]
            assert items.notice == ""

        asyncio.run(scenario())
        # 받기 함수는 직전 목록을 넘겨받는다(지오코딩 원천이 좌표를 물려받는 통로).
        assert seen_previous == [["옛"]]

    def test_failing_refresh_keeps_snapshot_until_it_is_a_week_old(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"

        async def fetch(_previous: list[str]) -> list[dict]:
            raise ListError("응답 오류 (429)")

        async def scenario(age: float) -> SnapshotList[str]:
            SnapshotStore(path).save("k", [{"name": "옛"}], fetched_at=time.time() - age)
            items = make_list(path, fetch)
            assert await items.get() == ["옛"]
            await items.guard._task  # type: ignore[misc]
            return items

        async def recent() -> None:
            items = await scenario(2 * DAY)
            assert await items.get() == ["옛"]
            assert "실시간 조회 실패: 응답 오류 (429)" in items.notice

        async def expired() -> None:
            items = await scenario(8 * DAY)
            with pytest.raises(ListError, match="7일"):
                await items.get()

        asyncio.run(recent())
        asyncio.run(expired())

    def test_no_snapshot_and_failing_fetch_raises(self, tmp_path: Path) -> None:
        async def fetch(_previous: list[str]) -> list[dict]:
            raise ListError("응답 오류 (500)")

        with pytest.raises(ListError, match="500"):
            asyncio.run(make_list(tmp_path / "c.json", fetch).get())

    def test_empty_or_shrunken_result_keeps_the_snapshot(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        rows = [{"name": str(n)} for n in range(10)]
        SnapshotStore(path).save("k", rows, fetched_at=time.time() - 2 * DAY)

        async def empty(_previous: list[str]) -> list[dict]:
            return []

        async def shrunk(_previous: list[str]) -> list[dict]:
            return rows[:3]

        with pytest.raises(ListError, match="비어 있습니다"):
            asyncio.run(make_list(path, empty, empty_message="비어 있습니다").refresh(force=True))
        with pytest.raises(ListError, match="응답 이상"):
            asyncio.run(make_list(path, shrunk).refresh(force=True))
        assert len(SnapshotStore(path).load("k").rows) == 10  # type: ignore[union-attr]

    def test_unreadable_snapshot_rows_fall_back_to_fetch(self, tmp_path: Path) -> None:
        path = tmp_path / "c.json"
        SnapshotStore(path).save("k", ["행이 아님"], fetched_at=time.time() - 3600)

        async def fetch(_previous: list[str]) -> list[dict]:
            return [{"name": "새"}]

        assert asyncio.run(make_list(path, fetch).get()) == ["새"]

    def test_trim_rows_keeps_only_listed_columns(self) -> None:
        rows = [{"a": 1, "b": 2, "c": 3}, "행이 아님", {"b": 5}]
        assert trim_rows(rows, ("a", "b")) == [{"a": 1, "b": 2}, {"b": 5}]


# ---------------------------------------------------------------------------
# 원천별 — 같은 규칙이 실제 클라이언트에 걸려 있는지
# ---------------------------------------------------------------------------
def standard_payload(rows: list[dict]) -> dict:
    return {"response": {"header": {"resultCode": "00"},
                         "body": {"items": rows, "totalCount": len(rows)}}}


def odcloud_payload(rows: list[dict]) -> dict:
    return {"page": 1, "perPage": 1000, "totalCount": len(rows), "data": rows}


def seoul_bus_payload(rows: list[dict]) -> dict:
    return {"busStopLocationXyInfo": {"list_total_count": len(rows),
                                      "RESULT": {"CODE": "INFO-000"}, "row": rows}}


def gg_payload(rows: list[dict]) -> dict:
    return {"ChmstryMttrBizplc": [
        {"head": [{"list_total_count": len(rows)}, {"RESULT": {"CODE": "INFO-000"}}]},
        {"row": rows},
    ]}


async def _geocode(_address: str) -> Coordinates | None:
    return JEONJU


class Case(NamedTuple):
    name: str
    make: Any       # (transport, snapshot_path) -> client
    row: dict       # 원천 한 행(쓰지 않는 열 "버릴열" 포함)
    payload: Any    # (rows) -> 응답 본문
    around: Any     # (client) -> 사업지 주변 조회 코루틴
    center: Coordinates = JEONJU


CASES: tuple[Case, ...] = (
    Case(
        "city_parks",
        lambda t, p: city_parks.CityParkClient("k", transport=t, snapshot_path=p),
        {"manageNo": "P-1", "parkNm": "금암공원", "parkSe": "어린이공원", "lnmadr": "전주시 1",
         "latitude": "35.83", "longitude": "127.13", "parkAr": "2033.7", "버릴열": "x"},
        standard_payload,
        lambda c: c.parks_around(JEONJU, 500),
    ),
    Case(
        "school_locations",
        lambda t, p: school_locations.SchoolLocationClient("k", transport=t, snapshot_path=p),
        {"schoolId": "S-1", "schoolNm": "전라중학교", "schoolSe": "중학교", "operSttus": "운영",
         "lnmadr": "전주시 1", "latitude": "35.83", "longitude": "127.13", "버릴열": "x"},
        standard_payload,
        lambda c: c.schools_around(JEONJU, 500),
    ),
    Case(
        "traditional_market",
        lambda t, p: traditional_market.TraditionalMarketClient("k", transport=t, snapshot_path=p),
        {"mrktNm": "남부시장", "mrktType": "상설장", "lnmadr": "전주시 1",
         "latitude": "35.83", "longitude": "127.13", "버릴열": "x"},
        standard_payload,
        lambda c: c.markets_around(JEONJU, 500),
    ),
    Case(
        "public_library",
        lambda t, p: public_library.PublicLibraryClient("k", transport=t, snapshot_path=p),
        {"lbrryNm": "인후도서관", "lbrrySe": "공공도서관", "rdnmadr": "전주시 1",
         "latitude": "35.83", "longitude": "127.13", "버릴열": "x"},
        standard_payload,
        lambda c: c.libraries_around(JEONJU, 500),
    ),
    Case(
        "rail_stations",
        lambda t, p: rail_stations.KorailStationClient("k", transport=t, snapshot_path=p),
        {"지역본부": "전북본부", "역명": "전주", "위도": 35.83, "경도": 127.13,
         "출입구 개수": 1, "버릴열": "x"},
        odcloud_payload,
        lambda c: c.stations_around(JEONJU, 500),
    ),
    Case(
        "transfer_center",
        lambda t, p: transfer_center.TransferCenterClient("k", transport=t, snapshot_path=p),
        {"trnsitlcCnterNm": "전주역환승센터", "rdnmadr": "전주시 1", "latitude": "35.83",
         "longitude": "127.13", "operYn": "Y", "버릴열": "x"},
        standard_payload,
        lambda c: c.centers_around(JEONJU, 500),
    ),
    Case(
        "cng",
        lambda t, p: cng.CngStationClient("k", transport=t, snapshot_path=p),
        {"시설명": "전주CNG충전소", "주소": "전북특별자치도 전주시 1", "위도": "35.83",
         "경도": "127.13", "행정구역": "전북"},
        odcloud_payload,
        lambda c: c.stations_around(JEONJU, 500),
    ),
    Case(
        "lpg_station_file",
        lambda t, p: lpg_station_file.LpgStationFileClient("k", transport=t, snapshot_path=p),
        {"업소명": "한빛충전소", "주소": "전북특별자치도 전주시 1", "위도": "35.83",
         "경도": "127.13", "관리구분": "자동차", "버릴열": "x"},
        odcloud_payload,
        lambda c: c.stations_around(JEONJU, 500),
    ),
    Case(
        "cng_gyeongnam",
        lambda t, p: cng_gyeongnam.CngGyeongnamClient(
            "k", geocode=_geocode, transport=t, snapshot_path=p
        ),
        {"충전소명": "창원충전소", "위치": "경상남도 창원시 1", "시군명": "창원시",
         "도시가스공급사": "경남에너지"},
        odcloud_payload,
        lambda c: c.stations_around(JEONJU, 500),
    ),
    Case(
        "gg_chemical",
        lambda t, p: gg_chemical.GgChemicalClient("k", transport=t, snapshot_path=p),
        {"ENTRPS_NM": "영신금속", "INDUTYPE_NM": "사용업", "REFINE_ROADNM_ADDR": "경기도 안산시 1",
         "REFINE_WGS84_LAT": 35.83, "REFINE_WGS84_LOGT": 127.13, "BIZREGNO": "1348142184"},
        gg_payload,
        lambda c: c.facilities_around(JEONJU, 500),
    ),
    Case(
        "seoul_bus",
        lambda t, p: seoul_bus.SeoulBusStopClient("k", transport=t, snapshot_path=p),
        {"STOPS_NO": "02001", "STOPS_NM": "서울역", "XCRD": "126.97", "YCRD": "37.55",
         "STOPS_TYPE": "중앙차로", "NODE_ID": "x"},
        seoul_bus_payload,
        lambda c: c.stops_around(SEOUL, 500),
        SEOUL,
    ),
)


def serving(case: Case):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=case.payload([case.row]))

    return calls, httpx.MockTransport(handler)


def failing():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="Service Unavailable")

    return calls, httpx.MockTransport(handler)


def build_snapshot(case: Case, path: Path) -> None:
    """실제 수신 경로로 사본을 만든다(쓰기 경로까지 함께 시험한다)."""

    _calls, transport = serving(case)
    found = asyncio.run(case.around(case.make(transport, path)))
    assert len(found) == 1, case.name
    assert path.is_file(), case.name


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
class TestWholeListSources:
    def test_fresh_snapshot_answers_without_remote_call(self, case: Case, tmp_path: Path) -> None:
        path = tmp_path / "snapshot.json"
        build_snapshot(case, path)
        calls, transport = failing()
        client = case.make(transport, path)

        # 새로 켜진 서버 — API 가 죽어 있어도(또는 붐벼도) 사본으로 곧바로 답한다.
        found = asyncio.run(case.around(client))

        assert len(found) == 1
        assert calls == []
        assert client.snapshot_notice == "" and client.data_as_of is not None

    def test_snapshot_keeps_only_used_columns(self, case: Case, tmp_path: Path) -> None:
        path = tmp_path / "snapshot.json"
        build_snapshot(case, path)

        stored = next(iter(json.loads(path.read_text(encoding="utf-8")).values()))["rows"]

        assert len(stored) == 1
        assert "버릴열" not in stored[0]

    def test_stale_snapshot_with_failing_api_is_served_with_a_dated_notice(
        self, case: Case, tmp_path: Path
    ) -> None:
        path = tmp_path / "snapshot.json"
        build_snapshot(case, path)
        rewind(path, 2 * DAY)
        calls, transport = failing()
        client = case.make(transport, path)

        async def scenario() -> None:
            assert len(await case.around(client)) == 1
            await client._list.guard._task
            # 새로 받기가 실패해도 예외(→ 대체 원천 + 경고)로 넘어가지 않는다.
            assert len(await case.around(client)) == 1
            assert "서버 사본 사용(기준일 " in client.snapshot_notice
            assert "실시간 조회 실패" in client.snapshot_notice

        asyncio.run(scenario())
        assert calls  # 뒤에서 실제로 새로 받기를 시도했다

    def test_week_old_snapshot_with_failing_api_is_an_outage(
        self, case: Case, tmp_path: Path
    ) -> None:
        path = tmp_path / "snapshot.json"
        build_snapshot(case, path)
        rewind(path, 8 * DAY)
        _calls, transport = failing()
        client = case.make(transport, path)

        async def scenario() -> None:
            assert len(await case.around(client)) == 1
            await client._list.guard._task
            with pytest.raises(Exception, match="7일"):
                await case.around(client)

        asyncio.run(scenario())

    def test_no_snapshot_and_failing_api_is_still_an_outage(
        self, case: Case, tmp_path: Path
    ) -> None:
        _calls, transport = failing()
        client = case.make(transport, tmp_path / "snapshot.json")

        with pytest.raises(Exception):  # noqa: B017, PT011 — 원천마다 예외 종류가 다르다
            asyncio.run(case.around(client))
        assert not (tmp_path / "snapshot.json").exists()

    def test_default_constructor_has_no_snapshot_path(self, case: Case) -> None:
        # 사본 경로는 앱 배선만 준다 — 테스트·도구가 실제 data 폴더에 쓰지 않는다.
        client = case.make(httpx.MockTransport(lambda request: httpx.Response(503)), None)
        assert client._list.guard.store.path is None


def test_gyeongnam_refresh_reuses_coordinates_and_geocodes_new_addresses_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "snapshot.json"
    old = {"충전소명": "창원충전소", "위치": "경상남도 창원시 1", "시군명": "창원시"}
    new = {"충전소명": "진주충전소", "위치": "경상남도 진주시 2", "시군명": "진주시"}
    asked: list[str] = []

    async def geocode(address: str) -> Coordinates | None:
        asked.append(address)
        return Coordinates(lat=35.2, lng=128.6)

    def client_for(rows: list[dict]) -> cng_gyeongnam.CngGyeongnamClient:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=odcloud_payload(rows))
        )
        return cng_gyeongnam.CngGyeongnamClient(
            "k", geocode=geocode, transport=transport, snapshot_path=path
        )

    asyncio.run(client_for([old]).all_stations())
    assert asked == ["경상남도 창원시 1"]

    stations = asyncio.run(client_for([old, new]).refresh(force=True))

    assert [s.name for s in stations] == ["창원충전소", "진주충전소"]
    assert asked == ["경상남도 창원시 1", "경상남도 진주시 2"]


# ---------------------------------------------------------------------------
# 코드에 둔 명단(카지노·도시가스 제조시설) — 좌표만 사본으로
# ---------------------------------------------------------------------------
class TestRegistryCoordinates:
    def test_coordinate_snapshot_round_trip_and_writes_only_when_changed(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "coords.json"
        first = CoordinateSnapshot(path, "k")
        assert first.get("a", "주소 1") is None
        first.save()
        assert not path.exists()  # 새 좌표가 없으면 파일을 만들지 않는다

        first.put("a", "주소 1", Coordinates(lat=35.0, lng=127.0))
        first.save()

        second = CoordinateSnapshot(path, "k")
        assert second.get("a", "주소 1") == Coordinates(lat=35.0, lng=127.0)
        # 주소가 바뀌면 사본 좌표를 쓰지 않는다(다시 지오코딩한다).
        assert second.get("a", "주소 2") is None

        # 명단에서 주소가 바뀐 항목의 옛 좌표는 버린다.
        second.put("a", "주소 2", Coordinates(lat=36.0, lng=128.0))
        second.keep({("a", "주소 2")})
        second.save()
        rows = json.loads(path.read_text(encoding="utf-8"))["k"]["rows"]
        assert [(row["id"], row["addr"]) for row in rows] == [("a", "주소 2")]

    @pytest.mark.parametrize(
        ("module", "client_class", "method", "count"),
        [
            (casino_registry, casino_registry.CasinoRegistryClient, "all_casinos",
             len(casino_registry.CASINOS)),
            (city_gas_registry, city_gas_registry.CityGasRegistryClient, "all_plants",
             len(city_gas_registry.CITY_GAS_PLANTS)),
        ],
    )
    def test_registry_geocodes_once_then_reuses_the_snapshot(
        self, module, client_class, method, count, tmp_path: Path
    ) -> None:
        path = tmp_path / "registry.json"
        asked: list[str] = []

        async def geocode(address: str) -> Coordinates | None:
            asked.append(address)
            return Coordinates(lat=35.0 + len(asked) * 0.01, lng=127.0)

        first = asyncio.run(getattr(client_class(geocode=geocode, snapshot_path=path), method)())
        assert len(first) == count and len(asked) == count

        # 새로 켜진 서버 — 명단은 코드에서, 좌표는 사본에서. 지오코딩을 다시 하지 않는다.
        second = asyncio.run(getattr(client_class(geocode=geocode, snapshot_path=path), method)())
        assert len(asked) == count
        assert [item.coordinates for item in second] == [item.coordinates for item in first]
        assert json.loads(path.read_text(encoding="utf-8"))[module.SNAPSHOT_KEY]["rows"]

    def test_registry_without_snapshot_path_writes_nothing(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)

        async def geocode(_address: str) -> Coordinates | None:
            return Coordinates(lat=35.0, lng=127.0)

        casinos = asyncio.run(casino_registry.CasinoRegistryClient(geocode=geocode).all_casinos())

        assert len(casinos) == len(casino_registry.CASINOS)
        assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# 2차 수집기 — 켜진 직후 공원 원천 경고가 나던 자리
# ---------------------------------------------------------------------------
KAKAO_PARKS = {"공원": [place("금암어린이공원", 150, "여행 > 공원 > 도시근린공원 > 어린이공원")]}


@pytest.fixture
def default_options(monkeypatch):
    monkeypatch.setattr(amenities, "option_enabled", lambda key: OPTION_BY_KEY[key].default)


def _park_row(name: str, point: Coordinates) -> dict:
    return {"manageNo": name, "parkNm": name, "parkSe": "어린이공원", "lnmadr": f"{name} 주소",
            "latitude": str(point.lat), "longitude": str(point.lng), "parkAr": "2000"}


def _park_snapshot(path: Path, age_seconds: float) -> None:
    from app.services.geo import offset_coordinates

    SnapshotStore(path).save(
        city_parks.SNAPSHOT_KEY,
        [_park_row("금암공원", offset_coordinates(CENTER, 140, 0))],
        fetched_at=time.time() - age_seconds,
    )


class TestParkFeedAfterColdStart:
    @pytest.mark.asyncio
    async def test_snapshot_answers_while_api_is_unreachable_without_alert(
        self, tmp_path: Path, default_options
    ) -> None:
        path = tmp_path / "parks.json"
        _park_snapshot(path, 3600)
        calls, transport = failing()
        kakao = FakeKakao(keywords=KAKAO_PARKS)
        collector = AmenityCollector(
            kakao=kakao, tago=FakeTago([]),
            park_client=city_parks.CityParkClient("k", transport=transport, snapshot_path=path),
        )

        group = (await collector.collect([], CENTER))["park"]

        # 표준데이터가 답했다 — 카카오 대체도, 원천 경고도, 사본 고지도 없다.
        assert [f.name for f in group.facilities] == ["금암공원"]
        assert group.state == "connected"
        assert group.note == park_rules.PARK_STANDARD_NOTE
        assert group.source_alert == ""
        assert calls == [] and "공원" not in kakao.calls

    @pytest.mark.asyncio
    async def test_stale_snapshot_is_used_and_dated_in_the_group_note(
        self, tmp_path: Path, default_options
    ) -> None:
        path = tmp_path / "parks.json"
        _park_snapshot(path, 2 * DAY)
        _calls, transport = failing()
        collector = AmenityCollector(
            kakao=FakeKakao(keywords=KAKAO_PARKS), tago=FakeTago([]),
            park_client=city_parks.CityParkClient("k", transport=transport, snapshot_path=path),
        )

        group = (await collector.collect([], CENTER))["park"]

        assert [f.name for f in group.facilities] == ["금암공원"]
        assert group.state == "connected" and group.source_alert == ""
        # 실시간 답처럼 보이지 않게 기준일이 비고에 드러난다.
        assert group.note.startswith(park_rules.PARK_STANDARD_NOTE)
        assert "전국도시공원정보표준데이터 — 서버 사본 사용(기준일 " in group.note

    @pytest.mark.asyncio
    async def test_without_snapshot_a_failing_api_still_falls_back_with_alert(
        self, tmp_path: Path, default_options
    ) -> None:
        _calls, transport = failing()
        collector = AmenityCollector(
            kakao=FakeKakao(keywords=KAKAO_PARKS), tago=FakeTago([]),
            park_client=city_parks.CityParkClient(
                "k", transport=transport, snapshot_path=tmp_path / "parks.json"
            ),
        )

        group = (await collector.collect([], CENTER))["park"]

        assert [f.name for f in group.facilities] == ["금암어린이공원"]
        assert group.state == "substituted"
        assert group.source_alert == park_rules.PARK_STANDARD_ALERT
