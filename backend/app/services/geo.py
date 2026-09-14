from __future__ import annotations

import math
from collections.abc import Sequence
from functools import lru_cache
from typing import NamedTuple

from pyproj import Transformer
from shapely.geometry import MultiPolygon, Point, Polygon
from shapely.ops import nearest_points, unary_union

from app.models import Coordinates


EARTH_RADIUS_M = 6_371_000
METERS_PER_DEGREE_LAT = 111_320

WGS84 = "EPSG:4326"

# 한국 평면직각좌표계(GRS80) 원점별 EPSG. 경도 기준으로 고른다.
TM_WEST = 5185     # 서부원점 125°E
TM_CENTRAL = 5186  # 중부원점 127°E
TM_EAST = 5187     # 동부원점 129°E
TM_EAST_SEA = 5188  # 동해원점 131°E


def haversine_meters(a: Coordinates, b: Coordinates) -> float:
    """두 좌표 사이의 대권 직선거리(m)."""

    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    delta_lat = lat2 - lat1
    delta_lng = math.radians(b.lng - a.lng)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    )
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def offset_coordinates(
    center: Coordinates,
    north_m: float,
    east_m: float,
) -> Coordinates:
    """기준 좌표에서 북/동 방향으로 이동한 좌표를 만든다."""

    lat = center.lat + north_m / METERS_PER_DEGREE_LAT
    longitude_scale = max(math.cos(math.radians(center.lat)), 0.2)
    lng = center.lng + east_m / (METERS_PER_DEGREE_LAT * longitude_scale)
    return Coordinates(lat=lat, lng=lng)


class BoundaryDistance(NamedTuple):
    """두 도형 사이의 최단거리와 그 거리를 만드는 두 지점."""

    distance_m: float
    nearest_a: Coordinates
    nearest_b: Coordinates
    overlaps: bool


def tm_epsg_for(lng: float) -> int:
    """경도로 한국 평면좌표계 원점을 고른다.

    위경도(도 단위)로 거리를 계산하면 미터가 나오지 않으므로, 거리 계산 전에
    반드시 평면좌표계로 투영해야 한다.
    """

    if lng < 126.0:
        return TM_WEST
    if lng < 128.0:
        return TM_CENTRAL
    if lng < 130.0:
        return TM_EAST
    return TM_EAST_SEA


@lru_cache(maxsize=8)
def _transformers(epsg: int) -> tuple[Transformer, Transformer]:
    """투영/역투영 Transformer 쌍. 생성 비용이 커서 캐시한다."""

    target = f"EPSG:{epsg}"
    return (
        Transformer.from_crs(WGS84, target, always_xy=True),
        Transformer.from_crs(target, WGS84, always_xy=True),
    )


def _ring_epsg(ring: Sequence[Coordinates]) -> int:
    if not ring:
        raise ValueError("폴리곤 좌표가 비어 있습니다.")
    return tm_epsg_for(ring[0].lng)


def _to_polygon(ring: Sequence[Coordinates], epsg: int) -> Polygon:
    forward, _ = _transformers(epsg)
    points = [forward.transform(point.lng, point.lat) for point in ring]
    if len(points) < 4:
        raise ValueError("폴리곤은 최소 4개 좌표가 필요합니다.")
    polygon = Polygon(points)
    # 지적도 원본에 자기교차가 섞여 있으면 거리 계산이 깨지므로 보정한다.
    return polygon if polygon.is_valid else polygon.buffer(0)


def _to_point(coordinates: Coordinates, epsg: int) -> Point:
    forward, _ = _transformers(epsg)
    return Point(*forward.transform(coordinates.lng, coordinates.lat))


def _to_coordinates(point: Point, epsg: int) -> Coordinates:
    _, inverse = _transformers(epsg)
    lng, lat = inverse.transform(point.x, point.y)
    return Coordinates(lat=lat, lng=lng)


def polygon_area_m2(ring: Sequence[Coordinates]) -> float:
    """폴리곤 면적(㎡)."""

    epsg = _ring_epsg(ring)
    return _to_polygon(ring, epsg).area


def polygon_centroid(ring: Sequence[Coordinates]) -> Coordinates:
    epsg = _ring_epsg(ring)
    return _to_coordinates(_to_polygon(ring, epsg).centroid, epsg)


def distance_point_to_polygon_m(
    point: Coordinates,
    ring: Sequence[Coordinates],
) -> float:
    """점에서 폴리곤 경계까지의 최단거리(m). 폴리곤 안이면 0."""

    epsg = _ring_epsg(ring)
    return _to_point(point, epsg).distance(_to_polygon(ring, epsg))


def max_extent_m(center: Coordinates, ring: Sequence[Coordinates]) -> float:
    """center 에서 폴리곤의 가장 먼 꼭짓점까지 거리(m).

    장소 검색은 점(주소 좌표) 기준 반경으로만 되기 때문에, 경계 기준으로 재려면
    이 값만큼 검색 반경을 넓혀야 필지 반대편에 붙은 시설을 놓치지 않는다.
    """

    if not ring:
        return 0.0
    return max(haversine_meters(center, point) for point in ring)


def buffer_ring(
    ring: Sequence[Coordinates],
    distance_m: float,
) -> list[Coordinates]:
    """폴리곤을 distance_m 만큼 바깥으로 확장한 외곽선.

    경계 기준 거리 밴드를 지도에 그릴 때 쓴다. 중심점 기준 원과 달리 필지 모양을
    따라간다.
    """

    epsg = _ring_epsg(ring)
    expanded = _to_polygon(ring, epsg).buffer(distance_m)
    if expanded.is_empty:
        return []
    boundary = expanded.exterior if hasattr(expanded, "exterior") else None
    if boundary is None:
        return []
    return [
        _to_coordinates(Point(x, y), epsg) for x, y in boundary.coords
    ]


def _union(rings: Sequence[Sequence[Coordinates]], epsg: int):
    """여러 필지를 하나의 도형으로 합친다. 맞닿아 있으면 자동으로 이어진다."""

    return unary_union([_to_polygon(ring, epsg) for ring in rings if ring])


def _exterior_rings(geometry, epsg: int) -> list[list[Coordinates]]:
    parts = (
        list(geometry.geoms)
        if isinstance(geometry, MultiPolygon)
        else [geometry]
    )
    rings: list[list[Coordinates]] = []
    for part in parts:
        if part.is_empty or not hasattr(part, "exterior"):
            continue
        rings.append(
            [_to_coordinates(Point(x, y), epsg) for x, y in part.exterior.coords]
        )
    return rings


def distance_point_to_polygons_m(
    point: Coordinates,
    rings: Sequence[Sequence[Coordinates]],
) -> float:
    """여러 필지 중 가장 가까운 경계까지의 거리(m)."""

    valid = [ring for ring in rings if ring]
    if not valid:
        raise ValueError("폴리곤 좌표가 비어 있습니다.")
    epsg = _ring_epsg(valid[0])
    return _to_point(point, epsg).distance(_union(valid, epsg))


def buffer_rings(
    rings: Sequence[Sequence[Coordinates]],
    distance_m: float,
) -> list[list[Coordinates]]:
    """여러 필지를 합친 뒤 distance_m 만큼 확장한 외곽선들.

    맞닿은 필지는 하나로 이어지고, 떨어진 필지는 각각 남는다.
    """

    valid = [ring for ring in rings if ring]
    if not valid:
        return []
    epsg = _ring_epsg(valid[0])
    expanded = _union(valid, epsg).buffer(distance_m)
    if expanded.is_empty:
        return []
    return _exterior_rings(expanded, epsg)


def max_extent_multi(
    center: Coordinates,
    rings: Sequence[Sequence[Coordinates]],
) -> float:
    """center 에서 모든 필지의 가장 먼 꼭짓점까지 거리(m)."""

    distances = [max_extent_m(center, ring) for ring in rings if ring]
    return max(distances) if distances else 0.0


def nearest_boundary_point(
    ring: Sequence[Coordinates],
    target: Coordinates,
) -> Coordinates:
    """폴리곤에서 target 에 가장 가까운 지점. 지도에 최단 연결선을 그릴 때 쓴다."""

    epsg = _ring_epsg(ring)
    polygon = _to_polygon(ring, epsg)
    point = _to_point(target, epsg)
    on_polygon, _ = nearest_points(polygon, point)
    return _to_coordinates(on_polygon, epsg)


def nearest_boundary_point_multi(
    rings: Sequence[Sequence[Coordinates]],
    target: Coordinates,
) -> Coordinates | None:
    """여러 필지의 합집합에서 target 에 가장 가까운 지점.

    지도에 최단 연결선을 그릴 때 쓴다. 이 점에서 시작하지 않으면 선이 재는 것과
    표시되는 숫자가 달라진다.
    """

    valid = [ring for ring in rings if ring]
    if not valid:
        return None
    epsg = _ring_epsg(valid[0])
    on_polygon, _ = nearest_points(_union(valid, epsg), _to_point(target, epsg))
    return _to_coordinates(on_polygon, epsg)


def distance_polygons_to_polygon_m(
    rings: Sequence[Sequence[Coordinates]],
    other: Sequence[Coordinates],
) -> BoundaryDistance:
    """여러 필지(사업지)와 한 필지(시설) 사이의 경계 대 경계 최단거리.

    LH 공고가 말하는 '대지경계 대 시설경계'가 이것이다. 시설을 점으로 재면
    실제보다 멀게 나온다.
    """

    valid = [ring for ring in rings if ring]
    if not valid or not other:
        raise ValueError("폴리곤 좌표가 비어 있습니다.")
    epsg = _ring_epsg(valid[0])
    site = _union(valid, epsg)
    target = _to_polygon(other, epsg)
    point_a, point_b = nearest_points(site, target)
    return BoundaryDistance(
        distance_m=site.distance(target),
        nearest_a=_to_coordinates(point_a, epsg),
        nearest_b=_to_coordinates(point_b, epsg),
        overlaps=site.intersects(target),
    )


def distance_polygon_to_polygon_m(
    ring_a: Sequence[Coordinates],
    ring_b: Sequence[Coordinates],
) -> BoundaryDistance:
    """두 폴리곤 경계 사이의 최단거리와 최단 연결선 양 끝점."""

    epsg = _ring_epsg(ring_a)
    polygon_a = _to_polygon(ring_a, epsg)
    polygon_b = _to_polygon(ring_b, epsg)
    point_a, point_b = nearest_points(polygon_a, polygon_b)
    return BoundaryDistance(
        distance_m=polygon_a.distance(polygon_b),
        nearest_a=_to_coordinates(point_a, epsg),
        nearest_b=_to_coordinates(point_b, epsg),
        overlaps=polygon_a.intersects(polygon_b),
    )
