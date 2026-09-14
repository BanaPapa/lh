"""사업지 필지 확보 공용 로직.

주소 또는 좌표에서 PNU를 만들고 연속지적도 폴리곤을 받아온다.
유해시설 검토(임시)와 유해시설 확인 두 탭이 같은 기준을 쓰도록 여기서 공유한다.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Protocol

from app.models import Coordinates
from app.services.pnu import build_pnu, pnu_from_address_document
from app.services.vworld import ParcelFeature, VWorldAPIError


class KakaoLike(Protocol):
    async def address_documents(self, query: str) -> list[dict[str, object]]: ...

    async def coord_to_address(
        self, lat: float, lng: float
    ) -> dict[str, object] | None: ...

    async def legal_code(self, lat: float, lng: float) -> str: ...


class VWorldLike(Protocol):
    async def parcel_by_pnu(self, pnu: str) -> ParcelFeature | None: ...


class CadastralLike(Protocol):
    """전북 연속지적도 로컬 조회기(app.services.cadastral_local.CadastralLocalStore).

    좌표·PNU 로 로컬 지적 필지를 돌려준다. 인덱스가 없으면 None 을 돌려주며 예외로
    죽지 않는다(다른 PC 에는 원본이 없다). LocalParcel 은 ParcelFeature 와 동일한
    링·PNU·지번·면적을 담아, 여기서 ParcelFeature 로 그대로 옮겨 실을 수 있다.
    """

    def parcel_at(self, lat: float, lng: float) -> Any | None: ...

    def parcel_by_pnu(self, pnu: str) -> Any | None: ...


def _local_to_feature(local: Any) -> ParcelFeature:
    """CadastralLocalStore 의 LocalParcel 을 앱 공통 ParcelFeature 로 옮긴다."""

    return ParcelFeature(
        pnu=local.pnu,
        address=local.address or "",
        jibun=local.jibun or "",
        ring=local.ring,
        area_m2=local.area_m2,
    )


class LocateResult(NamedTuple):
    """필지 조회 결과. 실패해도 예외 대신 사유를 담아 돌려준다."""

    parcel: ParcelFeature | None
    error: str = ""

    @property
    def ring(self) -> list[Coordinates] | None:
        return self.parcel.ring if self.parcel else None


class SiteParcelLocator:
    def __init__(
        self,
        kakao: KakaoLike,
        vworld: VWorldLike,
        cadastral: CadastralLike | None = None,
    ) -> None:
        self.kakao = kakao
        self.vworld = vworld
        # 전북 연속지적도 로컬 조회기. 인덱스가 없으면 None 처럼 조용히 넘어가고
        # 기존 폴백(임시 필지)으로 물러난다.
        self.cadastral = cadastral

    async def locate(
        self,
        address: str,
        coordinates: Coordinates,
    ) -> LocateResult:
        """후보 PNU를 순서대로 조회해 첫 번째로 실재하는 필지를 돌려준다.

        주소가 만든 지번이 지적도에 없는 경우가 있다. 지하철역 지하 주소처럼
        도로명에 붙은 지번이 실재 필지가 아닌 경우다. 그래서 주소 경로 하나만
        믿지 않고 좌표 경로까지 시도한다.
        """

        # 좌표를 품는 필지가 가장 정확하다. 주소 지번은 자투리·유령 필지로
        # 이어질 수 있어 보조 수단으로만 쓴다.
        parcel_at = getattr(self.vworld, "parcel_at", None)
        if parcel_at is not None:
            try:
                parcel = await parcel_at(coordinates.lat, coordinates.lng)
            except VWorldAPIError:
                parcel = None
            if parcel:
                return LocateResult(parcel)

        # VWorld 좌표 조회의 로컬 폴백. 전북 연속지적도 인덱스가 적재돼 있으면
        # 좌표를 품는 실제 지적 필지를 오프라인으로 돌려준다. 인덱스가 없으면
        # None 이라 조용히 다음 단계(주소 PNU)로 넘어간다.
        local = self._cadastral_at(coordinates)
        if local is not None:
            return LocateResult(local)

        candidates = await self.candidate_pnus(address, coordinates)
        if not candidates:
            return LocateResult(None, "지번 정보로 PNU를 만들지 못했습니다.")

        for pnu in candidates:
            try:
                parcel = await self.vworld.parcel_by_pnu(pnu)
            except VWorldAPIError as exc:
                return LocateResult(None, str(exc))
            if parcel:
                return LocateResult(parcel)

        # VWorld PNU 조회도 실패했을 때의 마지막 로컬 폴백. 임시 필지로 떨어지기
        # 전에 연속지적도 인덱스에서 PNU 로 필지를 찾는다.
        for pnu in candidates:
            local = self._cadastral_by_pnu(pnu)
            if local is not None:
                return LocateResult(local)

        return LocateResult(
            None,
            f"지적도에서 필지를 찾지 못했습니다. (시도한 PNU {len(candidates)}건)",
        )

    def _cadastral_at(self, coordinates: Coordinates) -> ParcelFeature | None:
        if self.cadastral is None:
            return None
        try:
            local = self.cadastral.parcel_at(coordinates.lat, coordinates.lng)
        except Exception:
            # 인덱스 손상·경로 오류 등은 조용히 폴백한다. 다른 PC 엔 원본이 없다.
            return None
        return _local_to_feature(local) if local is not None else None

    def _cadastral_by_pnu(self, pnu: str) -> ParcelFeature | None:
        if self.cadastral is None:
            return None
        try:
            local = self.cadastral.parcel_by_pnu(pnu)
        except Exception:
            return None
        return _local_to_feature(local) if local is not None else None

    async def candidate_pnus(
        self,
        address: str,
        coordinates: Coordinates,
    ) -> list[str]:
        """주소 경로 PNU들, 그다음 좌표 경로 PNU. 중복은 제거하고 순서는 지킨다."""

        candidates: list[str] = []

        def add(pnu: str | None) -> None:
            if pnu and pnu not in candidates:
                candidates.append(pnu)

        if address:
            try:
                documents = await self.kakao.address_documents(address)
            except Exception:
                documents = []
            for document in documents:
                add(pnu_from_address_document(document))

        add(await self._coordinate_pnu(coordinates))
        return candidates

    async def _coordinate_pnu(self, coordinates: Coordinates) -> str | None:
        try:
            resolved = await self.kakao.coord_to_address(
                coordinates.lat,
                coordinates.lng,
            )
            legal_code = await self.kakao.legal_code(
                coordinates.lat,
                coordinates.lng,
            )
        except Exception:
            return None

        if not resolved or not legal_code:
            return None
        main_no = str(resolved.get("main_address_no") or "")
        if not main_no:
            return None
        try:
            return build_pnu(
                legal_code,
                str(resolved.get("mountain_yn") or "N"),
                main_no,
                str(resolved.get("sub_address_no") or ""),
            )
        except ValueError:
            return None
