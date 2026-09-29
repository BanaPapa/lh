"""2차 주거여건 「공원」의 원천 선택.

LH 최종 보고서의 공원은 전국도시공원정보표준데이터 생활권공원(전북 653곳, 내부망 앱 원천
54번)과 국공립공원 구역(자연공원 용도지구 도형, 원천 53번)이다. 카카오 「공원」 검색은
이름이 다르게 오거나(LH 「금암공원」 ↔ 카카오 「금암어린이공원」) 작은 소공원이 빠졌다. 그래서

- 기본(LH 기준): 도시공원 표준데이터만 센다. 이름·지번이 LH 결과와 같다.
- 관리자 옵션 park_kakao_supplement 를 켜면: 표준데이터 + 카카오 공원(겹치는 곳 제외).
- 표준데이터가 응답하지 않으면: 카카오 공원으로 대체하고 경고를 띄운다(조용히 넘어가지 않는다).

이 모듈은 문구·중복 판정만 맡는다. 원천 호출과 거리 측정은 amenities 가 한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.models import Coordinates
from app.services.geo import haversine_meters


PARK_STANDARD_SOURCE = "전국도시공원정보표준데이터(지방자치단체)"
# 표준데이터 공원의 category_name 머리. _is_park 가 공원구분과 관계없이 남긴다
# (소공원·문화공원·묘지공원 등은 카카오 공원 분류 목록에 없다).
PARK_STANDARD_CATEGORY = "도시공원정보표준데이터"

# 관리자 옵션 키(rules_config.OPTION_DEFS).
PARK_SUPPLEMENT_OPTION = "park_kakao_supplement"

PARK_STANDARD_NOTE = (
    "LH 기준대로 전국도시공원정보표준데이터(어린이·근린·소공원·주제공원)로 산정했습니다. "
    "공원 필지는 공원 지번주소로 찾아 경계에서 잽니다."
)
PARK_SUPPLEMENT_NOTE = (
    "전국도시공원정보표준데이터에 더해, 관리자 설정 「공원에 지도 검색 공원 더하기」에 따라 "
    "지도(카카오) 공원 중 표준데이터에 없는 곳도 셌습니다. LH 기준보다 넓습니다."
)
PARK_STANDARD_ALERT = (
    "전국도시공원정보표준데이터가 응답하지 않아 지도(카카오) 공원 검색으로 대체했습니다. "
    "LH 결과와 공원 이름이 다르거나 작은 공원이 빠졌을 수 있으니 재심사하세요."
)
PARK_SUPPLEMENT_ALERT = (
    "지도(카카오) 공원 보강 검색이 응답하지 않아 도시공원 표준데이터만으로 셌습니다."
)

# 카카오 공원이 표준데이터 공원과 같은 곳인지 보는 거리. 좌표만 가까워도 같은 공원이다.
SAME_SPOT_M = 80.0
# 이름 열쇠가 같으면 이 안에서 같은 공원으로 본다(큰 공원은 대표점이 수백 m 어긋난다).
SAME_NAME_M = 1000.0

_NAME_NOISE = re.compile(r"[\s()\[\]·.,\-]")
_PARK_SUFFIX = re.compile(r"(어린이|근린|체육|소|문화|수변|역사|묘지|도시|생태)?공원$")


def standard_category(kind: str) -> str:
    return f"{PARK_STANDARD_CATEGORY} > {kind or '공원'}"


def is_standard_category(category_name: str) -> bool:
    return category_name.startswith(PARK_STANDARD_CATEGORY)


def name_key(name: str) -> str:
    """「금암어린이공원」·「금암공원」 → 「금암」. 공원 종류 꼬리와 공백·기호를 뗀다."""

    squashed = _NAME_NOISE.sub("", name or "")
    return _PARK_SUFFIX.sub("", squashed) or squashed


def is_duplicate(
    name: str,
    coordinates: Coordinates,
    standard: Iterable[tuple[str, Coordinates]],
) -> bool:
    key = name_key(name)
    for other_name, other_point in standard:
        distance = haversine_meters(coordinates, other_point)
        if distance <= SAME_SPOT_M:
            return True
        if key and distance <= SAME_NAME_M and name_key(other_name) == key:
            return True
    return False
