from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    kakao_rest_api_key: str = ""
    tago_service_key: str = ""
    public_data_service_key: str = ""
    naver_search_client_id: str = ""
    naver_search_client_secret: str = ""
    vworld_api_key: str = ""
    opinet_api_key: str = ""
    safemap_api_key: str = ""
    # 서울 열린데이터광장 인증키 — 서울 버스정류소(TAGO 미제공 지역).
    seoul_open_data_key: str = ""
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
