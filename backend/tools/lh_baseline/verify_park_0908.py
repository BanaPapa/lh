"""박진주 대표님 0908 정정본이 회신 내용대로 반영됐는지 확인한다."""
import io, os, sqlite3, sys, zipfile, collections, openpyxl
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8")
    except Exception: pass

# 진용성님 배포본에 동봉된 0903 판(대조 기준). 환경변수로 바꿀 수 있다.
BASE = Path(os.environ.get("LH_STANDARD_0903_DIR", Path(r"C:\Users\Bana\Downloads\LH 심사 지원 앱\data\update-delivery\standardized")))
# 박진주님 0908 정정본.
ZIP = Path(os.environ.get("LH_STANDARD_ZIP", Path(r"C:\Users\Bana\Documents\카카오톡 받은 파일\0908-standardized.zip")))

def rows_of(src, member=None):
    data = io.BytesIO(zipfile.ZipFile(src).read(member)) if member else src
    ws = openpyxl.load_workbook(data, read_only=True, data_only=True).worksheets[0]
    it = ws.iter_rows(values_only=True)
    hdr = [str(c or "") for c in next(it)]
    return [dict(zip(hdr, r)) for r in it]

def zeros(rows):
    return sum(1 for r in rows
               if len(str(r.get("pnu") or "")) >= 11 and str(r.get("pnu"))[10] == "0")

print("== ① PNU 필지구분 코드(11번째 자리) 오류 ==")
t03 = t08 = 0
for f in ("facilities", "facilities_review", "facilities_exclude"):
    a = rows_of(BASE / f"{f}.xlsx")
    b = rows_of(ZIP, f"standardized/{f}.xlsx")
    za, zb = zeros(a), zeros(b)
    t03 += za; t08 += zb
    print(f"   {f:20} 0903 {len(a):5}행·오류 {za:3}  →  0908 {len(b):5}행·오류 {zb:3}")
print(f"   {'합계':18} 0903 {t03}건  →  0908 {t08}건")

print("\n== ④⑤ 위치 검토 표시 (협회안 대조 후 유지분) ==")
main = rows_of(ZIP, "standardized/facilities.xlsx")
for k, v in collections.Counter(
        str(r.get("location_review_status") or "(표시 없음)") for r in main).most_common():
    print(f"   {k}: {v}")

print("\n== 검토 파일로 옮긴 것 ==")
rev = rows_of(ZIP, "standardized/facilities_review.xlsx")
for k, v in collections.Counter(str(r.get("middle_category")) for r in rev).most_common():
    print(f"   {k}: {v}")

print("\n== ⑥ 박진주 대표님 지적 — 병원이 위험물로 잡히는 건 ==")
from app.services.facility_store import DEFAULT_DB_PATH
con = sqlite3.connect(str(DEFAULT_DB_PATH))
like = "(name like '%병원%' or name like '%의료원%')"
for label, extra in (("전국", ""), ("전북", " and address like '%전북%'")):
    n = con.execute(
        f"select count(*) from facilities where dataset_key='high_pressure_gas' and {like}{extra}"
    ).fetchone()[0]
    print(f"   고압가스업 원장 중 병원·의료원 상호 — {label}: {n}건")
print("   전북 사례(제조구분 포함):")
for r in con.execute(
        f"select name,category,address from facilities where dataset_key='high_pressure_gas' "
        f"and {like} and address like '%전북%' limit 6"):
    print(f"     {r[0][:26]:28} 제조구분={r[1]:6} {r[2][:38]}")
