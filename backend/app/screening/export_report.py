"""일괄 심사 결과 내려받기 — 화면 결과표 한 장 + 건별 심사표.

Excel(.xlsx)과 PDF(.pdf)가 같은 내용을 내도록, 화면(BatchPanel 결과표·ScreeningSheet
심사표)이 보여 주는 것을 먼저 「보고서 블록」(제목·항목값·표·목록)으로 옮기고, 두 파일
형식은 그 블록을 받아 적기만 한다. 라벨·순서·표기(「—」·「123.4m」·「33/40」)는 화면과
같게 둔다 — 담당자가 화면에서 본 것과 내려받은 것이 달라 보이면 안 된다.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from app.kst import KST
from app.screening.export_xlsx import (
    JUDGEMENT_LABELS,
    MEASUREMENT_TIER_LABELS,
    ExportSite,
    ItemJudgement,
    _site_labels,
)
from app.screening.models import (
    ScreeningCriterion,
    ScreeningExclusionItem,
    ScreeningFacilityHit,
    ScreeningRequirement,
    ScreeningResult,
)

if TYPE_CHECKING:
    from app.screening.batch import BatchRowStatus, BatchStatus


# ---------------------------------------------------------------------------
# 보고서 블록
# ---------------------------------------------------------------------------
@dataclass
class Block:
    """심사표 한 토막. kind 에 따라 text/rows 를 쓴다.

    heading  — text 제목(level 1·2)
    text     — 문단(style: normal·muted·strong·warning)
    kv       — rows = [(항목, 값)]
    table    — columns 머리글, rows 본문, sub 는 시설 목록처럼 들여쓴 행의 인덱스
    bullets  — rows = [문장]
    """

    kind: str
    text: str = ""
    level: int = 1
    style: str = "normal"
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    # 표 열 너비 비율(PDF 용). 비면 균등.
    weights: list[float] = field(default_factory=list)
    # 들여쓴 하위 행(시설 목록)·굵은 묶음 행의 인덱스.
    sub_rows: set[int] = field(default_factory=set)
    group_rows: set[int] = field(default_factory=set)


@dataclass
class SiteReport:
    """사업지 한 건의 심사표."""

    receipt_no: str
    title: str
    subtitle: str
    blocks: list[Block]


@dataclass
class BatchReport:
    """내려받기 한 벌 — 결과표 한 장과 건별 심사표."""

    title: str
    generated_at: datetime
    info: list[tuple[str, str]]
    columns: list[str]
    rows: list[list[str]]
    sites: list[SiteReport]


# ---------------------------------------------------------------------------
# 화면과 같은 표기
# ---------------------------------------------------------------------------
def fmt_distance(value: float | None) -> str:
    """ScreeningSheet formatDistance — 없으면 「—」, 있으면 소수 첫째 자리까지."""

    if value is None:
        return "—"
    return f"{value:.1f}m"


def fmt_threshold(value: int | None) -> str:
    return "—" if value is None else f"{value:,}m"


def fmt_points(value: int | None) -> str:
    return "—" if value is None else str(value)


def stamp(value: datetime | None, with_time: bool = True) -> str:
    if value is None:
        return ""
    local = value.astimezone(KST)
    return local.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def score_text(row: BatchRowStatus) -> str:
    """BatchPanel scoreText + 「/만점」."""

    if row.status != "completed":
        return "—"
    if row.determined and row.living_score is not None:
        text = str(row.living_score)
    elif row.living_score_min is not None and row.living_score_max is not None:
        text = f"{row.living_score_min}~{row.living_score_max}"
    else:
        text = "—"
    if row.living_maximum is not None:
        text += f"/{row.living_maximum}"
    return text


def criterion_text(row: BatchRowStatus, key: str) -> str:
    """BatchPanel criterionText."""

    item = row.bonus if key == "station_area" else next((c for c in row.criteria if c.key == key), None)
    if item is None:
        return "·" if row.status == "completed" else ""
    if item.awarded is not None:
        return str(item.awarded)
    return f"{item.awarded_min}~{item.awarded_max}"


def data_snapshot_text(result: ScreeningResult) -> str:
    """ScreeningSheet dataSnapshotText."""

    snapshot = result.data_snapshot
    if snapshot is None:
        return "데이터 판 기록 없음(이전 심사) · 공공 API 실시간 조회"
    start = stamp(snapshot.localdata_synced_at_min, with_time=False)
    end = stamp(snapshot.localdata_synced_at_max, with_time=False)
    if start:
        span = start if start == end else f"{start}~{end}"
        localdata = (
            f"인허가(LOCALDATA) 캐시 {span} 동기화 · "
            f"{snapshot.localdata_dataset_count}종 {snapshot.localdata_record_count:,}건"
        )
    else:
        localdata = "인허가(LOCALDATA) 캐시 없음"
    parts = [
        f"데이터 판 {localdata}",
        snapshot.live_api_note or "그 밖의 원천은 공공 API 실시간 조회",
    ]
    if snapshot.options:
        parts.append("판정 옵션: " + ", ".join(f"{o.label} {'켬' if o.enabled else '끔'}" for o in snapshot.options))
    if snapshot.relaxed_2027:
        parts.append("2027 완화 기준 적용")
    return " · ".join(parts)


def measurement_short(hit: ScreeningFacilityHit) -> str:
    """screeningOverlays measurementShortLabel."""

    return MEASUREMENT_TIER_LABELS.get(hit.measurement_tier) or hit.measurement_method or "시설 좌표"


def requirement_line(req: ScreeningRequirement) -> str:
    mark = "?" if req.met is None else ("✓" if req.met else "✗")
    return f"{mark} {req.text}" + (f" · {req.evidence}" if req.evidence else "")


def source_kinds(item: ScreeningExclusionItem) -> str:
    """ScreeningSheet renderSourceKinds — 원천 종류 요약."""

    kinds = []
    for source in item.data_sources:
        if source.kind not in kinds:
            kinds.append(source.kind)
    if "api" in kinds:
        kinds = [k for k in kinds if k not in ("partial", "bypass")]
    if "bypass" in kinds:
        kinds = [k for k in kinds if k != "partial"]
    labels = {"api": "API", "local": "로컬", "demo": "데모", "partial": "API 일부연결", "bypass": "API 우회연결"}
    text = " · ".join(labels[k] for k in kinds if k in labels)
    if text:
        return text
    if any(f.metadata.get("reference") for f in item.facilities):
        return "API 일부연결"
    return "API 미연결"


def source_data_labels(item: ScreeningExclusionItem) -> str:
    """심사표 「데이터」 칩 — 붙은 원천 이름."""

    return " / ".join(s.label for s in item.data_sources)


# ---------------------------------------------------------------------------
# 건별 심사표
# ---------------------------------------------------------------------------
_OUTCOME_RANK = {"fail": 3, "review": 2, "pass": 1}


def _site_title(site: ExportSite) -> tuple[str, str]:
    """(제목, 주소 줄) — ScreeningSheet 머리말과 같다."""

    result = site.result
    if result is None:
        return site.address or site.receipt_no, ""
    # 일괄 심사는 site.name 에 접수번호를 넣으므로 제목은 대표 필지 주소로 쓴다.
    address = result.site.address or site.address or "주소 미확보"
    parcels = result.site.parcels
    if len(parcels) > 1:
        return f"{address} 외 {len(parcels) - 1}필지", " · ".join(p.address or p.pnu or "?" for p in parcels)
    return address, ""


def _sentences(text: str) -> list[str]:
    """ScreeningSheet ReasonSentences — 「…다. 」 뒤에서 문장을 나눈다."""

    import re

    parts = [p.strip() for p in re.split(r"(?<=[가-힣]{2}[다요음됨임])\.\s+", text)]
    return [p for p in parts if p] or [text]


def _stage_one_blocks(site: ExportSite, result: ScreeningResult, compact: bool) -> list[Block]:
    one = result.stage_one
    blocks: list[Block] = [
        Block("heading", "1차 매입제외 판정 — 주거환경 저해시설", level=2),
        Block("text", one.summary, style="muted"),
    ]
    if one.checklist_note:
        blocks.append(Block("text", one.checklist_note, style="muted"))

    # 화면과 같이 규칙(rule_id)별로 묶고, 묶음 머리행에는 가장 나쁜 결과를 적는다.
    groups: dict[str, list[ScreeningExclusionItem]] = {}
    for item in one.items:
        groups.setdefault(item.rule_id, []).append(item)
    table = Block(
        "table",
        columns=["항목", "기준거리", "최근접", "결과", "근거", "원천 · 데이터", "측정 기준", "담당자 판단"],
        weights=[3.4, 1.1, 1.1, 1.2, 4.2, 2.6, 2.0, 1.6],
    )
    for items in groups.values():
        worst = max(items, key=lambda i: _OUTCOME_RANK.get(i.outcome, 0))
        facility_count = sum(len(i.facilities) for i in items)
        group_label = "미적용" if all(i.passthrough for i in items) else worst.outcome_label
        head = f"{items[0].rule_label} — 세부 {len(items)}종"
        if facility_count:
            head += f" · 시설 {facility_count}곳"
        table.group_rows.add(len(table.rows))
        table.rows.append([head, fmt_threshold(items[0].threshold_m), "—", group_label, "", "", "", ""])
        # PDF 는 한 쪽에 가깝게 — 「해당 없음」(신청유형에 미적용) 세부 항목은 한 줄로 모은다.
        skipped = [i for i in items if compact and i.outcome == "not_applicable" and not i.facilities]
        if len(skipped) > 1:
            reasons = []
            for item in skipped:
                if item.reason not in reasons:
                    reasons.append(item.reason)
            table.rows.append([
                f"해당 없음 {len(skipped)}종 — " + " · ".join(i.label for i in skipped), "—", "—", "해당 없음",
                "\n".join(reasons), source_kinds(skipped[0]), "", "",
            ])
        for item in items:
            if len(skipped) > 1 and item in skipped:
                continue
            judgement = site.judgements.get(item.key, ItemJudgement())
            judged = JUDGEMENT_LABELS[judgement.state]
            if judgement.memo:
                judged += f" — 메모: {judgement.memo}"
            label = item.label + (f" ({len(item.facilities)})" if item.facilities else "")
            note = ""
            if (
                item.note
                and item.note != item.reason
                and not item.passthrough
                and not (item.outcome == "pass" and not item.facilities)
                and not item.note.startswith("스냅샷 범위 기준")
            ):
                note = item.note
            sources = source_kinds(item)
            data_labels = source_data_labels(item)
            if data_labels:
                sources += f" · {data_labels}"
            if note:
                sources += f"\n{note}"
            table.rows.append([
                label, fmt_threshold(item.threshold_m), fmt_distance(item.nearest_distance_m),
                item.outcome_label, "\n".join(_sentences(item.reason)), sources,
                item.measurement_method or "", judged,
            ])
            for facility in item.facilities:
                table.sub_rows.add(len(table.rows))
                detail = [facility.name, facility.address or "주소 미확보"]
                if facility.zoning_name:
                    detail.append(f"용도지역 {facility.zoning_name}")
                table.rows.append([
                    "└ " + " · ".join(detail), "", fmt_distance(facility.distance_m), "",
                    facility.classification_note or "", facility.source_label, "", "",
                ])
    blocks.append(table)
    if one.passthrough_notes:
        blocks.append(Block("heading", "판정 미적용 항목", level=3))
        blocks.append(Block("bullets", rows=[[n] for n in one.passthrough_notes]))
    return blocks


def _criterion_reason_row(criterion: ScreeningCriterion, bonus: bool) -> list[str]:
    """ScreeningSheet ScoreReasons 한 줄 — 받은 근거와 더 받지 못한 이유."""

    index = next((i for i, t in enumerate(criterion.tiers) if t.selected), -1)
    selected = criterion.tiers[index] if index >= 0 else None
    above = criterion.tiers[index - 1] if index > 0 else None
    if selected is not None:
        lines = [f"{selected.points}점 등급 · {selected.condition}"]
        lines += [requirement_line(r) for r in selected.requirements] or ["위 등급 조건을 하나도 충족하지 못해 최하 등급입니다."]
    else:
        lines = [f"산정 가능 범위 {criterion.awarded_min}~{criterion.awarded_max}점" + (f" · {criterion.note}" if criterion.note else "")]
    if selected is not None and above is None:
        missed = "만점"
    elif above is not None:
        missed = "\n".join([above.condition, *[requirement_line(r) for r in above.requirements if r.met is not True]])
    else:
        missed = ""
    label = criterion.label + (" (가점)" if bonus else "")
    return [label, f"{fmt_points(criterion.awarded)} / {criterion.maximum}", "\n".join(lines), missed]


COMPACT_HITS = 6


def _hit_notes(hit: ScreeningFacilityHit) -> list[str]:
    meta = [f"측정 {measurement_short(hit)}"]
    if hit.measurement_method:
        meta.append(hit.measurement_method)
    if hit.front_door_source:
        meta.append(hit.front_door_source)
    notes = [" · ".join(meta)]
    if hit.front_door_notice:
        notes.append(f"⚠ {hit.front_door_notice}")
    if hit.count_note:
        notes.append(f"✕ 배점 제외 — {hit.count_note}" if not hit.counted else f"✓ {hit.count_note}")
    elif not hit.counted:
        notes.append("✕ 배점 제외")
    if hit.lh_alignment:
        notes.append(hit.lh_alignment)
    return notes


def _compact_hits(hits: list[ScreeningFacilityHit]) -> str:
    """PDF 용 — 시설군의 시설을 한 칸에 「이름 거리」로 늘어놓는다.

    배점에 센 시설은 가까운 순으로 COMPACT_HITS 곳까지, 제외한 시설은 사유와 함께 전부.
    측정 기준은 시설마다 같은 경우가 대부분이라 한 번만 적는다.
    """

    counted = [h for h in hits if h.counted]
    excluded = [h for h in hits if not h.counted]
    lines: list[str] = []
    if counted:
        shown = counted[:COMPACT_HITS]
        text = " · ".join(f"{h.name or '이름 미확보'} {fmt_distance(h.distance_m)}" for h in shown)
        if len(counted) > len(shown):
            text += f" 외 {len(counted) - len(shown)}곳"
        lines.append(f"반영 {len(counted)}곳: {text}")
        tiers = []
        for hit in counted:
            label = measurement_short(hit)
            if label not in tiers:
                tiers.append(label)
        lines.append("측정 " + " · ".join(tiers))
        # 시설별 필지 고지(「시설 필지 PNU …」)는 곳마다 달라 다 적으면 쪽이 넘친다 — LH 개별 맞춤만 남긴다.
        notices = []
        for hit in counted:
            if hit.lh_alignment and hit.lh_alignment not in notices:
                notices.append(hit.lh_alignment)
        lines += notices
    for hit in excluded:
        reason = hit.count_note or "배점 제외"
        lines.append(f"✕ {hit.name or '이름 미확보'} {fmt_distance(hit.distance_m)} — {reason}")
    return "\n".join(lines)


def _group_rows(criterion: ScreeningCriterion, table: Block, compact: bool) -> None:
    for group in criterion.groups:
        name = group.label
        if (
            not compact
            and group.designated_source and group.actual_source
            and group.designated_source != group.actual_source
        ):
            name += f"\n지정 원천 {group.designated_source} · 실제 원천 {group.actual_source}"
        if group.point_exception:
            name += f"\n거리 기준 {group.point_exception}"
        if group.state != "connected" and group.note:
            basis = group.note
        elif group.count > 0:
            basis = f"{group.actual_source or group.designated_source} 조회 결과 {group.count:,}건."
        else:
            basis = "조회 범위 안에 해당 시설이 없습니다."
        if group.front_door_notice:
            basis += f"\n⚠ {group.front_door_notice}"
        if compact and group.hits:
            basis += "\n" + _compact_hits(group.hits)
        table.rows.append([
            name, f"{group.count:,}건", fmt_distance(group.nearest_distance_m), group.state_label, basis,
            group.actual_source if group.actual_source and group.state != "missing" else "",
        ])
        if compact:
            continue
        for hit in group.hits:
            table.sub_rows.add(len(table.rows))
            table.rows.append([
                f"└ {hit.name or '이름 미확보'} · {hit.address or '주소 미확보'}", "", fmt_distance(hit.distance_m),
                "반영" if hit.counted else "제외", "\n".join(_hit_notes(hit)), hit.source_label,
            ])


def _stage_two_blocks(result: ScreeningResult, compact: bool) -> list[Block]:
    two = result.stage_two
    blocks: list[Block] = [Block("heading", f"2차 생활편의성 배점 — {two.sheet_label}", level=2)]
    if two.reference_only:
        blocks.append(Block("text", "1차 매입제외에 해당해 아래 배점은 참고용입니다. 합격 여부를 가르지 않습니다.", style="warning"))
    if two.determined:
        score = f"생활편의성 점수 {fmt_points(two.living_score)} / {two.living_maximum}점"
    else:
        score = f"생활편의성 점수 산정 가능 범위 {two.living_score_min}~{two.living_score_max}점"
    blocks.append(Block("text", score, style="strong"))
    blocks.append(Block(
        "text",
        f"심사표 총점 {two.total_sheet_points}점 · 합격선 {two.pass_threshold}점 · "
        f"이 심사는 생활편의성 {two.living_maximum}점만 산정합니다.",
        style="muted",
    ))
    if not two.determined and two.note:
        blocks.append(Block("text", two.note, style="muted"))

    criteria = [(c, False) for c in two.criteria] + ([(two.bonus, True)] if two.bonus else [])
    reasons = Block(
        "table", columns=["평가항목", "점수", "받은 근거", "더 받지 못한 이유"], weights=[2.2, 1.2, 6.0, 4.0],
    )
    for criterion, bonus in criteria:
        reasons.rows.append(_criterion_reason_row(criterion, bonus))
    blocks.append(Block("heading", "배점 근거", level=3))
    blocks.append(reasons)

    all_groups = {g.key: g for c, _ in criteria for g in c.groups}
    connected = sum(1 for g in all_groups.values() if g.state == "connected")
    blocks.append(Block("heading", f"생활편의시설 연결상세 — 시설군 {len(all_groups)}종 중 지정 원천 연결 {connected}종", level=3))
    for criterion, bonus in criteria:
        head = f"{criterion.label}{' (가점)' if bonus else ''} — {fmt_points(criterion.awarded)} / {criterion.maximum}점"
        if not criterion.determined:
            head += " · 확정 불가"
        lines = [head]
        if criterion.tier_condition:
            lines.append(criterion.tier_condition)
        if not criterion.determined:
            lines.append(f"산정 가능 범위 {criterion.awarded_min}~{criterion.awarded_max}점" + (f" · {criterion.note}" if criterion.note else ""))
        elif criterion.note:
            lines.append(criterion.note)
        if criterion.basis:
            lines.append(criterion.basis)
        blocks.append(Block("text", "\n".join(lines), style="strong"))
        if criterion.groups:
            table = Block(
                "table", columns=["시설군", "건수", "최근접", "결과", "근거", "원천"],
                weights=[3.6, 1.0, 1.2, 1.2, 5.6, 2.4],
            )
            _group_rows(criterion, table, compact)
            blocks.append(table)
    if two.out_of_scope:
        blocks.append(Block("heading", f"산정 대상 아님 — {two.out_of_scope_points}점", level=3))
        blocks.append(Block("bullets", rows=[[f"{o.label} {o.maximum}점 — {o.reason}"] for o in two.out_of_scope]))
    return blocks


def _nearest_facility(item: ScreeningExclusionItem) -> tuple[str, float | None]:
    """(가장 가까운 시설명, 거리). 시설 목록이 없으면 항목의 최근접 거리만."""

    nearest = min(item.facilities, key=lambda f: f.distance_m, default=None)
    if nearest is not None:
        return nearest.name, nearest.distance_m
    return "", item.nearest_distance_m


def _one_page_stage_one(site: ExportSite, result: ScreeningResult) -> Block:
    """1차 — 규칙(대분류) 한 줄씩. 세부 항목은 시설이 있거나 통과가 아닌 것만 비고에 적는다."""

    groups: dict[str, list[ScreeningExclusionItem]] = {}
    for item in result.stage_one.items:
        groups.setdefault(item.rule_id, []).append(item)
    table = Block(
        "table", columns=["1차 규칙", "판정", "기준거리", "최근접 시설 · 거리", "세부(통과·해당 없음 제외)"],
        weights=[3.0, 1.2, 1.1, 3.2, 6.5],
    )
    for items in groups.values():
        worst = max(items, key=lambda i: _OUTCOME_RANK.get(i.outcome, 0))
        label = "미적용" if all(i.passthrough for i in items) else worst.outcome_label
        candidates = [(name, dist, item) for item in items for name, dist in [_nearest_facility(item)] if dist is not None]
        if candidates:
            name, dist, _ = min(candidates, key=lambda c: c[1])
            nearest = f"{name} {fmt_distance(dist)}" if name else fmt_distance(dist)
        else:
            nearest = "—"
        details = []
        for item in items:
            if item.outcome in ("pass", "not_applicable") and not item.facilities:
                continue
            name, dist = _nearest_facility(item)
            text = f"{item.label} {item.outcome_label}"
            if dist is not None:
                text += f" {fmt_distance(dist)}" + (f"({name})" if name else "")
            judgement = site.judgements.get(item.key)
            if judgement and (judgement.state != "unchecked" or judgement.memo):
                text += f" [{JUDGEMENT_LABELS[judgement.state]}{' · ' + judgement.memo if judgement.memo else ''}]"
            details.append(text)
        table.rows.append([items[0].rule_label, label, fmt_threshold(items[0].threshold_m), nearest, " / ".join(details)])
    return table


def _one_page_stage_two(result: ScreeningResult) -> Block:
    """2차 — 축별 점수 한 줄 + 시설군별 「가장 가까운 시설 · 거리 · 개수」 한 줄씩."""

    two = result.stage_two
    table = Block(
        "table", columns=["평가항목 · 시설군", "점수 · 채택 등급 / 최근접 시설 · 거리", "개수", "제외 후보"],
        weights=[3.2, 6.4, 1.0, 4.4],
    )
    criteria = [(c, False) for c in two.criteria] + ([(two.bonus, True)] if two.bonus else [])
    for criterion, bonus in criteria:
        head = f"{criterion.label}{' (가점)' if bonus else ''}"
        score = f"{fmt_points(criterion.awarded)} / {criterion.maximum}점"
        if not criterion.determined:
            score += f" · 산정 범위 {criterion.awarded_min}~{criterion.awarded_max}"
        if criterion.tier_condition:
            score += f" · {criterion.tier_condition}"
        table.group_rows.add(len(table.rows))
        table.rows.append([head, score, "", ""])
        for group in criterion.groups:
            counted = [h for h in group.hits if h.counted]
            excluded = [h for h in group.hits if not h.counted]
            nearest = min(counted, key=lambda h: h.distance_m, default=None)
            if nearest is not None:
                text = f"{nearest.name or '이름 미확보'} {fmt_distance(nearest.distance_m)}"
            elif group.state != "connected":
                text = f"없음 · {group.state_label}" + (f"({group.actual_source})" if group.actual_source else "")
            else:
                text = "없음"
            table.sub_rows.add(len(table.rows))
            table.rows.append([
                f"└ {group.label}", text, f"{len(counted)}건" if group.hits else f"{group.count}건",
                ", ".join(h.name or "이름 미확보" for h in excluded),
            ])
    return table


def site_report_one_page(site: ExportSite, index: int) -> SiteReport:
    """PDF 용 한 쪽 심사표 — 머리표 한 블록, 1차 규칙별 한 줄, 2차 축·시설군별 한 줄. 고지문은 뺀다."""

    receipt, address, housing, application = _site_labels(site)
    result = site.result
    title, subtitle = _site_title(site)
    heading = f"{index}. 접수번호 {receipt or '—'} · {title}"
    if result is None:
        return SiteReport(
            receipt_no=receipt, title=heading, subtitle=address,
            blocks=[
                Block("kv", rows=[["소재지", address], ["분류 · 신청유형", f"{housing} · {application}"]], weights=[1.4, 8.6]),
                Block("text", f"심사 결과 없음 — {site.error or '심사가 끝나지 않았습니다.'}", style="warning"),
            ],
        )
    two = result.stage_two
    one = result.stage_one
    parcels = result.site.parcels
    extras = " · ".join(f"{k} {v}" for k, v in site.extras.items() if v)
    scores = " · ".join(
        f"{c.label} {fmt_points(c.awarded) if c.determined else f'{c.awarded_min}~{c.awarded_max}'}/{c.maximum}"
        for c in [*two.criteria, *([two.bonus] if two.bonus else [])]
    )
    living = (
        f"{fmt_points(two.living_score)} / {two.living_maximum}점" if two.determined
        else f"산정 범위 {two.living_score_min}~{two.living_score_max} / {two.living_maximum}점"
    ) + (" (1차 매입제외 — 참고용)" if two.reference_only else "")
    info: list[list[str]] = [
        ["소재지", address + (f"  ·  {extras}" if extras else "")],
        ["분류 · 유형", f"{housing} · {application} · 규칙팩 {result.rule_pack_id or '미지정'} v{result.rule_pack_version or '—'} · 심사 {stamp(result.created_at)}"],
        [f"필지 {len(parcels)}건", ", ".join(p.address or p.pnu or p.parcel_id for p in parcels) + (f" · {site.resolve_note}" if site.resolve_note else "")],
        ["종합 판정", f"{result.verdict_label} — {result.verdict_summary}"],
    ]
    reasons = one.reasons if result.verdict == "fail" else (one.review_reasons if result.verdict == "review" else [])
    if reasons:
        info.append(["사유", " / ".join(reasons)])
    info.append(["생활편의성", f"{living} · {scores}"])
    if result.source_alerts:
        info.append([
            f"원천 경고 {len(result.source_alerts)}건",
            ", ".join(f"{'1차' if a.stage == 'stage_one' else '2차'} {a.source}" for a in result.source_alerts) + " — 재심사 권장",
        ])
    blocks: list[Block] = [Block("kv", rows=info, weights=[1.4, 8.6]), _one_page_stage_one(site, result)]
    if one.passthrough_notes:
        blocks.append(Block("text", "판정 미적용: " + " / ".join(one.passthrough_notes), style="muted"))
    blocks.append(_one_page_stage_two(result))
    if two.out_of_scope:
        blocks.append(Block(
            "text",
            f"산정 대상 아님 {two.out_of_scope_points}점 — " + ", ".join(f"{o.label} {o.maximum}점" for o in two.out_of_scope),
            style="muted",
        ))
    # 필지 주소는 머리표 「필지」 줄에 있으므로 부제목으로 되풀이하지 않는다.
    return SiteReport(receipt_no=receipt, title=heading, subtitle="", blocks=blocks)


def site_report(site: ExportSite, index: int, compact: bool = False) -> SiteReport:
    """사업지 한 건의 심사표 블록. 실패·중단 건은 사유만 적는다.

    compact — PDF 용 한 쪽 압축본(site_report_one_page). Excel 은 전부 펼친다.
    """

    if compact:
        return site_report_one_page(site, index)

    receipt, address, housing, application = _site_labels(site)
    result = site.result
    title, subtitle = _site_title(site)
    heading = f"{index}. 접수번호 {receipt or '—'} · {title}"
    if result is None:
        return SiteReport(
            receipt_no=receipt, title=heading, subtitle=address,
            blocks=[
                Block("kv", rows=[["소재지", address], ["분류 · 신청유형", f"{housing} · {application}"]]),
                Block("text", f"심사 결과 없음 — {site.error or '심사가 끝나지 않았습니다.'}", style="warning"),
            ],
        )

    parcels = result.site.parcels
    parcel_lines = [
        " · ".join(p for p in (parcel.address, f"PNU {parcel.pnu}" if parcel.pnu else "") if p) or parcel.parcel_id
        for parcel in parcels
    ]
    info: list[list[str]] = [
        ["소재지", address],
        ["분류 · 신청유형", f"{housing} · {application}"],
        ["적용 규칙팩", f"{result.rule_pack_id or '미지정'} · v{result.rule_pack_version or '—'}" + (" · 데모 데이터" if result.demo else "")],
        ["심사 시각 · ID", f"{stamp(result.created_at)} · {result.screening_id}"],
        [f"필지 {len(parcels)}건", "\n".join(parcel_lines)],
    ]
    for key, value in site.extras.items():
        if value:
            info.append([key, value])
    if site.resolve_note:
        info.append(["필지 비고", site.resolve_note])
    info.append(["데이터 판", data_snapshot_text(result)])
    blocks: list[Block] = [Block("kv", rows=info)]

    if result.source_alerts:
        blocks.append(Block(
            "heading", f"일부 공공 데이터가 응답하지 않았습니다 — 잠시 후 재심사하세요 ({len(result.source_alerts)}건)",
            level=3, style="warning",
        ))
        blocks.append(Block(
            "bullets",
            rows=[[f"{'1차' if a.stage == 'stage_one' else '2차'} · {a.source} — {a.message}"] for a in result.source_alerts],
            style="warning",
        ))
        blocks.append(Block(
            "text",
            "이 결과의 판정·점수는 대체 자료로 낸 것이라 실제와 다를 수 있습니다. 확정 전에 다시 심사해 경고가 사라졌는지 확인해 주세요.",
            style="warning",
        ))

    blocks.append(Block("heading", f"종합 판정 — {result.verdict_label}", level=2, style=result.verdict))
    blocks.append(Block("text", result.verdict_summary))
    one = result.stage_one
    if result.verdict == "fail" and one.reasons:
        blocks.append(Block("heading", f"부적격 사유 {len(one.reasons)}건", level=3))
        blocks.append(Block("bullets", rows=[["\n".join(_sentences(r))] for r in one.reasons]))
    if result.verdict == "review" and one.review_reasons:
        blocks.append(Block("heading", f"검토 필요 사유 {len(one.review_reasons)}건", level=3))
        blocks.append(Block("bullets", rows=[[r] for r in one.review_reasons]))

    blocks += _stage_one_blocks(site, result, compact)
    blocks += _stage_two_blocks(result, compact)

    memos = [
        (item.label, site.judgements[item.key])
        for item in one.items
        if item.key in site.judgements and (site.judgements[item.key].memo or site.judgements[item.key].state != "unchecked")
    ]
    if memos:
        blocks.append(Block("heading", "담당자 판단 · 메모", level=3))
        blocks.append(Block(
            "table", columns=["항목", "판단", "메모"], weights=[4, 2, 8],
            rows=[[label, JUDGEMENT_LABELS[j.state], j.memo] for label, j in memos],
        ))
    if result.calculation_note:
        blocks.append(Block("text", result.calculation_note, style="muted"))
    if result.disclaimer:
        blocks.append(Block("text", result.disclaimer, style="strong"))
    return SiteReport(receipt_no=receipt, title=heading, subtitle=subtitle, blocks=blocks)


# ---------------------------------------------------------------------------
# 결과표(화면 BatchPanel 표와 같은 열)
# ---------------------------------------------------------------------------
EXTRA_KEYS = ("차수", "접수일자", "매도자명")


def batch_table(batch: BatchStatus, housing_labels: dict[str, str], application_labels: dict[str, str]) -> tuple[list[str], list[list[str]]]:
    """BatchPanel 결과표 — 같은 열·순서·라벨·표기. 「심사표 열기」 버튼 열만 없다."""

    has_extras = any(row.extras.get(k) for row in batch.rows for k in EXTRA_KEYS)
    columns = ["접수번호", *(["차수·접수일자·매도자"] if has_extras else []), "소재지", "분류 · 신청유형",
               "1차 판정", "2차 점수", "교통", "주거", "교육", "가점", "필지", "상태"]
    rows: list[list[str]] = []
    for row in batch.rows:
        address = row.address
        if row.site_address and row.site_address != row.address:
            address += f"\n대표 필지 {row.site_address}"
        if row.error:
            address += f"\n{row.error}"
        if row.status == "running":
            state = f"{row.progress}% · {row.stage or '심사 중'}"
        else:
            state = row.message or "대기"
        cells = [row.id]
        if has_extras:
            cells.append(" · ".join(v for v in (row.extras.get(k, "") for k in EXTRA_KEYS) if v) or "—")
        cells += [
            address,
            f"{housing_labels.get(row.housing_type, row.housing_type)} · {application_labels.get(row.application_type, row.application_type)}",
            row.verdict_label or "—",
            score_text(row),
            criterion_text(row, "transit"),
            criterion_text(row, "living"),
            criterion_text(row, "education"),
            criterion_text(row, "station_area"),
            str(row.parcel_count) if row.parcel_count else "",
            state,
        ]
        rows.append(cells)
    return columns, rows


def batch_report(
    batch: BatchStatus,
    sites: list[ExportSite],
    generated_at: datetime,
    housing_labels: dict[str, str],
    application_labels: dict[str, str],
    compact: bool = False,
) -> BatchReport:
    columns, rows = batch_table(batch, housing_labels, application_labels)
    completed = sum(1 for r in batch.rows if r.status == "completed")
    failed = sum(1 for r in batch.rows if r.status == "failed")
    state = {"completed": "완료", "cancelled": "중단됨", "running": "검토 중", "queued": "대기"}.get(batch.status, batch.status)
    info = [
        ("생성 시각", stamp(generated_at)),
        ("일괄 심사 시작", stamp(batch.created_at)),
        ("기본 분류 · 신청유형", f"{housing_labels.get(batch.housing_type, batch.housing_type)} · {application_labels.get(batch.application_type, batch.application_type)}"),
        ("진행", f"{state} · {batch.done}/{batch.total}건 끝남 · 완료 {completed} · 실패 {failed}"),
        ("일괄 심사 ID", batch.batch_id),
    ]
    return BatchReport(
        title="LH 신축매입약정 서류심사 — 일괄 심사 결과",
        generated_at=generated_at,
        info=info,
        columns=columns,
        rows=rows,
        sites=[site_report(site, index, compact) for index, site in enumerate(sites, start=1)],
    )


def attachment_header(prefix: str, generated_at: datetime, extension: str) -> str:
    """Content-Disposition. 한글 파일명은 RFC 5987 로 싣고 ASCII 대체 이름도 준다."""

    from urllib.parse import quote

    stamp_text = generated_at.astimezone(KST).strftime("%Y%m%d_%H%M")
    ascii_name = f"LH_batch_{stamp_text}.{extension}"
    utf8_name = quote(f"{prefix}_{stamp_text}.{extension}")
    return f"attachment; filename={ascii_name}; filename*=UTF-8''{utf8_name}"


# ---------------------------------------------------------------------------
# Excel — 시트 1 결과표, 시트 2부터 건별 심사표
# ---------------------------------------------------------------------------
_ILLEGAL_SHEET_CHARS = str.maketrans({c: " " for c in "[]:*?/\\"})
DETAIL_COLUMN_WIDTHS = [34, 12, 12, 12, 52, 30, 22, 22]


def _clean_cell(value: str) -> str:
    """openpyxl 이 거부하는 제어문자(원천 데이터에 섞여 들어올 수 있다)를 뺀다."""

    return "".join(ch for ch in value if ch >= " " or ch in "\n\t")


def _sheet_title(index: int, receipt: str, used: set[str]) -> str:
    base = f"{index}. {receipt}".translate(_ILLEGAL_SHEET_CHARS).strip()[:28] if receipt else f"{index}. 심사표"
    title = base
    suffix = 2
    while title in used:
        title = f"{base[:25]} ({suffix})"
        suffix += 1
    used.add(title)
    return title


def build_batch_workbook(report: BatchReport) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    bold = Font(bold=True)
    title_font = Font(bold=True, size=14)
    head_font = Font(bold=True, size=12)
    muted = Font(color="666666")
    warning = Font(color="B45309", bold=True)
    header_fill = PatternFill("solid", fgColor="DCE6F1")
    group_fill = PatternFill("solid", fgColor="F2F2F2")
    wrap = Alignment(wrap_text=True, vertical="top")
    last_column = len(DETAIL_COLUMN_WIDTHS)

    book = Workbook()
    sheet = book.active
    sheet.title = "결과표"
    sheet.append([report.title])
    sheet["A1"].font = title_font
    for label, value in report.info:
        sheet.append([label, value])
        sheet.cell(row=sheet.max_row, column=1).font = bold
    sheet.append([])
    sheet.append(report.columns)
    header_row = sheet.max_row
    for cell in sheet[header_row]:
        cell.font = bold
        cell.fill = header_fill
        cell.alignment = wrap
    for row in report.rows:
        sheet.append([_clean_cell(v) for v in row])
        for cell in sheet[sheet.max_row]:
            cell.alignment = wrap
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)
    widths = {"접수번호": 11, "차수·접수일자·매도자": 24, "소재지": 46, "분류 · 신청유형": 22, "1차 판정": 16,
              "2차 점수": 10, "교통": 7, "주거": 7, "교육": 7, "가점": 7, "필지": 7, "상태": 18}
    for index, column in enumerate(report.columns, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = widths.get(column, 14)

    used: set[str] = {"결과표"}
    for index, site in enumerate(report.sites, start=1):
        detail = book.create_sheet(_sheet_title(index, site.receipt_no, used))
        for column_index, width in enumerate(DETAIL_COLUMN_WIDTHS, start=1):
            detail.column_dimensions[get_column_letter(column_index)].width = width

        def line(text: str, font: Font | None = None, merge: bool = True) -> None:
            detail.append([_clean_cell(text)])
            cell = detail.cell(row=detail.max_row, column=1)
            cell.alignment = wrap
            if font is not None:
                cell.font = font
            if merge:
                detail.merge_cells(start_row=detail.max_row, start_column=1, end_row=detail.max_row, end_column=last_column)

        line(site.title, title_font)
        if site.subtitle:
            line(site.subtitle, muted)
        for block in site.blocks:
            if block.kind == "heading":
                detail.append([])
                line(block.text, warning if block.style == "warning" else (head_font if block.level <= 2 else bold))
            elif block.kind == "text":
                line(block.text, {"muted": muted, "strong": bold, "warning": warning}.get(block.style))
            elif block.kind == "kv":
                for label, value in block.rows:
                    detail.append([_clean_cell(label), _clean_cell(value)])
                    detail.cell(row=detail.max_row, column=1).font = bold
                    detail.cell(row=detail.max_row, column=2).alignment = wrap
                    detail.merge_cells(start_row=detail.max_row, start_column=2, end_row=detail.max_row, end_column=last_column)
            elif block.kind == "bullets":
                for (text,) in block.rows:
                    line("• " + text, warning if block.style == "warning" else None)
            elif block.kind == "table":
                detail.append(block.columns)
                for cell in detail[detail.max_row]:
                    cell.font = bold
                    cell.fill = header_fill
                for row_index, row in enumerate(block.rows):
                    detail.append([_clean_cell(v) for v in row])
                    for cell in detail[detail.max_row]:
                        cell.alignment = wrap
                        if row_index in block.group_rows:
                            cell.font = bold
                            cell.fill = group_fill
                        elif row_index in block.sub_rows:
                            cell.font = muted
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
