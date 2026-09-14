"""유해요소 판정 서비스 조립을 앱(router)과 대조 도구(tools/lh_baseline)가 공유한다.

배경: 대조 도구 run_ours.py 가 서비스를 자체 조립하면서 실제 앱이 넘기는 인자
7종(cadastral·local_sources·pnu_resolver·safemap·crematorium·noise_emission·
building_register)을 빠뜨려, 공장·화장장·소음·생활안전지도·시설경계 폴백이 통째로
빠진 스냅샷이 나오는 사고가 났다. 조립 경로가 두 벌이면 이 사고가 재발한다. 그래서
서비스 조립을 이 모듈 한 곳으로 모으고, 앱과 도구가 같은 함수를 쓴다.

router 는 `get_hazard_service()`(lru_cache + FastAPI 의존성)로 감싸 첫 요청에서
백그라운드 워밍업으로 로컬 원천 묶음을 채운다. CLI 는 백그라운드로 미룰 이유가
없으므로 `build_hazard_service()` 로 서비스와 loader 를 받아, 판정 시작 전에 loader
를 동기로 돌려 완전한 묶음을 주입한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.config import Settings
from app.hazard_review.service import HazardReviewService
from app.models import Coordinates
from app.services.address_pnu_kakao import KakaoAddressPnu
from app.services.building_register import BuildingRegisterClient
from app.services.cadastral_local import CadastralLocalStore
from app.services.cadastral_vworld import VWorldCadastralStore
from app.services.cng import CngStationClient
from app.services.crematorium import CrematoriumClient
from app.services.facility_store import FacilityStore
from app.services.kakao import KakaoClient
from app.services.kgs import KgsLpgClient
from app.services.local_wiring import (
    LocalSourcesBundle,
    build_local_sources_bundle,
)
from app.services.noise_emission import NoiseEmissionClient
from app.services.opinet import OpinetClient
from app.services.pnu_resolver import (
    LegalDongIndex,
    PnuResolver,
    build_pnu_resolver,
)
from app.services.safemap import SafemapFuelClient
from app.services.vworld import VWorldClient


logger = logging.getLogger(__name__)

# 완전한 로컬 원천 묶음(factoryON 원본 PNU 확정 포함)을 계산하는 클로저 타입.
LocalSourcesLoader = Callable[[], LocalSourcesBundle]


def build_local_sources_loader(
    legal_dong: LegalDongIndex | None,
    legal_dong_path: str | None,
    cadastral_db_path,
    *,
    vworld_api_key: str = "",
    vworld_domain: str = "localhost",
    kakao_rest_api_key: str = "",
) -> LocalSourcesLoader:
    """factoryON 원본 PNU 확정을 포함한 완전한 로컬 원천 묶음을 계산하는 클로저.

    확정 계산은 shapely 를 쓰는 무거운 CPU 작업이라 이벤트 루프가 아니라 스레드풀
    (run_in_executor)에서 돌린다. 스레드에서 지적도 SQLite 를 안전하게 쓰려면 그
    스레드 안에서 만든 연결을 써야 하므로(서비스의 요청 시점 resolver 와 연결을
    공유하지 않는다), 워밍업 전용 CadastralLocalStore 를 새로 만든다. 워밍업
    resolver 는 geocoder=None 으로 둔다 — 확정은 결정적이어야 캐시 재현성이
    보장되고, 동기 경로에서 async 지오코더를 부를 수도 없기 때문이다.

    지적도·법정동 원천의 선택 (2026-09-08)
    --------------------------------------
    이 앱은 납품본(정적 데이터셋)을 검사하는 교차검증 경로다. 검사 대상이 쓰는
    파일(인계본 동봉 연속지적도·법정동코드표)을 빌려 오면 검증이 성립하지 않는다.
    그래서 로컬 색인이 없으면 **API 로 대체**한다.

      · 지적도  CadastralLocalStore(색인 있음) → 없으면 VWorldCadastralStore
                (LP_PA_CBND_BUBUN — 같은 국토부 연속지적도이며 현행)
      · 조립    LegalDongIndex(표 있음) → 없거나 실패하면 카카오 주소검색
                (주소별 b_code 를 직접 받으므로 표가 필요 없다)

    둘 다 없으면 종전처럼 확정하지 않는다(빈 결과로 삼키지 않는다).
    """

    def load() -> LocalSourcesBundle:
        warm_cadastral = CadastralLocalStore(db_path=cadastral_db_path)
        cadastral_backend = warm_cadastral
        remote_cadastral: VWorldCadastralStore | None = None
        address_pnu: KakaoAddressPnu | None = None

        if not warm_cadastral.status().available and vworld_api_key:
            remote_cadastral = VWorldCadastralStore(
                vworld_api_key, domain=vworld_domain
            )
            cadastral_backend = remote_cadastral
        if kakao_rest_api_key:
            address_pnu = KakaoAddressPnu(kakao_rest_api_key)

        warm_resolver = PnuResolver(
            legal_dong=legal_dong,
            cadastral=cadastral_backend,
            geocoder=None,
            address_pnu=address_pnu,
        )
        try:
            bundle = build_local_sources_bundle(
                resolver=warm_resolver,
                legal_dong_path=legal_dong_path,
                cadastral_db_path=cadastral_db_path,
            )
        finally:
            warm_cadastral.close()
            if remote_cadastral is not None:
                logger.info("PNU 확정 지적도(원격) — %s", remote_cadastral.stats.summary())
                remote_cadastral.close()
            if address_pnu is not None:
                logger.info("PNU 확정 주소조립 — %s", address_pnu.stats.summary())
                address_pnu.close()
        return bundle

    return load


def build_hazard_service(
    config: Settings,
) -> tuple[HazardReviewService, LocalSourcesLoader]:
    """실제 앱과 동일한 인자로 HazardReviewService 를 조립한다.

    반환: (서비스, 로컬 원천 묶음 loader). 서비스는 local_sources 를 빈 묶음으로
    시작한다. 완전한 묶음(factoryON 원본 PNU 확정 포함)은 loader 를 돌려 얻는다.
    router 는 loader 를 백그라운드 워밍업으로, CLI 는 판정 시작 전 동기로 돌린다.
    """

    kakao = KakaoClient(config.kakao_rest_api_key)

    async def geocode_address(address: str) -> Coordinates | None:
        """화장시설 주소 → 좌표. 화장시설 API 는 좌표를 주지 않아 필요하다."""

        if not kakao.enabled:
            return None
        try:
            candidates = await kakao.geocode(address)
        except Exception:
            return None
        return candidates[0].coordinates if candidates else None

    # 전북 연속지적도 로컬 인덱스. 인덱스가 없으면 status().available=False 라
    # 시설 필지 폴백에서 조용히 빠진다(다른 PC 엔 원본 없음). PNU 확정기와 서비스가
    # 같은 인덱스를 공유한다(읽기전용 · 2.25GB 연결 중복을 피한다).
    cadastral = CadastralLocalStore()
    # PNU 확정 파이프라인(설계서 §7.2). 법정동표(config.legal_dong_path)+연속지적도로
    # factoryON 원본·소음배출시설의 PNU 를 확정한다. 법정동표·인덱스가 없으면
    # ready=False 라 확정을 건너뛰고 배선 전과 동일하게 동작한다.
    legal_dong = (
        LegalDongIndex.from_xlsx(config.legal_dong_path)
        if config.legal_dong_path
        else None
    )
    # 요청 시점 PNU 확정(소음배출시설 라목)의 지적도·조립 원천.
    # 로컬 색인이 없으면 API 로 대체한다 — 이 앱은 납품본이 쓰는 파일을 빌리지 않고
    # 독립 원천으로 같은 판정을 내는 것이 존재 이유다(§ build_local_sources_loader).
    # 요청 경로에서 동기 HTTP 를 부르지만, 대상은 사업지 50m 안 소음배출시설 후보뿐
    # (실측 0~3건)이고 결과가 프로세스 메모에 남아 같은 필지를 다시 묻지 않는다.
    runtime_cadastral = (
        cadastral
        if cadastral.status().available or not config.vworld_api_key
        else VWorldCadastralStore(config.vworld_api_key, domain=config.vworld_domain)
    )
    pnu_resolver = build_pnu_resolver(
        cadastral=runtime_cadastral,
        geocoder=geocode_address,
        legal_dong=legal_dong,
        address_pnu=(
            KakaoAddressPnu(config.kakao_rest_api_key)
            if config.kakao_rest_api_key
            else None
        ),
    )

    service = HazardReviewService(
        kakao=kakao,
        demo_mode=config.demo_mode,
        opinet=OpinetClient(config.opinet_api_key),
        kgs_lpg=KgsLpgClient(config.public_data_key),
        # 가스안전공사 CNG 충전소(ODcloud 15001508). 같은 공공데이터포털 키를 쓰되
        # 데이터셋 활용신청 전에는 401 이라 조회 실패로 기록된다(로컬 CSV 가 보조).
        cng=CngStationClient(config.public_data_key),
        facility_store=FacilityStore(),
        vworld=VWorldClient(config.vworld_api_key, domain=config.vworld_domain),
        safemap=SafemapFuelClient(config.safemap_api_key),
        crematorium=CrematoriumClient(
            config.public_data_key, geocode=geocode_address
        ),
        cadastral=cadastral,
        # factoryON PNU 집합·표준본 공장·CNG 등. 여기서는 빈 묶음으로 시작한다.
        # 완전한 묶음(factoryON 원본 PNU 확정 포함)은 loader 가 계산해 교체한다.
        local_sources=LocalSourcesBundle(),
        # 건축물대장 표제부 교차확인(§6.4 단란주점·테마파크 AND). 키가 없으면
        # enabled=False 라 review_required 로 남는다.
        building_register=BuildingRegisterClient(config.public_data_key),
        # 소음진동배출시설. LH 확정 2026-09-11 이후 판정 근거가 아니라 등록공장
        # 후보에 「소음배출 신고 있음」을 덧붙이는 부가 정보(주석) 전용이다. API
        # 미승인(403)·CSV 미주입이면 enabled=False 라 주석을 달지 않을 뿐이다.
        noise_emission=NoiseEmissionClient(
            config.public_data_key,
            csv_path=config.noise_emission_csv_path or None,
        ),
        # 소음배출시설(PNU 미보유) 후보에 요청 시점 PNU 확정 → 공장 PNU 대조(라목).
        pnu_resolver=pnu_resolver,
    )

    loader = build_local_sources_loader(
        legal_dong=legal_dong,
        legal_dong_path=config.legal_dong_path,
        cadastral_db_path=cadastral.db_path,
        vworld_api_key=config.vworld_api_key,
        vworld_domain=config.vworld_domain,
        kakao_rest_api_key=config.kakao_rest_api_key,
    )
    return service, loader
