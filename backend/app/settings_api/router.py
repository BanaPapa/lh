"""화면에서 서버 비밀키를 넣는 로컬 전용 설정 API.

보안 원칙:
- 이 라우터는 로컬 전용이다. 소켓 상대가 루프백이 아니면 403.
- `X-Forwarded-For` 같은 우회 가능한 헤더는 신뢰하지 않는다.
- 응답·에러·로그에 키 원문을 절대 남기지 않는다. (GET 은 마스킹 힌트만)
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.config import get_settings
from app.settings_api.connections import (
    ConnectionsResponse,
    build_connections,
    check_connections,
)
from app.settings_api.store import (
    DEMO_MODE_KEY,
    SERVER_KEY_SPECS,
    apply_updates,
    mask_hint,
)


router = APIRouter(prefix="/api/settings", tags=["settings"])

# 소켓 상대 주소가 이 집합에 있어야만 설정 API 를 허용한다.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

# 키 값 상한. 실제 서버 키(공공데이터·카카오·네이버·브이월드 등)는 넉넉히 이 안에 든다.
_VALUE_MAX_LEN = 512
# 허용 문자: 공백·제어문자·비ASCII 를 배제한 출력 가능한 ASCII 만.
# 개행(\n,\r)을 허용하면 .env 에 다른 환경변수를 주입할 수 있어 반드시 막는다.
_ALLOWED_VALUE = re.compile(r"^[\x21-\x7e]+$")


def _sanitize_key_value(label: str, raw: str) -> str:
    """서버 키 값을 검증·정리한다. 위반 시 400.

    개행이 들어간 값을 그대로 `.env` 에 쓰면 `x\nDEMO_MODE=false` 처럼 다른
    환경변수를 주입할 수 있다(루프백 호출자라 해도 막는다). 빈 값은 '변경 안 함'
    으로 취급하도록 빈 문자열을 돌려준다.
    """

    if "\n" in raw or "\r" in raw:
        raise HTTPException(
            status_code=400,
            detail=f"{label} 값에는 줄바꿈 문자를 넣을 수 없습니다.",
        )
    value = raw.strip()
    if not value:
        return ""
    if len(value) > _VALUE_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"{label} 값이 너무 깁니다(최대 {_VALUE_MAX_LEN}자).",
        )
    if not _ALLOWED_VALUE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"{label} 값에 허용되지 않는 문자가 있습니다(공백·제어문자 불가).",
        )
    return value


def require_loopback(request: Request) -> None:
    """실제 TCP 상대가 루프백일 때만 통과시킨다.

    프록시가 붙일 수 있는 헤더는 보지 않고 소켓 주소(`request.client.host`)만 본다.
    """

    client = request.client
    host = client.host if client else None
    if host not in _LOOPBACK_HOSTS:
        raise HTTPException(
            status_code=403,
            detail="설정 API는 로컬(루프백)에서만 사용할 수 있습니다.",
        )


class ServerKeyStatus(BaseModel):
    key: str
    label: str
    description: str
    configured: bool
    hint: str
    scope: str = "server"
    # 발급처 이름·URL. 사용자가 준 값이 없으면 빈 문자열(프런트는 링크를 숨긴다).
    issuer_name: str = ""
    issuer_url: str = ""


class KeysStatusResponse(BaseModel):
    keys: list[ServerKeyStatus]
    demo_mode: bool


class KeysUpdateRequest(BaseModel):
    """서버 비밀키 갱신 요청.

    - 필드 미포함: 변경 안 함
    - 빈 문자열(""): 변경 안 함
    - null: 명시적 삭제
    - 그 외 값: 해당 값으로 설정
    """

    kakao_rest_api_key: str | None = None
    tago_service_key: str | None = None
    public_data_service_key: str | None = None
    naver_search_client_id: str | None = None
    naver_search_client_secret: str | None = None
    vworld_api_key: str | None = None
    opinet_api_key: str | None = None
    safemap_api_key: str | None = None
    seoul_open_data_key: str | None = None
    demo_mode: bool | None = None


# 구조적 방어: SERVER_KEY_SPECS 에 노출되는 키는 반드시 KeysUpdateRequest 에도
# 필드가 있어야 PUT 이 반영된다. 스펙에 키를 더하고 요청 모델 필드를 빠뜨리면
# 화면 저장이 조용히 무시되므로(같은 실수가 세 번 났다), 임포트 시점에 두 목록이
# 어긋나면 즉시 실패시켜 배포 전에 잡는다.
_MISSING_UPDATE_FIELDS = [
    spec.key.lower()
    for spec in SERVER_KEY_SPECS
    if spec.key.lower() not in KeysUpdateRequest.model_fields
]
if _MISSING_UPDATE_FIELDS:
    raise RuntimeError(
        "KeysUpdateRequest 에 SERVER_KEY_SPECS 필드가 누락됐습니다: "
        f"{_MISSING_UPDATE_FIELDS}. PUT 이 조용히 무시되므로 필드를 추가하세요."
    )


def _clear_caches() -> None:
    """설정과 설정을 캡처한 서비스 팩토리 캐시를 모두 비운다."""

    get_settings.cache_clear()
    # 지연 임포트로 순환 참조를 피한다.
    from app.hazard_review.router import get_hazard_service
    from app.screening.router import get_screening_service

    get_hazard_service.cache_clear()
    get_screening_service.cache_clear()


def _build_status() -> KeysStatusResponse:
    settings = get_settings()
    keys: list[ServerKeyStatus] = []
    for spec in SERVER_KEY_SPECS:
        raw = str(getattr(settings, spec.key.lower(), "") or "")
        keys.append(
            ServerKeyStatus(
                key=spec.key,
                label=spec.label,
                description=spec.description,
                configured=bool(raw),
                hint=mask_hint(raw),
                issuer_name=spec.issuer_name,
                issuer_url=spec.issuer_url,
            )
        )
    return KeysStatusResponse(keys=keys, demo_mode=settings.demo_mode)


@router.get(
    "/keys",
    response_model=KeysStatusResponse,
    dependencies=[Depends(require_loopback)],
)
async def read_keys() -> KeysStatusResponse:
    """설정 상태를 돌려준다. 키 원문은 절대 내려보내지 않고 마스킹 힌트만."""

    return _build_status()


@router.put(
    "/keys",
    response_model=KeysStatusResponse,
    dependencies=[Depends(require_loopback)],
)
async def update_keys(payload: KeysUpdateRequest) -> KeysStatusResponse:
    """받은 값만 `.env` 와 환경변수에 반영하고 캐시를 비운다."""

    provided = payload.model_fields_set
    updates: dict[str, str | None] = {}

    for spec in SERVER_KEY_SPECS:
        field = spec.key.lower()
        if field not in provided:
            continue
        value = getattr(payload, field)
        if value is None:
            updates[spec.key] = None  # 명시적 삭제
            continue
        cleaned = _sanitize_key_value(spec.label, value)
        if cleaned == "":
            continue  # 빈 값·공백만 = 변경 안 함
        updates[spec.key] = cleaned

    if "demo_mode" in provided and payload.demo_mode is not None:
        updates[DEMO_MODE_KEY] = "true" if payload.demo_mode else "false"

    if updates:
        apply_updates(updates)
        _clear_caches()

    return _build_status()


# ---------------------------------------------------------------------------
# API 연결 현황·점검 — 설정 패널의 「API 연결」 표
# ---------------------------------------------------------------------------


def _services() -> tuple[object, object, bool]:
    # 지연 임포트로 순환 참조를 피한다(설정 갱신이 이 팩토리들의 캐시를 비운다).
    from app.hazard_review.router import get_hazard_service
    from app.screening.router import get_screening_service

    return get_hazard_service(), get_screening_service(), get_settings().demo_mode


class ConnectionsCheckRequest(BaseModel):
    # 비우면 키가 있는 원천 전부를 점검한다.
    ids: list[str] | None = None


@router.get(
    "/connections",
    response_model=ConnectionsResponse,
    dependencies=[Depends(require_loopback)],
)
async def read_connections() -> ConnectionsResponse:
    """키 유무와 마지막 점검 결과. 원격 호출은 하지 않는다."""

    hazard, screening, demo = _services()
    return build_connections(hazard, screening, demo)


@router.post(
    "/connections/check",
    response_model=ConnectionsResponse,
    dependencies=[Depends(require_loopback)],
)
async def check_connections_endpoint(
    payload: ConnectionsCheckRequest | None = None,
) -> ConnectionsResponse:
    """키가 있는 원천을 실제로 호출해 응답 여부를 확인한다."""

    hazard, screening, demo = _services()
    only = set(payload.ids) if payload and payload.ids else None
    return await check_connections(hazard, screening, demo, only)
