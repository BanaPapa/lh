"""생활안전지도(safemap) 오픈API2 — 주유시설(IF_0033) 밖의 시설 레이어들.

2026-09-17 카탈로그 실측(safemap.go.kr/opna/data): 같은 인증키로 레이어마다 따로
「데이터 사용신청」을 해야 하며, 승인 전에는 resultCode 30(등록되지 않은 서비스키)이
온다. 이 모듈은 레이어 목록과 공통 호출·페이징만 맡고, 판정 배선은 승인 뒤 응답
컬럼을 실측한 다음 시설군마다 붙인다(컬럼 이름이 레이어마다 다르다).

| 레이어 | 카탈로그 | 쓰임 |
|---|---|---|
| IF_0022 | 병의원(종합병원) · 국립중앙의료원 | 2차 의료시설 교차 대조 |
| IF_0031 | 관공서 · 행정안전부 | 2차 공공시설 지정 원천 후보 |
| IF_0034 | 학교(대학교) · 교육부 | 2차 대학교 후보 |
| IF_0035 | 학교(초·중·고·기타) · 교육부 | 2차 교육여건 지정 원천 후보 |
| IF_0037 | 유아시설 · 교육부 | 참고(배점 항목 아님) |
| IF_0040 | 유해화학시설-환경배출시설 · 환경부 | 1차 공장 대기·환경배출 주석 |
| IF_0049 | 유해화학시설-화학물취급시설 · 환경부 | 1차 마목 유독물 참고 핀 |
| IF_0038 | 소방시설(소방서·119안전센터) · 소방청 | 2차 공공시설 보강 |
| IF_0051 | 유해화학시설-폐기물처리시설 · 환경부 | 1차 차목 참고 핀(협의 대상) |
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple

import httpx

from app.services.http_client import shared_verify
from app.services.safemap import SafemapAPIError
from app.services.single_flight import LoopSafeLock

SAFEMAP_LAYER_URL = "http://www.safemap.go.kr/openapi2/{layer}"
PAGE_SIZE = 1000
MAX_PAGES = 200
CACHE_TTL_SECONDS = 24 * 3600


class SafemapLayer(NamedTuple):
    layer_id: str
    label: str
    agency: str
    purpose: str


SAFEMAP_LAYERS: tuple[SafemapLayer, ...] = (
    SafemapLayer("IF_0022", "병의원(종합병원)", "국립중앙의료원", "2차 의료시설 — 국립중앙의료원 시도 조회의 전국본 폴백(381곳)"),
    SafemapLayer("IF_0031", "관공서", "행정안전부", "2차 공공시설 지정 원천 — 관공서·행정복지센터·우체국·도서관(9,679곳)"),
    SafemapLayer("IF_0034", "학교(대학교)", "교육부", "2차 대학교 후보 보강(1,621곳 · 정문은 카카오·네이버 검색)"),
    SafemapLayer("IF_0035", "학교(초·중·고·기타)", "교육부", "2차 교육여건 초·중·고 지정 원천(12,215곳)"),
    SafemapLayer("IF_0037", "유아시설(유치원·어린이집)", "교육부", "참고 — 배점 항목 아님(미배선)"),
    SafemapLayer("IF_0038", "소방시설(소방서·119안전센터)", "소방청", "2차 공공시설 보강 — 소방서·119안전센터(2,136곳, 관공서 레이어와 40m 중복 제거)"),
    SafemapLayer("IF_0040", "유해화학시설-환경배출시설", "환경부", "1차 등록공장 대기·수질 배출 주석(좌표 보유 47,101곳 · 판정 아님)"),
    SafemapLayer("IF_0049", "유해화학시설-화학물취급시설", "환경부", "1차 마목 유독물 참고 핀(3,820곳 · 판정은 수기 확인 유지)"),
    SafemapLayer("IF_0051", "유해화학시설-폐기물처리시설", "환경부", "1차 차목(그 밖의 유사시설) 참고 핀 — 개별 협의 대상 표시(판정 아님)"),
)
LAYER_BY_ID: dict[str, SafemapLayer] = {layer.layer_id: layer for layer in SAFEMAP_LAYERS}


class SafemapLayerClient:
    """레이어 하나의 전량 조회·캐시. 행은 원본 dict 그대로 둔다(컬럼은 레이어마다 다르다)."""

    def __init__(
        self,
        service_key: str,
        layer_id: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if layer_id not in LAYER_BY_ID:
            raise ValueError(f"알 수 없는 생활안전지도 레이어: {layer_id}")
        self.service_key = service_key
        self.layer = LAYER_BY_ID[layer_id]
        self.timeout = timeout
        self._transport = transport
        self._cache: list[dict[str, Any]] = []
        self._cached_at = 0.0
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    async def probe(self) -> tuple[int, list[str]]:
        """첫 페이지 2건만 받아 (전체 건수, 컬럼 이름)을 돌려준다. 연결 점검용."""

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify(),
            follow_redirects=True,
        ) as client:
            rows, total = await self._page(client, 1, 2)
        columns = list(rows[0].keys()) if rows else []
        return total, columns

    async def all_rows(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        if self._cache and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS:
            return self._cache
        async with self._fill_lock.get():
            if self._cache and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS:
                return self._cache
            rows: list[dict[str, Any]] = []
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport, verify=shared_verify(),
                follow_redirects=True,
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    page_rows, total = await self._page(client, page, PAGE_SIZE)
                    rows.extend(page_rows)
                    if len(page_rows) < PAGE_SIZE or len(rows) >= total:
                        break
            self._cache = rows
            self._cached_at = time.monotonic()
            return rows

    async def _page(
        self, client: httpx.AsyncClient, page: int, size: int
    ) -> tuple[list[dict[str, Any]], int]:
        response = await client.get(
            SAFEMAP_LAYER_URL.format(layer=self.layer.layer_id),
            params={
                "serviceKey": self.service_key,
                "pageNo": str(page),
                "numOfRows": str(size),
                "returnType": "json",
            },
        )
        # 미승인 레이어는 HTTP 500 에 resultCode 30 을 실어 보낸다(2026-09-17 실측).
        # 상태코드로 먼저 자르지 말고 본문을 읽어 사유를 구분한다.
        try:
            payload = response.json()
        except ValueError as exc:
            raise SafemapAPIError(
                f"{self.layer.label} 응답 오류 ({response.status_code})", response.status_code
            ) from exc
        header = payload.get("header") or {}
        code = str(header.get("resultCode") or "00")
        if code not in ("00", "0"):
            message = header.get("resultMsg") or code
            if code == "30":
                message = (
                    f"이 레이어({self.layer.layer_id})는 생활안전지도에서 데이터 사용신청이 "
                    "아직 승인되지 않았습니다 (등록되지 않은 서비스키)"
                )
            raise SafemapAPIError(f"{self.layer.label}: {message}")
        body = payload.get("body") or {}
        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        return list(items), int(body.get("totalCount") or 0)
