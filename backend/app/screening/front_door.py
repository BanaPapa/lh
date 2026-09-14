"""대학 캠퍼스 정문 기준점의 저장과 해석.

국장님 「LOCUS 판정 로직 명세서 v1」 §3-2/§3-3 「측정 기준 — 사업지 대지경계 +
시설 측 3단」의 1순위(정문 기준)를 판정 엔진에 붙이기 위한 데이터 계층이다.

이 모듈은 좌표·거리 계산을 하지 않는다(그건 amenities.py 가 지적도·geo 와 함께
한다). 여기서는 "어느 대학의 정문이 어디인지"라는 사실만 보존·조회한다.

저장 위치·형식 근거
--------------------
* 위치: backend/data/front_doors.json — facility_store 의 SQLite 와 같은 data/
  아래에 둔다. 정문 지정은 시·군 한 곳당 대학 몇 곳 규모라 레코드가 극소수이고,
  운영자가 눈으로 확인·수정할 일이 잦다. 그래서 SQLite 대신 사람이 읽을 수 있는
  JSON 한 파일로 둔다. 스키마 마이그레이션 부담도 없다.
* 형식: { 정규화된_대학명: {label, pnu, lat, lng, origin, source_label,
  designated_at} } 의 평면 딕셔너리. 키는 공백을 제거한 대학명이라 "전주대학교"·
  "전주 대학교"가 같은 항목으로 모인다.
* 저장 실패 안전장치: 파일 쓰기가 실패해도 예외를 올리지 않는다. 정문 저장이
  깨졌다고 검토(판정)까지 죽으면 안 된다. 실패 시 메모리에는 반영해 두고,
  다음 프로세스 재기동 전까지만 유효하다.

수기 지정은 origin="manual" 로 이 저장소에 남고, 다음 검토부터 반영된다(§3-3).
자동 채택(정류장 원장 근사)은 저장하지 않고 조회 때마다 파생한다 — 근사값을
사실처럼 굳혀 두지 않기 위해서다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.models import Coordinates


# 캠퍼스 통필지 안전장치 임계면적. 국장님 §3-3 값(5만㎡)을 그대로 쓴다.
# 근거: 대학 캠퍼스가 하나의 지번(통필지)으로 묶여 있으면 그 필지 경계가 캠퍼스
# 전체 외곽(도로변)까지 물고 있어, 이를 '정문'으로 쓰면 사업지~정문 거리가 실제
# 정문 위치보다 짧게 나오는 축소 왜곡이 생긴다. 이 크기 이상이면 필지경계 기준을
# 쓰지 않고 좌표(점) 기준으로 폴백한다. 판정을 느슨하게 만드는 방향의 추정을
# 막기 위한 보수적 임계다.
CAMPUS_PARCEL_MAX_AREA_M2 = 50_000.0

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "data" / "front_doors.json"

# 정류장 원장에서 정문 후보로 인정하는 명칭 토큰(§3-3 자동 채택).
FRONT_DOOR_NAME_TOKENS: tuple[str, ...] = ("정문", "입구")

# 대학 정문 자동 채택에서 배제할 명칭. '전북대학병원입구' 처럼 캠퍼스가 아닌
# 부속기관이 대학명 토큰에 걸려 캠퍼스 정문으로 오인되는 것을 막는다.
# 네이버 지역검색은 「○○대학교 정문」에 부설학교·아파트·주차장 정문까지 함께
# 돌려주므로(2026-09-08 실호출), 그 계열도 함께 배제한다.
_AUTO_EXCLUDE_TOKENS: tuple[str, ...] = (
    "병원", "의원", "치과", "한의원",
    "초등학교", "중학교", "고등학교", "부설", "부속",
    "아파트", "오피스텔", "주차장", "상가", "빌딩", "학원",
)

# 종합병원 정문 후보에서는 '병원' 계열 토큰을 배제하면 안 된다(그 자체가 병원이다).
# 대학용 배제 목록에서 의료 토큰만 뺀 것을 쓴다(#8, LH 확정 2026-09-11).
HOSPITAL_EXCLUDE_TOKENS: tuple[str, ...] = tuple(
    t for t in _AUTO_EXCLUDE_TOKENS if t not in {"병원", "의원", "치과", "한의원"}
)

# 문 후보로 인정하는 명칭 토큰(#7·#11). 정문이 불명확하거나 출입구가 여럿이면
# 전부 후보로 모아 담당자가 고를 수 있게 한다.
FACILITY_DOOR_TOKENS: tuple[str, ...] = (
    "정문", "후문", "동문", "서문", "남문", "북문", "출입구", "입구",
)
# 역·지하철 출구 후보. 「N번 출구」도 '출구' 토큰으로 걸린다.
STATION_DOOR_TOKENS: tuple[str, ...] = ("출구", "출입구")


def university_base(name: str) -> str:
    """대학 시설명에서 정문을 공유하는 단위를 뽑는다.

    「같은 정문을 사용하는 대학교·대학원은 동일한 정문 좌표를 적용한다」는
    표준 데이터셋의 처리(2026-09-08 회신)와 같은 규칙이다.
    「전북대학교 경영대학원」·「우석대학교 한의과대학」은 본체 정문을 쓰고,
    「전북대학교 전주캠퍼스」처럼 캠퍼스가 다르면 캠퍼스별 정문을 따로 찾는다.
    """

    text = (name or "").strip()
    index = text.find("대학교")
    if index < 0:
        return text
    head, rest = text[: index + 3], text[index + 3 :].strip()
    return text if "캠퍼스" in rest else head


def normalize_key(name: str) -> str:
    """대학명을 저장·조회 키로 정규화한다. 공백만 제거해 동일 대학을 한 항목으로 모은다."""

    return "".join((name or "").split())


@dataclass(frozen=True)
class FrontDoorRef:
    """정문 기준점 하나. 필지(PNU)로 지정됐거나 좌표로만 지정됐을 수 있다."""

    key: str
    label: str
    pnu: str = ""
    lat: float | None = None
    lng: float | None = None
    # "manual"(수기 지정) | "auto_stop"(정류장 원장 근사)
    origin: str = "manual"
    # 화면에 그대로 보일 출처 라벨. 자동 채택이면 근사임이 드러나야 한다(§3-3).
    source_label: str = ""

    @property
    def coordinates(self) -> Coordinates | None:
        if self.lat is None or self.lng is None:
            return None
        return Coordinates(lat=float(self.lat), lng=float(self.lng))

    @property
    def has_parcel(self) -> bool:
        return bool(self.pnu)


class FrontDoorStore:
    """정문 수기 지정의 영속 저장소. 파일이 없거나 손상돼도 조회는 실패하지 않는다."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        # 지정이 바뀔 때마다 증가한다. 수집기 캐시 키에 넣어 새 지정이 다음
        # 검토부터 반영되게 한다(600초 캐시에 막히지 않도록).
        self._revision = 0
        self._records: dict[str, FrontDoorRef] = {}
        self._load()

    # -- 조회 ---------------------------------------------------------------
    def revision(self) -> int:
        return self._revision

    def get(self, name: str) -> FrontDoorRef | None:
        return self._records.get(normalize_key(name))

    def all(self) -> dict[str, FrontDoorRef]:
        return dict(self._records)

    # -- 지정 ---------------------------------------------------------------
    def designate(
        self,
        name: str,
        *,
        pnu: str = "",
        lat: float | None = None,
        lng: float | None = None,
        source_label: str = "",
    ) -> FrontDoorRef:
        """정문을 수기 지정한다. 저장이 실패해도 메모리에는 반영하고 조용히 넘어가지 않는다."""

        key = normalize_key(name)
        ref = FrontDoorRef(
            key=key,
            label=name.strip(),
            pnu=(pnu or "").strip(),
            lat=lat,
            lng=lng,
            origin="manual",
            source_label=source_label or "운영자 수기 지정",
        )
        self._records[key] = ref
        self._revision += 1
        self._save()
        return ref

    def remove(self, name: str) -> bool:
        key = normalize_key(name)
        if key in self._records:
            del self._records[key]
            self._revision += 1
            self._save()
            return True
        return False

    # -- 영속 ---------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 파일 부재·손상은 '지정 없음'으로 다룬다. 판정을 막지 않는다.
            self._records = {}
            return
        records: dict[str, FrontDoorRef] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                if not isinstance(value, dict):
                    continue
                records[key] = FrontDoorRef(
                    key=key,
                    label=str(value.get("label") or key),
                    pnu=str(value.get("pnu") or ""),
                    lat=_as_float(value.get("lat")),
                    lng=_as_float(value.get("lng")),
                    origin=str(value.get("origin") or "manual"),
                    source_label=str(value.get("source_label") or ""),
                )
        self._records = records

    def _save(self) -> None:
        payload = {
            key: {
                "label": ref.label,
                "pnu": ref.pnu,
                "lat": ref.lat,
                "lng": ref.lng,
                "origin": ref.origin,
                "source_label": ref.source_label,
                "designated_at": datetime.now(UTC).isoformat(),
            }
            for key, ref in self._records.items()
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 임시 파일에 쓴 뒤 교체해 부분 쓰기로 파일이 깨지는 것을 막는다.
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(tmp, self.path)
        except OSError:
            # 저장 실패로 판정이 죽으면 안 된다. 메모리 반영만으로 이번 세션은 동작한다.
            pass


def _as_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def collect_door_candidates(
    facility_name: str,
    places: "list",
    *,
    door_tokens: tuple[str, ...] = FACILITY_DOOR_TOKENS,
    exclude_tokens: tuple[str, ...] = _AUTO_EXCLUDE_TOKENS,
    require_facility_token: bool = True,
) -> list[tuple[str, Coordinates]]:
    """네이버 지역검색 결과에서 이 시설의 문·출구 후보를 전부 모은다(#7·#11).

    정문/후문/동문/서문/남문/북문/출입구/입구(또는 역 출구) 류를 모두 담고,
    부속기관·타 시설(exclude_tokens)은 버린다. `require_facility_token` 이면 시설의
    변별 토큰을 포함하는 결과만 남긴다(인근 건물 문을 걸러낸다). 거리 계산은 하지
    않는다 — 호출부(amenities)가 사업지 기준 거리를 매긴다.
    """

    tokens = _university_tokens(facility_name)
    out: list[tuple[str, Coordinates]] = []
    seen: set[tuple[str, float, float]] = set()
    for place in places:
        raw_name = getattr(place, "name", "") or ""
        squashed = "".join(raw_name.split())
        if not any(t in squashed for t in door_tokens):
            continue
        if any(bad in squashed for bad in exclude_tokens):
            continue
        if require_facility_token and tokens and not any(tok in squashed for tok in tokens):
            continue
        coords = getattr(place, "coordinates", None)
        if coords is None:
            continue
        signature = (squashed, round(coords.lat, 6), round(coords.lng, 6))
        if signature in seen:
            continue
        seen.add(signature)
        out.append((raw_name.strip(), coords))
    return out


def auto_front_door(
    university_name: str,
    stop_names_and_points: list[tuple[str, Coordinates]],
    *,
    exclude_tokens: tuple[str, ...] = _AUTO_EXCLUDE_TOKENS,
) -> FrontDoorRef | None:
    """정류장 원장에서 대학 정문·입구 명칭 정류장을 찾아 자동 후보로 삼는다(§3-3).

    보수적으로 매칭한다. 정류장 이름이 (1) '정문'·'입구' 를 포함하고, (2) 대학의
    변별 토큰(예: '전주대학교'·'전주대')을 포함하며, (3) 병원 등 부속기관 명칭이
    아니어야 한다. 잘못 매칭해 실제보다 가까운 점을 정문으로 쓰면 거리가 축소되므로
    확신이 서는 경우만 채택한다. 채택해도 좌표(점) 기준이라 통필지 경계 축소 왜곡은
    발생하지 않고, 출처 라벨로 근사임을 반드시 드러낸다.
    """

    tokens = _university_tokens(university_name)
    if not tokens:
        return None
    for raw_name, point in stop_names_and_points:
        name = raw_name or ""
        if not any(t in name for t in FRONT_DOOR_NAME_TOKENS):
            continue
        if any(bad in name for bad in exclude_tokens):
            continue
        if not any(tok in name for tok in tokens):
            continue
        return FrontDoorRef(
            key=normalize_key(university_name),
            label=university_name.strip(),
            lat=point.lat,
            lng=point.lng,
            origin="auto_stop",
            source_label=f"정류장 원장 근사(정류장명 '{name.strip()}')",
        )
    return None


def naver_front_door(
    university_name: str,
    places: "list",
    *,
    exclude_tokens: tuple[str, ...] = _AUTO_EXCLUDE_TOKENS,
) -> FrontDoorRef | None:
    """네이버 지역검색 결과에서 그 대학의 정문을 고른다.

    표준 데이터셋이 대학 정문·역 출구 좌표를 같은 API 로 확보했으므로(2026-09-08
    회신), 이 경로를 쓰면 기준점이 서로 어긋나지 않는다.

    「○○대학교 정문」으로 검색하면 부속병원·인근 학교·아파트 정문이 함께 나온다.
    그래서 (1) 이름에 정문·입구가 있고 (2) 대학의 변별 토큰을 포함하며
    (3) 부속기관·타 시설 토큰이 없는 것만 채택한다. 실제보다 가까운 점을 정문으로
    쓰면 거리가 축소되므로, 확신이 서지 않으면 채택하지 않는다.
    """

    tokens = _university_tokens(university_name)
    if not tokens:
        return None
    for place in places:
        name = "".join((getattr(place, "name", "") or "").split())
        if not any(t in name for t in FRONT_DOOR_NAME_TOKENS):
            continue
        if any(bad in name for bad in exclude_tokens):
            continue
        if not any(tok in name for tok in tokens):
            continue
        coords = getattr(place, "coordinates", None)
        if coords is None:
            continue
        return FrontDoorRef(
            key=normalize_key(university_name),
            label=university_name.strip(),
            lat=coords.lat,
            lng=coords.lng,
            origin="naver_local",
            source_label=f"네이버 지역검색('{getattr(place, 'name', '').strip()}')",
        )
    return None


def _university_tokens(name: str) -> tuple[str, ...]:
    """대학명에서 정류장명과 대조할 변별 토큰들. 전체명과 '대' 축약형을 함께 쓴다."""

    full = "".join((name or "").split())
    if not full:
        return ()
    tokens = {full}
    for suffix in ("대학교", "대학"):
        if full.endswith(suffix) and len(full) > len(suffix):
            core = full[: -len(suffix)] + "대"  # 전주대학교 → 전주대
            if len(core) >= 3:
                tokens.add(core)
    return tuple(tokens)
