"""생활편의시설 수집기 검증.

핵심은 두 가지다. (1) 지정 원천을 지도 검색으로 근사한 사실이 note 로 남는가,
(2) 원천이 없을 때 '0개'가 아니라 'missing' 으로 내려가는가.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.models import Coordinates
from app.screening.amenities import (
    KAKAO_DISABLED_NOTE,
    TRANSFER_MISSING_NOTE,
    AmenityCollector,
)
from app.services.geo import haversine_meters, offset_coordinates


CENTER = Coordinates(lat=37.40111, lng=127.10853)


def place(name: str, offset_m: float, category: str = "") -> dict[str, Any]:
    point = offset_coordinates(CENTER, offset_m, 0)
    return {
        "place_name": name,
        "address_name": f"{name} 주소",
        "road_address_name": "",
        "category_name": category,
        "x": str(point.lng),
        "y": str(point.lat),
    }


class FakeKakao:
    """카테고리·키워드 응답을 미리 정해 놓는 대역."""

    enabled = True

    def __init__(
        self,
        categories: dict[str, list[dict[str, Any]]] | None = None,
        keywords: dict[str, list[dict[str, Any]]] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self.categories = categories or {}
        self.keywords = keywords or {}
        self.fail = fail or set()
        self.calls: list[str] = []

    async def search_category(self, code, lat, lng, radius_m, max_pages=3):
        self.calls.append(code)
        if code in self.fail:
            raise RuntimeError("카카오 응답 오류")
        return list(self.categories.get(code, []))

    async def search_keyword(self, keyword, lat, lng, radius_m, max_pages=3):
        self.calls.append(keyword)
        if keyword in self.fail:
            raise RuntimeError("카카오 응답 오류")
        return list(self.keywords.get(keyword, []))


class DisabledKakao:
    enabled = False

    async def search_category(self, *args, **kwargs):  # pragma: no cover - 호출되면 버그
        raise AssertionError("키가 없으면 호출하지 않아야 한다")

    async def search_keyword(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("키가 없으면 호출하지 않아야 한다")


class FakeTago:
    def __init__(self, rows: list[dict[str, Any]] | None = None, enabled: bool = True) -> None:
        self.rows = rows or []
        self.enabled = enabled
        self.calls = 0

    async def nearby_stops(self, lat, lng):
        self.calls += 1
        if self.rows is None:
            raise RuntimeError("TAGO 장애")
        return list(self.rows)


class BrokenTago(FakeTago):
    async def nearby_stops(self, lat, lng):
        self.calls += 1
        raise RuntimeError("TAGO 장애")


@pytest.mark.asyncio
async def test_missing_kakao_key_marks_every_kakao_group_missing() -> None:
    collector = AmenityCollector(kakao=DisabledKakao(), tago=FakeTago(enabled=False))

    result = await collector.collect([], CENTER)

    assert result["subway"].state == "missing"
    assert result["subway"].note == KAKAO_DISABLED_NOTE
    # 상업시설은 카카오 원천이 아니라 지정 원천(대규모점포 원장) 미확보다.
    # 카카오 키 유무와 무관하게 같은 사유로 산정하지 않는다.
    assert result["retail"].state == "missing"
    assert "대규모점포" in result["retail"].note
    # 환승시설은 키 문제가 아니라 원천 자체가 없다. 사유를 구분해서 적는다.
    assert result["transfer"].note == TRANSFER_MISSING_NOTE
    # 정류장은 TAGO·카카오가 모두 없어 조회 실패로 남는다.
    assert result["bus_stop"].state == "missing"
    assert "실패" in result["bus_stop"].note


@pytest.mark.asyncio
async def test_group_states_and_notes_follow_the_source_map() -> None:
    kakao = FakeKakao(
        categories={"SW8": [place("판교역", 400)], "CT1": [place("판교아트홀", 900)]},
    )
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]))

    result = await collector.collect([], CENTER)

    assert result["subway"].state == "connected"
    # 연결된 원천이라도 기준점이 출입구가 아니면 그 사실을 밝힌다.
    assert "역 대표점" in result["subway"].note
    assert result["culture"].state == "connected"
    assert result["culture"].note == ""
    assert result["hospital"].state == "substituted"
    assert "건강보험심사평가원" in result["hospital"].note


@pytest.mark.asyncio
async def test_filters_drop_places_the_scoresheet_does_not_recognise() -> None:
    kakao = FakeKakao(
        categories={
            "HP8": [
                place("서울대병원", 800, "의료,건강 > 병원 > 종합병원"),
                place("행복의원", 300, "의료,건강 > 병원 > 의원"),
            ],
            "SC4": [
                place("판교초등학교", 300),
                place("낙생중학교", 600),
                place("분당고등학교", 900),
                place("가천대학교", 1200, "교육,학문 > 학교 > 대학교"),
            ],
        },
        keywords={
            "기차역": [place("성남역", 700), place("성남역 앞 카페", 720)],
            "공원": [
                place("중앙공원", 500, "여행 > 관광,명소 > 공원"),
                # 이름에 '공원' 이 들어가도 분류가 놀이터면 공원이 아니다.
                place("중앙공원 놀이터", 480, "여행 > 관광,명소 > 놀이터"),
            ],
            # 대학교 판별은 분류로 한다. 이름만으로는 어학원·상가가 섞인다.
            "대학교": [
                place("가천대학교", 1200, "교육,학문 > 학교 > 대학교"),
                # 이름에 '대학교' 가 들어가도 분류가 어학원이면 대학이 아니다.
                place(
                    "가천대학교SLP 분당",
                    900,
                    "교육,학문 > 학원 > 어학원 > 영어학원 > 가천대학교SLP",
                ),
            ],
        },
    )
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]))

    result = await collector.collect([], CENTER)

    assert [f.name for f in result["hospital"].facilities] == ["서울대병원"]
    assert [f.name for f in result["railway"].facilities] == ["성남역"]
    assert [f.name for f in result["park"].facilities] == ["중앙공원"]
    assert [f.name for f in result["school_elementary"].facilities] == ["판교초등학교"]
    assert [f.name for f in result["school_middle"].facilities] == ["낙생중학교"]
    assert [f.name for f in result["school_high"].facilities] == ["분당고등학교"]
    # SC4 와 키워드 검색이 같은 대학을 물어 와도 한 번만 센다.
    assert [f.name for f in result["university"].facilities] == ["가천대학교"]


def test_university_accepts_junior_colleges() -> None:
    """전문대학도 대학교로 센다.

    카카오 분류는 같은 성격의 학교를 다르게 적는다 — 전주비전대학교는
    「학교 > 대학교」, 전북과학대학교·군산간호대학교·한국폴리텍대학은
    「학교 > 전문대학」이다(2026-09-11 실측). 「대학교」로 시작하는 것만 받으면
    앞의 하나만 잡혀, 판정이 원천 표기의 흔들림에 좌우된다.
    """

    from app.screening.amenities import RawPlace, _is_university

    def at(name: str, category: str) -> RawPlace:
        return RawPlace(name=name, address="", category_name=category, coordinates=CENTER)

    assert _is_university(at("전주비전대학교", "교육,학문 > 학교 > 대학교"))
    assert _is_university(at("전북과학대학교", "교육,학문 > 학교 > 전문대학"))
    assert _is_university(at("군산간호대학교", "교육,학문 > 학교 > 전문대학"))
    # 기능대학. LH 확인 요청 7번이 미결이라 지금은 함께 센다.
    assert _is_university(at("한국폴리텍대학 익산캠퍼스", "교육,학문 > 학교 > 전문대학"))

    # 오탐 차단은 그대로다.
    assert not _is_university(at("전주기전중학교", "교육,학문 > 학교 > 중학교"))
    assert not _is_university(at("버거아이엔지 전주대점", "교육,학문 > 학교 > 대학교"))
    assert not _is_university(
        at("서강대학교SLP 전주", "교육,학문 > 학원 > 어학원 > 영어학원 > 서강대학교SLP")
    )
    # 부속시설은 학교 자체가 아니다.
    assert not _is_university(
        at("한국폴리텍대학 익산캠퍼스 본관", "교육,학문 > 학교부속시설")
    )


@pytest.mark.asyncio
async def test_terminal_merges_two_keyword_searches() -> None:
    kakao = FakeKakao(
        keywords={
            "버스터미널": [place("성남종합버스터미널", 1500)],
            "고속버스터미널": [place("성남종합버스터미널", 1500), place("서울고속터미널", 2800)],
        }
    )
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]))

    result = await collector.collect([], CENTER)

    assert len(result["terminal"].facilities) == 2
    assert result["terminal"].state == "substituted"


@pytest.mark.asyncio
async def test_source_failure_becomes_missing_with_reason() -> None:
    kakao = FakeKakao(fail={"SW8"})
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]))

    result = await collector.collect([], CENTER)

    assert result["subway"].state == "missing"
    assert result["subway"].note.startswith("원천 조회에 실패했습니다:")
    # 한 원천이 죽어도 나머지 시설군은 살아 있어야 한다.
    assert result["culture"].state == "connected"


@pytest.mark.asyncio
async def test_bus_stops_prefer_tago_and_fall_back_to_kakao() -> None:
    rows = [{"gpslati": CENTER.lat, "gpslong": CENTER.lng, "nodenm": "판교역"}]
    tago = FakeTago(rows)
    collector = AmenityCollector(kakao=FakeKakao(), tago=tago)

    result = await collector.collect([], CENTER)
    assert tago.calls == 1
    assert [f.name for f in result["bus_stop"].facilities] == ["판교역"]
    assert "TAGO" in result["bus_stop"].actual_source

    broken = BrokenTago([])
    kakao = FakeKakao(keywords={"버스정류장": [place("판교역환승센터", 200)]})
    fallback = AmenityCollector(kakao=kakao, tago=broken)

    result = await fallback.collect([], CENTER)
    assert broken.calls == 1
    assert [f.name for f in result["bus_stop"].facilities] == ["판교역환승센터"]
    assert result["bus_stop"].state == "substituted"


@pytest.mark.asyncio
async def test_distance_is_measured_from_the_parcel_boundary() -> None:
    half = 20.0
    ring = [
        offset_coordinates(CENTER, -half, -half),
        offset_coordinates(CENTER, -half, half),
        offset_coordinates(CENTER, half, half),
        offset_coordinates(CENTER, half, -half),
        offset_coordinates(CENTER, -half, -half),
    ]
    kakao = FakeKakao(categories={"SW8": [place("판교역", 400)]})
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]))

    from_center = await collector.collect([], CENTER)
    from_boundary = await collector.collect([ring], CENTER)

    center_distance = from_center["subway"].distances_m[0]
    boundary_distance = from_boundary["subway"].distances_m[0]
    assert center_distance == pytest.approx(400, abs=2)
    # 경계에서 재면 반쪽 폭만큼 가까워진다.
    assert boundary_distance == pytest.approx(center_distance - half, abs=2)


@pytest.mark.asyncio
async def test_repeated_collection_uses_the_cache() -> None:
    kakao = FakeKakao(categories={"SW8": [place("판교역", 400)]})
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]))

    await collector.collect([], CENTER)
    first_calls = len(kakao.calls)
    await collector.collect([], CENTER)

    assert len(kakao.calls) == first_calls


# ---------------------------------------------------------------------------
# 카카오 45건 상한 — 잘린 결과를 그대로 쓰면 먼 시설이 통째로 사라진다
# ---------------------------------------------------------------------------
class TestKakaoResultCap:
    """카카오 로컬 API 는 한 질의당 45건(3페이지 x 15)이 하드 상한이다.

    3km 반경에서 병원·문화시설·학교는 이 상한에 걸린다. 거리순 정렬이라
    가까운 의원이 45칸을 채우면 먼 종합병원이 통째로 빠진다. 상한에 걸린
    질의는 영역을 쪼개 다시 훑어야 한다.
    """

    class CappedKakao:
        """반경이 크면 45건에서 자르는 카카오. 쪼개서 물으면 먼 시설도 준다."""

        enabled = True
        CAP = 45

        def __init__(self) -> None:
            # 중심에서 2.5km 떨어진 '먼 시설' 하나와, 근처를 채우는 잡음 60개.
            self.far = {"place_name": "먼종합병원", "x": "127.1480", "y": "35.8467"}
            self.near = [
                {"place_name": f"의원{i}", "x": "127.1481", "y": "35.8243"}
                for i in range(60)
            ]
            self.calls = 0

        async def search_category(self, code, lat, lng, radius_m, max_pages=3):
            self.calls += 1
            inside = []
            for row in [*self.near, self.far]:
                d = haversine_meters(
                    Coordinates(lat=lat, lng=lng),
                    Coordinates(lat=float(row["y"]), lng=float(row["x"])),
                )
                if d <= radius_m:
                    inside.append(row)
            # 거리순으로 자른다 — 실제 API 와 같은 방식.
            inside.sort(
                key=lambda r: haversine_meters(
                    Coordinates(lat=lat, lng=lng),
                    Coordinates(lat=float(r["y"]), lng=float(r["x"])),
                )
            )
            return inside[: self.CAP]

        async def search_keyword(self, query, lat, lng, radius_m, max_pages=3):
            return []

    def test_capped_category_search_still_finds_far_facility(self) -> None:
        kakao = self.CappedKakao()
        collector = AmenityCollector(kakao=kakao, tago=None)  # type: ignore[arg-type]
        rows = asyncio.run(
            collector._kakao_category("HP8", Coordinates(lat=35.8242, lng=127.1480), 3000)
        )
        names = {p.name for p in rows.places}
        assert "먼종합병원" in names, f"상한에 잘려 먼 시설이 사라졌습니다 (호출 {kakao.calls}회)"


# ---------------------------------------------------------------------------
# 시설군 필터는 이름이 아니라 분류값으로 걸러야 한다
# ---------------------------------------------------------------------------
class TestCategoryLeafFilters:
    """이름에 낱말이 들어갔다고 그 시설인 것은 아니다.

    실측에서 '리드앤톡 영어도서관학원'(학원)·'쪽구름도서관 화장실'(화장실)·
    '현대가구백화점'(가구점)이 그대로 배점 근거로 올라왔다. 카카오가 주는
    category_name 의 마지막 조각으로 걸러야 심사표가 말한 시설만 남는다.
    """

    @staticmethod
    def _place(name: str, category: str) -> Any:
        from app.screening.amenities import RawPlace

        return RawPlace(
            name=name,
            address="",
            category_name=f"가정,생활 > {category}",
            coordinates=Coordinates(lat=35.8242, lng=127.1480),
        )

    def test_park_filter_drops_park_fixtures(self) -> None:
        from app.screening.amenities import GROUP_SPECS

        keep = GROUP_SPECS["park"].keep
        assert keep is not None
        assert keep(self._place("여의공원", "공원"))
        assert keep(self._place("만성지구 근린공원", "도시근린공원"))
        assert not keep(self._place("쪽구름도서관 화장실", "화장실"))
        assert not keep(self._place("공원 주차장", "주차장"))
        assert not keep(self._place("여의 풋살장", "풋살장"))

    def test_library_filter_drops_academies_and_toilets(self) -> None:
        from app.screening.amenities import GROUP_SPECS

        keep = GROUP_SPECS["public"].keep
        assert keep is not None
        assert keep(self._place("장동푸른작은도서관", "작은도서관"))
        assert keep(self._place("전주시립도서관", "국공립도서관"))
        assert keep(self._place("전주시청", "시청"))  # PO3 공공기관은 그대로 통과
        assert not keep(self._place("리드앤톡 영어도서관학원 전주여의센터", "영어학원"))
        assert not keep(self._place("쪽구름도서관 화장실", "화장실"))

    def test_retail_uses_localdata_not_kakao_approximation(self) -> None:
        # 심사표는 대규모점포·전통시장 등재분만 인정한다. 카카오 '대형마트'
        # 분류에는 동네 마트가 섞여 근사가 성립하지 않으므로, 카카오가 아니라
        # 대규모점포 인허가 원장(localdata)을 직접 지정 원천으로 쓴다.
        from app.screening.amenities import GROUP_SPECS

        spec = GROUP_SPECS["retail"]
        assert spec.kakao_backed is False
        assert spec.localdata_datasets == ("large_scale_retail_stores",)
        # 미적재 시 남길 사유에는 여전히 '대규모점포' 가 들어간다.
        assert "대규모점포" in spec.missing_note


# ---------------------------------------------------------------------------
# 상업시설(retail) — 대규모점포 인허가 원장(localdata) 직결
# ---------------------------------------------------------------------------
class TestRetailLocalData:
    """retail 은 카카오 근사가 아니라 대규모점포 인허가 캐시를 직접 조회한다."""

    @staticmethod
    def _store(tmp_path, records):
        from app.services.facility_store import FacilityStore

        store = FacilityStore(db_path=tmp_path / "facilities.db")
        if records:
            store.replace_dataset("large_scale_retail_stores", records)
        return store

    @staticmethod
    def _record(name: str, offset_m: float):
        from app.services.localdata import LocalDataRecord

        point = offset_coordinates(CENTER, offset_m, 0)
        return LocalDataRecord(
            dataset_key="large_scale_retail_stores",
            record_id=name,
            name=name,
            address=f"{name} 지번주소",
            road_address=f"{name} 도로명주소",
            coordinates=point,
            status="영업/정상",
            category="대규모점포",
        )

    def test_no_store_reports_missing_not_zero(self) -> None:
        # facility_store 가 없으면 '0개' 가 아니라 미적재 사유로 남긴다.
        from app.screening.amenities import RETAIL_MISSING_NOTE

        collector = AmenityCollector(kakao=FakeKakao(), tago=FakeTago([]))
        result = asyncio.run(collector.collect([], CENTER))

        assert result["retail"].state == "missing"
        assert result["retail"].note == RETAIL_MISSING_NOTE

    def test_empty_dataset_reports_missing(self, tmp_path) -> None:
        store = self._store(tmp_path, [])
        collector = AmenityCollector(
            kakao=FakeKakao(), tago=FakeTago([]), facility_store=store
        )
        result = asyncio.run(collector.collect([], CENTER))

        assert result["retail"].state == "missing"
        assert "대규모점포" in result["retail"].note

    def test_loaded_dataset_populates_from_localdata(self, tmp_path) -> None:
        store = self._store(
            tmp_path,
            [self._record("행복대형마트", 400), self._record("먼점포", 4000)],
        )
        collector = AmenityCollector(
            kakao=FakeKakao(), tago=FakeTago([]), facility_store=store
        )
        result = asyncio.run(collector.collect([], CENTER, radius_m=3000))

        retail = result["retail"]
        assert retail.state == "connected"
        # 반경(3km) 밖 점포는 빠지고, 가까운 점포만 지정 원천으로 잡힌다.
        assert [f.name for f in retail.facilities] == ["행복대형마트"]
        assert "대규모점포" in retail.actual_source
        # 전통시장 미확보 사실을 note 로 남긴다.
        assert "전통시장" in retail.note

    def test_localdata_hit_carries_shortest_line_anchors(self, tmp_path) -> None:
        # 2a 잔여 결함: 상업시설(localdata) hit 의 최단거리선이 null 이던 것을 고친다.
        # 대지 경계가 있으면 _build_group 과 같이 시설 기준점·경계 앵커를 채운다.
        half = 20.0
        ring = [
            offset_coordinates(CENTER, -half, -half),
            offset_coordinates(CENTER, -half, half),
            offset_coordinates(CENTER, half, half),
            offset_coordinates(CENTER, half, -half),
            offset_coordinates(CENTER, -half, -half),
        ]
        store = self._store(tmp_path, [self._record("행복대형마트", 400)])
        collector = AmenityCollector(
            kakao=FakeKakao(), tago=FakeTago([]), facility_store=store
        )
        result = asyncio.run(collector.collect([ring], CENTER, radius_m=3000))

        hit = result["retail"].facilities[0]
        assert hit.nearest_facility_point == hit.coordinates
        assert hit.nearest_boundary_point is not None


# ---------------------------------------------------------------------------
# 의료시설(hospital) — 국립중앙의료원 종합병원 원장 직결
# ---------------------------------------------------------------------------
class TestHospitalNcmc:
    """의료시설은 국립중앙의료원 원장이 붙으면 카카오 근사에서 지정 원천으로 바뀐다."""

    class RegionKakao(FakeKakao):
        async def region_info(self, lat, lng):
            from app.services.kakao import RegionInfo

            return RegionInfo(
                legal_name="전북특별자치도 전주시 완산구 서노송동",
                legal_code="",
                administrative_name="",
                administrative_code="",
            )

    class FakeHospitalClient:
        enabled = True

        def __init__(self, records):
            self.records = records
            self.seen_sido: str | None = None

        async def hospitals_around(self, center, sido, radius_m):
            self.seen_sido = sido
            return [
                r
                for r in self.records
                if haversine_meters(center, r.coordinates) <= radius_m
            ]

    @staticmethod
    def _record(name: str, offset_m: float, div: str = "종합병원"):
        from app.services.ncmc_hospital import HospitalRecord

        return HospitalRecord(
            hpid=name,
            name=name,
            coordinates=offset_coordinates(CENTER, offset_m, 0),
            div_name=div,
            is_tertiary=(div == "상급종합병원"),
        )

    def test_hospital_uses_ncmc_when_client_present(self) -> None:
        hospital_client = self.FakeHospitalClient(
            [
                self._record("전주종합병원", 500),
                self._record("전북대학교병원", 800, "상급종합병원"),
            ]
        )
        collector = AmenityCollector(
            kakao=self.RegionKakao(),
            tago=FakeTago([]),
            hospital_client=hospital_client,
        )
        result = asyncio.run(collector.collect([], CENTER))

        hospital = result["hospital"]
        # 카카오 근사(substituted)가 아니라 지정 원천(connected)으로 바뀐다.
        assert hospital.state == "connected"
        assert "국립중앙의료원" in hospital.note
        # 종합병원·상급종합병원이 모두 잡히되, 시도는 카카오 역지오코딩으로 구한다.
        assert hospital_client.seen_sido == "전북특별자치도"
        assert {f.name for f in hospital.facilities} == {"전주종합병원", "전북대학교병원"}

    def test_falls_back_to_kakao_when_no_client(self) -> None:
        # 원천이 없으면 기존 카카오 근사 동작을 유지한다.
        kakao = FakeKakao(
            categories={"HP8": [place("서울대병원", 800, "의료,건강 > 병원 > 종합병원")]}
        )
        collector = AmenityCollector(kakao=kakao, tago=FakeTago([]))
        result = asyncio.run(collector.collect([], CENTER))

        assert result["hospital"].state == "substituted"

    class NoRegionKakao(FakeKakao):
        async def region_info(self, lat, lng):
            from app.services.kakao import RegionInfo

            # 역지오코딩이 시도명을 못 준 상황(빈 법정동).
            return RegionInfo(
                legal_name="",
                legal_code="",
                administrative_name="",
                administrative_code="",
            )

    def test_reverse_geocode_failure_marks_hospital_missing_not_connected(self) -> None:
        # 시도 해석에 실패하면 NCMC 조회 자체가 불가능하다. 성공한 빈 피드로 두면
        # '병원 없음'과 구분되지 않아 2차 주거여건 등급이 잘못 내려가므로, 시설군이
        # connected(0건)가 아니라 missing 이어야 한다.
        hospital_client = self.FakeHospitalClient([self._record("전주종합병원", 500)])
        collector = AmenityCollector(
            kakao=self.NoRegionKakao(),
            tago=FakeTago([]),
            hospital_client=hospital_client,
        )
        result = asyncio.run(collector.collect([], CENTER))

        hospital = result["hospital"]
        assert hospital.state == "missing"
        assert hospital.state != "connected"
        # 조회 자체를 못 했으므로 병원 원장을 부르지 않았다.
        assert hospital_client.seen_sido is None


# ---------------------------------------------------------------------------
# 시설 경계 측정 — 초·중·고·공원·상업·문화·공공은 시설 필지경계에서 잰다
# (docs/hazards/MEASUREMENT.md §3). 정문·출구 예외군은 손대지 않는다.
# ---------------------------------------------------------------------------


def square_ring(center: Coordinates, half: float) -> list[Coordinates]:
    return [
        offset_coordinates(center, -half, -half),
        offset_coordinates(center, -half, half),
        offset_coordinates(center, half, half),
        offset_coordinates(center, half, -half),
        offset_coordinates(center, -half, -half),
    ]


class FakeVWorld:
    """좌표를 품는 필지를 정해진 반폭의 정사각형으로 돌려주는 대역."""

    enabled = True

    def __init__(self, half: float = 30.0, fail: bool = False, area_m2: float = 3600.0) -> None:
        self.half = half
        self.fail = fail
        self.area_m2 = area_m2
        self.calls: list[tuple[float, float]] = []

    async def parcel_at(self, lat: float, lng: float):
        from app.services.vworld import ParcelFeature

        self.calls.append((lat, lng))
        if self.fail:
            raise RuntimeError("VWorld 장애")
        return ParcelFeature(
            pnu="4511300000000000001",
            address="",
            jibun="100-1",
            ring=square_ring(Coordinates(lat=lat, lng=lng), self.half),
            area_m2=self.area_m2,
        )


@pytest.mark.asyncio
async def test_school_is_measured_to_its_parcel_boundary() -> None:
    site = square_ring(CENTER, 20.0)
    kakao = FakeKakao(categories={"SC4": [place("전주초등학교", 400, "교육,학문 > 학교 > 초등학교")]})
    vworld = FakeVWorld(half=30.0)
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]), vworld=vworld)

    result = await collector.collect([site], CENTER)

    group = result["school_elementary"]
    facility = group.facilities[0]
    # 점 기준 400 − 사업지 반폭 20 − 시설 필지 반폭 30 = 350.
    assert facility.distance_m == pytest.approx(350, abs=3)
    assert group.distances_m[0] == pytest.approx(facility.distance_m)
    assert facility.measurement_tier == "site_boundary"
    assert "시설 경계" in facility.measurement_label
    assert "100-1" in facility.front_door_notice
    assert facility.nearest_facility_point is not None
    assert facility.nearest_boundary_point is not None


@pytest.mark.asyncio
async def test_exception_groups_keep_their_point_basis() -> None:
    site = square_ring(CENTER, 20.0)
    kakao = FakeKakao(categories={"SW8": [place("판교역", 400)]})
    stop = offset_coordinates(CENTER, 300, 0)
    tago = FakeTago([{"nodenm": "정류장", "gpslati": stop.lat, "gpslong": stop.lng, "nodeid": "s1"}])
    vworld = FakeVWorld(half=30.0)
    collector = AmenityCollector(kakao=kakao, tago=tago, vworld=vworld)

    result = await collector.collect([site], CENTER)

    facility = result["subway"].facilities[0]
    assert facility.measurement_tier == "coordinate"
    assert facility.distance_m == pytest.approx(380, abs=3)
    # 버스정류장은 도로 필지 위라 필지경계로 재지 않고 정류장 좌표로 잰다.
    if result["bus_stop"].facilities:
        assert result["bus_stop"].facilities[0].measurement_tier == "coordinate"
    assert vworld.calls == []


@pytest.mark.asyncio
async def test_vworld_failure_falls_back_to_coordinate_measurement() -> None:
    site = square_ring(CENTER, 20.0)
    kakao = FakeKakao(categories={"SC4": [place("전주초등학교", 400, "교육,학문 > 학교 > 초등학교")]})
    collector = AmenityCollector(
        kakao=kakao, tago=FakeTago([]), vworld=FakeVWorld(fail=True)
    )

    result = await collector.collect([site], CENTER)

    facility = result["school_elementary"].facilities[0]
    assert facility.measurement_tier == "coordinate"
    assert facility.distance_m == pytest.approx(380, abs=3)
    assert "시설 경계" in facility.front_door_notice  # 폴백 사실을 숨기지 않는다


@pytest.mark.asyncio
async def test_boundary_lookup_is_limited_to_nearest_facilities() -> None:
    from app.screening.amenities import BOUNDARY_LOOKUP_PER_GROUP

    site = square_ring(CENTER, 20.0)
    rows = [
        place(f"공원{i}", 300 + 100 * i, "여행 > 관광,명소 > 공원")
        for i in range(BOUNDARY_LOOKUP_PER_GROUP + 3)
    ]
    kakao = FakeKakao(keywords={"공원": rows})
    vworld = FakeVWorld(half=30.0)
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]), vworld=vworld)

    result = await collector.collect([site], CENTER)

    group = result["park"]
    assert len(vworld.calls) == BOUNDARY_LOOKUP_PER_GROUP
    assert len(group.distances_m) == len(rows)
    assert list(group.distances_m) == sorted(group.distances_m)
    tiers = [f.measurement_tier for f in group.facilities]
    assert tiers[:BOUNDARY_LOOKUP_PER_GROUP] == ["site_boundary"] * BOUNDARY_LOOKUP_PER_GROUP
    assert tiers[BOUNDARY_LOOKUP_PER_GROUP:] == ["coordinate"] * 3


@pytest.mark.asyncio
async def test_oversized_parcel_keeps_point_basis_with_notice() -> None:
    from app.screening.front_door import CAMPUS_PARCEL_MAX_AREA_M2

    site = square_ring(CENTER, 20.0)
    kakao = FakeKakao(categories={"SC4": [place("전주초등학교", 400, "교육,학문 > 학교 > 초등학교")]})
    vworld = FakeVWorld(half=300.0, area_m2=CAMPUS_PARCEL_MAX_AREA_M2 * 2)
    collector = AmenityCollector(kakao=kakao, tago=FakeTago([]), vworld=vworld)

    result = await collector.collect([site], CENTER)

    facility = result["school_elementary"].facilities[0]
    assert facility.measurement_tier == "coordinate"
    assert facility.distance_m == pytest.approx(380, abs=3)
    assert "통필지" in facility.front_door_notice
