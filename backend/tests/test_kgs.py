"""한국가스안전공사 LPG 충전소 어댑터 — 좌표 위생 검사.

docs/hazards H-02-나 §6-4: 같은 원천의 원본에 서울시청 기본값(37.5665, 126.978)과
전북 밖(경도 128.7~128.9) 좌표가 섞여 있었다. 전국 API 라 전북 범위 검사를 그대로
쓸 수는 없으므로 「주소가 말하는 시도」와 좌표가 어긋나면 격리한다.
"""

from __future__ import annotations

from app.services.kgs import KgsLpgClient, _station


def row(name: str, addr: str, lat: str, lot: str, sect: str = "전북 완주군") -> dict:
    return {"BSES_NM": name, "ADDR": addr, "LAT": lat, "LOT": lot, "SECT_NM": sect,
            "MGT_NM": "자동차", "TELNO": ""}


def test_normal_station_is_kept() -> None:
    station = _station(row("광진충전소", "전북 고창군 중앙로 152", "35.43", "126.69"))
    assert station is not None
    assert station.name == "광진충전소"


def test_seoul_city_hall_default_is_quarantined() -> None:
    result = _station(row(
        "현대자동차(주)전주공장", "전북 완주군 봉동읍 완주산단5로 163",
        "37.5665", "126.978",
    ))
    assert result is None


def test_coordinate_outside_address_province_is_quarantined() -> None:
    # 김제시 주소인데 경도 128.90(경상도 권역).
    result = _station(row("우리LPG충전소", "전북 김제시 금구면 1", "35.9426894", "128.9018102"))
    assert result is None


def test_province_check_uses_address_not_sect_name() -> None:
    # 서울 주소·서울 좌표는 정상이다(전북 전용 검사가 아니다).
    result = _station(row("서울충전소", "서울특별시 강남구 1", "37.50", "127.03", sect="서울"))
    assert result is not None


def test_client_records_quarantine_reasons() -> None:
    client = KgsLpgClient("key")
    kept = client._ingest([
        row("정상", "전북 고창군 중앙로 152", "35.43", "126.69"),
        row("기본값", "전북 완주군 1", "37.5665", "126.978"),
    ])
    assert [s.name for s in kept] == ["정상"]
    assert len(client.quarantined) == 1
    assert client.quarantined[0].name == "기본값"
    assert "기본값" in client.quarantined[0].reason
