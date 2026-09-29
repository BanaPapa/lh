"""시설 주소 → 좌표 공통 지오코더: 카카오 주소검색 → VWorld 주소검색(보조).

번지가 남은 후보만 쓴다(address_candidates). 동·구 이름만 남은 주소로 물러나 동 중심점을
시설 위치로 삼는 일이 없게 한다(2026-09-30 성락시장 6.9m 오류). 카카오가 못 찾는 옛 지번
(분할·합병으로 사라진 번지)은 VWorld 검색 API 가 한 건으로 딱 맞출 때만 받는다
(VWorldClient.search_address_point — 법정동 이름이 질의에 있어야 채택).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx

from app.models import Coordinates
from app.services.address_candidates import address_candidates

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


# 통신 오류(연결 거부·포트 고갈 등)는 잠깐 쉬었다 다시 시도한다. 그래도 안 되면 예외를
# 올려 호출 쪽이 「못 찾음」으로 캐시하지 않게 한다(2026-09-30 동기화 중 포트 고갈로
# 멀쩡한 주소가 「없음」으로 굳을 뻔했다).
TRANSIENT_RETRIES = 3
TRANSIENT_BACKOFF_SECONDS = 1.5


async def _retrying(call: Callable[[], Awaitable]):
    for attempt in range(TRANSIENT_RETRIES):
        try:
            return await call()
        except httpx.TransportError:
            if attempt == TRANSIENT_RETRIES - 1:
                raise
            await asyncio.sleep(TRANSIENT_BACKOFF_SECONDS * (attempt + 1))


def make_address_geocoder(kakao, vworld=None) -> Geocoder:
    async def geocode(address: str) -> Coordinates | None:
        candidates = address_candidates(address)
        if getattr(kakao, "enabled", False):
            for candidate in candidates:
                found = await _retrying(lambda c=candidate: kakao.geocode(c))
                if found:
                    return found[0].coordinates
        if vworld is not None and getattr(vworld, "enabled", False):
            for candidate in candidates:
                point = await _retrying(
                    lambda c=candidate: vworld.search_address_point(c, strict=True)
                )
                if point is not None:
                    return point
        return None

    return geocode
