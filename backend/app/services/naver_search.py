from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.models import Coordinates
from app.services.http_client import shared_verify


NAVER_SEARCH_BASE = "https://openapi.naver.com"


class NaverSearchError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class NaverSearchItem:
    title: str
    link: str
    description: str
    source_type: str
    published_at: datetime | None = None


def clean_search_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", value or "")
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


@dataclass(frozen=True)
class NaverLocalPlace:
    """네이버 지역검색 결과 한 곳.

    좌표(mapx·mapy)는 경위도를 10^7 배한 정수로 온다(2026-09-08 실호출 확인:
    원광대학교정문 mapx=1269533063 → 경도 126.9533063). TM128 이 아니므로
    좌표계 변환 없이 10^7 로 나누기만 하면 WGS84 가 된다.
    """

    name: str
    category: str
    address: str
    road_address: str
    coordinates: Coordinates


class NaverSearchClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        timeout: float = 12.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
        }

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise NaverSearchError("네이버 검색 API 자격정보가 설정되지 않았습니다.")
        async with httpx.AsyncClient(
            base_url=NAVER_SEARCH_BASE,
            headers=self.headers,
            timeout=self.timeout,
            verify=shared_verify(),
        ) as client:
            response = await client.get(path, params=params)
        if response.status_code != 200:
            raise NaverSearchError(
                f"네이버 검색 API 요청에 실패했습니다. ({response.status_code})",
                response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise NaverSearchError("네이버 검색 API 응답을 읽지 못했습니다.") from exc

    async def web(self, query: str, display: int = 10) -> list[NaverSearchItem]:
        payload = await self._get(
            "/v1/search/webkr.json",
            {"query": query, "display": max(1, min(display, 100)), "start": 1},
        )
        return [
            NaverSearchItem(
                title=clean_search_text(str(item.get("title") or "")),
                link=str(item.get("link") or ""),
                description=clean_search_text(str(item.get("description") or "")),
                source_type="web",
            )
            for item in payload.get("items", [])
            if item.get("title") and item.get("link")
        ]

    async def local(self, query: str, display: int = 5) -> list[NaverLocalPlace]:
        """지역검색. 대학 정문·역 출구처럼 「지점」을 찾을 때 쓴다.

        표준 데이터셋이 대학 정문·철도역 출구 좌표를 이 API 로 확보했다
        (2026-09-08 회신). 같은 원천을 쓰면 기준점이 어긋나지 않는다.
        display 는 API 상한이 5 다.
        """

        payload = await self._get(
            "/v1/search/local.json",
            {"query": query, "display": max(1, min(display, 5)), "sort": "random"},
        )
        rows: list[NaverLocalPlace] = []
        for item in payload.get("items", []):
            try:
                lng = int(item["mapx"]) / 1e7
                lat = int(item["mapy"]) / 1e7
            except (KeyError, TypeError, ValueError):
                continue
            if not (33.0 <= lat <= 39.5 and 124.0 <= lng <= 132.0):
                continue  # 좌표계 오인·결측은 버린다
            rows.append(
                NaverLocalPlace(
                    name=clean_search_text(str(item.get("title") or "")),
                    category=str(item.get("category") or ""),
                    address=str(item.get("address") or ""),
                    road_address=str(item.get("roadAddress") or ""),
                    coordinates=Coordinates(lat=lat, lng=lng),
                )
            )
        return rows

    async def news(self, query: str, display: int = 10) -> list[NaverSearchItem]:
        payload = await self._get(
            "/v1/search/news.json",
            {
                "query": query,
                "display": max(1, min(display, 100)),
                "start": 1,
                "sort": "date",
            },
        )
        rows: list[NaverSearchItem] = []
        for item in payload.get("items", []):
            link = str(item.get("originallink") or item.get("link") or "")
            if not item.get("title") or not link:
                continue
            published_at: datetime | None = None
            try:
                published_at = parsedate_to_datetime(str(item.get("pubDate") or ""))
            except (TypeError, ValueError, OverflowError):
                pass
            rows.append(
                NaverSearchItem(
                    title=clean_search_text(str(item.get("title") or "")),
                    link=link,
                    description=clean_search_text(
                        str(item.get("description") or "")
                    ),
                    source_type="news",
                    published_at=published_at,
                )
            )
        return rows
