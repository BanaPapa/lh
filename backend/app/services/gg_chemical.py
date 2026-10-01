"""경기데이터드림 유해화학물질 취급사업장 현황 — openapi.gg.go.kr/ChmstryMttrBizplc.

마목 유독물(H-02-마)의 **참고 핀** 원천이다. 판정 원천이 아니다 — LH [요청 2] 승인으로
마목은 「판정 미적용 — 별도 수기 확인」이고, 이 원장은 화학물질관리법 영업허가 사업장
(제조·사용·판매·보관저장·알선판매·운반)이라 「유독물 보관·저장·판매시설」 그 자체가
아니다. 업종구분(INDUTYPE_NM)을 핀에 실어 담당자가 판매업·보관저장업을 먼저 보게 한다.

실측(2026-09-17): 6,170건 · 경기 31개 시군 한정 · WGS84 위경도 직접 제공(REFINE_*) ·
pSize 1000 허용 · 갱신 연간(기준일 2026-02-06). 환경부 제공 중단으로 연간취급량 열은
비어 있다. WAF 가 기본 User-Agent 를 「보안 정책」 HTML(euc-kr)로 막으므로 브라우저형
UA 를 보낸다. 응답 봉투: {"ChmstryMttrBizplc":[{"head":[{"list_total_count"},{"RESULT"},
{"api_version"}]},{"row":[...]}]} · 서비스명 오류는 {"RESULT":{"CODE":"ERROR-310"}} 만 온다.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import PublicDataAPIError
from app.services.safemap_facilities import (
    SafemapFacility,
    facility_from_snapshot,
    snapshot_row,
)
from app.services.snapshot_store import DATA_DIR, SnapshotList

GG_CHEMICAL_SERVICE = "ChmstryMttrBizplc"
GG_CHEMICAL_URL = f"https://openapi.gg.go.kr/{GG_CHEMICAL_SERVICE}"
GG_CHEMICAL_DATASET_PAGE_URL = (
    "https://data.gg.go.kr/portal/data/service/selectServicePage.do"
    "?infId=M37YD49AM5UFN6VJM2CZ29567068&infSeq=3"
)
# 참고 핀의 layer_id. 생활안전지도 레이어와 같은 SafemapFacility 모양을 쓴다.
GG_CHEMICAL_LAYER_ID = "GG_CHEM"

PAGE_SIZE = 1000
MAX_PAGES = 20
CACHE_TTL_SECONDS = 24 * 60 * 60
# 전량 목록 서버 사본(services/snapshot_store). 어느 사업지든 위험물 Rule 이 이 목록을 묻기
# 때문에, 켜진 직후 첫 심사가 전량 수신을 기다리지 않게 실어 둔다. 앱 배선만 경로를 준다.
DEFAULT_SNAPSHOT_PATH = DATA_DIR / "gg_chemical_cache.json"
SNAPSHOT_KEY = GG_CHEMICAL_LAYER_ID
# 원천이 WAF 로 기본 UA(python-httpx) 를 차단한다(2026-09-17 실측).
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
# 취급시설이 그 자리에 있지 않은 영업 형태. 알선판매업은 시설 없는 중개, 운반업은 차량.
EXCLUDED_BUSINESS_TYPES: frozenset[str] = frozenset({"알선판매업", "운반업"})
# 한반도 밖 좌표는 원천 오류로 보고 뺀다.
_KOREA_BBOX = (33.0, 39.5, 124.0, 132.0)


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _coordinates(row: dict[str, Any]) -> Coordinates | None:
    try:
        lat = float(row.get("REFINE_WGS84_LAT"))
        lng = float(row.get("REFINE_WGS84_LOGT"))
    except (TypeError, ValueError):
        return None
    south, north, west, east = _KOREA_BBOX
    if not (south <= lat <= north and west <= lng <= east):
        return None
    return Coordinates(lat=lat, lng=lng)


def parse_row(row: dict[str, Any], ordinal: int) -> SafemapFacility | None:
    """행 하나를 참고 핀 레코드로. 이름·좌표가 없거나 제외 업종이면 None."""

    name = _text(row, "ENTRPS_NM")
    kind = _text(row, "INDUTYPE_NM")
    if not name or kind in EXCLUDED_BUSINESS_TYPES:
        return None
    coordinates = _coordinates(row)
    if coordinates is None:
        return None
    address = _text(
        row, "REFINE_ROADNM_ADDR", "LOCPLC_ROADNM_ADDR", "REFINE_LOTNO_ADDR", "LOCPLC_LOTNO_ADDR"
    )
    # 사업자번호는 다공장(1공장·2공장)에서 겹치므로 적재 순번을 붙여 유일하게 만든다.
    record_id = f"{_text(row, 'BIZREGNO') or 'nobiz'}-{ordinal}"
    return SafemapFacility(
        GG_CHEMICAL_LAYER_ID, record_id, name, address, kind, coordinates
    )


def _facilities_from_snapshot(rows: list[Any]) -> list[SafemapFacility]:
    return [
        facility
        for facility in (facility_from_snapshot(GG_CHEMICAL_LAYER_ID, row) for row in rows)
        if facility is not None
    ]


class GgChemicalClient:
    """경기 유해화학물질 취급사업장 전량 캐시(24시간 + 서버 사본) + 반경 조회.

    SafemapFacilityFeed 와 같은 모양(enabled · all_facilities · facilities_around)이라
    hazard_review 의 참고 핀 헬퍼가 그대로 받는다.
    """

    layer_id = GG_CHEMICAL_LAYER_ID

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
        # 전량 목록(메모리 하루 캐시 + 서버 사본). 사본에는 파싱한 핀 레코드를 둔다.
        self._list: SnapshotList[SafemapFacility] = SnapshotList(
            snapshot_path, SNAPSHOT_KEY, "경기 유해화학물질 취급사업장",
            fetch=self._facility_rows, parse=_facilities_from_snapshot, error=PublicDataAPIError,
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

    async def facilities_around(
        self, center: Coordinates, radius_m: float
    ) -> list[SafemapFacility]:
        facilities = await self.all_facilities()
        return [f for f in facilities if haversine_meters(center, f.coordinates) <= radius_m]

    async def all_facilities(self) -> list[SafemapFacility]:
        """경기 전량. 메모리 → 서버 사본(하루 넘었으면 뒤에서 갱신) → API 순."""

        if not self.enabled:
            return []
        return await self._list.get()

    async def refresh(self, force: bool = False) -> list[SafemapFacility]:
        """전량을 API 에서 새로 받아 메모리와 서버 사본을 갈아 끼운다."""

        if not self.enabled:
            return []
        return await self._list.refresh(force)

    async def _facility_rows(self, _previous: list[SafemapFacility]) -> list[dict[str, Any]]:
        rows = await self._fetch_rows()
        parsed = (parse_row(row, ordinal) for ordinal, row in enumerate(rows))
        return [snapshot_row(facility) for facility in parsed if facility is not None]

    async def _fetch_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            verify=shared_verify(),
            # Accept: application/json 을 붙이면 500 이 온다(2026-09-17 실측). UA 만 보낸다.
            headers={"User-Agent": BROWSER_USER_AGENT},
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                page_rows, total = await self._page(client, page)
                rows.extend(page_rows)
                # 빈 페이지 또는 총건수 도달이 끝. 짧은 페이지는 끝으로 보지 않는다
                # (총건수가 정본이고, 없으면 빈 페이지가 올 때까지 간다).
                if not page_rows or len(rows) >= total:
                    break
        return rows

    async def _page(
        self, client: httpx.AsyncClient, page: int
    ) -> tuple[list[dict[str, Any]], int]:
        params = {
            "KEY": self.service_key,
            "Type": "json",
            "pIndex": str(page),
            "pSize": str(PAGE_SIZE),
        }
        try:
            response = await client.get(GG_CHEMICAL_URL, params=params)
        except httpx.HTTPError as exc:
            raise PublicDataAPIError(
                f"경기 유해화학물질 취급사업장 조회에 실패했습니다: {exc}"
            ) from exc
        if response.status_code != 200:
            raise PublicDataAPIError(
                f"경기 유해화학물질 취급사업장 응답 오류 ({response.status_code})",
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            # WAF 차단(euc-kr HTML)·서비스 장애 HTML 이 여기로 온다.
            raise PublicDataAPIError(
                "경기 유해화학물질 취급사업장 응답을 해석하지 못했습니다 "
                "(JSON 이 아님 — WAF 차단 또는 서비스 오류)."
            ) from exc
        return _unwrap(payload)


def _unwrap(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """경기데이터드림 봉투를 풀어 (행, 총건수)로. 오류 코드는 예외로 올린다."""

    if not isinstance(payload, dict):
        raise PublicDataAPIError("경기 유해화학물질 취급사업장 응답 형식이 다릅니다.")
    result = payload.get("RESULT")
    if isinstance(result, dict):
        code = str(result.get("CODE", ""))
        raise PublicDataAPIError(
            f"경기 유해화학물질 취급사업장 오류 {code}: {result.get('MESSAGE', '')}"
        )
    envelope = payload.get(GG_CHEMICAL_SERVICE)
    if not isinstance(envelope, list):
        raise PublicDataAPIError("경기 유해화학물질 취급사업장 응답 형식이 다릅니다.")
    head: list[Any] = []
    rows: list[dict[str, Any]] = []
    for part in envelope:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("head"), list):
            head = part["head"]
        if isinstance(part.get("row"), list):
            rows = [row for row in part["row"] if isinstance(row, dict)]
    total = MAX_PAGES * PAGE_SIZE
    for item in head:
        if not isinstance(item, dict):
            continue
        result = item.get("RESULT")
        if isinstance(result, dict) and str(result.get("CODE", "")) != "INFO-000":
            raise PublicDataAPIError(
                f"경기 유해화학물질 취급사업장 오류 {result.get('CODE')}: "
                f"{result.get('MESSAGE', '')}"
            )
        if "list_total_count" in item:
            try:
                total = int(item["list_total_count"])
            except (TypeError, ValueError):
                pass
    return rows, total
