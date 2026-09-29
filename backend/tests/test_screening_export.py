"""결과 Excel — 4시트(종합요약·1차 상세·2차 상세·데이터 스냅샷)와 담당자 판단 반영."""

from __future__ import annotations

import io
import os
from datetime import UTC, datetime

os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("KAKAO_REST_API_KEY", "")

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.main import app
from app.screening.batch import BatchRowStatus, BatchStatus, batch_results, batches
from app.screening.export_xlsx import ExportSite, ItemJudgement, build_workbook
from app.screening.models import ScreeningRequest, ScreeningResult
from app.services.facility_store import SyncState
from app.screening.service import ScreeningService

from tests.test_screening_scorebook import (  # noqa: E402
    TWO_TRANSIT,
    FakeAmenityCollector,
    FakeHazardService,
    category,
    collections,
    site,
)

SHEETS = ["종합요약", "1차 상세", "2차 상세", "데이터 스냅샷"]


class FakeStore:
    def sync_states(self) -> list[SyncState]:
        return [
            SyncState("lodgings", datetime(2026, 9, 1, tzinfo=UTC), 10),
            SyncState("gas_station", datetime(2026, 9, 20, tzinfo=UTC), 5),
            SyncState("empty", datetime(2026, 9, 25, tzinfo=UTC), 0),
        ]


async def _result() -> ScreeningResult:
    hazard = FakeHazardService([
        category("gas", "exclusion_match", label="주유소", nearest=20),
        category("lpg", "no_conflict_in_snapshot", label="LPG 충전소"),
    ])
    hazard.facility_store = FakeStore()  # type: ignore[attr-defined]
    service = ScreeningService(
        hazard=hazard,  # type: ignore[arg-type]
        amenities=FakeAmenityCollector(collections(**TWO_TRANSIT)),  # type: ignore[arg-type]
    )
    return await service.screen(ScreeningRequest(site=site()))


def _rows(data: bytes, sheet: str) -> list[tuple]:
    book = load_workbook(io.BytesIO(data))
    return [tuple(r) for r in book[sheet].iter_rows(values_only=True)]


@pytest.mark.asyncio
async def test_result_carries_data_snapshot() -> None:
    result = await _result()
    snapshot = result.data_snapshot
    assert snapshot is not None
    # 레코드 0건 데이터셋은 판에서 뺀다.
    assert snapshot.localdata_dataset_count == 2
    assert snapshot.localdata_record_count == 15
    assert snapshot.localdata_synced_at_min == datetime(2026, 9, 1, tzinfo=UTC)
    assert snapshot.localdata_synced_at_max == datetime(2026, 9, 20, tzinfo=UTC)
    assert {o.key for o in snapshot.options} >= {"bus_headway_filter"}


@pytest.mark.asyncio
async def test_workbook_has_four_sheets_with_judgements() -> None:
    result = await _result()
    data = build_workbook([
        ExportSite(
            receipt_no="001",
            result=result,
            judgements={"gas": ItemJudgement(state="false_positive", memo="현장 확인 폐업")},
        ),
        ExportSite(receipt_no="002", address="없는주소", error="주소를 찾지 못했습니다."),
    ])

    assert load_workbook(io.BytesIO(data)).sheetnames == SHEETS

    summary = _rows(data, "종합요약")
    assert summary[0][:4] == ("접수번호", "소재지", "분류", "신청유형")
    assert summary[1][0] == "001" and summary[1][4] == result.stage_one.verdict_label
    assert summary[1][13] == "1/2"  # 담당자 확인 1건/2항목
    assert summary[2][0] == "002" and summary[2][-1] == "주소를 찾지 못했습니다."

    one = _rows(data, "1차 상세")
    head = one[0]
    gas = next(r for r in one[1:] if r[3] == "주유소")
    assert gas[head.index("담당자 판단")] == "오탐"
    assert gas[head.index("메모")] == "현장 확인 폐업"
    lpg = next(r for r in one[1:] if r[3] == "LPG 충전소")
    assert lpg[head.index("담당자 판단")] == "미확인"

    two = _rows(data, "2차 상세")
    assert two[0][5] == "시설군" and two[0][7] == "시설명"
    assert any(r[2] == "대중교통 접근성" for r in two[1:])

    snap = dict((r[0], r[1]) for r in _rows(data, "데이터 스냅샷") if r and r[0])
    assert "생성 시각" in snap
    assert snap["규칙팩 버전"].startswith(result.rule_pack_id)
    assert snap["인허가(LOCALDATA) 캐시 동기화"].startswith("2026-09-01")
    assert any(k.startswith("적용 판정 옵션") for k in snap)


@pytest.mark.asyncio
async def test_single_export_endpoint_returns_xlsx() -> None:
    result = await _result()
    client = TestClient(app)
    response = client.post(
        "/api/screening/export.xlsx",
        json={"sites": [{
            "receipt_no": "A-1",
            "result": result.model_dump(mode="json"),
            "judgements": {"gas": {"state": "confirmed", "memo": ""}},
        }]},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    one = _rows(response.content, "1차 상세")
    gas = next(r for r in one[1:] if r[3] == "주유소")
    assert gas[one[0].index("담당자 판단")] == "확인 완료"


@pytest.mark.asyncio
async def test_batch_export_endpoint_uses_server_results_and_judgements() -> None:
    result = await _result()
    batch = BatchStatus(
        batch_id="bx", status="completed", created_at=datetime.now(UTC),
        housing_type="house", application_type="general", total=2,
        rows=[
            BatchRowStatus(id="1", address="전주시 A", housing_type="house", application_type="general",
                           status="completed", screening_id=result.screening_id),
            BatchRowStatus(id="2", address="없는주소", housing_type="house", application_type="general",
                           status="failed", error="주소를 찾지 못했습니다.", message="실패"),
        ],
    )
    batches["bx"] = batch
    batch_results[("bx", "1")] = result
    try:
        client = TestClient(app)
        response = client.post(
            "/api/screening/batch/bx/export.xlsx",
            json={"judgements": {result.screening_id: {"gas": {"state": "evidence_requested", "memo": "공문 요청"}}}},
        )
        assert response.status_code == 200
        summary = _rows(response.content, "종합요약")
        assert [r[0] for r in summary[1:]] == ["1", "2"]
        assert summary[2][-1] == "주소를 찾지 못했습니다."
        one = _rows(response.content, "1차 상세")
        gas = next(r for r in one[1:] if r[3] == "주유소")
        assert gas[0] == "1"
        assert gas[one[0].index("담당자 판단")] == "추가 증빙 요청"
        assert gas[one[0].index("메모")] == "공문 요청"

        assert client.post("/api/screening/batch/nope/export.xlsx", json={}).status_code == 404
    finally:
        batches.pop("bx", None)
        batch_results.pop(("bx", "1"), None)

