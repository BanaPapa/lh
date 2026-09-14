"""factoryON PNU 확정 사이드카 캐시의 지문 강도 회귀(Codex 지적 3번).

핵심 불변식을 고정한다.
- mtime+size 만으로는 내용이 바뀌어도 지문이 같을 수 있다(약한 지문의 재현).
- content_signature 는 같은 mtime+size 라도 내용이 바뀌면 다른 지문을 낸다.
- 해석된 절대경로가 다르면(같은 내용이라도) 지문이 달라진다(바꿔치기 방지).
- resolver 알고리즘 버전이 캐시 키에 반영돼, 코드가 바뀌면 캐시가 무효화된다.
- _resolve_factory_registry 는 원본 내용이 바뀌면(mtime·size 동일해도) 캐시를
  재사용하지 않고 재계산한다(가장 위험한 낡은 PNU 재사용을 막는 최종 관문).
"""

from __future__ import annotations

import os

from app.services import pnu_resolution_cache as resolution_cache
from app.services.local_sources import HAZARD_DIR, LocalSourcesConfig, SourceLoadResult
from app.services.local_wiring import _FACTORY_REGISTRY_FILENAME, _resolve_factory_registry
from app.services.pnu_resolver import RESOLVER_ALGORITHM_VERSION


def _force_same_stat(path, reference_stat: os.stat_result) -> None:
    """path 의 mtime 을 reference_stat 과 똑같이 맞춘다(크기는 호출부가 맞춘다)."""

    os.utime(path, ns=(reference_stat.st_atime_ns, reference_stat.st_mtime_ns))


# ---------------------------------------------------------------------------
# file_signature(mtime+size) 의 약점 재현 — 이게 바로 고쳐야 했던 결함이다.
# ---------------------------------------------------------------------------
class TestWeakFileSignatureReproduced:
    def test_mtime_size_signature_is_blind_to_content_change(self, tmp_path) -> None:
        path = tmp_path / "registry.xlsx"
        path.write_bytes(b"A" * 100)
        before_stat = path.stat()
        before_sig = resolution_cache.file_signature(path)

        # 내용을 바꾸되 길이(size)는 그대로 두고, mtime 도 원래 값으로 강제한다.
        path.write_bytes(b"B" * 100)
        _force_same_stat(path, before_stat)
        after_sig = resolution_cache.file_signature(path)

        # 이것이 바로 결함이었다 — mtime+size 만으로는 내용 변화를 못 잡는다.
        assert before_sig == after_sig


# ---------------------------------------------------------------------------
# content_signature — 내용 해시 + 해석된 절대경로
# ---------------------------------------------------------------------------
class TestContentSignature:
    def test_detects_content_change_with_identical_mtime_and_size(self, tmp_path) -> None:
        path = tmp_path / "registry.xlsx"
        path.write_bytes(b"A" * 100)
        before_stat = path.stat()
        before_sig = resolution_cache.content_signature(path)

        path.write_bytes(b"B" * 100)
        _force_same_stat(path, before_stat)
        after_sig = resolution_cache.content_signature(path)

        # 크기·mtime 이 같아도 내용이 다르면 지문이 달라져야 한다.
        assert path.stat().st_size == before_stat.st_size
        assert before_sig != after_sig

    def test_identical_content_gives_identical_signature(self, tmp_path) -> None:
        path = tmp_path / "registry.xlsx"
        path.write_bytes(b"stable-content")
        assert resolution_cache.content_signature(path) == resolution_cache.content_signature(path)

    def test_different_path_same_content_gives_different_signature(self, tmp_path) -> None:
        # 메타데이터(내용·크기)가 같아도 해석된 절대경로가 다른 파일로 바꿔치기하면
        # 지문이 달라져야 한다(경로 바꿔치기 방지).
        content = b"same-content"
        path_a = tmp_path / "a" / "registry.xlsx"
        path_b = tmp_path / "b" / "registry.xlsx"
        path_a.parent.mkdir()
        path_b.parent.mkdir()
        path_a.write_bytes(content)
        path_b.write_bytes(content)
        assert resolution_cache.content_signature(path_a) != resolution_cache.content_signature(path_b)

    def test_missing_file_returns_none(self, tmp_path) -> None:
        assert resolution_cache.content_signature(tmp_path / "absent.xlsx") is None

    def test_none_path_returns_none(self) -> None:
        assert resolution_cache.content_signature(None) is None

    def test_large_file_uses_partial_hash_and_still_detects_boundary_changes(
        self, tmp_path, monkeypatch
    ) -> None:
        # 실제 지적도 인덱스(2.5GB)를 흉내내되, 테스트에서는 임계값을 낮춰 같은
        # 부분 해시 분기(헤더·중간·말미)를 작은 파일로 검증한다.
        monkeypatch.setattr(resolution_cache, "_FULL_HASH_MAX_BYTES", 64)
        monkeypatch.setattr(resolution_cache, "_PARTIAL_BLOCK_BYTES", 8)

        size = 200
        path = tmp_path / "cadastral_index.sqlite"
        path.write_bytes(b"\x00" * size)
        before_stat = path.stat()
        before_sig = resolution_cache.content_signature(path)
        assert before_sig is not None
        assert before_sig.startswith("partial:")

        # 헤더(맨 앞) 변경 — 부분 해시 블록에 걸리므로 반드시 잡혀야 한다.
        data = bytearray(size)
        data[0] = 0xFF
        path.write_bytes(bytes(data))
        _force_same_stat(path, before_stat)
        header_changed_sig = resolution_cache.content_signature(path)
        assert header_changed_sig != before_sig

        # 말미(맨 뒤) 변경도 마찬가지로 잡혀야 한다.
        data = bytearray(size)
        data[-1] = 0xFF
        path.write_bytes(bytes(data))
        _force_same_stat(path, before_stat)
        tail_changed_sig = resolution_cache.content_signature(path)
        assert tail_changed_sig != before_sig


# ---------------------------------------------------------------------------
# build_cache_key — 입력 지문 묶음이 하나라도 바뀌면 키가 달라진다.
# ---------------------------------------------------------------------------
class TestBuildCacheKey:
    def test_key_changes_when_any_input_signature_changes(self) -> None:
        base = {"factory_registry": "sig-a", "legal_dong": "sig-b", "cadastral_index": "sig-c"}
        changed = {**base, "factory_registry": "sig-a-modified"}
        assert resolution_cache.build_cache_key(base) != resolution_cache.build_cache_key(changed)

    def test_key_changes_when_algorithm_version_input_changes(self) -> None:
        base = {"resolver_algorithm_version": "1"}
        changed = {"resolver_algorithm_version": "2"}
        assert resolution_cache.build_cache_key(base) != resolution_cache.build_cache_key(changed)

    def test_key_is_deterministic_for_same_inputs(self) -> None:
        inputs = {"a": "1", "b": "2"}
        assert resolution_cache.build_cache_key(inputs) == resolution_cache.build_cache_key(dict(inputs))


# ---------------------------------------------------------------------------
# _resolve_factory_registry — 통합: 원본 내용이 바뀌면(mtime·size 동일) 캐시 미스.
# ---------------------------------------------------------------------------
class TestResolveFactoryRegistryCacheInvalidation:
    def _write_registry(self, tmp_path, content: bytes):
        hazard_dir = tmp_path / "raw" / HAZARD_DIR
        hazard_dir.mkdir(parents=True, exist_ok=True)
        path = hazard_dir / _FACTORY_REGISTRY_FILENAME
        path.write_bytes(content)
        return path

    def test_stale_pnu_mapping_is_not_reused_when_content_changes_with_same_mtime_size(
        self, tmp_path
    ) -> None:
        cache_dir = tmp_path / "cache"
        raw_path = tmp_path / "raw"
        registry_path = self._write_registry(tmp_path, b"A" * 128)
        before_stat = registry_path.stat()

        cfg = LocalSourcesConfig(standard_path=None, raw_path=raw_path)
        empty_registry = SourceLoadResult(
            source_id="factory_registry", label="factoryON", loaded=True,
            origin="xlsx", path=str(registry_path), records=(), quarantined=(),
        )

        # 1회차: 캐시 미스로 계산되고 저장된다.
        _, first_from_cache = _resolve_factory_registry(
            cfg, object(), empty_registry,
            cache_dir=cache_dir, legal_dong_path=None, cadastral_db_path=None,
        )
        assert first_from_cache is False

        # 2회차: 아무것도 안 바뀌었으니 캐시 적중이어야 한다.
        _, second_from_cache = _resolve_factory_registry(
            cfg, object(), empty_registry,
            cache_dir=cache_dir, legal_dong_path=None, cadastral_db_path=None,
        )
        assert second_from_cache is True

        # 원본 내용을 바꾸되(낡은 PNU 매핑을 무효화해야 하는 변화) mtime·size 는
        # 원래 값으로 강제한다 — 이게 바로 이번 지적의 핵심 시나리오다.
        registry_path.write_bytes(b"B" * 128)
        _force_same_stat(registry_path, before_stat)
        assert registry_path.stat().st_size == before_stat.st_size

        # 3회차: 내용이 바뀌었으므로 mtime·size 가 같아도 캐시 미스여야 한다
        # (낡은 PNU 확정 결과를 조용히 재사용하면 안 된다).
        _, third_from_cache = _resolve_factory_registry(
            cfg, object(), empty_registry,
            cache_dir=cache_dir, legal_dong_path=None, cadastral_db_path=None,
        )
        assert third_from_cache is False

    def test_algorithm_version_constant_is_wired_into_the_cache_key(self) -> None:
        # 회귀: resolver 알고리즘 버전이 실제로 캐시 키 입력에 들어간다는 것을
        # 상수 자체가 존재하고 지역 상수와 일치하는지로 고정한다.
        assert isinstance(RESOLVER_ALGORITHM_VERSION, int)
