"""버스정류장 운행주기(15분) 판정 — LH 심사 담당자 계산법을 API 로 재현한다.

심사표 평가기준 2-1: 대중교통 시설분류에 「운행주기가 15분 이내인 버스정류장」.
LH 심사 담당자의 실제 계산(2026-09-15 백승환 대리, 「26_1001_버스 운행 주기 계산
자료 (1차~3차).xlsx」, 접수번호 1001-001~119 전건):

    정류장을 지나는 노선마다  60 / 배차간격(분)  → 시간당 도착 버스 수를 합산
    ÷ 4  = 「15분당 평균 도착 버스 수」
    1 이상이면 정류장 인정 (「15분 주기 내에 1대 이상 있는 건에 대하여 존재로 인정」)

수행팀 로컬 앱은 배차 자료가 없어 정류장 전건을 인정한다(수행팀 결정). 이 앱은
국토교통부 TAGO 정류소 경유노선 + 노선 정보(배차간격)로 같은 값을 계산한다.
LH 시트의 범위 표기(「평일 34-46, 주말 34-90」)는 작은 값을 썼으므로 여기서도
평일 배차(intervaltime)를 우선하고, 범위면 최소값을 쓴다.

배차간격을 한 노선도 알 수 없는 정류장은 「미달」로 확정하지 않는다 — 자료 부재를
시설 부재로 접지 않는 원칙(룰북 §3)에 따라 「확인 필요」로 남기고 배점에는 넣지
않되 근거 목록에는 보인다.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Iterable, NamedTuple, Protocol


# 15분당 평균 도착 버스 수가 이 값 이상이면 정류장을 인정한다(LH 실무 기준).
QUALIFY_ARRIVALS_PER_15MIN = 1.0

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


class RouteHeadway(NamedTuple):
    """정류장을 지나는 노선 하나의 배차간격."""

    route_no: str
    interval_min: float


class StopHeadway(NamedTuple):
    """정류장 하나의 운행주기 판정."""

    stop_ref: str                       # "{cityCode}:{nodeId}"
    route_count: int                    # 경유 노선 수(배차 미확인 포함)
    routes: tuple[RouteHeadway, ...]    # 배차간격을 확인한 노선
    unknown_routes: tuple[str, ...]     # 배차간격을 확인하지 못한 노선번호
    arrivals_per_15min: float
    qualifies: bool                     # 15분당 1대 이상 → 배점에 센다
    determined: bool                    # 배차를 하나라도 확인했는가
    label: str                          # 화면용 한 줄


class RouteInfoSource(Protocol):
    async def routes_through_stop(self, city_code: str, stop_id: str) -> list[dict[str, Any]]: ...
    async def route_info(self, city_code: str, route_id: str) -> dict[str, Any] | None: ...


def parse_interval_minutes(raw: Any) -> float | None:
    """배차간격 원문을 분(float)으로. 범위(「34-46」)면 작은 값, 못 읽으면 None."""

    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value > 0 else None
    numbers = [float(token) for token in _NUMBER_RE.findall(str(raw))]
    numbers = [value for value in numbers if value > 0]
    if not numbers:
        return None
    return min(numbers)


def arrivals_per_15min(intervals_min: Iterable[float]) -> float:
    """노선별 배차간격(분) → 15분당 평균 도착 버스 수. 0·음수는 무시한다.

    LH 시트의 셀식 `ROUND(60/배차간격, 2)` 를 노선마다 적용한 뒤 합산·÷4 한다.
    노선별 반올림을 생략하면 예시 시트 값(1.625)과 셋째 자리에서 어긋난다.
    """

    per_hour = sum(
        round(60.0 / interval, 2)
        for interval in intervals_min
        if interval and interval > 0
    )
    return per_hour / 4.0


def qualifies(arrivals: float) -> bool:
    # 부동소수 오차로 15분 배차(정확히 1.0)가 탈락하지 않도록 아주 작은 여유를 둔다.
    return arrivals + 1e-9 >= QUALIFY_ARRIVALS_PER_15MIN


def _route_interval(info: dict[str, Any] | None) -> float | None:
    if not info:
        return None
    # 평일 → 토요일 → 일요일 순. LH 시트도 평일 값을 기본으로 썼다.
    for key in ("intervaltime", "intervalsattime", "intervalsuntime"):
        value = parse_interval_minutes(info.get(key))
        if value is not None:
            return value
    return None


def _label(
    routes: tuple[RouteHeadway, ...],
    unknown: tuple[str, ...],
    arrivals: float,
    ok: bool,
    determined: bool,
) -> str:
    if not determined:
        if not routes and not unknown:
            return "경유 노선 정보 없음 — 운행주기 확인 불가, 정류장으로 셈"
        return f"경유 노선 {len(unknown)}개 배차간격 미확인 — 운행주기 확인 불가, 정류장으로 셈"
    parts = ", ".join(f"{r.route_no}({r.interval_min:g}분)" for r in routes[:6])
    more = f" 외 {len(routes) - 6}개" if len(routes) > 6 else ""
    tail = f" · 배차 미확인 {len(unknown)}개" if unknown else ""
    verdict = "인정" if ok else "미달(배점 제외)"
    return (
        f"15분당 평균 {arrivals:.2f}대 → {verdict} · 노선 {parts}{more}{tail}"
    )


class BusHeadwayResolver:
    """TAGO 로 정류장별 15분당 도착 버스 수를 구한다. 노선 정보는 정류장 간 공유·캐시."""

    def __init__(self, source: RouteInfoSource, concurrency: int = 4) -> None:
        self.source = source
        self._semaphore = asyncio.Semaphore(concurrency)
        self._route_cache: dict[str, float | None] = {}
        self._route_locks: dict[str, asyncio.Lock] = {}

    async def _interval_for(self, city_code: str, route_id: str) -> float | None:
        key = f"{city_code}:{route_id}"
        if key in self._route_cache:
            return self._route_cache[key]
        lock = self._route_locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._route_cache:
                return self._route_cache[key]
            try:
                async with self._semaphore:
                    info = await self.source.route_info(city_code, route_id)
            except Exception:
                # 노선 한 건의 조회 실패를 정류장 전체의 실패로 번지게 하지 않는다.
                # 캐시하지 않아 다음 정류장에서 다시 시도한다.
                return None
            interval = _route_interval(info)
            self._route_cache[key] = interval
            return interval

    async def resolve(self, city_code: str, stop_id: str) -> StopHeadway:
        stop_ref = f"{city_code}:{stop_id}"
        try:
            async with self._semaphore:
                rows = await self.source.routes_through_stop(city_code, stop_id)
        except Exception:
            rows = []
        seen: set[str] = set()
        route_ids: list[tuple[str, str]] = []
        for row in rows:
            route_id = str(row.get("routeid") or row.get("routeId") or "").strip()
            route_no = str(row.get("routeno") or row.get("routeNo") or route_id).strip()
            if not route_id or route_id in seen:
                continue
            seen.add(route_id)
            route_ids.append((route_id, route_no))

        intervals = await asyncio.gather(
            *(self._interval_for(city_code, route_id) for route_id, _ in route_ids)
        )
        known: list[RouteHeadway] = []
        unknown: list[str] = []
        for (route_id, route_no), interval in zip(route_ids, intervals):
            if interval is None:
                unknown.append(route_no)
            else:
                known.append(RouteHeadway(route_no, interval))

        routes = tuple(known)
        arrivals = arrivals_per_15min(r.interval_min for r in routes)
        determined = bool(routes)
        ok = determined and qualifies(arrivals)
        return StopHeadway(
            stop_ref=stop_ref,
            route_count=len(route_ids),
            routes=routes,
            unknown_routes=tuple(unknown),
            arrivals_per_15min=arrivals,
            qualifies=ok,
            determined=determined,
            label=_label(routes, tuple(unknown), arrivals, ok, determined),
        )

    async def resolve_many(
        self, stops: Iterable[tuple[str, str]]
    ) -> dict[str, StopHeadway]:
        unique: dict[str, tuple[str, str]] = {}
        for city_code, stop_id in stops:
            unique.setdefault(f"{city_code}:{stop_id}", (city_code, stop_id))
        results = await asyncio.gather(
            *(self.resolve(city, stop) for city, stop in unique.values())
        )
        return {result.stop_ref: result for result in results}
