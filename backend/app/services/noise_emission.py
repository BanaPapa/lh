"""전국 소음진동배출시설 표준데이터 조회 — 공공데이터포털 tn_pubr 표준데이터.

docs/hazards/H-01-라 공장 라목「소음배출시설 AND 공장」판정의 원천이다. 이
어댑터는 그중 **소음배출시설**만 책임진다. 공장 여부(AND)는 통합 단계에서
`app/services/local_sources.py` 의 `factory_registry_pnus()`(PNU 577개)와 대조해
판정한다. 이 원천에는 PNU 가 없으므로 지번주소(lctnLotnoAddr)를 그대로 넘겨
통합 단계가 PNU 해석·주소 매칭을 하게 한다.

실호출로 확정한 사실(2026-08-28):
- 호스트가 `api.data.go.kr` 다. 다른 어댑터가 쓰는 `apis.data.go.kr` 가 아니다.
- 경로: `https://api.data.go.kr/openapi/tn_pubr_public_noise_vibration_emission_fclt_api`
- 실제 경로는 403 SERVICE_KEY_IS_NOT_REGISTERED_ERROR(활용신청 미완)를 준다.
  없는 경로는 400 NO_OPENAPI_SERVICE_ERROR 다. 즉 엔드포인트는 존재하고 막고
  있는 것은 활용신청뿐이다(safemap·테마파크 데이터셋과 같은 상태).
  => **활용신청이 필요하다.** 승인되면 이 어댑터는 코드 수정 없이 붙는다.
- 파라미터 규약: `serviceKey · pageNo · numOfRows · type` 을 받는다. `type` 을
  넣어도 INVALID_REQUEST_PARAMETER_ERROR 가 나지 않는다(화장시설 API 와 다르다).
  표준데이터 기본 응답은 XML 이므로 JSON 을 받으려면 `type=json` 이 필요하다.
  이 넷 외의 파라미터는 넣지 않는다(불필요 파라미터로 API 가 깨진 전례가 있다).

응답 스키마(전북 원본 08_noise_vibration_facilities.csv 실측, 16컬럼):
표준데이터 CSV 헤더 = 표준데이터 API JSON 키(동일 필드코드)라 CSV·API 를 같은
`_facility()` 로 파싱한다.
- fcltNm          사업장(시설)명
- ctpvNm          시도명            (전북 원본은 전부 '전북특별자치도')
- sggNm           시군구명
- lctnRoadNmAddr  소재지도로명주소
- lctnLotnoAddr   소재지지번주소     (146건 중 142건 존재)
- lat / lot       위도 / 경도  => **WGS84 위경도**(예: 35.919, 126.949). 투영이 아니다.
- noisVbrtSeNm    소음진동구분명     (전북 원본은 전부 '소음')
- noisVbrtMainCn  소음진동주요내용   (전북 원본은 **전건 공란**)
- gnrlRgnYn / rdsdRgnYn  일반지역여부 / 도로변지역여부
- telno / rprsvNm 전화번호 / 대표자명
- dataCrtrYmd / insttCode / insttNm  데이터기준일자 / 제공기관코드 / 제공기관명

**50dB 예외(라목 법령: 소음도 50dB 이하이거나 방음시설로 50dB 이하가 될 수 있으면
제외)에 필요한 필드가 이 원천에 없다.** 소음도(dB)·방음시설 컬럼이 없고,
noisVbrtMainCn 은 전건 공란이다. 따라서 이 원천만으로는 50dB 예외를 확인할 수
없다. 각 시설은 `noise_level_db=None` 을 노출하고 `NOISE_LEVEL_FIELD_AVAILABLE`
은 False 다. 룰북 §3「확인 불가 조건은 추정하지 않는다」에 따라, 통합 단계는
라목 판정을 예외 미확인 상태(확인불가 계열)로 남겨야 한다. 예외가 없다고 단정해
매입제외를 확정하면 안 된다.

배선(통합 단계에서 내가 한다):
- 클라이언트 생성:  `NoiseEmissionClient(config.public_data_key, csv_path=...)`
  (safemap·crematorium 처럼 `app/hazard_review/router.py` 의 서비스 조립부.)
- CSV 보조 경로는 설정값으로 주입한다. config 에 필드가 없으므로 통합 단계에서
  `Settings` 에 예: `noise_emission_csv_path: str = ""` 를 추가하고
  `csv_path=config.noise_emission_csv_path or None` 로 넘기면 된다. 하드코딩 금지.
- 서비스(`HazardReviewService`)에 `noise_emission` 파라미터를 추가하고, 조회 실패
  (`NoiseEmissionAPIError`)는 다른 원천과 동일하게 dataset_missing 으로 반영한다.
- 라목 판정: `facilities_around()` 결과의 지번주소로 공장 PNU 집합과 대조 →
  AND. 매칭돼도 50dB 예외 미확인이므로 확인불가 계열로 표기.
"""

from __future__ import annotations

import csv
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, NamedTuple

import httpx
from pyproj import Transformer

from app.models import Coordinates
from app.services.geo import haversine_meters
from app.services.http_client import shared_verify
from app.services.single_flight import LoopSafeLock


NOISE_EMISSION_URL = (
    "https://api.data.go.kr/openapi/"
    "tn_pubr_public_noise_vibration_emission_fclt_api"
)

# 표준데이터(tn_pubr) 공통 상한. 승인 전이라 실측하지 못했다. 1000 은 표준데이터
# 계열이 일반적으로 허용하는 값이다. 승인 후 실측으로 조정할 수 있다.
PAGE_SIZE = 1000

# 안전장치. 전국 규모를 특정하지 못했으므로 넉넉히 잡고 totalCount 로 멈춘다.
MAX_PAGES = 200

# 배출시설 목록은 하루에도 몇 곳씩 바뀌지 않는다. 하루 한 번이면 충분하다.
CACHE_TTL_SECONDS = 24 * 60 * 60

# 표준데이터 CSV 인코딩. 전북 원본은 UTF-8 BOM(utf-8-sig)이다.
CSV_ENCODING = "utf-8-sig"

# 라목 50dB 예외를 확인할 소음도·방음시설 필드가 이 원천에 없음을 명시한다.
NOISE_LEVEL_FIELD_AVAILABLE = False

# 소음진동구분명에서 '소음배출시설'을 가리키는 값. 라목은 소음배출시설이 대상이다.
NOISE_SE_KEYWORD = "소음"

# 투영좌표(구 중부원점, Bessel)로 판명될 경우의 변환 대비. 전북 원본은 위경도라
# 실제로는 쓰이지 않지만, 승인 API 가 투영좌표를 줄 가능성에 대비해 둔다(룰북 §7 ①).
FALLBACK_PROJECTED_CRS = "EPSG:5174"

# 투영좌표로 볼 최소 크기(m). 한국 TM 동/북거리는 수십만 m 라 이보다 작으면
# 투영좌표가 아니라 손상값으로 본다.
PROJECTED_MIN_MAGNITUDE = 10000.0


class NoiseEmissionAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code

    @property
    def not_registered(self) -> bool:
        """활용신청 미완(미승인) 오류인지. 이때만 CSV 보조로 넘어간다."""

        if self.status_code == 403:
            return True
        code = (self.error_code or "").upper()
        return "SERVICE_KEY_IS_NOT_REGISTERED" in code


class NoiseEmissionFacility(NamedTuple):
    """소음진동배출시설 한 곳.

    라목은「소음배출시설 AND 공장」이다. 이 타입은 소음배출시설만 담는다. 공장
    여부는 통합 단계가 `pnu`(없으면 `lotno_address`)로 판정한다.
    """

    facility_id: str
    name: str
    coordinates: Coordinates
    region: str            # ctpvNm 시도
    sigungu: str           # sggNm 시군구
    road_address: str      # lctnRoadNmAddr
    lotno_address: str     # lctnLotnoAddr — 통합 단계의 공장 PNU 대조 열쇠
    se_name: str           # noisVbrtSeNm ('소음'/'진동')
    main_content: str      # noisVbrtMainCn (전북 원본은 전건 공란)
    representative: str = ""
    phone: str = ""
    # 이 원천에 PNU 가 없다. 통합 단계가 지번주소로 해석·매칭한다.
    pnu: str = ""
    # 50dB 예외 판정에 필요한 소음도(dB). 원천에 필드가 없어 항상 None 이다.
    # None 이면 통합 단계는 예외를 확인할 수 없으므로 확인불가로 남겨야 한다.
    noise_level_db: float | None = None

    @property
    def is_noise_facility(self) -> bool:
        """라목 대상인 소음배출시설인지. 진동만인 레코드는 제외된다."""

        return NOISE_SE_KEYWORD in (self.se_name or "")

    @property
    def exception_verifiable(self) -> bool:
        """50dB 예외를 이 원천만으로 확인할 수 있는지. 항상 False."""

        return NOISE_LEVEL_FIELD_AVAILABLE and self.noise_level_db is not None


@lru_cache(maxsize=1)
def _to_wgs84() -> Transformer:
    """투영좌표 → WGS84 변환기. 위경도가 아닐 때의 대비용(전북 원본엔 미사용)."""

    return Transformer.from_crs(FALLBACK_PROJECTED_CRS, "EPSG:4326", always_xy=True)


def _in_korea(lat: float, lng: float) -> bool:
    return 33.0 <= lat <= 39.5 and 124.0 <= lng <= 132.0


def _to_coords(lat_raw: Any, lot_raw: Any) -> Coordinates | None:
    """lat/lot 을 좌표로. 위경도면 그대로, 투영좌표면 변환 뒤 범위 재검사."""

    try:
        lat = float(lat_raw)
        lng = float(lot_raw)
    except (TypeError, ValueError):
        return None
    # 표준데이터 전북 원본은 WGS84 위경도다. 범위 안이면 그대로 쓴다.
    if _in_korea(lat, lng):
        return Coordinates(lat=lat, lng=lng)
    # 위경도 범위를 벗어나되 값이 작으면(예: 10) 투영좌표가 아니라 손상값이다.
    # 한국 TM(EPSG:5174) 동/북거리는 수십만 m 수준이라, 그 규모일 때만 변환을
    # 시도한다. 작은 값을 원점 근처로 변환해 억지로 살리지 않는다.
    if abs(lat) < PROJECTED_MIN_MAGNITUDE or abs(lng) < PROJECTED_MIN_MAGNITUDE:
        return None
    # 위경도가 아니면 투영좌표(x=lot, y=lat)로 보고 변환한 뒤 다시 범위를 본다.
    try:
        lng2, lat2 = _to_wgs84().transform(float(lot_raw), float(lat_raw))
    except (TypeError, ValueError):
        return None
    if _in_korea(lat2, lng2):
        return Coordinates(lat=lat2, lng=lng2)
    # 변환 후에도 한반도 밖이면 좌표 오류로 보고 버린다(룰북 §7 ①).
    return None


def _facility(row: dict[str, Any]) -> NoiseEmissionFacility | None:
    """응답/CSV 한 행을 시설로. 좌표가 없거나 범위를 벗어나면 버린다."""

    coordinates = _to_coords(row.get("lat"), row.get("lot"))
    if coordinates is None:
        return None

    name = str(row.get("fcltNm") or "").strip()
    lotno = str(row.get("lctnLotnoAddr") or "").strip()
    road = str(row.get("lctnRoadNmAddr") or "").strip()
    facility_id = f"{name}:{lotno or road}:{coordinates.lat:.6f}:{coordinates.lng:.6f}"

    return NoiseEmissionFacility(
        facility_id=facility_id,
        name=name,
        coordinates=coordinates,
        region=str(row.get("ctpvNm") or "").strip(),
        sigungu=str(row.get("sggNm") or "").strip(),
        road_address=road,
        lotno_address=lotno,
        se_name=str(row.get("noisVbrtSeNm") or "").strip(),
        main_content=str(row.get("noisVbrtMainCn") or "").strip(),
        representative=str(row.get("rprsvNm") or "").strip(),
        phone=str(row.get("telno") or "").strip(),
        # 원천에 PNU·소음도(dB) 필드가 없다. 값을 지어내지 않는다.
        pnu="",
        noise_level_db=None,
    )


class NoiseEmissionClient:
    """소음진동배출시설 원천. API 우선 · CSV 보조.

    - service_key 가 있으면 API 를 먼저 호출한다.
    - API 가 미승인(403 SERVICE_KEY_IS_NOT_REGISTERED)이고 csv_path 가 주입돼
      있으면 CSV 로 보조한다. 그 외의 API 오류는 삼키지 않고 전파한다(상위에서
      dataset_missing 이 되도록).
    - service_key 가 없고 csv_path 만 있으면 CSV 로 동작한다.
    - 둘 다 없으면 enabled=False, 조회는 빈 결과(미적재)다.
    """

    def __init__(
        self,
        service_key: str,
        csv_path: str | Path | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.service_key = service_key or ""
        self._csv_path = Path(csv_path) if csv_path else None
        self.timeout = timeout
        self._transport = transport
        self._cache: list[NoiseEmissionFacility] = []
        self._cached_at = 0.0
        self._warmed = False
        # 전국 목록 캐시 채우기를 직렬화하는 single-flight 잠금. 동시 호출이 여럿
        # 이어도 실제 조회는 1회만 일어난다(캐시 스탬피드 방지).
        self._fill_lock = LoopSafeLock()

    @property
    def enabled(self) -> bool:
        return bool(self.service_key) or self._csv_path is not None

    def _is_warm(self) -> bool:
        return self._warmed and time.monotonic() - self._cached_at < CACHE_TTL_SECONDS

    async def facilities_around(
        self,
        center: Coordinates,
        radius_m: float,
    ) -> list[NoiseEmissionFacility]:
        """반경 안의 소음진동배출시설. 전국 목록을 캐시해 두고 거리로 거른다."""

        facilities = await self.all_facilities()
        return [
            facility
            for facility in facilities
            if haversine_meters(center, facility.coordinates) <= radius_m
        ]

    async def covered_sigungu(self) -> set[str]:
        """자료가 실제로 등록된 시군 집합.

        표준데이터 포털은 지자체가 개별 등록하는 구조라 전북 14개 시군 중 군산·진안·
        익산 3곳만 자료가 있다(docs/hazards H-01-라 §6-2). 등록하지 않은 시군은
        「시설 없음」이 아니라 「자료 없음」이므로, 판정 단계가 이 집합으로 사업지
        시군의 자료 유무를 가려 「판정 미적용 — 별도 수기 확인」으로 표시한다(§7-1).
        """

        return {
            facility.sigungu.strip()
            for facility in await self.all_facilities()
            if facility.sigungu.strip()
        }

    async def all_facilities(self) -> list[NoiseEmissionFacility]:
        if not self.enabled:
            return []
        if self._is_warm():
            return self._cache

        async with self._fill_lock.get():
            # 잠금을 잡은 뒤 다시 확인한다. 먼저 들어간 호출이 이미 채웠으면
            # 재조회하지 않는다(double-checked).
            if self._is_warm():
                return self._cache

            # 조회가 실패하면 예외가 그대로 전파돼 캐시를 건드리지 않는다. 실패를
            # 캐시하지 않으므로 다음 호출이 다시 시도할 수 있다.
            facilities = await self._load()
            self._cache = facilities
            self._cached_at = time.monotonic()
            self._warmed = True
            return facilities

    async def _load(self) -> list[NoiseEmissionFacility]:
        # API 우선.
        if self.service_key:
            try:
                return await self._fetch_api()
            except NoiseEmissionAPIError as exc:
                # 미승인일 때만 CSV 보조로 넘어간다. 그 외 오류는 전파한다.
                if exc.not_registered and self._csv_path is not None:
                    return self._load_csv()
                raise
        # 키가 없으면 CSV(보조 경로)로 동작한다.
        if self._csv_path is not None:
            return self._load_csv()
        return []

    # ---- API ----------------------------------------------------------------

    async def _fetch_api(self) -> list[NoiseEmissionFacility]:
        facilities: list[NoiseEmissionFacility] = []
        async with httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            follow_redirects=True,
            verify=shared_verify(),
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                rows, total = await self._page(client, page)
                for row in rows:
                    facility = _facility(row)
                    if facility:
                        facilities.append(facility)
                if len(rows) < PAGE_SIZE or page * PAGE_SIZE >= total:
                    break
        return facilities

    async def _page(
        self,
        client: httpx.AsyncClient,
        page: int,
    ) -> tuple[list[dict[str, Any]], int]:
        # 허용 파라미터는 이 넷뿐이다. 다른 파라미터를 넣으면 API 가 깨진 전례가 있다.
        params = {
            "serviceKey": self.service_key,
            "pageNo": str(page),
            "numOfRows": str(PAGE_SIZE),
            "type": "json",
        }
        try:
            response = await client.get(NOISE_EMISSION_URL, params=params)
        except httpx.HTTPError as exc:
            raise NoiseEmissionAPIError(
                f"소음진동배출시설 조회에 실패했습니다: {exc}"
            ) from exc

        if response.status_code != 200:
            # 표준데이터 오류 본문(cmmMsgHeader.errMsg)에서 사유 코드를 뽑아
            # 미승인 여부를 판정에 쓴다.
            error_code = _error_code(response)
            raise NoiseEmissionAPIError(
                f"소음진동배출시설 응답 오류 ({response.status_code})",
                status_code=response.status_code,
                error_code=error_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise NoiseEmissionAPIError(
                "소음진동배출시설 응답을 해석하지 못했습니다."
            ) from exc

        # 미승인 등은 200 이 아니라 상단 OpenAPI_ServiceResponse 로 오기도 한다.
        fault = payload.get("OpenAPI_ServiceResponse")
        if isinstance(fault, dict):
            header = fault.get("cmmMsgHeader") or {}
            code = str(header.get("errMsg") or header.get("returnReasonCode") or "")
            raise NoiseEmissionAPIError(
                f"소음진동배출시설 오류: {code or '알 수 없음'}",
                error_code=code,
            )

        response_body = payload.get("response") or {}
        header = response_body.get("header") or {}
        result_code = str(header.get("resultCode") or "00").strip()
        if result_code not in ("00", "0"):
            raise NoiseEmissionAPIError(
                f"소음진동배출시설 오류: {header.get('resultMsg') or result_code}",
                error_code=str(header.get("resultMsg") or result_code),
            )

        body = response_body.get("body") or {}
        items = body.get("items") or []
        # 표준데이터는 items 를 직접 리스트로 주지만, 계열별로 items.item 래핑도
        # 있어 양쪽을 모두 받는다.
        if isinstance(items, dict):
            items = items.get("item") or []
        if isinstance(items, dict):
            items = [items]
        rows = [row for row in items if isinstance(row, dict)]
        try:
            total = int(body.get("totalCount") or 0)
        except (TypeError, ValueError):
            total = 0
        return rows, total

    # ---- CSV 보조 -----------------------------------------------------------

    def _load_csv(self) -> list[NoiseEmissionFacility]:
        assert self._csv_path is not None
        path = self._csv_path
        if not path.exists():
            # 파일 부재를 빈 결과로 삼키지 않는다. 상위에서 dataset_missing 이 되게.
            raise NoiseEmissionAPIError(
                f"소음진동배출시설 CSV 를 찾지 못했습니다: {path}"
            )
        try:
            text = path.read_text(encoding=CSV_ENCODING)
        except OSError as exc:
            raise NoiseEmissionAPIError(
                f"소음진동배출시설 CSV 를 읽지 못했습니다: {exc}"
            ) from exc

        facilities: list[NoiseEmissionFacility] = []
        reader = csv.DictReader(text.splitlines())
        for row in reader:
            facility = _facility(row)
            if facility:
                facilities.append(facility)
        return facilities


def _error_code(response: httpx.Response) -> str | None:
    """표준데이터 오류 본문에서 errMsg(사유 코드)를 뽑는다. JSON·XML 모두 대응."""

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        fault = payload.get("OpenAPI_ServiceResponse")
        if isinstance(fault, dict):
            header = fault.get("cmmMsgHeader") or {}
            code = header.get("errMsg") or header.get("returnReasonCode")
            if code:
                return str(code)
    # XML 본문은 정규식 없이 단순 조회로 사유를 찾는다.
    text = response.text or ""
    if "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in text:
        return "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
    return None
