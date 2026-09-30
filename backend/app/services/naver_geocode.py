"""네이버 클라우드 플랫폼(NCP) Maps Geocoding — 카카오 주소검색이 막힐 때의 대체 지오코더.

카카오 일일 쿼터가 바닥나면 카카오는 HTTP 400 {"code":-10,"msg":"API limit has been
exceeded."} 을 돌려준다. 그동안 주소 → 좌표가 전부 멈추지 않게 NCP 지오코딩으로 넘긴다.

- 요청: GET https://maps.apigw.ntruss.com/map-geocode/v2/geocode?query=<주소>
  헤더 x-ncp-apigw-api-key-id(Client ID)·x-ncp-apigw-api-key(Client Secret).
- 응답: {"status":"OK","addresses":[{"roadAddress","jibunAddress","x":경도,"y":위도}, ...]}
- 인증 실패: HTTP 401 {"error":{"errorCode":"200","message":"Authentication Failed"}}.
  Client ID 가 틀렸거나 콘솔에서 Geocoding 을 사용 설정하지 않은 경우다. 한 번 보면
  이 프로세스 동안은 네이버를 끄고(로그 1회) 다음 원천(VWorld)으로 넘어간다 — 수천 행
  동기화가 행마다 같은 401 을 두드리지 않게.

Client Secret 원문은 로그·예외 문구·응답 어디에도 싣지 않는다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.models import Coordinates, GeocodeCandidate
from app.services.http_client import shared_verify

logger = logging.getLogger("uvicorn.error")

NAVER_GEOCODE_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"

# NCP API Gateway 인증 오류 코드(200 Authentication Failed · 210 Permission Denied).
_AUTH_ERROR_CODES = {"200", "210"}

AUTH_HINT = (
    "네이버 지오코딩 인증 실패 — NCP 콘솔에서 Application 의 Client ID·Client Secret 과 "
    "Maps 「Geocoding」 사용 설정을 확인해 주세요."
)

# 인증 실패로 끈 Client ID → 사유. 프로세스 동안 유지한다(설정 화면에서 키를 바꾸면
# Client ID 가 달라져 다시 시도한다).
_auth_disabled: dict[str, str] = {}


class NaverGeocodeError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NaverGeocodeAuthError(NaverGeocodeError):
    """키·권한 문제. 다시 불러도 같으므로 이 프로세스에서는 네이버를 끈다."""


@dataclass(frozen=True)
class NaverAddress:
    road_address: str
    jibun_address: str
    coordinates: Coordinates


# 질의 끝의 번지·건물번호(「1575-1」「산79-5」「368번지」). 앞 토막은 동·도로 이름.
_TRAILING_NUMBER = re.compile(r"^(.*?)(산?\d+(?:-\d+)?)(?:번지)?$")


def lot_key(query: str) -> str | None:
    """질의의 「동·도로 이름 + 번지」 열쇠(공백 없음). 번지로 끝나지 않으면 None.

    「전주시 덕진구 인후동2가 1575-1」 → 「인후동2가1575-1」, 「쑥고개로 368」 → 「쑥고개로368」.
    """

    tokens = " ".join(query.split()).split(" ")
    if not tokens or not tokens[-1]:
        return None
    match = _TRAILING_NUMBER.match(tokens[-1])
    if not match:
        return None
    head, number = match.group(1), match.group(2)
    if not head:
        if len(tokens) < 2:
            return None
        head = tokens[-2]
    return f"{head}{number}"


def keeps_lot(query: str, found: NaverAddress) -> bool:
    """결과 주소(지번·도로명)가 질의의 동·도로 이름과 번지를 그대로 품는가.

    번지를 잃고 동 중심점으로 떨어진 결과(「인후동2가」만 남은 주소)를 시설 위치로 쓰지
    않게 한다(address_candidates 와 같은 이유 · 2026-09-30 성락시장). 「1575-1」이
    「1575-10」에 걸리지 않게 뒤에 숫자·하이픈이 이어지면 불일치로 본다.
    """

    key = lot_key(query)
    if key is None:
        return False
    pattern = re.compile(re.escape(key) + r"(?![\d-])")
    for address in (found.jibun_address, found.road_address):
        if address and pattern.search(address.replace(" ", "")):
            return True
    return False


def auth_failure_reason(client_id: str) -> str | None:
    return _auth_disabled.get(client_id)


def reset_auth_failures() -> None:
    """테스트·키 교체용."""

    _auth_disabled.clear()


def clear_auth_failure(client_id: str) -> None:
    """설정 패널 재점검용 — 콘솔에서 사용 설정을 고친 뒤 다시 시도할 수 있게."""

    _auth_disabled.pop(client_id, None)


class NaverGeocodeClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def enabled(self) -> bool:
        """키가 있고, 이 프로세스에서 인증 실패로 꺼지지 않았는가."""

        return self.configured and self.client_id not in _auth_disabled

    @property
    def disabled_reason(self) -> str | None:
        return auth_failure_reason(self.client_id)

    def _disable(self, message: str) -> None:
        if self.client_id in _auth_disabled:
            return
        _auth_disabled[self.client_id] = message
        logger.warning("%s 이 실행 동안 네이버 지오코딩을 건너뜁니다. (%s)", AUTH_HINT, message)

    async def search(self, query: str) -> list[NaverAddress]:
        """주소 → 후보 목록. 결과 없음은 빈 목록, 조회 실패는 예외."""

        query = " ".join(query.split())
        if not self.configured:
            raise NaverGeocodeError("네이버 지오코딩 키(NAVER_MAP_CLIENT_ID·SECRET)가 없습니다.")
        if self.client_id in _auth_disabled:
            raise NaverGeocodeAuthError(AUTH_HINT, 401)
        if not query:
            return []
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            response = await client.get(
                NAVER_GEOCODE_URL,
                params={"query": query},
                headers={
                    "x-ncp-apigw-api-key-id": self.client_id,
                    "x-ncp-apigw-api-key": self.client_secret,
                    "Accept": "application/json",
                },
            )
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            body = {}
        error = body.get("error") if isinstance(body.get("error"), dict) else None
        if response.status_code in {401, 403} or (
            error and str(error.get("errorCode")) in _AUTH_ERROR_CODES
        ):
            message = str((error or {}).get("message") or f"HTTP {response.status_code}")
            self._disable(message)
            raise NaverGeocodeAuthError(f"{AUTH_HINT} ({message})", response.status_code)
        if response.status_code != 200 or error:
            message = str((error or {}).get("message") or "")
            raise NaverGeocodeError(
                f"네이버 지오코딩 요청 실패({response.status_code}){' ' + message if message else ''}",
                response.status_code,
            )
        status = str(body.get("status") or "")
        if status == "INVALID_REQUEST":
            # 질의가 주소 꼴이 아니다 — 조회는 정상이고 결과만 없다.
            return []
        if status != "OK":
            raise NaverGeocodeError(
                f"네이버 지오코딩 응답 이상({status or '상태 없음'}): {body.get('errorMessage') or ''}".strip(),
                response.status_code,
            )
        found: list[NaverAddress] = []
        for item in body.get("addresses") or []:
            try:
                coordinates = Coordinates(lat=float(item["y"]), lng=float(item["x"]))
            except (KeyError, TypeError, ValueError):
                continue
            found.append(
                NaverAddress(
                    road_address=str(item.get("roadAddress") or ""),
                    jibun_address=str(item.get("jibunAddress") or ""),
                    coordinates=coordinates,
                )
            )
        return found

    async def point(self, query: str, strict: bool = True) -> Coordinates | None:
        """주소 → 좌표 한 점. strict 면 번지까지 맞는 결과만 받는다."""

        for found in await self.search(query):
            if not strict or keeps_lot(query, found):
                return found.coordinates
        return None

    async def candidates(self, query: str) -> list[GeocodeCandidate]:
        """주소 검색창용 후보. 질의가 번지로 끝나면 번지가 맞는 결과만 남긴다."""

        rows = await self.search(query)
        if lot_key(query) is not None:
            rows = [row for row in rows if keeps_lot(query, row)]
        return [
            GeocodeCandidate(
                id=f"naver-{index}-{row.coordinates.lng}-{row.coordinates.lat}",
                name=row.road_address or row.jibun_address or query,
                address=row.jibun_address,
                road_address=row.road_address,
                coordinates=row.coordinates,
                source="naver",
            )
            for index, row in enumerate(rows[:8])
        ]
