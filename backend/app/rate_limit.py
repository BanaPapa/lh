"""접속자(IP)별 심사 시작 제한 — 누구나 접속하는 배포에서 공공 API 쿼터를 지킨다.

심사 1건은 공공 API 를 수십~수백 번 부른다. 링크가 퍼져 누군가 심사를 연달아 돌리면
카카오·공공데이터포털 일일 쿼터가 바닥나 다른 사람의 심사까지 「원천 장애」가 된다.
심사를 **시작**하는 요청만 센다. 진행 상황 조회(0.25초 간격 폴링)나 지도 조회는 세지
않는다.

접속자 IP 는 Vercel 이 붙이는 `X-Forwarded-For` 의 첫 값을 쓴다. 이 값은 흉내 낼 수
있지만, 여기서는 쿼터 보호용 계수일 뿐 권한 판단에는 쓰지 않는다(설정 API 의 루프백
검사는 소켓 주소만 본다). Cloud Run 은 인스턴스 1대로 돌리므로 메모리 계수로 충분하다.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import get_settings

# 심사를 새로 시작하는 요청. (메서드, 경로) 가 정확히 일치할 때만 센다.
LIMITED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/screening"),
        ("POST", "/api/screening/jobs"),
        ("POST", "/api/hazard-review/jobs"),
        ("POST", "/api/screening/batch"),
    }
)

MINUTE = 60.0
DAY = 24 * 60 * 60.0

_hits: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


def check(ip: str, now: float, per_minute: int, per_day: int) -> str:
    """제한에 걸리면 안내 문구, 아니면 빈 문자열. 통과하면 이번 요청을 기록한다."""

    hits = _hits[ip]
    while hits and now - hits[0] >= DAY:
        hits.popleft()
    if per_day and len(hits) >= per_day:
        return f"테스트 서버는 접속자당 하루 {per_day}건까지 심사할 수 있습니다. 내일 다시 시도해 주세요."
    if per_minute:
        recent = sum(1 for t in hits if now - t < MINUTE)
        if recent >= per_minute:
            return f"심사는 1분에 {per_minute}건까지 시작할 수 있습니다. 잠시 후 다시 시도해 주세요."
    hits.append(now)
    return ""


async def rate_limit_middleware(request: Request, call_next):
    settings = get_settings()
    if (settings.rate_limit_per_minute or settings.rate_limit_per_day) and (
        request.method,
        request.url.path.rstrip("/") or "/",
    ) in LIMITED_ROUTES:
        message = check(
            client_ip(request),
            time.monotonic(),
            settings.rate_limit_per_minute,
            settings.rate_limit_per_day,
        )
        if message:
            return JSONResponse(status_code=429, content={"detail": message})
    return await call_next(request)
