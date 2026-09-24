"""관리자 「기준 편집」 — 임계거리 덮어쓰기 · 2027 완화안 · LH 개별 확인 제외 시설."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import rules_config
from app.hazard_review.rulebook import default_threshold_for, threshold_for
from app.rules_config import ExcludedFacility, RulesConfig, excluded_reason, save_config, threshold_key
from app.screening.scorebook import (
    PASS_THRESHOLD,
    PASS_THRESHOLD_RELAXED,
    Facts,
    bonus_criterion,
    evaluate,
    pass_threshold_for,
    sheet_for,
)


@pytest.fixture(autouse=True)
def _isolated_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rules_config, "RULES_PATH", tmp_path / "rule_overrides.json")
    monkeypatch.setattr(rules_config, "_cache", None)
    yield


def test_threshold_override_beats_matrix_and_default_is_kept() -> None:
    assert threshold_for("RB14-FACTORY", "house", "general") == 50
    save_config(RulesConfig(stage1_thresholds={threshold_key("house", "general", "factory"): 80}))

    assert threshold_for("RB14-FACTORY", "house", "general") == 80
    assert default_threshold_for("RB14-FACTORY", "house", "general") == 50
    # 다른 조합은 건드리지 않는다.
    assert threshold_for("RB14-FACTORY", "house", "youth") == 50


def test_override_can_turn_a_rule_off() -> None:
    save_config(RulesConfig(stage1_thresholds={threshold_key("house", "general", "factory"): None}))
    assert threshold_for("RB14-FACTORY", "house", "general") is None


def test_excluded_facility_matches_loosely() -> None:
    save_config(RulesConfig(excluded_facilities=[ExcludedFacility(name="SK신흥주유소", reason="철거 확인")]))

    assert excluded_reason("SK신흥주유소") == "철거 확인"
    assert excluded_reason("SK 신흥주유소(효자동)") == "철거 확인"
    assert excluded_reason("GS신흥주유소") is None


def _facts(**distances: list[float]) -> Facts:
    return Facts(distances, set())


def test_relaxed_education_accepts_either_condition() -> None:
    # 초·중 500m 안이지만 고등학교가 1km 밖 — 현행은 8점, 완화안은 10점.
    facts = _facts(school_elementary=[300.0], school_middle=[400.0], school_high=[1400.0])
    current = next(c for c in sheet_for("general").criteria if c.key == "education")
    relaxed = next(c for c in sheet_for("general", relaxed=True).criteria if c.key == "education")

    assert evaluate(current, facts)[0] == 8
    assert evaluate(relaxed, facts)[0] == 10
    # 60% 이하 등급은 그대로다.
    assert [t.points for t in relaxed.tiers] == [10, 8, 6, 4, 2]


def test_relaxed_bonus_extends_to_university_and_youth_gets_ten() -> None:
    facts = _facts(university=[300.0])
    assert evaluate(bonus_criterion("common"), facts)[0] == 0
    assert evaluate(bonus_criterion("common", relaxed=True), facts)[0] == 5
    assert evaluate(bonus_criterion("youth", relaxed=True), facts)[0] == 10
    # 청년형도 역세권만 있으면 5점.
    assert evaluate(bonus_criterion("youth", relaxed=True), _facts(subway=[200.0]))[0] == 5


def test_pass_threshold_follows_relaxed_flag() -> None:
    assert pass_threshold_for(False) == PASS_THRESHOLD == 70
    assert pass_threshold_for(True) == PASS_THRESHOLD_RELAXED == 65


def test_rules_api_roundtrip_is_loopback_only() -> None:
    import os

    os.environ["DEMO_MODE"] = "true"
    from fastapi.testclient import TestClient

    from app.main import app

    outsider = TestClient(app, client=("203.0.113.9", 51000))
    assert outsider.get("/api/settings/rules").status_code == 403

    client = TestClient(app, client=("127.0.0.1", 51000))
    initial = client.get("/api/settings/rules").json()
    assert initial["config"]["relaxed_2027"] is False
    cell = next(c for c in initial["cells"] if c["key"] == "house:general:factory")
    assert cell == {**cell, "default": 50, "value": 50, "overridden": False}

    saved = client.put(
        "/api/settings/rules",
        json={
            # 정본과 같은 값(officetel general cremation 500)은 덮어쓰기로 남지 않는다.
            "stage1_thresholds": {"house:general:factory": 80, "officetel:general:cremation_military": 500},
            "relaxed_2027": True,
            "excluded_facilities": [{"name": " SK신흥주유소 ", "reason": "철거"}, {"name": "", "reason": "빈 이름은 버림"}],
        },
    ).json()
    assert saved["config"]["stage1_thresholds"] == {"house:general:factory": 80}
    assert saved["config"]["relaxed_2027"] is True
    assert saved["config"]["excluded_facilities"] == [{"name": "SK신흥주유소", "reason": "철거"}]
    cell = next(c for c in saved["cells"] if c["key"] == "house:general:factory")
    assert cell["value"] == 80 and cell["overridden"] is True and cell["default"] == 50
    # 판정 경로도 같은 값을 읽는다.
    assert threshold_for("RB14-FACTORY", "house", "general") == 80

    bad = client.put("/api/settings/rules", json={"stage1_thresholds": {"house:general:nope": 1}})
    assert bad.status_code == 400
