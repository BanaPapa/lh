"""전국전통시장표준데이터 — 2차 상업시설의 전통시장 원천(소상공인시장진흥공단).

LH 최종보고서의 2차 「상업시설」은 대규모점포 + 전통시장이다(전북 138곳). 내부망 앱
표준 데이터셋은 전통시장을 소상공인시장진흥공단 「전국 전통시장 현황」(42_traditional_
markets.xlsx, 전북 59곳)에서 받아 시장 지번주소로 PNU 를 만들고(location_basis=PNU)
사업지 대지경계 ↔ 그 필지 경계로 잰다. 이 앱은 대규모점포(행안부 localdata)만 세고
있어 전통시장이 통째로 빠졌다. 같은 원장의 공공 API 판으로 채운다.

실호출로 확정한 사실(2026-09-30, 활용신청 승인 직후):
- 경로: `https://api.data.go.kr/openapi/tn_pubr_public_trdit_mrkt_api`, `type=json`.
  응답은 `{"header","body"}` 로 `response` 감싸개가 없다.
- 전국 1,393곳(기준일 2025-11-10), 전북 57곳. 한 페이지 1,000건이 0.3초라 전량을
  받아 하루 캐시하고 좌표로 거른다. 반경·지역 필터 파라미터는 없다.
- `mrktNm`(시장명), `mrktType`(상설장·5일장·상설장+5일장 …), `mrktEstblCycle`,
  `lnmadr`(지번)·`rdnmadr`(도로명), `latitude`·`longitude`(WGS84).
- 운영 상태 필드는 없다(표준데이터는 현재 개설된 시장만 싣는다). 좌표가 빈 곳이
  16곳 있다(전북은 익산 함열시장 1곳) — 좌표 없이는 반경을 거를 수 없어 뺀다.
- 전북 주소가 「전북특별차치도」로 오타가 나 있다. 주소 필지 조회(카카오 주소검색)가
  빗나가지 않게 「전북특별자치도」로 고쳐 쓴다.

서버 사본(2026-10-01): 받아 둔 전량 목록을 파일(traditional_markets_cache.json)로 실어 서버가
켜지자마자 쓰고, 하루가 지났으면 뒤에서 새로 받는다(snapshot_store 참고).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.snapshot_store import DATA_DIR, SnapshotList, trim_rows


TRADITIONAL_MARKET_URL = "https://api.data.go.kr/openapi/tn_pubr_public_trdit_mrkt_api"

PAGE_SIZE = 1000
MAX_PAGES = 10
CACHE_TTL_SECONDS = 24 * 60 * 60

# 전량 목록 사본(앱 배선만 이 경로를 준다)과 사본에 남기는 열(판정이 읽는 것만).
DEFAULT_SNAPSHOT_PATH = DATA_DIR / "traditional_markets_cache.json"
SNAPSHOT_KEY = "markets"
SNAPSHOT_COLUMNS = ("mrktNm", "mrktType", "lnmadr", "rdnmadr", "latitude", "longitude")

# 대규모점포와 같은 자리로 보는 거리. 시장 건물이 대규모점포(「그 밖의 대규모점포」·
# 「시장」)로도 인허가된 곳은 한 시설을 두 번 세지 않는다. 다른 시설군의 「같은 자리
# 40m」(소방서·환승센터 보충)와 같은 기준이다.
MARKET_DUPLICATE_M = 40.0

MARKET_SOURCE = "소상공인시장진흥공단 전국전통시장표준데이터"
# 전통시장은 LH앱처럼 시장 지번주소 필지 경계로 잰다. 시설 고지 앞머리에 붙인다.
MARKET_ADDRESS_PARCEL_NOTICE = "시장 지번주소 필지"

# 원천 오타 교정(주소 필지 조회용). 원문은 표시에도 고쳐 쓴다.
_ADDRESS_TYPOS: tuple[tuple[str, str], ...] = (("전북특별차치도", "전북특별자치도"),)


class TraditionalMarketAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MarketRecord(NamedTuple):
    name: str
    kind: str          # 상설장·5일장·상설장+5일장 …
    address: str       # 지번주소 우선(LH앱이 지번주소로 필지를 만든다)
    road_address: str
    coordinates: Coordinates


def normalize_market_address(address: str) -> str:
    text = (address or "").strip()
    for wrong, right in _ADDRESS_TYPOS:
        text = text.replace(wrong, right)
    return text


def _market(row: dict[str, Any]) -> MarketRecord | None:
    name = str(row.get("mrktNm") or "").strip()
    if not name:
        return None
    try:
        coordinates = Coordinates(lat=float(row["latitude"]), lng=float(row["longitude"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not (33.0 <= coordinates.lat <= 39.5 and 124.0 <= coordinates.lng <= 132.0):
        return None
    road = normalize_market_address(str(row.get("rdnmadr") or ""))
    return MarketRecord(
        name=name,
        kind=str(row.get("mrktType") or "").strip(),
        address=normalize_market_address(str(row.get("lnmadr") or "")) or road,
        road_address=road,
        coordinates=coordinates,
    )


def drop_duplicate_markets(
    markets: Iterable[MarketRecord],
    store_points: Sequence[Coordinates],
    within_m: float = MARKET_DUPLICATE_M,
) -> list[MarketRecord]:
    """대규모점포와 같은 자리(within_m)인 시장, 같은 이름·같은 자리로 거듭 실린 시장을 뺀다.

    대규모점포는 인허가 원장이라 그쪽을 남긴다. 표준데이터에는 같은 시장이 상설장과
    5일장으로 두 줄 실린 곳이 있어 이름·자리가 같으면 하나로 센다.
    """

    kept: list[MarketRecord] = []
    for market in markets:
        if any(
            haversine_meters(market.coordinates, point) <= within_m for point in store_points
        ):
            continue
        if any(
            other.name == market.name
            and haversine_meters(other.coordinates, market.coordinates) <= within_m
            for other in kept
        ):
            continue
        kept.append(market)
    return kept


def markets_from_rows(rows: list[Any]) -> list[MarketRecord]:
    """표준데이터 행 목록 → 좌표 있는 시장(API 행·사본 행 공통)."""

    return [
        record
        for record in (_market(row) for row in rows if isinstance(row, dict))
        if record is not None
    ]


class TraditionalMarketClient:
    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        snapshot_path: Path | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        # 전량 목록(메모리 하루 캐시 + 서버 사본). 동시에 여러 심사가 처음 불러도 한 번만 받는다.
        self._list: SnapshotList[MarketRecord] = SnapshotList(
            snapshot_path, SNAPSHOT_KEY, "전국전통시장표준데이터",
            fetch=self._fetch_rows, parse=markets_from_rows, error=TraditionalMarketAPIError,
            empty_message="전통시장 표준데이터가 비어 있습니다.",
        )

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    @property
    def snapshot_notice(self) -> str:
        """받은 지 하루 넘은 목록(서버 사본)으로 답하고 있으면 그 고지. 아니면 빈 문자열."""

        return self._list.notice

    @property
    def data_as_of(self) -> datetime | None:
        return self._list.as_of

    async def markets_around(
        self, center: Coordinates, radius_m: float
    ) -> list[MarketRecord]:
        """반경 안의 전통시장. 실패는 예외로 올린다(0곳과 구분)."""

        markets = await self._all_markets()
        return [
            market
            for market in markets
            if haversine_meters(center, market.coordinates) <= radius_m
        ]

    async def _all_markets(self) -> list[MarketRecord]:
        """전국 목록. 메모리 → 서버 사본(하루 넘었으면 뒤에서 갱신) → API 순."""

        return await self._list.get()

    async def refresh(self, force: bool = False) -> list[MarketRecord]:
        """전국 목록을 API 에서 새로 받아 메모리와 서버 사본을 갈아 끼운다."""

        return await self._list.refresh(force)

    async def _fetch_rows(self, _previous: list[MarketRecord]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=True,
            verify=shared_verify(),
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                page_rows, total = await self._page(client, page)
                rows.extend(page_rows)
                if len(page_rows) < PAGE_SIZE or page * PAGE_SIZE >= total:
                    break
        return trim_rows(rows, SNAPSHOT_COLUMNS)

    async def _page(
        self, client: httpx.AsyncClient, page: int
    ) -> tuple[list[dict[str, Any]], int]:
        response = await client.get(
            TRADITIONAL_MARKET_URL,
            params={
                "serviceKey": self.service_key,
                "type": "json",
                "numOfRows": str(PAGE_SIZE),
                "pageNo": str(page),
            },
        )
        if response.status_code != 200:
            raise TraditionalMarketAPIError(
                f"전통시장 표준데이터 HTTP {response.status_code}", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TraditionalMarketAPIError("전통시장 표준데이터 응답을 읽지 못했습니다.") from exc
        root = payload.get("response") or payload
        header = root.get("header") or {}
        code = str(header.get("resultCode", "00"))
        if code not in ("00", "0"):
            raise TraditionalMarketAPIError(
                f"전통시장 표준데이터 오류 {code}: {header.get('resultMsg', '')}"
            )
        body = root.get("body") or {}
        items = body.get("items")
        if isinstance(items, dict):
            items = items.get("item")
        rows = items if isinstance(items, list) else ([items] if isinstance(items, dict) else [])
        return [row for row in rows if isinstance(row, dict)], int(body.get("totalCount") or 0)


# 전통시장 원천이 응답하지 않았을 때(심사 결과 상단 경고). 조용히 대규모점포만 세지 않는다.
MARKET_OUTAGE_ALERT = (
    "전국전통시장표준데이터가 응답하지 않아 상업시설을 대규모점포만으로 셌습니다. "
    "주변 전통시장이 빠졌을 수 있으니 잠시 뒤 다시 심사해 주십시오."
)
# 전통시장을 반영하지 못한 상업시설 고지(키 미설정·원천 장애).
RETAIL_STORES_ONLY_NOTE = (
    "행정안전부 대규모점포 인허가 원장으로 산정했습니다. "
    "전통시장(소상공인시장진흥공단 전국전통시장표준데이터)은 이번 심사에서 받지 못해 "
    "대규모점포만 반영했습니다."
)
