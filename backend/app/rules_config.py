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


class RulesConfig(BaseModel):
    # 1차 임계거리 덮어쓰기. 키 "house:general:factory" → m. None 은 「미적용」.
    # 여기 없는 조합·Rule 은 룰북 정본 값을 쓴다.
    stage1_thresholds: dict[str, int | None] = Field(default_factory=dict)
    # 2027 서류심사 기준 완화(교육여건 or 조건 · 합격선 65 · 대학교 가점) 적용 여부.
    relaxed_2027: bool = False
    excluded_facilities: list[ExcludedFacility] = Field(default_factory=list)
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
