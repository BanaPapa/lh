"""건축HUB 건축물대장정보 서비스(apis.data.go.kr/1613000/BldRgstHubService) 표제부 조회.

룰북 §6.4 위락시설 가목(단란주점)·다목(테마파크)은 "인접 후보가 제2종 근린생활시설
비해당"이라는 AND 조건을 매입제외 확정의 전제로 둔다(다목은 운동시설 비해당까지).
이 교차확인 원천이 없어 해당 종류는 후보가 있어도 영구히 review_required 로 남아 있었다.
이 어댑터가 그 구멍을 메운다.

핵심은 표제부 오퍼레이션 `getBrTitleInfo` 다. 필지의 동별 표제부에서
`mainPurpsCdNm`(주용도코드명)·`etcPurps`(기타용도)를 준다. 이 앱은 시설마다 PNU 를
갖고 있으므로 PNU 19자리를 요청 파라미터로 분해하면 그대로 호출된다.

판정 원칙(룰북 §3): 확인 불가 조건은 추정하지 않는다. 용도가 명확한 표준 카테고리로
읽히지 않으면 '비해당'으로 단정하지 않고 '확인불가'로 둔다. 이 값이 매입제외 확정에
직접 쓰이므로 모호한 것을 비해당으로 넘기면 곧 오판정이 된다.

serviceKey 이중 인코딩 함정: 참고 구현(R8_AptReport/server.py)은 serviceKey 를 URL
인코딩 없이 끝에 그대로 붙였다. data.go.kr 키가 이미 인코딩된 형태일 때 httpx 의
params 인코딩과 겹쳐 SERVICE_KEY 오류가 나는 것을 피하려는 것이다. 다만 이 저장소는
`get_settings().public_data_key`(디코딩된 원문 키)를 localdata·kgs·ncmc 어댑터에서
모두 params 딕셔너리로 넘겨 정상 동작한다. 즉 이 저장소의 키는 디코딩본이라 params
경로가 맞다. 그래서 여기서도 동일 규약(params 딕셔너리)을 따른다. 만약 배선 시
활용신청 승인 뒤에도 SERVICE_KEY 오류가 나면 키가 인코딩본으로 저장된 것이므로
`_ENCODE_SERVICE_KEY = False` 로 두고 URL 말미에 직접 붙이는 경로로 바꾸면 된다.
"""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET
from enum import Enum
from typing import NamedTuple

import httpx

from app.services.http_client import shared_verify


BR_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"

# 표제부(동별) 조회. mainPurpsCdNm·etcPurps 를 주는 오퍼레이션.
TITLE_OP = "getBrTitleInfo"

# 한 필지의 동 수는 많아야 수십 건이다. 100이면 한 페이지로 끝난다.
PAGE_SIZE = 100

# 한 사업지당 인접 후보(=PNU)가 여러 건이면 호출이 그만큼 늘어난다. 동시성을 제한해
# 429(요청 과다)를 피한다. localdata 와 같은 보수적 값.
CONCURRENCY = 6

# 포털이 429 를 돌려줄 때의 재시도. kgs·localdata 와 동일 문체.
MAX_RETRIES = 5
RETRY_BASE_SECONDS = 1.5

# 건축물대장은 하루에도 몇 번씩 바뀌지 않는다. 같은 사업지 재조회·후보 중복을 위해
# PNU 단위로 캐시한다.
CACHE_TTL_SECONDS = 24 * 60 * 60

# 성공 판정 코드. "00"=정상, "03"=NODATA(해당 필지 표제부 없음). NODATA 는 장애가
# 아니라 "건물 없음"이므로 예외가 아니라 빈 결과(→ 확인불가)로 다룬다. 그 외 코드
# (SERVICE_KEY·LIMIT 등)는 조회 실패이므로 반드시 예외로 전파한다. 실패를 빈 결과로
# 삼키면 상위에서 dataset_missing 을 못 내고 오판정이 된다.
SUCCESS_CODE = "00"
NODATA_CODES = ("03",)


class BuildingRegisterAPIError(RuntimeError):
    """건축물대장 조회 실패. 빈 결과로 삼키지 말고 이 예외로 전파한다."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class UseVerdict(str, Enum):
    """용도 교차확인 3값. 모호하면 UNKNOWN 이지 NOT_APPLICABLE 이 아니다."""

    APPLICABLE = "해당"
    NOT_APPLICABLE = "비해당"
    UNKNOWN = "확인불가"


# ── PNU 분해 ──────────────────────────────────────────────────────────

# PNU 19자리 = 시군구(5) + 법정동(5) + 필지구분(1) + 본번(4) + 부번(4).
PNU_LENGTH = 19


class BrParams(NamedTuple):
    """표제부 요청 파라미터. PNU 를 분해한 결과."""

    sigungu_cd: str  # 5
    bjdong_cd: str  # 5
    plat_gb_cd: str  # "0" 대지(일반) / "1" 산
    bun: str  # 4
    ji: str  # 4

    @property
    def is_mountain(self) -> bool:
        return self.plat_gb_cd == "1"


def parse_pnu(pnu: str) -> BrParams:
    """PNU 19자리를 표제부 요청 파라미터로 분해한다.

    필지구분 코드는 PNU 11번째 자리다. 표준 PNU 규약상 '1'=일반(대지), '2'=산이며,
    건축물대장 `platGbCd` 는 '0'=대지, '1'=산이라 한 단계 매핑한다.

    길이 부족·비숫자·산여부 이상은 조용히 넘기지 않고 ValueError 로 튕긴다. PNU 가
    깨져 있으면 조회 자체가 성립하지 않으므로 상위에서 확인불가로 처리하게 둔다.
    """

    if not isinstance(pnu, str):
        raise ValueError(f"PNU 는 문자열이어야 합니다: {type(pnu).__name__}")
    value = pnu.strip()
    if len(value) != PNU_LENGTH:
        raise ValueError(f"PNU 는 19자리여야 합니다(입력 {len(value)}자리): {value!r}")
    if not value.isdigit():
        raise ValueError(f"PNU 는 숫자만 허용됩니다: {value!r}")

    parcel_gb = value[10]
    if parcel_gb == "1":
        plat_gb_cd = "0"  # 일반(대지)
    elif parcel_gb == "2":
        plat_gb_cd = "1"  # 산
    else:
        raise ValueError(f"PNU 필지구분(11번째 자리)이 1/2 가 아닙니다: {parcel_gb!r}")

    return BrParams(
        sigungu_cd=value[0:5],
        bjdong_cd=value[5:10],
        plat_gb_cd=plat_gb_cd,
        bun=value[11:15],
        ji=value[15:19],
    )


# ── 용도 판정 ─────────────────────────────────────────────────────────

# 건축법 시행령 별표1 의 표준 용도 대분류(공백 제거·정규화본). mainPurpsCdNm 이
# 이 중 하나로 읽히면 '인식된 표준 용도'로 본다. 대상 용도가 아니면서 인식되면
# '비해당'을 확정할 수 있다. 여기에 없거나 비면 '확인불가'로 둔다.
_STANDARD_USE_CATEGORIES: frozenset[str] = frozenset(
    _category.replace(" ", "")
    for _category in (
        "단독주택",
        "공동주택",
        "제1종근린생활시설",
        "제2종근린생활시설",
        "문화및집회시설",
        "종교시설",
        "판매시설",
        "운수시설",
        "의료시설",
        "교육연구시설",
        "노유자시설",
        "수련시설",
        "운동시설",
        "업무시설",
        "숙박시설",
        "위락시설",
        "공장",
        "창고시설",
        "위험물저장및처리시설",
        "자동차관련시설",
        "동물및식물관련시설",
        "자원순환관련시설",
        "교정및군사시설",
        "국방군사시설",
        "방송통신시설",
        "발전시설",
        "묘지관련시설",
        "관광휴게시설",
        "장례시설",
        "야영장시설",
    )
)

# 대상 용도 정규화 키. 판정 함수의 target.
_SECOND_CLASS_KEY = "제2종근린생활시설"
_SPORTS_KEY = "운동시설"


def _norm(value: str | None) -> str:
    """용도 문자열 정규화: 공백·구분자를 제거해 표기 흔들림을 흡수한다.

    표제부 용도는 '제2종근린생활시설', '제 2종 근린생활시설', '제2종 근린생활시설'
    처럼 공백이 들쭉날쭉하고, 복합용도는 쉼표·슬래시로 이어 붙는다. 판정은 포함
    관계로 하므로 구분자를 지우고 붙여 둔다.
    """

    if not value:
        return ""
    stripped = value.strip()
    for ch in (" ", "\t", "\n", "　"):
        stripped = stripped.replace(ch, "")
    return stripped


def _classify_use(main_purps: str | None, etc_purps: str | None, target_key: str) -> UseVerdict:
    """주용도·기타용도에서 target 용도 해당 여부를 3값으로 판정한다.

    - 해당: target 용도가 주용도 또는 기타용도에 명시됨(복합용도 포함).
    - 비해당: 주용도가 표준 용도로 인식되고 target 이 아니며, 어디에도 target 이
      명시되지 않음.
    - 확인불가: 용도 문자열이 없거나, 주용도가 표준 용도로 읽히지 않는(모호한) 경우.

    주용도(mainPurpsCdNm)를 비해당 판정의 권위 있는 값으로 삼는다. 복합용도로
    target 이 섞인 경우는 기타용도까지 포함 검사해 '해당'으로 먼저 걸러 낸다.
    (한계: 기타용도가 표준 카테고리명이 아니라 '일반음식점' 같은 세부 업종명으로만
    적혀 있으면 그 세부 업종이 제2종근생 하위인지까지는 이 어댑터가 단정하지 않는다.
    그 경우 주용도 기준으로 판정하며, 더 엄격한 세부 업종 매핑이 필요하면 배선 시
    오케스트레이터가 결정한다.)
    """

    main_n = _norm(main_purps)
    etc_n = _norm(etc_purps)

    # 용도 문자열이 아예 없으면 추정하지 않는다.
    if not main_n and not etc_n:
        return UseVerdict.UNKNOWN

    # 해당: 대상 용도가 어느 쪽에든 명시됨.
    if target_key in main_n or target_key in etc_n:
        return UseVerdict.APPLICABLE

    # 비해당: 주용도가 표준 용도로 인식되고 대상이 아님.
    if _recognizes_standard_use(main_n):
        return UseVerdict.NOT_APPLICABLE

    # 주용도가 모호(표준 용도 미인식)하면 비해당으로 단정하지 않는다.
    return UseVerdict.UNKNOWN


def _recognizes_standard_use(main_n: str) -> bool:
    """정규화된 주용도가 표준 용도 대분류 중 하나를 포함하는지."""

    return any(category in main_n for category in _STANDARD_USE_CATEGORIES)


def classify_second_class_neighborhood(
    main_purps: str | None, etc_purps: str | None
) -> UseVerdict:
    """제2종 근린생활시설 해당/비해당/확인불가(§6.4 가목·다목 AND)."""

    return _classify_use(main_purps, etc_purps, _SECOND_CLASS_KEY)


def classify_sports_facility(
    main_purps: str | None, etc_purps: str | None
) -> UseVerdict:
    """운동시설 해당/비해당/확인불가(§6.4 다목 테마파크 AND)."""

    return _classify_use(main_purps, etc_purps, _SPORTS_KEY)


def _aggregate(verdicts: list[UseVerdict]) -> UseVerdict:
    """한 필지의 동별 표제부 판정을 하나로 합친다.

    - 동이 하나라도 '해당'이면 필지 전체가 '해당'(제2종근생 건물이 섞여 있음).
    - 동이 없으면(표제부 없음) '확인불가'.
    - 모든 동이 '비해당'이면 '비해당'.
    - 하나라도 '확인불가'가 섞이면(그리고 '해당'은 없으면) '확인불가'.
    """

    if not verdicts:
        return UseVerdict.UNKNOWN
    if any(v is UseVerdict.APPLICABLE for v in verdicts):
        return UseVerdict.APPLICABLE
    if all(v is UseVerdict.NOT_APPLICABLE for v in verdicts):
        return UseVerdict.NOT_APPLICABLE
    return UseVerdict.UNKNOWN


class BuildingUse(NamedTuple):
    """표제부 동 하나의 용도."""

    dong_name: str
    main_purpose: str
    etc_purpose: str


class BuildingUseResult(NamedTuple):
    """한 PNU(필지)의 건축물대장 교차확인 결과."""

    pnu: str
    params: BrParams
    uses: tuple[BuildingUse, ...]
    second_class_neighborhood: UseVerdict
    sports_facility: UseVerdict

    @property
    def has_building(self) -> bool:
        return bool(self.uses)


def _build_result(pnu: str, params: BrParams, uses: list[BuildingUse]) -> BuildingUseResult:
    second = _aggregate(
        [classify_second_class_neighborhood(u.main_purpose, u.etc_purpose) for u in uses]
    )
    sports = _aggregate(
        [classify_sports_facility(u.main_purpose, u.etc_purpose) for u in uses]
    )
    return BuildingUseResult(
        pnu=pnu,
        params=params,
        uses=tuple(uses),
        second_class_neighborhood=second,
        sports_facility=sports,
    )


class BuildingRegisterClient:
    def __init__(
        self,
        service_key: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = CONCURRENCY,
    ) -> None:
        self.service_key = service_key
        self.timeout = timeout
        self._transport = transport
        self.concurrency = max(1, concurrency)
        self._cache: dict[str, BuildingUseResult] = {}
        self._cache_times: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.service_key)

    async def lookup(self, pnu: str) -> BuildingUseResult:
        """한 필지의 표제부 용도를 조회해 교차확인 결과로 돌려준다.

        조회 실패(네트워크·인증·파싱)는 BuildingRegisterAPIError 로 전파한다.
        표제부가 없는 필지(NODATA)는 실패가 아니라 확인불가 결과로 돌려준다.
        """

        params = parse_pnu(pnu)
        cached = self._cached(pnu)
        if cached is not None:
            return cached

        async with self._client() as client:
            uses = await self._fetch_uses(client, params)
        result = _build_result(pnu, params, uses)
        self._store(pnu, result)
        return result

    async def lookup_many(self, pnus: list[str]) -> dict[str, BuildingUseResult]:
        """여러 필지를 동시성 제한 아래 병렬 조회한다.

        한 건이라도 조회 실패하면 예외를 전파한다(빈 결과로 둔갑시키지 않는다).
        PNU 자체가 깨진 건(ValueError)은 조회가 성립하지 않으므로 함께 전파한다.
        """

        unique = list(dict.fromkeys(pnus))
        semaphore = asyncio.Semaphore(self.concurrency)

        async def one(pnu: str) -> tuple[str, BuildingUseResult]:
            async with semaphore:
                return pnu, await self.lookup(pnu)

        pairs = await asyncio.gather(*(one(pnu) for pnu in unique))
        return dict(pairs)

    def _cached(self, pnu: str) -> BuildingUseResult | None:
        result = self._cache.get(pnu)
        if result is None:
            return None
        if time.monotonic() - self._cache_times.get(pnu, 0.0) >= CACHE_TTL_SECONDS:
            self._cache.pop(pnu, None)
            self._cache_times.pop(pnu, None)
            return None
        return result

    def _store(self, pnu: str, result: BuildingUseResult) -> None:
        self._cache[pnu] = result
        self._cache_times[pnu] = time.monotonic()

    def _client(self) -> httpx.AsyncClient:
        # 참고 구현이 붙인 User-Agent 를 그대로 둔다. 포털이 기본 UA 를 막는
        # 경우가 있어 curl 흉내를 낸다.
        headers = {"User-Agent": "curl/8.0", "Accept": "*/*"}
        return httpx.AsyncClient(
            timeout=self.timeout,
            transport=self._transport,
            headers=headers,
            verify=shared_verify(),
        )

    async def _fetch_uses(
        self, client: httpx.AsyncClient, params: BrParams
    ) -> list[BuildingUse]:
        query = {
            "serviceKey": self.service_key,
            "sigunguCd": params.sigungu_cd,
            "bjdongCd": params.bjdong_cd,
            "platGbCd": params.plat_gb_cd,
            "bun": params.bun,
            "ji": params.ji,
            "numOfRows": str(PAGE_SIZE),
            "pageNo": "1",
        }
        url = f"{BR_BASE}/{TITLE_OP}"
        response = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await client.get(url, params=query)
            except httpx.HTTPError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise BuildingRegisterAPIError(
                        f"건축물대장 표제부 조회에 실패했습니다: {exc}"
                    ) from exc
                await asyncio.sleep(RETRY_BASE_SECONDS * (2**attempt))
                continue
            # 429 만 재시도로 풀린다. 그 외 상태는 재시도해도 같다.
            if response.status_code != 429:
                break
            if attempt == MAX_RETRIES - 1:
                raise BuildingRegisterAPIError(
                    "건축물대장 요청이 과다합니다. 잠시 뒤 다시 시도하세요.", 429
                )
            await asyncio.sleep(RETRY_BASE_SECONDS * (2**attempt))

        if response is None or response.status_code != 200:
            raise BuildingRegisterAPIError(
                "건축물대장 표제부 응답 오류 "
                f"({response.status_code if response else '응답 없음'})",
                response.status_code if response else None,
            )
        return _parse_title_xml(response.text)


def _parse_title_xml(text: str) -> list[BuildingUse]:
    """표제부 XML 을 동별 용도 목록으로 파싱한다.

    resultCode 00 은 정상, NODATA(03)는 표제부 없음(→ 빈 목록). 그 외 코드는 조회
    실패이므로 예외로 전파한다.
    """

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise BuildingRegisterAPIError(
            f"건축물대장 표제부 응답을 해석하지 못했습니다: {exc}"
        ) from exc

    code = (root.findtext(".//resultCode") or "").strip()
    if code in NODATA_CODES:
        return []
    if code != SUCCESS_CODE:
        msg = (root.findtext(".//resultMsg") or "").strip()
        raise BuildingRegisterAPIError(
            f"건축물대장 표제부 오류: {msg or code or '알 수 없음'}"
        )

    uses: list[BuildingUse] = []
    for item in root.findall(".//item"):
        main = (item.findtext("mainPurpsCdNm") or "").strip()
        etc = (item.findtext("etcPurps") or "").strip()
        dong = (item.findtext("dongNm") or "").strip()
        # 주용도·기타용도가 모두 비면 판정에 쓸모없는 행이라 건너뛴다.
        if not main and not etc:
            continue
        uses.append(BuildingUse(dong_name=dong, main_purpose=main, etc_purpose=etc))
    return uses
