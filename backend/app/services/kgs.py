"""한국가스안전공사 전국 LPG 충전소 조회.

오피넷은 유가 정보를 파는 곳이라 LPG 취급 주유소 위주로 잡히고, 자동차 충전만
하는 순수 충전소는 빠진다. 이 원천이 그 구멍을 메운다.

이 API는 지역 필터가 없고 한 번에 100건씩만 준다. 전국이 2천 건 이하라
전량을 받아 메모리에 캐시한 뒤 좌표로 걸러 쓴다.
"""

from __future__ import annotations

import time
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.single_flight import LoopSafeLock


KGS_LPG_URL = "https://apis.data.go.kr/B410019/kgsapi/lpg_station"

# 공공데이터포털 공통 상한. 더 큰 값을 넣어도 100건만 온다.
PAGE_SIZE = 100

# 안전장치. 전국이 2천 건 수준이라 이 이상은 응답 이상으로 본다.
MAX_PAGES = 60

# 충전소는 하루에도 몇 곳씩 바뀌지 않는다. 하루 한 번이면 충분하다.
CACHE_TTL_SECONDS = 24 * 60 * 60

# --- 좌표 위생 검사 (docs/hazards H-02-나 §6-4) ---------------------------------
# 같은 원천의 전북 원본 139건 중 19건(14%)이 1km 이상, 5건이 10km 이상 틀렸다.
# 유형 ① 지오코딩 실패 기본값(서울시청 37.5665, 126.978) ② 주소는 전북인데 경도
# 128.7~128.9(경상도 권역) ③ 동명 업소 좌표 복사. 전국 API 라 전북 범위 검사를
# 그대로 쓸 수 없으므로 ①과 「주소의 시도 ↔ 좌표」 불일치(②)를 격리한다. ③은
# 판정 단계에서 같은 좌표·다른 주소를 review 로 다룬다.
SUSPECT_DEFAULT_COORDINATES: tuple[tuple[float, float, str], ...] = (
    (37.5665, 126.978, "서울시청"),
)
SUSPECT_DEFAULT_TOLERANCE_DEG = 0.0005  # 약 50m

# 시도별 대략 경계(lat_min, lat_max, lng_min, lng_max). 좌표가 주소의 시도 경계를
# 크게 벗어나면 좌표 오류로 본다. 인접 시도와 겹치도록 넉넉히 잡아 오탐을 피한다.
PROVINCE_BBOX: dict[str, tuple[float, float, float, float]] = {
    "서울": (37.4, 37.75, 126.7, 127.25),
    "인천": (37.0, 37.9, 124.5, 126.9),
    "경기": (36.85, 38.4, 126.3, 127.9),
    "강원": (37.0, 38.7, 127.0, 129.5),
    "충북": (36.0, 37.3, 127.2, 128.7),
    "충남": (35.9, 37.1, 125.9, 127.6),
    "대전": (36.15, 36.55, 127.2, 127.6),
    "세종": (36.4, 36.8, 127.1, 127.4),
    "전북": (34.9, 36.4, 125.9, 128.0),
    "전남": (33.9, 35.6, 125.0, 127.9),
    "광주": (35.0, 35.35, 126.6, 127.05),
    "경북": (35.4, 37.6, 127.7, 131.0),
    "대구": (35.6, 36.05, 128.3, 128.85),
    "경남": (34.4, 35.95, 127.5, 129.4),
    "부산": (34.95, 35.45, 128.7, 129.4),
    "울산": (35.3, 35.75, 128.9, 129.5),
    "제주": (33.0, 34.1, 126.0, 127.1),
}
# 주소 앞머리 → 시도 키. 정식 명칭·약칭이 섞여 온다.
PROVINCE_ALIASES: tuple[tuple[str, str], ...] = (
    ("서울", "서울"), ("인천", "인천"), ("경기", "경기"), ("강원", "강원"),
    ("충청북도", "충북"), ("충북", "충북"), ("충청남도", "충남"), ("충남", "충남"),
    ("대전", "대전"), ("세종", "세종"),
    ("전라북도", "전북"), ("전북", "전북"), ("전라남도", "전남"), ("전남", "전남"),
    ("광주", "광주"), ("경상북도", "경북"), ("경북", "경북"),
    ("대구", "대구"), ("경상남도", "경남"), ("경남", "경남"),
    ("부산", "부산"), ("울산", "울산"), ("제주", "제주"),
)


def province_of(address: str) -> str | None:
    """주소 앞머리에서 시도 키를 뽑는다. 못 찾으면 None(검사하지 않는다)."""

    head = (address or "").strip()
    for alias, key in PROVINCE_ALIASES:
        if head.startswith(alias):
            return key
    return None


def coordinate_quarantine_reason(address: str, coordinates: Coordinates) -> str:
    """좌표를 격리해야 하면 그 사유를, 정상이면 빈 문자열을 돌려준다."""

    lat, lng = coordinates.lat, coordinates.lng
    if not (33.0 <= lat <= 39.5 and 124.0 <= lng <= 132.0):
        return f"좌표 한반도 범위 밖(lat={lat}, lng={lng})"
    for d_lat, d_lng, label in SUSPECT_DEFAULT_COORDINATES:
        if (
            abs(lat - d_lat) <= SUSPECT_DEFAULT_TOLERANCE_DEG
            and abs(lng - d_lng) <= SUSPECT_DEFAULT_TOLERANCE_DEG
        ):
            return f"지오코딩 실패 기본값 의심({label} 좌표) — 주소 기반 재산출 필요"
    province = province_of(address)
    if province is not None:
        lat_min, lat_max, lng_min, lng_max = PROVINCE_BBOX[province]
        if not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max):
            return (
                f"좌표가 주소의 시도({province}) 범위 밖(lat={lat}, lng={lng}) — "
                "주소 기반 재산출 필요"
            )
    return ""


class PublicDataAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class QuarantinedStation(NamedTuple):
    """좌표 위생 검사에서 격리한 충전소. 삭제하지 않고 사유와 함께 남긴다."""

    name: str
    address: str
    reason: str


class LpgStation(NamedTuple):
    """LPG 충전소 한 곳."""

    name: str
    address: str
    region: str
    coordinates: Coordinates
    phone: str = ""
    usage: str = ""

    @property
    def station_id(self) -> str:
        """이 API는 고유키를 주지 않아 좌표와 상호로 식별자를 만든다."""

        return (
            f"{self.coordinates.lat:.6f}:{self.coordinates.lng:.6f}:{self.name}"
        )


def _station_or_reason(row: dict[str, Any]) -> tuple[LpgStation | None, str]:
    """행 하나를 충전소로 바꾼다. 격리 대상이면 (None, 사유)."""

    lat = row.get("LAT")
    lng = row.get("LOT")
    if lat in (None, "") or lng in (None, ""):
        return None, "좌표 결측"
    try:
        coordinates = Coordinates(lat=float(lat), lng=float(lng))
    except (TypeError, ValueError):
        return None, f"좌표 해석 불가(LAT={lat!r}, LOT={lng!r})"
    address = str(row.get("ADDR") or "").strip()
    reason = coordinate_quarantine_reason(address, coordinates)
    if reason:
        return None, reason
    return LpgStation(
        name=str(row.get("BSES_NM") or "").strip(),
        address=str(row.get("ADDR") or "").strip(),
        region=str(row.get("SECT_NM") or "").strip(),
        coordinates=coordinates,
        phone=str(row.get("TELNO") or "").strip(),
        usage=str(row.get("MGT_NM") or "").strip(),
    ), ""


def _station(row: dict[str, Any]) -> LpgStation | None:
    return _station_or_reason(row)[0]


class KgsLpgClient:
    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self._cache: list[LpgStation] = []
        self._quarantined: list[QuarantinedStation] = []
        self._cached_at = 0.0
        # 전국 목록 캐시 채우기를 직렬화하는 single-flight 잠금. 동시 호출이 여럿
        # 이어도 실제 조회는 1회만 일어난다(캐시 스탬피드 방지).
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    @property
    def quarantined(self) -> list[QuarantinedStation]:
        """좌표 위생 검사로 격리된 충전소. 마지막 조회 기준."""

        return list(self._quarantined)

    def _ingest(self, rows: list[dict[str, Any]]) -> list[LpgStation]:
        """행 목록을 충전소로 바꾸고 격리분은 사유와 함께 따로 모은다."""

        stations: list[LpgStation] = []
        quarantined: list[QuarantinedStation] = []
        for row in rows:
            station, reason = _station_or_reason(row)
            if station is not None:
                stations.append(station)
            elif reason != "좌표 결측":
                quarantined.append(
                    QuarantinedStation(
                        name=str(row.get("BSES_NM") or "").strip(),
                        address=str(row.get("ADDR") or "").strip(),
                        reason=reason,
                    )
                )
        self._quarantined = quarantined
        return stations

    async def stations_around(
        self,
        center: Coordinates,
        radius_m: float,
    ) -> list[LpgStation]:
        """반경 안의 충전소. 전국 목록을 캐시해 두고 거리로 거른다."""

        stations = await self.all_stations()
        return [
            station
            for station in stations
            if haversine_meters(center, station.coordinates) <= radius_m
        ]

    def _is_warm(self) -> bool:
        return bool(self._cache) and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    async def all_stations(self) -> list[LpgStation]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache

        async with self._fill_lock.get():
            # 잠금을 잡은 뒤 다시 확인한다. 먼저 들어간 호출이 이미 채웠으면
            # 재조회하지 않는다(double-checked).
            if self._is_warm():
                return self._cache

            all_rows: list[dict[str, Any]] = []
            # 조회가 실패하면 예외가 그대로 전파돼 캐시를 건드리지 않는다. 실패를
            # 캐시하지 않으므로 다음 호출이 다시 시도할 수 있다.
            async with httpx.AsyncClient(
                timeout=self.timeout,
                transport=self._transport,
                verify=shared_verify(),
            ) as client:
                for page in range(1, MAX_PAGES + 1):
                    rows, total_pages = await self._page(client, page)
                    all_rows.extend(rows)
                    if len(rows) < PAGE_SIZE or page >= total_pages:
                        break

            stations = self._ingest(all_rows)
            self._cache = stations
            self._cached_at = time.monotonic()
            return stations

    async def _page(
        self,
        client: httpx.AsyncClient,
        page: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """한 페이지의 행과 전체 페이지 수.

        주의: 이 API의 totalCount 는 레코드 수가 아니라 페이지 수다.
        numOfRows=1 이면 1976, numOfRows=100 이면 20 이 온다.
        """

        params = {
            "serviceKey": self.service_key,
            "pageNo": str(page),
            "numOfRows": str(PAGE_SIZE),
            "type": "json",
        }
        try:
            response = await client.get(KGS_LPG_URL, params=params)
        except httpx.HTTPError as exc:
            raise PublicDataAPIError(f"LPG 충전소 조회에 실패했습니다: {exc}") from exc

        if response.status_code != 200:
            raise PublicDataAPIError(
                f"LPG 충전소 응답 오류 ({response.status_code})",
                response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError("LPG 충전소 응답을 해석하지 못했습니다.") from exc

        body = (payload.get("response") or {}).get("body") or {}
        header = (payload.get("response") or {}).get("header") or {}
        code = str(header.get("resultCode") or "00")
        if code not in ("00", "0"):
            raise PublicDataAPIError(
                f"LPG 충전소 오류: {header.get('resultMsg') or code}"
            )

        items = body.get("items") or []
        if isinstance(items, dict):
            items = items.get("item") or []
        rows = [row for row in items if isinstance(row, dict)]
        try:
            total_pages = int(body.get("totalCount") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        return rows, total_pages
