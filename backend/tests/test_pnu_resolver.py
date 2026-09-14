"""PNU 확정 파이프라인 회귀 테스트 (설계서 §7.2).

경계 케이스: 산 표기·부번 없음·도로명만 / 후보 2개 이상이면 미확정 / 조립↔공간조인
불일치 시 미확정 / 실패 사유 3종 분류 / 확정 경로 기록.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.models import Coordinates
from app.services.cadastral_local import CadastralLocalStore
from app.services.pnu_resolver import (
    FAIL_BONBUN_NOT_IN_CADASTRAL,
    FAIL_JIBUN_NOT_IN_CADASTRAL,
    FAIL_NO_JIBUN,
    METHOD_ADDRESS_ASSEMBLY,
    METHOD_CADASTRAL_REPRESENTATIVE_POINT,
    METHOD_COORDINATE_SPATIAL_JOIN,
    METHOD_INPUT_PNU,
    REVIEW_ASSEMBLY_SPATIAL_MISMATCH,
    REVIEW_COORDINATE_OUTSIDE_PARCEL,
    REVIEW_INPUT_PNU_MISMATCH,
    REVIEW_MULTIPLE_PARCELS,
    LegalDongIndex,
    PnuResolver,
    assemble_pnu,
    normalize_address,
    resolve_records,
    validate_pnu,
)


# ── 픽스처: 소형 법정동 색인 / 가짜 지적도 ────────────────────────────────────

LEGAL_ENTRIES = [
    ("5211112800", "전북특별자치도", "전주시완산구", "중화산동2가", ""),
    ("5213014600", "전북특별자치도", "군산시", "소룡동", ""),
    ("5221034031", "전북특별자치도", "김제시", "용지면", "용수리"),
]


@pytest.fixture
def legal_dong() -> LegalDongIndex:
    return LegalDongIndex(LEGAL_ENTRIES)


class _FakeParcel:
    def __init__(self, pnu: str, ring=None):
        self.pnu = pnu
        self.ring = ring or []


class _FakeCadastral:
    """PnuResolver 가 요구하는 지적도 인터페이스의 가짜 구현."""

    def __init__(self, existing, point_parcels=None, rep=None):
        self.existing = set(existing)
        self.point_parcels = point_parcels or {}
        self.rep = rep or {}

    def parcel_by_pnu(self, pnu):
        return _FakeParcel(pnu) if pnu in self.existing else None

    def sole_parcel_at(self, lat, lng):
        pnus = self.point_parcels.get((round(lat, 6), round(lng, 6)), [])
        if len(pnus) == 1:
            return _FakeParcel(pnus[0]), 1
        return None, len(pnus)

    def representative_point(self, parcel):
        return self.rep.get(parcel.pnu, Coordinates(lat=35.5, lng=127.0))

    def pnu_prefix_exists(self, prefix):
        return any(p.startswith(prefix) for p in self.existing)


# ── 정규화·검증 단위 ─────────────────────────────────────────────────────────


def test_normalize_address_maps_old_names():
    assert normalize_address("전라북도 전주시").startswith("전북특별자치도")
    assert normalize_address("전북 군산시 소룡동").startswith("전북특별자치도 ")


def test_validate_pnu_requires_19_digits():
    assert validate_pnu("5211112800106350007") == "5211112800106350007"
    assert validate_pnu(" 5211112800106350007 ") == "5211112800106350007"
    assert validate_pnu("52111128001063500") == ""  # 17자리
    assert validate_pnu("5211112800X06350007") == ""  # 숫자 아님
    assert validate_pnu("") == ""


# ── 조립 경계 케이스: 산 표기·부번 없음·리·도로명 ────────────────────────────


def test_assemble_general_bonbun_bubun(legal_dong):
    a = assemble_pnu("전북특별자치도 전주시 완산구 중화산동2가 635-7번지 2층", legal_dong)
    assert a is not None
    assert a.pnu == "5211112800106350007"
    assert a.is_mountain is False
    assert (a.bonbun, a.bubun) == (635, 7)


def test_assemble_mountain_flag(legal_dong):
    a = assemble_pnu("전북특별자치도 군산시 소룡동 산 12-3", legal_dong)
    assert a is not None
    # 대지구분 2(산): 5213014600 + 2 + 0012 + 0003
    assert a.pnu == "5213014600200120003"
    assert a.is_mountain is True


def test_assemble_without_bubun_pads_zero(legal_dong):
    a = assemble_pnu("전북특별자치도 군산시 소룡동 100", legal_dong)
    assert a is not None
    assert a.pnu == "5213014600101000000"
    assert (a.bonbun, a.bubun) == (100, 0)


def test_assemble_ri_level(legal_dong):
    a = assemble_pnu("전북특별자치도 김제시 용지면 용수리 667-3번지", legal_dong)
    assert a is not None
    assert a.pnu == "5221034031106670003"


def test_assemble_road_address_returns_none(legal_dong):
    # 도로명주소는 법정동을 못 찾아 조립 불가.
    assert assemble_pnu("전북특별자치도 전주시 완산구 백제대로 100", legal_dong) is None


def test_longest_match_picks_specific_dong(legal_dong):
    # 시군구는 공백 없이("전주시완산구") 저장되지만 주소는 공백을 넣는다("전주시 완산구").
    a = assemble_pnu("전북특별자치도 전주시 완산구 중화산동2가 1", legal_dong)
    assert a is not None and a.pnu.startswith("5211112800")


# ── 확정 경로 ─────────────────────────────────────────────────────────────────


def test_confirm_input_pnu(legal_dong):
    cad = _FakeCadastral(existing={"5213014600101000000"})
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(pnu="5213014600101000000", address="", coordinates=None)
    assert res.confirmed
    assert res.method == METHOD_INPUT_PNU
    assert res.pnu == "5213014600101000000"


def test_confirm_address_assembly_matches_spatial(legal_dong):
    pnu = "5211112800106350007"
    coords = Coordinates(lat=35.81, lng=127.11)
    cad = _FakeCadastral(
        existing={pnu},
        point_parcels={(35.81, 127.11): [pnu]},
    )
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(
        address="전북특별자치도 전주시 완산구 중화산동2가 635-7", coordinates=coords
    )
    assert res.confirmed
    assert res.method == METHOD_ADDRESS_ASSEMBLY
    assert res.pnu == pnu


def test_confirm_coordinate_spatial_join_without_assembly(legal_dong):
    # 조립 불가(도로명)지만 좌표가 단일 필지에 포함 → 그 필지로 확정.
    join_pnu = "5213014600101230000"
    coords = Coordinates(lat=35.97, lng=126.68)
    cad = _FakeCadastral(existing=set(), point_parcels={(35.97, 126.68): [join_pnu]})
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(address="전북특별자치도 군산시 산단로 5", coordinates=coords)
    assert res.confirmed
    assert res.method == METHOD_COORDINATE_SPATIAL_JOIN
    assert res.pnu == join_pnu


def test_confirm_representative_point_without_coords(legal_dong):
    pnu = "5221034031106670003"
    rep = Coordinates(lat=35.79, lng=126.88)
    cad = _FakeCadastral(existing={pnu}, rep={pnu: rep})
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(
        address="전북특별자치도 김제시 용지면 용수리 667-3", coordinates=None
    )
    assert res.confirmed
    assert res.method == METHOD_CADASTRAL_REPRESENTATIVE_POINT
    assert res.pnu == pnu
    assert res.coordinates == rep


# ── 미확정: 후보 2개 이상 / 불일치 ────────────────────────────────────────────


def test_review_when_multiple_parcels(legal_dong):
    coords = Coordinates(lat=35.81, lng=127.11)
    cad = _FakeCadastral(
        existing={"5211112800106350007"},
        point_parcels={(35.81, 127.11): ["5211112800106350007", "5211112800106350008"]},
    )
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(
        address="전북특별자치도 전주시 완산구 중화산동2가 635-7", coordinates=coords
    )
    assert res.status == "review"
    assert res.reason == REVIEW_MULTIPLE_PARCELS
    assert res.pnu == ""  # 확정하지 않는다


def test_review_when_assembly_spatial_mismatch(legal_dong):
    assembled = "5211112800106350007"
    other = "5211112800109990000"
    coords = Coordinates(lat=35.81, lng=127.11)
    cad = _FakeCadastral(
        existing={assembled, other},
        point_parcels={(35.81, 127.11): [other]},
    )
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(
        address="전북특별자치도 전주시 완산구 중화산동2가 635-7", coordinates=coords
    )
    assert res.status == "review"
    assert res.reason == REVIEW_ASSEMBLY_SPATIAL_MISMATCH
    assert res.pnu == ""


def test_review_when_input_pnu_conflicts_spatial(legal_dong):
    input_pnu = "5213014600101000000"
    other = "5213014600109990000"
    coords = Coordinates(lat=35.97, lng=126.68)
    cad = _FakeCadastral(
        existing={input_pnu, other},
        point_parcels={(35.97, 126.68): [other]},
    )
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(pnu=input_pnu, coordinates=coords)
    assert res.status == "review"
    assert res.reason == REVIEW_INPUT_PNU_MISMATCH


def test_review_when_coordinate_outside_any_parcel_despite_real_assembly(legal_dong):
    # Codex 지적 4번 회귀: 좌표가 있는데 그 좌표가 지적도 상 어떤 필지에도 포함되지
    # 않으면(공간조인 0건), 조립 PNU 가 지적도에 실재하더라도 대표점으로 확정하지
    # 않는다. 주소 오타가 우연히 실재 필지로 조립되면 좌표와 모순된 채 그 필지가
    # 조용히 붙을 수 있으므로, 이 경우는 확정하지 않고 검토대상으로 분리한다.
    pnu = "5211112800106350007"
    # point_parcels 에 이 좌표 항목이 없으므로 sole_parcel_at() 은 (None, 0) 을
    # 돌려준다 — 공간조인 0건을 재현한다.
    coords = Coordinates(lat=35.99, lng=127.99)
    cad = _FakeCadastral(existing={pnu}, point_parcels={})
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(
        address="전북특별자치도 전주시 완산구 중화산동2가 635-7", coordinates=coords
    )
    assert res.status == "review"
    assert res.reason == REVIEW_COORDINATE_OUTSIDE_PARCEL
    assert res.confirmed is False
    assert res.pnu == ""  # 대표점으로 확정하지 않는다
    assert res.method != METHOD_CADASTRAL_REPRESENTATIVE_POINT


# ── 실패 사유 3종 (설계서 §7.2 ⑥) ───────────────────────────────────────────


def test_fail_no_jibun_for_road_address(legal_dong):
    cad = _FakeCadastral(existing=set())
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(address="전북특별자치도 전주시 완산구 백제대로 100", coordinates=None)
    assert res.status == "failed"
    assert res.reason == FAIL_NO_JIBUN
    assert res.action == "지번주소 확보"


def test_fail_jibun_not_in_cadastral_same_bonbun_other_bubun(legal_dong):
    # 조립 PNU(부번 0007)는 없지만 같은 본번 다른 부번(0001)이 실재 → 지번 변경·합병.
    cad = _FakeCadastral(existing={"5211112800106350001"})
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(
        address="전북특별자치도 전주시 완산구 중화산동2가 635-7", coordinates=None
    )
    assert res.status == "failed"
    assert res.reason == FAIL_JIBUN_NOT_IN_CADASTRAL
    assert res.action == "지번 변경 이력 확인"
    assert res.assembled_pnu == "5211112800106350007"


def test_fail_bonbun_not_in_cadastral(legal_dong):
    # 조립 PNU 도 없고 같은 본번 접두도 없음 → 본번 자체 없음(폐쇄지번·오류).
    cad = _FakeCadastral(existing={"5211112800100010000"})  # 본번 0001만 존재
    r = PnuResolver(legal_dong, cad)
    res = r.resolve(
        address="전북특별자치도 전주시 완산구 중화산동2가 635-7", coordinates=None
    )
    assert res.status == "failed"
    assert res.reason == FAIL_BONBUN_NOT_IN_CADASTRAL
    assert res.action == "원천 주소 재확인"


# ── 백엔드 부재는 실패로 전파(빈 결과로 삼키지 않음) ─────────────────────────


def test_missing_cadastral_backend_propagates_failure(legal_dong):
    r = PnuResolver(legal_dong, cadastral=None)
    res = r.resolve(address="전북특별자치도 군산시 소룡동 100")
    assert res.status == "failed"


# ── 배치 집계 ─────────────────────────────────────────────────────────────────


def test_resolve_records_summary(legal_dong):
    class Rec:
        def __init__(self, address="", pnu="", coordinates=None):
            self.address = address
            self.pnu = pnu
            self.coordinates = coordinates

    cad = _FakeCadastral(existing={"5213014600101000000"})
    r = PnuResolver(legal_dong, cad)
    recs = [
        Rec(address="전북특별자치도 군산시 소룡동 100"),  # confirmed rep point
        Rec(address="전북특별자치도 전주시 완산구 백제대로 1"),  # fail NO_JIBUN
    ]
    summary, results = resolve_records(r, recs)
    assert summary.total == 2
    assert summary.confirmed == 1
    assert summary.failed == 1
    assert "5213014600101000000" in summary.confirmed_pnus
    assert summary.by_method.get(METHOD_CADASTRAL_REPRESENTATIVE_POINT) == 1
    assert len(results) == 2


# ── 실제 CadastralLocalStore 의 단일 필지·대표점 (임시 SQLite) ────────────────


def _make_temp_cadastral(tmp_path: Path, parcels) -> CadastralLocalStore:
    """schema 를 맞춘 임시 SQLite 지적도 인덱스를 만든다.

    parcels: [(pnu, ring[list[Coordinates]])]. bbox·ring_json 을 채워 넣는다.
    """

    import json

    db = tmp_path / "mini_cadastral.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE parcels(
            pnu TEXT, jibun TEXT, area_m2 REAL,
            min_lat REAL, min_lng REAL, max_lat REAL, max_lng REAL,
            in_jeonbuk INTEGER, ring_json TEXT
        );
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    for pnu, ring in parcels:
        lats = [c.lat for c in ring]
        lngs = [c.lng for c in ring]
        ring_json = json.dumps([[c.lat, c.lng] for c in ring])
        conn.execute(
            "INSERT INTO parcels VALUES(?,?,?,?,?,?,?,?,?)",
            (pnu, "", 1.0, min(lats), min(lngs), max(lats), max(lngs), 1, ring_json),
        )
    conn.execute("INSERT INTO meta VALUES('parcel_count', ?)", (str(len(parcels)),))
    conn.commit()
    conn.close()
    return CadastralLocalStore(db_path=db)


def _square(lat0, lng0, size=0.001):
    return [
        Coordinates(lat=lat0, lng=lng0),
        Coordinates(lat=lat0, lng=lng0 + size),
        Coordinates(lat=lat0 + size, lng=lng0 + size),
        Coordinates(lat=lat0 + size, lng=lng0),
        Coordinates(lat=lat0, lng=lng0),
    ]


def test_store_sole_parcel_single(tmp_path):
    store = _make_temp_cadastral(
        tmp_path,
        [
            ("5213014600101000000", _square(35.50, 127.00)),
            ("5213014600101000001", _square(35.60, 127.20)),
        ],
    )
    parcel, count = store.sole_parcel_at(35.5005, 127.0005)
    assert count == 1
    assert parcel is not None and parcel.pnu == "5213014600101000000"


def test_store_sole_parcel_multiple_returns_none(tmp_path):
    # 두 필지가 같은 점을 공유(경계 공유) → 단일 특정 불가 → None, 2.
    store = _make_temp_cadastral(
        tmp_path,
        [
            ("5213014600101000000", _square(35.50, 127.00)),
            ("5213014600101000002", _square(35.50, 127.00)),  # 동일 위치 겹침
        ],
    )
    parcel, count = store.sole_parcel_at(35.5005, 127.0005)
    assert parcel is None
    assert count == 2


def test_store_representative_point_inside(tmp_path):
    store = _make_temp_cadastral(
        tmp_path, [("5213014600101000000", _square(35.50, 127.00))]
    )
    parcel = store.parcel_by_pnu("5213014600101000000")
    assert parcel is not None
    point = store.representative_point(parcel)
    assert point is not None
    assert 35.50 <= point.lat <= 35.501
    assert 127.00 <= point.lng <= 127.001


def test_store_pnu_prefix_exists(tmp_path):
    store = _make_temp_cadastral(
        tmp_path, [("5213014600106350009", _square(35.50, 127.00))]
    )
    assert store.pnu_prefix_exists("5213014600106350") is True
    assert store.pnu_prefix_exists("5213014600199990") is False
