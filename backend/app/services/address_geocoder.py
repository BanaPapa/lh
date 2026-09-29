"""시설 주소 → 좌표 공통 지오코더: 카카오 주소검색 → VWorld 주소검색(보조).

번지가 남은 후보만 쓴다(address_candidates). 동·구 이름만 남은 주소로 물러나 동 중심점을
시설 위치로 삼는 일이 없게 한다(2026-09-30 성락시장 6.9m 오류). 카카오가 못 찾는 옛 지번
(분할·합병으로 사라진 번지)은 VWorld 검색 API 가 한 건으로 딱 맞출 때만 받는다
(VWorldClient.search_address_point — 법정동 이름이 질의에 있어야 채택).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.models import Coordinates
from app.services.address_candidates import address_candidates

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


def make_address_geocoder(kakao, vworld=None) -> Geocoder:
    async def geocode(address: str) -> Coordinates | None:
        candidates = address_candidates(address)
        if getattr(kakao, "enabled", False):
            for candidate in candidates:
                try:
                    found = await kakao.geocode(candidate)
                except Exception:  # noqa: BLE001 — 한 후보 실패로 전체를 멈추지 않는다
                    found = []
                if found:
                    return found[0].coordinates
        if vworld is not None and getattr(vworld, "enabled", False):
            for candidate in candidates:
                point = await vworld.search_address_point(candidate)
                if point is not None:
                    return point
        return None

    return geocode
