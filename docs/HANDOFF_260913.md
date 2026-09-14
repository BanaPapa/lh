# 작업 인계 — 2026-09-13

> **다른 PC 또는 새 AI 세션이 이어받기 위한 문서.**
> 8-30 문서의 §2(옮겨야 하는 파일)는 여전히 유효하다 — 단, **이 PC(Space)에는 2026-09-12 에 전부 배선을 끝냈다**(§1-2).
>
> 좌표계·PNU·지적도 개념이 필요하면 `docs/COORDINATES_PRIMER_2026-09-13.md` 를 먼저 읽는다(A부에 프로젝트 브리핑도 있다).

읽는 순서: **이 문서** → `docs/hazards/INDEX.md`(판정 정본) → `docs/hazards/TIMELINE.md §8`(09-11 결정) → `docs/SCREENING_ENGINE_GUIDE.md` → `backend/tools/lh_baseline/README.md`.

---

## 0. 한 문단 요약

2026-09-11 LH 2차 보고 회의의 확인 요청 15항목 결정을 **엔진·화면·도구에 전부 반영**했고(규칙팩 v1.5 → **v1.6**), 결정 전 기준선(9/11 Codex 실측)과 118·17·15건을 전수 대조한 회귀 보고를 냈다. 백엔드 테스트 **512 passed**, 프런트 타입체크·빌드 통과, Codex 리뷰 7건 중 6건 반영(1건 오탐). **전부 미커밋 상태다**(§5). 이어받을 사람이 할 첫 일은 **네이버 검색 키를 넣고 15·17건을 다시 돌리는 것**(§4-1)이다 — 지금 수치는 대학·종합병원 정문이 좌표 폴백인 채로 나온 것이다.

---

## 1. 환경

### 1-1. 실행
```bash
cd backend && .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
cd frontend && npm run dev            # http://localhost (포트 80)
cd backend && .\.venv\Scripts\python.exe -m pytest -q      # 512 passed
cd frontend && npm run lint && npm run build
```

⚠️ **`--reload` 가 코드 변경을 놓친 사례가 두 번 있었다**(에이전트가 여러 파일을 고친 뒤 OpenAPI 스키마가 옛 코드 그대로였다). 에이전트 작업 뒤에는 무조건 **수동 재기동**하고 `/openapi.json` 으로 새 필드가 있는지 확인한다. 죽일 때는 부모 uvicorn 뿐 아니라 **자식 python 프로세스**까지 죽여야 포트가 풀린다(8-30 문서 주의사항 4 와 같은 결함).

### 1-2. 이 PC 의 로컬 원천 배선 (2026-09-12 완료)
`backend/.env` 에 아래 7개가 `C:\Users\Space\Documents\카카오톡 받은 파일\LH 용역\` 아래를 가리키도록 들어 있다(슬래시 경로).

| 키 | 가리키는 것 |
|---|---|
| `LH_SOURCE_DIR` | `결과물/AI공모전` — 118건 원장 |
| `LH_LOCAL_RAW_PATH` | `전북시설원본.zip` — factoryON 등록공장 7,983·CNG·소음 CSV |
| `LH_LOCAL_STANDARD_PATH` | `LH 심사 지원 앱_v3/data/update-delivery/standardized/facilities.xlsx` — 박진주 **0910판** |
| `CADASTRAL_JEONBUK_SHP` / `CADASTRAL_JEONBUK_DB` | `delivery/spatial/cadastral_jeonbuk/*.shp` → `backend/data/cadastral_jeonbuk.sqlite` (**3,888,333 필지 · R-Tree · 2.5GB**, 빌드 31분) |
| `NOISE_EMISSION_CSV_PATH` | `…/전북시설원본/유해시설/08_noise_vibration_facilities.csv` |
| `LH_LEGAL_DONG_PATH` | `delivery/metadata/legal_dong_codes.xlsx` (키 이름 정정 — 8-30 문서의 `LEGAL_DONG_PATH` 는 틀린 이름이었다) |

인허가 원장 재동기화 완료: 석유판매업 13,106행(휴업 619) · 고압가스 16,472행(휴업 744) · 특정고압가스 6,988행. 대표 5건 예열: LPG 3,661 · 화장시설 61 · 소음 146 · 결손 0.

### 1-3. 아직 비어 있는 키 — ★ 결과에 영향
- **`NAVER_SEARCH_CLIENT_ID` / `NAVER_SEARCH_CLIENT_SECRET` 이 빈 값이다.** 대학·종합병원 정문 자동 조회, 역 출구 후보가 전부 이 키를 쓴다. 지금은 **좌표 폴백**(「정문 미확인」 고지)이라 청년 유형 교육 점수·정문 기준 거리가 확정값이 아니다.
- `SAFEMAP_API_KEY` 빈 값(주유소 보조 원천 — 인허가 원장으로 대체되므로 급하지 않음).
- 카카오 지도 **JS 키는 브라우저 localStorage 에만** 있다. 헤드리스 검수에서는 지도가 안 뜬다.

---

## 2. 2026-09-11 결정 ↔ 반영 (규칙팩 v1.6)

| # | 결정 | 반영 위치 | 상태 |
|---|---|---|---|
| 1 | 등록공장 **전체** 「공장 있음」 검토 표시만, 자동 제외·가~라목 매칭 없음 | `rulebook.py` RB14-FACTORY 카테고리 5종 → **`factory_registered` 1종**, `service.py` factoryON 후보만·대기/소음은 40m 이내 주석 | ✅ |
| 2 | 휴업 판정 포함 | `rulebook.operating_state("휴업") → active` | ✅ |
| 3 | 고압가스 자가설비 제외(검토도 없음) | `service.is_self_use_gas(명칭, 업태, 제조구분)` — 원장 실측값 업태 `제조/저장소/판매`, 제조구분 `냉동/일반/충전/특정` | ✅ |
| 4 | 자동차용 LPG 충전소 25m | `lpg_station` → RB14-FUEL25 (판매소·저장소는 HAZMAT 50m 유지) | ✅ |
| 5 | 석유대체연료 25m + 용도지역 병기, 주거지역 「확인 요청」 | `vworld.zoning_at`(LT_C_UQ111 `uname`, 20m 재조회, 캐시) · `service._attach_zoning/_petroleum_zoning_hold` · 프런트 배지 + 지적편집도(`USE_DISTRICT`) 토글 | ✅ |
| 6 | 생활숙박 제외 | `숙박업(생활)` 6,855행 제외 필터(코드가 결정과 반대로 돌고 있었음) | ✅ |
| 7 | 기능대학·전문대·사이버대 포함(LH 확인 대기) | `UNIVERSITY_KINDS` — 이미 포함 | ✅ |
| 8 | 대형 필지 시설 정문 기본 + 담당자 필지·문 선택 재계산 | `amenities._measure_with_front_door`(대학·종합병원 공용), `/api/screening/front-doors` 임의 시설명·PNU/좌표, 프런트 「기준점 지정」 모드 | ✅ |
| 9 | 다필지 대표+인접 합집합 + 담당자 추가 | `multi_parcel.parse_multi_parcel_address` + `ParcelResolver.resolve`(전 지번 해석·PNU 중복 제거·대표 첫 항목), 프런트 `slice(0,1)` 제거 | ✅ |
| 11 | 정문·출구 좌표, 여럿이면 모두 표시·최근접·담당자 선택 | `front_door_candidates[]` + 프런트 문 핀/드롭다운, 역·터미널 수기 지정도 측정 반영 | ✅ |
| 15 | 여유구간 100m | 이미 100m. 예비검색 슬랙 `SEARCH_SLACK_M=150` 을 **분리**(조준환 엔진 5건 누락 사례 예방) | ✅ |
| 기타② | 1차 제외 건 2차 참고 점수 | 이미 구현(`reference_only`) | ✅ |
| 12·13·14 | 표본·공고문·KPI | LH 대기 / 09-22 서면 협의 | — |

정본 문서 갱신: `TIMELINE.md §8`, `INDEX.md §10-1`, `MEASUREMENT.md §3`, `H-01-공장-공통`, `H-02-가`, `H-02-나`, `H-02-라바`, `H-05`, `SCREENING_ENGINE_GUIDE.md §4.2.1·4.2.2`.

Codex 리뷰(`.claude-review/codex-review.md`) 반영 6건: 역·터미널 수기 지정 측정 반영 / 대학병원 정문 캐시 키 / 다필지 첫 지번 누락 / 참고반경 0 일 때 슬랙 / CORS `DELETE`(프런트가 vite 프록시 없이 :8000 직접 호출 — 개발에서도 CORS 적용됨) / 공백 정규화 키 비교. 오탐 1건(untracked 파일 누락 지적).

---

## 3. 재검산 결과 (2026-09-13, `docs/reports/regress_20260913/`)

기준선 = 결정 전(커밋 8b2201d, 9/11 08:15 Codex 실측, 이 PC 로컬 원천 미배선 상태). 원천 배선으로 인한 변화는 「WIRING(환경 차이)」로 분리 집계.

| 표본 | 1차 전→후 (적격/검토/부적격) | 2차 확정 | LH 일치 | 원인 |
|---|---|---|---|---|
| 118건(주택·일반) | 110/2/6 → **90/21/7** (22건 변경) | 105→116 | — | #1 공장 23 · #9 다필지 5 · #3 자가설비 3 · #2 휴업 1 · 슬랙 1 · 미귀속 0 |
| 17건(LH 표본) | 16/0/1 → 16/0/1 | 13→17 | **5→7** | WIRING · #1 1 · #9 1 |
| 15건(조준환 검증) | 14/0/1 → 14/0/1 | 11→15 | **4→6** | 동일 |

대표 5건 실측(1단계 직후): 041 진북동 통과→**제외**(어은터널 주유소·휴업 14.9m), 105 수계리 검토→**통과**(완주소방서 자가설비 해제), 006 금암동 통과→**검토**(등록공장 극동간판 16m), 004 송천동 제외/오피스텔 37점(LH 37), 001 통과 35점(LH 35).

조준환 v1.5 와의 비교 포인트: 118건 제외 8/검토 5(원장 매칭분) · 등록공장 전체 레이어 예비 적용 시 검토 40 ↔ 우리 제외 7/검토 21(factoryON 전체 원본). 9/18 전 **제외 1건 차이의 접수번호를 짚어야 한다.**

⚠️ 118건 배치는 Codex 수정 **전** 코드로 돌았다(다필지 첫 지번 보정 등). 방향은 같으나 제출 전 재실행이 맞다(§4-2).

산출물: `LH_판정로직_v1.6_결정반영_회귀검증_정민재앱_2026-09-13.html`(44KB, 외부 요청 0) · `regress.json` · `정민재_{118,17,15}건_결과_20260913.json` · `qa_1440.png`. 생성기 `backend/tools/lh_baseline/regress_0913.py`. **`docs/*` 는 gitignore 라 이 폴더는 커밋되지 않는다** — 전달은 파일로.

---

## 4. 다음 작업 (9/18 금 마감)

### 4-1. ★ 네이버 키 → 15·17건 재실행
`.env` 에 `NAVER_SEARCH_CLIENT_ID/SECRET` 채우고 서버 재기동 후:
```bash
cd backend
PYTHONIOENCODING=utf-8 .\.venv\Scripts\python.exe tools/lh_baseline/run_15_cases.py
PYTHONIOENCODING=utf-8 .\.venv\Scripts\python.exe tools/lh_baseline/run_17_cases.py
```
060 익산 부송동(청년) 교육 점수와 004·108 종합병원 거리가 정문 기준으로 바뀌는지 본다.

### 4-2. 118건 재실행 + 보고 재생성
```bash
PYTHONIOENCODING=utf-8 .\.venv\Scripts\python.exe -m tools.lh_baseline.run_ours     # 10~20분
PYTHONIOENCODING=utf-8 .\.venv\Scripts\python.exe tools/lh_baseline/regress_0913.py
```
일시 조회 실패(`living_score=None`)는 `python -m tools.lh_baseline.rerun_cases 002 109` 식으로 그 건만.

### 4-3. 브라우저 검수 (지도 키 있는 환경)
2차 근거 시설 핀·최단거리선·거리 라벨, 문 후보 핀 클릭→재실행, 기준점 지정 모드에서 필지 클릭(PNU)/빈 곳 클릭(좌표), 「해제」, 용도지역 토글·자동 점등. 헤드리스로는 심사표 쪽(행·배지·지정 모드 안내·Esc·모바일 400px)만 확인했다.

### 4-4. 데이터 교체 대기
- 박진주 0908 등록공장 전체 레이어(7,923) 확정본 → `LH_LOCAL_RAW_PATH` 의 factoryON 원본과 교체 여부 결정
- 박진주 9/18 표준셋 최종본 → `LH_LOCAL_STANDARD_PATH` 교체
- 표준셋에 「자가설비 열」「용도지역 열」이 들어오면 명칭 패턴·VWorld 조회를 그 열로 대체

### 4-5. LH 회신 반영
#7 기능대학 「제외」로 오면 `UNIVERSITY_KINDS` 옆 이름 예외 한 줄. #13 공고문 원본 오면 `MEASUREMENT.md` 문언 재대조.

---

## 5. 커밋 안 된 것 (2026-09-13 기준)

작업 트리 전체가 미커밋이다. 권장 분할:

| 커밋 | 포함 |
|---|---|
| ① 배선·원장 | `settings_api/store.py`, `.env.example`, `data/facilities.db`(재동기화 +2MB), `wiring.py`, `local_wiring.py` |
| ② 엔진 결정 반영 | `hazard_review/rulebook.py`·`service.py`·`models.py`, 관련 테스트, `docs/hazards/*` |
| ③ 정문·다필지·용도지역 | `hazard_review/parcels.py`·`multi_parcel.py`(신규), `screening/*`, `services/vworld.py`, `main.py`, 테스트(`test_multi_parcel.py`·`test_vworld_zoning.py` 신규) |
| ④ 프런트 | `frontend/src/**`(`screeningOverlays.ts` 신규 포함) |
| ⑤ 도구 | `tools/lh_baseline/run_*.py`, `regress_0913.py`(신규), `test_regress_0913.py`(신규), `data/ours_5site_20260908.json` |
| 보류 | `tools/lh_baseline/*_0911.py` 4개(Codex 검증기록부 생성기 — 커밋 여부 결정), 유해요소 정리본 폴더(정본은 `docs/hazards/` — 이후 삭제됨) |

`docs/*` 는 `.gitignore` 라 `HANDOFF_*.md`·`COORDINATES_PRIMER_*.md`·`reports/` 는 예외 규칙(`!docs/…`)이 있어야 올라간다. 8-30·9-11 인계문서가 추적되는 규칙을 그대로 따른다(§6-1).

---

## 6. 주의사항 (이번에 새로 물린 것)

1. **`--reload` 를 믿지 말 것** — §1-1.
2. **`.env` 의 새 경로 키는 `PROCESS_ENV_KEYS` 에도 넣을 것** — `LH_LEGAL_DONG_PATH` 가 빠져 있어 설정해도 프로세스에 안 올라갔다.
3. **printf/heredoc 으로 `.env` 에 Windows 경로를 쓰지 말 것** — `\b`·`\f`·`\0` 이 이스케이프로 먹혀 경로가 깨졌다. 파이썬으로, 슬래시 경로로 쓴다.
4. **세션이 끊기면 백그라운드 에이전트·dev 서버가 함께 죽는다.** 재개 시 `git status` 로 부분 작업을 확인하고 같은 에이전트를 `SendMessage` 로 이어 붙인다(1단계·4단계가 그렇게 복구됐다).
5. **원천 배선 차이와 결정 차이를 섞지 말 것** — 회귀 보고에서 WIRING 을 별도 원인으로 둔 이유. 다른 PC 의 결과와 비교할 때 먼저 `dataset_missing` 범주 수를 맞춘다.
6. 8-30·9-11 문서의 주의사항 6개는 그대로 유효하다.
