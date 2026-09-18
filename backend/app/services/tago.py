from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.services.http_client import shared_verify


TAGO_STOP_URL = (
    "https://apis.data.go.kr/1613000/BusSttnInfoInqireService/"
    "getCrdntPrxmtSttnList"
)
TAGO_STOP_ROUTES_URL = (
    "https://apis.data.go.kr/1613000/BusSttnInfoInqireService/"
    "getSttnThrghRouteList"
)
TAGO_ROUTE_STOPS_URL = (
    "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/"
    "getRouteAcctoThrghSttnList"
)
# 노선 기본정보 — 배차간격(intervaltime 평일 · intervalsattime 토 · intervalsuntime 일).
# 버스정류장 운행주기 15분 판정(app.screening.bus_headway)의 원천이다.
TAGO_ROUTE_INFO_URL = (
    "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/"
    "getRouteInfoIem"
)


class TagoAPIError(RuntimeError):
    pass


class TagoClient:
    """Official TAGO nearby-stop adapter.

    The provider operation documents a 500 m proximity search. Results are
    therefore marked as partial when the user's selected radius is larger.
    """

    def __init__(self, service_key: str, timeout: float = 10.0) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self.cache_ttl_seconds = 900
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    async def _request(
        self,
        url: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.enabled:
            raise TagoAPIError("TAGO_SERVICE_KEY is not configured")

        request_params = {
            "serviceKey": self.service_key,
            "_type": "json",
            **params,
        }
        payload: dict[str, Any] | None = None
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, verify=shared_verify()
                ) as client:
                    response = await client.get(url, params=request_params)

                if response.status_code != 200:
                    raise TagoAPIError(
                        f"TAGO request failed ({response.status_code})"
                    )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise TagoAPIError("TAGO returned a non-JSON response") from exc
                break
            except (httpx.RequestError, TagoAPIError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(0.35)

        if payload is None:
            if isinstance(last_error, TagoAPIError):
                raise last_error
            raise TagoAPIError("TAGO request failed after retry") from last_error

        header = payload.get("response", {}).get("header", {})
        if str(header.get("resultCode", "00")) not in {"00", "0"}:
            raise TagoAPIError(header.get("resultMsg") or "TAGO returned an error")
        return payload

    @staticmethod
    def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
        container = (
            payload.get("response", {})
            .get("body", {})
            .get("items", {})
        )
        if not isinstance(container, dict):
            return []
        items = container.get("item", [])
        if isinstance(items, dict):
            return [items]
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    async def nearby_stops(self, lat: float, lng: float) -> list[dict[str, Any]]:
        if not self.enabled:
            return []

        payload = await self._request(
            TAGO_STOP_URL,
            {
                "gpsLati": lat,
                "gpsLong": lng,
                "numOfRows": 100,
                "pageNo": 1,
            },
        )
        return self._items(payload)

    async def routes_through_stop(
        self,
        city_code: str,
        stop_id: str,
    ) -> list[dict[str, Any]]:
        cache_key = f"stop-routes:{city_code}:{stop_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        payload = await self._request(
            TAGO_STOP_ROUTES_URL,
            {
                "cityCode": city_code,
                "nodeid": stop_id,
                "numOfRows": 100,
                "pageNo": 1,
            },
        )
        rows = self._items(payload)
        self._cache_set(cache_key, rows)
        return rows

    async def route_stops(
        self,
        city_code: str,
        route_id: str,
    ) -> list[dict[str, Any]]:
        cache_key = f"route-stops:{city_code}:{route_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        first_page = await self._request(
            TAGO_ROUTE_STOPS_URL,
            {
                "cityCode": city_code,
                "routeId": route_id,
                "numOfRows": 200,
                "pageNo": 1,
            },
        )
        items = self._items(first_page)
        body = first_page.get("response", {}).get("body", {})
        total_count = int(body.get("totalCount") or len(items))
        page = 2
        while len(items) < total_count:
            payload = await self._request(
                TAGO_ROUTE_STOPS_URL,
                {
                    "cityCode": city_code,
                    "routeId": route_id,
                    "numOfRows": 200,
                    "pageNo": page,
                },
            )
            page_items = self._items(payload)
            if not page_items:
                break
            items.extend(page_items)
            page += 1
        self._cache_set(cache_key, items)
        return items

    async def route_info(
        self,
        city_code: str,
        route_id: str,
    ) -> dict[str, Any] | None:
        """노선 기본정보 한 건(배차간격 포함). 없으면 None."""

        cache_key = f"route-info:{city_code}:{route_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached[0] if cached else None
        payload = await self._request(
            TAGO_ROUTE_INFO_URL,
            {
                "cityCode": city_code,
                "routeId": route_id,
            },
        )
        rows = self._items(payload)
        self._cache_set(cache_key, rows[:1])
        return rows[0] if rows else None

    def _cache_get(self, key: str) -> list[dict[str, Any]] | None:
        cached = self._cache.get(key)
        if not cached:
            return None
        expires_at, rows = cached
        if expires_at <= time.monotonic():
            self._cache.pop(key, None)
            return None
        return [dict(row) for row in rows]

    def _cache_set(self, key: str, rows: list[dict[str, Any]]) -> None:
        self._cache[key] = (
            time.monotonic() + self.cache_ttl_seconds,
            [dict(row) for row in rows],
        )
