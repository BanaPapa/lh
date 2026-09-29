"""2차 주거여건 「문화시설」의 원천 선택.

LH 최종 보고서의 문화시설은 행정안전부 지방행정인허가 공연장·박물관미술관·영화상영관
세 원장뿐이다(전북 279곳, 내부망 앱 원천 38·39·40번). 카카오 문화시설 분류(CT1)는
갤러리·공연단체·전시장이 섞이고 일부 LH 시설(예: 일제강점기 군산역사관)을 엉뚱한
좌표로 주거나 놓친다. 그래서

- 기본(LH 기준): 인허가 세 원장만 센다.
- 관리자 옵션 culture_extended 를 켜면: 인허가 세 원장 + 카카오 CT1(겹치는 곳 제외).
- 세 원장 중 적재되지 않은 것이 있으면: 카카오 CT1 로 메우고 경고를 띄운다.
  조용히 넘어가지 않는다 — 담당자가 재심사를 판단할 수 있어야 한다.

이 모듈은 원천 조합·문구·중복 제거만 맡는다. 거리 측정은 amenities 가 한다.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import NamedTuple

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.localdata import DATASET_BY_KEY


# 인허가 원장 키(localdata.DATASETS). 순서는 LH 원천 번호(38·39·40) 순이다.
CULTURE_DATASETS: tuple[str, ...] = (
    "performance_halls",
    "museums_and_art_galleries",
    "movie_theaters",
)

CULTURE_LOCALDATA_SOURCE = "행정안전부 지방행정인허가 공연장·박물관미술관·영화상영관"

# 카카오 결과가 인허가 시설과 같은 곳인지 보는 거리. 같은 건물의 공연장이 카카오와
# 인허가에서 수십 m 어긋나 온다(인허가 좌표는 건물 대표점, 카카오는 입구 부근).
DEDUPE_RADIUS_M = 40.0

# 관리자 옵션 키(rules_config.OPTION_DEFS).
CULTURE_EXTENDED_OPTION = "culture_extended"

CULTURE_STANDARD_NOTE = (
    "LH 기준대로 행정안전부 인허가 공연장·박물관미술관·영화상영관 원장만 문화시설로 "
    "셌습니다(휴업 포함, 폐업 제외 — LH 데이터셋과 같습니다)."
)
CULTURE_EXTENDED_NOTE = (
    "인허가 공연장·박물관미술관·영화상영관 원장에 더해, 관리자 설정 「문화시설 넓게 "
    "보기」에 따라 지도(카카오) 문화시설 분류의 갤러리·전시장 등도 셌습니다. LH 기준보다 "
    "넓습니다."
)
CULTURE_FALLBACK_NOTE = (
    "인허가 원장({missing})이 적재되지 않아 {what}. LH 기준과 시설 범위가 다를 수 있습니다."
)
CULTURE_FALLBACK_ALERT = (
    "문화시설 지정 원천(행정안전부 인허가 {missing})이 적재되지 않아 {what}. "
    "LH 기준과 달리 갤러리·공연단체가 섞이거나 LH 시설을 놓칠 수 있으니, "
    "원장 적재(활용신청·동기화) 후 재심사하세요."
)
CULTURE_MISSING_NOTE = (
    "문화시설 지정 원천(행정안전부 인허가 공연장·박물관미술관·영화상영관)이 적재되지 "
    "않았고 대체할 지도 검색도 쓸 수 없어 산정하지 않았습니다."
)

CULTURE_KAKAO_COUNT_NOTE = "지도(카카오) 문화시설 분류 — LH 기준 밖(관리자 설정으로 추가)"
CULTURE_SUSPENDED_COUNT_NOTE = "휴업 중 — LH 데이터셋도 문화시설로 셉니다"


class CulturePlan(NamedTuple):
    """이번 심사에서 문화시설을 어떤 원천 조합으로 채울지."""

    extended: bool
    # 적재된(레코드가 있는) 인허가 원장 키.
    ready: tuple[str, ...]
    # 적재되지 않은 인허가 원장 키.
    missing: tuple[str, ...]

    @property
    def use_localdata(self) -> bool:
        return bool(self.ready)

    @property
    def fallback(self) -> bool:
        """지정 원천 일부·전부가 없어 카카오로 메우는가."""

        return bool(self.missing)

    @property
    def use_kakao(self) -> bool:
        return self.extended or self.fallback

    @property
    def state(self) -> str:
        # 지정 원천이 모두 답했으면 연결이다. 관리자 확장은 판단에 따른 선택이라
        # 근사로 보지 않는다. 원장이 빠져 카카오로 메웠으면 근사다.
        return "substituted" if self.fallback else "connected"

    def note(self, kakao_ok: bool = True) -> str:
        """시설군 note. kakao_ok 는 카카오 CT1 조회가 실제로 답했는지다."""

        if self.fallback:
            if not kakao_ok and not self.ready:
                return CULTURE_MISSING_NOTE
            return CULTURE_FALLBACK_NOTE.format(
                missing=_labels(self.missing), what=self._fallback_what(kakao_ok)
            )
        return CULTURE_EXTENDED_NOTE if self.extended else CULTURE_STANDARD_NOTE

    def alert(self, kakao_ok: bool = True) -> str:
        """심사 결과 상단 경고. 지정 원천이 다 있으면 비어 있다."""

        if not self.fallback:
            return ""
        return CULTURE_FALLBACK_ALERT.format(
            missing=_labels(self.missing), what=self._fallback_what(kakao_ok)
        )

    def _fallback_what(self, kakao_ok: bool) -> str:
        if not kakao_ok:
            return (
                "지도 검색도 쓸 수 없어 적재된 원장만 셌습니다"
                if self.ready
                else "지도 검색도 쓸 수 없어 문화시설을 산정하지 못했습니다"
            )
        if self.ready:
            return "빠진 종류를 지도(카카오) 문화시설 분류로 메웠습니다"
        return "지도(카카오) 문화시설 분류로 대신 산정했습니다"


def plan_culture(ready_datasets: Iterable[str], extended: bool) -> CulturePlan:
    ready = set(ready_datasets)
    return CulturePlan(
        extended=extended,
        ready=tuple(key for key in CULTURE_DATASETS if key in ready),
        missing=tuple(key for key in CULTURE_DATASETS if key not in ready),
    )


def _labels(keys: Sequence[str]) -> str:
    return "·".join(DATASET_BY_KEY[key].label if key in DATASET_BY_KEY else key for key in keys)


# 「CGV 전주고사 3관」 → 「CGV전주고사」. 영화상영관 원장은 상영관(관) 하나가 한 행이다.
_SCREEN_SUFFIX = re.compile(r"\s*(제\s*)?\d+\s*관$")
_NAME_NOISE = re.compile(r"[\s()\[\]·.,\-]")


def venue_name(name: str) -> str:
    """상영관 번호를 뗀 시설 이름. 같은 극장의 여러 관을 한 곳으로 묶는 데 쓴다."""

    return _SCREEN_SUFFIX.sub("", (name or "").strip()).strip() or (name or "").strip()


def name_key(name: str) -> str:
    return _NAME_NOISE.sub("", venue_name(name)).lower()


def is_duplicate(
    name: str,
    point: Coordinates,
    existing: Sequence[tuple[str, Coordinates]],
    radius_m: float = DEDUPE_RADIUS_M,
) -> bool:
    """카카오 시설이 이미 센 인허가 시설과 같은 곳인가(가까운 좌표 또는 같은 이름)."""

    key = name_key(name)
    for other_name, other_point in existing:
        if haversine_meters(point, other_point) <= radius_m:
            return True
        if key and key == name_key(other_name):
            return True
    return False
