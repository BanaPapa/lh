"""카카오가 막힐 때(일일 쿼터 초과 등) 네이버(NCP) 지오코딩 → VWorld 로 넘어가는 지오코딩 체인."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import Settings, get_settings
from app.models import Coordinates, GeocodeCandidate
from app.services import address_geocoder
from app.services.address_geocoder import (
    GeocodeUnavailable,
    make_address_geocoder,
    search_address_candidates,
)
from app.services.kakao import KakaoAPIError
from app.services.naver_geocode import (
    NaverGeocodeClient,
    keeps_lot,
    lot_key,
    NaverAddress,
    reset_auth_failures,
)

ADDRESS = "전북특별자치도 전주시 덕진구 인후동2가 1575-1"
NAVER_POINT = Coordinates(lat=35.8301, lng=127.1502)
VWORLD_POINT = Coordinates(lat=35.8302, lng=127.1503)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    address_geocoder.reset_outages()
    reset_auth_failures()
    monkeypatch.setattr(address_geocoder, "TRANSIENT_BACKOFF_SECONDS", 0)
    yield
    address_geocoder.reset_outages()
    reset_auth_failures()


class FakeKakao:
    enabled = True
    api_key = "kakao-test"

    def __init__(self, *, error: Exception | None = None, found: list | None = None) -> None:
        self.error = error
        self.found = found or []
        self.calls: list[str] = []

    async def geocode(self, query: str):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return self.found


def quota_error() -> KakaoAPIError:
    return KakaoAPIError("API limit has been exceeded.", 400)


class FakeVWorld:
    enabled = True

    def __init__(self, point: Coordinates | None = None, error: Exception | None = None) -> None:
        self.point = point
        self.error = error
        self.calls: list[str] = []

    async def search_address_point(self, query: str, strict: bool = False):
        self.calls.append(query)
        if self.error is not None:
            raise self.error
        return self.point


def naver_client(handler) -> tuple[NaverGeocodeClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return (
        NaverGeocodeClient("naver-id", "naver-secret", transport=httpx.MockTransport(wrapped)),
        seen,
    )


def naver_ok(jibun: str = ADDRESS, road: str = "전북특별자치도 전주시 덕진구 기린대로 1"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": "OK",
            "addresses": [{
                "roadAddress": road, "jibunAddress": jibun,
                "x": str(NAVER_POINT.lng), "y": str(NAVER_POINT.lat),
            }],
        })

    return handler


def naver_auth_failed(request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": {"errorCode": "200", "message": "Authentication Failed"}})


def naver_empty(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "OK", "addresses": []})


def naver_server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": {"errorCode": "500", "message": "Internal"}})


@pytest.mark.asyncio
async def test_kakao_quota_error_falls_back_to_naver_and_skips_kakao_afterwards() -> None:
    kakao = FakeKakao(error=quota_error())
    naver, seen = naver_client(naver_ok())
    geocode = make_address_geocoder(kakao, FakeVWorld(), naver=naver)

    assert await geocode(ADDRESS) == NAVER_POINT
    # 두 번째 주소부터는 막힌 카카오를 기다리지 않는다.
    assert await geocode(ADDRESS) == NAVER_POINT
    assert len(kakao.calls) == 1
    assert len(seen) == 2
    headers = seen[0].headers
    assert headers["x-ncp-apigw-api-key-id"] == "naver-id"
    assert headers["x-ncp-apigw-api-key"] == "naver-secret"
    assert seen[0].url.params["query"] == ADDRESS


@pytest.mark.asyncio
async def test_naver_auth_failure_uses_vworld_and_disables_naver_for_the_run() -> None:
    kakao = FakeKakao(error=quota_error())
    naver, seen = naver_client(naver_auth_failed)
    vworld = FakeVWorld(point=VWORLD_POINT)
    geocode = make_address_geocoder(kakao, vworld, naver=naver)

    assert await geocode(ADDRESS) == VWORLD_POINT
    assert await geocode(ADDRESS) == VWORLD_POINT
    assert len(seen) == 1  # 인증 실패는 한 번만 두드린다
    assert naver.enabled is False
    assert naver.disabled_reason == "Authentication Failed"


@pytest.mark.asyncio
async def test_naver_auth_failure_alone_does_not_block_a_clean_not_found() -> None:
    naver, _ = naver_client(naver_auth_failed)
    geocode = make_address_geocoder(FakeKakao(), FakeVWorld(), naver=naver)

    assert await geocode(ADDRESS) is None


@pytest.mark.asyncio
async def test_all_providers_error_raises_instead_of_not_found() -> None:
    naver, _ = naver_client(naver_server_error)
    vworld = FakeVWorld(error=httpx.ConnectError("down"))
    geocode = make_address_geocoder(FakeKakao(error=quota_error()), vworld, naver=naver)

    with pytest.raises(GeocodeUnavailable):
        await geocode(ADDRESS)


@pytest.mark.asyncio
async def test_kakao_outage_without_other_hits_raises_so_caches_stay_clean() -> None:
    naver, _ = naver_client(naver_empty)
    geocode = make_address_geocoder(FakeKakao(error=quota_error()), FakeVWorld(), naver=naver)

    with pytest.raises(GeocodeUnavailable):
        await geocode(ADDRESS)


@pytest.mark.asyncio
async def test_clean_not_found_returns_none() -> None:
    naver, _ = naver_client(naver_empty)
    vworld = FakeVWorld()
    geocode = make_address_geocoder(FakeKakao(), vworld, naver=naver)

    assert await geocode(ADDRESS) is None
    assert vworld.calls


@pytest.mark.asyncio
async def test_naver_result_that_lost_the_lot_number_is_rejected() -> None:
    # 번지를 잃고 동 중심점으로 떨어진 결과는 받지 않고 VWorld 로 넘어간다.
    naver, _ = naver_client(naver_ok(jibun="전북특별자치도 전주시 덕진구 인후동2가", road=""))
    geocode = make_address_geocoder(
        FakeKakao(error=quota_error()), FakeVWorld(point=VWORLD_POINT), naver=naver
    )

    assert await geocode(ADDRESS) == VWORLD_POINT


@pytest.mark.asyncio
async def test_fallback_on_miss_false_keeps_kakao_answer_when_kakao_is_healthy() -> None:
    naver, seen = naver_client(naver_ok())
    geocode = make_address_geocoder(FakeKakao(), naver=naver, fallback_on_miss=False)

    assert await geocode(ADDRESS) is None
    assert seen == []

    address_geocoder.reset_outages()
    down = make_address_geocoder(FakeKakao(error=quota_error()), naver=naver, fallback_on_miss=False)
    assert await down(ADDRESS) == NAVER_POINT


def test_lot_matching_rules() -> None:
    assert lot_key("전주시 덕진구 인후동2가 1575-1") == "인후동2가1575-1"
    assert lot_key("전주시완산구 효자동2가 368번지") == "효자동2가368"
    assert lot_key("서울특별시청") is None
    exact = NaverAddress("", "전북 전주시 덕진구 인후동2가 1575-1", NAVER_POINT)
    longer = NaverAddress("", "전북 전주시 덕진구 인후동2가 1575-10", NAVER_POINT)
    assert keeps_lot(ADDRESS, exact)
    assert not keeps_lot(ADDRESS, longer)


@pytest.mark.asyncio
async def test_search_candidates_reports_fallback_notice() -> None:
    naver, _ = naver_client(naver_ok())
    candidates, notice = await search_address_candidates(
        ADDRESS, kakao=FakeKakao(error=quota_error()), naver=naver, vworld=FakeVWorld()
    )

    assert [c.source for c in candidates] == ["naver"]
    assert candidates[0].coordinates == NAVER_POINT
    assert "네이버" in notice and "카카오" in notice


def _api_client(monkeypatch, kakao, naver, vworld) -> TestClient:
    settings = Settings(_env_file=None, demo_mode=False, kakao_rest_api_key="k")
    main.app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(main, "geocode_clients", lambda config: (kakao, naver, vworld))
    return TestClient(main.app)


def test_api_geocode_falls_back_to_naver_when_kakao_quota_is_exceeded(monkeypatch) -> None:
    naver, _ = naver_client(naver_ok())
    try:
        client = _api_client(monkeypatch, FakeKakao(error=quota_error()), naver, FakeVWorld())
        response = client.get("/api/geocode", params={"query": ADDRESS})
    finally:
        main.app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200
    body = response.json()
    assert body["candidates"][0]["source"] == "naver"
    assert body["candidates"][0]["coordinates"] == {"lat": NAVER_POINT.lat, "lng": NAVER_POINT.lng}
    assert body["notice"]


def test_api_geocode_kakao_hit_needs_no_fallback(monkeypatch) -> None:
    hit = GeocodeCandidate(id="a", name="n", address=ADDRESS, coordinates=NAVER_POINT)
    naver, seen = naver_client(naver_ok())
    try:
        client = _api_client(monkeypatch, FakeKakao(found=[hit]), naver, FakeVWorld())
        response = client.get("/api/geocode", params={"query": ADDRESS})
    finally:
        main.app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 200
    assert response.json()["candidates"][0]["source"] == "kakao"
    assert response.json()["notice"] == ""
    assert seen == []


def test_api_geocode_reports_quota_when_every_provider_fails(monkeypatch) -> None:
    naver, _ = naver_client(naver_auth_failed)
    try:
        client = _api_client(
            monkeypatch, FakeKakao(error=quota_error()), naver,
            FakeVWorld(error=httpx.ConnectError("down")),
        )
        response = client.get("/api/geocode", params={"query": ADDRESS})
    finally:
        main.app.dependency_overrides.pop(get_settings, None)

    assert response.status_code == 429
    assert "쿼터" in response.json()["detail"]


@pytest.mark.asyncio
async def test_factory_geocache_does_not_store_outage_as_not_found(tmp_path) -> None:
    from app.services.factory_registry import FactoryRegistryClient

    async def down(address: str):
        raise GeocodeUnavailable("원천 오류", [quota_error()])

    client = FactoryRegistryClient("k", geocoder=down, cache_path=tmp_path / "geo.json")
    assert await client._coordinates_for("전북 전주시 완산구 효자동2가 368") is None
    assert client.last_geocode_outages == 1
    assert client._load_geocache() == {}


def test_connections_show_naver_auth_failure_before_probe(monkeypatch) -> None:
    from app.settings_api import connections

    class Disabled:
        enabled = True
        disabled_reason = "Authentication Failed"

    spec = next(s for s in connections.CONNECTION_SPECS if s.id == "naver_geocode")
    monkeypatch.setattr(
        connections, "CONNECTION_SPECS",
        (connections.ConnectionSpec(spec.id, spec.label, spec.purpose, spec.key_name,
                                    lambda h, s: Disabled(), spec.probe),),
    )
    monkeypatch.setattr(connections, "_last_results", {})
    row = connections.build_connections(None, None, False).connections[0]
    assert row.label == "네이버 지오코딩(NCP)"
    assert row.state == "failed"
    assert "인증 실패" in row.detail


def test_provider_status_reports_kakao_limit_and_probes_rarely(monkeypatch) -> None:
    # 카카오 한도 초과면 화면이 안내를 띄우고 지도를 네이버로 돌린다. 확인은 10분에 한 번만.
    from fastapi.testclient import TestClient

    import app.main as main_module
    from app.services import address_geocoder
    from app.services.kakao import KakaoAPIError

    address_geocoder.reset_outages()
    monkeypatch.setitem(main_module._kakao_probe, "at", float("-inf"))
    calls: list[str] = []

    async def over_limit(self, query: str):
        calls.append(query)
        raise KakaoAPIError("API limit has been exceeded.", 400)

    monkeypatch.setattr(main_module.KakaoClient, "geocode", over_limit)
    monkeypatch.setattr(main_module.KakaoClient, "enabled", property(lambda self: True))
    client = TestClient(main_module.app)
    body = client.get("/api/status/providers").json()
    assert body["kakao_limited"] is True
    assert "네이버 지도" in body["notice"]
    client.get("/api/status/providers")
    assert len(calls) == 1
    address_geocoder.reset_outages()
