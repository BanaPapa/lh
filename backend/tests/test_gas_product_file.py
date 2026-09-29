"""가스안전공사 가스제품 제조업소정보(ODcloud 15152505) — 업소 병합·지오코딩·활용신청 오류."""

from __future__ import annotations

import httpx
import pytest

from app.models import Coordinates
from app.services.gas_product_file import (
    GAS_PRODUCT_URL,
    GasProductFileClient,
    merge_rows,
)
from app.services.kgs import PublicDataAPIError

CENTER = Coordinates(lat=35.97, lng=126.62)


def row(name: str, address: str, product: str, status: str = "영업", law: str = "고법") -> dict:
    return {
        "행정구역": "전북 군산시", "업소명": name, "소재지": address,
        "생산품목": product, "영업상태": status, "법구분": law,
    }


ROWS = [
    row("(주)성현", "전북특별자치도 군산시 자유무역2길 31 (오식도동) ", "소형탱크"),
    row("(주)성현", "전북특별자치도 군산시 자유무역2길 31 (오식도동)", "압력용기"),
    row("(주)에쎈테크", "전북특별자치도 군산시 자유무역2길 15", "배관용밸브", law="액법"),
    row("(주)에쎈테크", "전북특별자치도 군산시 자유무역2길 15", "배관용밸브", law="고법"),
    row("문닫은공장", "전북특별자치도 군산시 어딘가 1", "압력용기", status="폐업"),
]


def test_merge_rows_folds_products_per_place_and_drops_closed() -> None:
    places = {p.name: p for p in merge_rows(ROWS)}
    assert set(places) == {"(주)성현", "(주)에쎈테크"}
    assert places["(주)성현"].products == ("소형탱크", "압력용기")
    assert places["(주)에쎈테크"].products == ("배관용밸브",)
    assert places["(주)에쎈테크"].laws == ("액법", "고법")


def transport(status: int, payload: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(GAS_PRODUCT_URL)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_client_geocodes_merged_places_and_filters_radius() -> None:
    asked: list[str] = []

    async def geocode(address: str) -> Coordinates | None:
        asked.append(address)
        return CENTER if "31" in address else Coordinates(lat=36.5, lng=127.5)

    client = GasProductFileClient(
        "key", geocode=geocode,
        transport=transport(200, {"data": ROWS, "totalCount": len(ROWS)}),
        bundled_path=None,  # 서버 내장 파일 대신 API 경로를 시험한다
    )
    rows = await client.manufacturers_around(CENTER, 500)
    assert [r.name for r in rows] == ["(주)성현"]
    assert rows[0].products == "소형탱크 · 압력용기"
    # 같은 업소는 한 번만 지오코딩한다.
    assert len(asked) == 2


@pytest.mark.asyncio
async def test_unregistered_key_raises_with_dataset_id() -> None:
    async def geocode(address: str) -> Coordinates | None:
        return CENTER

    client = GasProductFileClient(
        "key", geocode=geocode,
        transport=transport(401, {"code": -401, "msg": "유효하지 않은 인증키 입니다."}),
        bundled_path=None,
    )
    with pytest.raises(PublicDataAPIError, match="15152505"):
        await client.all_manufacturers()


@pytest.mark.asyncio
async def test_bundled_file_is_used_without_key_or_geocoding() -> None:
    # 공공데이터포털이 CSV 로만 주는 자료라 서버에 좌표까지 구워 실었다(기준일 2025-09-30).
    from app.services.gas_product_file import BUNDLED_GEOCODED_CSV

    client = GasProductFileClient("", geocode=None)
    assert client.has_bundle and client.enabled
    rows = await client.all_manufacturers()
    assert client.loaded_from == "bundle"
    # 번지 없는 동 중심점 좌표는 쓰지 않아 767곳 중 745곳만 확정(22곳 주소로 못 찾음).
    assert len(rows) == 745
    assert sum("전북" in r.address for r in rows) == 18
    assert BUNDLED_GEOCODED_CSV.name.endswith("20250930.geocoded.csv")
