"""전국도시공원정보표준데이터 — 도시공원(생활권·주제공원) 위치.

2차 주거여건 「공원」의 1순위 원천이다. LH 내부망 앱의 생활권공원(JB_54_NEIGHBORHOOD_PARKS,
전북 653곳)이 이 표준데이터를 그대로 받은 것이라 공원 이름·지번이 LH 결과와 같다.
카카오 「공원」 검색은 「금암어린이공원」처럼 다른 이름으로 오거나 작은 공원이 빠졌다
(2026-09-30 전주 금암동: LH 「금암공원」).

LH 는 공원구분(어린이·근린·소공원·문화·수변·역사·체육·묘지·기타)을 가리지 않고 모두
센다. 여기서도 거르지 않는다.

실호출로 확정한 사실(2026-09-30):
- 경로: `https://api.data.go.kr/openapi/tn_pubr_public_cty_park_info_api`, `type=json`.
- 전국 18,295곳. 반경 파라미터가 없어 전량(1,000건 × 19쪽)을 받아 하루 캐시하고 좌표로 거른다.
- `manageNo`, `parkNm`, `parkSe`(공원구분), `lnmadr`(지번주소), `rdnmadr`(도로명주소),
  `latitude`·`longitude`(WGS84), `parkAr`(㎡).

서버 사본(2026-10-01): 배포 서버가 켜진 직후 첫 심사에서 전량 19쪽을 받다가(동시 예열로
붐빌 때 27초) 시간이 넘어 카카오 대체 + 원천 경고가 떴다. 받아 둔 전량 목록을 파일
(city_parks_cache.json)로 실어 곧바로 쓰고, 하루가 지났으면 뒤에서 새로 받는다
(snapshot_store 참고).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.snapshot_store import DATA_DIR, SnapshotList, trim_rows


CITY_PARK_URL = "https://api.data.go.kr/openapi/tn_pubr_public_cty_park_info_api"

PAGE_SIZE = 1000
MAX_PAGES = 40
CACHE_TTL_SECONDS = 24 * 60 * 60

# 전량 목록 사본(앱 배선만 이 경로를 준다)과 사본에 남기는 열(판정이 읽는 것만).
DEFAULT_SNAPSHOT_PATH = DATA_DIR / "city_parks_cache.json"
SNAPSHOT_KEY = "parks"
SNAPSHOT_COLUMNS = (
    "manageNo", "parkNm", "parkSe", "lnmadr", "rdnmadr", "latitude", "longitude", "parkAr",
)


class CityParkAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ParkRecord(NamedTuple):
    manage_no: str
    name: str
    kind: str          # 공원구분(어린이공원·근린공원·소공원 …)
    address: str       # 지번주소 우선(LH앱은 지번주소로 PNU 를 만든다)
    coordinates: Coordinates
    area_m2: float = 0.0


def _park(row: dict[str, Any]) -> ParkRecord | None:
    try:
        coordinates = Coordinates(lat=float(row["latitude"]), lng=float(row["longitude"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not (33.0 <= coordinates.lat <= 39.5 and 124.0 <= coordinates.lng <= 132.0):
        return None
    name = str(row.get("parkNm") or "").strip()
    if not name:
        return None
    try:
        area = float(row.get("parkAr") or 0)
    except (TypeError, ValueError):
        area = 0.0
    return ParkRecord(
        manage_no=str(row.get("manageNo") or "").strip(),
        name=name,
        kind=str(row.get("parkSe") or "").strip(),
        address=str(row.get("lnmadr") or row.get("rdnmadr") or "").strip(),
        coordinates=coordinates,
        area_m2=area,
    )


def parks_from_rows(rows: list[Any]) -> list[ParkRecord]:
    """표준데이터 행 목록 → 공원 목록(API 행·사본 행 공통)."""

    parks: list[ParkRecord] = []
    seen: set[str] = set()
    for row in rows:
        record = _park(row) if isinstance(row, dict) else None
        if record is None:
            continue
        # 쪽 경계에서 같은 공원이 두 번 오는 일을 막는다.
        signature = record.manage_no or f"{record.name}|{record.address}"
        if signature in seen:
            continue
        seen.add(signature)
        parks.append(record)
    return parks


class CityParkClient:
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
        self._list: SnapshotList[ParkRecord] = SnapshotList(
            snapshot_path, SNAPSHOT_KEY, "전국도시공원정보표준데이터",
            fetch=self._fetch_rows, parse=parks_from_rows, error=CityParkAPIError,
            empty_message="도시공원정보 표준데이터가 비어 있습니다.",
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

    async def parks_around(self, center: Coordinates, radius_m: float) -> list[ParkRecord]:
        """반경 안의 도시공원. 실패는 예외로 올린다(0곳과 구분)."""

        parks = await self._all_parks()
        return [
            park for park in parks if haversine_meters(center, park.coordinates) <= radius_m
        ]

    async def _all_parks(self) -> list[ParkRecord]:
        """전국 목록. 메모리 → 서버 사본(하루 넘었으면 뒤에서 갱신) → API 순."""

        return await self._list.get()

    async def refresh(self, force: bool = False) -> list[ParkRecord]:
        """전국 목록을 API 에서 새로 받아 메모리와 서버 사본을 갈아 끼운다."""

        return await self._list.refresh(force)

    async def _fetch_rows(self, _previous: list[ParkRecord]) -> list[dict[str, Any]]:
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
            CITY_PARK_URL,
            params={
                "serviceKey": self.service_key,
                "type": "json",
                "numOfRows": str(PAGE_SIZE),
                "pageNo": str(page),
            },
        )
        if response.status_code != 200:
            raise CityParkAPIError(
                f"도시공원정보 표준데이터 HTTP {response.status_code}", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CityParkAPIError("도시공원정보 표준데이터 응답을 읽지 못했습니다.") from exc
        header = (payload.get("response") or payload).get("header") or {}
        code = str(header.get("resultCode", "00"))
        if code not in ("00", "0"):
            raise CityParkAPIError(
                f"도시공원정보 표준데이터 오류 {code}: {header.get('resultMsg', '')}"
            )
        body = (payload.get("response") or payload).get("body") or {}
        items = body.get("items")
        if isinstance(items, dict):
            items = items.get("item")
        rows = items if isinstance(items, list) else ([items] if isinstance(items, dict) else [])
        return [row for row in rows if isinstance(row, dict)], int(body.get("totalCount") or 0)
