"""시설 지번주소 → 시설 필지.

초·중·고는 LH 내부망 앱이 학교 지번주소로 PNU 를 만들어 연속지적도 필지를 받고,
사업지 대지경계 ↔ 그 필지 경계로 잰다. 학교 위치 표준데이터의 좌표로 필지를 찾으면
좌표가 옆 필지(같은 학교 부지의 작은 필지·도로·이웃 학교 부지)에 떨어져 50~150m
길게 나왔다(2026-09-30 전주 서완산동2가: 전주완산서초 좌표 → 서완산동1가 62 학
219m, 지번주소 227-1 → 169m = LH). 그래서 학교는 주소 필지를 먼저 쓰고, 못 찾을 때만
좌표 필지로 물러난다.

주소 → PNU 는 법정동코드표 조립(설정돼 있으면) → 카카오 주소검색(b_code) 순이다.
주소가 틀렸을 때 엉뚱한 필지로 재지 않도록, 필지가 시설 좌표에서
ADDRESS_PARCEL_MAX_OFFSET_M 안에 있어야 채택한다(전주 학교 80곳 실측 최대 40m).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.models import Coordinates
from app.services.geo import distance_point_to_polygon_m
from app.services.parcel_sanity import parcel_rejection_reason
from app.services.pnu import pnu_from_address_document
from app.services.pnu_resolver import LegalDongIndex, assemble_pnu
from app.services.vworld import ParcelFeature

logger = logging.getLogger(__name__)

# 주소 필지가 시설 좌표에서 이보다 멀면 주소가 틀린 것으로 보고 쓰지 않는다.
ADDRESS_PARCEL_MAX_OFFSET_M = 300.0
# 학교는 옮기지 않는 한 주소 필지가 그대로다. 수집기 인스턴스 동안 주소별로 기억한다.
ADDRESS_PARCEL_CACHE_MAX = 4000


class AddressSearch(Protocol):
    async def address_documents(self, query: str) -> list[dict[str, Any]]: ...


PnuParcelFetcher = Callable[[str], Awaitable[ParcelFeature | None]]
PointLocator = Callable[[str], Awaitable[Coordinates | None]]
PointParcelFetcher = Callable[[Coordinates], Awaitable[ParcelFeature | None]]

# 주소 끝의 지번(「산」 + 본번-부번). 대체 경로로 찾은 필지가 같은 번지인지 확인한다.
_TAIL_LOT = re.compile(r"(산)?\s*(\d+)(?:\s*-\s*(\d+))?\s*$")
_HEAD_LOT = re.compile(r"^\s*(산)?\s*(\d+)(?:\s*-\s*(\d+))?")


def _lot(text: str, pattern: re.Pattern[str]) -> tuple[bool, int, int] | None:
    match = pattern.search(text or "")
    if match is None:
        return None
    return bool(match.group(1)), int(match.group(2)), int(match.group(3) or 0)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _candidate_pnus(
    address: str,
    documents: list[dict[str, Any]],
    legal_dong: LegalDongIndex | None,
) -> list[str]:
    """주소에서 나올 수 있는 PNU 후보(중복 없이, 믿을 만한 순서).

    카카오는 행정동 이름으로 찾은 주소를 여러 법정동으로 돌려준다(정읍 「농소동 14-1」 →
    농소동·용계동·흑암동 14-1). 문서의 법정동명이 주소에 그대로 있는 것을 앞에 둔다.
    """

    candidates: list[str] = []

    def add(pnu: str | None) -> None:
        if pnu and pnu not in candidates:
            candidates.append(pnu)

    if legal_dong is not None:
        assembled = assemble_pnu(address, legal_dong)
        if assembled is not None:
            add(assembled.pnu)
    squashed = _squash(address)
    matched: list[str] = []
    others: list[str] = []
    for document in documents:
        pnu = pnu_from_address_document(document)
        if not pnu:
            continue
        detail = document.get("address") or {}
        dong = _squash(str(detail.get("region_3depth_name") or ""))
        (matched if dong and dong in squashed else others).append(pnu)
    for pnu in matched + others:
        add(pnu)
    return candidates


class AddressParcelResolver:
    """시설 주소로 시설 필지를 찾는다. 실패하면 None(호출부가 좌표 필지로 물러난다)."""

    def __init__(
        self,
        search: AddressSearch | None,
        fetch_by_pnu: PnuParcelFetcher,
        legal_dong: LegalDongIndex | None = None,
        locate: PointLocator | None = None,
        fetch_at: PointParcelFetcher | None = None,
    ) -> None:
        self.search = search
        self.fetch_by_pnu = fetch_by_pnu
        self.legal_dong = legal_dong
        # 카카오 주소검색이 막혔을 때의 대체 경로: 주소 → 좌표(네이버·VWorld) → 그 자리 필지.
        # 찾은 필지의 번지가 주소의 번지와 같을 때만 쓴다(옆 필지로 재지 않게).
        self.locate = locate
        self.fetch_at = fetch_at
        self._cache: dict[str, ParcelFeature | None] = {}

    async def parcel_for(
        self, address: str, near: Coordinates
    ) -> ParcelFeature | None:
        address = (address or "").strip()
        if not address:
            return None
        if address in self._cache:
            return self._accept(self._cache[address], near)
        documents: list[dict[str, Any]] = []
        failed = False
        if self.search is not None and getattr(self.search, "enabled", True):
            try:
                documents = await self.search.address_documents(address)
            except Exception:
                logger.warning("시설 주소검색 실패: %s", address, exc_info=True)
                failed = True
        parcel: ParcelFeature | None = None
        candidates = _candidate_pnus(address, documents, self.legal_dong)
        for pnu in candidates:
            try:
                feature = await self.fetch_by_pnu(pnu)
            except Exception:
                feature = None
            if feature is None:
                failed = True
            if self._accept(feature, near) is not None:
                parcel = feature
                break
        if parcel is None and failed and not documents:
            parcel = await self._by_point(address, near)
        # 필지 조회가 비어 온 것은 지적도 원천 장애일 수 있어 기억하지 않는다(다음 심사에서
        # 다시 찾는다). 필지를 받았는데 좌표와 멀거나 도로라 버린 것은 기억한다.
        if parcel is not None or not failed:
            if len(self._cache) >= ADDRESS_PARCEL_CACHE_MAX:
                self._cache.clear()
            self._cache[address] = parcel
        return parcel

    async def _by_point(self, address: str, near: Coordinates) -> ParcelFeature | None:
        """카카오 없이: 주소를 좌표로 바꿔 그 자리 필지를 받고, 번지가 같을 때만 쓴다."""

        if self.locate is None or self.fetch_at is None:
            return None
        wanted = _lot(address, _TAIL_LOT)
        if wanted is None:
            return None
        try:
            point = await self.locate(address)
            feature = await self.fetch_at(point) if point is not None else None
        except Exception:
            logger.warning("시설 주소 대체 필지 조회 실패: %s", address, exc_info=True)
            return None
        if feature is None or _lot(feature.jibun, _HEAD_LOT) != wanted:
            return None
        return self._accept(feature, near)

    @staticmethod
    def _accept(
        parcel: ParcelFeature | None, near: Coordinates
    ) -> ParcelFeature | None:
        if parcel is None or len(parcel.ring) < 4:
            return None
        if parcel_rejection_reason(parcel.jibun):
            return None
        if distance_point_to_polygon_m(near, parcel.ring) > ADDRESS_PARCEL_MAX_OFFSET_M:
            return None
        return parcel
