"""지오코딩 입력 후보 — 원장 주소 뒤에 상호·괄호 부기가 붙어 실패하는 경우를 위한 공통 규칙.

물류창고(30/286 실패, 2026-09-17)·LPG 판매소처럼 사람이 적은 주소 열은 「도로명 12 (주)○○」
꼴이 흔하다. 원문 → 괄호 앞 → 뒤 토큰을 하나씩 뗀 형태(최소 3토큰) 순으로 시도한다.

단, 번지(건물번호)가 떨어져 나간 후보는 만들지 않는다. 「…인후동2가」·「…덕진구」처럼 동·구
이름만 남으면 지오코더가 동 중심점을 돌려주는데, 그걸 시설 위치로 쓰면 엉뚱한 곳이 된다
(2026-09-30 성락시장: 원장 주소 「인후동2가 호 1575-1」이 정확 매칭에 실패하자 「인후동2가」
중심점으로 떨어져 사업지 옆 6.9m 로 재였다 — LH 141m). 번지 앞에 끼어든 「호」·「번지」 같은
토막은 떼고 찾는다.
"""

from __future__ import annotations

import re

# 번지 바로 앞에 잘못 끼어든 토막(「인후동2가 호 1575-1」). 떼면 정확 매칭이 된다.
_STRAY_BEFORE_NUMBER = re.compile(r"\s(?:호|번지|번)\s+(?=산?\d)")
# 후보가 번지·건물번호로 끝나는가(「1575-1」「12」「산79-5」「12번지」「73-1번길 5」).
_ENDS_WITH_NUMBER = re.compile(r"(?:산?\d+(?:-\d+)?)(?:번지)?$")


def _has_number(candidate: str) -> bool:
    tokens = candidate.split()
    return bool(tokens) and bool(_ENDS_WITH_NUMBER.search(tokens[-1]))


def address_candidates(address: str) -> list[str]:
    cleaned = " ".join(address.split())
    if not cleaned:
        return []
    candidates = [cleaned]
    repaired = _STRAY_BEFORE_NUMBER.sub(" ", cleaned)
    if repaired != cleaned:
        candidates.append(repaired)
    head = repaired.split("(")[0].strip()
    if head and head not in candidates:
        candidates.append(head)
    tokens = head.split()
    while len(tokens) > 3:
        tokens = tokens[:-1]
        joined = " ".join(tokens)
        if joined not in candidates:
            candidates.append(joined)
    # 원문은 그대로 한 번 시도하되, 줄여 만든 후보는 번지가 남은 것만 쓴다.
    return [candidates[0]] + [c for c in candidates[1:] if _has_number(c)]
