"""건축HUB 건축물대장 표제부 교차확인 어댑터 검증.

룰북 §6.4 가목·다목의 AND 조건(제2종 근린생활시설·운동시설 비해당)을 매입제외
확정에 쓰는 값이라, 여기서 판정이 틀리면 곧 오판정이 된다. 세 축을 고정한다.

1. PNU 19자리 분해 경계(길이·비숫자·산여부).
2. 용도 3값 판정 — 특히 모호한 입력이 '확인불가'로 떨어지는지(비해당으로 단정 금지).
3. 조회 실패가 예외로 전파되는지(빈 결과로 둔갑하지 않는지).

네트워크 없이 도는 단위 테스트다. httpx.MockTransport 로 응답을 흉내 낸다.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.services.building_register import (
    BrParams,
    BuildingRegisterAPIError,
    BuildingRegisterClient,
    UseVerdict,
    classify_second_class_neighborhood,
    classify_sports_facility,
    parse_pnu,
)


# 전북 완주군 어느 필지(일반 대지)를 흉내 낸 19자리 PNU.
SAMPLE_PNU = "4571025000102340000"


def title_xml(items: list[str], code: str = "00", msg: str = "NORMAL SERVICE.") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<response><header>"
        f"<resultCode>{code}</resultCode><resultMsg>{msg}</resultMsg>"
        "</header><body><items>"
        f"{''.join(items)}"
        "</items><numOfRows>100</numOfRows><pageNo>1</pageNo>"
        f"<totalCount>{len(items)}</totalCount></body></response>"
    )


def item_xml(main: str, etc: str = "", dong: str = "") -> str:
    return (
        "<item>"
        f"<dongNm>{dong}</dongNm>"
        f"<mainPurpsCdNm>{main}</mainPurpsCdNm>"
        f"<etcPurps>{etc}</etcPurps>"
        "</item>"
    )


def client_with(handler) -> BuildingRegisterClient:
    return BuildingRegisterClient(
        service_key="test-key", transport=httpx.MockTransport(handler)
    )


# ── PNU 분해 경계 ─────────────────────────────────────────────────────


class TestParsePnu:
    def test_decomposes_general_parcel(self) -> None:
        params = parse_pnu(SAMPLE_PNU)
        assert params == BrParams(
            sigungu_cd="45710",
            bjdong_cd="25000",
            plat_gb_cd="0",
            bun="0234",
            ji="0000",
        )
        assert params.is_mountain is False

    def test_mountain_flag_maps_to_plat_gb_1(self) -> None:
        # 11번째 자리 2 = 산 → platGbCd "1".
        pnu = "45710250002" + "0234" + "0000"
        assert len(pnu) == 19
        params = parse_pnu(pnu)
        assert params.plat_gb_cd == "1"
        assert params.is_mountain is True

    def test_strips_surrounding_whitespace(self) -> None:
        params = parse_pnu(f"  {SAMPLE_PNU}  ")
        assert params.sigungu_cd == "45710"

    def test_rejects_short_length(self) -> None:
        with pytest.raises(ValueError):
            parse_pnu("4571025000")

    def test_rejects_long_length(self) -> None:
        with pytest.raises(ValueError):
            parse_pnu(SAMPLE_PNU + "0")

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(ValueError):
            parse_pnu("45710250001023400AB")

    def test_rejects_bad_parcel_flag(self) -> None:
        # 11번째 자리가 1/2 가 아니면(여기선 9) 튕겨야 한다.
        bad = "45710250009" + "0234" + "0000"
        assert len(bad) == 19
        with pytest.raises(ValueError):
            parse_pnu(bad)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            parse_pnu(4571025000102340000)  # type: ignore[arg-type]


# ── 용도 3값 판정 ─────────────────────────────────────────────────────


class TestSecondClassClassification:
    def test_explicit_second_class_is_applicable(self) -> None:
        assert (
            classify_second_class_neighborhood("제2종근린생활시설", "")
            is UseVerdict.APPLICABLE
        )

    def test_whitespace_variants_are_applicable(self) -> None:
        # 공백이 들쭉날쭉해도 포함 관계로 잡아야 한다.
        assert (
            classify_second_class_neighborhood("제2종 근린생활시설", "")
            is UseVerdict.APPLICABLE
        )

    def test_second_class_in_etc_purps_is_applicable(self) -> None:
        # 복합용도: 주용도가 공동주택이라도 기타용도에 제2종근생이 섞이면 해당.
        assert (
            classify_second_class_neighborhood("공동주택", "제2종근린생활시설")
            is UseVerdict.APPLICABLE
        )

    def test_recognized_other_use_is_not_applicable(self) -> None:
        assert (
            classify_second_class_neighborhood("공동주택", "")
            is UseVerdict.NOT_APPLICABLE
        )

    def test_first_class_neighborhood_is_not_applicable(self) -> None:
        # 제1종은 제2종이 아니므로 비해당(포함 검사가 오인하면 안 됨).
        assert (
            classify_second_class_neighborhood("제1종근린생활시설", "")
            is UseVerdict.NOT_APPLICABLE
        )

    def test_empty_is_unknown(self) -> None:
        assert classify_second_class_neighborhood("", "") is UseVerdict.UNKNOWN
        assert classify_second_class_neighborhood(None, None) is UseVerdict.UNKNOWN

    def test_ambiguous_main_use_is_unknown_not_not_applicable(self) -> None:
        # 표준 용도로 읽히지 않는 모호한 문자열은 비해당으로 단정하지 않는다.
        assert (
            classify_second_class_neighborhood("기타", "미상")
            is UseVerdict.UNKNOWN
        )
        assert (
            classify_second_class_neighborhood("○○용도", "")
            is UseVerdict.UNKNOWN
        )


class TestSportsClassification:
    def test_explicit_sports_is_applicable(self) -> None:
        assert classify_sports_facility("운동시설", "") is UseVerdict.APPLICABLE

    def test_other_recognized_use_is_not_applicable(self) -> None:
        assert classify_sports_facility("업무시설", "") is UseVerdict.NOT_APPLICABLE

    def test_ambiguous_is_unknown(self) -> None:
        assert classify_sports_facility("불명", "") is UseVerdict.UNKNOWN

    def test_empty_is_unknown(self) -> None:
        assert classify_sports_facility("", "") is UseVerdict.UNKNOWN


# ── 조회 · 집계 ───────────────────────────────────────────────────────


class TestLookup:
    def test_single_dong_not_applicable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=title_xml([item_xml("공동주택", "", "101동")]))

        result = asyncio.run(client_with(handler).lookup(SAMPLE_PNU))
        assert result.has_building is True
        assert result.second_class_neighborhood is UseVerdict.NOT_APPLICABLE
        assert result.sports_facility is UseVerdict.NOT_APPLICABLE

    def test_any_applicable_dong_makes_parcel_applicable(self) -> None:
        # 한 동이라도 제2종근생이면 필지 전체가 해당.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=title_xml(
                    [
                        item_xml("공동주택", "", "101동"),
                        item_xml("제2종근린생활시설", "", "상가동"),
                    ]
                ),
            )

        result = asyncio.run(client_with(handler).lookup(SAMPLE_PNU))
        assert result.second_class_neighborhood is UseVerdict.APPLICABLE

    def test_mixed_unknown_dong_makes_parcel_unknown(self) -> None:
        # 비해당 + 확인불가 가 섞이고 해당이 없으면 확인불가(비해당으로 올리지 않음).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text=title_xml(
                    [item_xml("공동주택", "", "101동"), item_xml("기타", "", "미상동")]
                ),
            )

        result = asyncio.run(client_with(handler).lookup(SAMPLE_PNU))
        assert result.second_class_neighborhood is UseVerdict.UNKNOWN

    def test_nodata_is_unknown_not_error(self) -> None:
        # 표제부 없음(NODATA)은 실패가 아니라 확인불가.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=title_xml([], code="03", msg="NODATA_ERROR"))

        result = asyncio.run(client_with(handler).lookup(SAMPLE_PNU))
        assert result.has_building is False
        assert result.second_class_neighborhood is UseVerdict.UNKNOWN
        assert result.sports_facility is UseVerdict.UNKNOWN

    def test_request_carries_decomposed_params(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(200, text=title_xml([item_xml("공동주택")]))

        asyncio.run(client_with(handler).lookup(SAMPLE_PNU))
        assert seen["sigunguCd"] == "45710"
        assert seen["bjdongCd"] == "25000"
        assert seen["platGbCd"] == "0"
        assert seen["bun"] == "0234"
        assert seen["ji"] == "0000"

    def test_caches_by_pnu(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, text=title_xml([item_xml("공동주택")]))

        client = client_with(handler)
        asyncio.run(client.lookup(SAMPLE_PNU))
        asyncio.run(client.lookup(SAMPLE_PNU))
        assert calls["n"] == 1

    def test_lookup_many_dedups(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, text=title_xml([item_xml("업무시설")]))

        client = client_with(handler)
        results = asyncio.run(client.lookup_many([SAMPLE_PNU, SAMPLE_PNU]))
        assert calls["n"] == 1
        assert set(results) == {SAMPLE_PNU}


class TestFailurePropagates:
    def test_http_500_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with pytest.raises(BuildingRegisterAPIError):
            asyncio.run(client_with(handler).lookup(SAMPLE_PNU))

    def test_403_raises_with_status(self) -> None:
        # 활용신청 미승인 시 403. 빈 결과로 삼키지 말고 상태코드까지 전파해야
        # 상위에서 '활용신청 필요'를 낼 수 있다.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, text="SERVICE_KEY_IS_NOT_REGISTERED_ERROR")

        with pytest.raises(BuildingRegisterAPIError) as exc:
            asyncio.run(client_with(handler).lookup(SAMPLE_PNU))
        assert exc.value.status_code == 403

    def test_error_result_code_raises(self) -> None:
        # resultCode 가 00/NODATA 가 아니면(예: 30 SERVICE_KEY) 실패로 전파.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text=title_xml([], code="30", msg="SERVICE_KEY_IS_NOT_REGISTERED_ERROR")
            )

        with pytest.raises(BuildingRegisterAPIError):
            asyncio.run(client_with(handler).lookup(SAMPLE_PNU))

    def test_network_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route")

        with pytest.raises(BuildingRegisterAPIError):
            asyncio.run(client_with(handler).lookup(SAMPLE_PNU))

    def test_lookup_many_propagates_one_failure(self) -> None:
        # 한 건이라도 실패하면 전체가 실패로 전파(빈 결과로 둔갑 금지).
        def handler(request: httpx.Request) -> httpx.Response:
            if "0234" in dict(request.url.params).get("bun", ""):
                return httpx.Response(500, text="boom")
            return httpx.Response(200, text=title_xml([item_xml("공동주택")]))

        other = "45710250001099900000"
        with pytest.raises(BuildingRegisterAPIError):
            asyncio.run(client_with(handler).lookup_many([SAMPLE_PNU, other]))

    def test_bad_pnu_raises_before_call(self) -> None:
        called = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            called["n"] += 1
            return httpx.Response(200, text=title_xml([item_xml("공동주택")]))

        with pytest.raises(ValueError):
            asyncio.run(client_with(handler).lookup("badpnu"))
        assert called["n"] == 0
