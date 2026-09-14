"""시설 좌표가 떨어진 필지를 「시설 경계」로 써도 되는지 가리는 안전장치.

시설 좌표(지도 POI)는 종종 시설 앞 도로·하천·구거 필지 위에 찍힌다. 그 필지를
경계로 쓰면 도로망 전체가 시설 경계가 되어 거리가 터무니없이 짧아진다
(2026-09-15 검수: 군산 산북동 작은도서관 좌표가 도로 필지에 떨어져 블록 전체가
칠해짐). 지목(jibun 끝 글자)이 건축물이 설 수 없는 종류면 필지를 버리고 좌표로 잰다.
"""

from __future__ import annotations

# 지번 끝 글자로 오는 지목 중 시설 경계로 쓰면 안 되는 것.
#   도=도로 · 천=하천 · 구=구거 · 제=제방 · 철=철도용지 · 유=유지(저수지) · 광=광천지
NON_FACILITY_JIMOK: frozenset[str] = frozenset({"도", "천", "구", "제", "철", "유", "광"})

JIMOK_NAMES: dict[str, str] = {
    "도": "도로",
    "천": "하천",
    "구": "구거",
    "제": "제방",
    "철": "철도용지",
    "유": "유지",
    "광": "광천지",
}


def jimok_of(jibun: str) -> str:
    """지번 문자열 끝의 지목 글자. 없으면 빈 문자열(로컬 지적도는 지목이 없을 수 있다)."""

    text = (jibun or "").strip()
    if not text:
        return ""
    last = text[-1]
    return last if "가" <= last <= "힣" else ""


def parcel_rejection_reason(jibun: str) -> str:
    """이 필지를 시설 경계로 쓰면 안 되는 사유. 써도 되면 빈 문자열."""

    jimok = jimok_of(jibun)
    if jimok in NON_FACILITY_JIMOK:
        return f"좌표가 {JIMOK_NAMES[jimok]} 필지(지목 {jimok}) 위에 있어 시설 경계로 쓰지 않음"
    return ""
