"""전국도서관표준데이터 — 2차 공공시설의 공공도서관 원천(15013109).

LH 내부망 앱 표준 데이터셋의 공공시설 「도서관」(JB_45, 전북 66곳)은 문체부 전국문화기반
시설 총람 「공공도서관」 시트다. 같은 분류(국가도서관통계 공공도서관)를 좌표와 함께 주는
공공 API 가 전국도서관표준데이터다. 도서관유형(lbrrySe)으로 공공도서관만 남긴다 —
작은도서관·전문도서관·학교도서관은 LH 목록에 없다.

실호출로 확정한 사실(2026-09-30, 활용신청 승인 직후):
- 응답은 `{"header","body":{"items":{"item":[…]},"totalCount"}}` 로 `response` 감싸개가 없다
  (감싸개가 있는 형식도 받는다). 전국 3,601행, 한 페이지 1,000건.
- 필드: lbrryNm(도서관명)·ctprvnNm·signguNm·lbrrySe(도서관유형)·rdnmadr·latitude·longitude·
  referenceDate 등. 도서관유형 값: 작은도서관 2,096 · 공공도서관 1,439 · 어린이도서관 33 ·
  학교도서관 26 · 전문도서관 4 · 장애인도서관 2 · 대학도서관 1.
- 전북 공공도서관 65 + 어린이도서관 1 = 66행이 LH 66곳과 수는 같지만 같은 곳은 59곳이다.
  김제시립도서관(본관·금구·만경분관)·명봉도서관이 없고, 군산시립·설림도서관이 방 이름
  (「(자료열람실)」·「(학습실)」)으로 두 줄씩, 진안도서관이 두 줄 실렸다. 이름은 「전주시립」
  같은 앞머리 없이 「인후도서관」 꼴이다. 그래서 amenities 가 VWorld 장소검색 「공공도서관」
  분류(LH 66곳과 이름까지 같음)로 빠진 곳을 보충하고 이름을 맞춘다.
- 장애 동안에는 amenities 가 VWorld 분류로 대체하고 경고를 띄운다.

서버 사본(2026-10-01): 받아 둔 공공도서관 목록을 파일(public_libraries_cache.json)로 실어
서버가 켜지자마자 쓰고, 하루가 지났으면 뒤에서 새로 받는다(snapshot_store 참고).
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


PUBLIC_LIBRARY_URL = "https://api.data.go.kr/openapi/tn_pubr_public_lbrry_api"
PUBLIC_LIBRARY_DATASET_ID = "15013109"

PAGE_SIZE = 1000
MAX_PAGES = 30
CACHE_TTL_SECONDS = 24 * 60 * 60

# 공공도서관 목록 사본(앱 배선만 이 경로를 준다).
DEFAULT_SNAPSHOT_PATH = DATA_DIR / "public_libraries_cache.json"
SNAPSHOT_KEY = "libraries"

LIBRARY_STANDARD_SOURCE = "전국도서관표준데이터(공공도서관)"

# 공공도서관으로 세는 도서관유형. 어린이도서관은 도서관법상 공공도서관의 한 종류이고 LH
# 목록에도 있다(남원시어린이청소년도서관·정읍기적의도서관).
PUBLIC_LIBRARY_TYPES: frozenset[str] = frozenset({"공공도서관", "어린이도서관"})

NAME_KEYS = ("lbrryNm", "도서관명")
TYPE_KEYS = ("lbrrySe", "도서관유형")
ROAD_KEYS = ("rdnmadr", "소재지도로명주소")
LAT_KEYS = ("latitude", "위도")
LNG_KEYS = ("longitude", "경도")
# 사본에 남기는 열(판정이 읽는 것만 — 영문·한글 열 이름 모두).
SNAPSHOT_COLUMNS = NAME_KEYS + TYPE_KEYS + ROAD_KEYS + LAT_KEYS + LNG_KEYS


class PublicLibraryAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def unapproved(self) -> bool:
        """활용신청 전(미등록 키)인가 — 장애가 아니라 아직 쓸 수 없는 상태."""

        return self.status_code in (401, 403)


class LibraryRecord(NamedTuple):
    name: str
    kind: str
    address: str
    coordinates: Coordinates


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def library_record(row: dict[str, Any]) -> LibraryRecord | None:
    """표준데이터 한 행 → 공공도서관. 공공도서관이 아니거나 좌표가 없으면 None."""

    name = _pick(row, NAME_KEYS)
    kind = _pick(row, TYPE_KEYS)
    if not name or kind not in PUBLIC_LIBRARY_TYPES:
        return None
    try:
        coordinates = Coordinates(lat=float(_pick(row, LAT_KEYS)), lng=float(_pick(row, LNG_KEYS)))
    except ValueError:
        return None
    if not (33.0 <= coordinates.lat <= 39.5 and 124.0 <= coordinates.lng <= 132.0):
        return None
    return LibraryRecord(name, kind, _pick(row, ROAD_KEYS), coordinates)


def libraries_from_rows(rows: list[Any]) -> list[LibraryRecord]:
    """표준데이터 행 목록 → 공공도서관(API 행·사본 행 공통)."""

    return [
        record
        for record in (library_record(row) for row in rows if isinstance(row, dict))
        if record is not None
    ]


class PublicLibraryClient:
    """전국도서관표준데이터 전량 → 공공도서관만 → 하루 캐시(+ 서버 사본)."""

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
        self._list: SnapshotList[LibraryRecord] = SnapshotList(
            snapshot_path, SNAPSHOT_KEY, "전국도서관표준데이터",
            fetch=self._fetch_rows, parse=libraries_from_rows, error=PublicLibraryAPIError,
            empty_message="전국도서관표준데이터에 공공도서관이 없습니다.",
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

    async def libraries_around(
        self, center: Coordinates, radius_m: float
    ) -> list[LibraryRecord]:
        """반경 안의 공공도서관. 실패는 예외로 올린다(0곳과 구분)."""

        libraries = await self._all_libraries()
        return [
            library
            for library in libraries
            if haversine_meters(center, library.coordinates) <= radius_m
        ]

    async def _all_libraries(self) -> list[LibraryRecord]:
        """전국 공공도서관. 메모리 → 서버 사본(하루 넘었으면 뒤에서 갱신) → API 순."""

        return await self._list.get()

    async def refresh(self, force: bool = False) -> list[LibraryRecord]:
        """전국 목록을 API 에서 새로 받아 메모리와 서버 사본을 갈아 끼운다."""

        return await self._list.refresh(force)

    async def _fetch_rows(self, _previous: list[LibraryRecord]) -> list[dict[str, Any]]:
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
        # 사본에는 공공도서관 행만, 판정이 읽는 열만 둔다(작은도서관 2천여 행은 버린다).
        return trim_rows(
            [row for row in rows if library_record(row) is not None], SNAPSHOT_COLUMNS
        )

    async def _page(
        self, client: httpx.AsyncClient, page: int
    ) -> tuple[list[dict[str, Any]], int]:
        try:
            response = await client.get(
                PUBLIC_LIBRARY_URL,
                params={
                    "serviceKey": self.service_key,
                    "type": "json",
                    "numOfRows": str(PAGE_SIZE),
                    "pageNo": str(page),
                },
            )
        except httpx.HTTPError as exc:
            raise PublicLibraryAPIError(f"전국도서관표준데이터 요청 실패: {exc}") from exc
        if response.status_code != 200:
            raise PublicLibraryAPIError(
                f"전국도서관표준데이터 HTTP {response.status_code}"
                + (" (활용신청 승인 전)" if response.status_code in (401, 403) else ""),
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicLibraryAPIError("전국도서관표준데이터 응답을 읽지 못했습니다.") from exc
        root = payload.get("response") or payload
        header = root.get("header") or {}
        code = str(header.get("resultCode", "00"))
        if code not in ("00", "0"):
            raise PublicLibraryAPIError(
                f"전국도서관표준데이터 오류 {code}: {header.get('resultMsg', '')}"
            )
        body = root.get("body") or {}
        items = body.get("items")
        if isinstance(items, dict):
            items = items.get("item")
        rows = items if isinstance(items, list) else ([items] if isinstance(items, dict) else [])
        try:
            total = int(body.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = 0
        return [row for row in rows if isinstance(row, dict)], total
