"""건강보험심사평가원 병원정보서비스 — 종합병원·상급종합병원 반경 조회.

2차 심사표 의료시설의 지정 원천이다(「건강보험심사평가원 종류=종합병원」). LH 표준
데이터셋(JB_00_HOSPITALS, 전북 14곳)도 이 목록이다. 국립중앙의료원 원장은 갱신이
늦어 「정읍한국병원」이 빠져 있었다(2026-09-29 정읍 연지동: LH 38점 vs 우리 35점).

실호출로 확정한 사실(2026-09-29):
- 경로: `https://apis.data.go.kr/B551182/hospInfoServicev2/getHospBasisList`
  (기관코드 B551182 = 건강보험심사평가원). `_type=json` 으로 JSON 을 받는다.
- `clCd` 가 종별이다: 01 상급종합, 11 종합병원. 심사표는 둘 다 종합병원으로 인정한다
  (2026-09-18 확정).
- `xPos`(경도)·`yPos`(위도)·`radius`(m)로 반경검색이 된다. 좌표는 `XPos`·`YPos`(WGS84).
- 전북 시도 전량은 종합 12 + 상급 2 = 14곳으로 LH 데이터셋과 같다.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.models import Coordinates
from app.services.http_client import shared_verify
from app.services.ncmc_hospital import HospitalRecord


HIRA_HOSPITAL_URL = "https://apis.data.go.kr/B551182/hospInfoServicev2/getHospBasisList"

# 종별코드 → 심사표 표기. 둘 다 「종합병원」으로 인정한다.
CLASS_CODES: dict[str, str] = {"01": "상급종합병원", "11": "종합병원"}

PAGE_SIZE = 100
MAX_PAGES = 10

# 종합병원 목록은 자주 바뀌지 않는다. 같은 자리 재조회는 하루 동안 캐시한다.
CACHE_TTL_SECONDS = 24 * 60 * 60


class HiraHospitalAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _items(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    response = payload.get("response") or {}
    header = response.get("header") or {}
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0"):
        raise HiraHospitalAPIError(f"심평원 병원정보 오류 {code}: {header.get('resultMsg', '')}")
    body = response.get("body") or {}
    items = (body.get("items") or {}) if isinstance(body.get("items"), dict) else {}
    item = items.get("item") or []
    rows = item if isinstance(item, list) else [item]
    return [row for row in rows if isinstance(row, dict)], int(body.get("totalCount") or 0)


def _hospital(row: dict[str, Any], class_code: str) -> HospitalRecord | None:
    try:
        coordinates = Coordinates(lat=float(row["YPos"]), lng=float(row["XPos"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not (33.0 <= coordinates.lat <= 39.5 and 124.0 <= coordinates.lng <= 132.0):
        return None
    name = str(row.get("yadmNm") or "").strip()
    if not name:
        return None
    return HospitalRecord(
        hpid=str(row.get("ykiho") or ""),
        name=name,
        coordinates=coordinates,
        div_name=CLASS_CODES[class_code],
        address=str(row.get("addr") or "").strip(),
        phone=str(row.get("telno") or "").strip(),
        is_tertiary=class_code == "01",
    )


class HiraHospitalClient:
    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self._cache: dict[str, tuple[float, list[HospitalRecord]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    async def hospitals_around(
        self, center: Coordinates, radius_m: float
    ) -> list[HospitalRecord]:
        """반경 안의 종합병원·상급종합병원. 실패는 예외로 올린다(0곳과 구분)."""

        if not self.enabled:
            return []
        key = f"{center.lat:.5f},{center.lng:.5f}:{int(radius_m)}"
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

        hospitals: list[HospitalRecord] = []
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=True,
            verify=shared_verify(),
        ) as client:
            for class_code in CLASS_CODES:
                for page in range(1, MAX_PAGES + 1):
                    rows, total = await self._page(client, center, radius_m, class_code, page)
                    hospitals.extend(
                        record
                        for record in (_hospital(row, class_code) for row in rows)
                        if record is not None
                    )
                    if len(rows) < PAGE_SIZE or page * PAGE_SIZE >= total:
                        break

        self._cache[key] = (time.monotonic(), hospitals)
        return hospitals

    async def _page(
        self,
        client: httpx.AsyncClient,
        center: Coordinates,
        radius_m: float,
        class_code: str,
        page: int,
    ) -> tuple[list[dict[str, Any]], int]:
        response = await client.get(
            HIRA_HOSPITAL_URL,
            params={
                "serviceKey": self.service_key,
                "_type": "json",
                "clCd": class_code,
                "xPos": f"{center.lng:.7f}",
                "yPos": f"{center.lat:.7f}",
                "radius": str(int(radius_m)),
                "numOfRows": str(PAGE_SIZE),
                "pageNo": str(page),
            },
        )
        if response.status_code != 200:
            raise HiraHospitalAPIError(
                f"심평원 병원정보 HTTP {response.status_code}", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise HiraHospitalAPIError("심평원 병원정보 응답을 읽지 못했습니다.") from exc
        return _items(payload)
