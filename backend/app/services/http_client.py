"""어댑터 전체가 공유하는 SSL 컨텍스트.

실측(cProfile, 판정 1회): `httpx.AsyncClient.__init__` 이 143회 호출되며 176.05초를
쓰는데 그중 175.79초가 `httpx._config.create_ssl_context()` 다(전체 298.1초의 59%).
16개 어댑터가 요청마다 `httpx.AsyncClient` 를 새로 만들고 닫아서, `verify=True`
기본값이 매번 `ssl.create_default_context(cafile=certifi.where())` 를 다시 실행해
인증서 번들을 읽고 파싱한다(1회 약 1.23초). 실제 네트워크 통신은 7.5초뿐이었다.

인증서 번들은 프로세스 수명 동안 바뀌지 않으므로 컨텍스트를 한 번만 만들어
`AsyncClient(verify=...)` 에 공유한다. `httpx._config.create_ssl_context` 는
`verify` 가 이미 `ssl.SSLContext` 인스턴스면 그대로 반환하므로(재생성하지 않음),
이 값을 넘기면 176초가 거의 사라진다.

클라이언트 재사용(커넥션 풀링)까지는 하지 않았다 — 16개 어댑터마다 이벤트 루프
결합·수명 관리를 개별로 검증해야 하는 위험이 있고, SSL 컨텍스트 공유만으로 병목의
대부분(176초/298초)이 사라지므로 이번 수정 범위를 여기로 제한한다.

테스트에서 `transport=httpx.MockTransport(...)` 를 넘기는 경로는 안전하다.
`httpx.AsyncClient._init_transport()` 는 `transport` 가 주어지면 `verify` 를
아예 쓰지 않고 그 transport 를 그대로 쓴다(SSL 컨텍스트도 만들지 않는다).
"""

from __future__ import annotations

import ssl
from functools import lru_cache

import httpx


@lru_cache(maxsize=1)
def shared_verify() -> ssl.SSLContext:
    """프로세스당 한 번만 만드는 기본 SSL 컨텍스트.

    `lru_cache` 가 최초 호출 결과를 캐시하므로 몇 번을 호출하든
    `httpx.create_ssl_context()` 는 1회만 실행된다.
    """

    return httpx.create_ssl_context()
