"""시군구 「액화석유가스업」 인허가 파일(ODcloud) 레지스트리 — LPG 판매소·저장소 지역 원천.

LPG 판매·저장은 액화석유가스법상 시군구 허가라 행안부 LOCALDATA(고압가스업)에 없고, 전국
통합 원장도 없다(H-02-나 §8). 대신 시군구가 저마다 「가스사업자 현황」 파일을 올린다.
2026-09-17 조사로 확인한 51개 파일을 여기 표로 두고, 사업지 주소의 시도·시군구에 맞는
파일만 불러 판매(→ 나목 판정 후보)·저장(→ 저장소 참고 핀)을 가려낸다.

한계(문구로 밝힌다):
- ODcloud 파일 API 는 **데이터셋마다 활용신청**이 필요하다. 승인되지 않은 파일은 401 이
  오고, 이 어댑터는 그 사실을 `unavailable` 로 돌려 설정 화면이 「활용신청 필요」로 보이게
  한다(실측: 51개 중 부안군만 승인, 7개는 uddi 가 바뀌어 404).
- 파일마다 열 이름이 달라(상호/업체명/업소명 · 소재지/사업소주소/도로명주소 · 사업종류/
  구분/업종) 휴리스틱으로 읽는다. 좌표 열이 있는 파일(광진구·미추홀구·남양주 등)은 그것을
  쓰고, 없으면 주소를 카카오로 지오코딩한다.
- 갱신주기가 연 1회~수시로 제각각이라 폐업 반영이 늦을 수 있다.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

import httpx

from app.models import Coordinates
from app.services.address_candidates import address_candidates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.kgs import PublicDataAPIError
from app.services.single_flight import LoopSafeLock

ODCLOUD_BASE = "https://api.odcloud.kr/api"
PAGE_SIZE = 1000
MAX_PAGES = 5
CACHE_TTL_SECONDS = 24 * 60 * 60

Geocoder = Callable[[str], Awaitable[Coordinates | None]]


class MunicipalDataset(NamedTuple):
    dataset_id: str
    sido: str  # 짧은 시도명: 서울·부산·대구·인천·전남(광주 포함)·대전·울산·경기·강원·충북·충남·전북·경북·경남·제주
    sigungu: str  # 비면 시도 통합본
    title: str
    uddi: str

    @property
    def url(self) -> str:
        return f"{ODCLOUD_BASE}/{self.dataset_id}/v1/{self.uddi}"

    @property
    def page_url(self) -> str:
        return f"https://www.data.go.kr/data/{self.dataset_id}/fileData.do"


# 2026-09-17 data.go.kr 실측(제목·uddi). 시군구가 비면 시도 통합본. 파일 변환 API 가 없는
# 링크형(은평구 3072781 · 서울 통합 15048520 → 열린데이터광장 API 로 대체 · 영도구 15083305 ·
# 수영구 3076109 · 사상구 15001656 · 울산 남구 3076230)은 뺐다.
LPG_MUNICIPAL_DATASETS: tuple[MunicipalDataset, ...] = (
    MunicipalDataset("15036318", "서울", "광진구", "서울특별시 광진구_가스관련업", "uddi:d718c55f-7f1e-4757-b2af-851ad945fbc3"),
    MunicipalDataset("3070055", "부산", "북구", "부산광역시 북구_LP가스판매업 현황", "uddi:d2ee720a-eef2-4c34-a67d-faff647a3a0a"),
    MunicipalDataset("3070484", "부산", "금정구", "부산광역시 금정구_가스(LPG) 취급업소 현황", "uddi:2125ff56-82f6-4869-8ed6-cc29b39545b0"),
    MunicipalDataset("15060276", "부산", "남구", "부산광역시 남구_액화석유가스판매업소현황", "uddi:46a44114-80aa-46b5-8134-f664f0c7ee20"),
    MunicipalDataset("15025969", "부산", "동래구", "부산광역시 동래구_액화석유가스판매업소현황", "uddi:fdbfd83b-735e-428e-9495-9778131e0b5f"),
    MunicipalDataset("3075760", "부산", "해운대구", "부산광역시 해운대구_가스사업자 현황", "uddi:28151a2b-4e4a-4d54-84cf-bb9ea922dc58"),
    MunicipalDataset("3055762", "대구", "동구", "대구광역시 동구_에너지관련정보", "uddi:d6c0d014-6c78-475f-8e0f-8784bf624a07"),
    MunicipalDataset("15030569", "대구", "북구", "대구광역시 북구_가스사업자", "uddi:b7a999a9-9ad7-461b-a4df-95425f3a22be"),
    MunicipalDataset("15093581", "대구", "서구", "대구광역시 서구_가스사업자 현황", "uddi:cc5b600b-f984-4458-971e-c06c0e104bf0"),
    MunicipalDataset("3081430", "대구", "서구", "대구광역시 서구_에너지 관련 업체 현황", "uddi:ab7274fb-73b5-4e65-85ae-9df8ea728b7e"),
    MunicipalDataset("15016230", "인천", "미추홀구", "인천광역시 미추홀구_가스사업자 현황", "uddi:02ffb116-ff76-45d4-958d-bcf2db4cedd3"),
    MunicipalDataset("15102594", "인천", "부평구", "인천광역시 부평구_액화석유가스사업자 현황", "uddi:e4dce756-1067-4298-85fa-f4f66061c0d4"),
    MunicipalDataset("15105596", "인천", "서해구", "인천광역시 서해구_액화석유가스 판매업체", "uddi:2b206311-ae49-4456-ad92-ffb516a310c9"),
    MunicipalDataset("15118117", "인천", "서해구", "인천광역시 서해구_가스판매업체", "uddi:4b4323be-5710-4849-a63e-bb8df98ecf2a"),
    MunicipalDataset("15067176", "인천", "남동구", "인천광역시 남동구_가스사업자현황", "uddi:c3abc884-d35d-4b5e-80b6-ac358268e6cd"),
    MunicipalDataset("15006345", "인천", "제물포구", "인천광역시 제물포구_가스사업자현황", "uddi:5b1234be-dcab-4d37-90ee-fafa4ef9bc0f"),
    MunicipalDataset("15064799", "인천", "연수구", "인천광역시 연수구_가스사업자 현황", "uddi:ee882c15-d809-45e4-982a-e0fde89115c3"),
    MunicipalDataset("15038702", "인천", "중구", "인천광역시 중구_가스공급 및 판매업소", "uddi:8a6cc8ff-e699-4516-ac3b-deb23bbeccea"),
    MunicipalDataset("15072846", "대전", "중구", "대전광역시 중구_가스사업자 현황", "uddi:a4c1acef-beda-4f96-9fdf-a0f95e05dac8"),
    MunicipalDataset("3081083", "대전", "동구", "대전광역시 동구 가스사업자 현황", "uddi:65c33864-ee1f-4c8f-9167-8ae7d05f519a"),
    MunicipalDataset("15159968", "대전", "대덕구", "대전광역시 대덕구_가스사업자현황", "uddi:ca155b89-f9b5-4310-aee5-b74e361d9acb"),
    MunicipalDataset("15061959", "대전", "서구", "대전광역시 서구_고압가스업현황", "uddi:5ee4819b-1069-4019-97d4-aca28dda56fb"),
    MunicipalDataset("15033608", "경기", "오산시", "경기도 오산시_액화석유가스 정보", "uddi:473c156f-6cf9-4a5e-9622-dde2edca6519"),
    MunicipalDataset("3078987", "경기", "고양시", "경기도 고양시_가스사업자현황", "uddi:382d4002-7caf-4de9-8e1d-ad47506aab80"),
    MunicipalDataset("3079364", "경기", "부천시", "경기도 부천시_가스사업자 현황", "uddi:7cdb7986-f91f-48b3-bad1-d88a5a92268a"),
    MunicipalDataset("15044240", "경기", "용인시", "경기도 용인시_가스사업자 현황", "uddi:a62742a5-b0cc-40c1-b6c1-ec0e9e9857f4"),
    MunicipalDataset("15126786", "경기", "남양주시", "경기도 남양주시_가스사업자 현황", "uddi:b932ed49-c8fa-43dc-8425-df595780afce"),
    MunicipalDataset("15105096", "경기", "안산시", "경기도 안산시_가스사업자 현황", "uddi:79dde7e9-bff7-4cb5-b6d6-d5bc5e65697a"),
    MunicipalDataset("3044402", "강원", "원주시", "강원특별자치도 원주시_가스사업 허가 정보", "uddi:0910d776-2944-4d2f-af17-c3f31eb4c1db"),
    MunicipalDataset("15033688", "강원", "", "강원도_가스사업자 현황", "uddi:bb5f8b2c-980a-48f2-b265-add8d2a09f28"),
    MunicipalDataset("15064201", "전북", "부안군", "전북특별자치도 부안군_액화석유가스업", "uddi:d6e68266-5548-40ad-9fac-e2a595b9f54c"),
    # 익산은 ODcloud 경로에 버전 접미사가 붙는다(2026-09-17 OAS 확인). 접미사 없이 부르면 404.
    MunicipalDataset("3079211", "전북", "익산시", "전북특별자치도 익산시_가스사업자", "uddi:a3e232db-b13e-466e-8ffd-fce01436c100_201910141642"),
    MunicipalDataset("3034600", "전남", "동구", "전남광주통합특별시 동구_가스공급및자동차충전업소", "uddi:632d4270-4a87-430a-bb93-c34ec2658b3e"),
    MunicipalDataset("15157953", "전남", "남구", "전남광주통합특별시 남구_가스사업 허가업소", "uddi:1e749691-56bf-4b86-a6e6-989105d5f33a"),
    MunicipalDataset("15052478", "전남", "", "전남광주통합특별시 북부소방서 관내 가스시설 현황", "uddi:f34c8942-4a33-41b7-9ee8-e40f2853472a"),
    MunicipalDataset("15001884", "전남", "", "전남광주통합특별시 LPG 판매업소 현황", "uddi:62b432a7-db0d-4f71-9f3f-93917fddc064"),
    MunicipalDataset("15033653", "전남", "영광군", "전남광주통합특별시 영광군_LPG충전소 및 판매소", "uddi:cf58d2e1-1604-4225-821d-f972061ca93f"),
    MunicipalDataset("3069584", "경북", "구미시", "경상북도 구미시_가스판매업소정보", "uddi:dd05d2b0-da1c-48d4-865b-c4ebe8322b0c"),
    MunicipalDataset("15154348", "경북", "안동시", "경상북도 안동시_대표홈페이지 에너지공급업체", "uddi:d4b4c242-e6af-488f-ab35-9863180918b5"),
    MunicipalDataset("3079318", "경남", "거제시", "경상남도 거제시_액화석유가스충전사업자현황", "uddi:b8ac7f0f-35ae-4e3e-a56c-d747af0a9577"),
    MunicipalDataset("15104217", "경남", "양산시", "경상남도 양산시_가스시설보유업체 현황", "uddi:7e43cc4c-94ed-4f4f-b8db-c59d6a0695bf"),
    MunicipalDataset("15087098", "경남", "하동군", "경상남도 하동군_고압가스업 현황", "uddi:95dbbc69-2f36-4a53-ae1c-d21cdfbe1e6e"),
    MunicipalDataset("15039855", "충남", "청양군", "충청남도 청양군_가스충전소 및 고압가스저장소 설치현황", "uddi:dd60e274-a6f8-43c6-9d8d-e8800729e8e9"),
    MunicipalDataset("15053148", "제주", "서귀포시", "제주특별자치도 서귀포시_가스판매업현황", "uddi:6a25b2ba-e40a-4f1d-81bf-8f8f84002e60"),
)
DATASET_BY_ID: dict[str, MunicipalDataset] = {d.dataset_id: d for d in LPG_MUNICIPAL_DATASETS}

# 주소 앞머리 → 짧은 시도명. 옛 이름(전라북도·광주광역시 등)도 받는다.
SIDO_PREFIXES: tuple[tuple[str, str], ...] = (
    ("서울", "서울"), ("부산", "부산"), ("대구", "대구"), ("인천", "인천"),
    ("광주", "전남"), ("전남광주", "전남"), ("대전", "대전"), ("울산", "울산"),
    ("세종", "세종"), ("경기", "경기"), ("강원", "강원"), ("충청북도", "충북"),
    ("충북", "충북"), ("충청남도", "충남"), ("충남", "충남"), ("전북", "전북"),
    ("전라북도", "전북"), ("전라남도", "전남"), ("전남", "전남"), ("경상북도", "경북"),
    ("경북", "경북"), ("경상남도", "경남"), ("경남", "경남"), ("제주", "제주"),
)

NAME_KEYS = ("상호", "업체명", "업소명", "사업장명", "상호명", "시설명", "회사명", "사업자명")
ADDRESS_KEYS = (
    "사업소주소", "사업장소재지", "소재지도로명주소", "도로명주소", "소재지주소", "소재지",
    "사업장주소", "주소", "지번주소", "소재지지번주소", "영업소재지", "사업소소재지",
)
KIND_KEYS = ("사업종류", "업종구분", "사업구분", "허가구분", "구분", "업종", "유형", "취급가스", "사업의종류", "사업자구분", "인허가구분", "종류")
STATUS_KEYS = ("영업구분", "영업상태", "영업상태명", "상태", "운영상태")
LAT_KEYS = ("위도", "lat", "LAT")
LNG_KEYS = ("경도", "lng", "LNG", "lon")
CLOSED_TOKENS = ("폐업", "폐지", "취소", "말소", "휴업", "휴지")


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    # 열 이름에 공백·괄호가 섞인 파일 대비: 부분 일치.
    for column, value in row.items():
        if value in (None, ""):
            continue
        flat = str(column).replace(" ", "")
        if any(key in flat for key in keys):
            return str(value).strip()
    return ""


def classify_kind(raw: str) -> str:
    """원장의 사업종류를 판매·저장·충전·기타·미상 으로 정규화한다."""

    text = raw.replace(" ", "")
    if not text:
        return "미상"
    # 고압가스(고법) 행이 섞인 파일(익산 「고압가스판매」 등)은 LPG 가 아니라 기타.
    if "고압" in text:
        return "기타"
    if "저장" in text:
        return "저장"
    if "충전" in text:
        return "충전"
    if "판매" in text:
        return "판매"
    return "기타"


class MunicipalRow(NamedTuple):
    name: str
    address: str
    kind: str
    kind_raw: str
    status: str
    coordinates: Coordinates | None


def parse_row(row: dict[str, Any]) -> MunicipalRow | None:
    name = _pick(row, NAME_KEYS)
    address = _pick(row, ADDRESS_KEYS)
    if not name or not address:
        return None
    status = _pick(row, STATUS_KEYS)
    if any(token in status for token in CLOSED_TOKENS):
        return None
    kind_raw = _pick(row, KIND_KEYS)
    coordinates: Coordinates | None = None
    lat, lng = _pick(row, LAT_KEYS), _pick(row, LNG_KEYS)
    if lat and lng:
        try:
            coordinates = Coordinates(lat=float(lat), lng=float(lng))
        except (TypeError, ValueError):
            coordinates = None
    return MunicipalRow(name, address, classify_kind(kind_raw), kind_raw, status, coordinates)


def sido_of(address: str) -> str:
    head = address.strip()
    for prefix, sido in SIDO_PREFIXES:
        if head.startswith(prefix):
            return sido
    return ""


def datasets_for_address(address: str) -> list[MunicipalDataset]:
    """사업지 주소의 시도·시군구에 해당하는 파일들. 시도 통합본은 시도만 맞으면 포함."""

    sido = sido_of(address)
    if not sido:
        return []
    tokens = address.replace(",", " ").split()
    return [
        d for d in LPG_MUNICIPAL_DATASETS
        if d.sido == sido and (not d.sigungu or any(token.startswith(d.sigungu) for token in tokens))
    ]


class MunicipalFacility(NamedTuple):
    dataset_id: str
    dataset_title: str
    record_id: str
    name: str
    address: str
    kind: str
    kind_raw: str
    status: str
    coordinates: Coordinates


class GeocodeFailure(NamedTuple):
    name: str
    address: str


class MunicipalLookup(NamedTuple):
    facilities: list[MunicipalFacility]
    datasets_used: list[str]
    unavailable: dict[str, str]
    geocode_failures: list[GeocodeFailure]

    @property
    def covered(self) -> bool:
        return bool(self.datasets_used)


class LpgMunicipalClient:
    """사업지 시군구의 파일만 불러(데이터셋별 24시간 캐시) 지오코딩·반경 필터한다."""

    def __init__(
        self,
        service_key: str,
        geocode: Geocoder | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key
        self._geocode = geocode
        self.timeout = timeout
        self._transport = transport
        self._cache: dict[str, tuple[list[MunicipalFacility], list[GeocodeFailure], float]] = {}
        self._unavailable: dict[str, str] = {}
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key and self._geocode is not None)

    @property
    def unavailable(self) -> dict[str, str]:
        return dict(self._unavailable)

    async def facilities_for_site(
        self, site_address: str, center: Coordinates, radius_m: float
    ) -> MunicipalLookup:
        if not self.enabled:
            return MunicipalLookup([], [], {}, [])
        facilities: list[MunicipalFacility] = []
        used: list[str] = []
        unavailable: dict[str, str] = {}
        failures: list[GeocodeFailure] = []
        for dataset in datasets_for_address(site_address):
            try:
                rows, dataset_failures = await self._dataset(dataset)
            except PublicDataAPIError as exc:
                unavailable[dataset.dataset_id] = str(exc)
                self._unavailable[dataset.dataset_id] = str(exc)
                continue
            self._unavailable.pop(dataset.dataset_id, None)
            used.append(dataset.dataset_id)
            failures.extend(dataset_failures)
            facilities.extend(
                f for f in rows if haversine_meters(center, f.coordinates) <= radius_m
            )
        return MunicipalLookup(facilities, used, unavailable, failures)

    async def probe(self, dataset: MunicipalDataset) -> int:
        """첫 1건으로 승인·생존 확인. 총건수를 돌려준다."""

        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            _, total = await self._page(client, dataset, 1, per_page=1)
        return total

    async def _dataset(
        self, dataset: MunicipalDataset
    ) -> tuple[list[MunicipalFacility], list[GeocodeFailure]]:
        cached = self._cache.get(dataset.dataset_id)
        if cached and time.monotonic() - cached[2] < CACHE_TTL_SECONDS:
            return cached[0], cached[1]
        async with self._fill_lock.get():
            cached = self._cache.get(dataset.dataset_id)
            if cached and time.monotonic() - cached[2] < CACHE_TTL_SECONDS:
                return cached[0], cached[1]
            rows = await self._fetch_rows(dataset)
            facilities, failures = await self._geocode_rows(dataset, rows)
            self._cache[dataset.dataset_id] = (facilities, failures, time.monotonic())
            return facilities, failures

    async def _geocode_rows(
        self, dataset: MunicipalDataset, rows: list[dict[str, Any]]
    ) -> tuple[list[MunicipalFacility], list[GeocodeFailure]]:
        assert self._geocode is not None
        facilities: list[MunicipalFacility] = []
        failures: list[GeocodeFailure] = []
        for index, row in enumerate(rows):
            parsed = parse_row(row)
            if parsed is None:
                continue
            coordinates = parsed.coordinates
            if coordinates is None:
                for candidate in address_candidates(parsed.address):
                    coordinates = await self._geocode(candidate)
                    if coordinates is not None:
                        break
            if coordinates is None:
                failures.append(GeocodeFailure(parsed.name, parsed.address))
                continue
            facilities.append(
                MunicipalFacility(
                    dataset.dataset_id, dataset.title, f"{dataset.dataset_id}-{index}",
                    parsed.name, parsed.address, parsed.kind, parsed.kind_raw, parsed.status,
                    coordinates,
                )
            )
        return facilities, failures

    async def _fetch_rows(self, dataset: MunicipalDataset) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self._transport, verify=shared_verify()
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                page_rows, total = await self._page(client, dataset, page)
                rows.extend(page_rows)
                if not page_rows or len(rows) >= total:
                    break
        return rows

    async def _page(
        self, client: httpx.AsyncClient, dataset: MunicipalDataset, page: int,
        per_page: int = PAGE_SIZE,
    ) -> tuple[list[dict[str, Any]], int]:
        params = {
            "serviceKey": self.service_key,
            "page": str(page),
            "perPage": str(per_page),
            "returnType": "JSON",
        }
        try:
            response = await client.get(dataset.url, params=params)
        except httpx.HTTPError as exc:
            raise PublicDataAPIError(f"{dataset.title} 조회에 실패했습니다: {exc}") from exc
        if response.status_code == 401:
            raise PublicDataAPIError(
                f"{dataset.title}({dataset.dataset_id}) 활용신청 필요 (401)", 401
            )
        if response.status_code == 404:
            raise PublicDataAPIError(
                f"{dataset.title}({dataset.dataset_id}) 파일 버전(uddi) 변경 — 재확인 필요 (404)", 404
            )
        if response.status_code != 200:
            raise PublicDataAPIError(
                f"{dataset.title} 응답 오류 ({response.status_code})", response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PublicDataAPIError(f"{dataset.title} 응답을 해석하지 못했습니다.") from exc
        if not isinstance(payload, dict) or "data" not in payload:
            raise PublicDataAPIError(
                f"{dataset.title} 오류: {payload.get('msg') if isinstance(payload, dict) else payload}"
            )
        rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]
        try:
            total = int(payload.get("totalCount"))
        except (TypeError, ValueError):
            total = MAX_PAGES * PAGE_SIZE
        return rows, total
