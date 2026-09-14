"""2026-09-11 LH 결정 반영 엔진의 전후 회귀 검증 (정민재 앱).

무엇을 하는가
--------------
커밋 8b2201d(결정 반영 전, 2026-09-11 08:15) 스냅샷을 기준선으로 삼고, 같은 입력을
**현재 작업 트리 엔진**(1~3단계·495 passed)으로 다시 판정해 건별 전후 차이를 계산한다.
차이마다 원인을 2026-09-11 회의 결정 번호에 귀속하고, 귀속 안 되는 차이는 「미귀속」으로
남긴다. 원천이 이 PC 에 로컬로 붙어 dataset_missing 이 사라진 것은 결정이 아니라
환경 차이이므로 「원천 배선(WIRING)」으로 따로 센다.

하위 명령
---------
  generate   기준선 입력(주소·좌표·유형)을 그대로 써서 15·17·118 건을 새 엔진으로
             재판정하고 docs/reports/regress_20260913/ 에 결과 JSON 3종을 쓴다.
             (실 API 호출 — 오피넷/카카오/브이월드/TAGO. 10~20분.)
  report     기준선 2종 ↔ 새 결과 3종을 대조해 regress.json 과 단일 HTML 을 만든다.
             generate 없이도 새 결과 JSON 이 이미 있으면 바로 돈다.

수치는 전부 JSON 에서 생성하고 손으로 적지 않는다. 원인 귀속 함수(attribute_*)는
tests/test_regress_0913.py 가 단위로 검증한다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tools" / "lh_baseline"))

for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if callable(_reconf):
        try:
            _reconf(encoding="utf-8")
        except (ValueError, OSError):
            pass

MEETING_DIR = REPO / "docs" / "reports" / "meeting_20260911"
OUT_DIR = REPO / "docs" / "reports" / "regress_20260913"
BASELINE_118 = MEETING_DIR / "정민재_118건_결과.json"
BASELINE_17 = MEETING_DIR / "정민재_17건_결과.json"

STAMP = "20260913"
RULE_PACK_LABEL = "v1.6"

# ── 결정 번호 ↔ 의미 ──────────────────────────────────────────────────────────
DECISIONS: dict[str, str] = {
    "1": "공장 판정 — 등록공장 소재 검토 표시(대기·소음 부가)",
    "2": "휴업 시설 판정 대상 포함",
    "3": "고압가스 자가설비(기관 자체 사용) 제외",
    "4": "LPG·CNG 충전소 25m 예외",
    "5": "석유대체연료 용도지역 확인 요청",
    "6": "생활숙박업(숙박업(생활)) 제외",
    "8": "대학·종합병원 정문 기준 측정",
    "9": "다필지 합집합(인접 필지 연계)",
    "G": "예비검색 슬랙(경계거리 재판정)",
    "WIRING": "원천 배선(환경 차이 — 로컬 원천 적재)",
    "UNATTRIBUTED": "미귀속",
}

# 결정 ↔ 코드 위치·값(구조 정보 — 수치가 아니라 코드 근거라 정적으로 둔다).
DECISION_IMPL: list[dict] = [
    {"no": "1", "where": "app/hazard_review/service.py:127-130,409,778 · rulebook RB14-FACTORY",
     "value": "등록공장 소재 시 검토 표시(절대 exclusion 금지). 대기·소음 배출은 부가 정보만."},
    {"no": "2", "where": "app/hazard_review/rulebook.py:721-738",
     "value": "휴업을 영업과 동일한 판정 대상(active)으로 포함. business_status 원문 유지."},
    {"no": "3", "where": "app/hazard_review/service.py:199-245,983",
     "value": "기관 패턴+업태 저장소/제조구분 충전 → 자가설비로 후보에서 완전 제외."},
    {"no": "4", "where": "app/hazard_review/service.py:1053 · rulebook RB14-FUEL25:471-495",
     "value": "LPG·CNG 충전소를 25m 예외 3종으로 이동. 25m·주택 전 유형."},
    {"no": "5", "where": "app/hazard_review/service.py:790-794",
     "value": "석유대체연료 후보에만 지적편집도 용도지역을 붙여 확인 요청."},
    {"no": "6", "where": "app/hazard_review/rulebook.py:629-639 · service.py:245-247",
     "value": "일반숙박에서 관광숙박·생활숙박업(숙박업(생활)) 제외."},
    {"no": "8", "where": "app/services 정문 조회(네이버) · probe_university_gate.py",
     "value": "대학·종합병원은 정문 좌표로 측정. (이 PC 네이버 키 없음 → 좌표 폴백)"},
    {"no": "9", "where": "app/hazard_review/parcels.py · multi_parcel.py",
     "value": "주소의 복수 지번을 합집합 필지로 해석(대표+인접). parcel_count>1."},
    {"no": "G", "where": "app/hazard_review/service.py:93-99,764-770 (SEARCH_SLACK_M=150)",
     "value": "판정창+슬랙까지 넓게 후보를 모으고 경계 부착 후 거리로 재판정."},
]

# 카테고리 key → 기본 귀속 결정.
FACTORY_KEYS = {
    "factory_air_specific", "factory_air_1_3", "factory_air_4_5",
    "factory_noise", "factory_adjacent", "factory_registered",
}
LPG25_KEYS = {"lpg_station", "cng_station"}
HPG_KEYS = {"high_pressure_gas"}
LODGING_KEYS = {"general_lodging"}

# 휴업/폐업 운영상태 표기(business_status 원문).
_SUSPENDED_TOKENS = ("휴업", "휴 업")


# ──────────────────────────────────────────────────────────────────────────────
# 정규화 — 기준선(full result) 과 새 결과(full result) 를 같은 비교 스키마로 편다.
# ──────────────────────────────────────────────────────────────────────────────
def _facility_brief(fac: dict) -> dict:
    return {
        "name": fac.get("name", ""),
        "distance_m": fac.get("distance_m"),
        "type": fac.get("facility_type_label") or fac.get("facility_type", ""),
        "business_status": fac.get("business_status", ""),
        "address": fac.get("address", ""),
    }


def normalize_result(result: dict) -> dict:
    """full ScreeningResult(dict) → 전후 비교용 평면 스키마.

    순수 함수다(네트워크·엔진 호출 없음). 기준선과 새 결과 모두 같은 모델을
    model_dump 한 것이라 같은 추출기가 양쪽에 쓰인다.
    """

    stage_one = result.get("stage_one", {}) or {}
    stage_two = result.get("stage_two", {}) or {}
    hazard = result.get("hazard_review", {}) or {}

    status_by_key = {
        c.get("key"): c.get("status", "") for c in hazard.get("categories", []) or []
    }

    categories: dict[str, dict] = {}
    for item in stage_one.get("items", []) or []:
        key = item.get("key")
        facilities = [_facility_brief(f) for f in item.get("facilities", []) or []]
        categories[key] = {
            "outcome": item.get("outcome", ""),
            "status": status_by_key.get(key, ""),
            "nearest_distance_m": item.get("nearest_distance_m"),
            "candidate_count": item.get("candidate_count", 0),
            "inside_threshold_count": item.get("inside_threshold_count", 0),
            "measurement_method": item.get("measurement_method", ""),
            "facilities": facilities,
        }

    axes = {
        c.get("key"): c.get("awarded")
        for c in stage_two.get("criteria", []) or []
    }
    axes_determined = {
        c.get("key"): bool(c.get("determined"))
        for c in stage_two.get("criteria", []) or []
    }

    return {
        "verdict": result.get("verdict"),
        "living_score": stage_two.get("living_score"),
        "living_determined": bool(stage_two.get("determined")),
        "axes": axes,
        "axes_determined": axes_determined,
        "categories": categories,
        "reasons": stage_one.get("reasons", []) or [],
        "review_reasons": stage_one.get("review_reasons", []) or [],
    }


# ──────────────────────────────────────────────────────────────────────────────
# 원인 귀속 — 순수 함수(테스트 대상).
# ──────────────────────────────────────────────────────────────────────────────
def _base_decision_for_key(key: str) -> str | None:
    if key in FACTORY_KEYS:
        return "1"
    if key in LPG25_KEYS:
        return "4"
    if key in HPG_KEYS:
        return "3"
    if key in LODGING_KEYS:
        return "6"
    return None


def _has_suspended(facilities: list[dict]) -> bool:
    for fac in facilities:
        status = str(fac.get("business_status") or "")
        if any(token in status for token in _SUSPENDED_TOKENS):
            return True
    return False


def _facility_names(facilities: list[dict]) -> set[str]:
    return {f.get("name", "") for f in facilities if f.get("name")}


def category_changed(before: dict, after: dict) -> bool:
    """카테고리에 유의미한 변화가 있는지(순수)."""

    if before.get("outcome") != after.get("outcome"):
        return True
    if before.get("status") != after.get("status"):
        return True
    if (before.get("inside_threshold_count") or 0) != (after.get("inside_threshold_count") or 0):
        return True
    bn, an = before.get("nearest_distance_m"), after.get("nearest_distance_m")
    if (bn is None) != (an is None):
        return True
    if bn is not None and an is not None and abs(bn - an) > 0.5:
        return True
    if _facility_names(before.get("facilities", [])) != _facility_names(after.get("facilities", [])):
        return True
    return False


def _dedup(causes: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for cause in causes:
        if cause not in seen:
            seen.add(cause)
            ordered.append(cause)
    return ordered


def attribute_category(
    key: str, before: dict, after: dict, parcel_count_after: int
) -> list[str]:
    """한 카테고리의 전후 차이를 결정 번호 목록으로 귀속한다(순수).

    귀속 원칙
    ---------
    - dataset_missing ↔ active 전환은 **원천 배선(WIRING)** 이 1차 원인이다. 기준선은
      해당 원천을 못 봤으므로 규칙 효과를 분리할 수 없다. 다만 데이터가 새로 붙어
      결과가 검토/부적격으로 잡히면, 그 범주를 관장하는 결정(#1 공장·#4 LPG 등)을
      「배선으로 드러난 결정」으로 함께 단다.
    - 양쪽 다 데이터가 있는데 outcome/status 가 바뀌면 그것은 **규칙 변화**라
      결정 번호로 귀속한다.
    - outcome/status 가 그대로인데 거리·시설만 미세하게 움직인 것은 라이브 원천
      노이즈로 보고 무시한다. 단 다필지(#9)로 최근접 거리가 실제로 줄면 그 기하
      효과는 기록한다.
    - 변화가 있는데 어느 결정에도 귀속되지 않으면 'UNATTRIBUTED' 로 남긴다(숨기지 않음).
    """

    b_status = before.get("status", "")
    a_status = after.get("status", "")
    b_out = before.get("outcome")
    a_out = after.get("outcome")
    # 「평가 못 함」은 두 가지다: 원천 미확보(dataset_missing)이거나, 그 범주 자체가
    # 한쪽 엔진에 없어 항목이 빠진 경우(outcome None). 등록공장 범주는 새 엔진이
    # 신설(#1)했고 기준선엔 없었으므로 outcome None = 평가 전무로 본다.
    b_missing = b_status == "dataset_missing" or b_out is None
    a_missing = a_status == "dataset_missing" or a_out is None

    # 원천 배선 전환.
    if b_missing != a_missing:
        if a_missing:
            # 데이터가 사라짐(이 PC 에선 드묾) — 환경 차이.
            return ["WIRING"]
        causes = ["WIRING"]
        if a_out in ("review", "fail"):
            base = _base_decision_for_key(key)
            if base is not None:
                causes.append(base)
            if _has_suspended(after.get("facilities", [])):
                causes.append("2")
        return _dedup(causes)

    if b_missing and a_missing:
        return []

    causes: list[str] = []

    # 다필지(#9): 인접 필지가 더해져(>1) 최근접 거리가 실제로 줄면 기하 효과로 기록.
    bn, an = before.get("nearest_distance_m"), after.get("nearest_distance_m")
    if parcel_count_after > 1 and bn is not None and an is not None and an < bn - 0.5:
        causes.append("9")

    outcome_changed = b_out != a_out
    status_changed = b_status != a_status
    if outcome_changed or status_changed:
        # 휴업(#2): 한쪽에만 휴업 근거 시설.
        if _has_suspended(after.get("facilities", [])) != _has_suspended(
            before.get("facilities", [])
        ):
            causes.append("2")
        base = _base_decision_for_key(key)
        if base is not None:
            causes.append(base)
        # 슬랙(G): 판정창 안 후보가 새로 잡혔는데(inside 증가) 다필지가 아니면 경계거리.
        if (
            (after.get("inside_threshold_count") or 0)
            > (before.get("inside_threshold_count") or 0)
            and "9" not in causes
            and parcel_count_after <= 1
        ):
            causes.append("G")
        if not causes:
            causes.append("UNATTRIBUTED")

    return _dedup(causes)


def attribute_zoning(before_review: list[str], after_review: list[str]) -> list[str]:
    """용도지역 확인 요청(#5) 문구가 새로 등장/소멸했는지(순수)."""

    def _has_zoning(reasons: list[str]) -> bool:
        return any(("용도지역" in str(r)) or ("지적편집도" in str(r)) for r in reasons)

    if _has_zoning(before_review) != _has_zoning(after_review):
        return ["5"]
    return []


# ──────────────────────────────────────────────────────────────────────────────
# 건별 전후 대조.
# ──────────────────────────────────────────────────────────────────────────────
def diff_case(before: dict, after: dict, parcel_count_after: int) -> dict:
    """정규화된 전(before)·후(after) 를 받아 전후 차이+원인을 계산(순수)."""

    category_changes: list[dict] = []
    all_causes: list[str] = []
    has_unattributed_category = False
    for key in sorted(set(before["categories"]) | set(after["categories"])):
        b = before["categories"].get(key, {})
        a = after["categories"].get(key, {})
        causes = attribute_category(key, b, a, parcel_count_after)
        if not causes:
            continue
        if "UNATTRIBUTED" in causes:
            has_unattributed_category = True
        category_changes.append({
            "key": key,
            "before": {"outcome": b.get("outcome"), "status": b.get("status"),
                       "nearest_distance_m": b.get("nearest_distance_m"),
                       "facilities": [f["name"] for f in b.get("facilities", [])]},
            "after": {"outcome": a.get("outcome"), "status": a.get("status"),
                      "nearest_distance_m": a.get("nearest_distance_m"),
                      "facilities": [f["name"] for f in a.get("facilities", [])]},
            "causes": causes,
        })
        all_causes.extend(causes)

    zoning = attribute_zoning(before["review_reasons"], after["review_reasons"])
    all_causes.extend(zoning)
    # 案건-레벨 원인 집합에서는 UNATTRIBUTED 토큰을 빼고(아래 불린으로 따로 센다)
    # 실제 귀속된 결정/배선만 남긴다.
    all_causes = [c for c in all_causes if c != "UNATTRIBUTED"]

    axis_deltas = {}
    for axis in sorted(set(before["axes"]) | set(after["axes"])):
        bv, av = before["axes"].get(axis), after["axes"].get(axis)
        if bv != av:
            axis_deltas[axis] = {"before": bv, "after": av}

    score_b, score_a = before["living_score"], after["living_score"]
    score_delta = None
    if isinstance(score_b, (int, float)) and isinstance(score_a, (int, float)):
        score_delta = score_a - score_b

    # 중복 제거한 원인 집합.
    seen: set[str] = set()
    causes_unique: list[str] = []
    for cause in all_causes:
        if cause not in seen:
            seen.add(cause)
            causes_unique.append(cause)

    verdict_changed = before["verdict"] != after["verdict"]
    # 미귀속: (1) 어느 카테고리가 outcome 은 바뀌었는데 결정에 귀속되지 않았거나,
    # (2) 판정/점수/축이 움직였는데 귀속된 원인이 하나도 없는 경우. 숨기지 않는다.
    material = verdict_changed or bool(axis_deltas) or (score_delta not in (None, 0))
    unattributed = has_unattributed_category or (material and not causes_unique)

    return {
        "verdict_before": before["verdict"],
        "verdict_after": after["verdict"],
        "verdict_changed": verdict_changed,
        "living_before": score_b,
        "living_after": score_a,
        "living_determined_before": before["living_determined"],
        "living_determined_after": after["living_determined"],
        "score_delta": score_delta,
        "axis_deltas": axis_deltas,
        "category_changes": category_changes,
        "zoning": zoning,
        "causes": causes_unique,
        "unattributed": unattributed,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 생성(generate) — 새 엔진으로 재판정.
# ──────────────────────────────────────────────────────────────────────────────
def _load_records(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["records"] if isinstance(data, dict) else data


async def _screen_case(case: dict, screening, resolver) -> dict:
    from app.hazard_review.models import HazardParcelResolveRequest, HazardSite
    from app.models import Coordinates
    from app.screening.models import ScreeningRequest

    from tools.lh_baseline.run_ours import _unresolved_from_note

    key = f"{case['seq']}_{case['housing_type']}_{case['application_type']}"
    coords = Coordinates(lat=case["lat"], lng=case["lng"])
    start = time.monotonic()
    resolved = await resolver.resolve(
        HazardParcelResolveRequest(name=key, address=case["address"], coordinates=coords)
    )
    request = ScreeningRequest(
        site=HazardSite(
            name=key,
            address=case["address"],
            coordinates=coords,
            housing_type=case["housing_type"],
            application_type=case["application_type"],
            parcels=resolved.parcels,
        ),
        include_stage_two_on_fail=True,
        requested_by="regress-20260913",
    )
    result = await screening.screen(request)
    dumped = result.model_dump(mode="json")
    return {
        "case": case,
        "result": dumped,
        "rule_pack_version": dumped.get("rule_pack_version"),
        "parcel_count": len(resolved.parcels),
        "parcels": [p.pnu for p in resolved.parcels],
        "unresolved_parcels": _unresolved_from_note(resolved.note),
        "elapsed_seconds": round(time.monotonic() - start, 2),
    }


async def generate() -> None:
    from app.config import get_settings
    from app.hazard_review.parcels import ParcelResolver
    from app.models import Coordinates
    from app.screening.router import get_screening_service
    from app.services.kakao import KakaoClient
    from app.services.vworld import VWorldClient

    from tools.lh_baseline.run_ours import (
        _provision_env_from_dotenv,
        _provision_noise_csv,
        warm_sources,
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    _provision_env_from_dotenv()
    _provision_noise_csv()

    cases_118 = [r["case"] for r in _load_records(BASELINE_118)]
    cases_17 = [r["case"] for r in _load_records(BASELINE_17)]
    # 좌표·유형·주소는 기준선 입력을 그대로 재사용(엔진 차이만 드러낸다).

    # 기준선(정민재_*_결과.json)은 앱 서버(:8000)로 산출됐다. 생활편의 원천(대규모점포·
    # 병원·정문·지적)이 전부 배선된 서버와 같은 조립을 써야 2차가 비교 가능하다. 그래서
    # 라우터의 get_screening_service() 를 그대로 쓴다(run_ours 의 bare build_service 아님).
    cfg = get_settings()
    screening = get_screening_service()
    resolver = ParcelResolver(
        kakao=KakaoClient(cfg.kakao_rest_api_key),
        vworld=VWorldClient(cfg.vworld_api_key, domain=cfg.vworld_domain),
        cadastral=screening.hazard.cadastral,
    )
    loader = getattr(screening.hazard, "_local_sources_loader", None)
    if loader is not None:
        screening.hazard.local_sources = loader()
        screening.hazard._local_sources_warmup_started = True
    centers = [Coordinates(lat=c["lat"], lng=c["lng"]) for c in cases_118]
    warm_counts, degraded = await warm_sources(screening, allow_degraded=True, centers=centers)
    print("예열:", warm_counts, "결손:", [d["source"] for d in degraded], flush=True)

    async def run_group(cases: list[dict], label: str) -> list[dict]:
        gate = asyncio.Semaphore(3)
        done = 0
        total = len(cases)

        async def guarded(case: dict) -> dict:
            nonlocal done
            async with gate:
                try:
                    row = await _screen_case(case, screening, resolver)
                except Exception as exc:  # 한 건 실패로 전체를 멈추지 않는다.
                    row = {"case": case, "error": f"{type(exc).__name__}: {exc}"}
                done += 1
                v = row.get("result", {}).get("verdict", row.get("error"))
                ls = row.get("result", {}).get("stage_two", {}).get("living_score")
                print(f"  [{label} {done}/{total}] {case['seq']} {v} score={ls}", flush=True)
                return row

        rows = await asyncio.gather(*(guarded(c) for c in cases))

        # 일시 조회 실패 재실행: living_score 가 None 인데 판정이 fail 이 아닌 건만
        # 순차로 한 번 더 돌려 채운다(인계문서 §3-1 방식).
        def _needs_rerun(row: dict) -> bool:
            if "error" in row:
                return True
            res = row.get("result", {})
            two = res.get("stage_two", {})
            return res.get("verdict") != "fail" and two.get("living_score") is None

        rerun = [r["case"] for r in rows if _needs_rerun(r)]
        if rerun:
            print(f"  [{label}] 재실행 대상 {len(rerun)}건: "
                  f"{[c['seq'] for c in rerun]}", flush=True)
            by_seq = {f"{r['case']['seq']}_{r['case']['housing_type']}_{r['case']['application_type']}": i
                      for i, r in enumerate(rows)}
            for case in rerun:
                try:
                    fresh = await _screen_case(case, screening, resolver)
                except Exception as exc:
                    print(f"    재실행 실패 {case['seq']}: {exc}", flush=True)
                    continue
                k = f"{case['seq']}_{case['housing_type']}_{case['application_type']}"
                if not _needs_rerun(fresh):
                    rows[by_seq[k]] = fresh
                    print(f"    재실행 성공 {case['seq']} score="
                          f"{fresh['result']['stage_two']['living_score']}", flush=True)
        return rows

    def _write(path: Path, records: list[dict], sample: int, note: str) -> None:
        path.write_text(json.dumps({
            "generated_at": datetime.now().astimezone().isoformat(),
            "sample": sample,
            "conditions_note": note,
            "engine": f"작업트리(커밋 8b2201d + 미커밋) · 규칙팩 {RULE_PACK_LABEL}",
            "warm_counts": warm_counts,
            "degraded": [d["source"] for d in degraded],
            "records": records,
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print("저장:", path, f"({len(records)}건)", flush=True)

    rows_17 = await run_group(cases_17, "17")
    rows_15 = [r for r in rows_17 if r["case"]["seq"] not in ("111", "115")]
    rows_118 = await run_group(cases_118, "118")

    _write(OUT_DIR / f"정민재_17건_결과_{STAMP}.json", rows_17, 17,
           "17건은 LH 표본 유형. 결정 반영 엔진 · 로컬 원천 적재.")
    _write(OUT_DIR / f"정민재_15건_결과_{STAMP}.json", rows_15, 15,
           "15건은 조준환 검증 유형(17건에서 111·115 제외). 결정 반영 엔진.")
    _write(OUT_DIR / f"정민재_118건_결과_{STAMP}.json", rows_118, 118,
           "118건은 주택·일반 공통. 결정 반영 엔진 · 로컬 원천 적재.")


# ──────────────────────────────────────────────────────────────────────────────
# 보고(report).
# ──────────────────────────────────────────────────────────────────────────────
def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _records_by_key(records: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rec in records:
        c = rec["case"]
        out[f"{c['seq']}_{c['housing_type']}_{c['application_type']}"] = rec
    return out


def build_report() -> dict:
    baseline_118 = _records_by_key(_load_records(BASELINE_118))
    baseline_17 = _records_by_key(_load_records(BASELINE_17))

    new_118 = _records_by_key(_load_records(OUT_DIR / f"정민재_118건_결과_{STAMP}.json"))
    new_17 = _records_by_key(_load_records(OUT_DIR / f"정민재_17건_결과_{STAMP}.json"))
    new_15 = _records_by_key(_load_records(OUT_DIR / f"정민재_15건_결과_{STAMP}.json"))

    def compare_group(base_map: dict, new_map: dict, lh_from: dict | None) -> dict:
        rows = []
        for key in new_map:
            new_rec = new_map[key]
            base_rec = base_map.get(key)
            if base_rec is None or "result" not in base_rec or "result" not in new_rec:
                continue
            before = normalize_result(base_rec["result"])
            after = normalize_result(new_rec["result"])
            d = diff_case(before, after, new_rec.get("parcel_count", 1) or 1)
            case = new_rec["case"]
            d.update({
                "seq": case["seq"],
                "key": key,
                "address": case["address"],
                "housing_type": case["housing_type"],
                "application_type": case["application_type"],
                "parcel_count": new_rec.get("parcel_count"),
                "unresolved_parcels": new_rec.get("unresolved_parcels", []),
                "lh_actual": case.get("lh_actual"),
            })
            rows.append(d)
        return {"rows": sorted(rows, key=lambda r: r["seq"])}

    groups = {
        "118": compare_group(baseline_118, new_118, None),
        "17": compare_group(baseline_17, new_17, baseline_17),
        "15": compare_group(baseline_17, new_15, baseline_17),
    }

    # 요약 수치(전부 여기서 집계).
    summary = {}
    for name, grp in groups.items():
        rows = grp["rows"]
        verdict_before = _count([r["verdict_before"] for r in rows])
        verdict_after = _count([r["verdict_after"] for r in rows])
        determined_before = sum(r["living_determined_before"] for r in rows)
        determined_after = sum(r["living_determined_after"] for r in rows)
        scores_before = [r["living_before"] for r in rows
                         if isinstance(r["living_before"], (int, float))]
        scores_after = [r["living_after"] for r in rows
                        if isinstance(r["living_after"], (int, float))]
        cause_counts: dict[str, int] = {}
        for r in rows:
            for cause in r["causes"]:
                cause_counts[cause] = cause_counts.get(cause, 0) + 1
        unattributed = [r["seq"] for r in rows if r["unattributed"]]
        multi_parcel = [r["seq"] for r in rows if (r.get("parcel_count") or 1) > 1]
        lh_equal_before = lh_equal_after = None
        if name in ("15", "17"):
            lh_equal_before = sum(
                1 for r in rows if r["lh_actual"] is not None
                and r["living_before"] == r["lh_actual"])
            lh_equal_after = sum(
                1 for r in rows if r["lh_actual"] is not None
                and r["living_after"] == r["lh_actual"])
        summary[name] = {
            "n": len(rows),
            "verdict_before": verdict_before,
            "verdict_after": verdict_after,
            "verdict_changed": sum(r["verdict_changed"] for r in rows),
            "determined_before": determined_before,
            "determined_after": determined_after,
            "avg_before": round(sum(scores_before) / len(scores_before), 2) if scores_before else None,
            "avg_after": round(sum(scores_after) / len(scores_after), 2) if scores_after else None,
            "cause_counts": dict(sorted(cause_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
            "unattributed_seqs": unattributed,
            "multi_parcel_seqs": multi_parcel,
            "lh_equal_before": lh_equal_before,
            "lh_equal_after": lh_equal_after,
        }

    hashes = {
        "기준선 118": _sha256(BASELINE_118),
        "기준선 17": _sha256(BASELINE_17),
        f"새 118 ({STAMP})": _sha256(OUT_DIR / f"정민재_118건_결과_{STAMP}.json"),
        f"새 17 ({STAMP})": _sha256(OUT_DIR / f"정민재_17건_결과_{STAMP}.json"),
        f"새 15 ({STAMP})": _sha256(OUT_DIR / f"정민재_15건_결과_{STAMP}.json"),
        "service.py": _sha256(BACKEND / "app" / "hazard_review" / "service.py"),
        "rulebook.py": _sha256(BACKEND / "app" / "hazard_review" / "rulebook.py"),
        "parcels.py": _sha256(BACKEND / "app" / "hazard_review" / "parcels.py"),
        "multi_parcel.py": _sha256(BACKEND / "app" / "hazard_review" / "multi_parcel.py"),
    }

    report = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "measured_on": "2026-09-13",
        "rule_pack": RULE_PACK_LABEL,
        "commit": "8b2201d + 미커밋 작업 트리",
        "decisions": DECISIONS,
        "decision_impl": DECISION_IMPL,
        "summary": summary,
        "groups": groups,
        "hashes": hashes,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "regress.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("저장:", OUT_DIR / "regress.json", flush=True)
    return report


def _count(values: list) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HTML (외부 요청 0 · 인라인 CSS).
# ──────────────────────────────────────────────────────────────────────────────
def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


VERDICT_LABEL = {"pass": "적격", "review": "검토", "fail": "부적격", None: "—"}


def build_html(report: dict) -> str:
    s = report["summary"]
    out: list[str] = []
    A = out.append

    A("<!doctype html><html lang='ko'><head><meta charset='utf-8'>")
    A("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    A("<title>LH 판정로직 v1.6 결정반영 회귀검증 · 정민재앱 · 2026-09-13</title>")
    A("<style>")
    A("""
    :root{--ink:#14181f;--muted:#5b6472;--line:#e3e7ee;--bg:#fff;--soft:#f6f8fb;
    --pos:#0b7a3b;--neg:#b42318;--warn:#9a6700;--accent:#1b4fd8;}
    *{box-sizing:border-box}
    body{margin:0;background:var(--soft);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Pretendard',sans-serif;
    line-height:1.55;font-size:15px;}
    .wrap{max-width:1120px;margin:0 auto;padding:32px 28px 80px;}
    header{border-bottom:2px solid var(--ink);padding-bottom:16px;margin-bottom:8px;}
    h1{font-size:24px;margin:0 0 6px;letter-spacing:-.01em;}
    .sub{color:var(--muted);font-size:13px;}
    .meta{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;}
    .chip{background:#eef2fb;color:#1b3a8f;border-radius:999px;padding:3px 11px;font-size:12px;font-weight:600;}
    .chip.warn{background:#fdf3e3;color:#8a5a00;}
    h2{font-size:18px;margin:40px 0 6px;padding-top:8px;border-top:1px solid var(--line);}
    h2 .n{color:var(--accent);font-variant-numeric:tabular-nums;}
    p.lead{color:var(--muted);margin:4px 0 14px;font-size:14px;}
    table{border-collapse:collapse;width:100%;margin:10px 0 6px;font-size:13px;
    background:var(--bg);border:1px solid var(--line);border-radius:8px;overflow:hidden;}
    th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);
    vertical-align:top;font-variant-numeric:tabular-nums;}
    th{background:#f0f3f8;font-weight:700;font-size:12px;color:#38414f;
    position:sticky;top:0;}
    tbody tr:last-child td{border-bottom:none;}
    td.num,th.num{text-align:right;}
    .pos{color:var(--pos);font-weight:700;} .neg{color:var(--neg);font-weight:700;}
    .warn{color:var(--warn);font-weight:700;}
    .mut{color:var(--muted);}
    .tag{display:inline-block;background:#eef2fb;color:#1b3a8f;border-radius:6px;
    padding:1px 7px;font-size:11px;font-weight:700;margin:1px 2px 1px 0;}
    .tag.w{background:#fdf3e3;color:#8a5a00;} .tag.u{background:#fdecea;color:#9b1c12;}
    .scroll{overflow-x:auto;border:1px solid var(--line);border-radius:8px;}
    .scroll table{border:none;border-radius:0;}
    code{background:#f0f3f8;padding:1px 5px;border-radius:4px;font-size:12px;}
    .note{background:#fdf3e3;border-left:4px solid var(--warn);padding:10px 14px;
    border-radius:0 8px 8px 0;margin:12px 0;font-size:13px;color:#6b4e00;}
    .grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:12px 0;}
    .card{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:14px 16px;}
    .card h3{margin:0 0 8px;font-size:14px;}
    .kv{display:flex;justify-content:space-between;font-size:13px;padding:2px 0;}
    @media(max-width:720px){.grid{grid-template-columns:1fr;}.wrap{padding:20px 14px 60px;}}
    """)
    A("</style></head><body><div class='wrap'>")

    # 헤더
    A("<header>")
    A("<h1>LH 판정로직 v1.6 · 결정반영 회귀검증 (정민재 앱)</h1>")
    A("<div class='sub'>2026-09-11 LH 2차 보고 회의 결정을 반영한 엔진으로 15·17·118건을 재검산하고 전후를 대조한다.</div>")
    A("<div class='meta'>")
    A(f"<span class='chip'>실측일 {_esc(report['measured_on'])}</span>")
    A(f"<span class='chip'>규칙팩 {_esc(report['rule_pack'])}</span>")
    A(f"<span class='chip'>커밋 {_esc(report['commit'])}</span>")
    A("<span class='chip warn'>원천: 이 PC 로컬 적재(기준선 대비 dataset_missing 해소)</span>")
    A("</div></header>")

    # §1 요지
    A("<h2>1. 요지</h2>")
    A("<p class='lead'>같은 입력(주소·좌표·유형)을 결정 반영 전/후 엔진으로 돌린 전후 대조다. "
      "차이는 결정 번호로 귀속하고, 로컬 원천 적재로 인한 변화는 환경 차이(원천 배선)로 분리한다.</p>")
    A("<div class='grid'>")
    for name, title in (("118", "118건 (주택·일반)"), ("17", "17건 (LH 표본)"), ("15", "15건 (조준환 검증)")):
        g = s[name]
        A("<div class='card'>")
        A(f"<h3>{_esc(title)} · n={g['n']}</h3>")
        A(f"<div class='kv'><span class='mut'>판정 변경</span><b>{g['verdict_changed']}건</b></div>")
        A(f"<div class='kv'><span class='mut'>2차 확정 전→후</span><b>{g['determined_before']} → {g['determined_after']}</b></div>")
        A(f"<div class='kv'><span class='mut'>2차 평균 전→후</span><b>{_esc(g['avg_before'])} → {_esc(g['avg_after'])}</b></div>")
        if g["lh_equal_before"] is not None:
            A(f"<div class='kv'><span class='mut'>LH 실제 일치 전→후</span><b>{g['lh_equal_before']} → {g['lh_equal_after']}</b></div>")
        A(f"<div class='kv'><span class='mut'>미귀속</span><b>{len(g['unattributed_seqs'])}건</b></div>")
        A("</div>")
    A("</div>")

    # §2 기준선·투입
    A("<h2>2. 기준선 · 투입</h2>")
    A("<table><thead><tr><th>구분</th><th>기준선(결정 전)</th><th>이번(결정 후)</th><th>조건</th></tr></thead><tbody>")
    A(f"<tr><td>118건</td><td>정민재_118건_결과.json</td><td>정민재_118건_결과_{STAMP}.json</td><td>주택·일반 공통</td></tr>")
    A(f"<tr><td>17건</td><td>정민재_17건_결과.json</td><td>정민재_17건_결과_{STAMP}.json</td><td>LH 표본 유형</td></tr>")
    A(f"<tr><td>15건</td><td>17건 중 111·115 제외</td><td>정민재_15건_결과_{STAMP}.json</td><td>조준환 검증 유형</td></tr>")
    A("</tbody></table>")
    A("<div class='note'>기준선은 커밋 8b2201d(2026-09-11 08:15) 스냅샷으로, 이 PC 에 로컬 원천이 붙기 전 13개 범주가 "
      "dataset_missing 이었다. 이번 재검산은 로컬 원천을 적재해 돌렸으므로, dataset_missing 이 풀린 변화는 "
      "결정이 아니라 <b>환경 차이(원천 배선)</b>로 따로 집계했다.</div>")

    # §3 결정 ↔ 반영 대응표
    A("<h2>3. 결정 ↔ 반영 대응표 <span class='n'>(코드 위치·값)</span></h2>")
    A("<div class='scroll'><table><thead><tr><th>#</th><th>결정</th><th>코드 위치</th><th>반영 값</th></tr></thead><tbody>")
    for impl in report["decision_impl"]:
        A(f"<tr><td><b>#{_esc(impl['no'])}</b></td><td>{_esc(report['decisions'].get(impl['no'], ''))}</td>"
          f"<td><code>{_esc(impl['where'])}</code></td><td>{_esc(impl['value'])}</td></tr>")
    A("</tbody></table></div>")

    # §4-1 분포·원인 분해
    A("<h2>4-1. 유형별 분포 · 원인 분해</h2>")
    A("<table><thead><tr><th>그룹</th><th class='num'>n</th><th>1차 전(적/검/부)</th><th>1차 후(적/검/부)</th>"
      "<th class='num'>판정변경</th><th class='num'>2차확정 전→후</th><th>2차평균 전→후</th></tr></thead><tbody>")
    for name in ("118", "17", "15"):
        g = s[name]
        vb, va = g["verdict_before"], g["verdict_after"]
        def trip(d):
            return f"{d.get('pass',0)}/{d.get('review',0)}/{d.get('fail',0)}"
        A(f"<tr><td>{name}건</td><td class='num'>{g['n']}</td><td>{trip(vb)}</td><td>{trip(va)}</td>"
          f"<td class='num'>{g['verdict_changed']}</td><td class='num'>{g['determined_before']}→{g['determined_after']}</td>"
          f"<td>{_esc(g['avg_before'])}→{_esc(g['avg_after'])}</td></tr>")
    A("</tbody></table>")

    A("<p class='lead'>원인별 건수(한 건이 여러 결정에 걸릴 수 있어 합이 n 을 넘을 수 있다). "
      "WIRING=원천 배선(환경), UNATTRIBUTED=미귀속.</p>")
    A("<div class='scroll'><table><thead><tr><th>원인</th><th>의미</th>"
      "<th class='num'>118</th><th class='num'>17</th><th class='num'>15</th></tr></thead><tbody>")
    all_causes = sorted(
        set().union(*[set(s[n]["cause_counts"]) for n in ("118", "17", "15")]),
        key=lambda c: (c == "UNATTRIBUTED", c == "WIRING", c),
    )
    for cause in all_causes:
        label = report["decisions"].get(cause, cause)
        cls = " class='warn'" if cause in ("WIRING", "UNATTRIBUTED") else ""
        A(f"<tr><td{cls}>{_esc(cause)}</td><td>{_esc(label)}</td>"
          f"<td class='num'>{s['118']['cause_counts'].get(cause,0)}</td>"
          f"<td class='num'>{s['17']['cause_counts'].get(cause,0)}</td>"
          f"<td class='num'>{s['15']['cause_counts'].get(cause,0)}</td></tr>")
    A("</tbody></table></div>")

    # 미귀속 건 명시
    A("<p class='lead'>미귀속 접수번호 — 숨기지 않고 전부 적는다.</p><ul>")
    for name in ("118", "17", "15"):
        seqs = s[name]["unattributed_seqs"]
        A(f"<li>{name}건: {('없음' if not seqs else ', '.join(seqs))}</li>")
    A("</ul>")

    # §4-6 118건 전수표
    A("<h2>4-6. 118건 전수 전후표</h2>")
    A("<p class='lead'>판정(1차)·2차 점수·원인. 변화 없는 건도 모두 싣는다.</p>")
    A("<div class='scroll'><table><thead><tr><th>접수</th><th>소재지</th><th class='num'>필지</th>"
      "<th>1차 전→후</th><th>2차 전→후</th><th>원인</th></tr></thead><tbody>")
    for r in report["groups"]["118"]["rows"]:
        A(_row_html(r))
    A("</tbody></table></div>")

    # §4-7 17건·15건 LH 대조
    for name, title in (("17", "4-7a. 17건 LH 표본 대조"), ("15", "4-7b. 15건 조준환 검증 대조")):
        A(f"<h2>{_esc(title)}</h2>")
        A("<div class='scroll'><table><thead><tr><th>접수</th><th>유형</th><th class='num'>필지</th>"
          "<th>1차 전→후</th><th>2차 전→후</th><th class='num'>LH실제</th><th>원인</th></tr></thead><tbody>")
        for r in report["groups"][name]["rows"]:
            A(_row_html(r, lh=True))
        A("</tbody></table></div>")

    # 미반영·대기 + 환경 제약
    A("<h2>5. 미반영 · 대기 · 환경 제약</h2>")
    A("<div class='note'><b>환경 제약</b> — 이 PC 에 네이버 검색 키가 없어 대학·종합병원의 "
      "정문 자동 조회(#8)가 동작하지 않고 <b>좌표 폴백</b>으로 측정한다. 교육 점수 축에 영향이 있을 수 있다.</div>")
    A("<ul>"
      "<li>#8 정문 기준 측정: 네이버 키 부재로 좌표 폴백(위 환경 제약).</li>"
      "<li>원천 배선(WIRING): 기준선 13개 범주 dataset_missing → 로컬 원천 적재로 해소. "
      "결정이 아니라 환경 차이라 별도 집계.</li>"
      "<li>미귀속 건은 §4-1 에 접수번호로 전부 노출했다.</li>"
      "</ul>")

    # 재현 방법·해시
    A("<h2>6. 재현 방법 · 파일 해시</h2>")
    A("<p class='lead'><code>python -m tools.lh_baseline.regress_0913 generate</code> 로 재판정 후 "
      "<code>python -m tools.lh_baseline.regress_0913 report</code> 로 이 문서를 만든다. "
      "PYTHONIOENCODING=utf-8, backend 의 .venv 파이썬.</p>")
    A("<table><thead><tr><th>파일</th><th>SHA-256</th></tr></thead><tbody>")
    for name, digest in report["hashes"].items():
        A(f"<tr><td>{_esc(name)}</td><td><code>{_esc(digest)}</code></td></tr>")
    A("</tbody></table>")
    A(f"<p class='sub'>생성 {_esc(report['generated_at'])} · 외부 요청 0 · 인라인 CSS.</p>")

    A("</div></body></html>")
    return "".join(out)


def _cause_tags(causes: list[str]) -> str:
    if not causes:
        return "<span class='mut'>변화 없음</span>"
    tags = []
    for c in causes:
        cls = "tag"
        if c == "WIRING":
            cls = "tag w"
        elif c == "UNATTRIBUTED":
            cls = "tag u"
        label = f"#{c}" if c.isdigit() else c
        tags.append(f"<span class='{cls}'>{_esc(label)}</span>")
    return "".join(tags)


def _fmt_score(r: dict) -> str:
    b = r["living_before"] if r["living_before"] is not None else "—"
    a = r["living_after"] if r["living_after"] is not None else "—"
    delta = r["score_delta"]
    arrow = ""
    if isinstance(delta, (int, float)) and delta != 0:
        cls = "pos" if delta > 0 else "neg"
        arrow = f" <span class='{cls}'>({delta:+d})</span>"
    return f"{_esc(b)} → {_esc(a)}{arrow}"


def _fmt_verdict(r: dict) -> str:
    vb = VERDICT_LABEL.get(r["verdict_before"], r["verdict_before"])
    va = VERDICT_LABEL.get(r["verdict_after"], r["verdict_after"])
    if r["verdict_changed"]:
        return f"<b class='neg'>{_esc(vb)} → {_esc(va)}</b>"
    return f"{_esc(vb)} → {_esc(va)}"


def _row_html(r: dict, lh: bool = False) -> str:
    pc = r.get("parcel_count")
    pc_cls = " class='num pos'" if (pc or 1) > 1 else " class='num'"
    cells = [
        f"<td>{_esc(r['seq'])}</td>",
    ]
    if lh:
        cells.append(f"<td class='mut'>{_esc(r['housing_type'])}/{_esc(r['application_type'])}</td>")
    else:
        cells.append(f"<td class='mut'>{_esc(r['address'])}</td>")
    cells.append(f"<td{pc_cls}>{_esc(pc)}</td>")
    cells.append(f"<td>{_fmt_verdict(r)}</td>")
    cells.append(f"<td>{_fmt_score(r)}</td>")
    if lh:
        cells.append(f"<td class='num'>{_esc(r.get('lh_actual'))}</td>")
    cells.append(f"<td>{_cause_tags(r['causes'])}</td>")
    return "<tr>" + "".join(cells) + "</tr>"


def write_html(report: dict) -> Path:
    path = OUT_DIR / f"LH_판정로직_v1.6_결정반영_회귀검증_정민재앱_2026-09-13.html"
    path.write_text(build_html(report), encoding="utf-8")
    print("저장:", path, f"({path.stat().st_size/1024:.0f}KB)", flush=True)
    return path


def report_command() -> None:
    report = build_report()
    write_html(report)


def main() -> None:
    parser = argparse.ArgumentParser(description="2026-09-11 결정 반영 전후 회귀 검증")
    parser.add_argument("command", choices=["generate", "report"])
    args = parser.parse_args()
    if args.command == "generate":
        asyncio.run(generate())
    else:
        report_command()


if __name__ == "__main__":
    main()
