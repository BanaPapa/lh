"""심사 기준 편집값 — 관리자 「기준 편집」이 저장하는 값의 단일 저장소.

09/22 회의 결정 6: 거리 숫자는 관리자 화면에서 고치고 기본값은 보관한다. AND/OR 조건
편집 UI 는 만들지 않고, 2027 완화 기준은 토글(온/오프) 하나로 켠다. 여기에 더해 LH 가
개별로 확인해 판정에서 뺀 시설(예: 철거된 주유소) 목록을 둔다.

값은 backend/data/rule_overrides.json 에 남고, 없으면 룰북 정본(rulebook.MATRIX ·
scorebook 배점표)이 그대로 쓰인다. 판정 경로는 매번 get_config() 로 읽으므로 저장
즉시 다음 심사부터 반영된다.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

BACKEND_DIR = Path(__file__).resolve().parents[1]
RULES_PATH = BACKEND_DIR / "data" / "rule_overrides.json"


class ExcludedFacility(BaseModel):
    """LH 가 개별 확인해 판정에서 뺀 시설 한 곳."""

    name: str
    reason: str = ""


class OptionDef(BaseModel):
    """관리자 설정의 판정 옵션 스위치 하나. 값은 RulesConfig.options[key] 에 남는다."""

    key: str
    label: str
    description: str
    # 기본값 = LH 기준(내부망 앱)과 같게 두는 쪽. 없던 스위치는 이 값으로 동작한다.
    default: bool
    # 화면에서 묶어 보일 이름(예: 「2차 대중교통」).
    group: str = ""


# 관리자 설정 「판정 옵션」 탭에 나오는 스위치 목록. 새 스위치는 여기 한 줄만 더하고
# 판정 코드에서 option_enabled(key) 로 읽는다. 기본값은 LH 기준을 따른다.
OPTION_DEFS: tuple[OptionDef, ...] = (
    OptionDef(
        key="bus_headway_filter",
        label="버스 운행주기 15분 기준 적용",
        description=(
            "TAGO 배차간격으로 15분당 평균 도착 버스가 1대 미만인 정류장을 배점에서 뺍니다. "
            "끄면 LH 내부망 앱처럼 운행주기와 관계없이 모든 정류장을 셉니다."
        ),
        default=True,
        group="2차 대중교통",
    ),
    OptionDef(
        key="culture_extended",
        label="문화시설 넓게 보기(지도 문화시설 분류 추가)",
        description=(
            "기본은 LH 기준대로 공연장·박물관·미술관·영화상영관만 문화시설로 셉니다. "
            "켜면 지도(카카오) 문화시설 분류의 갤러리·전시장 등도 더합니다."
        ),
        default=False,
        group="2차 주거여건",
    ),
    OptionDef(
        key="gas_product_manufacturers",
        label="가스제품 제조업소를 위험물 시설(50m)로 판정",
        description=(
            "LH 내부망 앱처럼 가스안전공사 「가스제품 제조업소정보」(압력용기·연소기·밸브 등 "
            "제조공장)를 위험물 저장·처리시설 50m 로 판정합니다. 끄면 법 정의상 도시가스 "
            "제조시설(아목)이 아니라는 이 앱 해석대로 판정하지 않고 지도에 참고로만 올립니다."
        ),
        default=True,
        group="1차 유해시설",
    ),
    OptionDef(
        key="factory_geocode_vworld_fallback",
        label="등록공장 주소를 VWorld 주소검색으로 한 번 더 찾기",
        description=(
            "산단공 등록공장 주소를 카카오가 못 찾으면(없어진 옛 지번 등) VWorld 주소검색으로 "
            "다시 찾습니다. LH 표준 데이터셋이 같은 방식으로 위치를 잡아 「공장 검토」 목록이 "
            "같아집니다(예: 전주 효자동2가 368번지 현대콘크리트 → 쑥고개로 368). 끄면 "
            "카카오로 찾은 공장만 싣습니다."
        ),
        default=True,
        group="1차 유해시설",
    ),
)
OPTION_BY_KEY: dict[str, OptionDef] = {option.key: option for option in OPTION_DEFS}


class RulesConfig(BaseModel):
    # 1차 임계거리 덮어쓰기. 키 "house:general:factory" → m. None 은 「미적용」.
    # 여기 없는 조합·Rule 은 룰북 정본 값을 쓴다.
    stage1_thresholds: dict[str, int | None] = Field(default_factory=dict)
    # 2027 서류심사 기준 완화(교육여건 or 조건 · 합격선 65 · 대학교 가점) 적용 여부.
    relaxed_2027: bool = False
    excluded_facilities: list[ExcludedFacility] = Field(default_factory=list)
    # 판정 옵션 스위치 값(OPTION_DEFS 키 → 켬/끔). 여기 없는 키는 기본값을 쓴다.
    options: dict[str, bool] = Field(default_factory=dict)
    updated_at: str = ""


def threshold_key(housing_type: str, application_type: str, column: str) -> str:
    return f"{housing_type}:{application_type}:{column}"


_lock = threading.Lock()
_cache: tuple[float | None, RulesConfig] | None = None


def _read() -> RulesConfig:
    if not RULES_PATH.exists():
        return RulesConfig()
    try:
        return RulesConfig.model_validate(json.loads(RULES_PATH.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        # 손상된 파일로 판정을 멈추지 않는다. 정본 값으로 돌아간다.
        return RulesConfig()


def get_config() -> RulesConfig:
    """파일이 바뀌었으면 다시 읽고, 아니면 캐시를 돌려준다."""

    global _cache
    mtime = RULES_PATH.stat().st_mtime if RULES_PATH.exists() else None
    with _lock:
        if _cache is not None and _cache[0] == mtime:
            return _cache[1]
        config = _read()
        _cache = (mtime, config)
        return config


def save_config(config: RulesConfig) -> RulesConfig:
    global _cache
    config.updated_at = datetime.now(UTC).isoformat()
    RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        RULES_PATH.write_text(
            json.dumps(config.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _cache = (RULES_PATH.stat().st_mtime, config)
    return config


def stage1_override(housing_type: str, application_type: str, column: str) -> tuple[bool, int | None]:
    """(덮어쓴 값이 있는가, 그 값). 없으면 (False, None)."""

    overrides = get_config().stage1_thresholds
    key = threshold_key(housing_type, application_type, column)
    if key in overrides:
        return True, overrides[key]
    return False, None


def is_relaxed() -> bool:
    return get_config().relaxed_2027


def option_enabled(key: str) -> bool:
    """판정 옵션 스위치 값. 저장값이 없으면 OPTION_DEFS 의 기본값(LH 기준)."""

    definition = OPTION_BY_KEY.get(key)
    if definition is None:
        raise KeyError(f"알 수 없는 판정 옵션: {key}")
    return get_config().options.get(key, definition.default)


def options_fingerprint() -> str:
    """지금 옵션 값을 한 줄로. 결과 캐시 키에 넣어 스위치를 바꾸면 캐시가 갈리게 한다."""

    return ",".join(f"{o.key}={int(option_enabled(o.key))}" for o in OPTION_DEFS)


def _norm(text: str) -> str:
    return re.sub(r"[\s()\[\]·.,-]", "", text).lower()


def excluded_reason(facility_name: str) -> str | None:
    """LH 개별 확인 제외 시설이면 사유를, 아니면 None 을 돌려준다.

    이름은 공백·괄호를 뗀 뒤 한쪽이 다른 쪽을 품으면 같은 시설로 본다
    (「SK신흥주유소」 ↔ 「SK신흥주유소(효자동)」).
    """

    name = _norm(facility_name)
    if not name:
        return None
    for item in get_config().excluded_facilities:
        target = _norm(item.name)
        if target and (target in name or name in target):
            return item.reason or "LH 개별 확인으로 판정 제외"
    return None
