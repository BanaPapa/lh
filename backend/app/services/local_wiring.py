"""로컬 원천(local_sources) → 판정 서비스 배선 어댑터.

local_sources.py 는 파일을 읽어 중립 레코드로 "반환"만 한다(저장·판정 배선은 통합
단계의 몫). 이 모듈이 그 통합 단계다. 판정 서비스가 매 요청마다 파일을 다시 읽지
않도록, 애플리케이션 기동 시 한 번 읽어 판정에 바로 쓸 수 있는 형태로 추린다.

핵심 산출물
-----------
* factory_pnus: factoryON 등록공장 PNU 집합(표준본 공장 dataset_id=2·middle=공장).
  대기배출 후보(localdata air_pollution)의 필지 PNU 와 대조해 룰북 §6.1 나·다목의
  "공장 AND" 를 문자열 유사매칭 없이(룰북 §3) 확정하는 우선키.
* factory_facilities: 좌표를 가진 표준본 공장 레코드. 「공장 있음(등록공장)」 판정의
  후보가 된다. 절대 매입제외로 올리지 않는다(LH 확정 2026-09-11: 검토 표시만).
* cng_facilities: 좌표를 가진 CNG 충전소(원본 25_cng_stations.csv). FUEL25 후보.
* prohibit_ksic: 국토부 고시업종 KSIC 집합. 다목 AND 의 한 축이나, factoryON(업종
  번호만)과 표준본 공장(PNU 만)을 잇는 확정 키가 없어(LH 질의 7번) 이번에는
  판정에 배선하지 않는다. 적재 여부만 실어 보고·화면 note 에 사유를 남긴다.

파일이 없으면(다른 PC·미배포) 모든 항목이 비고 loaded=False 로 떨어진다. 앱은
정상 동작하고, 판정은 배선 전과 동일하게 dataset_missing 으로 남는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from app.services import pnu_resolution_cache as resolution_cache
from app.services.local_sources import (
    HAZARD_DIR,
    LocalSourceRecord,
    LocalSourcesConfig,
    SourceLoadResult,
    factory_registry_pnus,
    load_cng_stations,
    load_factory_prohibit_list,
    load_factory_registry,
    load_standard_source,
)
from app.services.pnu_resolver import (
    RESOLVER_ALGORITHM_VERSION,
    PnuResolver,
    resolve_records,
)


# factoryON 등록공장 원본 멤버(load_factory_registry 가 읽는 파일). 캐시 키의
# 입력 지문을 만들 때 실제로 읽는 파일을 가리켜야 낡은 캐시를 쓰지 않는다.
_FACTORY_REGISTRY_FILENAME = "03_06_09_factory_registry.xlsx"


@dataclass(frozen=True)
class LocalSourceFile:
    """로컬 후보를 실제로 적재한 출처(표시용).

    같은 등록공장 후보라도 표준본(LH_LOCAL_STANDARD_PATH 의 facilities.xlsx)에서
    왔는지 원본(03_06_09_factory_registry.xlsx 등)에서 왔는지에 따라 「데이터」열
    칩에 보여야 할 파일명이 다르다. 종전엔 칩 detail 에 원본 파일명을 하드코딩해,
    표준본으로 판정이 성립할 때 표시 파일명이 실제 출처와 어긋났다(Codex 리뷰).
    그래서 실제 로드된 파일명·구분(origin)을 여기 보존해 칩이 이걸 쓰게 한다.
    """

    filename: str
    origin: str  # "standard" | "raw"
    qualifier: str = ""  # 표준본 시트/구분 같은 짧은 부기(선택)

    def chip_detail(self) -> str:
        """칩 detail 문자열. 파일명(+표준본이면 짧은 구분). 파일명 없으면 빈 값."""

        if not self.filename:
            return ""
        if self.qualifier:
            return f"{self.filename} · {self.qualifier}"
        return self.filename


@dataclass(frozen=True)
class LocalSourcesBundle:
    """판정에 바로 쓰는 로컬 원천 묶음. 파일이 없으면 전부 빈 값·loaded=False."""

    factory_pnus: frozenset[str] = frozenset()
    factory_registry_loaded: bool = False
    factory_facilities: tuple[LocalSourceRecord, ...] = ()
    cng_facilities: tuple[LocalSourceRecord, ...] = ()
    prohibit_ksic: frozenset[str] = frozenset()
    prohibit_loaded: bool = False
    # factoryON 원본(업종번호 보유·PNU 없음)을 설계서 §7.2 절차로 확정한 PNU 집합.
    # factory_pnus 에 합쳐져 나·다목 공장 AND 우선키를 넓힌다.
    factory_registry_resolved_pnus: frozenset[str] = frozenset()
    # 그중 국토부 고시업종(KSIC ∈ prohibit_ksic)인 공장 PNU. 다목(4·5종 AND 고시업종
    # AND 공장)의 「고시업종 공장」 확정 키. 이 집합이 있어야 다목이 판정 가능해진다.
    prohibit_factory_pnus: frozenset[str] = frozenset()
    prohibit_factory_loaded: bool = False
    # 로컬 후보를 실제로 어느 파일에서 적재했는지(키: factory_registry/cng_stations …).
    # 「데이터」열 칩 detail 이 하드코딩 대신 이걸 써 표시 파일명과 실제 출처를 맞춘다.
    source_files: dict[str, LocalSourceFile] = field(default_factory=dict)
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def loaded(self) -> bool:
        """하나라도 실제로 적재됐는지. 전량 미적재면 배선 전과 동일하게 동작한다."""

        return (
            self.factory_registry_loaded
            or bool(self.factory_facilities)
            or bool(self.cng_facilities)
            or self.prohibit_loaded
        )


def _with_coordinates(
    records: tuple[LocalSourceRecord, ...],
) -> tuple[LocalSourceRecord, ...]:
    """좌표를 가진 레코드만 남긴다. 좌표 없는 원천은 지오코딩 전까지 후보가 못 된다."""

    return tuple(r for r in records if r.coordinates is not None)


def _display_filename(path: str) -> str:
    """표시용 경로에서 파일명만 뽑는다.

    표준본 경로(디렉터리/파일)·원본 zip 멤버(root!subdir/file) 모두 마지막 파일명만
    남긴다. 경로를 알 수 없으면(미설정) 빈 문자열 → 칩은 파일명 없이 "로컬"만 낸다.
    """

    if not path or path.startswith("(미설정)"):
        return ""
    tail = path.replace("\\", "/").rsplit("!", 1)[-1]
    return tail.rsplit("/", 1)[-1]


def _source_file(result: SourceLoadResult) -> LocalSourceFile | None:
    """적재 결과에서 칩 표시용 출처를 만든다. 파일명을 못 얻으면 None."""

    filename = _display_filename(result.path)
    if not filename:
        return None
    # 표준본은 여러 원천이 같은 파일(facilities.xlsx)을 공유하므로, 파일명만으론
    # 어느 원천인지 모호하다. 표준본일 때만 짧은 구분(label)을 덧붙인다. 원본은
    # 파일 자체가 원천이라 파일명만으로 충분하다.
    qualifier = result.label if result.origin == "standard" else ""
    return LocalSourceFile(filename=filename, origin=result.origin, qualifier=qualifier)


def _factory_registry_signature(cfg: LocalSourcesConfig) -> str | None:
    """factoryON 원본의 내용 지문. 원본이 바뀌면 캐시가 무효화되게 한다.

    raw_path 가 zip 이면 zip 파일 자체, 디렉터리면 실제 읽는 멤버 파일의 내용 해시를
    쓴다(content_signature: 내용+절대경로. mtime+size 만보다 강하다).
    """

    root = cfg.raw_path
    if root is None:
        return None
    root = Path(root)
    if root.is_file() and root.suffix.lower() == ".zip":
        return resolution_cache.content_signature(root)
    return resolution_cache.content_signature(
        root / HAZARD_DIR / _FACTORY_REGISTRY_FILENAME
    )


def _resolve_factory_registry(
    cfg: LocalSourcesConfig,
    resolver: PnuResolver,
    registry: SourceLoadResult,
    *,
    cache_dir: str | os.PathLike[str] | None,
    legal_dong_path: str | os.PathLike[str] | None,
    cadastral_db_path: str | os.PathLike[str] | None,
) -> tuple[resolution_cache.FactoryResolution, bool]:
    """factoryON 원본 PNU 확정 결과를 얻는다(캐시 우선). (결과, 캐시적중여부) 반환.

    캐시 키는 원본·법정동표·지적도 인덱스의 내용 해시와 스키마·알고리즘 버전으로
    이뤄진다. 하나라도 바뀌면 미스가 나 재계산한다. 캐시 미스면 설계서 §7.2 절차로
    확정하고(실측 103초) 결과를 저장한다. 캐시 적중이면 재계산과 동일한 결과를
    즉시 돌려준다.
    """

    if cadastral_db_path is None:
        cadastral_db_path = getattr(
            getattr(resolver, "cadastral", None), "db_path", None
        )
    inputs: dict[str, str | None] = {
        "factory_registry": _factory_registry_signature(cfg),
        "legal_dong": resolution_cache.content_signature(legal_dong_path),
        "cadastral_index": resolution_cache.content_signature(cadastral_db_path),
        # resolver 확정/검토 분기 규칙이 바뀌면(코드 변경) 낡은 PNU 매핑을 재사용하지
        # 않도록 알고리즘 버전도 키에 넣는다. build_cache_key 가 문자열로 직렬화한다.
        "resolver_algorithm_version": str(RESOLVER_ALGORITHM_VERSION),
    }
    key = resolution_cache.build_cache_key(inputs)

    cached = resolution_cache.load(cache_dir, key)
    if cached is not None:
        return cached, True

    records = (*registry.records, *registry.quarantined)
    summary, results = resolve_records(resolver, records)
    confirmed = tuple(
        (resolution.pnu, tuple(getattr(record, "ksic_codes", ()) or ()))
        for record, resolution in results
        if resolution.confirmed and resolution.pnu
    )
    resolution = resolution_cache.FactoryResolution(
        confirmed=confirmed,
        total=summary.total,
        confirmed_count=summary.confirmed,
        review=summary.review,
        failed=summary.failed,
        by_reason=dict(summary.by_reason),
    )
    resolution_cache.save(cache_dir, key, resolution, inputs)
    return resolution, False


def build_local_sources_bundle(
    config: LocalSourcesConfig | None = None,
    resolver: PnuResolver | None = None,
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    legal_dong_path: str | os.PathLike[str] | None = None,
    cadastral_db_path: str | os.PathLike[str] | None = None,
) -> LocalSourcesBundle:
    """환경변수(LH_LOCAL_STANDARD_PATH / LH_LOCAL_RAW_PATH) 기준으로 묶음을 만든다.

    어떤 원천도 예외로 죽지 않는다. 파일이 없으면 loaded=False 인 빈 묶음이 나온다.

    resolver 를 주면(라우터가 법정동표+연속지적도로 만든 PnuResolver) factoryON
    원본 7,983건(업종번호 보유·PNU 없음)에 설계서 §7.2 절차로 PNU 를 확정해
    표준본 공장 PNU 와 합친다(나·다목 공장 AND 우선키 확장). 그중 고시업종 공장
    PNU 집합도 만들어 다목(4·5종 AND 고시업종 AND 공장)을 판정 가능하게 한다.
    resolver 가 없거나 지적도/법정동표가 없으면 이 확장은 건너뛰고 배선 전과 동일하게
    표준본 577개만 쓴다(하위호환). 확정 실패 건은 삭제하지 않고 사유별로 note 에 집계.

    factoryON 확정은 실측 103초로 무거우므로, 결과를 `cache_dir`(기본 backend/data)
    아래 사이드카 파일로 저장해 다음 기동에 재사용한다. 캐시 키는 원본 파일·법정동표·
    지적도 인덱스의 내용 해시와 스키마·알고리즘 버전으로 이뤄져, 하나라도 바뀌면
    자동 무효화된다(pnu_resolution_cache 모듈). legal_dong_path/cadastral_db_path 는
    캐시 키에 넣을 입력 파일 경로다. 안 주면 cadastral 인덱스 경로는 resolver 에서
    유추한다. 확정 계산은 외부 조회 없이 결정적이므로 캐시해도 재현성이 깨지지 않는다.
    """

    cfg = config or LocalSourcesConfig.from_env()
    notes: list[str] = []
    source_files: dict[str, LocalSourceFile] = {}

    # 1) factoryON PNU 집합(표준본 공장) — 공장 AND 우선키.
    pnu_result = factory_registry_pnus(cfg)
    factory_pnus: frozenset[str] = pnu_result.pnus if pnu_result.loaded else frozenset()

    # 2) 표준본 공장 레코드(좌표 보유) — 공장 인접 후보.
    factory_std = load_standard_source(cfg, "factory_standard")
    factory_facilities: tuple[LocalSourceRecord, ...] = ()
    if factory_std.loaded:
        factory_facilities = _with_coordinates(factory_std.records)
        # 등록공장 칩의 근거는 이 표준본 공장 레코드다. 원본 파일명을 박지 말고 실제
        # 적재 출처(표준본/원본)의 파일명을 칩에 실어야 표시가 어긋나지 않는다.
        src = _source_file(factory_std)
        if src is not None:
            source_files["factory_registry"] = src

    # 3) CNG 충전소(좌표 보유) — FUEL25 후보.
    cng = load_cng_stations(cfg)
    cng_facilities: tuple[LocalSourceRecord, ...] = ()
    if cng.loaded:
        cng_facilities = _with_coordinates(cng.records)
        src = _source_file(cng)
        if src is not None:
            source_files["cng_stations"] = src

    # 4) 고시업종 목록.
    prohibit = load_factory_prohibit_list(cfg)
    prohibit_ksic = prohibit.ksic_codes if prohibit.loaded else frozenset()

    # 5) factoryON 원본 PNU 확정(설계서 §7.2). resolver·지적도가 있을 때만.
    resolved_pnus: frozenset[str] = frozenset()
    prohibit_factory_pnus: frozenset[str] = frozenset()
    prohibit_factory_loaded = False
    if resolver is not None and resolver.ready:
        registry = load_factory_registry(cfg)
        if registry.loaded:
            resolution, from_cache = _resolve_factory_registry(
                cfg,
                resolver,
                registry,
                cache_dir=cache_dir,
                legal_dong_path=legal_dong_path,
                cadastral_db_path=cadastral_db_path,
            )
            resolved_pnus = resolution.confirmed_pnus
            factory_pnus = factory_pnus | resolved_pnus
            # 확정된 factoryON 레코드 중 고시업종(KSIC)인 공장의 PNU. 고시업종 목록은
            # 확정 계산의 입력이 아니라 값싼 별도 원천이므로, 캐시된 확정 결과와의
            # 교집합으로 매번 다시 계산한다(고시목록이 바뀌어도 캐시 재사용 가능).
            if prohibit.loaded:
                prohibit_factory_pnus = resolution.prohibit_pnus(prohibit_ksic)
                prohibit_factory_loaded = bool(prohibit_factory_pnus)
            reason_parts = ", ".join(
                f"{code} {n}" for code, n in sorted(resolution.by_reason.items())
            )
            notes.append(
                f"factoryON 원본 {resolution.total}건 PNU 확정: "
                f"확정 {resolution.confirmed_count}"
                f"({resolution.confirm_rate * 100:.1f}%)·"
                f"검토 {resolution.review}·실패 {resolution.failed}"
                + (f" [{reason_parts}]" if reason_parts else "")
                + (" · 캐시 재사용" if from_cache else " · 신규 계산·캐시 저장")
            )
    elif prohibit.loaded:
        notes.append(
            "다목(고시업종 AND)은 PnuResolver 미주입으로 판정 미배선(현행 "
            "dataset_missing 유지). 법정동표·연속지적도 주입 시 활성화."
        )

    return LocalSourcesBundle(
        factory_pnus=factory_pnus,
        # 공장 AND 게이트는 「등록공장 PNU 집합을 갖고 있는가」다. 그 집합을 어디서
        # 얻었는지는 게이트의 조건이 아니다. 종전에는 표준본(LH_LOCAL_STANDARD_PATH)
        # 경로만 봤는데, 이 앱은 표준본을 쓰지 않고 원장 주소를 API 로 확정해 같은
        # 집합을 만든다(설계서 §7.2 절차는 동일). 확정분이 있으면 게이트를 연다.
        factory_registry_loaded=pnu_result.loaded or bool(resolved_pnus),
        factory_facilities=factory_facilities,
        cng_facilities=cng_facilities,
        prohibit_ksic=prohibit_ksic,
        prohibit_loaded=prohibit.loaded,
        factory_registry_resolved_pnus=resolved_pnus,
        prohibit_factory_pnus=prohibit_factory_pnus,
        prohibit_factory_loaded=prohibit_factory_loaded,
        source_files=source_files,
        notes=tuple(notes),
    )
