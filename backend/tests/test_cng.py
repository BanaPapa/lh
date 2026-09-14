"""한국가스안전공사 전국 도시가스(CNG) 충전소 어댑터 — ODcloud 15001508.

2026-09-14 조사(docs/API_SOURCE_RESEARCH_2026-09-14 §1 #12)로 「CNG 는 공개 API 없음」이
틀렸음이 확인됐다. 이 어댑터는 그 자료를 전량 받아 캐시하고 반경으로 거른다.
좌표 위생 검사는 LPG(kgs.py)와 같은 규칙을 공유한다.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.models import Coordinates
from app.services.cng import (
    CNG_STATION_URL,
    CngStationClient,
    PublicDataAPIError,
    _station,
)


def row(name: str, addr: str, lat: str, lng: str, region: str = "전북") -> dict:
    return {
        "시설명": name, "주소": addr, "위도": lat, "경도": lng,
        "행정구역": region, "지사": "전북지역본부", "우편": "",
    }


def test_row_with_korean_columns_becomes_station() -> None:
    station = _station(row("전주CNG충전소", "전북 전주시 덕진구 1", "35.85", "127.13"))
    assert station is not None
    assert station.name == "전주CNG충전소"
    assert station.region == "전북"
    assert station.coordinates == Coordinates(lat=35.85, lng=127.13)


def test_row_with_alias_columns_is_accepted() -> None:
    # 파일 변환 API 는 갱신 시 열 이름이 바뀔 수 있어 흔한 별칭을 같이 받는다.
    station = _station({
        "충전소명": "별칭충전소", "소재지": "전북 익산시 1",
        "LAT": "35.94", "LOT": "126.95",
    })
    assert station is not None
    assert station.name == "별칭충전소"


def test_default_and_out_of_province_coordinates_are_quarantined() -> None:
    assert _station(row("기본값", "전북 완주군 1", "37.5665", "126.978")) is None
    assert _station(row("경상도좌표", "전북 김제시 1", "35.94", "128.90")) is None


def test_missing_coordinates_are_skipped_without_quarantine() -> None:
    client = CngStationClient("key")
    kept = client._ingest([
        row("정상", "전북 고창군 1", "35.43", "126.69"),
        row("좌표없음", "전북 고창군 2", "", ""),
        row("기본값", "전북 완주군 1", "37.5665", "126.978"),
    ])
    assert [s.name for s in kept] == ["정상"]
    assert [q.name for q in client.quarantined] == ["기본값"]


def _transport(pages: dict[int, list[dict]], total: int, status: int = 200):
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        assert str(request.url).startswith(CNG_STATION_URL)
        if status != 200:
            return httpx.Response(status, json={"code": -401, "msg": "유효하지 않은 인증키 입니다."})
        page = int(params["page"])
        data = pages.get(page, [])
        return httpx.Response(200, json={
            "page": page, "perPage": int(params["perPage"]),
            "currentCount": len(data), "totalCount": total, "data": data,
        })

    return httpx.MockTransport(handler), calls


def test_all_stations_pages_until_total_count() -> None:
    from app.services import cng as cng_module

    per_page = cng_module.PAGE_SIZE
    first = [row(f"충전소{i}", "전북 전주시 1", "35.85", "127.13") for i in range(per_page)]
    second = [row("마지막", "전북 군산시 1", "35.97", "126.71")]
    transport, calls = _transport({1: first, 2: second}, total=per_page + 1)
    client = CngStationClient("key", transport=transport)

    stations = asyncio.run(client.all_stations())

    assert len(stations) == per_page + 1
    assert [c["page"] for c in calls] == ["1", "2"]
    assert calls[0]["serviceKey"] == "key"


def test_stations_around_filters_by_distance_and_uses_cache() -> None:
    transport, calls = _transport(
        {1: [row("가까움", "전북 전주시 1", "35.8500", "127.1300"),
             row("멀리", "전북 군산시 1", "35.97", "126.71")]},
        total=2,
    )
    client = CngStationClient("key", transport=transport)
    center = Coordinates(lat=35.8501, lng=127.1301)

    near = asyncio.run(client.stations_around(center, 500))
    again = asyncio.run(client.stations_around(center, 500))

    assert [s.name for s in near] == ["가까움"]
    assert [s.name for s in again] == ["가까움"]
    assert len(calls) == 1  # 두 번째 호출은 캐시


def test_unauthorized_raises_public_data_error() -> None:
    transport, _ = _transport({}, total=0, status=401)
    client = CngStationClient("key", transport=transport)
    with pytest.raises(PublicDataAPIError) as excinfo:
        asyncio.run(client.all_stations())
    assert excinfo.value.status_code == 401


def test_auth_failure_is_not_retried_during_cooldown() -> None:
    # 활용신청 전 401 은 심사마다 같은 답이다. 쿨다운 동안 호출 없이 같은 예외를 올리고,
    # 쿨다운이 지나면 다시 실호출한다(승인 반영).
    from app.services import cng as cng_module

    transport, calls = _transport({}, total=0, status=401)
    client = CngStationClient("key", transport=transport)
    for _ in range(3):
        with pytest.raises(PublicDataAPIError) as excinfo:
            asyncio.run(client.all_stations())
        assert excinfo.value.status_code == 401
    assert len(calls) == 1

    client._auth_failed_at -= cng_module.AUTH_FAILURE_COOLDOWN_SECONDS + 1
    with pytest.raises(PublicDataAPIError):
        asyncio.run(client.all_stations())
    assert len(calls) == 2


def test_disabled_without_key() -> None:
    client = CngStationClient("")
    assert client.enabled is False
    assert asyncio.run(client.all_stations()) == []


def test_station_id_is_stable_for_same_row() -> None:
    a = _station(row("전주CNG충전소", "전북 전주시 1", "35.85", "127.13"))
    b = _station(json.loads(json.dumps(row("전주CNG충전소", "전북 전주시 1", "35.85", "127.13"))))
    assert a is not None and b is not None
    assert a.station_id == b.station_id
