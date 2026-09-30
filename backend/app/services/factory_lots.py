"""등록공장 지번(PNU) 파일 — 공장등록 API 가 주지 않는 「등록 지번」을 서버에 실어 둔다.

왜 필요한가
-----------
1차 공장 원천인 산단공 공장등록 필지정보 API(15087615)는 도로명주소(`rnAdres`)만 주고
지번·PNU 가 없다. 그래서 도로명을 지오코딩해 그 점의 필지를 시설 필지로 삼았는데, LH 앱은
factoryON 등록공장 대장의 **등록 지번**으로 PNU 를 조립한다. 도로명 주소가 가리키는 건물
필지와 등록 지번 필지가 다르면 거리가 달라진다(2026-09-30 실측: 대진식품 「추천로 217-14」
→ 팔복동1가 10-5 · 사업지 송천동1가 626-74 에서 약 578m, LH 는 등록 지번 팔복동1가 711-2
로 404.2m).

원천
----
공공데이터포털 「전북특별자치도 전주시_공장등록현황」(fileData 3069076, 데이터기준일자
2026-07-10, 전주시 기업지원과). 순번·단지명·회사명·공장대표주소(도로명)·공장대표주소(지번)·
업종명·생산품·연락처·공장면적·데이터기준일자 열이 있고 로그인 없이 받는다. 이 중 회사명·
도로명·지번·기준일만 옮기고, tools/build_factory_lots.py 가 지번으로 PNU 를 조립하고(VWorld
주소→좌표의 법정동코드 + 원본 본번·부번) 연속지적도 필지 안의 한 점을 붙여 둔다.

- LH 앱의 원본(03_06_09_factory_registry.xlsx)은 factoryON 「공장검색」 엑셀 내보내기라
  로그인이 필요해 이 앱의 원천으로 쓰지 않는다. 한국산업단지공단 「전국등록공장현황」
  (fileData 15105482, 2025-12-31)은 공개지만 공장주소 한 열뿐이고 도로명이 있으면 도로명만
  적혀 있어(대진식품도 「추천로 217-14」) 지번을 얻을 수 없다.
- 지금은 전주시만 지번 공개 파일이 있다. 다른 시군 공장은 종전대로 도로명 지오코딩을 쓴다.

대조
----
API 행과 파일 행은 공통 키(공장관리번호)가 없어 **회사명 + 주소**(파일의 도로명 또는 지번,
건물명·층·「번지」·「외 N필지」 표기는 떼고)가 같을 때 맞춘다. 맞춘 행에
지적도 좌표가 없으면(분할·합병으로 사라진 옛 지번) 쓰지 않고 도로명 지오코딩으로 넘어간다.
「외 N필지」는 원본에 나머지 필지 번호가 없어 대표 지번 한 필지만 쓴다.
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from app.models import Coordinates

FACTORY_LOTS_CSV = (
    Path(__file__).resolve().parents[2] / "data" / "factory_lots_jeonju_20260710.csv"
)
FACTORY_LOTS_DATASET_ID = "3069076"
FACTORY_LOTS_DATASET_TITLE = "전북특별자치도 전주시_공장등록현황"
FACTORY_LOTS_URL = "https://www.data.go.kr/data/3069076/fileData.do"
FACTORY_LOTS_AS_OF = "2026-07-10"


class FactoryLot(NamedTuple):
    seq: str
    name: str
    road_address: str
    jibun_address: str
    pnu: str
    extra_lots: int
    coordinates: Coordinates | None


# 회사명에서 떼는 법인 표기. 「(주)대진」「주식회사 대진」「㈜대진」을 같은 이름으로 본다.
_CORP_MARKS = re.compile(
    r"\(주\)|㈜|주식회사|\(유\)|유한회사|\(합\)|합자회사|\(사\)|사단법인|농업회사법인|"
    r"영농조합법인|\(재\)|재단법인"
)
_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]")


def normalize_name(name: str) -> str:
    return _NON_WORD.sub("", _CORP_MARKS.sub("", name or "")).lower()


_LOT_TAIL = re.compile(r"외\s*\d+\s*필지|번지")


def normalize_road(address: str) -> str:
    """「전북특별자치도 전주시 덕진구 추천로 217-14, 대진식품 (팔복동1가)」→「전주시덕진구추천로217-14」.

    API 가 도로명 칸에 지번(「용복동 278-1번지」)을 적은 행도 있어 「번지」「외 N필지」도 뗀다.
    """

    text = (address or "").split("(", 1)[0].split(",", 1)[0]
    text = text.replace("전북특별자치도", "").replace("전라북도", "")
    return re.sub(r"\s+", "", _LOT_TAIL.sub("", text))


class FactoryLotIndex:
    """파일 행을 회사명·도로명으로 찾는다."""

    def __init__(self, lots: list[FactoryLot]) -> None:
        self.lots = lots
        self._by_pair: dict[tuple[str, str], list[FactoryLot]] = {}
        self._by_name: dict[str, list[FactoryLot]] = {}
        for lot in lots:
            name = normalize_name(lot.name)
            if not name:
                continue
            for address in {normalize_road(lot.road_address), normalize_road(lot.jibun_address)}:
                if address:
                    self._by_pair.setdefault((name, address), []).append(lot)
            self._by_name.setdefault(name, []).append(lot)

    def __len__(self) -> int:
        return len(self.lots)

    def match(self, name: str, road_address: str) -> FactoryLot | None:
        """회사명+주소(파일의 도로명 또는 지번)가 같은 행. 여럿이 겹치면 None.

        주소가 달라도 같은 회사명이면 다른 공장(같은 회사의 다른 사업장)일 수 있어 맞추지
        않는다(2026-09-30 대조: 전주 1,204곳 중 주소가 다른 이름 일치 24건의 6건이 다른 필지).
        주소 한쪽이 비어 있을 때만, 회사명이 파일에 한 곳뿐이면 맞춘다.
        """

        key_name = normalize_name(name)
        if not key_name:
            return None
        address = normalize_road(road_address)
        pair = self._by_pair.get((key_name, address)) if address else None
        if pair:
            # 같은 회사가 같은 주소에 여러 번 등록돼도 지번이 같으면 한 곳이다.
            return pair[0] if len({lot.pnu for lot in pair}) == 1 else None
        same_name = self._by_name.get(key_name) or []
        if len(same_name) != 1:
            return None
        only = same_name[0]
        return only if not address or not normalize_road(only.road_address) else None


def _coordinates(row: dict[str, str]) -> Coordinates | None:
    try:
        return Coordinates(lat=float(row["lat"]), lng=float(row["lng"]))
    except (KeyError, TypeError, ValueError):
        return None


def read_lots(path: Path = FACTORY_LOTS_CSV) -> list[FactoryLot]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            FactoryLot(
                seq=row.get("seq", ""),
                name=row.get("name", ""),
                road_address=row.get("road_address", ""),
                jibun_address=row.get("jibun_address", ""),
                pnu=row.get("pnu", ""),
                extra_lots=int(row.get("extra_lots") or 0),
                coordinates=_coordinates(row),
            )
            for row in csv.DictReader(handle)
        ]


@lru_cache(maxsize=1)
def default_index() -> FactoryLotIndex:
    return FactoryLotIndex(read_lots())
