"""로컬 전용 설정 저장소.

화면에서 넣은 서버 비밀키를 `backend/.env` 에 보존하고, 실행 중인 프로세스의
`os.environ` 에도 반영해 재시작 없이 즉시 적용되게 한다.

주의: 여기 있는 값은 서버 비밀키다. 로그·응답에 원문을 남기지 않는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# app/settings_api/store.py -> parents[2] == backend/
BACKEND_DIR = Path(__file__).resolve().parents[2]

# 테스트가 임시 파일로 바꿔 끼울 수 있도록 모듈 전역으로 둔다.
ENV_PATH = BACKEND_DIR / ".env"
ENV_EXAMPLE_PATH = BACKEND_DIR / ".env.example"


@dataclass(frozen=True)
class KeySpec:
    """서버 비밀키 한 개의 메타데이터."""

    key: str  # .env 에 쓰이는 환경변수 이름
    label: str
    description: str = ""
    # 발급처 이름·URL. 사용자가 직접 준 값만 채운다. 없으면 빈 문자열로 두어
    # 화면에 발급처 링크를 렌더하지 않는다(추측 URL 금지).
    issuer_name: str = ""
    issuer_url: str = ""


# 화면에 노출할 서버 비밀키 목록. 순서가 화면 순서가 된다.
SERVER_KEY_SPECS: list[KeySpec] = [
    KeySpec(
        "KAKAO_REST_API_KEY",
        "카카오 REST API 키",
        "주소 검색·좌표 변환에 씁니다.",
        issuer_name="카카오 개발자 콘솔",
        issuer_url="https://developers.kakao.com/console/app",
    ),
    KeySpec(
        "TAGO_SERVICE_KEY",
        "TAGO 국토교통 대중교통 서비스 키",
        "2차 배점의 버스정류장 조회에 씁니다.",
        issuer_name="공공데이터포털",
        issuer_url="https://www.data.go.kr/iim/main/mypageMain.do",
    ),
    KeySpec(
        "PUBLIC_DATA_SERVICE_KEY",
        "공공데이터포털 서비스 키",
        "비우면 TAGO 키를 공통 키로 재사용합니다.",
        issuer_name="공공데이터포털",
        issuer_url="https://www.data.go.kr/iim/main/mypageMain.do",
    ),
    KeySpec(
        "NAVER_SEARCH_CLIENT_ID",
        "네이버 검색 Client ID",
        "대학·종합병원 정문과 역 출구 후보 조회에 씁니다.",
        issuer_name="네이버 개발자센터",
        issuer_url="https://developers.naver.com/apps/#/list",
    ),
    KeySpec(
        "NAVER_SEARCH_CLIENT_SECRET",
        "네이버 검색 Client Secret",
        "대학·종합병원 정문과 역 출구 후보 조회에 씁니다.",
        issuer_name="네이버 개발자센터",
        issuer_url="https://developers.naver.com/apps/#/list",
    ),
    KeySpec(
        "VWORLD_API_KEY",
        "브이월드 API 키",
        "지적도 필지 경계 조회에 씁니다.",
        issuer_name="브이월드",
        issuer_url="https://www.vworld.kr/mypo/mypo_apiKey_e001.do?apiIde=APIID_00000003125724",
    ),
    KeySpec(
        "OPINET_API_KEY",
        "오피넷 API 키",
        "주유소·충전소 유해요소 조회에 씁니다.",
        issuer_name="오피넷",
        issuer_url="https://www.opinet.co.kr/",
    ),
    KeySpec(
        "SAFEMAP_API_KEY",
        "생활안전지도 API 키",
        "전국 주유소·LPG충전소 유해요소 조회에 씁니다.",
        issuer_name="생활안전지도",
        issuer_url="https://www.safemap.go.kr/",
    ),
]

DEMO_MODE_KEY = "DEMO_MODE"


def mask_hint(value: str) -> str:
    """원문 대신 화면에 보여줄 마스킹 힌트를 만든다.

    끝 4글자만 남기고 나머지는 별로 가린다. 원문 길이도 최소화해 노출하지 않는다.
    """

    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return f"****{value[-4:]}"


def _env_value(value: str) -> str:
    """`.env` 한 줄에 안전하게 넣을 값 문자열을 만든다.

    개행·제어문자가 든 값은 다른 KEY 를 주입할 수 있으므로 거부한다(라우터에서
    이미 막지만, 저장소 단에서도 방어한다). 공백·`#`·따옴표가 있으면 따옴표로
    감싸 한 줄을 벗어나지 못하게 한다.
    """

    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise ValueError("환경변수 값에는 줄바꿈·제어문자를 넣을 수 없습니다.")
    if value == "" or not any(ch in value for ch in (" ", "#", '"', "'", "\t")):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _line_key(line: str) -> str | None:
    """`.env` 한 줄에서 키 이름을 뽑는다. 주석·빈 줄·비KEY 줄은 None."""

    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    left = stripped.split("=", 1)[0].strip()
    if left.startswith("export "):
        left = left[len("export ") :].strip()
    return left or None


def _seed_lines() -> list[str]:
    """`.env` 가 없을 때 채울 초기 줄. 예시 파일이 있으면 그 골격을 쓴다."""

    if ENV_EXAMPLE_PATH.exists():
        text = ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
        return text.splitlines()
    return []


def write_env(final_values: dict[str, str]) -> None:
    """`.env` 를 기존 주석·다른 키를 보존하며 갱신한다.

    `final_values` 는 KEY -> 최종 문자열 값. 삭제는 빈 문자열로 표현한다.
    파일 전체를 덮어쓰지 않고 해당 줄만 바꾸며, 없는 키는 끝에 덧붙인다.
    """

    if ENV_PATH.exists():
        raw = ENV_PATH.read_text(encoding="utf-8")
        lines = raw.splitlines()
        trailing_newline = raw.endswith("\n")
    else:
        lines = _seed_lines()
        trailing_newline = True

    remaining = dict(final_values)
    new_lines: list[str] = []
    for line in lines:
        key = _line_key(line)
        if key is not None and key in remaining:
            new_lines.append(f"{key}={_env_value(remaining.pop(key))}")
        else:
            new_lines.append(line)

    for key, value in remaining.items():
        new_lines.append(f"{key}={_env_value(value)}")

    output = "\n".join(new_lines)
    if trailing_newline or not output:
        output += "\n"
    ENV_PATH.write_text(output, encoding="utf-8")


# `.env` 에만 적혀 있으면 안 되고 프로세스 환경변수로도 올라가야 하는 키.
# Settings 필드가 아니라 os.environ 에서 직접 읽히는 값들이다(파일 경로뿐이며
# 비밀키는 하나도 없다 — 비밀키는 Settings 를 통해서만 흐른다).
PROCESS_ENV_KEYS: tuple[str, ...] = (
    "LH_LOCAL_STANDARD_PATH",
    "LH_LOCAL_RAW_PATH",
    "LH_SOURCE_DIR",
    "LH_LEGAL_DONG_PATH",
    "NOISE_EMISSION_CSV_PATH",
    "CADASTRAL_JEONBUK_SHP",
    "CADASTRAL_JEONBUK_DB",
)


def hydrate_process_env() -> tuple[str, ...]:
    """`.env` 의 경로 키를 프로세스 환경변수로 올린다.

    왜 필요한가
    -----------
    pydantic Settings 는 `.env` 를 자기 필드로만 읽고 `os.environ` 에는 반영하지
    않는다. 그런데 로컬 원천 배선(`LocalSourcesConfig.from_env`)은 `os.environ`
    에서 `LH_LOCAL_RAW_PATH`·`LH_LOCAL_STANDARD_PATH` 를 직접 읽는다. 그래서
    서버를 셸에서 그냥 띄우면 `.env` 에 경로가 멀쩡히 적혀 있어도 원천이 하나도
    안 붙고, 화면에는 「factoryON 등록공장 원천 미적재」처럼 데이터가 없는 것처럼
    보인다. 실제로는 읽지 않았을 뿐이다.

    옮기는 키는 `PROCESS_ENV_KEYS` 로 한정한다. 비밀키까지 환경변수로 퍼뜨릴
    이유가 없고, 테스트가 키를 빈 값으로 눌러 둔 것을 덮어써서도 안 된다.
    이미 들어 있는 키는 값이 빈 문자열이어도 건드리지 않는다 — 빈 값으로
    설정한 것 자체가 「쓰지 말라」는 뜻이다.

    돌려주는 값: 이번 호출에서 실제로 채운 키 이름들.
    """

    try:
        text = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return ()

    filled: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in PROCESS_ENV_KEYS and value and key not in os.environ:
            os.environ[key] = value
            filled.append(key)
    return tuple(filled)


def apply_updates(updates: dict[str, str | None]) -> None:
    """`.env` 와 현재 프로세스 환경변수를 함께 갱신한다.

    updates: KEY -> 새 값. None 은 명시적 삭제.
    환경변수는 `.env` 파일보다 우선순위가 높아, 여기서 함께 손대야 재시작 없이
    `get_settings()` 에 반영된다.
    """

    final_values: dict[str, str] = {}
    for key, value in updates.items():
        if value is None:
            final_values[key] = ""
            os.environ.pop(key, None)
        else:
            # os.environ 에 넣기 전에 먼저 검증한다. 개행·제어문자면 여기서 막아
            # 프로세스 환경변수도 오염되지 않게 한다.
            _env_value(value)
            final_values[key] = value
            os.environ[key] = value

    if final_values:
        write_env(final_values)
