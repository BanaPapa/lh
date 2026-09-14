from __future__ import annotations

import math
from typing import Any

from app.models import Coordinates, GeocodeCandidate, RegionInfo


def demo_geocode(query: str) -> list[GeocodeCandidate]:
    label = query.strip() or "판교 사업지"
    return [
        GeocodeCandidate(
            id="demo-primary",
            name=f"{label} (데모 후보)",
            address="경기도 성남시 분당구 삼평동",
            road_address="경기도 성남시 분당구 판교역로 235",
            coordinates=Coordinates(lat=37.40111, lng=127.10853),
            source="demo",
        ),
        GeocodeCandidate(
            id="demo-secondary",
            name=f"{label} 인근 (데모 후보)",
            address="경기도 성남시 분당구 백현동",
            road_address="경기도 성남시 분당구 판교역로 160",
            coordinates=Coordinates(lat=37.39549, lng=127.11063),
            source="demo",
        ),
    ]


DEMO_PLACES: list[tuple[str, str, str, float, float]] = [
    ("SW8", "지하철역", "샘플 판교역", 0.0025, 0.0031),
    ("PK6", "주차장", "샘플 공영주차장", -0.0015, 0.0021),
    ("PS3", "어린이집·유치원", "샘플 어린이집", 0.0011, -0.0017),
    ("SC4", "학교", "샘플 초등학교", -0.0022, -0.0011),
    ("AC5", "학원", "샘플 수학학원", -0.0031, 0.0024),
    ("AC5", "학원", "샘플 영어학원", 0.0035, -0.0022),
    ("MT1", "대형마트", "샘플 대형마트", 0.0048, 0.0015),
    ("CS2", "편의점", "샘플 편의점 A", 0.0008, 0.0009),
    ("CS2", "편의점", "샘플 편의점 B", -0.0012, 0.0006),
    ("HP8", "병원", "샘플 종합병원", 0.0041, -0.0042),
    ("PM9", "약국", "샘플 약국", 0.0019, 0.0017),
    ("BK9", "은행", "샘플 은행", -0.0028, -0.0025),
    ("FD6", "음식점", "샘플 음식점", 0.0024, -0.0035),
    ("CE7", "카페", "샘플 카페", -0.0009, -0.0014),
    ("PO3", "공공기관", "샘플 행정복지센터", -0.0045, 0.0035),
    ("CT1", "문화시설", "샘플 도서관", 0.0052, -0.0011),
    ("PARK", "공원", "샘플 근린공원", -0.0055, -0.0041),
    ("BUS_STOP", "버스정류장", "샘플 버스정류장", 0.0012, 0.0028),
    ("BUS_STOP", "버스정류장", "샘플 환승정류장", -0.0021, 0.0019),
]
