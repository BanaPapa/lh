import os
import time

from fastapi.testclient import TestClient


os.environ["DEMO_MODE"] = "true"
os.environ["KAKAO_REST_API_KEY"] = ""
os.environ["VWORLD_API_KEY"] = ""

from app.main import app


client = TestClient(app)


def hazard_payload(
    housing_type: str = "house",
    application_type: str = "multi_child",
) -> dict[str, object]:
    return {
        "site": {
            "name": "검증 사업지",
            "address": "경기도 성남시 분당구 판교역로 235",
            "coordinates": {"lat": 37.40111, "lng": 127.10853},
            "housing_type": housing_type,
            "application_type": application_type,
            "parcels": [
                {
                    "parcel_id": "prototype-parcel",
                    "pnu": "확인 필요",
                    "address": "경기도 성남시 분당구 판교역로 235",
                    "geometry": [
                        {"lat": 37.40095, "lng": 127.10833},
                        {"lat": 37.40095, "lng": 127.10873},
                        {"lat": 37.40127, "lng": 127.10873},
                        {"lat": 37.40127, "lng": 127.10833},
                        {"lat": 37.40095, "lng": 127.10833},
                    ],
                    "geometry_source": "provisional_polygon",
                    "geometry_note": "테스트 임시 필지",
                }
            ],
        },
        "rule_pack_id": "lh-rulebook-v1.4",
        "requested_by": "pytest",
    }


def wait_for_job(job_id: str) -> dict[str, object]:
    body: dict[str, object] = {}
    for _ in range(100):
        response = client.get(f"/api/hazard-review/jobs/{job_id}")
        assert response.status_code == 200
        body = response.json()
        if body["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    return body


def test_rule_pack_catalog_exposes_the_single_v14_pack() -> None:
    response = client.get("/api/hazard-review/rule-packs")

    assert response.status_code == 200
    packs = response.json()
    assert len(packs) == 1
    assert packs[0]["id"] == "lh-rulebook-v1.4"
    assert {rule["rule_id"] for rule in packs[0]["rules"]} == {
        "RB14-FACTORY",
        "RB14-HAZMAT",
        "RB14-FUEL25",
        "RB14-AMUSEMENT",
        "RB14-LODGING",
        "RB14-CREMATION-MILITARY",
    }


def test_application_types_returns_the_full_matrix() -> None:
    response = client.get("/api/hazard-review/application-types")

    assert response.status_code == 200
    body = response.json()
    assert len(body["combos"]) == 10
    officetel_multi = next(
        combo
        for combo in body["combos"]
        if combo["housing_type"] == "officetel"
        and combo["application_type"] == "multi_child"
    )
    assert officetel_multi["thresholds"]["RB14-LODGING"] == 25
    assert officetel_multi["thresholds"]["RB14-AMUSEMENT"] is None
    assert officetel_multi["thresholds"]["RB14-CREMATION-MILITARY"] == 500


def test_parcel_resolution_returns_provisional_polygon() -> None:
    response = client.post(
        "/api/hazard-review/parcels/resolve",
        json={
            "name": "검증 사업지",
            "address": "경기도 성남시 분당구 판교역로 235",
            "coordinates": {"lat": 37.40111, "lng": 127.10853},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["provisional"] is True
    assert body["parcels"][0]["geometry_source"] == "provisional_polygon"
    assert len(body["parcels"][0]["geometry"]) == 5


def test_demo_hazard_job_never_promotes_point_candidates_to_exclusion() -> None:
    started = client.post("/api/hazard-review/jobs", json=hazard_payload())

    assert started.status_code == 200
    body = wait_for_job(started.json()["job_id"])
    assert body["status"] == "completed"
    assert body["progress"] == 100
    result = body["result"]
    assert result["housing_type"] == "house"
    assert result["application_type"] == "multi_child"
    assert len(result["categories"]) == 26
    # 데모는 임시 필지라 경계 미확보다. 점 좌표 후보를 매입제외로 올리면 안 된다.
    assert all(
        category["status"] != "exclusion_match" for category in result["categories"]
    )
    assert result["overall_status"] == "review_required"


def test_officetel_general_drops_housing_rules_but_keeps_cremation() -> None:
    started = client.post(
        "/api/hazard-review/jobs",
        json=hazard_payload("officetel", "general"),
    )

    body = wait_for_job(started.json()["job_id"])
    categories = {c["key"]: c for c in body["result"]["categories"]}
    assert categories["gas_station"]["status"] == "not_applicable"
    assert categories["factory_registered"]["status"] == "not_applicable"
    assert categories["general_lodging"]["status"] == "not_applicable"
    assert categories["crematorium"]["threshold_m"] == 500
    assert categories["crematorium"]["status"] != "not_applicable"
