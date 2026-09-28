from __future__ import annotations

import pytest

from app.services.institutions import is_public_office, self_use_gas_institution


@pytest.mark.parametrize(
    ("name", "label"),
    [
        # 가이드 붙임 2 H09·H10: 병원·소방서·대학교 부분 일치.
        ("전북대학교병원", "대학교"),
        ("예수병원", "병원"),
        ("군산소방서[비응119안전센터]", "소방서"),
        ("원광대학교(숭산기념관)", "대학교"),
        # 「대학」 전체나 다른 공공기관으로 넓히지 않는다(A안).
        ("한국폴리텍대학 전북캠퍼스", ""),
        ("전북특별자치도청", ""),
        ("전주우체국", ""),
        ("전주시보건소", ""),
        ("", ""),
    ],
)
def test_self_use_gas_institution(name: str, label: str) -> None:
    assert self_use_gas_institution(name) == label


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("전북특별자치도청", True),
        ("전라북도청", True),
        ("전북특별자치도청 야외공연장", True),
        ("전북특별자치도의회", True),
        ("효자5동 주민센터", True),
        ("시청앞주유소", False),
        ("도청 앞 LPG", False),
        ("홀리데이청춘클럽", False),
        ("전북도청출장소", False),
    ],
)
def test_is_public_office(name: str, expected: bool) -> None:
    assert is_public_office(name) is expected
