from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.hazard_review.models import HazardParcelResolveRequest
from app.hazard_review.parcels import ParcelResolver
from app.models import Coordinates
from app.services.geo import offset_coordinates, polygon_area_m2
from app.services.vworld import ParcelFeature, VWorldAPIError


SITE = Coordinates(lat=37.5679747787287, lng=127.182079485736)

ADDRESS_DOCUMENT = {
    "address": {
        "address_name": "경기 하남시 망월동 1034",
        "b_code": "4145010900",
        "mountain_yn": "N",
        "main_address_no": "1034",
        "sub_address_no": "",
    }
}


def square(center: Coordinates, half_size_m: float) -> list[Coordinates]:
    return [
        offset_coordinates(center, -half_size_m, -half_size_m),
        offset_coordinates(center, -half_size_m, half_size_m),
        offset_coordinates(center, half_size_m, half_size_m),
        offset_coordinates(center, half_size_m, -half_size_m),
        offset_coordinates(center, -half_size_m, -half_size_m),
    ]


CADASTRAL_RING = square(SITE, 30)


class FakeKakao:
    def __init__(
        self,
        address_documents: list[dict[str, Any]] | None = None,
        coord_address: dict[str, Any] | None = None,
        legal_code: str = "",
    ) -> None:
        self._address_documents = address_documents or []
        self._coord_address = coord_address
        self._legal_code = legal_code
        self.address_queries: list[str] = []

    async def address_documents(self, query: str) -> list[dict[str, Any]]:
        self.address_queries.append(query)
        return self._address_documents

    async def coord_to_address(self, lat: float, lng: float) -> dict[str, Any] | None:
        return self._coord_address

    async def legal_code(self, lat: float, lng: float) -> str:
        return self._legal_code


class FakeVWorld:
    def __init__(
        self,
        parcel: ParcelFeature | None = None,
        error: Exception | None = None,
    ) -> None:
        self.parcel = parcel
        self.error = error
        self.requested_pnus: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def parcel_by_pnu(self, pnu: str) -> ParcelFeature | None:
        self.requested_pnus.append(pnu)
        if self.error:
            raise self.error
        return self.parcel


def cadastral_parcel() -> ParcelFeature:
    return ParcelFeature(
        pnu="4145010900110340000",
        address="경기도 하남시 망월동 1034",
        jibun="1034대",
        ring=CADASTRAL_RING,
        area_m2=polygon_area_m2(CADASTRAL_RING),
    )


def resolve(kakao: Any, vworld: Any, address: str = "경기 하남시 아리수로 499"):
    resolver = ParcelResolver(kakao=kakao, vworld=vworld)
    request = HazardParcelResolveRequest(
        name="검증 사업지",
        address=address,
        coordinates=SITE,
    )
    return asyncio.run(resolver.resolve(request))


class TestCadastralSuccess:
    def test_address_path_returns_a_real_cadastral_parcel(self) -> None:
        vworld = FakeVWorld(cadastral_parcel())

        response = resolve(FakeKakao([ADDRESS_DOCUMENT]), vworld)

        parcel = response.parcels[0]
        assert response.provisional is False
        assert parcel.geometry_source == "parcel_polygon"
        assert parcel.pnu == "4145010900110340000"
        assert parcel.address == "경기도 하남시 망월동 1034"
        assert parcel.area_m2 == pytest.approx(3_600, rel=0.02)
        assert len(parcel.geometry) >= 4
        assert vworld.requested_pnus == ["4145010900110340000"]

    def test_coordinate_path_is_used_when_address_search_is_empty(self) -> None:
        """역지오코딩은 법정동코드를 주지 않아 coord2regioncode 를 함께 써야 한다."""

        kakao = FakeKakao(
            address_documents=[],
            coord_address={
                "address_name": "경기 하남시 망월동 1034",
                "mountain_yn": "N",
                "main_address_no": "1034",
                "sub_address_no": "",
            },
            legal_code="4145010900",
        )
        vworld = FakeVWorld(cadastral_parcel())

        response = resolve(kakao, vworld)

        assert response.provisional is False
        assert vworld.requested_pnus == ["4145010900110340000"]


class TestProvisionalFallback:
    def test_falls_back_when_vworld_has_no_parcel(self) -> None:
        response = resolve(FakeKakao([ADDRESS_DOCUMENT]), FakeVWorld(None))

        parcel = response.parcels[0]
        assert response.provisional is True
        assert parcel.geometry_source == "provisional_polygon"
        assert len(parcel.geometry) == 5

    def test_falls_back_when_vworld_errors(self) -> None:
        vworld = FakeVWorld(error=VWorldAPIError("인증키 오류"))

        response = resolve(FakeKakao([ADDRESS_DOCUMENT]), vworld)

        assert response.provisional is True
        assert response.parcels[0].geometry_source == "provisional_polygon"
        assert "인증키" in response.note or "지적" in response.note

    def test_falls_back_when_pnu_cannot_be_built(self) -> None:
        kakao = FakeKakao(address_documents=[], coord_address=None, legal_code="")
        vworld = FakeVWorld(cadastral_parcel())

        response = resolve(kakao, vworld)

        assert response.provisional is True
        assert vworld.requested_pnus == []

    def test_note_explains_which_source_was_used(self) -> None:
        cadastral = resolve(FakeKakao([ADDRESS_DOCUMENT]), FakeVWorld(cadastral_parcel()))
        provisional = resolve(FakeKakao([ADDRESS_DOCUMENT]), FakeVWorld(None))

        assert cadastral.note != provisional.note
        assert "연속지적도" in cadastral.note


# ---------------------------------------------------------------------------
# 다필지 합집합(LH 확정 2026-09-11 #9) — 대표필지 + 인접 필지 연계
# ---------------------------------------------------------------------------
def _doc(b_code: str, main_no: str) -> dict[str, Any]:
    return {
        "address": {
            "b_code": b_code,
            "mountain_yn": "N",
            "main_address_no": main_no,
            "sub_address_no": "",
        }
    }


class AddressMappedKakao:
    """주소 문자열의 지번에 따라 서로 다른 PNU 문서를 돌려주는 대역."""

    def __init__(self, b_code: str) -> None:
        self._b_code = b_code
        self.address_queries: list[str] = []

    async def address_documents(self, query: str) -> list[dict[str, Any]]:
        self.address_queries.append(query)
        import re

        # 주소의 첫 지번 숫자를 본번으로 본다(대표필지는 '1243, 1244' 의 1243).
        match = re.search(r"(\d+)", query)
        main_no = match.group(1) if match else "1243"
        return [_doc(self._b_code, main_no)]

    async def coord_to_address(self, lat: float, lng: float) -> dict[str, Any] | None:
        return None

    async def legal_code(self, lat: float, lng: float) -> str:
        return ""


class PnuMappedVWorld:
    """PNU 별로 다른 필지를 돌려주는 대역."""

    def __init__(self, parcels: dict[str, ParcelFeature]) -> None:
        self._parcels = parcels
        self.requested_pnus: list[str] = []

    @property
    def enabled(self) -> bool:
        return True

    async def parcel_by_pnu(self, pnu: str) -> ParcelFeature | None:
        self.requested_pnus.append(pnu)
        return self._parcels.get(pnu)


class _PnuMappedVWorldWithCoord(PnuMappedVWorld):
    """좌표가 특정 필지에 떨어지는 상황을 만드는 대역(parcel_at 지원).

    locator 는 VWorld 에 parcel_at 이 있으면 좌표 기준 필지를 먼저 쓴다. 이 대역은
    좌표로 잡히는 대표필지를 목록의 두 번째 지번으로 둘 수 있게 한다.
    """

    def __init__(
        self,
        parcels: dict[str, ParcelFeature],
        coord_parcel: ParcelFeature,
    ) -> None:
        super().__init__(parcels)
        self._coord_parcel = coord_parcel

    async def parcel_at(self, lat: float, lng: float) -> ParcelFeature | None:
        return self._coord_parcel


class TestMultiParcelUnion:
    B_CODE = "5211025000"

    def _feature(self, main_no: str, center: Coordinates) -> ParcelFeature:
        pnu = f"{self.B_CODE}1{int(main_no):04d}0000"
        ring = square(center, 20)
        return ParcelFeature(
            pnu=pnu,
            address=f"전북특별자치도 완주군 봉동읍 제내리 {main_no}",
            jibun=f"{main_no}답",
            ring=ring,
            area_m2=polygon_area_m2(ring),
        )

    def test_representative_plus_adjacent_parcel_are_both_resolved(self) -> None:
        rep = self._feature("1243", SITE)
        adjacent = self._feature("1244", offset_coordinates(SITE, 0, 40))
        vworld = PnuMappedVWorld({rep.pnu: rep, adjacent.pnu: adjacent})
        resolver = ParcelResolver(kakao=AddressMappedKakao(self.B_CODE), vworld=vworld)
        request = HazardParcelResolveRequest(
            name="다필지 사업지",
            address="전북특별자치도 완주군 봉동읍 제내리 1243, 1244",
            coordinates=SITE,
        )

        response = asyncio.run(resolver.resolve(request))

        assert response.provisional is False
        pnus = [p.pnu for p in response.parcels]
        assert pnus == [rep.pnu, adjacent.pnu]  # 대표필지가 첫 항목
        assert all(p.geometry_source == "parcel_polygon" for p in response.parcels)
        assert "합집합" in response.note

    def test_first_jibun_is_kept_when_coordinate_falls_on_the_second(self) -> None:
        """좌표가 두 번째 지번 필지에 떨어져도 첫 지번이 누락되지 않는다.

        좌표로 잡은 대표필지가 목록의 첫 지번이라는 보장이 없다. 목록을 [1:] 로
        건너뛰면 첫 지번이 통째로 빠진다(회귀). 목록 전부를 해석하고 PNU 로 중복
        제거하되 대표필지를 첫 항목으로 둔다.
        """

        first = self._feature("1243", offset_coordinates(SITE, 0, 40))
        rep = self._feature("1244", SITE)  # 좌표가 떨어지는 대표필지 = 두 번째 지번
        vworld = _PnuMappedVWorldWithCoord(
            {first.pnu: first, rep.pnu: rep}, coord_parcel=rep
        )
        resolver = ParcelResolver(kakao=AddressMappedKakao(self.B_CODE), vworld=vworld)
        request = HazardParcelResolveRequest(
            name="다필지 사업지",
            address="전북특별자치도 완주군 봉동읍 제내리 1243, 1244",
            coordinates=SITE,
        )

        response = asyncio.run(resolver.resolve(request))

        assert response.provisional is False
        pnus = [p.pnu for p in response.parcels]
        assert pnus[0] == rep.pnu  # 대표필지(좌표로 잡은 것)가 첫 항목
        assert first.pnu in pnus  # 첫 지번도 결과에 포함(누락 방지)
        assert set(pnus) == {rep.pnu, first.pnu}
        assert "합집합" in response.note

    def test_oe_n_without_list_resolves_representative_only_with_note(self) -> None:
        rep = self._feature("1243", SITE)
        vworld = PnuMappedVWorld({rep.pnu: rep})
        resolver = ParcelResolver(kakao=AddressMappedKakao(self.B_CODE), vworld=vworld)
        request = HazardParcelResolveRequest(
            name="다필지 사업지",
            address="전북특별자치도 완주군 봉동읍 제내리 1243 외 4필지",
            coordinates=SITE,
        )

        response = asyncio.run(resolver.resolve(request))

        assert response.provisional is False
        assert [p.pnu for p in response.parcels] == [rep.pnu]
        assert "담당자 추가 선택" in response.note
