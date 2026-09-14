"""인허가 원장 적재의 영업상태 필터.

휴업 시설을 적재 단계에서 버리면, 판정층(`rulebook.operating_state`)이 판정에
올릴 수 없다. 적재된 172,770행의 상태값이 「영업/정상」 하나뿐이었고, 휴업 주유소가
기준거리 안에 있어도 앱이 잡지 못했다(2026-09-11 LH 회의 안건 ② · 041 진북동 사례).

LH 확정 2026-09-11: 휴업은 판정에 포함한다. 적재는 폐업만 버리고, 판정층은 휴업을
영업과 같이 판정 대상(active)으로 본다. 상태 공란만 확정하지 않고 검토로 남긴다.
"""

from __future__ import annotations

from app.hazard_review.rulebook import operating_state
from app.services.localdata import DATASETS, parse_record

# 실측 코드(2026-09-11 석유판매업 100행): 01 영업/정상 · 02 휴업 · 03 폐업.
_OPEN, _SUSPENDED, _CLOSED = "01", "02", "03"

_DATASET = DATASETS[0]


def _row(status_code: str, status_name: str) -> dict[str, object]:
    """전주 시내 좌표(EPSG:5174)를 가진 최소 응답 행."""

    return {
        "SALS_STTS_CD": status_code,
        "SALS_STTS_NM": status_name,
        "CRD_INFO_X": "196880.0",
        "CRD_INFO_Y": "298540.0",
        "MNG_NO": f"test-{status_code}",
        "BPLC_NM": "검증용 시설",
        "LOTNO_ADDR": "전북특별자치도 전주시 덕진구 진북동 1-1",
    }


def test_open_and_suspended_are_ingested_closed_is_not() -> None:
    assert parse_record(_DATASET, _row(_OPEN, "영업/정상")) is not None
    assert parse_record(_DATASET, _row(_SUSPENDED, "휴업")) is not None
    assert parse_record(_DATASET, _row(_CLOSED, "폐업")) is None


def test_suspended_record_is_judged_as_active() -> None:
    """적재한 휴업 행이 판정층에서 영업과 같이 판정 대상(active)으로 갈리는지 확인한다.

    LH 확정 2026-09-11 (안건 ②): 휴업은 일시 중단이므로 시설이 있는 것으로 보아
    판정에 포함한다. 운영상태 표기(「휴업」)는 status 에 그대로 남는다.
    """

    record = parse_record(_DATASET, _row(_SUSPENDED, "휴업"))
    assert record is not None
    assert record.status == "휴업"
    assert operating_state(record.status) == "active"

    open_record = parse_record(_DATASET, _row(_OPEN, "영업/정상"))
    assert open_record is not None
    assert operating_state(open_record.status) == "active"

    # 상태 공란은 여전히 확정하지 않고 검토(hold)로 남긴다.
    assert operating_state("") == "hold"
