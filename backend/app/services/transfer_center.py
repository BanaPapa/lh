"""국토교통부 전국대중교통환승센터 표준데이터(15034541) — 2차 환승시설 지정 원천.

`https://api.data.go.kr/openapi/tn_pubr_public_pbtrnspt_rnsit_cnter_api` 는 환승센터명·
주소·위경도·환승대상교통시설·운영유무를 준다(2026-09-15 조사, 제공 기관 14곳이라 전국
커버리지는 아니다). 공공데이터포털 활용신청 전에는 403(등록되지 않은 서비스키)이라
호출부(amenities)가 지도 검색으로 근사하고 그 사실을 고지한다.

전량을 받아 24시간 캐시하고 반경으로 거른다(가스안전공사 LPG 어댑터와 같은 방식).
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import PublicDataAPIError
from app.services.single_flight import LoopSafeLock

TRANSFER_CENTER_URL = (
    "https://api.data.go.kr/openapi/tn_pubr_public_pbtrnspt_rnsit_cnter_api"
)
PAGE_SIZE = 1000
MAX_PAGES = 20
CACHE_TTL_SECONDS = 24 * 3600


class TransferCenter(NamedTuple):
    name: str
    address: str
    coordinates: Coordinates
    # 환승대상교통시설(버스·도시철도·철도·터미널 …). 표시용.
    facilities: str
    operating: bool


def _center(row: dict[str, Any]) -> TransferCenter | None:
    """표준데이터 행 → 환승센터. 좌표가 없거나 숫자가 아니면 None."""

    try:
        lat = float(str(row.get("latitude") or "").strip())
        lng = float(str(row.get("longitude") or "").strip())
    except ValueError:
        return None
    if not (33.0 <= lat <= 39.5 and 124.0 <= lng <= 132.0):
        return None
    name = str(row.get("trnsitlcCnterNm") or "").strip()
    if not name:
        return None
    return TransferCenter(
        name=name,
        address=str(row.get("rdnmadr") or row.get("lnmadr") or "").strip(),
        coordinates=Coordinates(lat=lat, lng=lng),
        facilities=str(row.get("trnsitlcFclty") or "").strip(),
        operating=str(row.get("operYn") or "Y").strip().upper() != "N",
    )


class TransferCenterClient:
    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self._cache: list[TransferCenter] = []
        self._cached_at = 0.0
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    async def centers_around(
        self, center: Coordinates, radius_m: float
    ) -> list[TransferCenter]:
        centers = await self.all_centers()
        return [
            c
            for c in centers
            if c.operating and haversine_meters(center, c.coordinates) <= radius_m
        ]

    async def all_centers(self) -> list[TransferCenter]:
        if not self.enabled:
            return []
        if self._cache and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS:
            return self._cache
        async with self._fill_lock.get():
            if self._cache and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS:
                return self._cache
            rows: list[dict[str, Any]] = []
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport, verify=shared_verify()
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    page_rows, total = await self._page(client, page)
                    rows.extend(page_rows)
                    if len(page_rows) < PAGE_SIZE or len(rows) >= total:
                        break
            centers = [c for c in (_center(row) for row in rows) if c is not None]
            self._cache = centers
            self._cached_at = time.monotonic()
            return centers

    async def _page(
        self, client: httpx.AsyncClient, page: int
    ) -> tuple[list[dict[str, Any]], int]:
        response = await client.get(
            TRANSFER_CENTER_URL,
            params={
                "serviceKey": self.service_key,
                "pageNo": page,
                "numOfRows": PAGE_SIZE,
                "type": "json",
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError(
                f"환승센터 응답을 읽지 못했습니다 (HTTP {response.status_code})"
            ) from exc
        if "OpenAPI_ServiceResponse" in payload:
            header = payload["OpenAPI_ServiceResponse"].get("cmmMsgHeader", {})
            raise PublicDataAPIError(
                f"환승센터 조회 실패 ({header.get('returnReasonCode')}) "
                f"{header.get('returnAuthMsg') or header.get('errMsg') or ''}".strip()
            )
        body = (payload.get("response") or {}).get("body") or {}
        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        total = int(body.get("totalCount") or 0)
        return list(items), total
