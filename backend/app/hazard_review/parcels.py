"""신청 필지 확보.

주소 또는 좌표에서 PNU를 만들고, 연속지적도에서 실제 필지 폴리곤을 받아온다.
어느 단계든 실패하면 기존 임시 사각형으로 물러나되, 결과에 그 사실을 남긴다.
"""

from __future__ import annotations

import math
from uuid import uuid4

from app.hazard_review.models import (
    HazardParcel,
    HazardParcelResolveRequest,
    HazardParcelResolveResponse,
)
from app.hazard_review.multi_parcel import parse_multi_parcel_address
from app.models import Coordinates
from app.services.pnu import pnu_from_address_document
from app.services.pnu_resolver import LegalDongIndex, assemble_pnu
from app.services.site_parcel import (
    CadastralLike,
    KakaoLike,
    SiteParcelLocator,
    VWorldLike,
    _local_to_feature,
)
from app.services.vworld import ParcelFeature, VWorldAPIError


PROVISIONAL_HALF_SIZE_M = 18

CADASTRAL_NOTE = (
    "국토교통부 연속지적도에서 받은 필지 경계입니다. 참고도형이므로 최종 배제 "
    "판단 전에는 지적공부와 경계복원측량으로 확인해야 합니다."
)
PROVISIONAL_NOTE = (
    "VWorld 지적도를 사용할 수 없어 주소 좌표 주변의 임시 필지를 사용합니다. "
    "사용자가 확인한 뒤 사전검토 흐름만 실행할 수 있습니다."
)
CADASTRAL_GEOMETRY_NOTE = (
    "연속지적도 기준 대지경계입니다. 도곽 접합 오차가 있어 법적 경계와 다를 수 있습니다."
)
# 다필지 합집합 고지(LH 확정 2026-09-11 #9). 대표필지에 인접 필지를 연계해 합집합
# 경계로 판정하되, 담당자가 필지를 직접 추가·선택할 수 있음을 밝힌다.
MULTI_PARCEL_NOTE = (
    "주소에 복수 지번이 있어 대표필지에 인접 필지를 연계(합집합)했습니다. "
    "담당자가 필지를 추가·선택할 수 있습니다."
)
MULTI_PARCEL_UNRESOLVED_NOTE = (
    "복수 지번 중 일부는 지적도에서 필지를 찾지 못했습니다(대표필지만 확정). "
    "담당자 확인이 필요합니다: "
)
PROVISIONAL_GEOMETRY_NOTE = (
    "주소 좌표 주변에 만든 검증용 임시 형상입니다. "
    "공식 지적도와 PNU가 아니므로 최종 거리판정에 사용할 수 없습니다."
)


def provisional_ring(center: Coordinates) -> list[Coordinates]:
    """주소 좌표 주변의 임시 사각형. 지적도를 못 쓸 때만 사용한다."""

    delta_lat = PROVISIONAL_HALF_SIZE_M / 111_320
    longitude_scale = max(math.cos(math.radians(center.lat)), 0.2)
    delta_lng = PROVISIONAL_HALF_SIZE_M / (111_320 * longitude_scale)
    lat, lng = center.lat, center.lng
    return [
        Coordinates(lat=lat - delta_lat, lng=lng - delta_lng),
        Coordinates(lat=lat - delta_lat, lng=lng + delta_lng),
        Coordinates(lat=lat + delta_lat, lng=lng + delta_lng),
        Coordinates(lat=lat + delta_lat, lng=lng - delta_lng),
        Coordinates(lat=lat - delta_lat, lng=lng - delta_lng),
    ]


class ParcelResolver:
    def __init__(
        self,
        kakao: KakaoLike,
        vworld: VWorldLike,
        cadastral: CadastralLike | None = None,
    ) -> None:
        # cadastral(전북 연속지적도 로컬 인덱스)이 붙어 있으면, VWorld 실패 시에도
        # 임시 필지로 떨어지기 전에 실제 지적경계를 확보한다. 인덱스가 없으면
        # 조용히 기존 provisional 폴백으로 물러난다.
        self.kakao = kakao
        self.vworld = vworld
        self.cadastral = cadastral
        self.locator = SiteParcelLocator(
            kakao=kakao, vworld=vworld, cadastral=cadastral
        )
        # 다필지 인접 지번을 좌표 없이 확정하려면 법정동코드표로 PNU 를 조립한다.
        # 파일이 없으면 None 이라 카카오 주소검색 경로로만 물러난다.
        self._legal_dong = LegalDongIndex.from_env()

    def _feature_to_parcel(
        self, feature: ParcelFeature, fallback_address: str
    ) -> HazardParcel:
        return HazardParcel(
            parcel_id=f"cadastral:{feature.pnu}",
            pnu=feature.pnu,
            address=feature.address or fallback_address,
            area_m2=feature.area_m2,
            geometry=feature.ring,
            geometry_source="parcel_polygon",
            geometry_note=CADASTRAL_GEOMETRY_NOTE,
        )

    async def _feature_for_pnu(self, pnu: str) -> ParcelFeature | None:
        """PNU 로 필지 도형을 받는다. 로컬 지적도 우선, 없으면 VWorld 폴백."""

        if not pnu:
            return None
        if self.cadastral is not None:
            try:
                local = self.cadastral.parcel_by_pnu(pnu)
            except Exception:
                local = None
            if local is not None:
                return _local_to_feature(local)
        try:
            return await self.vworld.parcel_by_pnu(pnu)
        except VWorldAPIError:
            return None

    async def _resolve_additional(self, address: str) -> ParcelFeature | None:
        """인접 지번 하나를 좌표 없이 해석한다(로컬 지적도 우선).

        좌표가 없으므로 주소 → PNU 조립(법정동코드표) 또는 카카오 주소검색으로 PNU 를
        만든 뒤, 로컬 지적도 → VWorld 순으로 필지 도형을 받는다.
        """

        # 후보 PNU 를 순서대로 모은다(로컬 조립 우선, 카카오 주소검색 보조). 어느 하나가
        # 로컬 지적도나 VWorld 에서 실제 필지로 풀리면 그것으로 확정한다.
        candidates: list[str] = []

        def _add(pnu: str | None) -> None:
            if pnu and pnu not in candidates:
                candidates.append(pnu)

        if self._legal_dong is not None:
            assembled = assemble_pnu(address, self._legal_dong)
            if assembled is not None:
                _add(assembled.pnu)
        if self.kakao is not None:
            try:
                documents = await self.kakao.address_documents(address)
            except Exception:
                documents = []
            for document in documents:
                _add(pnu_from_address_document(document))

        for pnu in candidates:
            feature = await self._feature_for_pnu(pnu)
            if feature is not None:
                return feature
        return None

    async def resolve(
        self,
        request: HazardParcelResolveRequest,
    ) -> HazardParcelResolveResponse:
        located = await self.locator.locate(request.address, request.coordinates)
        parcel = located.parcel
        # PNU 조립 실패는 사용자에게 알릴 내용이 아니므로 VWorld 오류만 노출한다.
        failure_note = located.error if parcel is None and "PNU" not in located.error else ""

        if parcel:
            parcels = [self._feature_to_parcel(parcel, request.address)]
            note = CADASTRAL_NOTE
            # 다필지 합집합(#9) — 대표필지 외 지번을 좌표 없이 추가 해석한다.
            parsed = parse_multi_parcel_address(request.address)
            seen_pnus = {parcel.pnu}
            unresolved: list[str] = []
            if parsed.is_multi:
                # 좌표로 잡은 대표필지가 목록의 첫 지번이라는 보장이 없다(좌표가
                # 두 번째 지번 필지에 떨어질 수 있다). 그래서 목록을 [1:] 로 건너뛰지
                # 않고 전부 해석한 뒤 PNU 로 중복을 제거한다. 대표필지(이미 parcels
                # 첫 항목)의 지번은 같은 PNU 로 풀려 seen_pnus 에서 걸러지므로, 첫
                # 지번이 대표가 아니어도 결과에서 누락되지 않는다.
                for extra_address in parsed.jibun_addresses:
                    feature = await self._resolve_additional(extra_address)
                    if feature is None:
                        unresolved.append(extra_address)
                        continue
                    if feature.pnu in seen_pnus:
                        # 대표필지이거나 이미 실은 필지 — 중복으로 싣지 않는다.
                        continue
                    seen_pnus.add(feature.pnu)
                    parcels.append(self._feature_to_parcel(feature, extra_address))
                note = f"{CADASTRAL_NOTE} {MULTI_PARCEL_NOTE}"
                if unresolved:
                    note = f"{note} {MULTI_PARCEL_UNRESOLVED_NOTE}{', '.join(unresolved)}"
            elif parsed.note:
                note = f"{CADASTRAL_NOTE} {parsed.note}"
            return HazardParcelResolveResponse(
                parcels=parcels,
                provisional=False,
                note=note,
            )

        note = PROVISIONAL_NOTE
        if failure_note:
            note = f"{failure_note} {PROVISIONAL_NOTE}"
        return HazardParcelResolveResponse(
            parcels=[
                HazardParcel(
                    parcel_id=f"provisional:{uuid4()}",
                    pnu="확인 필요",
                    address=request.address,
                    area_m2=float((PROVISIONAL_HALF_SIZE_M * 2) ** 2),
                    geometry=provisional_ring(request.coordinates),
                    geometry_source="provisional_polygon",
                    geometry_note=PROVISIONAL_GEOMETRY_NOTE,
                )
            ],
            provisional=True,
            note=note,
        )
