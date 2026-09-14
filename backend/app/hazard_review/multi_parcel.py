"""다필지 주소 표기 해석 (LH 확정 2026-09-11 안건 #9 — 다필지 합집합).

원장(118건) 실측에서 한 칸에 여러 지번이 들어오는 표기가 여러 꼴로 섞여 있다.
대표필지 + 인접 필지를 **합집합**으로 보되, 목록이 없으면 대표만 해석하고
담당자가 추가로 고르게 남긴다(#9). 이 모듈은 **순수 함수**다 — 지적도·네트워크를
건드리지 않고 문자열만 여러 개의 지번주소로 펼친다. 실제 필지 조회는 ParcelResolver
가 한다.

지원 표기(원장 실측 예)
    낙평리 46-2, -45                         → 46-2 · 46-45(본번 생략 = 직전 본번 재사용)
    평화동1가 410-3, 411, 412-44, -45, 413-2  → 410-3 · 411 · 412-44 · 412-45 · 413-2
    제내리 1243, 1244                         → 1243 · 1244
    우아동3가 734-16 외 4필지(11,13,15,25)      → 734-16 · 734-11 · 734-13 · 734-15 · 734-25
    산8-15                                   → 산 8-15 (단일)

「외 N필지」만 있고 목록이 없으면 대표만 펼치고 note 로 담당자 추가 선택을 요구한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# 지번 시작 지점을 찾는다. 여기부터가 지번 목록, 앞은 법정동 접두다.
# 법정동명에 숫자가 붙는 경우(평화동1가·우아동3가)가 있어, 공백(또는 문자열 시작)
# 뒤에 오는 '산?숫자' 만 지번 시작으로 본다. 붙어 있는 '1가'의 1은 건드리지 않는다.
_DIGIT_START = re.compile(r"(?:^|\s)(산?\d)")

# 「외 N필지(부번,부번,...)」. 괄호 목록이 없으면 담당자 추가 선택이 필요하다.
_OESILJI = re.compile(r"외\s*(\d+)\s*필지\s*(?:\(\s*([^)]*)\))?")

# 쉼표로 분리한 지번 토큰 하나: (산)? (본번)? (-부번)?.
_TOKEN = re.compile(r"^\s*(산)?\s*(\d+)?\s*(?:-\s*(\d+))?\s*$")

OESILJI_NO_LIST_NOTE = "외 N필지 목록 없음 — 담당자 추가 선택 필요"


@dataclass(frozen=True)
class ParsedParcels:
    """다필지 주소 해석 결과. 첫 항목이 대표필지다."""

    jibun_addresses: list[str]
    note: str = ""

    @property
    def is_multi(self) -> bool:
        return len(self.jibun_addresses) > 1


def _format_number(is_mountain: bool, bonbun: int, bubun: int) -> str:
    head = "산" if is_mountain else ""
    return f"{head}{bonbun}-{bubun}" if bubun else f"{head}{bonbun}"


def parse_multi_parcel_address(address: str) -> ParsedParcels:
    """주소 문자열의 복수 지번을 각각의 지번주소로 펼친다.

    지번이 하나뿐이거나 숫자가 없으면 원문을 그대로 담은 길이 1 목록을 돌려준다
    (기존 단일 필지 경로가 그대로 동작한다).
    """

    text = (address or "").strip()
    if not text:
        return ParsedParcels([])
    start = _DIGIT_START.search(text)
    if not start:
        return ParsedParcels([text])
    prefix = text[: start.start(1)].strip()
    rest = text[start.start(1) :].strip()

    note = ""
    extra_bubuns: list[int] = []
    oe = _OESILJI.search(rest)
    if oe is not None:
        inside = (oe.group(2) or "").strip()
        if inside:
            extra_bubuns = [int(n) for n in re.findall(r"\d+", inside)]
        else:
            note = OESILJI_NO_LIST_NOTE
        rest = (rest[: oe.start()] + " " + rest[oe.end() :]).strip()

    parsed: list[tuple[bool, int, int]] = []
    cur_bonbun: int | None = None
    cur_mountain = False
    for raw in re.split(r"[,，]", rest):
        token = raw.strip()
        if not token:
            continue
        match = _TOKEN.match(token)
        if match is None:
            continue
        san, bon, bub = match.group(1), match.group(2), match.group(3)
        if bon is not None:
            cur_bonbun = int(bon)
            cur_mountain = bool(san)
            parsed.append((cur_mountain, cur_bonbun, int(bub or 0)))
        elif bub is not None and cur_bonbun is not None:
            # 본번 생략(-부번) — 직전 본번을 재사용한다.
            parsed.append((cur_mountain, cur_bonbun, int(bub)))

    # 「외 N필지(...)」 부번은 바로 앞(마지막) 본번에 붙는다.
    if extra_bubuns and cur_bonbun is not None:
        for bubun in extra_bubuns:
            parsed.append((cur_mountain, cur_bonbun, bubun))

    if not parsed:
        return ParsedParcels([text], note)

    seen: set[tuple[bool, int, int]] = set()
    addresses: list[str] = []
    for key in parsed:
        if key in seen:
            continue
        seen.add(key)
        number = _format_number(*key)
        addresses.append(f"{prefix} {number}".strip())
    return ParsedParcels(addresses, note)
