from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


Dimension = Literal["transport", "education", "living", "comfort"]
Profile = Literal["balanced", "family", "commuter", "commercial"]
ModuleStatus = Literal["ready", "partial", "planned", "unavailable"]
ProgressStatus = Literal["pending", "running", "completed", "failed"]


class Coordinates(BaseModel):
    lat: float
    lng: float


class GeocodeCandidate(BaseModel):
    id: str
    name: str
    address: str
    road_address: str = ""
    coordinates: Coordinates
    source: Literal["kakao", "naver", "vworld", "demo"] = "kakao"


class GeocodeResponse(BaseModel):
    query: str
    candidates: list[GeocodeCandidate]
    demo: bool = False
    # 카카오가 막혀 대체 원천(네이버·브이월드)으로 찾았을 때의 안내. 조용히 넘기지 않는다.
    notice: str = ""


class RegionInfo(BaseModel):
    legal_name: str = ""
    legal_code: str = ""
    administrative_name: str = ""
    administrative_code: str = ""
