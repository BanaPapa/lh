"""시설 주소 → 좌표 공통 지오코더: 카카오 주소검색 → 네이버(NCP) 지오코딩 → VWorld 주소검색.

번지가 남은 후보만 쓴다(address_candidates). 동·구 이름만 남은 주소로 물러나 동 중심점을
시설 위치로 삼는 일이 없게 한다(2026-09-30 성락시장 6.9m 오류). 카카오가 못 찾는 옛 지번
(분할·합병으로 사라진 번지)은 VWorld 검색 API 가 한 건으로 딱 맞출 때만 받는다
(VWorldClient.search_address_point — 법정동 이름이 질의에 있어야 채택).

카카오가 막히면(일일 쿼터 초과 HTTP 400 code -10 · 키 오류 · 429) 이 프로세스에서 한동안
카카오를 건너뛰고 네이버 → VWorld 로 넘어간다 — 수천 행 동기화가 행마다 죽은 카카오를
기다리지 않게. 네이버도 VWorld 처럼 번지까지 맞는 결과만 받는다(naver_geocode.keeps_lot).

「없음(None)」은 켜진 원천이 모두 정상으로 답했을 때만 돌려준다. 어느 원천이 오류였고
아무도 못 찾았으면 GeocodeUnavailable 을 올려, 호출 쪽이 「주소 없음」으로 캐시하지
않게 한다(localdata 지오코딩 캐시·공장 지오캐시).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from app.models import Coordinates, GeocodeCandidate
from app.services.address_candidates import address_candidates
from app.services.naver_geocode import NaverGeocodeAuthError, NaverGeocodeError

logger = logging.getLogger("uvicorn.error")

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


# 통신 오류(연결 거부·포트 고갈 등)는 잠깐 쉬었다 다시 시도한다. 그래도 안 되면 예외를
# 올려 호출 쪽이 「못 찾음」으로 캐시하지 않게 한다(2026-09-30 동기화 중 포트 고갈로
# 멀쩡한 주소가 「없음」으로 굳을 뻔했다).
TRANSIENT_RETRIES = 3
TRANSIENT_BACKOFF_SECONDS = 1.5

# 카카오(또는 네이버)가 HTTP 오류(쿼터 초과 등)를 내면 이만큼 건너뛴다. 동기화 한 번은
# 통째로 덮고, 오래 도는 서버는 쿼터가 풀린 뒤 다시 카카오를 쓴다.
PROVIDER_OUTAGE_COOLDOWN_SECONDS = 30 * 60


class GeocodeUnavailable(RuntimeError):
    """원천이 오류로 답하지 못해 「주소 없음」을 확정할 수 없다."""

    def __init__(self, message: str, errors: list[BaseException]) -> None:
        super().__init__(message)
        self.errors = errors


class ProviderOutage(RuntimeError):
    """앞서 오류를 낸 원천을 쉬는 중이라 묻지 않았다."""

    def __init__(self, provider: str, reason: str) -> None:
        super().__init__(f"{provider} 일시 중단: {reason}")
        self.provider = provider
        self.reason = reason


# (원천, 키) → (재개 시각 monotonic, 사유). 프로세스 안에만 둔다.
_outages: dict[tuple[str, str], tuple[float, str]] = {}


def _outage_key(provider: str, client: object) -> tuple[str, str]:
    key = getattr(client, "api_key", None) or getattr(client, "client_id", None) or ""
    return provider, str(key)


def _outage(provider: str, client: object) -> str | None:
    entry = _outages.get(_outage_key(provider, client))
    if entry is None:
        return None
    until, reason = entry
    if time.monotonic() >= until:
        _outages.pop(_outage_key(provider, client), None)
        return None
    return reason


def _trip(provider: str, client: object, exc: BaseException) -> None:
    key = _outage_key(provider, client)
    if key in _outages and time.monotonic() < _outages[key][0]:
        return
    reason = str(exc) or exc.__class__.__name__
    _outages[key] = (time.monotonic() + PROVIDER_OUTAGE_COOLDOWN_SECONDS, reason)
    logger.warning(
        "%s 지오코딩 오류(%s) — %d분 동안 건너뛰고 대체 원천으로 찾습니다.",
        provider, reason[:120], PROVIDER_OUTAGE_COOLDOWN_SECONDS // 60,
    )


def mark_outage(provider: str, client: object, exc: BaseException) -> None:
    """바깥(상태 확인)에서 본 원천 오류를 쉬는 목록에 올린다."""

    _trip(provider, client, exc)


def current_outages() -> dict[str, str]:
    """지금 쉬는 원천 → 사유(설정 패널·안내 문구용)."""

    now = time.monotonic()
    return {provider: reason for (provider, _), (until, reason) in _outages.items() if now < until}


def reset_outages() -> None:
    """테스트용."""

    _outages.clear()


async def _retrying(call: Callable[[], Awaitable]):
    for attempt in range(TRANSIENT_RETRIES):
        try:
            return await call()
        except httpx.TransportError:
            if attempt == TRANSIENT_RETRIES - 1:
                raise
            await asyncio.sleep(TRANSIENT_BACKOFF_SECONDS * (attempt + 1))


def _is_http_failure(exc: BaseException) -> bool:
    """응답은 왔는데 오류(쿼터·키·서버 오류)인가. 통신 단계 실패와 구분한다."""

    return getattr(exc, "status_code", None) is not None or isinstance(exc, httpx.HTTPStatusError)


def _enabled(client: object | None, default: bool = False) -> bool:
    return client is not None and bool(getattr(client, "enabled", default))


def _kakao_enabled(kakao: object | None) -> bool:
    # enabled 속성이 없는 대역(테스트 가짜 클라이언트)은 켜진 것으로 본다.
    return _enabled(kakao, default=True)


async def _ask_kakao(kakao, candidates: list[str], errors: list[BaseException]):
    """카카오 후보 검색. 찾으면 후보 목록, 못 찾으면 빈 목록. 오류는 errors 에 담는다."""

    if not _kakao_enabled(kakao):
        return []
    reason = _outage("카카오", kakao)
    if reason is not None:
        errors.append(ProviderOutage("카카오", reason))
        return []
    for candidate in candidates:
        try:
            found = await _retrying(lambda c=candidate: kakao.geocode(c))
        except Exception as exc:  # noqa: BLE001 — 원천 오류는 모아서 판단한다
            if _is_http_failure(exc):
                _trip("카카오", kakao, exc)
            errors.append(exc)
            return []
        if found:
            return found
    return []


async def _ask_naver(naver, candidates: list[str], errors: list[BaseException], strict: bool = True):
    if not _enabled(naver):
        return None
    reason = _outage("네이버", naver)
    if reason is not None:
        errors.append(ProviderOutage("네이버", reason))
        return None
    for candidate in candidates:
        try:
            point = await _retrying(lambda c=candidate: naver.point(c, strict=strict))
        except NaverGeocodeAuthError:
            # 키·권한 문제는 설정 문제다. 네이버가 스스로 꺼졌으니(로그 1회) 미설정처럼
            # 다루고 다음 원천으로 넘어간다 — 오류로 세면 모든 「없음」이 영영 확정되지 않는다.
            return None
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, NaverGeocodeError) and _is_http_failure(exc):
                _trip("네이버", naver, exc)
            errors.append(exc)
            return None
        if point is not None:
            return point
    return None


async def _ask_vworld(vworld, candidates: list[str], errors: list[BaseException]):
    if not _enabled(vworld):
        return None
    for candidate in candidates:
        try:
            point = await _retrying(
                lambda c=candidate: vworld.search_address_point(c, strict=True)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            return None
        if point is not None:
            return point
    return None


def _unavailable(address: str, errors: list[BaseException]) -> GeocodeUnavailable:
    summary = " · ".join(str(e) or e.__class__.__name__ for e in errors)[:300]
    return GeocodeUnavailable(f"주소 조회 실패(원천 오류): {address} — {summary}", errors)


def make_address_geocoder(
    kakao, vworld=None, naver=None, *, fallback_on_miss: bool = True
) -> Geocoder:
    """주소 → 좌표. 못 찾으면 None, 원천 오류로 확정할 수 없으면 GeocodeUnavailable.

    fallback_on_miss=False 면 카카오가 정상으로 「없음」이라 답한 주소는 대체 원천에 다시
    묻지 않는다 — 카카오가 막혔을 때만 네이버·VWorld 로 넘어간다. LH 앱과 결과를 맞춰 온
    판정 원천(등록공장·명단 지오코딩)은 카카오가 살아 있는 동안 결과가 바뀌지 않게 이 값을 쓴다.
    """

    async def geocode(address: str) -> Coordinates | None:
        candidates = address_candidates(address)
        if not candidates:
            return None
        errors: list[BaseException] = []
        found = await _ask_kakao(kakao, candidates, errors)
        if found:
            return found[0].coordinates
        kakao_answered = _kakao_enabled(kakao) and not errors
        if kakao_answered and not fallback_on_miss:
            return None
        point = await _ask_naver(naver, candidates, errors)
        if point is not None:
            return point
        point = await _ask_vworld(vworld, candidates, errors)
        if point is not None:
            return point
        if errors:
            raise _unavailable(address, errors)
        return None

    return geocode


def make_lenient_geocoder(geocoder: Geocoder) -> Geocoder:
    """원천 오류를 None 으로 삼키는 판. 예외를 다루지 않는 기존 호출 쪽(명단 지오코딩 등)용."""

    async def geocode(address: str) -> Coordinates | None:
        try:
            return await geocoder(address)
        except Exception:  # noqa: BLE001 — 실패는 호출 쪽 지오코딩 실패 목록으로 드러난다
            return None

    return geocode


def _fallback_notice(errors: list[BaseException], source: str) -> str:
    reason = next((str(e) for e in errors if str(e)), "응답 오류")
    return (
        f"카카오 주소 검색이 응답하지 않아({reason[:80]}) {source}에서 찾았습니다. "
        "카카오가 복구되면 다시 검색해 확인해 주세요."
    )


async def search_address_candidates(
    query: str, *, kakao, naver=None, vworld=None
) -> tuple[list[GeocodeCandidate], str]:
    """주소 검색창·일괄 심사용 후보 검색: 카카오 → 네이버 → VWorld.

    반환: (후보, 안내 문구). 카카오가 막혀 대체 원천으로 찾았으면 안내 문구를 채운다
    (원천 장애는 조용히 넘기지 않는다). 아무도 못 찾았는데 오류가 섞였으면
    GeocodeUnavailable 을 올린다.
    """

    errors: list[BaseException] = []
    found = await _ask_kakao(kakao, [query], errors)
    if found:
        return list(found), ""
    kakao_errors = list(errors)
    if _enabled(naver):
        reason = _outage("네이버", naver)
        if reason is not None:
            errors.append(ProviderOutage("네이버", reason))
        else:
            try:
                rows = await _retrying(lambda: naver.candidates(query))
            except NaverGeocodeAuthError:
                rows = []
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, NaverGeocodeError) and _is_http_failure(exc):
                    _trip("네이버", naver, exc)
                errors.append(exc)
                rows = []
            if rows:
                return rows, _fallback_notice(kakao_errors, "네이버 지오코딩") if kakao_errors else ""
    point = await _ask_vworld(vworld, [query], errors)
    if point is not None:
        candidate = GeocodeCandidate(
            id=f"vworld-{point.lng}-{point.lat}",
            name=query,
            address=query,
            coordinates=point,
            source="vworld",
        )
        return [candidate], _fallback_notice(kakao_errors, "브이월드 주소검색") if kakao_errors else ""
    if errors:
        raise _unavailable(query, errors)
    return [], ""
