"""역·터미널·도서관 원천 검증 — 지도 회사(카카오·네이버)와 무관하게 같은 판정이 나오는가.

순서: 공공 API(전국도서관표준데이터 15013109·한국철도공사 역위치 정보 15127532) →
VWorld 장소검색 → 카카오(마지막 대체). 대체할 때는 경고를 올린다(조용히 넘어가지 않는다).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.models import Coordinates
from app.screening.amenities import (
    LIBRARY_NONE_SOURCE,
    VWORLD_LIBRARY_SOURCE,
    VWORLD_RAILWAY_SOURCE,
    VWORLD_SUBWAY_SOURCE,
    VWORLD_TERMINAL_SOURCE,
    AmenityCollector,
    _strip_room_suffix,
)
from app.services.geo import haversine_meters, offset_coordinates
from app.services.public_library import (
    LIBRARY_STANDARD_SOURCE,
    LibraryRecord,
    PublicLibraryAPIError,
    PublicLibraryClient,
    library_record,
)
from app.services.rail_stations import (
    KORAIL_STATION_SOURCE,
    KorailStationAPIError,
    KorailStationClient,
    StationExit,
    StationRecord,
    dataset_exits_for,
    load_station_exits,
    station_name,
    station_record,
)
from app.services.vworld import VWorldAPIError, VWorldClient, VWorldPlace


CENTER = Coordinates(lat=35.84, lng=127.13)


def at(offset_m: float, east_m: float = 0.0) -> Coordinates:
    return offset_coordinates(CENTER, offset_m, east_m)


def vw(title: str, offset_m: float, category: str, east_m: float = 0.0) -> VWorldPlace:
    return VWorldPlace(
        id=f"{title}@{offset_m}:{category}",
        title=title,
        category=category,
        road_address=f"{title} 도로명",
        parcel_address="",
        coordinates=at(offset_m, east_m),
    )


def kakao_doc(name: str, offset_m: float, category: str = "") -> dict[str, Any]:
    point = at(offset_m)
    return {
        "place_name": name,
        "address_name": f"{name} 주소",
        "road_address_name": "",
        "category_name": category,
        "x": str(point.lng),
        "y": str(point.lat),
    }


class FakeKakao:
    enabled = True

    def __init__(
        self,
        categories: dict[str, list[dict[str, Any]]] | None = None,
        keywords: dict[str, list[dict[str, Any]]] | None = None,
        down: bool = False,
    ) -> None:
        self.categories = categories or {}
        self.keywords = keywords or {}
        self.down = down
        self.calls: list[str] = []

    async def search_category(self, code, lat, lng, radius_m, max_pages=3):
        self.calls.append(code)
        if self.down:
            raise RuntimeError("API limit has been exceeded.")
        return list(self.categories.get(code, []))

    async def search_keyword(self, keyword, lat, lng, radius_m, max_pages=3):
        self.calls.append(keyword)
        if self.down:
            raise RuntimeError("API limit has been exceeded.")
        return list(self.keywords.get(keyword, []))


class FakeTago:
    enabled = False


class FakePlaceVWorld:
    """VWorld 장소검색 대역. 필지 조회는 없음(좌표로 잰다)."""

    enabled = True

    def __init__(self, places: dict[str, list[VWorldPlace]], fail: set[str] | None = None) -> None:
        self.places = places
        self.fail = fail or set()
        self.queries: list[str] = []

    async def places_around(self, query, center, radius_m, max_pages=5):
        self.queries.append(query)
        if query in self.fail:
            raise VWorldAPIError("VWorld 장소검색 오류: 장애")
        return [
            p for p in self.places.get(query, [])
            if haversine_meters(center, p.coordinates) <= radius_m
        ]

    async def parcel_at(self, lat, lng):
        return None

    async def parcel_by_pnu(self, pnu):
        return None


class FakeLibraries:
    def __init__(self, records=None, error: Exception | None = None) -> None:
        self.records = records or []
        self.error = error
        self.enabled = True

    async def libraries_around(self, center, radius_m):
        if self.error is not None:
            raise self.error
        return [r for r in self.records if haversine_meters(center, r.coordinates) <= radius_m]


class FakeKorail:
    def __init__(self, records=None, error: Exception | None = None) -> None:
        self.records = records or []
        self.error = error
        self.enabled = True

    async def stations_around(self, center, radius_m):
        if self.error is not None:
            raise self.error
        return [r for r in self.records if haversine_meters(center, r.coordinates) <= radius_m]


class FakeOffices:
    """생활안전지도 관공서 레이어 대역 — 공공시설의 관공서 몫(도서관 원천과 별개)."""

    enabled = True

    async def facilities_around(self, center, radius_m):
        from app.services.safemap_facilities import SafemapFacility

        return [SafemapFacility("IF_0031", "송천동주민센터", "송천동주민센터", "주소", "", at(600))]


def collector(kakao=None, vworld=None, **kwargs) -> AmenityCollector:
    return AmenityCollector(
        kakao=kakao or FakeKakao(),
        tago=FakeTago(),
        vworld=vworld,
        alignments=[],
        safemap_offices=kwargs.pop("safemap_offices", FakeOffices()),
        station_exits=kwargs.pop("station_exits", ()),
        **kwargs,
    )


def names(group) -> list[str]:
    return [f.name for f in group.facilities]


def library_names(group) -> list[str]:
    return [f.name for f in group.facilities if "도서관" in f.name]


# ---------------------------------------------------------------------------
# VWorld 장소검색
# ---------------------------------------------------------------------------
def _vworld_item(title: str, category: str, lat: float, lng: float, pid: str) -> dict[str, Any]:
    return {
        "id": pid,
        "title": title,
        "category": category,
        "address": {"road": f"{title} 도로명", "parcel": ""},
        "point": {"x": str(lng), "y": str(lat)},
    }


@pytest.mark.asyncio
async def test_vworld_place_search_pages_and_filters_the_radius() -> None:
    near = at(500)
    far = at(2900, 2900)  # 사각형 안, 원 밖
    pages = {
        "1": [_vworld_item("전주시립인후도서관", "공공도서관", near.lat, near.lng, "A")] * 1
        + [_vworld_item(f"도서관{i}", "공공도서관", near.lat, near.lng, f"P{i}") for i in range(999)],
        "2": [_vworld_item("먼도서관", "공공도서관", far.lat, far.lng, "B")],
    }
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        items = pages[params["page"]]
        body = {
            "response": {
                "status": "OK",
                "record": {"total": "1001", "current": str(len(items))},
                "result": {"items": items},
            }
        }
        return httpx.Response(200, json=body)

    client = VWorldClient("key", transport=httpx.MockTransport(handler))
    places = await client.places_around("공공도서관", CENTER, 3000)

    assert len(seen) == 2
    assert seen[0]["type"] == "place" and seen[0]["query"] == "공공도서관"
    assert seen[0]["crs"] == "EPSG:4326" and "," in seen[0]["bbox"]
    assert "먼도서관" not in [p.title for p in places]
    assert places[0].title == "전주시립인후도서관"
    assert places[0].category_leaf == "공공도서관"


@pytest.mark.asyncio
async def test_vworld_place_search_empty_and_error_are_distinguished() -> None:
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"response": {"status": "NOT_FOUND"}})

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"response": {"status": "ERROR", "error": {"text": "인증키 오류"}}}
        )

    assert await VWorldClient("key", transport=httpx.MockTransport(empty)).places_around(
        "철도역", CENTER, 3000
    ) == []
    with pytest.raises(VWorldAPIError):
        await VWorldClient("key", transport=httpx.MockTransport(broken)).places_around(
            "철도역", CENTER, 3000
        )
    with pytest.raises(VWorldAPIError):
        await VWorldClient("").places_around("철도역", CENTER, 3000)


# ---------------------------------------------------------------------------
# 공공 API 클라이언트
# ---------------------------------------------------------------------------
def test_library_record_keeps_public_and_children_libraries_only() -> None:
    row = {"lbrryNm": "인후도서관", "lbrrySe": "공공도서관", "rdnmadr": "안덕원로 349",
           "latitude": "35.8395", "longitude": "127.1634"}
    assert library_record(row).name == "인후도서관"
    assert library_record({**row, "lbrrySe": "어린이도서관"}) is not None
    assert library_record({**row, "lbrrySe": "작은도서관"}) is None
    assert library_record({**row, "latitude": ""}) is None


@pytest.mark.asyncio
async def test_library_client_reads_standard_payload_and_flags_unapproved_key() -> None:
    near = at(300)
    body = {
        "response": {
            "header": {"resultCode": "00"},
            "body": {
                "items": [
                    {"lbrryNm": "인후도서관", "lbrrySe": "공공도서관",
                     "latitude": str(near.lat), "longitude": str(near.lng)},
                    {"lbrryNm": "동네작은도서관", "lbrrySe": "작은도서관",
                     "latitude": str(near.lat), "longitude": str(near.lng)},
                ],
                "totalCount": 2,
            },
        }
    }
    ok = PublicLibraryClient("key", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert [r.name for r in await ok.libraries_around(CENTER, 3000)] == ["인후도서관"]

    denied = PublicLibraryClient(
        "key", transport=httpx.MockTransport(lambda r: httpx.Response(403, text="SERVICE_KEY_IS_NOT_REGISTERED_ERROR"))
    )
    with pytest.raises(PublicLibraryAPIError) as info:
        await denied.libraries_around(CENTER, 3000)
    assert info.value.unapproved


def test_station_names_are_normalized_across_sources() -> None:
    assert station_name("익산") == "익산역"
    assert station_name("익산역") == "익산역"
    assert station_name("군자(능동)역") == "군자역"
    assert station_name("군자역(능동)") == "군자역"
    record = station_record({"역명": "전주", "위도": "35.849772", "경도": "127.161845",
                             "지역본부": "전북", "출입구 개수": 1})
    assert record == StationRecord("전주역", "전북", Coordinates(lat=35.849772, lng=127.161845), 1)


@pytest.mark.asyncio
async def test_korail_client_reads_odcloud_payload_and_flags_unapproved_key() -> None:
    near = at(800)
    body = {"totalCount": 1, "data": [{"역명": "전주", "위도": str(near.lat), "경도": str(near.lng),
                                         "지역본부": "전북", "출입구 개수": 1}]}
    ok = KorailStationClient("key", transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert [s.name for s in await ok.stations_around(CENTER, 3000)] == ["전주역"]

    denied = KorailStationClient(
        "key", transport=httpx.MockTransport(lambda r: httpx.Response(401, json={"code": -401}))
    )
    with pytest.raises(KorailStationAPIError) as info:
        await denied.stations_around(CENTER, 3000)
    assert info.value.unapproved


def test_lh_station_exit_file_has_the_lh_exit_points() -> None:
    exits = load_station_exits()
    assert len(exits) == 14
    iksan = dataset_exits_for("익산", Coordinates(lat=35.9394, lng=126.946773), exits)
    assert sorted(label for label, _ in iksan) == ["익산역출구1", "익산역출구2"]
    # 같은 이름이어도 먼 역(다른 지역의 동명 역)은 붙이지 않는다.
    assert dataset_exits_for("익산역", CENTER, exits) == []


# ---------------------------------------------------------------------------
# 수집기 — 공공도서관
# ---------------------------------------------------------------------------
def lib(name: str, offset_m: float) -> LibraryRecord:
    return LibraryRecord(name, "공공도서관", f"{name} 도로명", at(offset_m))


def test_room_suffix_rows_fold_into_one_library() -> None:
    assert _strip_room_suffix("군산시립도서관(자료열람실)") == "군산시립도서관"
    assert _strip_room_suffix("설림도서관(학습실)") == "설림도서관"
    assert _strip_room_suffix("익산시립도서관(영등도서관)") == "익산시립도서관(영등도서관)"


@pytest.mark.asyncio
async def test_standard_libraries_are_supplemented_and_named_like_lh() -> None:
    libraries = FakeLibraries([
        lib("인후도서관", 400),
        lib("군산시립도서관(자료열람실)", 900),
        lib("군산시립도서관(학습실)", 905),
        # 표준데이터 좌표가 틀린 줄(실측: 금강도서관이 군산시립도서관 자리) — 이름으로 짝짓는다.
        lib("금강도서관", 902),
        lib("VWorld에없는도서관", 2000),
    ])
    vworld = FakePlaceVWorld({
        "공공도서관": [
            vw("전주시립인후도서관", 410, "공공도서관"),
            vw("김제시립도서관", 1500, "공공도서관"),  # 표준데이터에 없는 곳
            vw("군산시립도서관", 900, "공공도서관"),
            vw("군산시립금강도서관", 2500, "공공도서관"),
        ]
    })
    result = await collector(library_client=libraries, vworld=vworld).collect([], CENTER)

    public = result["public"]
    assert library_names(public) == [
        "전주시립인후도서관", "군산시립도서관", "김제시립도서관", "VWorld에없는도서관", "군산시립금강도서관",
    ]
    # 짝지은 도서관은 VWorld 좌표(410m)로, 표준데이터에만 있는 곳은 표준데이터 좌표로 잰다.
    assert public.facilities[0].distance_m == pytest.approx(410, abs=2)
    assert f"{LIBRARY_STANDARD_SOURCE} + {VWORLD_LIBRARY_SOURCE}(위치·보충)" in public.actual_source
    assert public.source_alert == ""


@pytest.mark.asyncio
async def test_unapproved_standard_falls_back_to_vworld_with_an_alert() -> None:
    libraries = FakeLibraries(error=PublicLibraryAPIError("HTTP 403 (활용신청 승인 전)", 403))
    kakao = FakeKakao(keywords={"도서관": [kakao_doc("영어도서관학원", 300, "교육 > 학원")]})
    vworld = FakePlaceVWorld({"공공도서관": [vw("전주시립인후도서관", 400, "공공도서관")]})
    result = await collector(kakao, vworld, library_client=libraries).collect([], CENTER)

    public = result["public"]
    assert library_names(public) == ["전주시립인후도서관"]
    assert VWORLD_LIBRARY_SOURCE in public.actual_source
    assert "활용신청 승인 전" in public.source_alert
    assert "다시 심사" not in public.source_alert  # 승인 전은 장애가 아니다
    assert "도서관" not in kakao.calls


@pytest.mark.asyncio
async def test_library_outage_does_not_drop_the_whole_public_group() -> None:
    libraries = FakeLibraries(error=PublicLibraryAPIError("HTTP 500", 500))
    vworld = FakePlaceVWorld({}, fail={"공공도서관"})
    result = await collector(FakeKakao(down=True), vworld, library_client=libraries).collect(
        [], CENTER
    )

    public = result["public"]
    assert public.state != "missing"
    assert LIBRARY_NONE_SOURCE in public.actual_source
    assert "도서관 없이" in public.source_alert
    assert "다시 심사" in public.source_alert


# ---------------------------------------------------------------------------
# 수집기 — 철도역·터미널·지하철
# ---------------------------------------------------------------------------
RAIL = "철도시설 > 철도/지하철 > 고속철도역"
TERMINAL = "도로시설 > 버스터미널/정류장 > 시외버스터미널"


@pytest.mark.asyncio
async def test_korail_is_the_railway_source_and_lh_exits_measure_the_station() -> None:
    station = at(1200)
    exits = (
        StationExit("전주역출구1", "전주역", at(1100)),
        StationExit("전주역출구2", "전주역", at(1150)),
    )
    korail = FakeKorail([StationRecord("전주역", "전북", station, 1)])
    kakao = FakeKakao(keywords={"기차역": [kakao_doc("딴역", 500)]})
    result = await collector(kakao, korail_client=korail, station_exits=exits).collect([], CENTER)

    railway = result["railway"]
    assert railway.state == "connected"
    assert railway.actual_source == KORAIL_STATION_SOURCE
    assert names(railway) == ["전주역"]
    # LH 출구점 중 가장 가까운 곳(출구1, 1100m)에서 잰다. 카카오 출구 검색은 하지 않는다.
    assert railway.facilities[0].distance_m == pytest.approx(1100, abs=2)
    assert not any("번출구" in call for call in kakao.calls)
    assert "기차역" not in kakao.calls


@pytest.mark.asyncio
async def test_railway_falls_back_to_vworld_railway_classes_on_outage() -> None:
    korail = FakeKorail(error=KorailStationAPIError("HTTP 500", 500))
    vworld = FakePlaceVWorld({
        "철도역": [
            vw("익산", 700, RAIL),
            vw("동익산역", 900, "철도시설 > 철도/지하철 > 철도정거장"),
        ]
    })
    result = await collector(FakeKakao(down=True), vworld, korail_client=korail).collect([], CENTER)

    railway = result["railway"]
    assert names(railway) == ["익산역"]
    assert railway.actual_source == VWORLD_RAILWAY_SOURCE
    assert "응답하지 않아" in railway.source_alert and "다시 심사" in railway.source_alert


@pytest.mark.asyncio
async def test_terminals_come_from_vworld_classes_without_kakao() -> None:
    vworld = FakePlaceVWorld({
        "시외버스터미널": [
            vw("전주시외버스공용터미널", 260, TERMINAL),
            vw("G car zone 전주터미널", 250, "기타정보서비스업 > 카셰어링존정보"),
        ],
        "고속버스터미널": [vw("전주고속버스터미널", 90, "도로시설 > 버스터미널/정류장 > 고속버스터미널")],
        "종합버스터미널": [vw("전주고속버스터미널", 90, "도로시설 > 버스터미널/정류장 > 종합버스터미널")],
    })
    kakao = FakeKakao()
    result = await collector(kakao, vworld).collect([], CENTER)

    terminal = result["terminal"]
    assert names(terminal) == ["전주고속버스터미널", "전주시외버스공용터미널"]
    assert terminal.actual_source == VWORLD_TERMINAL_SOURCE
    assert "버스터미널" not in kakao.calls and "고속버스터미널" not in kakao.calls


@pytest.mark.asyncio
async def test_subway_uses_vworld_stations_and_exits_when_kakao_is_down() -> None:
    vworld = FakePlaceVWorld({
        "지하철역": [
            vw("군자(능동)역", 800, "철도시설 > 철도/지하철 > 지하철역"),
            vw("군자역(능동)", 805, "철도시설 > 철도/지하철 > 지하철역"),
            vw("군자역3번출입구", 700, "도로시설 > 지하철역입구"),
            vw("군자역8번출입구", 820, "도로시설 > 지하철역입구"),
        ]
    })
    result = await collector(FakeKakao(down=True), vworld).collect([], CENTER)

    subway = result["subway"]
    assert names(subway) == ["군자역"]
    assert subway.actual_source == VWORLD_SUBWAY_SOURCE
    assert subway.state == "substituted"
    assert subway.facilities[0].distance_m == pytest.approx(700, abs=2)
    assert {d.label for d in subway.facilities[0].front_door_candidates} == {
        "군자역3번출입구", "군자역8번출입구",
    }
    assert "카카오" in subway.source_alert


@pytest.mark.asyncio
async def test_results_do_not_depend_on_kakao_availability() -> None:
    """카카오가 살아 있든(엉뚱한 결과를 주든) 죽어 있든 역·터미널·도서관 판정이 같다."""

    def build(kakao):
        vworld = FakePlaceVWorld({
            "철도역": [vw("전주", 1200, RAIL)],
            "시외버스터미널": [vw("전주시외버스공용터미널", 260, TERMINAL)],
            "공공도서관": [vw("전주시립인후도서관", 400, "공공도서관")],
        })
        return collector(
            kakao,
            vworld,
            library_client=FakeLibraries([lib("인후도서관", 400)]),
            korail_client=FakeKorail(error=KorailStationAPIError("401", 401)),
        )

    alive = FakeKakao(keywords={
        "기차역": [kakao_doc("카카오역", 300)],
        "버스터미널": [kakao_doc("카카오터미널", 100, TERMINAL)],
        "도서관": [kakao_doc("카카오도서관", 50, "문화,예술 > 도서관")],
    })
    up = await build(alive).collect([], CENTER)
    down = await build(FakeKakao(down=True)).collect([], CENTER)

    for key in ("railway", "terminal"):
        assert names(up[key]) == names(down[key])
        assert up[key].distances_m == down[key].distances_m
    assert library_names(up["public"]) == library_names(down["public"]) == ["전주시립인후도서관"]
