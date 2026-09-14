"""원천 캐시 스탬피드 회귀 테스트.

kgs·safemap·crematorium·noise_emission 네 원천은 전국 목록을 한 번에 받아
캐시한다. 잠금이 없으면 판정 여러 건이 동시에 빈 캐시를 보고 각자 전국 목록을
따로 긁는다(캐시 스탬피드). data.go.kr 이 스로틀하면 조회 실패가 「검토 필요」로
떨어져 118건 스냅샷이 통째로 오염된다(2026-08-30 실측 사고).

여기서는 실제 네트워크를 타지 않고, 「느린 가짜 조회를 동시에 N번 호출했을 때
원격 조회(1페이지 요청)가 정확히 1회인가」를 고정한다. 잠금이 빠지면 이 테스트가
N회를 관측해 즉시 실패한다.
"""

from __future__ import annotations

import asyncio

import httpx

from app.models import Coordinates
from app.services.crematorium import CrematoriumClient
from app.services.kgs import KgsLpgClient
from app.services.noise_emission import NoiseEmissionClient
from app.services.safemap import SafemapFuelClient


CONCURRENCY = 8

# 첫 조회가 끝나기 전에 나머지 동시 호출이 전부 잠금 앞에 줄 서도록 넉넉히 잡는다.
SLOW_SECONDS = 0.05


def _page1_counter() -> tuple[list[int], "callable"]:
    """1페이지 요청 횟수를 세는 카운터와, 느리게 응답하는 비동기 핸들러 팩토리.

    핸들러는 호출자가 넘긴 body 를 그대로 돌려준다. 1페이지(pageNo=1) 요청만 센다.
    """

    page1_calls: list[int] = []

    def make_handler(body_for):
        async def handler(request: httpx.Request) -> httpx.Response:
            page = request.url.params.get("pageNo")
            if page == "1":
                page1_calls.append(1)
            # 느린 원격을 흉내 내 동시 호출이 잠금 앞에 몰리게 한다.
            await asyncio.sleep(SLOW_SECONDS)
            return httpx.Response(200, json=body_for(page))

        return handler

    return page1_calls, make_handler


async def _hammer(coro_factory) -> None:
    """같은 클라이언트에 동시 N회 호출한다."""

    await asyncio.gather(*(coro_factory() for _ in range(CONCURRENCY)))


def test_kgs_all_stations_fetches_once_under_concurrency() -> None:
    page1_calls, make_handler = _page1_counter()

    def body_for(page: str) -> dict:
        return {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL"},
                "body": {
                    "totalCount": 1,  # 이 API 의 totalCount 는 페이지 수다.
                    "items": {
                        "item": [
                            {
                                "BSES_NM": "한빛충전소",
                                "ADDR": "서울특별시 강남구 역삼동 1",
                                "SECT_NM": "서울",
                                "LAT": "37.5",
                                "LOT": "127.0",
                            }
                        ]
                    },
                },
            }
        }

    handler = make_handler(body_for)
    client = KgsLpgClient(service_key="k", transport=httpx.MockTransport(handler))

    asyncio.run(_hammer(client.all_stations))

    assert len(page1_calls) == 1, f"동시 조회가 1회여야 하는데 {len(page1_calls)}회 발생"
    # 캐시가 실제로 채워졌는지도 확인한다.
    assert client._cache and client._cache[0].name == "한빛충전소"


def test_safemap_all_stations_fetches_once_under_concurrency() -> None:
    page1_calls, make_handler = _page1_counter()

    def body_for(page: str) -> dict:
        return {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {
                "totalCount": 1,
                "items": {
                    "item": [
                        {
                            "uni_cd": "A1",
                            "os_nm": "동남주유소",
                            "poll_div_co": "SOL",
                            "gpoll_div_co": "",
                            "lpg_yn": "N",
                            "x": "14108535",
                            "y": "4501386.0",
                        }
                    ]
                },
            },
        }

    handler = make_handler(body_for)
    client = SafemapFuelClient(service_key="k", transport=httpx.MockTransport(handler))

    asyncio.run(_hammer(client.all_stations))

    assert len(page1_calls) == 1, f"동시 조회가 1회여야 하는데 {len(page1_calls)}회 발생"
    assert client._warmed and client._cache


def test_crematorium_all_fetches_once_under_concurrency() -> None:
    page1_calls, make_handler = _page1_counter()

    def body_for(page: str) -> dict:
        return {
            "resultCode": "00",
            "resultMsg": "NORMAL SERVICE",
            "totalCount": 1,
            "items": [
                {
                    "fcltNm": "천안추모공원",
                    "addr": "충청남도 천안시 1",
                    "ctpv": "충청남도",
                    "sigungu": "천안시",
                    "gubun": "공설",
                    "brzCnt": "9",
                }
            ],
        }

    async def geocode(address: str) -> Coordinates:
        return Coordinates(lat=36.75, lng=127.10)

    handler = make_handler(body_for)
    client = CrematoriumClient(
        service_key="k", geocode=geocode, transport=httpx.MockTransport(handler)
    )

    asyncio.run(_hammer(client.all_crematoriums))

    assert len(page1_calls) == 1, f"동시 조회가 1회여야 하는데 {len(page1_calls)}회 발생"
    assert client._cache and client._cache[0].name == "천안추모공원"


def test_noise_emission_all_fetches_once_under_concurrency() -> None:
    page1_calls, make_handler = _page1_counter()

    def body_for(page: str) -> dict:
        return {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"},
                "body": {
                    "totalCount": 1,
                    "items": [
                        {
                            "fcltNm": "제일기계",
                            "ctpvNm": "전북특별자치도",
                            "sggNm": "군산시",
                            "lctnRoadNmAddr": "전북 군산시 서포리 78",
                            "lctnLotnoAddr": "전북 군산시 고봉리 161-7",
                            "lat": "35.919000",
                            "lot": "126.949000",
                            "noisVbrtSeNm": "소음",
                            "noisVbrtMainCn": "",
                        }
                    ],
                },
            }
        }

    handler = make_handler(body_for)
    client = NoiseEmissionClient(service_key="k", transport=httpx.MockTransport(handler))

    asyncio.run(_hammer(client.all_facilities))

    assert len(page1_calls) == 1, f"동시 조회가 1회여야 하는데 {len(page1_calls)}회 발생"
    assert client._warmed and client._cache


def test_failed_fetch_is_not_cached_and_retried() -> None:
    """조회 실패는 캐시하지 않는다. 다음 호출이 다시 시도할 수 있어야 한다."""

    attempts: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        # 첫 시도는 실패(500), 이후 성공.
        if len(attempts) == 1:
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={
                "response": {
                    "header": {"resultCode": "00"},
                    "body": {
                        "totalCount": 1,
                        "items": {
                            "item": [
                                {
                                    "BSES_NM": "재시도충전소",
                                    "ADDR": "서울 1",
                                    "SECT_NM": "서울",
                                    "LAT": "37.5",
                                    "LOT": "127.0",
                                }
                            ]
                        },
                    },
                }
            },
        )

    client = KgsLpgClient(service_key="k", transport=httpx.MockTransport(handler))

    async def scenario() -> None:
        # 첫 호출은 실패해야 하고 캐시를 남기지 않는다.
        try:
            await client.all_stations()
        except Exception:
            pass
        assert not client._cache
        # 다음 호출은 다시 시도해 성공한다.
        stations = await client.all_stations()
        assert stations and stations[0].name == "재시도충전소"

    asyncio.run(scenario())
