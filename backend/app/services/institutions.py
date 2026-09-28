"""기관 이름 판별 — 자가설비 가스 제외와 공공청사 판별.

1) 자가설비 가스 제외 (self_use_gas_institution)
   고압가스·특정고압가스 원장의 사업장명에 「병원」·「소방서」·「대학교」가 들어가면
   기관 안의 자가설비(산소탱크·냉난방·실험용 등)로 보고 유해시설 판정에서 뺀다.
   근거: LH 09/22 결정 2(병원·대학 부지 안 자체 사용 고압가스는 유해시설 아님, 09/11
   결정 3 재확인)와 표준 데이터셋 가이드 붙임 2 H09·H10(「BPLC_NM 에 병원·소방서·
   대학교 포함 시 EXCLUDED — 자가설비 예상 / 시설명 기준 제외(대학및병원및소방서)」).
   가이드가 「검색어를 '대학' 전체로 넓히지 않는다」고 못 박았으므로 낱말 세 개를
   부분 일치로만 본다. 도청·우체국 같은 다른 공공기관은 넓히지 않는다(사용자 결정
   2026-09-28 A안 — 납품 데이터셋과 같은 기준).

2) 공공청사 판별 (is_public_office)
   2차 공공시설 중 도청·시청 같은 청사 이름. 최근접 몇 곳 밖이어도 필지 경계를
   조회해, 같은 부지의 청사·부속 시설을 부지 안 점이 아니라 경계로 재게 한다.
   청사 낱말은 이름 끝이나 띄어쓰기·괄호 앞에 올 때만 인정한다(「시청앞주유소」 거름).
"""

from __future__ import annotations

import re

# 자가설비 가스 제외 낱말 → 화면 표기. 가이드 순서(대학·병원·소방서)를 따른다.
SELF_USE_GAS_TOKENS: dict[str, str] = {
    "대학교": "대학교",
    "병원": "병원",
    "소방서": "소방서",
}


def self_use_gas_institution(name: str) -> str:
    """사업장명에 병원·소방서·대학교가 있으면 그 낱말, 아니면 ""."""

    text = name or ""
    for token, label in SELF_USE_GAS_TOKENS.items():
        if token in text:
            return label
    return ""


_PUBLIC_OFFICE_TOKENS: tuple[str, ...] = (
    "도청", "시청", "군청", "구청", "청사", "행정복지센터", "주민센터",
    "읍사무소", "면사무소", "동사무소", "의회", "교육청", "교육지원청",
)
_BORROWED = re.compile(r"주유소|충전소|(앞|옆|건너편|맞은편|부근)(\s|$)")
_PUBLIC_OFFICE = re.compile(
    "("
    + "|".join(sorted(map(re.escape, _PUBLIC_OFFICE_TOKENS), key=len, reverse=True))
    + r")(?=$|[\s(\[·,\-])"
)


def is_public_office(name: str) -> bool:
    """도청·시청 같은 공공청사(또는 청사 이름을 단 부속 시설) 이름인가."""

    text = (name or "").strip()
    if not text or _BORROWED.search(text):
        return False
    return bool(_PUBLIC_OFFICE.search(text))
