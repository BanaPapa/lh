"""전국 소음진동배출시설 표준데이터 어댑터 검증.

네트워크 없이 도는 단위 테스트다(실호출은 검증용으로만 쓰고 여기 넣지 않는다).
회귀로 고정하는 것들:
(1) 파라미터 규약 — serviceKey·pageNo·numOfRows·type 넷만 보낸다.
(2) 조회 실패를 빈 결과로 삼키지 않고 전파한다(상위에서 dataset_missing).
(3) 좌표: 위경도는 그대로, 투영/범위 밖은 변환·재검사 뒤 버린다.
(4) 50dB 예외를 확인할 소음도 필드가 없다 — noise_level_db 는 None, 확인불가.
(5) 미승인(403) + CSV 주입 시 CSV 보조로 넘어가고, 미승인·파일부재 시 미적재.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.models import Coordinates
from app.services.noise_emission import (
    NOISE_LEVEL_FIELD_AVAILABLE,
    NoiseEmissionAPIError,
    NoiseEmissionClient,
    _facility,
)


# 전북 원본 08_noise_vibration_facilities.csv 실측 스키마를 그대로 쓴다.
def row(
    name: str = "제일기계",
    lat: str = "35.919000",
    lot: str = "126.949000",
    se: str = "소음",
    lotno: str = "전북특별자치도 군산시 성산면 고봉리 161-7",
    road: str = "전북특별자치도 군산시 나포면 서포리 78",
    main: str = "",
) -> dict:
    return {
        "fcltNm": name,
        "ctpvNm": "전북특별자치도",
        "sggNm": "군산시",
        "lctnRoadNmAddr": road,
        "lctnLotnoAddr": lotno,
        "lat": lat,
        "lot": lot,
        "noisVbrtSeNm": se,
        "noisVbrtMainCn": main,
        "telno": "063-851-2490",
        "rprsvNm": "홍길동",
        "dataCrtrYmd": "2026-03-26",
        "insttCode": "4681000",
        "insttNm": "전북특별자치도 군산시",
    }


def response(rows: list[dict], total: int | None = None) -> dict:
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE", "type": "json"},
            "body": {
                "pageNo": 1,
                "numOfRows": 1000,
                "totalCount": total if total is not None else len(rows),
                "items": rows,
            },
        }
    }


GUNSAN = Coordinates(lat=35.919, lng=126.949)


def client_with(handler, csv_path=None) -> NoiseEmissionClient:
    return NoiseEmissionClient(
        service_key="test-key",
        csv_path=csv_path,
        transport=httpx.MockTransport(handler),
    )


class TestParameters:
    def test_sends_only_the_four_allowed_params(self) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(200, json=response([]))

        asyncio.run(client_with(handler).all_facilities())

        assert seen, "요청이 나가지 않았다"
        assert set(seen[0]) == {"serviceKey", "pageNo", "numOfRows", "type"}
        assert seen[0]["type"] == "json"

    def test_hits_api_data_go_kr_host(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            return httpx.Response(200, json=response([]))

        asyncio.run(client_with(handler).all_facilities())
        # apis.data.go.kr 가 아니라 api.data.go.kr 다.
        assert seen[0] == "api.data.go.kr"


class TestParsing:
    def test_parses_facility_from_wgs84_row(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response([row()]))

        facilities = asyncio.run(client_with(handler).all_facilities())
        assert len(facilities) == 1
        f = facilities[0]
        assert f.name == "제일기계"
        assert f.region == "전북특별자치도"
        assert f.sigungu == "군산시"
        assert f.lotno_address.endswith("고봉리 161-7")
        assert abs(f.coordinates.lat - 35.919) < 1e-6
        assert abs(f.coordinates.lng - 126.949) < 1e-6
        assert f.is_noise_facility is True

    def test_no_pnu_in_source(self) -> None:
        # 원천에 PNU 가 없다. 값을 지어내지 않고 빈 문자열로 둔다(통합 단계가 처리).
        f = _facility(row())
        assert f is not None
        assert f.pnu == ""
        assert f.lotno_address  # 대신 지번주소를 넘긴다


class TestNoiseLevelException:
    def test_noise_level_field_absent(self) -> None:
        # 원천에 소음도(dB)·방음시설 필드가 없다. 모듈 상수로 명시한다.
        assert NOISE_LEVEL_FIELD_AVAILABLE is False

    def test_facility_noise_level_is_none_and_not_verifiable(self) -> None:
        f = _facility(row())
        assert f is not None
        # 50dB 예외를 확인할 값이 없으므로 None, 예외 확인 불가.
        assert f.noise_level_db is None
        assert f.exception_verifiable is False

    def test_vibration_only_is_not_noise_facility(self) -> None:
        f = _facility(row(se="진동"))
        assert f is not None
        assert f.is_noise_facility is False


class TestCoordinates:
    def test_out_of_korea_row_is_dropped(self) -> None:
        # 위경도도 아니고 투영 변환 뒤에도 범위 밖이면 버린다(룰북 §7 ①).
        assert _facility(row(lat="10.0", lot="10.0")) is None

    def test_non_numeric_coords_dropped(self) -> None:
        assert _facility(row(lat="", lot="")) is None
        assert _facility(row(lat="abc", lot="def")) is None

    def test_projected_coords_converted_and_range_rechecked(self) -> None:
        # 위경도 범위를 벗어난 큰 값은 투영좌표(EPSG:5174)로 보고 변환한다.
        # 전북 군산 부근의 구 중부원점 평면좌표 대략값.
        from pyproj import Transformer

        fwd = Transformer.from_crs("EPSG:4326", "EPSG:5174", always_xy=True)
        x, y = fwd.transform(126.949, 35.919)
        f = _facility(row(lat=str(y), lot=str(x)))
        assert f is not None
        assert 35.0 <= f.coordinates.lat <= 36.5
        assert 126.0 <= f.coordinates.lng <= 127.5


class TestFailurePropagation:
    def test_http_500_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with pytest.raises(NoiseEmissionAPIError):
            asyncio.run(client_with(handler).all_facilities())

    def test_network_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        with pytest.raises(NoiseEmissionAPIError):
            asyncio.run(client_with(handler).all_facilities())

    def test_service_error_body_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "response": {
                        "header": {"resultCode": "99", "resultMsg": "SERVICE ERROR"},
                        "body": {},
                    }
                },
            )

        with pytest.raises(NoiseEmissionAPIError):
            asyncio.run(client_with(handler).all_facilities())

    def test_not_registered_without_csv_propagates(self) -> None:
        # 미승인이고 CSV 가 없으면 삼키지 않고 전파한다(상위에서 dataset_missing).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                text=(
                    "<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>"
                    "SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg>"
                    "</cmmMsgHeader></OpenAPI_ServiceResponse>"
                ),
            )

        with pytest.raises(NoiseEmissionAPIError) as exc:
            asyncio.run(client_with(handler).all_facilities())
        assert exc.value.not_registered is True


class TestCsvFallback:
    def _write_csv(self, tmp_path) -> str:
        header = (
            "fcltNm,ctpvNm,sggNm,lctnRoadNmAddr,lctnLotnoAddr,lat,lot,"
            "noisVbrtSeNm,noisVbrtMainCn,gnrlRgnYn,rdsdRgnYn,telno,rprsvNm,"
            "dataCrtrYmd,insttCode,insttNm"
        )
        line = (
            "제일기계,전북특별자치도,군산시,전북특별자치도 군산시 나포면 78,"
            "전북특별자치도 군산시 성산면 고봉리 161-7,35.919000,126.949000,"
            "소음,,Y,N,063-851-2490,홍길동,2026-03-26,4681000,군산시"
        )
        path = tmp_path / "08_noise_vibration_facilities.csv"
        path.write_text("﻿" + header + "\n" + line + "\n", encoding="utf-8")
        return str(path)

    def test_not_registered_falls_back_to_csv(self, tmp_path) -> None:
        csv_path = self._write_csv(tmp_path)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                text=(
                    "<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>"
                    "SERVICE_KEY_IS_NOT_REGISTERED_ERROR</errMsg>"
                    "</cmmMsgHeader></OpenAPI_ServiceResponse>"
                ),
            )

        facilities = asyncio.run(client_with(handler, csv_path=csv_path).all_facilities())
        assert len(facilities) == 1
        assert facilities[0].name == "제일기계"
        assert facilities[0].noise_level_db is None

    def test_csv_only_client_without_key(self, tmp_path) -> None:
        csv_path = self._write_csv(tmp_path)
        client = NoiseEmissionClient(service_key="", csv_path=csv_path)
        assert client.enabled is True
        facilities = asyncio.run(client.all_facilities())
        assert len(facilities) == 1

    def test_missing_csv_file_raises(self, tmp_path) -> None:
        missing = str(tmp_path / "nope.csv")
        client = NoiseEmissionClient(service_key="", csv_path=missing)
        # 파일 부재를 빈 결과로 삼키지 않는다.
        with pytest.raises(NoiseEmissionAPIError):
            asyncio.run(client.all_facilities())


class TestDisabled:
    def test_no_key_no_csv_is_disabled_and_empty(self) -> None:
        client = NoiseEmissionClient(service_key="", csv_path=None)
        assert client.enabled is False
        assert asyncio.run(client.all_facilities()) == []


class TestAround:
    def test_filters_by_radius(self) -> None:
        near = row(name="가까운공장", lat="35.919000", lot="126.949000")
        far = row(name="먼공장", lat="37.566500", lot="126.978000")  # 서울

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=response([near, far]))

        found = asyncio.run(
            client_with(handler).facilities_around(GUNSAN, radius_m=1000.0)
        )
        names = {f.name for f in found}
        assert "가까운공장" in names
        assert "먼공장" not in names
