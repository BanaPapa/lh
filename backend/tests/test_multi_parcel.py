"""다필지 주소 표기 해석(LH 확정 2026-09-11 #9) 순수 함수 회귀.

원장(118건) 실측에서 확인된 모든 표기 꼴을 고정한다. 파서는 지적도·네트워크를
건드리지 않고 문자열만 여러 개의 지번주소로 펼친다.
"""

from __future__ import annotations

from app.hazard_review.multi_parcel import (
    OESILJI_NO_LIST_NOTE,
    parse_multi_parcel_address,
)


def test_dash_only_reuses_previous_bonbun() -> None:
    # 낙평리 46-2, -45 → 46-2 · 46-45(본번 생략 = 직전 본번 재사용)
    parsed = parse_multi_parcel_address("낙평리 46-2, -45")
    assert parsed.jibun_addresses == ["낙평리 46-2", "낙평리 46-45"]
    assert parsed.is_multi


def test_mixed_list_with_dash_reuse() -> None:
    parsed = parse_multi_parcel_address("평화동1가 410-3, 411, 412-44, -45, 413-2")
    assert parsed.jibun_addresses == [
        "평화동1가 410-3",
        "평화동1가 411",
        "평화동1가 412-44",
        "평화동1가 412-45",
        "평화동1가 413-2",
    ]


def test_bare_bonbun_list() -> None:
    parsed = parse_multi_parcel_address("제내리 1243, 1244")
    assert parsed.jibun_addresses == ["제내리 1243", "제내리 1244"]


def test_oe_n_parcels_with_explicit_list() -> None:
    # 우아동3가 734-16 외 4필지(11,13,15,25) → 734-16 과 734-11/13/15/25
    parsed = parse_multi_parcel_address("우아동3가 734-16 외 4필지(11,13,15,25)")
    assert parsed.jibun_addresses == [
        "우아동3가 734-16",
        "우아동3가 734-11",
        "우아동3가 734-13",
        "우아동3가 734-15",
        "우아동3가 734-25",
    ]
    assert parsed.note == ""


def test_oe_n_parcels_without_list_marks_note_and_keeps_representative() -> None:
    parsed = parse_multi_parcel_address("우아동3가 734-16 외 4필지")
    assert parsed.jibun_addresses == ["우아동3가 734-16"]
    assert not parsed.is_multi
    assert parsed.note == OESILJI_NO_LIST_NOTE


def test_mountain_parcel_single() -> None:
    parsed = parse_multi_parcel_address("산8-15")
    assert parsed.jibun_addresses == ["산8-15"]
    assert not parsed.is_multi


def test_single_parcel_passthrough() -> None:
    # 단일 지번은 원문을 그대로 담은 길이 1 목록(기존 경로 유지).
    parsed = parse_multi_parcel_address("경기 하남시 아리수로 499")
    assert parsed.jibun_addresses == ["경기 하남시 아리수로 499"]
    assert not parsed.is_multi


def test_dong_with_digit_is_not_mistaken_for_jibun() -> None:
    # 법정동명에 숫자가 붙어도(평화동1가·우아동3가) 지번 시작은 공백 뒤 숫자다.
    parsed = parse_multi_parcel_address("전북특별자치도 전주시 완산구 평화동1가 410-3")
    assert parsed.jibun_addresses == ["전북특별자치도 전주시 완산구 평화동1가 410-3"]


def test_empty_address() -> None:
    assert parse_multi_parcel_address("").jibun_addresses == []
