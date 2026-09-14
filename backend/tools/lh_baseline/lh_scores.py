"""LH 기존 웹앱 2종을 118건에 전수 적용해 기준선을 만든다.

04_ 2단계는 시설 DB 파일을 하나만 올려 쓰는 구조라, 사업지가 속한 시군의
마스터만 로드한 상태로 돌아간다. 그래서 두 가지를 함께 낸다.

  city  — LH 실제 운용대로 소재 시군 마스터 하나만 사용
  all   — 전북 7개 마스터를 합쳐 사용(행정경계로 자르지 않았을 때)

둘이 갈리면, 그 차이는 거리 산정이 아니라 '행정구역으로 잘린 데이터' 탓이다.
"""

from __future__ import annotations

import json
import os

from tools.lh_baseline.lh_replica import (
    STATUS_LABEL,
    analyze_site,
    calc_scores,
    hav_km,
    load_amenities,
    load_hazards,
    load_sites,
)

OUT = os.path.join(os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"), "lh_118.json")

# 원장의 '지역' 값 → 시설 DB 마스터 파일명. 마스터가 없는 시군은 None.
CITY_TO_MASTER: dict[str, str | None] = {
    "전주시": "전주",
    "군산시": "군산",
    "익산시": "익산",
    "정읍시": "정읍",
    "김제시": "김제",
    "남원시": "남원",
    "완주군": "완주",
}


def scores_for(lat: float, lng: float, rows: list[dict]) -> tuple[int, int, int]:
    scored = [{**r, "dist": hav_km(lat, lng, r["lat"], r["lng"])} for r in rows]
    return calc_scores(scored)


def main() -> None:
    hazards = load_hazards()
    amenities = load_amenities()
    merged = [row for rows in amenities.values() for row in rows]
    sites = load_sites()

    out = []
    for site in sites:
        if not site["valid"]:
            out.append({"no": site["no"], "stage1": "NOCOORD"})
            continue

        one = analyze_site(site["lat"], site["lng"], hazards)
        master = CITY_TO_MASTER.get(site["region"])
        city_rows = amenities.get(master, []) if master else []

        city_scores = scores_for(site["lat"], site["lng"], city_rows) if city_rows else None
        all_scores = scores_for(site["lat"], site["lng"], merged)

        out.append(
            {
                "no": site["no"],
                "region": site["region"],
                "address": f"{site['region']} {site['detail']}",
                "lat": site["lat"],
                "lng": site["lng"],
                "stage1": one["status"],
                "stage1_label": STATUS_LABEL[one["status"]],
                "nearest": (
                    {
                        "name": one["nearest"]["name"],
                        "major": one["nearest"]["major"],
                        "mid": one["nearest"]["mid"],
                        "distance_m": round(one["distance"], 1),
                    }
                    if one["nearest"]
                    else None
                ),
                "master": master,
                "stage2_city": list(city_scores) if city_scores else None,
                "stage2_city_total": sum(city_scores) if city_scores else None,
                "stage2_all": list(all_scores),
                "stage2_all_total": sum(all_scores),
            }
        )

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    print(f"저장: {OUT} ({len(out)}건)")
    print("시군별 마스터 행수:", {k: len(v) for k, v in sorted(amenities.items())})
    print("합본 행수:", len(merged))


if __name__ == "__main__":
    main()
