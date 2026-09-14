from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from app.models import Coordinates, GeocodeCandidate, RegionInfo
from app.services.http_client import shared_verify


KAKAO_API_BASE = "https://dapi.kakao.com"


class KakaoAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class KakaoClient:
    def __init__(self, api_key: str, timeout: float = 10.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"KakaoAK {self.api_key}"}

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise KakaoAPIError("KAKAO_REST_API_KEY is not configured")

        async with httpx.AsyncClient(
            base_url=KAKAO_API_BASE,
            headers=self.headers,
            timeout=self.timeout,
            verify=shared_verify(),
        ) as client:
            response = await client.get(path, params=params)

        if response.status_code != 200:
            message = f"Kakao API request failed ({response.status_code})"
            try:
                detail = response.json()
                message = detail.get("msg") or detail.get("message") or message
            except ValueError:
                pass
            raise KakaoAPIError(message, response.status_code)

        return response.json()

    async def geocode(self, query: str) -> list[GeocodeCandidate]:
        address_task = self._get(
            "/v2/local/search/address.json",
            {"query": query, "size": 10},
        )
        keyword_task = self._get(
            "/v2/local/search/keyword.json",
            {"query": query, "size": 10},
        )
        results = await asyncio.gather(address_task, keyword_task, return_exceptions=True)

        candidates: list[GeocodeCandidate] = []
        seen: set[tuple[str, str]] = set()

        address_result = results[0]
        if isinstance(address_result, dict):
            for index, item in enumerate(address_result.get("documents", [])):
                x, y = item.get("x"), item.get("y")
                if not x or not y:
                    continue
                address = item.get("address_name", "")
                road = (item.get("road_address") or {}).get("address_name", "")
                key = (str(x), str(y))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    GeocodeCandidate(
                        id=f"address-{index}-{x}-{y}",
                        name=road or address or query,
                        address=address,
                        road_address=road,
                        coordinates=Coordinates(lat=float(y), lng=float(x)),
                    )
                )

        keyword_result = results[1]
        if not candidates and isinstance(keyword_result, dict):
            for item in keyword_result.get("documents", []):
                if not self._keyword_matches_query(query, item):
                    continue
                x, y = item.get("x"), item.get("y")
                if not x or not y:
                    continue
                key = (str(x), str(y))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(
                    GeocodeCandidate(
                        id=f"place-{item.get('id', x + y)}",
                        name=item.get("place_name") or query,
                        address=item.get("address_name", ""),
                        road_address=item.get("road_address_name", ""),
                        coordinates=Coordinates(lat=float(y), lng=float(x)),
                    )
                )

        if not candidates:
            errors = [item for item in results if isinstance(item, Exception)]
            if len(errors) == len(results):
                raise errors[0]

        return candidates[:8]

    @staticmethod
    def _keyword_matches_query(query: str, item: dict[str, Any]) -> bool:
        """Reject broad Kakao keyword hits that omit a meaningful query token."""

        tokens = [
            token
            for token in re.findall(r"[가-힣a-z0-9]+", query.lower())
            if len(token) >= 2 and not token.isdigit()
        ]
        if not tokens:
            return True
        haystack = " ".join(
            str(item.get(field) or "").lower()
            for field in (
                "place_name",
                "address_name",
                "road_address_name",
                "category_name",
            )
        ).replace(" ", "")
        return all(token.replace(" ", "") in haystack for token in tokens)

    async def search_category(
        self,
        code: str,
        lat: float,
        lng: float,
        radius_m: int,
        max_pages: int = 3,
    ) -> list[dict[str, Any]]:
        return await self._search_places(
            "/v2/local/search/category.json",
            {
                "category_group_code": code,
                "x": lng,
                "y": lat,
                "radius": radius_m,
                "sort": "distance",
            },
            max_pages,
        )

    async def search_keyword(
        self,
        keyword: str,
        lat: float,
        lng: float,
        radius_m: int,
        max_pages: int = 3,
    ) -> list[dict[str, Any]]:
        return await self._search_places(
            "/v2/local/search/keyword.json",
            {
                "query": keyword,
                "x": lng,
                "y": lat,
                "radius": radius_m,
                "sort": "distance",
            },
            max_pages,
        )

    async def _search_places(
        self,
        path: str,
        params: dict[str, Any],
        max_pages: int,
    ) -> list[dict[str, Any]]:
        documents: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            page_params = {**params, "page": page, "size": 15}
            payload = await self._get(path, page_params)
            documents.extend(payload.get("documents", []))
            if payload.get("meta", {}).get("is_end", True):
                break
        return documents

    async def address_documents(self, query: str) -> list[dict[str, Any]]:
        """주소검색 원본 문서. PNU 조립에 필요한 b_code 가 여기에만 들어 있다."""

        payload = await self._get(
            "/v2/local/search/address.json",
            {"query": query, "size": 5},
        )
        return payload.get("documents", [])

    async def coord_to_address(
        self,
        lat: float,
        lng: float,
    ) -> dict[str, Any] | None:
        """좌표 -> 지번 주소. 법정동코드는 주지 않으므로 legal_code 와 함께 쓴다."""

        payload = await self._get(
            "/v2/local/geo/coord2address.json",
            {"x": lng, "y": lat},
        )
        documents = payload.get("documents", [])
        if not documents:
            return None
        return documents[0].get("address")

    async def legal_code(self, lat: float, lng: float) -> str:
        """좌표가 속한 법정동코드 10자리."""

        return (await self.region_info(lat, lng)).legal_code

    async def region_info(self, lat: float, lng: float) -> RegionInfo:
        payload = await self._get(
            "/v2/local/geo/coord2regioncode.json",
            {"x": lng, "y": lat},
        )
        legal = next(
            (item for item in payload.get("documents", []) if item.get("region_type") == "B"),
            {},
        )
        administrative = next(
            (item for item in payload.get("documents", []) if item.get("region_type") == "H"),
            {},
        )
        return RegionInfo(
            legal_name=legal.get("address_name", ""),
            legal_code=legal.get("code", ""),
            administrative_name=administrative.get("address_name", ""),
            administrative_code=administrative.get("code", ""),
        )
