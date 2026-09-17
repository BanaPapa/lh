"""가스안전공사 LPG 충전소 현황 파일(ODcloud 15001643) 어댑터 검증."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.models import Coordinates
from app.services.kgs import PublicDataAPIError
from app.services.lpg_station_file import LpgStationFileClient, station_from_row

# 최신본(_20251127) 실측 행. 전화번호 칸에 관리구분이 밀려 들어와 있어도 판정에는 안 쓴다.
ROW = {
    "경도": "128.8880731", "관리구분": "자동차", "순번": 1, "업소명": "SK동해충전소",
    "위도": "37.7674347", "전화번호": "용기, 자동차, 13KG용기",
    "주소": "강원특별자치도 강릉시 율곡로 3054 (교동)", "행정구역": "강원 강릉시",
}


def test_row_becomes_station_with_usage() -> None:
    station, reason = station_from_row(ROW)
    assert reason == "" and station is not None
    assert station.name == "SK동해충전소" and station.usage == "자동차"
    assert station.region == "강원 강릉시"


def test_row_with_province_mismatch_is_quarantined() -> None:
    station, reason = station_from_row(dict(ROW, 위도="35.1", 경도="126.9"))
    assert station is None and "범위 밖" in reason


def _transport(rows, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"page": 1, "perPage": 1000, "totalCount": len(rows), "currentCount": len(rows), "data": rows}
        return httpx.Response(status, content=json.dumps(payload, ensure_ascii=False).encode())

    return httpx.MockTransport(handler)


def test_client_filters_by_radius() -> None:
    client = LpgStationFileClient("key", transport=_transport([ROW]))
    near = asyncio.run(client.stations_around(Coordinates(lat=37.7674, lng=128.888), 300))
    far = asyncio.run(client.stations_around(Coordinates(lat=37.5, lng=127.0), 300))
    assert len(near) == 1 and far == []


def test_unauthorised_raises_public_data_error() -> None:
    client = LpgStationFileClient("key", transport=_transport([], status=401))
    with pytest.raises(PublicDataAPIError):
        asyncio.run(client.all_stations())
