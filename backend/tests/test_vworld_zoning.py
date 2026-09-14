from __future__ import annotations

import asyncio

import httpx

from app.services.vworld import VWorldClient, ZoningInfo, classify_zoning


# ---------------------------------------------------------------------------
# classify_zoning — 순수 함수
# ---------------------------------------------------------------------------
class TestClassifyZoning:
    def test_residential_zones(self) -> None:
        for name in (
            "제1종전용주거지역",
            "제2종일반주거지역",
            "제3종일반주거지역",
            "준주거지역",
        ):
            assert classify_zoning(name) == "residential", name

    def test_non_residential_zones(self) -> None:
        for name in ("일반공업지역", "자연녹지지역", "일반상업지역", "계획관리지역"):
            assert classify_zoning(name) == "non_residential", name

    def test_empty_and_none_are_unknown(self) -> None:
        assert classify_zoning(None) == "unknown"
        assert classify_zoning("") == "unknown"
        assert classify_zoning("   ") == "unknown"


# ---------------------------------------------------------------------------
# zoning_at — 2D 데이터 API 점 조회 + 20m BOX 재조회 + 캐시
# ---------------------------------------------------------------------------
def _zoning_payload(*names: str) -> dict:
    return {
        "response": {
            "status": "OK",
            "result": {
                "featureCollection": {
                    "features": [
                        {"type": "Feature", "properties": {"uname": name}}
                        for name in names
                    ]
                }
            },
        }
    }


def _client(handler) -> VWorldClient:
    return VWorldClient(api_key="test-key", transport=httpx.MockTransport(handler))


class TestZoningAt:
    def test_point_returns_first_non_blank_uname(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            # 한 점에 용도지역 폴리곤이 겹쳐 오며 첫 feature 의 uname 이 공란이다.
            return httpx.Response(200, json=_zoning_payload("", "제2종일반주거지역"))

        client = _client(handler)
        info = asyncio.run(client.zoning_at(35.585262, 126.859595))
        assert info == ZoningInfo(name="제2종일반주거지역")
        assert calls["n"] == 1  # 점 조회 한 번으로 끝난다(BOX 재조회 없음)

    def test_blank_point_retries_with_box(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            geom = request.url.params.get("geomFilter", "")
            seen.append(geom.split("(")[0])
            if geom.startswith("POINT"):
                return httpx.Response(200, json=_zoning_payload(""))
            return httpx.Response(200, json=_zoning_payload("일반공업지역"))

        client = _client(handler)
        info = asyncio.run(client.zoning_at(35.949162, 126.974753))
        assert info == ZoningInfo(name="일반공업지역")
        assert seen == ["POINT", "BOX"]

    def test_unconfirmed_returns_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_zoning_payload(""))

        info = asyncio.run(_client(handler).zoning_at(35.0, 127.0))
        assert info is None

    def test_http_error_returns_none_not_raise(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        info = asyncio.run(_client(handler).zoning_at(35.0, 127.0))
        assert info is None

    def test_disabled_client_returns_none_without_call(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("키가 없으면 호출하지 않는다")

        client = VWorldClient(api_key="", transport=httpx.MockTransport(handler))
        assert asyncio.run(client.zoning_at(35.0, 127.0)) is None

    def test_cache_avoids_repeat_calls_for_same_coordinate(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=_zoning_payload("일반상업지역"))

        client = _client(handler)

        async def run() -> None:
            await client.zoning_at(35.12345678, 127.1)
            await client.zoning_at(35.12345679, 127.1)  # 6자리 반올림 시 동일 키

        asyncio.run(run())
        assert calls["n"] == 1
