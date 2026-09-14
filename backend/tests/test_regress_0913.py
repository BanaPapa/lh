"""regress_0913 원인 귀속 함수 단위 검증.

전후 회귀 보고의 수치는 전부 이 귀속 로직에서 나온다. 로직이 흔들리면 보고가
흔들리므로, 결정별 대표 변화 몇 가지를 고정한다(순수 함수라 네트워크·엔진 불필요).
"""

from __future__ import annotations

from tools.lh_baseline.regress_0913 import (
    attribute_category,
    attribute_zoning,
    diff_case,
    normalize_result,
)


def _cat(outcome="pass", status="", nearest=None, inside=0, facilities=None):
    return {
        "outcome": outcome,
        "status": status,
        "nearest_distance_m": nearest,
        "candidate_count": len(facilities or []),
        "inside_threshold_count": inside,
        "measurement_method": "",
        "facilities": facilities or [],
    }


def test_dataset_missing_resolved_is_wiring():
    # 기준선 dataset_missing → 로컬 원천 적재로 통과. 결정이 아니라 환경 차이.
    before = _cat(outcome="pass", status="dataset_missing")
    after = _cat(outcome="pass", status="no_conflict_in_snapshot")
    causes = attribute_category("factory_air_specific", before, after, parcel_count_after=1)
    assert "WIRING" in causes


def test_factory_outcome_change_attributes_decision_1():
    # 등록공장 카테고리가 통과→검토로 바뀌면 #1 로 귀속.
    before = _cat(outcome="pass", status="no_conflict_in_snapshot")
    after = _cat(outcome="review", status="review_required",
                 facilities=[{"name": "등록공장", "business_status": "영업/정상"}])
    causes = attribute_category("factory_adjacent", before, after, parcel_count_after=1)
    assert "1" in causes
    assert "UNATTRIBUTED" not in causes


def test_lpg_station_change_attributes_decision_4():
    before = _cat(outcome="fail", status="exclusion_match", nearest=20.0,
                  facilities=[{"name": "LPG충전소", "business_status": "영업"}])
    after = _cat(outcome="pass", status="no_conflict_in_snapshot")
    causes = attribute_category("lpg_station", before, after, parcel_count_after=1)
    assert "4" in causes


def test_multi_parcel_distance_drop_attributes_decision_9():
    # 인접 필지가 더해져(>1) 최근접 거리가 줄면 #9.
    before = _cat(outcome="pass", nearest=60.0)
    after = _cat(outcome="pass", nearest=40.0)
    causes = attribute_category("gas_station", before, after, parcel_count_after=3)
    assert "9" in causes


def test_suspended_facility_change_attributes_decision_2():
    # 휴업 근거 시설이 한쪽에만 있으면 휴업 포함(#2)로 귀속.
    before = _cat(outcome="pass")
    after = _cat(outcome="review", status="review_required",
                 facilities=[{"name": "휴업주유소", "business_status": "휴업"}])
    causes = attribute_category("gas_station", before, after, parcel_count_after=1)
    assert "2" in causes


def test_no_change_returns_empty():
    same = _cat(outcome="pass", status="no_conflict_in_snapshot")
    assert attribute_category("gas_station", same, dict(same), parcel_count_after=1) == []


def test_zoning_review_reason_attributes_decision_5():
    assert attribute_zoning([], ["용도지역 확인 요청"]) == ["5"]
    assert attribute_zoning(["용도지역 확인 요청"], ["용도지역 확인 요청"]) == []


def test_diff_case_unattributed_when_score_moves_without_cause():
    # 카테고리 변화 없이 점수만 움직이면 미귀속으로 남는다(숨기지 않는다).
    before = normalize_result({
        "verdict": "pass",
        "stage_one": {"items": [], "reasons": [], "review_reasons": []},
        "stage_two": {"living_score": 38, "determined": True, "criteria": [
            {"key": "education", "awarded": 13, "determined": True}]},
        "hazard_review": {"categories": []},
    })
    after = normalize_result({
        "verdict": "pass",
        "stage_one": {"items": [], "reasons": [], "review_reasons": []},
        "stage_two": {"living_score": 35, "determined": True, "criteria": [
            {"key": "education", "awarded": 10, "determined": True}]},
        "hazard_review": {"categories": []},
    })
    d = diff_case(before, after, parcel_count_after=1)
    assert d["score_delta"] == -3
    assert d["unattributed"] is True
