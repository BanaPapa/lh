"""로컬 전용 설정 API 테스트.

- 비루프백 요청은 403
- GET /keys 응답에 키 원문이 새지 않음
- PUT 후 get_settings() 에 즉시 반영
- .env 의 기존 다른 키·주석 보존
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.settings_api import store


LOOPBACK = ("127.0.0.1", 51000)
NON_LOOPBACK = ("203.0.113.9", 51000)

SECRET = "topsecret-abcd1234"


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    """임시 .env 를 만들고 store 가 그 파일을 보게 바꾼다."""

    path = tmp_path / ".env"
    path.write_text(
        "# 사용자 설정 파일\n"
        "OTHER_KEY=keepme\n"
        "KAKAO_REST_API_KEY=\n"
        "DEMO_MODE=true\n"
        "CACHE_TTL_SECONDS=600\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(store, "ENV_PATH", path)

    managed = [spec.key for spec in store.SERVER_KEY_SPECS] + [store.DEMO_MODE_KEY]
    for key in managed:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()

    yield path

    for key in managed:
        os.environ.pop(key, None)
    get_settings.cache_clear()


def loopback_client() -> TestClient:
    return TestClient(app, client=LOOPBACK)


def test_non_loopback_request_is_forbidden(env_file) -> None:
    client = TestClient(app, client=NON_LOOPBACK)

    assert client.get("/api/settings/keys").status_code == 403
    assert (
        client.put("/api/settings/keys", json={"kakao_rest_api_key": SECRET}).status_code
        == 403
    )


def test_get_keys_never_returns_raw_values(env_file) -> None:
    client = loopback_client()
    client.put("/api/settings/keys", json={"kakao_rest_api_key": SECRET})

    response = client.get("/api/settings/keys")

    assert response.status_code == 200
    assert SECRET not in response.text

    body = response.json()
    kakao = next(item for item in body["keys"] if item["key"] == "KAKAO_REST_API_KEY")
    assert kakao["configured"] is True
    assert kakao["hint"] == "****1234"
    assert kakao["scope"] == "server"
    # 미설정 키는 힌트가 비어 있고 configured 는 False
    tago = next(item for item in body["keys"] if item["key"] == "TAGO_SERVICE_KEY")
    assert tago["configured"] is False
    assert tago["hint"] == ""


def test_put_reflects_in_get_settings(env_file) -> None:
    client = loopback_client()

    response = client.put(
        "/api/settings/keys",
        json={"kakao_rest_api_key": SECRET, "demo_mode": False},
    )

    assert response.status_code == 200
    settings = get_settings()
    assert settings.kakao_rest_api_key == SECRET
    assert settings.demo_mode is False
    # /api/health 도 즉시 갱신된다.
    health = client.get("/api/health").json()
    assert health["kakao_configured"] is True
    assert health["demo_mode"] is False


def test_empty_string_means_no_change_null_deletes(env_file) -> None:
    client = loopback_client()
    client.put("/api/settings/keys", json={"kakao_rest_api_key": SECRET})

    # 빈 문자열은 변경 안 함
    client.put("/api/settings/keys", json={"kakao_rest_api_key": ""})
    assert get_settings().kakao_rest_api_key == SECRET

    # null 은 명시적 삭제
    client.put("/api/settings/keys", json={"kakao_rest_api_key": None})
    assert get_settings().kakao_rest_api_key == ""


def test_env_file_preserves_other_keys_and_comments(env_file) -> None:
    client = loopback_client()
    client.put("/api/settings/keys", json={"tago_service_key": "tago-xyz9876"})

    text = env_file.read_text(encoding="utf-8")
    assert "OTHER_KEY=keepme" in text
    assert "# 사용자 설정 파일" in text
    assert "CACHE_TTL_SECONDS=600" in text
    assert "TAGO_SERVICE_KEY=tago-xyz9876" in text


def test_newline_value_is_rejected_and_does_not_pollute_env(env_file) -> None:
    client = loopback_client()

    # 개행으로 다른 환경변수를 주입하려는 시도.
    response = client.put(
        "/api/settings/keys",
        json={"kakao_rest_api_key": "abcd1234\nDEMO_MODE=false"},
    )
    assert response.status_code == 400

    # .env 에 주입 라인이 쓰이지 않았다. 기존 DEMO_MODE=true 가 그대로다.
    text = env_file.read_text(encoding="utf-8")
    assert "DEMO_MODE=false" not in text
    assert "DEMO_MODE=true" in text
    # 카카오 키도 바뀌지 않았다(줄바꿈 값은 거부).
    assert "abcd1234" not in text
    assert get_settings().kakao_rest_api_key == ""


def test_carriage_return_and_control_chars_rejected(env_file) -> None:
    client = loopback_client()

    assert (
        client.put(
            "/api/settings/keys", json={"tago_service_key": "abc\rdef"}
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/settings/keys", json={"tago_service_key": "has space"}
        ).status_code
        == 400
    )


def test_overlong_value_is_rejected(env_file) -> None:
    client = loopback_client()
    response = client.put(
        "/api/settings/keys", json={"vworld_api_key": "a" * 1000}
    )
    assert response.status_code == 400


def test_safemap_key_get_put_and_delete_roundtrip(env_file) -> None:
    # SAFEMAP_API_KEY 는 화면에 노출되므로 PUT 으로 저장·삭제가 실제로 동작해야 한다.
    # KeysUpdateRequest 에 필드가 없으면 PUT 이 조용히 무시된다(화면 저장 안 됨).
    client = loopback_client()
    secret = "safemap-key-9999"

    # 저장.
    response = client.put("/api/settings/keys", json={"safemap_api_key": secret})
    assert response.status_code == 200
    assert get_settings().safemap_api_key == secret
    assert "SAFEMAP_API_KEY=" + secret in env_file.read_text(encoding="utf-8")

    # GET 은 원문 대신 마스킹 힌트만 준다.
    body = client.get("/api/settings/keys").json()
    safemap = next(
        item for item in body["keys"] if item["key"] == "SAFEMAP_API_KEY"
    )
    assert safemap["configured"] is True
    assert safemap["hint"] == "****9999"
    assert secret not in client.get("/api/settings/keys").text

    # null 은 명시적 삭제.
    client.put("/api/settings/keys", json={"safemap_api_key": None})
    assert get_settings().safemap_api_key == ""


def test_every_server_key_spec_has_update_field() -> None:
    # 구조적 방어: 노출 스펙과 요청 모델이 어긋나면 PUT 이 조용히 무시된다.
    # 두 목록이 항상 일치하는지 대조한다(같은 누락이 재발하지 않게).
    from app.settings_api.router import KeysUpdateRequest

    model_fields = set(KeysUpdateRequest.model_fields)
    for spec in store.SERVER_KEY_SPECS:
        assert spec.key.lower() in model_fields, spec.key


def test_put_preflight_allows_put_for_allowed_origin() -> None:
    # 브라우저 프리플라이트: PUT 이 CORS 에서 허용돼야 실제 저장이 된다.
    client = TestClient(app, client=LOOPBACK)
    response = client.options(
        "/api/settings/keys",
        headers={
            "Origin": "http://localhost:5180",
            "Access-Control-Request-Method": "PUT",
        },
    )
    assert response.status_code == 200
    assert "PUT" in response.headers.get("access-control-allow-methods", "")
    assert (
        response.headers.get("access-control-allow-origin")
        == "http://localhost:5180"
    )


def test_hydrate_process_env_lifts_legal_dong_path(tmp_path, monkeypatch) -> None:
    # 배선 방어: pnu_resolver 가 os.environ 에서 직접 읽는 LH_LEGAL_DONG_PATH 는
    # PROCESS_ENV_KEYS 에 있어야 .env 에 적어도 프로세스로 올라간다. 없으면 .env 에
    # 경로가 멀쩡히 적혀 있어도 법정동코드표가 안 붙어 PNU 조립이 전건 실패한다.
    assert "LH_LEGAL_DONG_PATH" in store.PROCESS_ENV_KEYS

    path = tmp_path / ".env"
    path.write_text(
        "LH_LEGAL_DONG_PATH=/srv/lh/legal_dong_codes.xlsx\n"
        "OTHER_KEY=keepme\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(store, "ENV_PATH", path)
    monkeypatch.delenv("LH_LEGAL_DONG_PATH", raising=False)

    filled = store.hydrate_process_env()

    assert "LH_LEGAL_DONG_PATH" in filled
    assert os.environ["LH_LEGAL_DONG_PATH"] == "/srv/lh/legal_dong_codes.xlsx"


# ---------------------------------------------------------------------------
# API 연결 현황 — 모든 원천이 한 표에 나오고, 키 없는 원천은 missing_key 다
# ---------------------------------------------------------------------------


def test_connections_list_every_source_without_calling_out(env_file) -> None:
    from app.settings_api.connections import CONNECTION_SPECS

    client = loopback_client()
    response = client.get("/api/settings/connections")
    assert response.status_code == 200
    body = response.json()
    ids = [row["id"] for row in body["connections"]]
    assert ids == [spec.id for spec in CONNECTION_SPECS]
    for row in body["connections"]:
        assert row["state"] in {"missing_key", "ready", "ok", "failed"}
        assert row["purpose"]
        assert row["key_name"]
        # 키 원문은 어디에도 없다.
        assert "serviceKey" not in row["detail"]


def test_connections_check_records_probe_results(env_file, monkeypatch) -> None:
    from app.settings_api import connections as mod

    async def fake_ok(hazard, screening):
        return "12건"

    async def fake_fail(hazard, screening):
        raise RuntimeError("401 Unauthorized https://api?serviceKey=SECRET")

    class Client:
        enabled = True

    specs = (
        mod.ConnectionSpec("a", "A", "용도", "K", lambda h, s: Client(), fake_ok),
        mod.ConnectionSpec("b", "B", "용도", "K", lambda h, s: Client(), fake_fail),
        mod.ConnectionSpec("c", "C", "용도", "K", lambda h, s: None, fake_ok),
    )
    monkeypatch.setattr(mod, "CONNECTION_SPECS", specs)
    monkeypatch.setattr(mod, "_last_results", {})

    client = loopback_client()
    body = client.post("/api/settings/connections/check", json={}).json()
    by_id = {row["id"]: row for row in body["connections"]}
    assert by_id["a"]["state"] == "ok" and by_id["a"]["detail"] == "12건"
    assert by_id["b"]["state"] == "failed"
    assert "SECRET" not in by_id["b"]["detail"]
    assert by_id["c"]["state"] == "missing_key"

    # 비-루프백은 점검도 못 한다.
    assert TestClient(app, client=NON_LOOPBACK).get("/api/settings/connections").status_code == 403
