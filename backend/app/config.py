from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    kakao_rest_api_key: str = ""
    tago_service_key: str = ""
    public_data_service_key: str = ""
    naver_search_client_id: str = ""
    naver_search_client_secret: str = ""
    # 네이버 클라우드 플랫폼(NCP) Maps Geocoding — 카카오 주소검색이 막힐 때(일일 쿼터 초과 등)의 대체.
    naver_map_client_id: str = ""
    naver_map_client_secret: str = ""
    vworld_api_key: str = ""
    opinet_api_key: str = ""
    safemap_api_key: str = ""
    # 서울 열린데이터광장 인증키 — 서울 버스정류소(TAGO 미제공 지역).
    seoul_open_data_key: str = ""
    # 경기데이터드림 인증키 — 경기 유해화학물질 취급사업장(마목 참고 핀). data.gg.go.kr 발급.
    gg_open_api_key: str = ""
    # 전국 소음진동배출시설 표준데이터 CSV 보조 경로. 비밀키가 아니라 파일 경로라
    # 서버 키 목록(settings_api/store.py)에는 넣지 않는다. API 미승인(403) 상태에서
    # 전북 CSV(08_noise_vibration_facilities.csv)를 주입해 라목을 보조 동작시킬 때 쓴다.
    noise_emission_csv_path: str = ""
    # 행정안전부 법정동코드 xlsx 경로. PNU 조립(설계서 §7.2 ②)의 법정동명→코드
    # 색인 원천. 비밀키가 아니라 파일 경로라 서버 키 목록에는 넣지 않는다. 미설정이면
    # PnuResolver 가 조립을 건너뛰고(입력 PNU·좌표 공간조인 경로는 유지) 배선 전과
    # 동일하게 동작한다.
    legal_dong_path: str = ""
    vworld_domain: str = "localhost"
    demo_mode: bool = True
    allowed_origins: str = "http://localhost,http://localhost:5180,http://127.0.0.1:5180"
    cache_ttl_seconds: int = 600
    # --- 배포(Cloud Run) 설정. 기본값은 로컬 동작 그대로다. -----------------------
    # 기동 시 하루 지난 인허가 원장을 백그라운드로 다시 받을지. Cloud Run 은 서버가
    # 수시로 새로 켜지고 디스크가 임시라, 켤 때마다 58MB 를 다시 받게 된다. 배포에서는
    # 끄고 이미지를 만들 때 최신 원장을 구워 넣는다.
    facility_sync_on_startup: bool = True
    # 사본 갱신 빌드가 「업데이트 중」 상태 파일을 두는 버킷(app/maintenance.py). 비면 확인하지 않는다.
    snapshot_bucket: str = ""
    # 이 시각(ISO)까지 「업데이트 중」으로 본다 — 로컬 시험·수동 점검용.
    maintenance_until: str = ""
    # 누구나 접속하는 배포에서 공공 API 쿼터를 지키는 접속자(IP)별 심사 시작 제한.
    # 0 이면 제한하지 않는다(로컬 기본).
    rate_limit_per_minute: int = 0
    rate_limit_per_day: int = 0
    # 일괄 심사 한 번에 올릴 수 있는 행 수 상한. 배포에서는 작게 둔다.
    batch_max_rows: int = 500

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def public_data_key(self) -> str:
        """Use a dedicated public-data key when supplied, otherwise reuse TAGO's key."""

        return self.public_data_service_key or self.tago_service_key

    @property
    def naver_search_configured(self) -> bool:
        return bool(self.naver_search_client_id and self.naver_search_client_secret)

    @property
    def naver_geocode_configured(self) -> bool:
        return bool(self.naver_map_client_id and self.naver_map_client_secret)


@lru_cache
def get_settings() -> Settings:
    # `.env` 경로는 store.ENV_PATH 를 단일 출처로 삼는다. 프로덕션에서는
    # backend/.env 를 가리키고, 테스트는 store.ENV_PATH 를 임시 파일로 바꿔
    # 실제 backend/.env 에 손대지 않고 격리한다.
    # 지연 임포트로 순환 참조를 피한다.
    from app.settings_api import store

    # `.env` 의 값을 프로세스 환경변수로도 올린다. 로컬 원천 배선은 Settings 가
    # 아니라 os.environ 을 직접 읽어서, 이걸 빼면 경로가 적혀 있어도 원천이
    # 하나도 안 붙는다(자세한 이유는 store.hydrate_process_env 참고).
    store.hydrate_process_env()

    return Settings(_env_file=str(store.ENV_PATH))
