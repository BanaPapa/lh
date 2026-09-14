"""이벤트 루프에 안전한 single-flight 잠금.

전국 목록을 한 번에 받아 캐시하는 원천 클라이언트들(kgs·safemap·crematorium·
noise_emission)이 공유하는 패턴이다. 동시 호출이 여럿이어도 실제 원격 조회는 한
번만 일어나게 하고, 나머지는 그 결과를 기다린다. 잠금이 없으면 판정 여러 건이
동시에 빈 캐시를 보고 각자 전국 목록을 따로 긁어(캐시 스탬피드) data.go.kr 이
스로틀하고, 조회 실패가 정당하게 「검토 필요」로 떨어져 스냅샷이 오염된다.

주의: `get_hazard_service` 가 `lru_cache` 로 클라이언트를 싱글턴처럼 잡아 두어 한
인스턴스가 여러 이벤트 루프에 걸쳐 산다(요청마다 새 루프인 테스트 등). `asyncio.Lock`
은 만들어진 루프에 묶이므로 `__init__` 에서 만들면 루프가 바뀔 때 터진다. 그래서
락을 현재 러닝 루프에 지연 생성하고, 루프가 바뀌면 새로 만든다.
"""

from __future__ import annotations

import asyncio


class LoopSafeLock:
    """현재 러닝 루프에 지연 생성되는 asyncio 잠금.

    같은 루프 안에서는 같은 잠금을 돌려주고, 루프가 바뀌면 그 루프용 잠금을 새로
    만든다. 이렇게 해야 클라이언트를 루프 간에 재사용해도 「다른 루프에 묶인 락」
    오류가 나지 않는다.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def get(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._lock = asyncio.Lock()
            self._loop = loop
        return self._lock
