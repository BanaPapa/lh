"""건축물대장 용도 스캔 — 「위험물저장및처리시설」 건물을 사업지 주변에서 걸러내는 우회 원천.

LPG 저장소·위험물 제조소등·유독물 시설·도시가스 제조시설·화약류 저장소는 전국 위치 API 가
없다. 대신 건축법 시행령 별표1 제19호의 용도 「위험물저장및처리시설」이 그 세부(가~자목)를
모두 품는 건축물대장 주용도라, 다음 순서로 **있는 API 를 겹쳐** 후보를 만든다.

1. 브이월드 **GIS 건물통합정보**(LT_C_BLDGINFO)로 사업지 반경 안 건물을 한 번에 받는다.
   건물 외곽선과 `usability`(건축물대장 주용도코드 5자리)가 같이 온다 — 19000 이
   위험물저장및처리시설이다. 필지마다 표제부를 부르던 종전 방식(도심 160필지·25초)을
   호출 1회로 줄였다(2026-09-18).
2. 주용도코드가 비어 있는 건물(대장 미연계, 실측 1/3)은 그 자리 필지(연속지적도)의
   표제부를 조회해 보완한다. 이 보완만 건물 수만큼 호출이 든다.
3. 위험물저장및처리시설 건물은 필지 **층별개요**(getBrFlrOulnInfo)를 한 번 더 본다. 층별
   주용도는 세부 코드명(액화석유가스저장소 · 위험물취급소 · 유독물보관저장소 ·
   도시가스제조시설 · 화약류저장소 …)이라 종류가 그대로 갈린다(basis=code). 세부 코드명이
   「기타위험물저장처리시설」뿐이면 기타용도 문자열로 가른다(basis=text).
4. 이미 연결된 원천이 덮는 종류(주유소·LPG 충전·판매·고압가스·액화가스·도료류)는 뺀다.
   남는 것이 LPG 저장소·위험물·유독물·도시가스·화약류 후보이고, 못 가른 건물은 「미분류」.

핀의 경계는 건물 외곽선이다(LH 「시설경계」에 가장 가깝다). 반경 안 건물을 하나도 빠뜨리지
않고 끝냈을 때(`ScanResult.complete`)만 완전 연결과 같은 급(「우회 연결」)으로 본다.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Sequence
from typing import Any, NamedTuple, Protocol

from app.models import Coordinates
from app.services.geo import (
    distance_point_to_polygon_m,
    haversine_meters,
    polygon_centroid,
    polygon_contains,
    polygon_overlap_m2,
)

MAIN_USE_TOKEN = "위험물저장및처리시설"
# 건축물대장 주용도코드 대분류 — 19xxx 가 위험물저장및처리시설.
HAZMAT_USE_CODE_PREFIX = "19"
# 한 번에 받는 건물 수 상한(브이월드 페이지 상한과 같다). 반경 안 건물이 이보다 많으면 미완료.
MAX_BUILDINGS = 1000
MAX_SCAN_RADIUS_M = 200
# 주용도코드 공란 건물의 표제부 보완 상한. 이보다 많으면 가까운 순으로 보고 나머지는 미완료.
MAX_FALLBACK_LOOKUPS = 120
# 건축HUB 는 초당 호출을 제한해 동시 4건이면 429 재시도로 건당 5초가 넘는다(2026-09-17 실측).
# 단건은 0.06초라 순차가 가장 빠르다.
LOOKUP_CONCURRENCY = 1
# 한 건물이 걸친 필지 중 층별개요를 볼 최대 수. 충전소처럼 부지가 여러 필지면 사무동·캐노피가
# 다른 지번에 서 있어(2026-09-18 도곡동 실측: 건물은 552-5, 허가 지번은 552-4) 겹치는
# 필지를 겹침 면적 순으로 더 본다.
MAX_PARCELS_PER_BUILDING = 3
# 스캔 전체 시간 예산. 넘기면 남은 건물은 「조회 실패」로 세고 넘어간다(심사를 막지 않는다).
TIME_BUDGET_SECONDS = 40.0
METERS_PER_DEGREE_LAT = 111_320.0

# 종류 판별 규칙 — (종류, 필요한 토큰 묶음). 순서대로 첫 일치가 이긴다.
KIND_RULES: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
    ("LPG저장", (("lpg", "액화석유", "엘피지", "프로판", "부탄"), ("저장", "탱크", "벌크"))),
    ("LPG충전", (("lpg", "액화석유", "엘피지"), ("충전",))),
    ("LPG판매", (("lpg", "액화석유", "엘피지", "가스판매"), ("판매",))),
    ("주유소", (("주유", "석유판매", "유류판매"),)),
    ("고압가스", (("고압가스", "산소", "질소", "아세틸렌", "수소", "cng", "압축천연"),)),
    ("도시가스", (("도시가스", "정압", "가스공급"),)),
    ("유독물", (("유독물", "유해화학", "화학물질", "독극물"),)),
    ("위험물", (("위험물", "옥내저장", "옥외저장", "옥외탱크", "지하탱크", "제조소", "취급소"),)),
)
# 이미 연결된 원천이 덮는 종류 — 후보에서 뺀다. 액화가스(라)·고압가스(바)는 행안부
# 고압가스업, 도료류(사)는 석유대체연료판매업 근사가 덮는다.
COVERED_KINDS: frozenset[str] = frozenset(
    {"주유소", "LPG충전", "LPG판매", "고압가스", "액화가스", "도료류"}
)

# 층별개요 주용도(세부 코드명) → 종류. 건축물대장 용도코드 19xxx 의 이름은 건축법 시행령
# 별표1 제19호 가~자목을 그대로 따라 「액화석유가스저장소」「위험물취급소」「유독물판매소」
# 「도시가스제조시설」「화약류저장소」처럼 적히므로 이름 부분일치로 가른다(순서대로 첫 일치).
# 「기타위험물저장처리시설」(19999)은 종류를 말해 주지 않아 여기 없고, 기타용도 문자열로
# 내려간다(KIND_RULES).
FLOOR_KIND_RULES: tuple[tuple[str, str], ...] = (
    ("액화석유가스저장", "LPG저장"),
    ("액화석유가스충전", "LPG충전"),
    ("액화석유가스판매", "LPG판매"),
    ("위험물제조", "위험물"),
    ("위험물저장", "위험물"),
    ("위험물취급", "위험물"),
    ("액화가스취급", "액화가스"),
    ("액화가스판매", "액화가스"),
    ("유독물", "유독물"),
    ("고압가스", "고압가스"),
    ("도료류", "도료류"),
    ("도시가스제조", "도시가스"),
    ("화약류", "화약류"),
    ("주유소", "주유소"),
    ("석유판매", "주유소"),
)
GENERIC_FLOOR_TOKEN = "기타위험물"


def _norm(text: str) -> str:
    return "".join((text or "").split()).lower()


def is_hazmat_use(main_purpose: str, etc_purpose: str = "") -> bool:
    return MAIN_USE_TOKEN in _norm(main_purpose) or MAIN_USE_TOKEN in _norm(etc_purpose)


def is_hazmat_use_code(use_code: str) -> bool:
    return (use_code or "").strip().startswith(HAZMAT_USE_CODE_PREFIX)


def classify_use(main_purpose: str, etc_purpose: str) -> str:
    """기타용도(우선)·주용도 문자열에서 종류를 가른다. 못 가르면 「미분류」."""

    text = _norm(etc_purpose) + "|" + _norm(main_purpose).replace(MAIN_USE_TOKEN, "")
    for kind, groups in KIND_RULES:
        if all(any(token in text for token in group) for group in groups):
            return kind
    return "미분류"


def _floor_kind(main_purpose: str) -> str | None:
    text = _norm(main_purpose)
    if GENERIC_FLOOR_TOKEN in text:
        return None
    for token, kind in FLOOR_KIND_RULES:
        if token in text:
            return kind
    return None


def classify_floors(floors: Sequence[Any]) -> tuple[str, str]:
    """층별개요로 종류를 가른다 → (종류, 근거 문자열).

    세부 코드명이 있는 층이 이기고, 연결된 원천이 덮는 종류(충전소 등)와 미연결 종류
    (저장소 등)가 한 건물에 같이 있으면 미연결 종류를 앞세운다 — 핀을 놓치지 않기
    위해서다. 코드명이 「기타위험물저장처리시설」뿐이면 층의 기타용도 문자열로 내려간다.
    """

    coded: list[tuple[str, str]] = []
    generic_texts: list[str] = []
    for floor in floors:
        main = getattr(floor, "main_purpose", "") or ""
        etc = getattr(floor, "etc_purpose", "") or ""
        kind = _floor_kind(main)
        if kind is not None:
            coded.append((kind, main))
        elif GENERIC_FLOOR_TOKEN in _norm(main):
            generic_texts.append(etc)
    uncovered = [pair for pair in coded if pair[0] not in COVERED_KINDS]
    if uncovered:
        return uncovered[0]
    if coded:
        return coded[0]
    for text in generic_texts:
        kind = classify_use("", text)
        if kind != "미분류":
            return kind, text
    return "미분류", ""


class ScannedBuilding(NamedTuple):
    pnu: str
    address: str
    ring: list[Coordinates]
    centroid: Coordinates
    dong_name: str
    main_purpose: str
    etc_purpose: str
    kind: str
    # 종류를 가른 근거. code = 층별개요 세부 코드명 · text = 기타용도 문자열 추정 · none = 못 가름
    basis: str = "text"
    evidence: str = ""


class ScanResult(NamedTuple):
    buildings: list[ScannedBuilding]
    # 반경 조회로 받은 건물 수(주용도코드 보완 대상 포함).
    parcels_seen: int
    # 건축물대장(표제부·층별개요) 호출 수.
    lookups: int
    # 조회하지 못한 건물(예산 초과·보완 상한·조회 실패)의 필지 또는 건물 식별자.
    failed_pnus: list[str]

    @property
    def complete(self) -> bool:
        """반경 안 건물을 하나도 빠뜨리지 않고 훑었는가.

        조회 실패·예산 초과·보완 상한에 걸린 건물이 없고 건물 수 상한에 걸리지 않았을
        때다. 이때만 스캔 결과를 완전 연결과 같은 급(「우회 연결」)으로 본다.
        """

        return not self.failed_pnus and self.parcels_seen < MAX_BUILDINGS


class _Parcel(Protocol):
    pnu: str
    address: str
    ring: list[Coordinates]


def bbox_around(center: Coordinates, radius_m: float) -> tuple[float, float, float, float]:
    """(south, west, north, east)."""

    d_lat = radius_m / METERS_PER_DEGREE_LAT
    d_lng = radius_m / (METERS_PER_DEGREE_LAT * max(math.cos(math.radians(center.lat)), 0.2))
    return center.lat - d_lat, center.lng - d_lng, center.lat + d_lat, center.lng + d_lng


class BuildingUseScanner:
    """반경 안 건물(건물통합정보) → 주용도코드 → 층별개요 → 위험물저장및처리시설 후보."""

    def __init__(self, vworld: Any, building_register: Any) -> None:
        self._vworld = vworld
        self._register = building_register

    @property
    def enabled(self) -> bool:
        return bool(
            self._vworld is not None and getattr(self._vworld, "enabled", False)
            and self._register is not None and getattr(self._register, "enabled", False)
        )

    async def scan(self, center: Coordinates, radius_m: float) -> ScanResult:
        """반경(기준거리 + 사업지 반경 + 여유, 상한 MAX_SCAN_RADIUS_M) 안 건물을 훑는다."""

        if not self.enabled:
            return ScanResult([], 0, 0, [])
        radius_m = min(radius_m, MAX_SCAN_RADIUS_M)
        south, west, north, east = bbox_around(center, radius_m)
        raw = await self._vworld.buildings_in_box(south, west, north, east, limit=MAX_BUILDINGS)
        # 사각형 모서리를 잘라, 건물 외곽선이 반경 안에 닿는 것만 가까운 순으로.
        near = [
            b for b in raw
            if len(b.ring) >= 4 and distance_point_to_polygon_m(center, b.ring) <= radius_m
        ]
        near.sort(key=lambda b: distance_point_to_polygon_m(center, b.ring))
        deadline = time.monotonic() + TIME_BUDGET_SECONDS
        lookups = 0
        failed: list[str] = []

        # 1) 주용도코드가 있는 건물은 코드로 바로 가른다. 공란은 필지 표제부로 보완한다.
        hazmat: list[tuple[Any, str]] = []  # (건물, 표제부 기타용도)
        blank = [b for b in near if not b.use_code]
        parcels = await self._parcels_for(center, radius_m, blank)
        semaphore = asyncio.Semaphore(LOOKUP_CONCURRENCY)

        async def resolve_blank(index: int, building: Any) -> None:
            nonlocal lookups
            parcel = _parcel_containing(parcels, polygon_centroid(building.ring))
            if parcel is None:
                failed.append(f"building:{index}")
                return
            async with semaphore:
                if index >= MAX_FALLBACK_LOOKUPS or time.monotonic() > deadline:
                    failed.append(parcel.pnu)
                    return
                try:
                    lookups += 1
                    result = await self._register.lookup(parcel.pnu)
                except Exception:  # noqa: BLE001 — 건물 하나의 실패가 스캔 전체를 막지 않는다
                    failed.append(parcel.pnu)
                    return
            for use in getattr(result, "uses", ()):
                if is_hazmat_use(use.main_purpose or "", use.etc_purpose or ""):
                    hazmat.append((building, use.etc_purpose or ""))
                    return

        await asyncio.gather(*(resolve_blank(i, b) for i, b in enumerate(blank)))
        for building in near:
            if is_hazmat_use_code(building.use_code):
                hazmat.append((building, ""))

        # 2) 위험물저장및처리시설 건물만 필지를 잡아 층별개요로 종류를 가른다.
        buildings: list[ScannedBuilding] = []
        if hazmat:
            all_parcels = parcels or await self._parcels_for(
                center, radius_m, [b for b, _ in hazmat]
            )
            for building, etc in hazmat:
                centroid = polygon_centroid(building.ring)
                candidates = _parcels_overlapping(all_parcels, building.ring, centroid)
                parcel = candidates[0] if candidates else None
                pnu = parcel.pnu if parcel is not None else ""
                floor_kind, floor_evidence = (None, "")
                for candidate in candidates[:MAX_PARCELS_PER_BUILDING]:
                    if time.monotonic() > deadline:
                        break
                    lookups += 1
                    floor_kind, floor_evidence = await self._classify_by_floors(candidate.pnu)
                    if floor_kind is not None:
                        parcel, pnu = candidate, candidate.pnu
                        break
                if floor_kind is not None:
                    kind, basis, evidence = floor_kind, "code", floor_evidence
                else:
                    kind = classify_use(MAIN_USE_TOKEN, etc)
                    basis = "text" if kind != "미분류" else "none"
                    evidence = etc if kind != "미분류" else ""
                buildings.append(
                    ScannedBuilding(
                        pnu=pnu,
                        address=parcel.address if parcel is not None else "",
                        ring=list(building.ring),
                        centroid=centroid,
                        dong_name=building.dong_name or building.name or "",
                        main_purpose=MAIN_USE_TOKEN,
                        etc_purpose=etc,
                        kind=kind,
                        basis=basis,
                        evidence=evidence,
                    )
                )
        return ScanResult(buildings, len(raw), lookups, failed)

    async def _parcels_for(
        self, center: Coordinates, radius_m: float, buildings: list[Any]
    ) -> list[Any]:
        """건물 → 필지(PNU) 대응에 쓸 반경 안 필지 목록. 대응할 건물이 없으면 부르지 않는다."""

        if not buildings:
            return []
        south, west, north, east = bbox_around(center, radius_m + 40)
        try:
            parcels = await self._vworld.parcels_in_box(
                south, west, north, east, limit=MAX_BUILDINGS
            )
        except Exception:  # noqa: BLE001 — 필지 대응 실패는 보완·층별 조회만 못 할 뿐
            return []
        return [p for p in parcels if p.pnu and len(p.ring) >= 4]

    async def _classify_by_floors(self, pnu: str) -> tuple[str | None, str]:
        """층별개요로 종류를 가른다. 세부 코드명이 없거나 조회가 실패하면 (None, "")."""

        lookup_floors = getattr(self._register, "lookup_floors", None)
        if lookup_floors is None:
            return None, ""
        try:
            floors = await lookup_floors(pnu)
        except Exception:  # noqa: BLE001 — 층별개요 실패는 문자열 추정으로 내려갈 뿐
            return None, ""
        kind, evidence = classify_floors(floors)
        if kind == "미분류":
            return None, ""
        return kind, evidence


def _parcels_overlapping(
    parcels: Sequence[Any], ring: Sequence[Coordinates], centroid: Coordinates
) -> list[Any]:
    """건물 외곽선이 걸친 필지들 — 겹침 면적이 큰 순. 하나도 안 겹치면 중심점 기준 1건."""

    scored: list[tuple[float, Any]] = []
    for parcel in parcels:
        try:
            overlap = polygon_overlap_m2(ring, parcel.ring)
        except ValueError:
            continue
        if overlap > 0:
            scored.append((overlap, parcel))
    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        return [parcel for _overlap, parcel in scored]
    fallback = _parcel_containing(parcels, centroid)
    return [fallback] if fallback is not None else []


def _parcel_containing(parcels: Sequence[Any], point: Coordinates) -> Any | None:
    for parcel in parcels:
        try:
            if polygon_contains(parcel.ring, point):
                return parcel
        except ValueError:
            continue
    # 경계 위·자투리로 못 찾으면 가장 가까운 필지(40m 이내)로 본다.
    best = None
    best_distance = 40.0
    for parcel in parcels:
        distance = haversine_meters(point, polygon_centroid(parcel.ring))
        if distance < best_distance:
            best, best_distance = parcel, distance
    return best
