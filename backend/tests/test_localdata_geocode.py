"""인허가 원장에서 좌표가 빈 영업 중 행을 주소 지오코딩으로 살린다."""

from __future__ import annotations

import httpx
import pytest

from app.models import Coordinates
from app.services.localdata import DATASET_BY_KEY, LocalDataClient

ROWS = [
    {"MNG_NO": "1", "BPLC_NM": "좌표있는점포", "SALS_STTS_CD": "01", "SALS_STTS_NM": "영업",
     "CRD_INFO_X": "210000", "CRD_INFO_Y": "380000", "LOTNO_ADDR": "전북 전주시 A동 1"},
    {"MNG_NO": "2", "BPLC_NM": "성락시장", "SALS_STTS_CD": "01", "SALS_STTS_NM": "영업",
     "CRD_INFO_X": "", "CRD_INFO_Y": "", "LOTNO_ADDR": "전북특별자치도 전주시 덕진구 인후동2가 1575-1"},
    {"MNG_NO": "3", "BPLC_NM": "폐업점포", "SALS_STTS_CD": "03", "SALS_STTS_NM": "폐업",
     "CRD_INFO_X": "", "CRD_INFO_Y": "", "LOTNO_ADDR": "전북 전주시 B동 2"},
]


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        size = int(request.url.params["numOfRows"])
        items = ROWS[:1] if size == 1 else ROWS
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"},
                              "body": {"totalCount": len(ROWS), "items": {"item": items}}}})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_rows_without_coordinates_are_geocoded_and_cached(tmp_path) -> None:
    asked: list[str] = []

    async def geocode(address: str) -> Coordinates | None:
        asked.append(address)
        return Coordinates(lat=35.84, lng=127.14)

    cache = tmp_path / "cache.json"
    client = LocalDataClient("k", transport=_transport(), geocode=geocode, geocode_cache_path=cache)
    records = await client.fetch_all(DATASET_BY_KEY["large_scale_retail_stores"])

    names = {r.name: r for r in records}
    assert set(names) == {"좌표있는점포", "성락시장"}  # 폐업은 여전히 뺀다
    assert names["성락시장"].extra["coord_source"].startswith("주소 지오코딩")
    assert asked == ["전북특별자치도 전주시 덕진구 인후동2가 1575-1"]

    # 두 번째 동기화는 캐시를 쓰고 다시 묻지 않는다.
    again = LocalDataClient("k", transport=_transport(), geocode=geocode, geocode_cache_path=cache)
    await again.fetch_all(DATASET_BY_KEY["large_scale_retail_stores"])
    assert len(asked) == 1


@pytest.mark.asyncio
async def test_without_geocoder_rows_without_coordinates_are_dropped(tmp_path) -> None:
    client = LocalDataClient("k", transport=_transport(), geocode_cache_path=tmp_path / "c.json")
    records = await client.fetch_all(DATASET_BY_KEY["large_scale_retail_stores"])
    assert [r.name for r in records] == ["좌표있는점포"]
