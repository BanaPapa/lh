"""심사표 2항 「매입제외(불가) 사유」 전체 항목.

심사표는 15개 항목을 늘어놓고 "1개 항목이라도 해당 시 매입불가"라고 못박는다.
이 엔진이 좌표로 판정하는 것은 그중 「주거환경 저해시설」 하나뿐이다.

나머지 14개를 화면에서 아예 빼면, 자동 판정이 통과했을 때 심사가 끝난 것처럼
읽힌다. 그래서 전 항목을 그대로 싣되 자동 판정 대상이 아닌 항목은 「별도 확인」
으로 명시한다. 심사표 원문 순서를 바꾸지 않는다.
"""

from __future__ import annotations

from typing import Literal, NamedTuple


# automatic — 이 엔진이 좌표로 판정한다.
# manual     — 공간분석 대상이 아니다. 담당자가 서류로 확인한다.
ChecklistMode = Literal["automatic", "manual"]


class ChecklistItem(NamedTuple):
    """매입제외 항목 한 줄."""

    key: str
    label: str
    mode: ChecklistMode
    # manual 항목이 왜 자동 판정 대상이 아닌지. 화면에 그대로 노출한다.
    note: str = ""


EXCLUSION_CHECKLIST: tuple[ChecklistItem, ...] = (
    ChecklistItem(
        "development_district",
        "개발이 예정된 지구(지역) 내 주택",
        "manual",
        "재정비촉진지구·정비구역·공공주택지구·택지개발예정지구 등 지구 지정 여부 — 고시 확인 대상",
    ),
    ChecklistItem(
        "minimum_housing",
        "최저주거기준 미달 또는 공급유형별 전용면적 기준 미충족",
        "manual",
        "설계도서 확인 대상",
    ),
    ChecklistItem(
        "utilities",
        "도시가스 및 상하수도 미설치 지역의 주택",
        "manual",
        "농어촌 등 예외사유 포함 — 현장·공부 확인 대상",
    ),
    # 이 한 줄이 유해요소 판정 엔진이 담당하는 항목이다.
    ChecklistItem(
        "hazard_facilities",
        "주거환경 저해시설",
        "automatic",
        "군부대·사격장·화장장 / 공장 또는 위험물 저장·처리시설 / 일반숙박시설 / 위락시설",
    ),
    ChecklistItem(
        "basement_unit",
        "지하(반지하 포함) 세대 계획",
        "manual",
        "설계도서 확인 대상",
    ),
    ChecklistItem(
        "illegal_building",
        "불법건축물 및 법률상 제한사유가 있고 치유불가한 주택",
        "manual",
        "건축법 위반·압류·가압류·경매개시 등 — 등기·대장 확인 대상",
    ),
    ChecklistItem(
        "land_occupied",
        "타인 소유의 담장 등 시설물에 의해 부속토지가 점유된 주택",
        "manual",
        "현장 확인 대상",
    ),
    ChecklistItem(
        "access_road",
        "진입도로 미확보 또는 사도인 주택",
        "manual",
        "지적·도로 대장 확인 대상",
    ),
    ChecklistItem(
        "exterior_material",
        "건축물 외벽 마감재료가 준불연재 또는 불연재 성능 미충족",
        "manual",
        "자재 성능 확인 대상",
    ),
    ChecklistItem(
        "elevator",
        "엘리베이터 미설치 주택",
        "manual",
        "설계도서 확인 대상",
    ),
    ChecklistItem(
        "meters",
        "세대별 전기 및 수도계량기 미설치 주택",
        "manual",
        "설계도서 확인 대상",
    ),
    ChecklistItem(
        "related_party",
        "매도신청인이 전·현직 직원(임원 포함) 또는 그 직계 존·비속·배우자에 해당",
        "manual",
        "퇴직 후 5년 미경과자 포함 — 인적 확인 대상",
    ),
    ChecklistItem(
        "sanctioned",
        "청탁 등 부정행위로 공사로부터 제재를 받은 행위자가 신청하는 주택",
        "manual",
        "제재 이력 확인 대상",
    ),
    ChecklistItem(
        "unsold_apartment",
        "미분양아파트",
        "manual",
        "신청 물건 성격 확인 대상",
    ),
    ChecklistItem(
        "other",
        "기타사유",
        "manual",
        "심사자 기재 항목",
    ),
)

AUTOMATIC_KEYS: tuple[str, ...] = tuple(
    item.key for item in EXCLUSION_CHECKLIST if item.mode == "automatic"
)

MANUAL_ITEM_COUNT = sum(1 for item in EXCLUSION_CHECKLIST if item.mode == "manual")

CHECKLIST_NOTE = (
    f"심사표 2항 매입제외 사유 {len(EXCLUSION_CHECKLIST)}개 항목 중 "
    f"「주거환경 저해시설」 1개만 좌표로 자동 판정합니다. "
    f"나머지 {MANUAL_ITEM_COUNT}개는 공간분석 대상이 아니므로 별도 확인이 필요하며, "
    "자동 판정 통과가 매입적격을 뜻하지 않습니다."
)
