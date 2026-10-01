"""자료 업데이트(사본 갱신) 중 표시.

매일 오전 GCP 의 사본 갱신 빌드(deploy/nightly-refresh.yaml)가 시작할 때 버킷에
`status/maintenance.json` 을 써 두고 끝나면 지운다. 서버는 그 파일을 읽어 화면에
「업데이트 중」을 알리고 새 심사 시작을 잠깐 막는다 — 갱신 중에는 공공 API 를 한꺼번에
부르고, 끝나면 서버가 새 버전으로 바뀌어 돌던 심사가 끊기기 때문이다.

빌드가 중간에 죽어 파일이 남아도 `expires_at` 이 지나면 무시한다(앱이 계속 막히지 않게).
버킷을 읽지 못하면(로컬 실행·권한·통신 오류) 업데이트 중이 아닌 것으로 본다.
`MAINTENANCE_UNTIL`(ISO 시각)을 주면 그때까지 업데이트 중으로 본다(로컬 시험·수동 점검용).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel

from app.config import Settings
from app.kst import KST

logger = logging.getLogger(__name__)

FLAG_OBJECT = "status/maintenance.json"
# 상태 파일을 다시 읽는 간격(초). 화면이 20~60초마다 묻는다.
CACHE_SECONDS = 20
METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)
DEFAULT_MESSAGE = (
    "공공 데이터를 새로 받아 업데이트하는 중입니다. 매일 오전 9시에 약 20분 걸리며, "
    "끝나면 자동으로 다시 쓸 수 있습니다."
)


class MaintenanceState(BaseModel):
    updating: bool = False
    message: str = ""
    # 화면 표시용 한국 시각(HH:MM). 모르면 빈 문자열.
    started_at: str = ""
    expected_end: str = ""
    # 지금 서버에서 돌고 있는 심사·일괄 심사 수(빌드가 배포 전에 0 이 되길 기다린다).
    active_jobs: int = 0


_cache: tuple[float, MaintenanceState] | None = None
_token: tuple[float, str] | None = None


def _parse(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _hhmm(value: datetime | None) -> str:
    return value.astimezone(KST).strftime("%H:%M") if value else ""


def state_from_flag(flag: dict, now: datetime) -> MaintenanceState:
    """버킷 상태 파일 → 상태. 만료됐으면 업데이트 중이 아니다."""

    expires = _parse(str(flag.get("expires_at") or ""))
    if expires is None or now >= expires:
        return MaintenanceState()
    return MaintenanceState(
        updating=True,
        message=str(flag.get("message") or DEFAULT_MESSAGE),
        started_at=_hhmm(_parse(str(flag.get("started_at") or ""))),
        expected_end=_hhmm(_parse(str(flag.get("expected_end") or "")) or expires),
    )


async def _access_token(client: httpx.AsyncClient) -> str:
    global _token
    if _token is not None and time.monotonic() < _token[0]:
        return _token[1]
    response = await client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
    response.raise_for_status()
    body = response.json()
    _token = (time.monotonic() + max(int(body.get("expires_in", 300)) - 60, 30), body["access_token"])
    return _token[1]


async def _read_flag(bucket: str) -> dict | None:
    url = (
        f"https://storage.googleapis.com/storage/v1/b/{quote(bucket, safe='')}/o/"
        f"{quote(FLAG_OBJECT, safe='')}?alt=media"
    )
    async with httpx.AsyncClient(timeout=httpx.Timeout(4.0, connect=2.0)) as client:
        token = await _access_token(client)
        response = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 404:
        return None
    response.raise_for_status()
    data = json.loads(response.text)
    return data if isinstance(data, dict) else None


async def maintenance_state(config: Settings) -> MaintenanceState:
    """지금 자료 업데이트 중인가. 읽기 실패는 「아님」으로 본다(앱을 막지 않는다)."""

    global _cache
    now = datetime.now(UTC)
    forced = _parse(config.maintenance_until) if config.maintenance_until else None
    if forced is not None and now < forced:
        return MaintenanceState(updating=True, message=DEFAULT_MESSAGE, expected_end=_hhmm(forced))
    if not config.snapshot_bucket:
        return MaintenanceState()
    if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1].model_copy()
    try:
        flag = await _read_flag(config.snapshot_bucket)
        state = state_from_flag(flag, now) if flag else MaintenanceState()
    except Exception:  # noqa: BLE001 — 상태 확인 실패로 앱을 막지 않는다
        logger.warning("자료 업데이트 상태를 읽지 못했습니다.", exc_info=True)
        state = MaintenanceState()
    _cache = (time.monotonic(), state)
    return state.model_copy()


async def ensure_not_updating() -> None:
    """업데이트 중이면 새 심사를 받지 않는다(503 + 안내 문구)."""

    from fastapi import HTTPException

    from app.config import get_settings

    state = await maintenance_state(get_settings())
    if state.updating:
        until = f" (예상 종료 {state.expected_end})" if state.expected_end else ""
        raise HTTPException(status_code=503, detail=f"{state.message}{until}")


def reset_cache() -> None:
    """테스트용."""

    global _cache, _token
    _cache = None
    _token = None
