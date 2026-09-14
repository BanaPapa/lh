"""한국산업단지공단 공장등록 필지정보 API(15087615) — 등록공장을 지역 단위로 받는다.

API 실측(2026-09-15): `cmpnyNm` 이 필수지만 공백 한 칸(" ")이 와일드카드로 통하고,
`adresCode`(법정동코드 앞자리 = 시군구 5자리)로 지역을 좁힐 수 있다(군산시 52130 →
1,616건). 응답에는 도로명주소·관할기관·용도지역·건축면적이 있고 좌표·PNU 는 없다.
그래서 시군구 단위로 전량 받아 카카오 주소검색으로 좌표를 붙이고, 그 결과를
디스크에 캐시해 같은 주소를 다시 지오코딩하지 않는다. 시설 필지 경계는 호출부
(_attach_facility_boundaries)가 좌표로 조회한다.

등록공장은 매입제외 확정 근거가 아니라 「공장 있음」 검토 표시(LH 확정 2026-09-11
#1)이므로, 좌표를 못 붙인 행은 삭제하지 않고 격리 목록에 남긴다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.kgs import PublicDataAPIError
from app.services.single_flight import LoopSafeLock
from app.services.http_client import shared_verify

logger = logging.getLogger(__name__)

FACTORY_PARCEL_URL = (
    "https://apis.data.go.kr/B550624/fctryRegistLndpclInfo/getFctryLndpclService"
)
PAGE_SIZE = 1000
MAX_PAGES = 30
# 시군구 목록 메모리 캐시 수명. 등록공장은 자주 바뀌지 않는다.
CACHE_TTL_SECONDS = 24 * 3600
# `cmpnyNm` 은 필수인데 공백 한 칸이 와일드카드로 통한다(2026-09-15 실측).
WILDCARD_NAME = " "
# 지오코딩 동시 호출 상한(카카오 초당 제한 보호).
GEOCODE_CONCURRENCY = 8
DEFAULT_GEOCODE_CACHE = (
    Path(__file__).resolve().parents[2] / "data" / "factory_geocode_cache.json"
)

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


class FactoryRecord(NamedTuple):
    """등록공장 한 곳. 호출부(_records_to_facilities)가 읽는 필드명을 맞춘다."""

    record_id: str
    name: str
    address: str
    road_address: str
    coordinates: Coordinates | None
    status_text: str
    pnu: str
    # 관할기관·용도지역·건축면적. 판정 근거가 아니라 참고 표시용.
    organization: str
    zoning_name: str
    building_area_m2: float | None


def _text(item: ET.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def _parse_items(body: str) -> tuple[list[dict[str, str]], int]:
    """XML 응답 → (행 목록, totalCount). 오류 코드는 예외로 올린다."""

    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise PublicDataAPIError(f"등록공장 응답을 읽지 못했습니다: {exc}") from exc
    code = root.findtext("./header/resultCode") or root.findtext(
        "./cmmMsgHeader/returnReasonCode"
    )
    if code not in (None, "00"):
        message = root.findtext("./header/resultMsg") or root.findtext(
            "./cmmMsgHeader/returnAuthMsg"
        )
        raise PublicDataAPIError(f"등록공장 조회 실패 ({code}) {message or ''}".strip())
    rows: list[dict[str, str]] = []
    for item in root.iter("item"):
        rows.append({child.tag: (child.text or "").strip() for child in item})
    total = int(root.findtext("./body/totalCount") or 0)
    return rows, total


def record_from_row(row: dict[str, str], coordinates: Coordinates | None) -> FactoryRecord:
    area_text = row.get("fctryDongBuldAr") or ""
    try:
        area = float(area_text) if area_text else None
    except ValueError:
        area = None
    road = row.get("rnAdres") or ""
    return FactoryRecord(
        record_id=row.get("fctryManageNo") or road,
        name=row.get("cmpnyNm") or "",
        address=road,
        road_address=road,
        coordinates=coordinates,
        status_text="등록",
        pnu="",
        organization=row.get("cvplChrgOrgnztNm") or "",
        zoning_name=row.get("spfcSeCodeNm") or "",
        building_area_m2=area,
    )


def geocode_query(road_address: str) -> str:
    """지오코딩용 주소. 괄호 안 동·건물명은 카카오 주소검색을 방해하므로 뗀다."""

    return road_address.split("(", 1)[0].strip()


class FactoryRegistryClient:
    """시군구별 등록공장 목록 + 주소 지오코딩 캐시."""

    def __init__(
        self,
        service_key: str,
        geocoder: Geocoder | None = None,
        cache_path: Path | None = DEFAULT_GEOCODE_CACHE,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self.geocoder = geocoder
        self.cache_path = cache_path
        self.timeout = timeout
        self._transport = transport
        # 시군구 코드(5자리) → (조회 시각, 행 목록)
        self._rows: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._locks: dict[str, LoopSafeLock] = {}
        # 주소 → 좌표(없으면 None). 디스크 캐시와 동기화한다.
        self._geocache: dict[str, dict[str, float] | None] | None = None
        self._geocode_failures: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    @property
    def geocode_failures(self) -> list[str]:
        """이번 프로세스에서 좌표를 못 붙인 주소(격리, 삭제하지 않는다)."""

        return list(self._geocode_failures)

    # -- 목록 ---------------------------------------------------------------
    async def factories_in_sigungu(self, sigungu_code: str) -> list[dict[str, str]]:
        """시군구(법정동코드 앞 5자리)의 등록공장 행 전량. 24시간 캐시."""

        code = (sigungu_code or "")[:5]
        if not self.enabled or len(code) < 5:
            return []
        cached = self._rows.get(code)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]
        lock = self._locks.setdefault(code, LoopSafeLock())
        async with lock.get():
            cached = self._rows.get(code)
            if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
                return cached[1]
            rows: list[dict[str, str]] = []
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport, verify=shared_verify()
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    page_rows, total = await self._page(client, code, page)
                    rows.extend(page_rows)
                    if len(page_rows) < PAGE_SIZE or len(rows) >= total:
                        break
            self._rows[code] = (time.monotonic(), rows)
            return rows

    async def _page(
        self, client: httpx.AsyncClient, code: str, page: int
    ) -> tuple[list[dict[str, str]], int]:
        response = await client.get(
            FACTORY_PARCEL_URL,
            params={
                "serviceKey": self.service_key,
                "numOfRows": PAGE_SIZE,
                "pageNo": page,
                "cmpnyNm": WILDCARD_NAME,
                "adresCode": code,
            },
        )
        if response.status_code != 200:
            raise PublicDataAPIError(f"등록공장 조회 실패 (HTTP {response.status_code})")
        return _parse_items(response.text)

    # -- 좌표 ---------------------------------------------------------------
    def _load_geocache(self) -> dict[str, dict[str, float] | None]:
        if self._geocache is not None:
            return self._geocache
        data: dict[str, dict[str, float] | None] = {}
        if self.cache_path and self.cache_path.exists():
            try:
                raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
            except (OSError, ValueError):
                logger.warning("등록공장 지오코딩 캐시를 읽지 못해 새로 만듭니다: %s", self.cache_path)
        self._geocache = data
        return data

    def _save_geocache(self) -> None:
        if not self.cache_path or self._geocache is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._geocache, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            logger.warning("등록공장 지오코딩 캐시를 쓰지 못했습니다: %s", self.cache_path)

    async def _coordinates_for(self, address: str) -> Coordinates | None:
        cache = self._load_geocache()
        if address in cache:
            hit = cache[address]
            return Coordinates(lat=hit["lat"], lng=hit["lng"]) if hit else None
        if self.geocoder is None:
            return None
        try:
            found = await self.geocoder(geocode_query(address))
        except Exception:  # noqa: BLE001 — 지오코딩 장애는 격리로 남기고 계속 간다
            found = None
        cache[address] = {"lat": found.lat, "lng": found.lng} if found else None
        return found

    async def factories_near(
        self,
        center: Coordinates,
        radius_m: float,
        sigungu_code: str,
    ) -> list[FactoryRecord]:
        """사업지 시군구의 등록공장 중 반경 안의 것. 좌표 없는 행은 격리 목록으로."""

        rows = await self.factories_in_sigungu(sigungu_code)
        if not rows:
            return []
        semaphore = asyncio.Semaphore(GEOCODE_CONCURRENCY)

        async def locate(row: dict[str, str]) -> FactoryRecord:
            address = row.get("rnAdres") or ""
            if not address:
                return record_from_row(row, None)
            async with semaphore:
                return record_from_row(row, await self._coordinates_for(address))

        records = await asyncio.gather(*(locate(row) for row in rows))
        self._save_geocache()
        self._geocode_failures = [
            r.road_address for r in records if r.coordinates is None and r.road_address
        ]
        return [
            record
            for record in records
            if record.coordinates is not None
            and haversine_meters(center, record.coordinates) <= radius_m
        ]
