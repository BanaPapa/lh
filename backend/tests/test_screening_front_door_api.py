"""정문 기준점 지정 엔드포인트 회귀 — 지정·조회·해제.

UI(지적도 클릭 지정)가 쓰는 REST 계약을 고정한다. 저장은 판정 엔진과 같은
저장소 인스턴스를 공유해 다음 검토부터 반영된다(국장님 §3-3).
"""

from __future__ import annotations

import os

os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("KAKAO_REST_API_KEY", "")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.screening.front_door import FrontDoorStore, normalize_key
from app.screening.router import get_front_door_store


@pytest.fixture
def client(tmp_path):
    store = FrontDoorStore(path=tmp_path / "front_doors.json")
    app.dependency_overrides[get_front_door_store] = lambda: store
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_front_door_store, None)


def test_designate_list_and_remove_roundtrip(client) -> None:
    created = client.post(
        "/api/screening/front-doors",
        json={"university": "전주대학교", "pnu": "P1", "source_label": "지적도 클릭"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["label"] == "전주대학교"
    assert body["pnu"] == "P1"
    assert body["origin"] == "manual"

    listed = client.get("/api/screening/front-doors")
    assert listed.status_code == 200
    assert any(item["pnu"] == "P1" for item in listed.json())

    removed = client.delete("/api/screening/front-doors/전주대학교")
    assert removed.status_code == 200
    assert removed.json() == []


def test_coordinate_only_designation_is_accepted(client) -> None:
    response = client.post(
        "/api/screening/front-doors",
        json={"university": "군산대학교", "lat": 35.94, "lng": 126.68},
    )
    assert response.status_code == 200
    assert response.json()["lat"] == 35.94


def test_designation_without_pnu_or_coordinates_is_rejected(client) -> None:
    response = client.post(
        "/api/screening/front-doors",
        json={"university": "전북대학교"},
    )
    assert response.status_code == 422


def test_blank_university_is_rejected(client) -> None:
    response = client.post(
        "/api/screening/front-doors",
        json={"university": "   ", "pnu": "P9"},
    )
    assert response.status_code == 422


def test_delete_method_is_allowed_by_cors_preflight() -> None:
    """정문 해제(DELETE)가 CORS 프리플라이트에서 허용된다.

    별도 출처 배포(.env ALLOWED_ORIGINS)에서 DELETE 가 막히면 해제 버튼이 동작하지
    않는다. allow_methods 에 DELETE 가 빠져 있으면 Starlette 는 프리플라이트를
    400(Disallowed CORS method)으로 거절한다.
    """

    client = TestClient(app)
    response = client.options(
        "/api/screening/front-doors/전주대학교",
        headers={
            "Origin": "http://localhost",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert response.status_code == 200
    assert "DELETE" in response.headers.get("access-control-allow-methods", "")


def test_spaced_facility_name_round_trips_to_the_same_key(client) -> None:
    """공백 있는 이름도 정규화 키로 맞물려 배지·해제가 보인다.

    지정 목록의 facility 는 정규화 키, 시설명(hit.name)은 원문이라, 프런트가 양쪽을
    같은 규칙(모든 공백 제거)으로 비교해야 「전북대학교 전주캠퍼스」에서 배지·해제가
    보인다. 백엔드가 내려주는 facility 키가 그 비교 계약을 고정한다.
    """

    name = "전북대학교 전주캠퍼스"
    created = client.post(
        "/api/screening/front-doors",
        json={"university": name, "lat": 35.84, "lng": 127.13},
    )
    assert created.status_code == 200
    view = created.json()
    # 표시용 원문은 공백을 보존하고, 비교용 키는 공백을 제거한다.
    assert view["label"] == name
    assert view["facility"] == normalize_key(name)
    # 프런트 normalizeFacilityKey(hit.name) 와 동일 규칙(모든 공백 제거)임을 고정한다.
    assert view["facility"] == name.replace(" ", "")

    listed = client.get("/api/screening/front-doors")
    assert any(item["facility"] == normalize_key(name) for item in listed.json())
