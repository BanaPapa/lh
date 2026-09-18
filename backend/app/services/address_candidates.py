"""지오코딩 입력 후보 — 원장 주소 뒤에 상호·괄호 부기가 붙어 실패하는 경우를 위한 공통 규칙.

물류창고(30/286 실패, 2026-09-17)·LPG 판매소처럼 사람이 적은 주소 열은 「도로명 12 (주)○○」
꼴이 흔하다. 원문 → 괄호 앞 → 뒤 토큰을 하나씩 뗀 형태(최소 3토큰) 순으로 시도한다.
"""

from __future__ import annotations


def address_candidates(address: str) -> list[str]:
    cleaned = " ".join(address.split())
    if not cleaned:
        return []
    candidates = [cleaned]
    head = cleaned.split("(")[0].strip()
    if head and head not in candidates:
        candidates.append(head)
    tokens = head.split()
    while len(tokens) > 3:
        tokens = tokens[:-1]
        joined = " ".join(tokens)
        if joined not in candidates:
            candidates.append(joined)
    return candidates
