"""연속지적도 조회를 VWorld API 로 수행하는 백엔드.

왜 로컬 shapefile 을 쓰지 않는가
--------------------------------
이 앱은 납품본(정적 데이터셋)을 검사하는 교차검증 경로다. 검사 대상과 같은 파일을
쓰면 검증이 성립하지 않는다. 인계본에 동봉된 연속지적도(전북 2026-08, 1.1GB)를
빌려 오면 「같은 원천으로 같은 답을 냈다」는 말밖에 못 한다.

VWorld 의 `LP_PA_CBND_BUBUN` 이 그 shapefile 과 같은 국토교통부 연속지적도이고,
파일 스냅샷과 달리 현행이다. 원천 독립성과 최신성을 동시에 얻는다.

왜 전건 색인을 만들지 않는가
----------------------------
납품본은 LH 내부망에서 외부 호출 없이 돌아야 해서 3,888,333 필지를 통째로 색인한다.
이 앱에는 그 제약이 없다. 필요한 필지만 건별로 조회하면 되고, 등록공장 7,983건
확정에 실측 2.5분(동시성 5)이 걸린다. 색인 구축은 목적이 아니라 우회 수단이었다.

동기 프로토콜인 이유
--------------------
`PnuResolver.CadastralBackend` 가 동기다(로컬 SQLite 전제). 여기서 async 로 바꾸면
확정 파이프라인 전체가 async 로 번진다. 대신 이 백엔드는 **요청 경로에서 쓰지
않는다** — factoryON 확정은 기동 시 워밍업 스레드에서 한 번 돌고 결과가 캐시된다
(`pnu_resolution_cache`). 그래서 동기 HTTP 로 충분하고, 호출 수를 줄이는 메모 캐시를
프로세스 안에 둔다.

실패는 삼키지 않되 죽지도 않는다
--------------------------------
조회 실패(네트워크·쿼터·오류 응답)는 None 을 돌려준다. 상위 PnuResolver 는 None 을
「실재하지 않음」이 아니라 확정 불가로 다뤄 검토·실패로 분류하므로, 조용한 오확정이
생기지 않는다. 다만 실패가 몇 건이었는지는 `stats` 로 드러낸다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.models import Coordinates
from app.services.cadastral_local import LocalParcel
from app.services.http_client import shared_verify

logger = logging.getLogger(__name__)

VWORLD_DATA_URL = "https://api.vworld.kr/req/data"
CADASTRAL_LAYER = "LP_PA_CBND_BUBUN"

# 룰북 §7 ① 전북 범위 재검사. 로컬 백엔드(LocalParcel.in_jeonbuk)와 같은 기준을 쓴다.
JEONBUK_LAT = (35.0, 36.3)
JEONBUK_LNG = (126.3, 127.9)


@dataclass
class VWorldCadastralStats:
    """조회 계측. 배치가 끝난 뒤 사람이 눈으로 정상 여부를 본다."""

    by_pnu: int = 0
    by_point: int = 0
    prefix: int = 0
    hits: int = 0
    misses: int = 0
    errors: int = 0
    memo_hits: int = 0
    error_samples: list[str] = field(default_factory=list)

    @property
    def calls(self) -> int:
        return self.by_pnu + self.by_point + self.prefix

    def summary(self) -> str:
        return (
            f"VWorld 조회 {self.calls}회 "
            f"(PNU {self.by_pnu} · 점 {self.by_point} · 접두 {self.prefix}) · "
            f"적중 {self.hits} · 없음 {self.misses} · 오류 {self.errors} · "
            f"메모 재사용 {self.memo_hits}"
        )


def _outer_ring(geometry: dict[str, Any]) -> list[Coordinates]:
    """GeoJSON Polygon / MultiPolygon 에서 가장 넓은 외곽 링. 없으면 빈 목록."""

    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        rings = [coords[0]] if coords else []
    elif kind == "MultiPolygon":
        rings = [poly[0] for poly in coords if poly]
    else:
        return []
    if not rings:
        return []
    largest = max(rings, key=len)
    return [Coordinates(lat=float(p[1]), lng=float(p[0])) for p in largest]


def _ring_area_m2(ring: list[Coordinates]) -> float:
    """구면 근사 신발끈 면적. 필지 규모(수백~수만 ㎡)에서 오차가 무시할 수준이다.

    VWorld 응답에 면적 속성이 항상 있지는 않아 도형에서 직접 구한다. 위도별 경도
    수축을 반영하려고 평균 위도의 cos 를 곱한다.
    """

    if len(ring) < 4:
        return 0.0
    import math

    lat0 = sum(p.lat for p in ring) / len(ring)
    mx = 111_320.0 * math.cos(math.radians(lat0))
    my = 110_574.0
    acc = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i].lng * mx, ring[i].lat * my
        x2, y2 = ring[i + 1].lng * mx, ring[i + 1].lat * my
        acc += x1 * y2 - x2 * y1
    return abs(acc) / 2.0


def _in_jeonbuk(ring: list[Coordinates]) -> bool:
    if not ring:
        return False
    lat = sum(p.lat for p in ring) / len(ring)
    lng = sum(p.lng for p in ring) / len(ring)
    return JEONBUK_LAT[0] <= lat <= JEONBUK_LAT[1] and JEONBUK_LNG[0] <= lng <= JEONBUK_LNG[1]


class VWorldCadastralStore:
    """`PnuResolver.CadastralBackend` 를 VWorld API 로 구현한다.

    로컬 `CadastralLocalStore` 와 같은 `LocalParcel` 을 돌려주므로 상위 코드는
    어느 백엔드인지 몰라도 된다.
    """

    def __init__(
        self,
        api_key: str,
        *,
        domain: str = "localhost",
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = (api_key or "").strip()
        self.domain = domain
        self.timeout = timeout
        self._transport = transport
        self.stats = VWorldCadastralStats()
        # (종류, 키) → 결과. 같은 필지를 여러 경로에서 다시 묻는 일이 잦다.
        self._memo: dict[tuple[str, str], Any] = {}
        self._client: httpx.Client | None = None

    # -- 상태 --

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # -- 저수준 --

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                transport=self._transport,
                verify=shared_verify(),
            )
        return self._client

    def _features(self, filters: dict[str, str], size: int, geometry: bool) -> list[dict]:
        """GetFeature 응답의 feature 목록. 없으면 빈 목록, 오류도 빈 목록(계측만)."""

        if not self.available:
            return []
        params = {
            "service": "data",
            "request": "GetFeature",
            "data": CADASTRAL_LAYER,
            "key": self.api_key,
            "domain": self.domain,
            "format": "json",
            "geometry": "true" if geometry else "false",
            "size": str(size),
            **filters,
        }
        try:
            r = self._http().get(VWORLD_DATA_URL, params=params)
        except httpx.HTTPError as exc:
            self.stats.errors += 1
            self._note_error(f"{type(exc).__name__}: {exc}")
            return []
        if r.status_code != 200:
            self.stats.errors += 1
            self._note_error(f"HTTP {r.status_code}")
            return []
        try:
            body = (r.json() or {}).get("response") or {}
        except ValueError:
            self.stats.errors += 1
            self._note_error("응답이 JSON 이 아님")
            return []
        status = body.get("status")
        if status in ("NOT_FOUND", "EMPTY"):
            return []
        if status != "OK":
            self.stats.errors += 1
            self._note_error(str((body.get("error") or {}).get("text") or status))
            return []
        result = body.get("result") or {}
        collection = result.get("featureCollection") or {}
        return list(collection.get("features") or [])

    def _note_error(self, text: str) -> None:
        if len(self.stats.error_samples) < 5:
            self.stats.error_samples.append(text)

    @staticmethod
    def _to_parcel(feature: dict, fallback_pnu: str = "") -> LocalParcel | None:
        props = feature.get("properties") or {}
        ring = _outer_ring(feature.get("geometry") or {})
        if not ring:
            return None
        pnu = str(props.get("pnu") or fallback_pnu or "")
        return LocalParcel(
            pnu=pnu,
            jibun=str(props.get("jibun") or ""),
            address=str(props.get("addr") or ""),
            ring=ring,
            area_m2=_ring_area_m2(ring),
            in_jeonbuk=_in_jeonbuk(ring),
        )

    # -- CadastralBackend 프로토콜 --

    def parcel_by_pnu(self, pnu: str) -> LocalParcel | None:
        if not pnu:
            return None
        key = ("pnu", pnu)
        if key in self._memo:
            self.stats.memo_hits += 1
            return self._memo[key]
        self.stats.by_pnu += 1
        features = self._features({"attrFilter": f"pnu:=:{pnu}"}, size=1, geometry=True)
        parcel = self._to_parcel(features[0], fallback_pnu=pnu) if features else None
        if parcel is None:
            self.stats.misses += 1
        else:
            self.stats.hits += 1
        self._memo[key] = parcel
        return parcel

    def sole_parcel_at(self, lat: float, lng: float) -> tuple[LocalParcel | None, int]:
        """점을 포함하는 필지가 **단 하나일 때만** 그 필지를 돌려준다.

        VWorld 의 POINT geomFilter 는 그 점을 포함하는 필지를 준다. 경계를 공유하는
        필지가 함께 잡힐 수 있으므로 size=2 로 받아 개수를 센다. 둘 이상이면 상위가
        검토대상으로 분리하도록 (None, 개수) 를 돌려준다 — 로컬 백엔드와 같은 규약이다.
        """

        key = ("pt", f"{lat:.7f},{lng:.7f}")
        if key in self._memo:
            self.stats.memo_hits += 1
            return self._memo[key]
        self.stats.by_point += 1
        features = self._features(
            {"geomFilter": f"POINT({lng} {lat})", "crs": "EPSG:4326"},
            size=2,
            geometry=True,
        )
        count = len(features)
        parcel = self._to_parcel(features[0]) if count == 1 else None
        if count:
            self.stats.hits += 1
        else:
            self.stats.misses += 1
        out = (parcel, count)
        self._memo[key] = out
        return out

    def representative_point(self, parcel: LocalParcel) -> Coordinates | None:
        """필지 내부가 보장된 대표점. 외부 조회 없이 링에서 구한다.

        shapely 가 있으면 `representative_point()` 를 쓰고(오목 필지에서 중심점이
        밖으로 나가는 문제를 피한다), 없으면 링 평균점으로 물러선다.
        """

        if not parcel or not parcel.ring:
            return None
        try:
            from shapely.geometry import Polygon

            poly = Polygon([(p.lng, p.lat) for p in parcel.ring])
            if poly.is_valid and not poly.is_empty:
                pt = poly.representative_point()
                return Coordinates(lat=float(pt.y), lng=float(pt.x))
        except Exception:  # shapely 부재·도형 오류는 폴백으로 넘긴다
            pass
        n = len(parcel.ring)
        return Coordinates(
            lat=sum(p.lat for p in parcel.ring) / n,
            lng=sum(p.lng for p in parcel.ring) / n,
        )

    def pnu_prefix_exists(self, prefix: str) -> bool:
        """같은 본번의 다른 부번이 실재하는지(실패 사유 구분용).

        VWorld attrFilter 의 `like` 로 접두 조회한다. 도형은 필요 없어 받지 않는다.
        """

        if not prefix:
            return False
        key = ("prefix", prefix)
        if key in self._memo:
            self.stats.memo_hits += 1
            return self._memo[key]
        self.stats.prefix += 1
        features = self._features(
            {"attrFilter": f"pnu:like:{prefix}"}, size=1, geometry=False
        )
        out = bool(features)
        self._memo[key] = out
        return out
