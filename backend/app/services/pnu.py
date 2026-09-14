"""PNU(필지고유번호) 조립.

PNU 19자리 = 법정동코드(10) + 산여부(1) + 본번(4) + 부번(4)
지적도 API는 주소가 아니라 이 번호로만 검색되므로, 모든 필지 조회의 출발점이다.
"""

from __future__ import annotations

from typing import Any


PNU_LENGTH = 19
LEGAL_CODE_LENGTH = 10


def build_pnu(
    legal_code: str,
    mountain_yn: str,
    main_no: str,
    sub_no: str,
) -> str:
    """법정동코드와 지번 구성요소로 PNU를 만든다."""

    if len(legal_code) != LEGAL_CODE_LENGTH or not legal_code.isdigit():
        raise ValueError(f"법정동코드는 숫자 10자리여야 합니다: {legal_code!r}")
    mountain = "2" if str(mountain_yn).upper() == "Y" else "1"
    main = str(main_no or "0").zfill(4)
    sub = str(sub_no or "0").zfill(4)
    pnu = f"{legal_code}{mountain}{main}{sub}"
    if len(pnu) != PNU_LENGTH:
        raise ValueError(f"PNU 길이가 19자리가 아닙니다: {pnu!r}")
    return pnu


def pnu_from_address_document(document: dict[str, Any]) -> str | None:
    """카카오 주소검색 문서에서 PNU를 만든다.

    주소검색(`/v2/local/search/address.json`)은 `b_code`(법정동코드)를 주지만
    역지오코딩(`coord2address`)은 주지 않는다. 후자에서는 `coord2regioncode`로
    법정동코드를 따로 받아 `build_pnu`를 직접 호출해야 한다.
    """

    address = document.get("address") or document
    legal_code = str(address.get("b_code") or "")
    main_no = str(address.get("main_address_no") or "")
    if len(legal_code) != LEGAL_CODE_LENGTH or not main_no:
        return None
    try:
        return build_pnu(
            legal_code,
            str(address.get("mountain_yn") or "N"),
            main_no,
            str(address.get("sub_address_no") or ""),
        )
    except ValueError:
        return None
