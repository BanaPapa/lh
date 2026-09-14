"""지번주소 → PNU 조립을 카카오 주소검색 API 로 수행한다.

왜 법정동코드표(xlsx)를 쓰지 않는가
-----------------------------------
`LegalDongIndex` 는 행정안전부 법정동코드표 파일을 읽어 주소 문자열에서 최장 일치로
법정동을 고른다. 파일이 없으면 조립이 전건 실패한다(그래서 공장 나·다목 판정이 죽었다).

카카오 주소검색(`/v2/local/search/address.json`)은 주소 하나마다 **법정동코드
(b_code, 10자리)와 본번·부번·산여부를 직접** 돌려준다. 표를 들고 다닐 필요가 없고,
문자열 최장 일치보다 정확하다(「전주시 완산구 효자동2가」처럼 동명이 겹치거나 리
단위가 붙는 주소에서 특히).

「박진주 대표님 데이터를 제외한 나머지는 전부 API 로 확보한다」는 이 저장소의
원칙에도 이 경로가 맞다.

동기인 이유
-----------
`PnuResolver` 가 동기이고, 이 조립은 요청 경로가 아니라 기동 워밍업에서만 돈다
(결과는 `pnu_resolution_cache` 에 남아 다음 기동에서 재사용된다).

실패는 None
-----------
조회 실패·미발견은 None 이다. 상위는 조립 불가로 보고 실패 사유를 남기므로,
잘못된 PNU 가 조용히 붙는 일은 없다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.services.http_client import shared_verify
from app.services.pnu import LEGAL_CODE_LENGTH, build_pnu
from app.services.pnu_resolver import AssembledPnu

logger = logging.getLogger(__name__)

KAKAO_ADDRESS_URL = "https://dapi.kakao.com/v2/local/search/address.json"

# 원장 주소에 흔히 붙는 꼬리. 카카오 주소검색은 이런 접미가 붙으면 결과를 못 낸다.
_TRAILING = ("번지",)


def normalize_address(address: str) -> str:
    """원장 지번주소를 카카오가 받아들이는 형태로 다듬는다.

    「…667-3번지」의 `번지`, 그리고 「…635-7번지 2층」처럼 지번 뒤에 붙은 동·층
    표기를 떼어 낸다. 지번 자체는 건드리지 않는다.
    """

    text = (address or "").strip()
    if not text:
        return ""
    for tail in _TRAILING:
        text = text.replace(tail, " ")
    # 지번(숫자 또는 숫자-숫자) 이후의 동·층·호 표기는 조립에 방해만 된다.
    parts = text.split()
    cut = len(parts)
    for i, token in enumerate(parts):
        head = token.split("-")[0]
        if head.isdigit() and i >= 2:
            cut = i + 1
            break
    return " ".join(parts[:cut]).strip()


@dataclass
class KakaoAddressPnuStats:
    """계측. 배치 요약에 그대로 싣는다."""

    lookups: int = 0
    resolved: int = 0
    not_found: int = 0
    errors: int = 0
    memo_hits: int = 0
    error_samples: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"카카오 주소검색 {self.lookups}회 · 조립 {self.resolved} · "
            f"미발견 {self.not_found} · 오류 {self.errors} · 메모 재사용 {self.memo_hits}"
        )


class KakaoAddressPnu:
    """주소 문자열을 받아 `AssembledPnu` 를 돌려준다(`PnuResolver.address_pnu` 주입용)."""

    def __init__(
        self,
        rest_api_key: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = (rest_api_key or "").strip()
        self.timeout = timeout
        self._transport = transport
        self.stats = KakaoAddressPnuStats()
        self._memo: dict[str, AssembledPnu | None] = {}
        self._client: httpx.Client | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                transport=self._transport,
                verify=shared_verify(),
                headers={"Authorization": f"KakaoAK {self.api_key}"},
            )
        return self._client

    def _document(self, query: str) -> dict[str, Any] | None:
        try:
            r = self._http().get(KAKAO_ADDRESS_URL, params={"query": query, "size": 1})
        except httpx.HTTPError as exc:
            self.stats.errors += 1
            self._note(f"{type(exc).__name__}: {exc}")
            return None
        if r.status_code != 200:
            self.stats.errors += 1
            self._note(f"HTTP {r.status_code}")
            return None
        try:
            docs = (r.json() or {}).get("documents") or []
        except ValueError:
            self.stats.errors += 1
            self._note("응답이 JSON 이 아님")
            return None
        return docs[0] if docs else None

    def _note(self, text: str) -> None:
        if len(self.stats.error_samples) < 5:
            self.stats.error_samples.append(text)

    def __call__(self, address: str) -> AssembledPnu | None:
        query = normalize_address(address)
        if not query or not self.available:
            return None
        if query in self._memo:
            self.stats.memo_hits += 1
            return self._memo[query]

        self.stats.lookups += 1
        doc = self._document(query)
        result: AssembledPnu | None = None
        if doc is not None:
            result = self._assemble(doc)
        if result is None:
            self.stats.not_found += 1
        else:
            self.stats.resolved += 1
        self._memo[query] = result
        return result

    @staticmethod
    def _assemble(document: dict[str, Any]) -> AssembledPnu | None:
        """카카오 주소 문서에서 `AssembledPnu` 를 만든다.

        `pnu.pnu_from_address_document` 와 같은 필드를 쓰되, 상위(PnuResolver)가
        실패 사유 구분에 쓰는 `bonbun_prefix` 를 위해 구성요소를 함께 담는다.
        """

        addr = document.get("address") or document
        code = str(addr.get("b_code") or "")
        main = str(addr.get("main_address_no") or "")
        if len(code) != LEGAL_CODE_LENGTH or not main.isdigit():
            return None
        sub = str(addr.get("sub_address_no") or "")
        mountain = str(addr.get("mountain_yn") or "N").upper() == "Y"
        try:
            pnu = build_pnu(code, "Y" if mountain else "N", main, sub)
        except ValueError:
            return None
        return AssembledPnu(
            pnu=pnu,
            dong_code=code,
            is_mountain=mountain,
            bonbun=int(main),
            bubun=int(sub) if sub.isdigit() else 0,
        )
