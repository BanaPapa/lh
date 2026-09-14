"""factoryON 원본 PNU 확정 결과의 사이드카 캐시(기동 지연 해소).

배경
----
프로덕션 배선(`build_local_sources_bundle(resolver=...)`)은 factoryON 원본
약 7,983건의 PNU 를 설계서 §7.2 절차로 확정하는 데 실측 103초가 걸린다.
`get_hazard_service` 가 lru_cache 라 프로세스당 1회지만, 캐시가 없으면 그 1회가
요청 경로를 오래 막는다. 이 저장소는 요청 경로를 막는 동기 작업을 남기지 않는
원칙(safemap 콜드스타트 조치와 동일)을 지켜 왔다.

왜 캐시해도 안전한가
--------------------
PNU 확정 계산은 **외부 조회 없이 결정적**이다(법정동코드표 + 연속지적도 색인만
쓴다. 워밍업 resolver 는 geocoder=None 으로 만들어 네트워크 지오코딩을 배제한다).
같은 입력이면 항상 같은 결과가 나오므로, 결과를 파일로 저장해 다음 기동에
재사용해도 재현성이 깨지지 않는다.

가장 위험한 실패 = 낡은 캐시를 조용히 쓰는 것
--------------------------------------------
그래서 캐시 키에 **입력 지문 전부**를 넣어 하나라도 바뀌면 자동 무효화한다.
- 스키마 버전(SCHEMA_VERSION): 코드가 바뀌어 저장 형식·확정 절차가 달라지면 무효화.
- resolver 알고리즘 버전(RESOLVER_ALGORITHM_VERSION): 확정/검토 분기 규칙이 바뀌면 무효화.
- factoryON 원본 파일(zip 또는 디렉터리 멤버)의 내용 해시 + 해석된 절대경로.
- 법정동코드표 파일의 내용 해시 + 해석된 절대경로.
- 연속지적도 인덱스(SQLite) 파일의 부분 내용 해시(헤더·중간·말미 + 크기) + 절대경로.
mtime+size 만으로는 내용이 바뀌었는데 타임스탬프·크기가 같거나 메타데이터가 같은
다른 경로로 바꿔치기하면 낡은 PNU 매핑을 그대로 쓴다. 그래서 content_signature 로
내용과 경로를 키에 넣는다. 키가 조금이라도 다르면 캐시 미스로 보고 재계산한다.

깨진 캐시·쓰기 실패는 치명적이지 않다
--------------------------------------
캐시가 없거나 JSON 이 깨졌으면 조용히 None 을 돌려주고(호출부가 재계산),
쓰기 실패(권한·디스크)는 경고만 남기고 계산 결과로 계속 진행한다. 어떤 경우에도
예외로 죽지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 저장 형식·확정 절차가 바뀌면 이 값을 올린다. 키에 포함되므로 자동 무효화된다.
# v2: 캐시 키를 mtime+size(file_signature)에서 내용 해시+절대경로(content_signature)로
# 바꾸고, resolver 알고리즘 버전을 키에 넣었다(낡은 PNU 매핑 재사용 차단).
SCHEMA_VERSION = 2

CACHE_FILENAME = f"factory_pnu_resolution.v{SCHEMA_VERSION}.json"


@dataclass(frozen=True)
class FactoryResolution:
    """factoryON 원본 확정 결과 중 캐시로 재사용하는 부분.

    confirmed 는 확정된 (PNU, KSIC 업종번호 튜플) 목록이다. 고시업종 공장 PNU 집합
    (prohibit_factory_pnus)은 고시업종 목록(prohibit_ksic)과의 교집합으로 매번
    다시 계산한다. 고시업종 목록은 이 확정 계산의 입력이 아니라 값싸게 로드되는
    별도 원천이므로, 캐시 키에서 분리해 두는 편이 재사용성이 높다.
    """

    confirmed: tuple[tuple[str, tuple[str, ...]], ...] = ()
    total: int = 0
    confirmed_count: int = 0
    review: int = 0
    failed: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def confirmed_pnus(self) -> frozenset[str]:
        return frozenset(pnu for pnu, _ in self.confirmed if pnu)

    @property
    def confirm_rate(self) -> float:
        return self.confirmed_count / self.total if self.total else 0.0

    def prohibit_pnus(self, prohibit_ksic: frozenset[str]) -> frozenset[str]:
        """확정 공장 중 고시업종(KSIC ∈ prohibit_ksic)인 필지 PNU 집합."""

        if not prohibit_ksic:
            return frozenset()
        return frozenset(
            pnu
            for pnu, ksic in self.confirmed
            if pnu and (set(ksic) & prohibit_ksic)
        )


def file_signature(path: str | os.PathLike[str] | None) -> str | None:
    """파일 지문(mtime_ns:size). 파일이 없거나 접근 불가면 None.

    주의: mtime+size 는 내용이 바뀌었는데 타임스탬프·크기가 같거나(드묾), 메타데이터가
    같은 다른 경로로 바꾸면 변화를 놓친다. 캐시 키에는 content_signature 를 쓴다.
    이 함수는 하위호환·저비용 비교용으로만 남긴다.
    """

    if path is None:
        return None
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return f"{st.st_mtime_ns}:{st.st_size}"


# 전체 해시가 부담되지 않는 작은 입력(법정동표·공장 원장 xlsx, 보통 수십 MB 이하)은
# 내용 전체를 sha256 한다. 그 이상(연속지적도 색인 SQLite ~2.5GB)은 전체 해시가
# 느려 기동을 막으므로 부분 해시한다.
_FULL_HASH_MAX_BYTES = 64 * 1024 * 1024  # 64MiB
_PARTIAL_BLOCK_BYTES = 1024 * 1024  # 부분 해시 블록 크기(1MiB)


def content_signature(path: str | os.PathLike[str] | None) -> str | None:
    """내용에 민감한 파일 지문. 파일이 없거나 접근 불가면 None.

    mtime+size 보다 강하게, 실제 내용과 해석된 절대경로를 키에 반영한다.
    - 해석된 절대경로: 메타데이터가 같은 다른 경로로 바꿔치기해도 달라진다.
    - 크기: 언제나 반영.
    - 내용 해시:
      * 64MiB 이하는 전체를 sha256(법정동표·공장 원장).
      * 그 이상(지적도 색인 2.5GB)은 헤더·중간·말미 각 1MiB 블록만 해시한다.
        전체 해시(2.5GB)는 기동 경로를 수 초 막으므로 피한다. SQLite 색인은
        헤더에 스키마·페이지 수, 본문에 데이터, 말미에 최근 쓰인 페이지가 있어
        내용이 바뀌면 이 세 지점 중 하나는 대개 바뀐다. mtime/size 만보다 강하고
        전체 해시보다 빠른 절충이다(완벽하진 않으나 우연 충돌 가능성은 크게 낮다).
    """

    if path is None:
        return None
    try:
        resolved = Path(path).resolve()
        st = resolved.stat()
    except OSError:
        return None
    size = st.st_size
    hasher = hashlib.sha256()
    hasher.update(str(resolved).encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(str(size).encode("ascii"))
    try:
        with resolved.open("rb") as fh:
            if size <= _FULL_HASH_MAX_BYTES:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    hasher.update(chunk)
                mode = "full"
            else:
                fh.seek(0)
                hasher.update(fh.read(_PARTIAL_BLOCK_BYTES))  # 헤더
                fh.seek(size // 2)
                hasher.update(fh.read(_PARTIAL_BLOCK_BYTES))  # 중간
                fh.seek(max(0, size - _PARTIAL_BLOCK_BYTES))
                hasher.update(fh.read(_PARTIAL_BLOCK_BYTES))  # 말미
                mode = "partial"
    except OSError:
        return None
    return f"{mode}:{size}:{hasher.hexdigest()}"


def build_cache_key(inputs: Mapping[str, str | None]) -> str:
    """입력 지문 묶음으로 결정적 캐시 키(sha256)를 만든다.

    스키마 버전을 함께 넣어, 코드가 바뀌어 형식·절차가 달라지면 키가 달라지게 한다.
    입력 지문 중 하나라도 None(파일 부재)이면 그대로 키에 반영돼, 파일이 생기거나
    사라지는 것도 무효화 사유가 된다.
    """

    payload = {
        "schema_version": SCHEMA_VERSION,
        "inputs": {key: inputs[key] for key in sorted(inputs)},
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def default_cache_dir() -> Path:
    """기본 캐시 위치 = backend/data/.

    cadastral_*.sqlite 와 같은 디렉터리다. 산출물이 저장소에 커밋되지 않도록
    .gitignore 대상인지 통합 담당이 확인해야 한다(이 모듈은 산출물을 추적하지 않는다).
    """

    return Path(__file__).resolve().parents[2] / "data"


def cache_path(cache_dir: str | os.PathLike[str] | None = None) -> Path:
    base = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    return base / CACHE_FILENAME


def load(
    cache_dir: str | os.PathLike[str] | None,
    key: str,
) -> FactoryResolution | None:
    """키가 일치하는 캐시가 있으면 확정 결과를 돌려준다. 아니면 None.

    파일 부재·JSON 손상·키 불일치·스키마 불일치 어느 경우에도 예외로 죽지 않고
    None 을 돌려준다(호출부가 재계산). 낡은 캐시를 조용히 쓰지 않도록, 저장된
    key 필드가 현재 key 와 정확히 같을 때만 유효로 인정한다.
    """

    path = cache_path(cache_dir)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    if data.get("key") != key:
        # 입력이 바뀌었다 → 낡은 캐시. 조용히 무시하고 재계산하게 한다.
        return None
    raw_confirmed = data.get("confirmed")
    if not isinstance(raw_confirmed, list):
        return None
    try:
        confirmed = tuple(
            (str(item["pnu"]), tuple(str(c) for c in item.get("ksic", ())))
            for item in raw_confirmed
        )
    except (KeyError, TypeError):
        return None
    summary = data.get("summary") or {}
    by_reason_raw = summary.get("by_reason") or {}
    by_reason = {
        str(k): int(v)
        for k, v in by_reason_raw.items()
        if isinstance(v, (int, float))
    }
    try:
        return FactoryResolution(
            confirmed=confirmed,
            total=int(summary.get("total", 0)),
            confirmed_count=int(summary.get("confirmed", len(confirmed))),
            review=int(summary.get("review", 0)),
            failed=int(summary.get("failed", 0)),
            by_reason=by_reason,
        )
    except (TypeError, ValueError):
        return None


def save(
    cache_dir: str | os.PathLike[str] | None,
    key: str,
    resolution: FactoryResolution,
    inputs: Mapping[str, str | None] | None = None,
) -> bool:
    """확정 결과를 사이드카 파일로 원자적 저장한다. 성공하면 True.

    쓰기 실패(권한·디스크)는 치명적이지 않다. 경고만 남기고 False 를 돌려준다.
    호출부는 계산 결과로 그대로 진행한다.
    """

    path = cache_path(cache_dir)
    document = {
        "schema_version": SCHEMA_VERSION,
        "key": key,
        # inputs 는 감사·디버그용으로만 저장한다. 유효성 판정은 key 로만 한다.
        "inputs": dict(inputs) if inputs else {},
        "summary": {
            "total": resolution.total,
            "confirmed": resolution.confirmed_count,
            "review": resolution.review,
            "failed": resolution.failed,
            "by_reason": resolution.by_reason,
        },
        "confirmed": [
            {"pnu": pnu, "ksic": list(ksic)} for pnu, ksic in resolution.confirmed
        ],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 같은 디렉터리에 임시 파일로 쓴 뒤 원자적 교체(부분 기록 방지).
        fd, tmp_name = tempfile.mkstemp(
            prefix=CACHE_FILENAME + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(document, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_name, path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning(
            "factoryON PNU 확정 캐시 저장 실패(계속 진행): %s", exc
        )
        return False
    return True


__all__ = [
    "CACHE_FILENAME",
    "SCHEMA_VERSION",
    "FactoryResolution",
    "build_cache_key",
    "cache_path",
    "content_signature",
    "default_cache_dir",
    "file_signature",
    "load",
    "save",
]
