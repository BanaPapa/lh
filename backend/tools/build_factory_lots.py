"""전주시 공장등록현황(공공데이터포털 3069076) → 등록공장 지번·PNU 파일을 만든다.

    python -m tools.build_factory_lots <원본.csv> [출력.csv]

원본은 공공데이터포털 「전북특별자치도 전주시_공장등록현황」(fileData 3069076, 데이터기준일자
2026-07-10, cp949 CSV)이다. 로그인 없이 받을 수 있다. 열 가운데 순번·회사명·공장대표주소
(도로명)·공장대표주소(지번)·데이터기준일자만 옮기고, 지번주소에서 PNU 를 조립해 붙인다.

- PNU: 지번주소를 VWorld 주소→좌표(getcoord, PARCEL)에 넣어 법정동코드(10자리)를 받고,
  본번·부번·산 여부는 **원본 지번 그대로** 쓴다(정제 주소의 법정동·지번이 원본과 다르면
  조립하지 않는다). 카카오 주소검색도 b_code 를 주지만 한도가 자주 막혀 VWorld 를 쓴다.
- 좌표: VWorld 연속지적도에서 그 PNU 필지를 찾아 필지 안의 한 점(representative point)을
  적는다. 서버가 켤 때마다 1,280곳을 다시 조회하지 않게 미리 구워 둔다. 지적도에 없는
  PNU(분할·합병으로 사라진 옛 지번)는 좌표를 비워 두고, 런타임이 도로명 지오코딩으로 넘어간다.
- 「외 N필지」: 원본에 나머지 필지 번호가 없어 대표 지번 한 필지만 쓴다(extra_lots=N 으로 남김).

키는 backend/.env 의 VWORLD_API_KEY 를 읽는다.
"""

from __future__ import annotations

import csv
import os
import re
import sys
import time
from pathlib import Path

import httpx
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "factory_lots_jeonju_20260710.csv"
VWORLD_ADDRESS_URL = "https://api.vworld.kr/req/address"
VWORLD_URL = "https://api.vworld.kr/req/data"

_LOT = re.compile(
    r"^(?P<head>전북특별자치도\s*전주시\s*(?:덕진구|완산구))\s*(?P<dong>[가-힣]+\d*가?)\s+"
    r"(?P<san>산\s*)?(?P<main>\d+)(?:\s*-\s*(?P<sub>\d+))?"
)
_EXTRA = re.compile(r"외\s*(\d+)\s*필지")


def parse_lot(jibun_address: str) -> tuple[str, str, str, bool, int, int, int] | None:
    """「…덕진구 팔복동1가 711-2번지 대진식품」 → (질의 주소, 법정동, 지번, 산, 본번, 부번, 외 필지 수)."""

    text = " ".join((jibun_address or "").split())
    m = _LOT.match(text)
    if not m:
        return None
    head = re.sub(r"전주시\s*", "전주시 ", m.group("head"))
    san = m.group("san") is not None
    main = int(m.group("main"))
    sub = int(m.group("sub") or 0)
    extra = _EXTRA.search(text)
    lot = f"{'산 ' if san else ''}{main}" + (f"-{sub}" if sub else "")
    return (
        f"{head} {m.group('dong')} {lot}", m.group("dong"), lot, san, main, sub,
        int(extra.group(1)) if extra else 0,
    )


def read_env() -> dict[str, str]:
    """backend/.env(없으면 LH_ENV_FILE 경로)의 키를 읽는다."""

    env: dict[str, str] = {}
    path = Path(os.environ.get("LH_ENV_FILE") or ROOT / ".env")
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"')
    return env


def _get(client: httpx.Client, url: str, params: dict[str, str]) -> dict:
    """끊김·일시 오류는 몇 번 다시 묻는다. 끝내 실패하면 예외를 올린다."""

    for attempt in range(5):
        try:
            r = client.get(url, params=params)
            if r.status_code in (429, 502, 503):
                raise httpx.HTTPStatusError("retry", request=r.request, response=r)
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError):
            if attempt == 4:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def dong_code(client: httpx.Client, key: str, query: str, dong: str, lot: str) -> str:
    """VWorld 주소→좌표(getcoord, PARCEL)로 법정동코드 10자리를 받는다.

    정제 주소(refined)의 법정동·지번이 원본과 같을 때만 받는다. VWorld 는 못 찾으면 전국에서
    비슷한 번지를 골라 답하기도 해서(예: 강릉시 상시동리 711-2) 반드시 대조한다.
    """

    body = _get(client, VWORLD_ADDRESS_URL, {
        "service": "address", "request": "getcoord", "type": "PARCEL",
        "refine": "true", "simple": "false", "format": "json",
        "address": query, "key": key,
    }).get("response") or {}
    if body.get("status") != "OK":
        return ""
    structure = (body.get("refined") or {}).get("structure") or {}
    code = str(structure.get("level4LC") or "")
    same = (
        str(structure.get("level2") or "").replace(" ", "").startswith("전주시")
        and str(structure.get("level4L") or "") == dong
        and str(structure.get("level5") or "").replace(" ", "") == lot.replace(" ", "")
    )
    return code[:10] if same and len(code) >= 10 and code[:10].isdigit() else ""


def parcel_point(client: httpx.Client, key: str, pnu: str) -> tuple[float, float] | None:
    params = {
        "service": "data", "request": "GetFeature", "data": "LP_PA_CBND_BUBUN",
        "key": key, "geometry": "true", "format": "json", "size": "1",
        "attrFilter": f"pnu:=:{pnu}", "crs": "EPSG:4326",
    }
    body = _get(client, VWORLD_URL, params).get("response") or {}
    if body.get("status") != "OK":
        return None
    features = ((body.get("result") or {}).get("featureCollection") or {}).get("features") or []
    if not features:
        return None
    geometry = features[0].get("geometry") or {}
    coords = geometry.get("coordinates") or []
    if geometry.get("type") == "MultiPolygon":
        coords = coords[0] if coords else []
    if not coords:
        return None
    point = Polygon(coords[0]).representative_point()
    return point.y, point.x


def main(argv: list[str]) -> int:
    source = Path(argv[1])
    out = Path(argv[2]) if len(argv) > 2 else DEFAULT_OUT
    raw = source.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp949")
    rows = list(csv.DictReader(text.splitlines()))
    env = read_env()
    vworld_key = env["VWORLD_API_KEY"]
    results: list[dict[str, str]] = []
    stats = {"rows": len(rows), "parsed": 0, "pnu": 0, "point": 0}
    with httpx.Client(timeout=20.0) as client:
        for row in rows:
            jibun = " ".join((row.get("공장대표주소(지번)") or "").split())
            record = {
                "seq": row.get("순번", "").strip(),
                "name": " ".join((row.get("회사명") or "").split()),
                "road_address": " ".join((row.get("공장대표주소(도로명)") or "").split()),
                "jibun_address": jibun,
                "pnu": "", "extra_lots": "0", "lat": "", "lng": "",
                "as_of": row.get("데이터기준일자", "").strip(),
            }
            parsed = parse_lot(jibun)
            if parsed:
                stats["parsed"] += 1
                query, dong, lot, san, main_no, sub_no, extra = parsed
                record["extra_lots"] = str(extra)
                code = dong_code(client, vworld_key, query, dong, lot)
                if len(code) == 10:
                    pnu = f"{code}{'2' if san else '1'}{main_no:04d}{sub_no:04d}"
                    record["pnu"] = pnu
                    stats["pnu"] += 1
                    point = parcel_point(client, vworld_key, pnu)
                    if point:
                        record["lat"], record["lng"] = f"{point[0]:.7f}", f"{point[1]:.7f}"
                        stats["point"] += 1
            results.append(record)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)
    print(stats, "→", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
