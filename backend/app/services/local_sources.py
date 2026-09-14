"""박진주 대표 인계 로컬 원천 파일 적재 모듈.

인허가 개방 API(1741000)로는 뽑을 수 없는 원천들이 있다. 대표적으로 factoryON
등록공장 대장과 국토부 고시업종 목록이다. 이 둘이 없으면 룰북 1차 공장 5항목
(나·다·라목 + 공장 인접)의 AND 조건인 "공장 여부"를 확정할 수 없어 어떤 항목도
판정되지 않는다. 이 모듈은 그 로컬 원천을 읽어 이 앱의 중립 레코드로 정규화해
"반환"만 한다. 저장(적재 배선)은 통합 단계의 몫이라 여기서 하지 않는다.

설계 원칙
- 표준본 우선: 좌표·PNU 가 이미 붙어 있는 delivery/standardized/facilities.xlsx 를
  정본으로 쓴다. 원본 CSV/XLSX 는 표준본이 덮지 못하는 범위(factoryON 등록공장,
  고시업종 목록, 세부 업종 구분)에만 쓴다.
- 경로 주입: 원본 위치를 하드코딩하지 않는다. 인자 또는 환경변수로 받고, 파일이
  없으면 예외로 죽지 않고 loaded=False("미적재") 로 명확히 알린다. 저장소에는
  원본이 없어 다른 PC 에서는 경로가 다르다.
- 룰북 §7 표준화 전제: 폐업·취소·말소는 판정 투입 전 제외한다. 휴업·영업상태
  공란은 제외하지 않고 검토(review) 표시로 남긴다. 좌표 결측·범위 밖 행은 삭제하지
  않고 격리(quarantine)하고 사유를 남긴다.
- 룰북 §3 PNU 우선 매칭: 공장 원천 간 동일시설 매칭은 PNU 를 우선키로 한다.
  문자열 유사매칭으로 확정하지 않는다. factoryON 등록공장의 PNU 집합을 별도
  함수로 내보내 대기배출·소음 후보와 대조할 수 있게 한다.

좌표계 메모: 이 모듈이 읽는 좌표는 모두 이미 WGS84 십진도이다(표준본 longitude/
latitude, LPG/CNG 의 위경도 컬럼 모두 35.x/126.x 도 단위). 인허가 API 원천처럼
EPSG:5174 평면좌표를 변환할 일이 없어 pyproj 를 쓰지 않는다. 대신 변환 전제가
지켜졌는지 확인하는 의미로 전북 범위 재검사만 수행한다.
"""

from __future__ import annotations

import csv
import io
import os
import zipfile
from dataclasses import replace as dc_replace
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from openpyxl import load_workbook

from app.models import Coordinates


# ── 경로/좌표 상수 ──────────────────────────────────────────────────────────

# 원천 위치를 여기서 하드코딩하지 않는다. 환경변수 이름만 고정한다.
ENV_STANDARD_PATH = "LH_LOCAL_STANDARD_PATH"
ENV_RAW_PATH = "LH_LOCAL_RAW_PATH"

# 압축본 안의 경로 접두. 원본 zip 은 유해시설/·편의시설/ 두 폴더로 나뉜다.
HAZARD_DIR = "유해시설"
AMENITY_DIR = "편의시설"

# CSV 인코딩이 파일마다 다르다(utf-8-sig / cp949 혼재). 실측 우선순위.
CSV_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp949", "utf-8")

# 전북(전라북도/전북특별자치도) 대략 경계. WGS84 변환 전제가 지켜졌는지 재검사한다.
# 삭제 기준이 아니라 격리 기준이다. 범위를 넘으면 좌표를 버리지 않고 사유와 함께 격리.
JEONBUK_LAT_MIN, JEONBUK_LAT_MAX = 34.9, 36.4
JEONBUK_LNG_MIN, JEONBUK_LNG_MAX = 125.9, 128.0

# 한반도 위경도 대략 범위. lat/lng 자동 판별에 쓴다.
KOREA_LAT_MIN, KOREA_LAT_MAX = 33.0, 39.5
KOREA_LNG_MIN, KOREA_LNG_MAX = 124.0, 132.0

# 지오코딩 실패 시 들어가는 전형적 기본값. H-02-나 §6-4 ① — LPG 충전소 원본에서
# 현대자동차(주)전주공장(완주군)에 서울시청 좌표(37.5665, 126.978)가 들어 있었다.
# 값이 이 점에 붙어 있으면 실좌표가 아니라 기본값으로 보고 격리한다.
SUSPECT_DEFAULT_COORDINATES: tuple[tuple[float, float, str], ...] = (
    (37.5665, 126.978, "서울시청"),
)
SUSPECT_DEFAULT_TOLERANCE_DEG = 0.0005  # 약 50m

# H-03 §6-2 — 자동차용 천연가스충전소 원본(25_cng_stations.csv) 좌표 8건 전부가
# 714~3,779m 틀렸다(VWorld 주소검색·카카오 로컬 두 API 가 서로 일치하고 LH 기존
# 목록과도 맞는다). 원본 좌표를 버리고 VWorld 실측값으로 교체한다. 키는 시설명에서
# 공백을 뺀 값이다. 원본에 없는 시설은 이 표에 걸리지 않으므로 원본 좌표를 쓴다.
# 근거: docs/hazards/H-03-자동차용천연가스충전소.md §6-2 (2026-09-01 실측).
CNG_COORDINATE_BASIS = "VWorld·카카오 실측 좌표로 교체 (H-03 §6-2, 2026-09-01)"
CNG_VERIFIED_COORDINATES: dict[str, tuple[float, float]] = {
    "(유)호남고속팔복CNG충전소": (35.857275, 127.104786),
    "대흥산업가스(주)": (35.971200, 126.672957),
    "현대자동차(주)전주공장CNG제1충전소": (35.945800, 127.138686),
    "(유)전일여객덕진CNG충전소": (35.850147, 127.113510),
    "현대자동차(주)전주공장CNG제2충전소": (35.945800, 127.138686),
    "(유)제일씨엔지에너지평화동CNG충전소": (35.777787, 127.122307),
    "군산도시가스(주)신관CNG충전소": (35.935183, 126.685615),
    "전북에너지서비스(주)송학동CNG충전소": (35.938184, 126.922596),
}


def _name_key(name: str) -> str:
    """시설명 대조 키. 원본마다 띄어쓰기가 달라 공백을 전부 뺀다."""

    return "".join((name or "").split())

# 영업상태 텍스트 분류 키워드. 폐업 계열은 판정 투입 전 제외, 휴업 계열은 검토로 유지.
EXCLUDED_KEYWORDS: tuple[str, ...] = (
    "폐업",
    "말소",
    "등록취소",
    "직권취소",
    "취소",
    "철거",
    "폐쇄",
)
REVIEW_KEYWORDS: tuple[str, ...] = ("휴업", "영업정지", "정지", "중지")

StatusClass = Literal["active", "review", "excluded", "unknown"]


# ── 설정/레코드/결과 자료형 ─────────────────────────────────────────────────


@dataclass(frozen=True)
class LocalSourcesConfig:
    """원천 위치 주입. 둘 다 없어도 되며, 없는 쪽 원천은 미적재로 반환된다."""

    standard_path: Path | None = None
    raw_path: Path | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> "LocalSourcesConfig":
        source = os.environ if env is None else env
        standard = source.get(ENV_STANDARD_PATH) or None
        raw = source.get(ENV_RAW_PATH) or None
        return cls(
            standard_path=Path(standard) if standard else None,
            raw_path=Path(raw) if raw else None,
        )


@dataclass(frozen=True)
class LocalSourceRecord:
    """중립 시설 레코드.

    이 앱의 기존 표현(app.services.localdata.LocalDataRecord / facility_store
    스키마 / hazard_review.models.HazardFacility)에 그대로 실을 수 있게 필드를
    맞췄다. 다만 좌표는 결측을 허용해야 하므로(룰북 §7 격리 전제) LocalDataRecord
    와 달리 Optional 이다.
    """

    source_id: str
    record_id: str
    name: str
    address: str = ""
    road_address: str = ""
    coordinates: Coordinates | None = None
    status_text: str = ""
    status_class: StatusClass = "unknown"
    category: str = ""
    pnu: str = ""
    # 공장 업종번호(한국표준산업분류). 다목 AND 조건(고시업종) 대조용.
    ksic_codes: tuple[str, ...] = ()
    quarantined: bool = False
    quarantine_reason: str = ""
    review_reason: str = ""
    origin: str = ""  # "standard" | "raw"
    # 원본의 부가 컬럼(시군·좌표 교체 근거·병합 행수 등). 판정 근거 기록용.
    extra: Mapping[str, str] = field(default_factory=dict)

    def to_localdata_record(self):
        """app.services.localdata.LocalDataRecord 로 변환.

        좌표가 없으면(격리 대상) None 을 돌려준다. 저장 배선은 통합 단계에서 하되,
        기존 FacilityStore.replace_dataset 이 그대로 받아 쓸 수 있게 다리만 놓는다.
        여기서 import 를 지연시키는 것은 순환 import 를 피하기 위함이다.
        """

        if self.coordinates is None:
            return None
        from app.services.localdata import LocalDataRecord

        return LocalDataRecord(
            dataset_key=self.source_id,
            record_id=self.record_id,
            name=self.name,
            address=self.address,
            road_address=self.road_address,
            coordinates=self.coordinates,
            status=self.status_text,
            category=self.category,
        )


@dataclass(frozen=True)
class SourceLoadResult:
    """원천 하나의 적재 결과.

    records 는 판정 투입 가능한 후보(영업/검토 + 좌표 보유). quarantined 는 좌표
    결측·범위 밖으로 격리된 후보. excluded 는 폐업 계열로 판정 전 제외된 행이다.
    셋의 합이 원본 유효행 수와 맞아 감사할 수 있다.
    """

    source_id: str
    label: str
    loaded: bool
    origin: str
    path: str
    records: tuple[LocalSourceRecord, ...] = ()
    quarantined: tuple[LocalSourceRecord, ...] = ()
    excluded: tuple[LocalSourceRecord, ...] = ()
    note: str = ""

    @property
    def total_rows(self) -> int:
        return len(self.records) + len(self.quarantined) + len(self.excluded)

    @property
    def coord_rows(self) -> int:
        return sum(
            1 for r in self.records if r.coordinates is not None
        )

    @property
    def pnu_rows(self) -> int:
        rows = (*self.records, *self.quarantined)
        return sum(1 for r in rows if r.pnu)

    @property
    def review_rows(self) -> int:
        rows = (*self.records, *self.quarantined)
        return sum(1 for r in rows if r.status_class == "review")

    @property
    def excluded_count(self) -> int:
        return len(self.excluded)


@dataclass(frozen=True)
class ProhibitList:
    """국토부 고시업종 목록(다목 AND 조건). 시설 레코드가 아니라 코드 집합이다."""

    loaded: bool
    path: str
    ksic_codes: frozenset[str] = frozenset()
    entries: tuple[tuple[str, str], ...] = ()  # (코드, 업종명)
    note: str = ""


@dataclass(frozen=True)
class PnuSetResult:
    """factoryON 등록공장 PNU 집합. 대기배출·소음 후보와 PNU 로 대조하는 우선키."""

    loaded: bool
    origin: str
    path: str
    pnus: frozenset[str] = frozenset()
    note: str = ""


# ── 저수준 입출력 (zip/디렉터리/인코딩) ──────────────────────────────────────


def _raw_bytes(config: LocalSourcesConfig, subdir: str, filename: str) -> bytes | None:
    """원본 zip 또는 디렉터리에서 멤버 하나를 바이트로 읽는다.

    raw_path 가 .zip 이면 압축을 풀지 않고 메모리로 읽는다(원본을 저장소·디스크에
    복사하지 않기 위함). 디렉터리면 하위 경로를 조인한다. 없으면 None.
    """

    root = config.raw_path
    if root is None:
        return None
    member = f"{subdir}/{filename}"
    if root.is_file() and root.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(root) as archive:
                try:
                    return archive.read(member)
                except KeyError:
                    return None
        except (zipfile.BadZipFile, OSError):
            return None
    candidate = root / subdir / filename
    if candidate.is_file():
        try:
            return candidate.read_bytes()
        except OSError:
            return None
    return None


def _decode_csv(data: bytes) -> tuple[str, list[dict[str, str]]]:
    """CSV 바이트를 인코딩 자동 판별로 DictReader 결과까지 편다."""

    text: str | None = None
    used = CSV_ENCODINGS[-1]
    for encoding in CSV_ENCODINGS:
        try:
            text = data.decode(encoding)
            used = encoding
            break
        except UnicodeDecodeError:
            continue
    if text is None:  # 최후: 손상 문자를 흘려 읽는다(행 유실보다 낫다).
        text = data.decode(CSV_ENCODINGS[-1], errors="replace")
    rows = list(csv.DictReader(text.splitlines()))
    cleaned = [
        {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        for row in rows
    ]
    return used, cleaned


def _xlsx_rows(data: bytes, sheet: str | None = None) -> list[dict[str, str]]:
    """xlsx 바이트 첫 시트(또는 지정 시트)를 헤더 기준 dict 목록으로 편다."""

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        worksheet = workbook[sheet] if sheet else workbook[workbook.sheetnames[0]]
        # 일부 원본(factoryON 등록공장)은 저장된 차원 메타가 깨져 1x1 로 보고된다.
        # read_only 는 그 값을 믿어 헤더만 읽고 만다. 차원을 리셋해 전 행을 강제로
        # 훑게 한다. read_only 워크시트에만 있는 메서드라 방어적으로 호출한다.
        reset = getattr(worksheet, "reset_dimensions", None)
        if callable(reset):
            reset()
        iterator = worksheet.iter_rows(values_only=True)
        header_row = next(iterator, None)
        if header_row is None:
            return []
        header = [str(c).strip() if c is not None else "" for c in header_row]
        result: list[dict[str, str]] = []
        for row in iterator:
            if row is None or all(cell is None for cell in row):
                continue
            record: dict[str, str] = {}
            for key, cell in zip(header, row):
                if not key:
                    continue
                record[key] = "" if cell is None else str(cell).strip()
            result.append(record)
        return result
    finally:
        workbook.close()


# ── 정규화 헬퍼 ──────────────────────────────────────────────────────────────


def classify_status(text: str, *, has_status_field: bool) -> tuple[StatusClass, str]:
    """영업상태 텍스트를 룰북 §7 기준으로 분류한다.

    반환은 (분류, 검토사유). 폐업 계열=excluded, 휴업·공란=review. 원천에 영업상태
    컬럼 자체가 없으면(has_status_field=False) 공란을 검토로 보지 않고 active 로
    둔다. 상태를 추적하지 않는 대장(예: LPG 판매소·CNG)에 없는 상태를 근거로
    검토 딱지를 붙이면 노이즈만 는다.
    """

    value = (text or "").strip()
    if value:
        for keyword in EXCLUDED_KEYWORDS:
            if keyword in value:
                return "excluded", ""
        for keyword in REVIEW_KEYWORDS:
            if keyword in value:
                return "review", f"영업상태 '{value}' — 검토 필요"
        return "active", ""
    if has_status_field:
        return "review", "영업상태 공란 — 검토 필요"
    return "active", ""


def _to_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def coerce_lat_lng(a: object, b: object) -> tuple[float | None, float | None]:
    """두 좌표값에서 (lat, lng) 를 값 범위로 판별한다.

    일부 원천은 컬럼명이 뒤바뀌어 있다(13_lpg_charging: LAT 컬럼에 위도, LOT 컬럼에
    경도지만 이름만 봐선 헷갈린다). 위도는 33~39.5, 경도는 124~132 범위로 갈라
    이름이 아니라 값으로 확정한다.
    """

    x = _to_float(a)
    y = _to_float(b)
    values = [v for v in (x, y) if v is not None]
    lat = next(
        (v for v in values if KOREA_LAT_MIN <= v <= KOREA_LAT_MAX), None
    )
    lng = next(
        (v for v in values if KOREA_LNG_MIN <= v <= KOREA_LNG_MAX), None
    )
    return lat, lng


def _build_coordinates(
    lat: float | None,
    lng: float | None,
) -> tuple[Coordinates | None, str]:
    """좌표를 만들되 결측·범위 밖이면 (None, 격리사유) 를 돌려준다. 삭제하지 않는다."""

    if lat is None or lng is None:
        return None, "좌표 결측"
    for default_lat, default_lng, label in SUSPECT_DEFAULT_COORDINATES:
        if (
            abs(lat - default_lat) <= SUSPECT_DEFAULT_TOLERANCE_DEG
            and abs(lng - default_lng) <= SUSPECT_DEFAULT_TOLERANCE_DEG
        ):
            return None, (
                f"지오코딩 실패 기본값 의심({label} 좌표 lat={lat}, lng={lng}) — "
                "주소 기반 재산출 필요"
            )
    if not (KOREA_LAT_MIN <= lat <= KOREA_LAT_MAX and KOREA_LNG_MIN <= lng <= KOREA_LNG_MAX):
        return None, f"좌표 한반도 범위 밖(lat={lat}, lng={lng})"
    if not (
        JEONBUK_LAT_MIN <= lat <= JEONBUK_LAT_MAX
        and JEONBUK_LNG_MIN <= lng <= JEONBUK_LNG_MAX
    ):
        return None, f"좌표 전북 범위 밖(lat={lat}, lng={lng})"
    return Coordinates(lat=lat, lng=lng), ""


def _assemble(
    source_id: str,
    origin: str,
    records: list[LocalSourceRecord],
) -> tuple[
    tuple[LocalSourceRecord, ...],
    tuple[LocalSourceRecord, ...],
    tuple[LocalSourceRecord, ...],
]:
    """분류 결과를 records/quarantined/excluded 세 갈래로 나눈다."""

    keep: list[LocalSourceRecord] = []
    quarantined: list[LocalSourceRecord] = []
    excluded: list[LocalSourceRecord] = []
    for record in records:
        if record.status_class == "excluded":
            excluded.append(record)
        elif record.quarantined:
            quarantined.append(record)
        else:
            keep.append(record)
    return tuple(keep), tuple(quarantined), tuple(excluded)


# ── 원본(raw) 원천 로더 ──────────────────────────────────────────────────────


def load_factory_registry(config: LocalSourcesConfig) -> SourceLoadResult:
    """factoryON 등록공장 대장(03_06_09_factory_registry.xlsx).

    이 앱의 유일한 "공장 여부" 원천이다. 룰북 원칙 「공장등록 = 유해공장 아님」의
    등록 집합이자, 업종번호(KSIC)로 다목 고시업종 AND 를 판단할 근거를 담는다.

    주의: 이 원본에는 PNU·좌표가 없다. 회사명·공장주소(지번/도로명)·업종번호만 있다.
    따라서 PNU 우선 매칭에 쓸 factoryON PNU 집합은 이 원본이 아니라 표준본에서
    이미 지번→PNU 조립을 마친 공장 레코드(dataset_id=2, middle='공장')에서 얻는다.
    factory_registry_pnus() 를 따로 둔 이유가 이것이다.
    """

    label = "factoryON 등록공장"
    data = _raw_bytes(config, HAZARD_DIR, "03_06_09_factory_registry.xlsx")
    if data is None:
        return SourceLoadResult(
            source_id="factory_registry",
            label=label,
            loaded=False,
            origin="raw",
            path=_raw_display_path(config, HAZARD_DIR, "03_06_09_factory_registry.xlsx"),
            note="원본 미적재 — LH_LOCAL_RAW_PATH 미설정 또는 파일 부재",
        )
    rows = _xlsx_rows(data)
    records: list[LocalSourceRecord] = []
    for index, row in enumerate(rows):
        record_id = row.get("순번", "").strip() or str(index + 1)
        ksic = _split_ksic(row.get("업종번호", ""))
        records.append(
            LocalSourceRecord(
                source_id="factory_registry",
                record_id=record_id,
                name=row.get("회사명", "").strip(),
                address=row.get("공장주소(지번)", "").strip(),
                road_address=row.get("공장주소(도로명)", "").strip(),
                coordinates=None,
                status_text="",
                status_class="active",  # 등록 대장 = 유효 등록 상태
                category=row.get("업종명", "").strip(),
                ksic_codes=ksic,
                origin="raw",
                extra={
                    "산업단지명": row.get("산업단지명", ""),
                    "공장규모": row.get("공장규모", ""),
                    "대표업종": row.get("대표업종", ""),
                    "용도지역": row.get("용도지역", ""),
                },
            )
        )
    keep, quarantined, excluded = _assemble("factory_registry", "raw", records)
    return SourceLoadResult(
        source_id="factory_registry",
        label=label,
        loaded=True,
        origin="raw",
        path=_raw_display_path(config, HAZARD_DIR, "03_06_09_factory_registry.xlsx"),
        records=keep,
        quarantined=quarantined,
        excluded=excluded,
        note="좌표·PNU 없음(주소·업종번호만). PNU 대조는 표준본 공장 레코드로 한다.",
    )


def _split_ksic(text: str) -> tuple[str, ...]:
    """'20312, 20313' 같은 업종번호 문자열을 코드 튜플로 나눈다."""

    if not text:
        return ()
    parts = [chunk.strip() for chunk in str(text).replace(";", ",").split(",")]
    return tuple(code for code in parts if code)


def load_factory_prohibit_list(config: LocalSourcesConfig) -> ProhibitList:
    """국토부 고시업종 목록(factory_prohibit_list.csv).

    다목 AND 조건의 한 축. factoryON 공장의 업종번호(KSIC)가 이 집합에 들면 고시업종
    공장이다. 코드 집합과 (코드, 업종명) 목록을 함께 돌려준다.
    """

    data = _raw_bytes(config, HAZARD_DIR, "factory_prohibit_list.csv")
    display = _raw_display_path(config, HAZARD_DIR, "factory_prohibit_list.csv")
    if data is None:
        return ProhibitList(
            loaded=False,
            path=display,
            note="원본 미적재 — LH_LOCAL_RAW_PATH 미설정 또는 파일 부재",
        )
    _, rows = _decode_csv(data)
    entries: list[tuple[str, str]] = []
    codes: set[str] = set()
    for row in rows:
        code = (row.get("한국표준산업분류번호") or "").strip()
        name = (row.get("업종") or "").strip()
        if not code:
            continue
        codes.add(code)
        entries.append((code, name))
    return ProhibitList(
        loaded=True,
        path=display,
        ksic_codes=frozenset(codes),
        entries=tuple(entries),
        note=f"{len(codes)}개 고시업종 코드",
    )


def _load_raw_csv_source(
    config: LocalSourcesConfig,
    *,
    source_id: str,
    label: str,
    filename: str,
    subdir: str,
    name_col: str,
    address_col: str,
    id_col: str | None = None,
    road_col: str | None = None,
    status_col: str | None = None,
    category_col: str | None = None,
    lat_col: str | None = None,
    lng_col: str | None = None,
    extra_cols: Sequence[str] = (),
    note: str = "",
) -> SourceLoadResult:
    """컬럼 매핑만 다른 CSV 원천을 공통 규칙으로 정규화한다."""

    data = _raw_bytes(config, subdir, filename)
    display = _raw_display_path(config, subdir, filename)
    if data is None:
        return SourceLoadResult(
            source_id=source_id,
            label=label,
            loaded=False,
            origin="raw",
            path=display,
            note="원본 미적재 — LH_LOCAL_RAW_PATH 미설정 또는 파일 부재",
        )
    _, rows = _decode_csv(data)
    records = _normalize_rows(
        rows,
        source_id=source_id,
        name_col=name_col,
        address_col=address_col,
        id_col=id_col,
        road_col=road_col,
        status_col=status_col,
        category_col=category_col,
        lat_col=lat_col,
        lng_col=lng_col,
        extra_cols=extra_cols,
    )
    keep, quarantined, excluded = _assemble(source_id, "raw", records)
    return SourceLoadResult(
        source_id=source_id,
        label=label,
        loaded=True,
        origin="raw",
        path=display,
        records=keep,
        quarantined=quarantined,
        excluded=excluded,
        note=note,
    )


def _normalize_rows(
    rows: Iterable[Mapping[str, str]],
    *,
    source_id: str,
    name_col: str,
    address_col: str,
    id_col: str | None,
    road_col: str | None,
    status_col: str | None,
    category_col: str | None,
    lat_col: str | None,
    lng_col: str | None,
    extra_cols: Sequence[str] = (),
) -> list[LocalSourceRecord]:
    has_coords = bool(lat_col and lng_col)
    records: list[LocalSourceRecord] = []
    for index, row in enumerate(rows):
        extra = {
            col: (row.get(col) or "").strip()
            for col in extra_cols
            if (row.get(col) or "").strip()
        }
        name = (row.get(name_col) or "").strip()
        address = (row.get(address_col) or "").strip()
        road = (row.get(road_col) or "").strip() if road_col else ""
        status_text = (row.get(status_col) or "").strip() if status_col else ""
        category = (row.get(category_col) or "").strip() if category_col else ""
        record_id = (row.get(id_col) or "").strip() if id_col else ""
        if not record_id:
            record_id = f"{source_id}-{index + 1}"

        status_class, review_reason = classify_status(
            status_text, has_status_field=bool(status_col)
        )

        coordinates: Coordinates | None = None
        quarantined = False
        quarantine_reason = ""
        if has_coords:
            lat, lng = coerce_lat_lng(row.get(lat_col), row.get(lng_col))
            coordinates, quarantine_reason = _build_coordinates(lat, lng)
            quarantined = coordinates is None

        records.append(
            LocalSourceRecord(
                source_id=source_id,
                record_id=record_id,
                name=name,
                address=address,
                road_address=road,
                coordinates=coordinates,
                status_text=status_text,
                status_class=status_class,
                category=category,
                quarantined=quarantined,
                quarantine_reason=quarantine_reason,
                review_reason=review_reason,
                origin="raw",
                extra=extra,
            )
        )
    return records


def _flag_copied_coordinates(
    result: SourceLoadResult,
    *,
    region_key: str,
) -> SourceLoadResult:
    """같은 좌표를 서로 다른 시군의 행이 공유하면 「좌표 복사 의심」으로 검토 표시.

    H-02-나 §6-4 ③ — 동명 업소(호남가스충전소 전주·정읍)에서 전주 건 좌표가 정읍
    건에 그대로 복사돼 있었다. 적재 단계에서는 어느 쪽이 맞는지 알 수 없으므로
    삭제·격리하지 않고 둘 다 검토(review)로 낮춰 확정 판정에 쓰이지 않게 한다.
    """

    groups: dict[tuple[float, float], list[LocalSourceRecord]] = {}
    for record in result.records:
        if record.coordinates is None:
            continue
        key = (round(record.coordinates.lat, 6), round(record.coordinates.lng, 6))
        groups.setdefault(key, []).append(record)
    suspect: set[str] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        regions = {_name_key(m.extra.get(region_key, "")) for m in members}
        if len(regions) > 1:
            suspect.update(m.record_id for m in members)
    if not suspect:
        return result
    flagged = tuple(
        dc_replace(
            r,
            status_class="review" if r.status_class == "active" else r.status_class,
            review_reason=(
                f"{r.review_reason} · " if r.review_reason else ""
            )
            + "동명·이지역 좌표 복사 의심(같은 좌표를 다른 시군 행이 공유) — "
            "주소 기반 재산출 전 확정 금지 (H-02-나 §6-4)",
        )
        if r.record_id in suspect
        else r
        for r in result.records
    )
    return dc_replace(result, records=flagged)


def load_gas_stations(config: LocalSourcesConfig) -> SourceLoadResult:
    """주유소(11_gas_stations.csv) — ⚠️ 판정 원천이 아니다.

    이 파일은 현재상태 목록이 아니라 산업통상자원부 2015~2025년 등록 변동사유
    (신규등록/휴업/폐업/등록취소) 이력이다. 719건 중 654건(90.9%)이 휴업·폐업·취소
    기록이고 내용은 석유판매업 원장(12_oil_retailers)에 이미 반영돼 있다. 따로 쓰면
    말소된 비영업 주유소 83~111곳이 되살아난다(H-02-가 §6-2). 그래서 판정에는
    적재하지 않고(local_wiring 이 읽지 않는다) 갱신 감지 참고용으로만 남긴다.
    구분을 영업상태로 보아 폐업·등록취소는 제외, 휴업은 검토로 남긴다. 좌표는 없다.
    """

    return _load_raw_csv_source(
        config,
        source_id="gas_stations",
        label="주유소",
        filename="11_gas_stations.csv",
        subdir=HAZARD_DIR,
        name_col="업체명",
        address_col="소재지",
        status_col="구분",
        category_col="판매업종류",
        note=(
            "변동사유 이력(현재상태 아님) — 판정 원천에서 제외(H-02-가 §6-2). "
            "좌표 없음."
        ),
    )


def load_lpg_charging_stations(config: LocalSourcesConfig) -> SourceLoadResult:
    """LPG 자동차 충전소(13_lpg_charging_stations.csv).

    좌표 컬럼명이 헷갈린다(LAT 에 위도, LOT 에 경도). 값 범위로 판별해 확정한다.
    """

    result = _load_raw_csv_source(
        config,
        source_id="lpg_charging_stations",
        label="LPG 충전소",
        filename="13_lpg_charging_stations.csv",
        subdir=HAZARD_DIR,
        name_col="BSES_NM",
        address_col="ADDR",
        category_col="MGT_NM",
        lat_col="LAT",
        lng_col="LOT",
        extra_cols=("SECT_NM",),
        note=(
            "좌표 컬럼명 혼동(LAT/LOT) — 값 범위로 lat/lng 판별. 원본 좌표 19건(14%)이 "
            "1km 이상 오류(H-02-나 §6-4): 서울시청 기본값·전북 밖 좌표는 격리, 다른 "
            "시군 행과 좌표가 같은 건은 검토로 낮춘다."
        ),
    )
    if not result.loaded:
        return result
    return _flag_copied_coordinates(result, region_key="SECT_NM")


def load_lpg_retailers(config: LocalSourcesConfig) -> SourceLoadResult:
    """LPG 판매소(14_lpg_retailers.csv). cp949. 좌표 없음."""

    return _load_raw_csv_source(
        config,
        source_id="lpg_retailers",
        label="LPG 판매소",
        filename="14_lpg_retailers.csv",
        subdir=HAZARD_DIR,
        name_col="업소명",
        address_col="주소",
        id_col="일련번호",
        note="cp949. 좌표 없음 — 통합 시 주소 지오코딩 필요.",
    )


def load_gas_product_manufacturers(config: LocalSourcesConfig) -> SourceLoadResult:
    """도시가스 제조시설/가스용품 제조(22_gas_product_manufacturers.csv). 영업상태 보유.

    H-02-아 §6-3 — 28행은 「업소 × 생산품목」 행이라 고유 업소는 18곳이다. 그대로
    적재하면 같은 시설이 품목 수만큼 걸리므로 업소명·소재지로 접고 품목을 합친다.
    """

    result = _load_raw_csv_source(
        config,
        source_id="gas_product_manufacturers",
        label="도시가스 제조시설",
        filename="22_gas_product_manufacturers.csv",
        subdir=HAZARD_DIR,
        name_col="업소명",
        address_col="소재지",
        status_col="영업상태",
        category_col="생산품목",
        extra_cols=("법구분", "행정구역"),
        note=(
            "좌표 없음 — 통합 시 주소 지오코딩 필요(28건 전건 성공, H-02-아 §6-4). "
            "업소 × 생산품목 행을 고유 업소로 접는다(§6-3)."
        ),
    )
    if not result.loaded:
        return result
    return dc_replace(
        result,
        records=_merge_business_rows(result.records),
        quarantined=_merge_business_rows(result.quarantined),
        excluded=_merge_business_rows(result.excluded),
    )


def _merge_business_rows(
    records: tuple[LocalSourceRecord, ...],
) -> tuple[LocalSourceRecord, ...]:
    """업소명·소재지가 같은 행을 한 건으로 접고 품목(category)을 「·」로 합친다."""

    merged: dict[tuple[str, str], LocalSourceRecord] = {}
    categories: dict[tuple[str, str], list[str]] = {}
    counts: dict[tuple[str, str], int] = {}
    for record in records:
        key = (_name_key(record.name), _name_key(record.address))
        counts[key] = counts.get(key, 0) + 1
        items = categories.setdefault(key, [])
        if record.category and record.category not in items:
            items.append(record.category)
        merged.setdefault(key, record)
    result: list[LocalSourceRecord] = []
    for key, record in merged.items():
        extra = dict(record.extra)
        extra["merged_rows"] = str(counts[key])
        result.append(
            dc_replace(record, category="·".join(categories[key]), extra=extra)
        )
    return tuple(result)


def load_cng_stations(config: LocalSourcesConfig) -> SourceLoadResult:
    """CNG 충전소(25_cng_stations.csv). cp949. 위도/경도 컬럼 보유."""

    result = _load_raw_csv_source(
        config,
        source_id="cng_stations",
        label="CNG 충전소",
        filename="25_cng_stations.csv",
        subdir=HAZARD_DIR,
        name_col="시설명",
        address_col="주소",
        id_col="순번",
        lat_col="위도",
        lng_col="경도",
        extra_cols=("행정구역", "위도", "경도"),
        note=(
            "cp949. 원본 위경도 8건 전부 714~3,779m 오류 → 시설명 대조로 VWorld 실측 "
            "좌표 교체(H-03 §6-2). 영업상태·지번주소 없음."
        ),
    )
    if not result.loaded:
        return result
    corrected = [_apply_cng_correction(r) for r in (*result.records, *result.quarantined)]
    keep, quarantined, excluded = _assemble("cng_stations", "raw", corrected)
    return dc_replace(
        result,
        records=keep,
        quarantined=quarantined,
        excluded=(*result.excluded, *excluded),
    )


def _apply_cng_correction(record: LocalSourceRecord) -> LocalSourceRecord:
    """시설명이 실측표에 있으면 좌표를 교체하고 원본값·근거를 extra 에 남긴다."""

    verified = CNG_VERIFIED_COORDINATES.get(_name_key(record.name))
    if verified is None:
        return record
    lat, lng = verified
    extra = dict(record.extra)
    extra["original_lat"] = extra.pop("위도", "")
    extra["original_lng"] = extra.pop("경도", "")
    extra["coordinate_basis"] = CNG_COORDINATE_BASIS
    return dc_replace(
        record,
        coordinates=Coordinates(lat=lat, lng=lng),
        quarantined=False,
        quarantine_reason="",
        extra=extra,
    )


def load_knowledge_industry_centers(config: LocalSourcesConfig) -> SourceLoadResult:
    """지식산업센터(10_knowledge_industry_centers.csv).

    공장 예외 처리에 쓰인다(지식산업센터 내 공장은 별도 취급). cp949, 좌표 없음.
    상태 컬럼('[변경완료신고]' 등)은 인허가 진행단계라 영업상태가 아니어서 검토
    분류에 넣지 않는다(status_col 미지정 → active).
    """

    return _load_raw_csv_source(
        config,
        source_id="knowledge_industry_centers",
        label="지식산업센터",
        filename="10_knowledge_industry_centers.csv",
        subdir=HAZARD_DIR,
        name_col="지식산업센터명",
        address_col="공장대표주소(지번)",
        road_col="공장대표주소(도로명)",
        category_col="입지구분",
        note="cp949. 좌표 없음. 상태는 인허가 진행단계라 영업상태로 보지 않음.",
    )


# ── 표준본(standard) 원천 로더 ───────────────────────────────────────────────

# 표준본 시트 이름과 컬럼(실측). 전 행 좌표+PNU 보유, location_status=SUCCESS.
STD_HAZARD_SHEET = "유해시설"
STD_AMENITY_SHEET = "생활편의시설"

# 표준본에서 바로 뽑아 쓰는 명명 원천. (시트, dataset_id, middle_category|None).
# 표준본이 이미 덮는 범위이므로 원본 대신 이쪽을 정본으로 쓴다.
STANDARD_SOURCE_FILTERS: dict[str, tuple[str, str, str | None, str]] = {
    "factory_standard": (STD_HAZARD_SHEET, "2", "공장", "표준 공장(PNU 보유)"),
    "hazardous_material_standard": (
        STD_HAZARD_SHEET,
        "2",
        "위험물 저장 및 처리 시설",
        "표준 위험물 저장·처리시설",
    ),
    "amusement_standard": (STD_HAZARD_SHEET, "1", None, "표준 위락시설"),
    "lodging_standard": (STD_HAZARD_SHEET, "3", None, "표준 숙박시설"),
    "traditional_markets": (STD_AMENITY_SHEET, "16", None, "전통시장"),
}


def load_standard_source(
    config: LocalSourcesConfig,
    source_id: str,
) -> SourceLoadResult:
    """표준본에서 명명 원천 하나를 정규화해 돌려준다."""

    if source_id not in STANDARD_SOURCE_FILTERS:
        raise KeyError(f"알 수 없는 표준 원천: {source_id}")
    sheet, dataset_id, middle, label = STANDARD_SOURCE_FILTERS[source_id]
    display = str(config.standard_path) if config.standard_path else "(미설정)"
    rows = _standard_rows(config, sheet)
    if rows is None:
        return SourceLoadResult(
            source_id=source_id,
            label=label,
            loaded=False,
            origin="standard",
            path=display,
            note="표준본 미적재 — LH_LOCAL_STANDARD_PATH 미설정 또는 파일 부재",
        )
    records = _standard_records(rows, source_id, dataset_id, middle)
    keep, quarantined, excluded = _assemble(source_id, "standard", records)
    return SourceLoadResult(
        source_id=source_id,
        label=label,
        loaded=True,
        origin="standard",
        path=display,
        records=keep,
        quarantined=quarantined,
        excluded=excluded,
        note="표준본 정본(좌표·PNU 보유).",
    )


def _standard_rows(
    config: LocalSourcesConfig,
    sheet: str,
) -> list[dict[str, str]] | None:
    """표준본 시트 한 장을 dict 목록으로 읽는다. 파일 없으면 None."""

    path = config.standard_path
    if path is None or not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return _xlsx_rows(data, sheet=sheet)
    except KeyError:
        return []


def _standard_records(
    rows: Iterable[Mapping[str, str]],
    source_id: str,
    dataset_id: str,
    middle: str | None,
) -> list[LocalSourceRecord]:
    records: list[LocalSourceRecord] = []
    for row in rows:
        if str(row.get("dataset_id", "")).strip() != dataset_id:
            continue
        if middle is not None and (row.get("middle_category") or "").strip() != middle:
            continue
        lat = _to_float(row.get("latitude"))
        lng = _to_float(row.get("longitude"))
        coordinates, quarantine_reason = _build_coordinates(lat, lng)
        records.append(
            LocalSourceRecord(
                source_id=source_id,
                record_id=(row.get("facility_id") or "").strip(),
                name=(row.get("facility_name") or "").strip(),
                address=(row.get("address") or "").strip(),
                coordinates=coordinates,
                status_text="",
                status_class="active",
                category=(row.get("middle_category") or "").strip(),
                pnu=(row.get("pnu") or "").strip(),
                quarantined=coordinates is None,
                quarantine_reason=quarantine_reason,
                origin="standard",
                extra={
                    "location_status": row.get("location_status", ""),
                    "coordinate_source": row.get("coordinate_source", ""),
                    "pnu_source": row.get("pnu_source", ""),
                },
            )
        )
    return records


# ── PNU 매칭 (룰북 §3) ───────────────────────────────────────────────────────


def pnus_of(records: Iterable[LocalSourceRecord]) -> frozenset[str]:
    """레코드 집합의 PNU 우선키 집합. 빈 PNU 는 뺀다."""

    return frozenset(r.pnu for r in records if r.pnu)


def factory_registry_pnus(config: LocalSourcesConfig) -> PnuSetResult:
    """factoryON 등록공장의 PNU 집합.

    원본 factory_registry 에는 PNU 가 없다. 그래서 표준본에서 지번→PNU 조립을 마친
    공장 레코드(dataset_id=2, middle='공장')의 PNU 를 factoryON 등록공장 PNU 집합의
    정본으로 삼는다. 이 집합을 대기배출·소음 후보(위험물 저장·처리시설 등)의 PNU 와
    대조하면, 같은 필지에 등록공장이 있는지를 문자열 유사매칭 없이 확정할 수 있다.
    """

    result = load_standard_source(config, "factory_standard")
    if not result.loaded:
        return PnuSetResult(
            loaded=False,
            origin="standard",
            path=result.path,
            note=result.note,
        )
    pnus = pnus_of((*result.records, *result.quarantined))
    # 파일이 있어도 유효 PNU 가 0개면 공장 AND 대조를 할 수 없다. 파일 존재를
    # loaded 로 보면 좌표·PNU 해석 가능한 공장 스냅샷이 없는데도 「충돌 없음」이
    # 나온다(룰북 §3). 최소 1개의 유효 PNU 가 있어야 loaded 로 인정한다.
    if not pnus:
        return PnuSetResult(
            loaded=False,
            origin="standard",
            path=result.path,
            note=(
                f"표준 공장 {len(result.records) + len(result.quarantined)}행이 있으나 "
                "유효 PNU 가 0개라 AND 대조 불가(미적재로 취급)."
            ),
        )
    return PnuSetResult(
        loaded=True,
        origin="standard",
        path=result.path,
        pnus=pnus,
        note=f"표준 공장 {len(result.records) + len(result.quarantined)}행 중 "
        f"PNU {len(pnus)}개",
    )


def match_by_pnu(
    left: Iterable[LocalSourceRecord],
    right: Iterable[LocalSourceRecord],
) -> list[tuple[LocalSourceRecord, LocalSourceRecord]]:
    """두 원천을 PNU 우선키로만 매칭한다(룰북 §3, 문자열 유사매칭 금지)."""

    index: dict[str, list[LocalSourceRecord]] = {}
    for record in right:
        if record.pnu:
            index.setdefault(record.pnu, []).append(record)
    pairs: list[tuple[LocalSourceRecord, LocalSourceRecord]] = []
    for record in left:
        if not record.pnu:
            continue
        for counterpart in index.get(record.pnu, ()):
            pairs.append((record, counterpart))
    return pairs


# ── 오케스트레이션 ───────────────────────────────────────────────────────────

# 통합 단계가 순회할 raw 시설 원천 로더. prohibit list 는 시설이 아니라 별도.
RAW_SOURCE_LOADERS = {
    "factory_registry": load_factory_registry,
    "gas_stations": load_gas_stations,
    "lpg_charging_stations": load_lpg_charging_stations,
    "lpg_retailers": load_lpg_retailers,
    "gas_product_manufacturers": load_gas_product_manufacturers,
    "cng_stations": load_cng_stations,
    "knowledge_industry_centers": load_knowledge_industry_centers,
}


def load_all(config: LocalSourcesConfig) -> dict[str, SourceLoadResult]:
    """명명 시설 원천 전체를 적재해 source_id 로 반환한다.

    표준본이 덮는 원천은 표준본에서, 그렇지 않은 원천(factoryON 등록공장·세부
    가스 원천 등)은 원본에서 읽는다. 파일이 없는 원천은 loaded=False 로 그대로
    포함해, 무엇이 미적재인지 통합 단계가 한눈에 보게 한다. 저장은 하지 않는다.
    """

    results: dict[str, SourceLoadResult] = {}
    for source_id in STANDARD_SOURCE_FILTERS:
        results[source_id] = load_standard_source(config, source_id)
    for source_id, loader in RAW_SOURCE_LOADERS.items():
        results[source_id] = loader(config)
    return results


def _raw_display_path(config: LocalSourcesConfig, subdir: str, filename: str) -> str:
    root = config.raw_path
    if root is None:
        return f"(미설정)/{subdir}/{filename}"
    if root.is_file():
        return f"{root}!{subdir}/{filename}"
    return str(root / subdir / filename)


__all__ = [
    "ENV_RAW_PATH",
    "ENV_STANDARD_PATH",
    "LocalSourceRecord",
    "LocalSourcesConfig",
    "PnuSetResult",
    "ProhibitList",
    "SourceLoadResult",
    "classify_status",
    "coerce_lat_lng",
    "factory_registry_pnus",
    "load_all",
    "load_cng_stations",
    "load_factory_prohibit_list",
    "load_factory_registry",
    "load_gas_product_manufacturers",
    "load_gas_stations",
    "load_knowledge_industry_centers",
    "load_lpg_charging_stations",
    "load_lpg_retailers",
    "load_standard_source",
    "match_by_pnu",
    "pnus_of",
]
