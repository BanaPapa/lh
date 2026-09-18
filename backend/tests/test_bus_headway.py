"""버스정류장 운행주기(15분) 판정 — LH 심사 담당자 계산법 재현.

근거: 2026-09-15 백승환 대리 「26_1001_버스 운행 주기 계산 자료 (1차~3차).xlsx」.
정류장을 지나는 노선마다 60/배차간격(분) 을 합산해 시간당 도착 버스 수를 구하고,
÷4 한 「15분당 평균 도착 버스 수」가 1 이상이면 정류장을 인정한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.screening.bus_headway import (
    BusHeadwayResolver,
    StopHeadway,
    arrivals_per_15min,
    parse_interval_minutes,
    qualifies,
)


def test_arrivals_matches_lh_example_sheet() -> None:
    # LH 「예시」 시트 — 만성골드클래스후문: 74(46분)·75(46분)·101(30분)·200(33분)·1994(720분)
    arrivals = arrivals_per_15min([46, 46, 30, 33, 720])
    assert round(arrivals, 3) == 1.625
    assert qualifies(arrivals) is True


def test_single_sparse_route_does_not_qualify() -> None:
    # 120분 배차 노선 하나 → 15분당 0.125대
    arrivals = arrivals_per_15min([120])
    assert round(arrivals, 3) == 0.125
    assert qualifies(arrivals) is False


def test_exactly_one_per_fifteen_minutes_qualifies() -> None:
    # 15분 배차 1개 노선 = 15분당 정확히 1대 → 인정 (백승환 「15분 주기 내 1대 이상」)
    assert qualifies(arrivals_per_15min([15])) is True
    assert qualifies(arrivals_per_15min([16])) is False


def test_zero_or_missing_intervals_are_ignored() -> None:
    assert arrivals_per_15min([0, -5, 30]) == arrivals_per_15min([30])
    assert arrivals_per_15min([]) == 0.0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (30, 30.0),
        ("30", 30.0),
        ("30분", 30.0),
        ("34-46", 34.0),  # 범위 표기는 LH 시트처럼 작은 값(평일 최소)을 쓴다
        ("", None),
        (None, None),
        ("미정", None),
    ],
)
def test_parse_interval_minutes(raw: Any, expected: float | None) -> None:
    assert parse_interval_minutes(raw) == expected


class FakeTago:
    """정류장별 경유노선과 노선별 배차간격을 미리 정해 둔 대역."""

    enabled = True

    def __init__(
        self,
        stop_routes: dict[str, list[dict[str, Any]]],
        route_infos: dict[str, dict[str, Any] | None],
        fail_routes: set[str] | None = None,
    ) -> None:
        self.stop_routes = stop_routes
        self.route_infos = route_infos
        self.fail_routes = fail_routes or set()
        self.route_info_calls: list[str] = []

    async def routes_through_stop(self, city_code: str, stop_id: str) -> list[dict[str, Any]]:
        return list(self.stop_routes.get(stop_id, []))

    async def route_info(self, city_code: str, route_id: str) -> dict[str, Any] | None:
        self.route_info_calls.append(route_id)
        if route_id in self.fail_routes:
            raise RuntimeError("TAGO 장애")
        return self.route_infos.get(route_id)


@pytest.mark.asyncio
async def test_resolver_sums_routes_and_reports_qualification() -> None:
    tago = FakeTago(
        stop_routes={
            "S1": [
                {"routeid": "R74", "routeno": "74"},
                {"routeid": "R75", "routeno": "75"},
                {"routeid": "R101", "routeno": "101"},
                {"routeid": "R200", "routeno": "200"},
                {"routeid": "R1994", "routeno": "1994"},
            ]
        },
        route_infos={
            "R74": {"intervaltime": "46"},
            "R75": {"intervaltime": "46"},
            "R101": {"intervaltime": "30"},
            "R200": {"intervaltime": "33"},
            "R1994": {"intervaltime": "720"},
        },
    )
    resolver = BusHeadwayResolver(tago)
    result = await resolver.resolve("35010", "S1")

    assert isinstance(result, StopHeadway)
    assert result.route_count == 5
    assert round(result.arrivals_per_15min, 3) == 1.625
    assert result.qualifies is True
    assert result.determined is True
    assert "1.6" in result.label and "인정" in result.label


@pytest.mark.asyncio
async def test_resolver_marks_stop_undetermined_when_no_interval_is_known() -> None:
    tago = FakeTago(
        stop_routes={"S2": [{"routeid": "R9", "routeno": "9"}]},
        route_infos={"R9": {"intervaltime": ""}},
    )
    result = await BusHeadwayResolver(tago).resolve("35010", "S2")

    # 배차간격을 하나도 알 수 없으면 「미달」로 확정하지 않는다 — 확인 필요로 남긴다.
    assert result.determined is False
    assert result.qualifies is False
    assert result.unknown_routes == ("9",)
    assert "확인" in result.label


@pytest.mark.asyncio
async def test_resolver_keeps_known_routes_when_some_lookups_fail() -> None:
    tago = FakeTago(
        stop_routes={"S3": [{"routeid": "R1", "routeno": "1"}, {"routeid": "R2", "routeno": "2"}]},
        route_infos={"R1": {"intervaltime": "10"}},
        fail_routes={"R2"},
    )
    result = await BusHeadwayResolver(tago).resolve("35010", "S3")

    assert result.qualifies is True  # 10분 배차 하나만으로 1.5대
    assert result.unknown_routes == ("2",)
    assert result.determined is True


@pytest.mark.asyncio
async def test_resolver_uses_weekend_interval_when_weekday_missing() -> None:
    tago = FakeTago(
        stop_routes={"S4": [{"routeid": "R1", "routeno": "1"}]},
        route_infos={"R1": {"intervaltime": "", "intervalsattime": "12"}},
    )
    result = await BusHeadwayResolver(tago).resolve("35010", "S4")
    assert result.determined is True
    assert result.qualifies is True


@pytest.mark.asyncio
async def test_resolve_many_caches_route_lookups_across_stops() -> None:
    tago = FakeTago(
        stop_routes={
            "A": [{"routeid": "R1", "routeno": "1"}],
            "B": [{"routeid": "R1", "routeno": "1"}],
        },
        route_infos={"R1": {"intervaltime": "20"}},
    )
    resolver = BusHeadwayResolver(tago)
    results = await resolver.resolve_many([("35010", "A"), ("35010", "B")])

    assert set(results) == {"35010:A", "35010:B"}
    assert tago.route_info_calls == ["R1"]


@pytest.mark.asyncio
async def test_resolver_without_routes_is_undetermined_not_failed() -> None:
    tago = FakeTago(stop_routes={}, route_infos={})
    result = await BusHeadwayResolver(tago).resolve("35010", "NONE")
    assert result.route_count == 0
    assert result.determined is False
