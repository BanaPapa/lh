"""LH 개별 맞춤 등록부 — 공공 API 자료를 LH 데이터셋 기준에 일부러 맞춘 특이점 모음.

이 앱은 공공 API 만으로 시설을 모으고, LH 내부망 앱(진용성 v6)은 LH 가 정리한 로컬
데이터셋(facility-sets)을 쓴다. 두 원천은 대부분 같은 결과를 내지만, 몇몇 시설은 API 가
돌려주는 위치·이름·범위가 LH 데이터셋과 다르다(예: API 는 대학 정문 좌표를 주는데 LH
데이터셋은 학교 지점이나 캠퍼스 필지로 잰다). 일반 규칙으로 재현할 수 없는 이런 곳을
시설 한 곳씩 여기에 적어 두고, 수집 단계에서 LH 기준으로 맞춘다.

원칙
    - 항목마다 LH 데이터셋의 근거(파일·행·좌표·PNU)와 LH 거리를 함께 적는다. 근거가
      없는 차이는 여기 넣지 않는다.
    - 사유는 「공공 API 자료 범위를 LH 데이터셋 기준에 맞췄다」로 쓴다. 앱 오류로 읽히는
      표현을 쓰지 않는다.
    - 맞춘 시설은 결과에 「LH 개별 맞춤」 표시가 붙는다(화면·엑셀). 조용히 바꾸지 않는다.

값은 저장소에 커밋하는 backend/data/lh_alignments.json 에 있다. 관리자 설정의 편집
가능한 「LH 개별 확인 제외」 목록(rule_overrides.json · rules_config.excluded_reason)은
그대로 두고, 여기의 유해시설 제외(scope=hazard)는 그 목록과 함께 적용한다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.models import Coordinates
from app.services.geo import haversine_meters

logger = logging.getLogger("uvicorn.error")

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALIGNMENTS_PATH = BACKEND_DIR / "data" / "lh_alignments.json"

# 결과 표시 문구. 화면 배지·엑셀 비고에 그대로 쓴다.
LH_ALIGNMENT_BADGE = "LH 개별 맞춤"
LH_ALIGNMENT_SOURCE = "LH 데이터셋 개별 맞춤"

# 이름이 같아도 이 거리 밖의 시설은 다른 곳으로 본다(동명 시설·다른 캠퍼스).
# 대학교는 학교 단위 키로 넓게 맞추므로 캠퍼스 간 거리(전북대 전주 ↔ 익산 19km)보다
# 훨씬 좁게, 그 밖은 이름을 그대로 맞추므로 넉넉하게 둔다 — 지도 POI 가 실제 전시관
# 자리를, LH 데이터셋이 등록 주소를 가리켜 2km 넘게 벌어진 예가 있다(군산역사관).
UNIVERSITY_MATCH_RADIUS_M = 3_000.0
NAME_MATCH_RADIUS_M = 5_000.0

Action = Literal["point", "add", "rename", "exclude", "merge"]

ACTION_LABELS: dict[str, str] = {
    "point": "LH 기준점에서 잼",
    "add": "LH 시설 추가",
    "rename": "LH 이름으로 표기",
    "exclude": "판정에서 뺌",
    "merge": "LH 시설 하나로 합침",
}

HAZARD_GROUP_LABEL = "1차 유해시설"


class LhEvidence(BaseModel):
    """LH 결과에서 확인한 거리 한 건. 맞춘 뒤 이 앱 거리도 함께 적는다."""

    site: str
    lh_distance_m: float | None = None
    before_m: float | None = None
    after_m: float | None = None
    note: str = ""


class LhValue(BaseModel):
    """LH 데이터셋이 이 시설에 준 값."""

    # LH 데이터셋·결과에 나오는 이름. 비어 있으면 항목 이름을 쓴다.
    name: str = ""
    address: str = ""
    # 기준점 좌표. pnu 가 있으면 필지 경계로 재고, 이 좌표는 필지를 못 받을 때의 폴백이자
    # 같은 시설인지 가리는 기준점이다.
    lat: float | None = None
    lng: float | None = None
    pnu: str = ""
    evidence: list[LhEvidence] = Field(default_factory=list)

    @property
    def coordinates(self) -> Coordinates | None:
        if self.lat is None or self.lng is None:
            return None
        return Coordinates(lat=self.lat, lng=self.lng)


class MergeSource(BaseModel):
    """merge 로 LH 시설에 합칠 API 시설(다른 시설군일 수 있다)."""

    group: str
    names: list[str]


class LhAlignment(BaseModel):
    """LH 개별 맞춤 한 곳."""

    id: str
    # amenity = 2차 생활편의시설, hazard = 1차 유해시설.
    scope: Literal["amenity", "hazard"] = "amenity"
    name: str
    aliases: list[str] = Field(default_factory=list)
    # 2차 시설군 키(university·terminal·culture …). hazard 는 비워 둔다.
    group: str = ""
    action: Action
    merge_from: list[MergeSource] = Field(default_factory=list)
    lh: LhValue = Field(default_factory=LhValue)
    reason: str
    source: str
    date: str

    # -- 표시 ------------------------------------------------------------
    @property
    def lh_name(self) -> str:
        return (self.lh.name or self.name).strip()

    @property
    def group_label(self) -> str:
        if self.scope == "hazard":
            return HAZARD_GROUP_LABEL
        from app.screening.scorebook import FACILITY_GROUP_BY_KEY

        group = FACILITY_GROUP_BY_KEY.get(self.group)
        return group.label if group else self.group

    def summary(self) -> str:
        """무엇을 맞췄는지 한 줄. 관리자 목록과 결과 표시에 쓴다."""

        where = ""
        if self.lh.pnu:
            where = f"LH 필지(PNU {self.lh.pnu}) 경계"
        elif self.lh.coordinates is not None:
            where = "LH 좌표"
        if self.action == "point":
            return f"{where or 'LH 기준점'}에서 잼"
        if self.action == "add":
            return f"API 에 없거나 위치가 다른 LH 시설 「{self.lh_name}」을 {where or 'LH 위치'}에서 잼"
        if self.action == "rename":
            return f"LH 이름 「{self.lh_name}」으로 표기(거리는 같음)"
        if self.action == "exclude":
            return "LH 개별 확인으로 판정에서 뺌"
        merged = " · ".join(name for source in self.merge_from for name in source.names)
        return f"{merged} → LH 시설 「{self.lh_name}」 하나로 합쳐 {where or 'LH 위치'}에서 잼"

    def note(self) -> str:
        """결과 시설에 붙는 한 줄(「LH 개별 맞춤 — …」)."""

        return f"{LH_ALIGNMENT_BADGE} — {self.summary()}. {self.reason}".strip()

    # -- 이름 맞추기 --------------------------------------------------------
    def names(self) -> list[str]:
        return [self.name, self.lh_name, *self.aliases]

    def matches(self, name: str, coordinates: Coordinates | None = None) -> bool:
        """API 시설 이름·좌표가 이 항목의 시설인가."""

        return _matches(self.group, self.names(), self.lh.coordinates, name, coordinates)

    def matches_merge_source(
        self, group: str, name: str, coordinates: Coordinates | None = None
    ) -> bool:
        return any(
            source.group == group
            and _matches(group, source.names, self.lh.coordinates, name, coordinates)
            for source in self.merge_from
        )


class LhAlignmentRegistry(BaseModel):
    description: str = ""
    entries: list[LhAlignment] = Field(default_factory=list)


def normalize_name(text: str) -> str:
    """공백·괄호·가운뎃점·마침표를 뗀 비교용 이름."""

    return re.sub(r"[\s()\[\]·.,\-]", "", text or "").lower()


def _matches(
    group: str,
    names: list[str],
    anchor: Coordinates | None,
    name: str,
    coordinates: Coordinates | None,
) -> bool:
    if not name:
        return False
    if group == "university":
        # 대학은 본교·대학원·단과대학이 한 정문을 쓴다. 학교 단위 키로 맞추고, 캠퍼스가
        # 다른 동명 학교는 기준점 거리로 가른다.
        from app.screening.front_door import school_key

        key = school_key(name)
        hit = bool(key) and any(school_key(candidate) == key for candidate in names)
        radius = UNIVERSITY_MATCH_RADIUS_M
    else:
        # 그 밖은 이름이 정확히 같을 때만(「익산」이 「익산시외버스터미널」을 삼키지 않게).
        target = normalize_name(name)
        hit = any(normalize_name(candidate) == target for candidate in names if candidate)
        radius = NAME_MATCH_RADIUS_M
    if not hit:
        return False
    if anchor is None or coordinates is None:
        return True
    return haversine_meters(anchor, coordinates) <= radius


# ---------------------------------------------------------------------------
# 읽기 — 파일이 바뀌면 다시 읽는다(rules_config.get_config 와 같은 방식)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_cache: tuple[Path, float | None, LhAlignmentRegistry, str] | None = None


def _read(path: Path) -> tuple[LhAlignmentRegistry, str]:
    if not path.exists():
        return LhAlignmentRegistry(), ""
    try:
        raw = path.read_bytes()
        registry = LhAlignmentRegistry.model_validate(json.loads(raw.decode("utf-8")))
    except (ValueError, OSError, ValidationError):
        # 등록부가 깨져도 심사를 멈추지 않는다. 맞춤 없이(공공 API 그대로) 진행하고 알린다.
        logger.exception("LH 개별 맞춤 등록부를 읽지 못했습니다: %s", path)
        return LhAlignmentRegistry(), "invalid"
    return registry, hashlib.sha1(raw).hexdigest()[:12]


def load_registry(path: Path | None = None) -> LhAlignmentRegistry:
    return _load(path)[0]


def _load(path: Path | None = None) -> tuple[LhAlignmentRegistry, str]:
    global _cache
    target = path or ALIGNMENTS_PATH
    mtime = target.stat().st_mtime if target.exists() else None
    with _lock:
        if _cache is not None and _cache[0] == target and _cache[1] == mtime:
            return _cache[2], _cache[3]
        registry, digest = _read(target)
        _cache = (target, mtime, registry, digest)
        return registry, digest


def amenity_alignments(path: Path | None = None) -> tuple[LhAlignment, ...]:
    return tuple(e for e in load_registry(path).entries if e.scope == "amenity")


def alignments_fingerprint(path: Path | None = None) -> str:
    """등록부 내용 지문. 주변시설 캐시 키에 넣어 항목을 고치면 캐시가 갈리게 한다."""

    return _load(path)[1] or "none"


def lh_excluded_reason(
    facility_name: str, parcel_pnu: str = "", path: Path | None = None
) -> str | None:
    """1차 유해시설 판정에서 뺄 시설이면 사유를, 아니면 None.

    등록부의 유해시설 제외(scope=hazard)를 먼저 보고, 관리자 설정에서 편집하는 「LH 개별
    확인 제외」 목록(rules_config.excluded_reason)을 이어서 본다. 이름은 공백·괄호를 뗀 뒤
    한쪽이 다른 쪽을 품으면 같은 시설로 본다(관리자 목록과 같은 규칙). 등록부에 PNU 가
    있고 시설 PNU 도 알면 법정동(PNU 앞 10자리)이 같아야 한다 — 다른 동네의 같은 이름
    주유소는 빼지 않는다. 필지까지 같기를 요구하지 않는 것은 공공 API 주소 좌표가 옆
    필지에 떨어질 수 있어서다.
    """

    from app.rules_config import excluded_reason

    name = normalize_name(facility_name)
    if name:
        for entry in load_registry(path).entries:
            if entry.scope != "hazard" or entry.action != "exclude":
                continue
            if entry.lh.pnu and parcel_pnu and entry.lh.pnu[:10] != parcel_pnu[:10]:
                continue
            for candidate in entry.names():
                target = normalize_name(candidate)
                if target and (target in name or name in target):
                    return entry.reason or "LH 개별 확인으로 판정 제외"
    return excluded_reason(facility_name)
