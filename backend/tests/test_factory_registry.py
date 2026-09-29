"""산단공 공장등록 필지정보 API(15087615) 어댑터 — 지역 열거·지오코딩 캐시·서비스 배선.

2026-09-15 실측: cmpnyNm 공백 와일드카드 + adresCode(시군구 5자리) 로 지역 전량을
받을 수 있다. 좌표가 없어 도로명주소를 지오코딩하며, 이 결과는 디스크에 캐시한다.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.models import Coordinates
from app.services.factory_registry import (
    FACTORY_PARCEL_URL,
    WILDCARD_NAME,
    FactoryRegistryClient,
    PublicDataAPIError,
    geocode_query,
    record_from_row,
)
from app.services.geo import offset_coordinates

CENTER = Coordinates(lat=35.9678, lng=126.7368)


def xml_page(items: list[dict[str, str]], total: int) -> str:
    body = "".join(
        "<item>" + "".join(f"<{k}>{v}</{k}>" for k, v in item.items()) + "</item>"
        for item in items
    )
    return (
        "<response><header><resultCode>00</resultCode><resultMsg>NORMAL_SERVICE</resultMsg>"
        f"</header><body><items>{body}</items><numOfRows>1000</numOfRows><pageNo>1</pageNo>"
        f"<totalCount>{total}</totalCount></body></response>"
    )


def factory_row(no: str, name: str, road: str) -> dict[str, str]:
    return {
        "fctryManageNo": no,
        "cmpnyNm": name,
        "rnAdres": road,
        "rprsntvNm": "홍길동",
        "cvplChrgOrgnztNm": "전북특별자치도 군산시",
        "cmpnyTelno": "",
        "fctryLndpclAr": "",
        "fctryDongBuldAr": "1043.600",
        "spfcSeCodeNm": "도시지역/공업지역/준공업지역",
        "irsttNm": "",
    }


class Recorder:
    """요청 파라미터를 기록하고 정해진 XML 을 돌려주는 transport."""

    def __init__(self, pages: list[str]) -> None:
        self.pages = pages
        self.requests: list[dict[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            self.requests.append(params)
            page = int(params.get("pageNo", "1"))
            return httpx.Response(200, text=self.pages[min(page, len(self.pages)) - 1])

        return httpx.MockTransport(handler)


def test_geocode_query_drops_parenthesised_building_names() -> None:
    assert geocode_query("전북특별자치도 군산시 내항2길 283 (해망동, 백화점냉동공장)") == (
        "전북특별자치도 군산시 내항2길 283"
    )


def test_record_from_row_keeps_registry_fields() -> None:
    record = record_from_row(factory_row("2431", "군산얼음", "군산시 내항2길 283"), None)
    assert record.record_id == "2431"
    assert record.zoning_name == "도시지역/공업지역/준공업지역"
    assert record.building_area_m2 == pytest.approx(1043.6)
    assert record.coordinates is None


@pytest.mark.asyncio
async def test_sigungu_listing_uses_wildcard_name_and_area_code(tmp_path) -> None:
    recorder = Recorder([xml_page([factory_row("1", "A공장", "군산시 1로 1")], total=1)])
    client = FactoryRegistryClient(
        "key", cache_path=tmp_path / "geo.json", transport=recorder.transport()
    )

    rows = await client.factories_in_sigungu("5213011900100010000")

    assert [r["cmpnyNm"] for r in rows] == ["A공장"]
    sent = recorder.requests[0]
    assert sent["cmpnyNm"] == WILDCARD_NAME
    assert sent["adresCode"] == "52130"
    assert sent["numOfRows"] == "1000"
    # 24시간 캐시: 같은 시군구 재조회는 원격을 타지 않는다.
    await client.factories_in_sigungu("52130")
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_api_error_code_raises(tmp_path) -> None:
    body = (
        "<response><header><resultCode>11</resultCode>"
        "<resultMsg>NO_MANDATORY_REQUEST_PARAMETERS_ERROR</resultMsg></header></response>"
    )
    client = FactoryRegistryClient(
        "key", cache_path=tmp_path / "geo.json", transport=Recorder([body]).transport()
    )
    with pytest.raises(PublicDataAPIError):
        await client.factories_in_sigungu("52130")


@pytest.mark.asyncio
async def test_factories_near_geocodes_once_and_caches_to_disk(tmp_path) -> None:
    near = offset_coordinates(CENTER, 30, 0)
    far = offset_coordinates(CENTER, 5000, 0)
    calls: list[str] = []

    async def geocoder(address: str) -> Coordinates | None:
        calls.append(address)
        return {"군산시 근처로 1": near, "군산시 먼로 2": far}.get(address)

    pages = [
        xml_page(
            [
                factory_row("1", "가까운공장", "군산시 근처로 1 (산북동)"),
                factory_row("2", "먼공장", "군산시 먼로 2"),
                factory_row("3", "주소없는공장", ""),
                factory_row("4", "지오코딩실패", "군산시 없는길 9"),
            ],
            total=4,
        )
    ]
    cache = tmp_path / "geo.json"
    client = FactoryRegistryClient(
        "key", geocoder=geocoder, cache_path=cache, transport=Recorder(pages).transport()
    )

    found = await client.factories_near(CENTER, 100, "52130")

    assert [f.name for f in found] == ["가까운공장"]
    assert found[0].coordinates == near
    # 괄호 안 동명은 빼고 묻는다.
    assert "군산시 근처로 1" in calls
    # 좌표를 못 붙인 행은 격리 목록에 남는다(삭제하지 않는다).
    assert client.geocode_failures == ["군산시 없는길 9"]
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved["군산시 없는길 9"] is None
    assert saved["군산시 근처로 1 (산북동)"]["lat"] == pytest.approx(near.lat)

    # 두 번째 호출은 디스크 캐시로 지오코딩하지 않는다.
    calls.clear()
    again = FactoryRegistryClient(
        "key", geocoder=geocoder, cache_path=cache, transport=Recorder(pages).transport()
    )
    await again.factories_near(CENTER, 100, "52130")
    assert calls == []


@pytest.mark.asyncio
async def test_vworld_fallback_places_vanished_lot_and_respects_switch(tmp_path) -> None:
    """카카오가 못 찾은 옛 지번(효자동2가 368번지)은 보조 지오코더로 한 번 더 찾는다.

    보조 결과는 「vworld|주소」 키로 따로 캐시해, 스위치를 끄면 카카오 결과만 쓴다.
    """

    old_lot = "전북특별자치도 전주시완산구 효자동2가 368번지"
    near = offset_coordinates(CENTER, 37, 0)
    kakao_calls: list[str] = []
    vworld_calls: list[str] = []

    async def kakao(address: str) -> Coordinates | None:
        kakao_calls.append(address)
        return None

    async def vworld(address: str) -> Coordinates | None:
        vworld_calls.append(address)
        return near

    pages = [xml_page([factory_row("9", "현대콘크리트", old_lot)], total=1)]
    cache = tmp_path / "geo.json"
    switch = {"on": True}
    client = FactoryRegistryClient(
        "key", geocoder=kakao, cache_path=cache, transport=Recorder(pages).transport(),
        fallback_geocoder=vworld, fallback_enabled=lambda: switch["on"],
    )

    found = await client.factories_near(CENTER, 100, "52111")
    assert [f.name for f in found] == ["현대콘크리트"]
    assert vworld_calls == [old_lot]
    saved = json.loads(cache.read_text(encoding="utf-8"))
    assert saved[old_lot] is None
    assert saved["vworld|" + old_lot]["lat"] == pytest.approx(near.lat)

    # 캐시가 있으면 다시 묻지 않고, 스위치를 끄면 보조 결과를 쓰지 않는다.
    switch["on"] = False
    assert await client.factories_near(CENTER, 100, "52111") == []
    switch["on"] = True
    assert [f.name for f in await client.factories_near(CENTER, 100, "52111")] == ["현대콘크리트"]
    assert vworld_calls == [old_lot] and kakao_calls == [old_lot]


# ---------------------------------------------------------------------------
# 서비스 배선 — API 등록공장이 「공장 있음」 검토 표시로 올라온다
# ---------------------------------------------------------------------------


class FakeFactoryRegistry:
    enabled = True

    def __init__(self, records: list[Any]) -> None:
        self.records = records
        self.calls: list[tuple[float, str]] = []

    async def factories_near(self, center, radius_m, sigungu_code):
        self.calls.append((radius_m, sigungu_code))
        return list(self.records)


def test_api_factory_becomes_review_candidate() -> None:
    from tests.test_hazard_review_service import (
        SITE_CENTER,
        build_request,
        category_for,
        run_review,
    )
    from app.hazard_review.service import HazardReviewService
    from tests.test_hazard_review_service import FakeKakaoClient

    request = build_request(
        "house", "general", geometry_source="parcel_polygon", half_size_m=20,
        pnu="5213011900100010000",
    )
    record = record_from_row(
        factory_row("77", "군산정밀", "군산시 산북동 1"),
        offset_coordinates(SITE_CENTER, 0, 40),
    )
    registry = FakeFactoryRegistry([record])
    service = HazardReviewService(
        kakao=FakeKakaoClient(),  # type: ignore[arg-type]
        demo_mode=False,
        factory_registry=registry,  # type: ignore[arg-type]
    )

    async def progress(*_args: Any) -> None:
        return None

    result = asyncio.run(service.review(request, progress, asyncio.Event()))
    factory = category_for(result, "factory_registered")
    assert registry.calls and registry.calls[0][1] == "52130"
    assert factory.status == "review_required"
    assert "공장 있음" in factory.note
    hit = next(
        f
        for finding in result.findings
        for f in list(finding.facilities) + list(finding.nearby_facilities)
        if f.facility_type == "factory"
    )
    assert hit.name == "군산정밀"
    assert hit.provider == "local_sources"
    assert hit.metadata.get("factory_registered") is True
    assert all(
        c.status != "exclusion_match" for c in result.categories if c.rule_id == "RB14-FACTORY"
    )
    # 이 밖의 공장 사유(dataset_missing)가 남지 않는다.
    assert "등록공장 원천 없음" not in (factory.note or "")


class BrokenFactoryRegistry(FakeFactoryRegistry):
    async def factories_near(self, center, radius_m, sigungu_code):
        raise PublicDataAPIError("등록공장 조회 실패 (04) HTTP 에러")


def test_api_outage_is_dataset_missing_not_no_conflict() -> None:
    """산단공 API 장애(2026-09-30 실측 「04 HTTP 에러」)를 「충돌 없음」으로 접지 않는다."""

    from tests.test_hazard_review_service import FakeKakaoClient, build_request, category_for
    from app.hazard_review.service import HazardReviewService

    request = build_request(
        "house", "general", geometry_source="parcel_polygon", half_size_m=20,
        pnu="5213011900100010000",
    )
    service = HazardReviewService(
        kakao=FakeKakaoClient(),  # type: ignore[arg-type]
        demo_mode=False,
        factory_registry=BrokenFactoryRegistry([]),  # type: ignore[arg-type]
    )

    async def progress(*_args: Any) -> None:
        return None

    result = asyncio.run(service.review(request, progress, asyncio.Event()))
    factory = category_for(result, "factory_registered")
    assert factory.status == "dataset_missing"
    assert "조회 실패" in factory.note and "재심사" in factory.note
    # 원천 상태에도 「실패」 줄이 생겨 심사 결과 경고(source_alerts)가 재심사를 안내한다.
    failed = [s for s in result.sources if s.state == "failed"]
    assert [s.source_id for s in failed] == ["failed-factory_registry"]
