"""SSL 컨텍스트가 프로세스당 한 번만 만들어지는지 검증한다.

실측(cProfile, 판정 1회): `httpx.AsyncClient.__init__` 이 143번 불리며 176.05초를
쓰는데 그중 175.79초가 `httpx._config.create_ssl_context()` 다(전체 298.1초의
59%). 16개 어댑터가 요청마다 `httpx.AsyncClient()` 를 새로 만들고 닫아서
`verify=True` 기본값이 매번 `ssl.create_default_context(cafile=certifi.where())`
를 다시 실행했기 때문이다(1회 약 1.23초).

`app.services.http_client.shared_verify()` 로 컨텍스트를 한 번만 만들어 모든
어댑터에 공유하도록 고쳤다. 이 테스트가 그 수정이 되돌아가는 것을 잡는다.
"""

from __future__ import annotations

import ast
import pathlib
import ssl

import httpx
import pytest

from app.services import http_client as http_client_module
from app.services.http_client import shared_verify


SERVICES_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "services"


@pytest.fixture(autouse=True)
def _reset_shared_verify_cache():
    """각 테스트가 캐시 상태에 서로 영향을 주지 않도록 초기화한다."""

    shared_verify.cache_clear()
    yield
    shared_verify.cache_clear()


def test_shared_verify_creates_ssl_context_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """몇 번을 호출하든 `httpx.create_ssl_context()` 는 1회만 실행된다."""

    calls = 0
    real_create_ssl_context = httpx.create_ssl_context

    def counting(*args: object, **kwargs: object) -> ssl.SSLContext:
        nonlocal calls
        calls += 1
        return real_create_ssl_context(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(http_client_module.httpx, "create_ssl_context", counting)

    for _ in range(20):
        shared_verify()

    assert calls == 1


def test_shared_verify_returns_the_same_context_instance() -> None:
    first = shared_verify()
    second = shared_verify()
    assert first is second


def test_reusing_shared_context_skips_expensive_cert_bundle_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실제 병목이었던 `ssl.create_default_context(cafile=...)` 재실행을 잡는다.

    `verify=True`(기본값)로 `httpx.AsyncClient` 를 만들 때마다 인증서 번들을
    다시 읽어 파싱하던 것이 병목의 실체였다. 공유 컨텍스트를 넘기면 httpx 가
    이미 `ssl.SSLContext` 인 값을 그대로 쓰므로 이 호출이 다시 일어나지 않는다.
    """

    calls = 0
    real_create_default_context = ssl.create_default_context

    def counting(*args: object, **kwargs: object) -> ssl.SSLContext:
        nonlocal calls
        calls += 1
        return real_create_default_context(*args, **kwargs)

    monkeypatch.setattr(ssl, "create_default_context", counting)

    context = shared_verify()
    calls_after_warmup = calls  # shared_verify() 자체가 1회 소비했을 수 있다.

    # 16개 어댑터가 요청마다 하던 것과 동일하게, 공유 컨텍스트로 클라이언트를
    # 반복해서 만든다(실제 네트워크 연결은 만들지 않는다 — 생성 시점에만 비용이 든다).
    for _ in range(10):
        client = httpx.AsyncClient(verify=context)
        del client

    assert calls == calls_after_warmup


def _asyncclient_calls_missing_verify(path: pathlib.Path) -> list[int]:
    """`httpx.AsyncClient(...)` 호출 중 `verify=` 가 빠진 줄 번호."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    missing: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_asyncclient = (
            isinstance(func, ast.Attribute) and func.attr == "AsyncClient"
        ) or (isinstance(func, ast.Name) and func.id == "AsyncClient")
        if not is_asyncclient:
            continue
        has_verify = any(
            keyword.arg == "verify" or keyword.arg is None  # None = **kwargs 전달
            for keyword in node.keywords
        )
        if not has_verify:
            missing.append(node.lineno)
    return missing


def test_every_asyncclient_construction_shares_the_ssl_context() -> None:
    """`app/services/` 의 모든 `httpx.AsyncClient(...)` 생성에 `verify=` 가 있어야 한다.

    새 어댑터를 추가하면서 `verify=shared_verify()` 를 빠뜨리면, 그 어댑터 하나가
    다시 호출마다 SSL 컨텍스트를 새로 만들어 병목을 되살린다. 정적으로 잡는다.
    """

    offenders: dict[str, list[int]] = {}
    for path in sorted(SERVICES_DIR.glob("*.py")):
        missing_lines = _asyncclient_calls_missing_verify(path)
        if missing_lines:
            offenders[path.name] = missing_lines

    assert not offenders, (
        "httpx.AsyncClient(...) 생성에 verify=shared_verify() 가 빠졌다 — "
        f"{offenders}. SSL 컨텍스트를 공유하지 않으면 판정 1회당 176초가 되돌아온다."
    )
