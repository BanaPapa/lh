from __future__ import annotations

import asyncio
from typing import Any

from app.models import Coordinates
from app.services.geo import offset_coordinates
from app.services.site_parcel import SiteParcelLocator
from app.services.vworld import ParcelFeature, VWorldAPIError


SITE = Coordinates(lat=37.3948, lng=127.1112)

# 판교역 지하 주소에서 나오는 PNU. 지적도에 존재하지 않는다.
ADDRESS_PNU = "4113511000100190000"
# 같은 좌표의 역지오코딩에서 나오는 PNU. 실재하는 필지다.
COORD_PNU = "4113511000105320001"


def ring() -> list[Coordinates]:
    return [
        offset_coordinates(SITE, -20, -20),
        offset_coordinates(SITE, -20, 20),
        offset_coordinates(SITE, 20, 20),
        offset_coordinates(SITE, 20, -20),
        offset_coordinates(SITE, -20, -20),
    ]


def parcel(pnu: str) -> ParcelFeature:
    return ParcelFeature(
        pnu=pnu,
        address="경기도 성남시 분당구 백현동 532-1",
        jibun="532-1대",
        ring=ring(),
        area_m2=1600.0,
    )


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

    async def address_documents(self, query: str) -> list[dict[str, Any]]:
        return self._address_documents

    async def coord_to_address(self, lat: float, lng: float) -> dict[str, Any] | None:
        return self._coord_address

    async def legal_code(self, lat: float, lng: float) -> str:
        return self._legal_code


class FakeVWorld:
    def __init__(self, parcels_by_pnu: dict[str, ParcelFeature]) -> None:
        self.parcels_by_pnu = parcels_by_pnu
        self.requested: list[str] = []

    async def parcel_by_pnu(self, pnu: str) -> ParcelFeature | None:
        self.requested.append(pnu)
        return self.parcels_by_pnu.get(pnu)


class RaisingVWorld:
    def __init__(self) -> None:
        self.requested: list[str] = []

    async def parcel_by_pnu(self, pnu: str) -> ParcelFeature | None:
        self.requested.append(pnu)
        raise VWorldAPIError("인증키 오류")


def pangyo_kakao() -> FakeKakao:
    return FakeKakao(
        address_documents=[
            {
                "address": {
                    "b_code": "4113511000",
                    "mountain_yn": "N",
                    "main_address_no": "19",
                    "sub_address_no": "",
                }
            }
        ],
        coord_address={
            "mountain_yn": "N",
            "main_address_no": "532",
            "sub_address_no": "1",
        },
        legal_code="4113511000",
    )


def locate(kakao: Any, vworld: Any, address: str = "경기 성남시 분당구 판교역로 지하 160"):
    locator = SiteParcelLocator(kakao=kakao, vworld=vworld)
    return asyncio.run(locator.locate(address, SITE))


class TestCandidateFallthrough:
    """주소로 만든 PNU가 지적도에 없으면 좌표로 만든 PNU까지 시도해야 한다."""

    def test_falls_through_to_the_coordinate_pnu(self) -> None:
        vworld = FakeVWorld({COORD_PNU: parcel(COORD_PNU)})

        result = locate(pangyo_kakao(), vworld)

        assert result.parcel is not None
        assert result.parcel.pnu == COORD_PNU
        assert vworld.requested == [ADDRESS_PNU, COORD_PNU]

    def test_stops_at_the_first_hit(self) -> None:
        vworld = FakeVWorld({ADDRESS_PNU: parcel(ADDRESS_PNU)})

        result = locate(pangyo_kakao(), vworld)

        assert result.parcel is not None
        assert vworld.requested == [ADDRESS_PNU]

    def test_reports_failure_when_no_candidate_matches(self) -> None:
        vworld = FakeVWorld({})

        result = locate(pangyo_kakao(), vworld)

        assert result.parcel is None
        assert result.error
        assert vworld.requested == [ADDRESS_PNU, COORD_PNU]

    def test_duplicate_candidates_are_queried_once(self) -> None:
        kakao = FakeKakao(
            address_documents=[
                {
                    "address": {
                        "b_code": "4113511000",
                        "mountain_yn": "N",
                        "main_address_no": "532",
                        "sub_address_no": "1",
                    }
                }
            ],
            coord_address={
                "mountain_yn": "N",
                "main_address_no": "532",
                "sub_address_no": "1",
            },
            legal_code="4113511000",
        )
        vworld = FakeVWorld({})

        locate(kakao, vworld)

        assert vworld.requested == [COORD_PNU]

    def test_vworld_error_stops_and_reports(self) -> None:
        vworld = RaisingVWorld()

        result = locate(pangyo_kakao(), vworld)

        assert result.parcel is None
        assert "인증키" in result.error

    def test_no_candidate_at_all_reports_pnu_failure(self) -> None:
        kakao = FakeKakao(address_documents=[], coord_address=None, legal_code="")
        vworld = FakeVWorld({})

        result = locate(kakao, vworld)

        assert result.parcel is None
        assert "PNU" in result.error
        assert vworld.requested == []


class FakeVWorldWithPoint:
    """좌표 조회와 PNU 조회를 따로 스크립트한다."""

    def __init__(
        self,
        by_point: ParcelFeature | None = None,
        by_pnu: dict[str, ParcelFeature] | None = None,
    ) -> None:
        self._by_point = by_point
        self._by_pnu = by_pnu or {}
        self.point_calls: list[tuple[float, float]] = []
        self.pnu_calls: list[str] = []

    async def parcel_at(self, lat: float, lng: float) -> ParcelFeature | None:
        self.point_calls.append((lat, lng))
        return self._by_point

    async def parcel_by_pnu(self, pnu: str) -> ParcelFeature | None:
        self.pnu_calls.append(pnu)
        return self._by_pnu.get(pnu)


class _LocalParcelStub:
    """CadastralLocalStore.LocalParcel 을 대신하는 최소 스텁(ring·pnu·jibun·area)."""

    def __init__(self, pnu: str) -> None:
        self.pnu = pnu
        self.jibun = "532-1 대"
        self.address = ""
        self.ring = ring()
        self.area_m2 = 1600.0


class FakeCadastral:
    """전북 연속지적도 로컬 인덱스 스텁. 좌표/PNU 로 로컬 필지를 돌려준다."""

    def __init__(
        self,
        by_point: _LocalParcelStub | None = None,
        by_pnu: dict[str, _LocalParcelStub] | None = None,
    ) -> None:
        self._by_point = by_point
        self._by_pnu = by_pnu or {}
        self.point_calls: list[tuple[float, float]] = []
        self.pnu_calls: list[str] = []

    def parcel_at(self, lat: float, lng: float) -> _LocalParcelStub | None:
        self.point_calls.append((lat, lng))
        return self._by_point

    def parcel_by_pnu(self, pnu: str) -> _LocalParcelStub | None:
        self.pnu_calls.append(pnu)
        return self._by_pnu.get(pnu)


def locate_with_cadastral(kakao, vworld, cadastral, address="경기 성남시 분당구 판교역로 지하 160"):
    locator = SiteParcelLocator(kakao=kakao, vworld=vworld, cadastral=cadastral)
    return asyncio.run(locator.locate(address, SITE))


class TestCadastralLocalFallback:
    """연속지적도 로컬 인덱스는 VWorld 실패 시 실제 지적경계를 돌려준다.

    인덱스가 없으면(cadastral=None) 조용히 기존 폴백으로 물러나야 한다.
    """

    def test_cadastral_point_lookup_fills_when_vworld_has_no_parcel(self) -> None:
        vworld = FakeVWorldWithPoint(by_point=None, by_pnu={})
        cadastral = FakeCadastral(by_point=_LocalParcelStub("5211111111100010000"))

        result = locate_with_cadastral(pangyo_kakao(), vworld, cadastral)

        assert result.parcel is not None
        assert result.parcel.pnu == "5211111111100010000"
        # 좌표 로컬 폴백이 먼저 성립하므로 VWorld PNU 조회는 하지 않는다.
        assert vworld.pnu_calls == []
        assert cadastral.point_calls == [(SITE.lat, SITE.lng)]

    def test_cadastral_pnu_lookup_fills_after_vworld_pnu_miss(self) -> None:
        vworld = FakeVWorldWithPoint(by_point=None, by_pnu={})  # VWorld 전부 미스
        cadastral = FakeCadastral(
            by_point=None,
            by_pnu={COORD_PNU: _LocalParcelStub(COORD_PNU)},
        )

        result = locate_with_cadastral(pangyo_kakao(), vworld, cadastral)

        assert result.parcel is not None
        assert result.parcel.pnu == COORD_PNU
        # 좌표 로컬 미스 → VWorld PNU 조회 → 로컬 PNU 폴백 순서.
        assert vworld.pnu_calls == [ADDRESS_PNU, COORD_PNU]
        assert COORD_PNU in cadastral.pnu_calls

    def test_missing_cadastral_index_falls_back_to_provisional_path(self) -> None:
        # 인덱스가 없으면(cadastral=None) 배선 전과 동일하게 실패를 보고한다.
        vworld = FakeVWorldWithPoint(by_point=None, by_pnu={})
        result = locate_with_cadastral(pangyo_kakao(), vworld, cadastral=None)
        assert result.parcel is None

    def test_cadastral_errors_are_swallowed_to_fallback(self) -> None:
        class Raising(FakeCadastral):
            def parcel_at(self, lat: float, lng: float):
                raise RuntimeError("인덱스 손상")

        vworld = FakeVWorldWithPoint(
            by_point=None, by_pnu={ADDRESS_PNU: parcel(ADDRESS_PNU)}
        )
        result = locate_with_cadastral(pangyo_kakao(), vworld, Raising())
        # 로컬 조회가 터져도 죽지 않고 VWorld PNU 경로로 넘어간다.
        assert result.parcel is not None
        assert result.parcel.pnu == ADDRESS_PNU


class TestCoordinateFirstLookup:
    """주소 지번은 자투리·유령 필지로 이어질 수 있어 좌표 조회를 먼저 쓴다."""

    def test_coordinate_lookup_wins_over_the_address_pnu(self) -> None:
        station = parcel("1168011500107230000")
        vworld = FakeVWorldWithPoint(
            by_point=station,
            by_pnu={ADDRESS_PNU: parcel(ADDRESS_PNU)},
        )

        result = locate(pangyo_kakao(), vworld)

        assert result.parcel is not None
        assert result.parcel.pnu == "1168011500107230000"
        assert vworld.point_calls == [(SITE.lat, SITE.lng)]
        # 좌표로 찾았으면 PNU 조회는 하지 않는다.
        assert vworld.pnu_calls == []

    def test_falls_back_to_pnu_when_no_parcel_covers_the_point(self) -> None:
        vworld = FakeVWorldWithPoint(
            by_point=None,
            by_pnu={COORD_PNU: parcel(COORD_PNU)},
        )

        result = locate(pangyo_kakao(), vworld)

        assert result.parcel is not None
        assert result.parcel.pnu == COORD_PNU
        assert vworld.pnu_calls == [ADDRESS_PNU, COORD_PNU]

    def test_point_lookup_error_falls_back_instead_of_failing(self) -> None:
        class Raising(FakeVWorldWithPoint):
            async def parcel_at(self, lat: float, lng: float):
                raise VWorldAPIError("일시 오류")

        vworld = Raising(by_pnu={ADDRESS_PNU: parcel(ADDRESS_PNU)})

        result = locate(pangyo_kakao(), vworld)

        assert result.parcel is not None
        assert result.parcel.pnu == ADDRESS_PNU
