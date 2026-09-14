"""지도 켜진 상태로 판정 화면을 캡처한다."""
from __future__ import annotations
import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(r"C:\Users\Bana\orca\lh_mvp\docs\shots")
OUT.mkdir(parents=True, exist_ok=True)
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass


def judge(page, addr, officetel=False):
    page.goto("http://localhost/", wait_until="networkidle", timeout=60000)
    time.sleep(3)
    ok = page.evaluate("typeof kakao !== 'undefined' && !!(kakao.maps)")
    print(f"   [지도 SDK] {ok}")
    page.query_selector("input[placeholder*='주소']").fill(addr)
    page.keyboard.press("Enter"); time.sleep(4)
    for label in ("유해시설", "심사"):
        el = page.query_selector(f"text={label}")
        if el:
            try: el.click(); time.sleep(0.5)
            except Exception: pass
    if officetel:
        el = page.query_selector("button:has-text('주거용 오피스텔')")
        if el: el.click(); time.sleep(1)
    page.query_selector("button:has-text('분석 실행')").click()
    for i in range(150):
        time.sleep(2)
        t = page.inner_text("body")
        if "검토 진행 중" not in t and "분석 중지" not in t and ("부적격" in t or "적격" in t):
            print(f"   [완료] {i*2}초"); break
    time.sleep(5)
    return ok


with sync_playwright() as pw:
    b = pw.chromium.launch()
    page = b.new_page(viewport={"width": 1600, "height": 1000}, device_scale_factor=2)

    print("== 004 · 주택·일반 (조준환님 조건) ==")
    judge(page, "전주시 덕진구 송천동1가 626-74")
    page.screenshot(path=str(OUT / "map_004_house.png"))
    print("   [저장] map_004_house.png")
    for t, n in (("적용 임계거리", "map_004_threshold"), ("유해시설", "map_004_hazard")):
        el = page.query_selector(f"text={t}")
        if el:
            el.scroll_into_view_if_needed(); time.sleep(1.5)
            page.screenshot(path=str(OUT / f"{n}.png")); print(f"   [저장] {n}.png")

    print("\n== 082 · 주택·신혼 (두 엔진이 함께 제외한 건) ==")
    judge(page, "전주시 완산구 효자동2가 363-2")
    body = page.inner_text("body")
    print("   판정:", "부적격" if "부적격" in body else "적격")
    page.screenshot(path=str(OUT / "map_082.png")); print("   [저장] map_082.png")

    print("\n== 004 · 주거용 오피스텔 ==")
    judge(page, "전주시 덕진구 송천동1가 626-74", officetel=True)
    body = page.inner_text("body")
    print("   판정:", "부적격" if "부적격" in body else "적격")
    el = page.query_selector("text=적용 임계거리")
    if el: el.scroll_into_view_if_needed(); time.sleep(1.5)
    page.screenshot(path=str(OUT / "map_004_officetel.png")); print("   [저장] map_004_officetel.png")
    b.close()
