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
    source: Literal["kakao", "demo"] = "kakao"


class GeocodeResponse(BaseModel):
    query: str
    candidates: list[GeocodeCandidate]
    demo: bool = False


class RegionInfo(BaseModel):
    legal_name: str = ""
    legal_code: str = ""
    administrative_name: str = ""
    administrative_code: str = ""
