# 작업 인계 — 2026-09-14

> **다른 PC 또는 새 AI 세션이 이어받기 위한 문서.** `HANDOFF_260913.md` 를 대체한다. 9/13 문서의 §1(환경·로컬 원천 배선), §2(9/11 결정 반영), §4(네이버 키·118건 재실행·데이터 교체·LH 회신)는 **여전히 유효**하며 여기서 반복하지 않는다.
>
> 좌표계·PNU·지적도 개념은 `docs/COORDINATES_PRIMER_2026-09-13.md`. 원천 API 현황은 `docs/API_SOURCE_RESEARCH_2026-09-14.md`.

읽는 순서: **이 문서** → `docs/PRODUCTION_ROADMAP_2026-09-14.md`(상용화 수정 목록·우선순위) → `docs/API_SOURCE_RESEARCH_2026-09-14.md` → `docs/HANDOFF_260913.md §1·§4` → `docs/hazards/INDEX.md`.

> 2026-09-14 결정: MVP에서 멈추지 않고 **상용화 수준**으로 간다. 고칠 것 전부는 로드맵 문서에 있다. 첫 스프린트는 로드맵 §1(P0: 인증, 잡 영속화, 루프 블로킹, 저장소 위생)이다.

---

## 0. 한 문단 요약

9/13~14 세션에서 네 가지를 했다. ① 지도 뷰포트 버그 2건(필지 추가 선택 시 축척 리셋, 같은 검색어 재클릭 무반응) 수정. ② 심사표 1차 판정 표에 **「데이터」 열** 추가 — 항목별로 판정에 실제 쓰인 원천을 API(링크)·로컬·데모 칩으로 표시. ③ **미연결 원천 전국 공개 API 조사**를 Grok·Codex 두 도구로 독립 수행해 종합(§3). ④ 지도 핀을 **1차 빨강·2차 파랑**으로 분리하고 지적편집도를 **사업지 3km까지 타일 방식**으로 확장. 백엔드 테스트 **528 passed**, 프런트 타입체크·빌드 통과. **전부 이 문서와 함께 커밋·푸시했다.** 이어받을 사람이 할 첫 일은 **지도 키 있는 브라우저에서 ④를 눈으로 확인하는 것**(§4-1)이다 — 이 세션의 헤드리스 검수에는 카카오 키가 없어 핀 색·타일 로딩을 실제로 보지 못했다.

---

## 1. 환경 — 9/13 §1 에 추가된 것

- **프런트는 80번 포트 고정.** `frontend/vite.config.ts` 가 host 가 `localhost` 가 아니면 `http://localhost/` 로 307 리다이렉트한다. 다른 포트로 띄워도 80번 인스턴스로 간다. 검수는 `http://localhost/`.
- **uvicorn `--reload` 가 또 죽었다.** 부모를 죽여도 `multiprocessing spawn_main` 자식이 8000 포트를 잡고 옛 코드를 계속 서빙했다. 확인법: `curl localhost:8000/openapi.json` 에 `HazardDataSource` 가 있으면 새 코드. 없으면 `Get-NetTCPConnection -LocalPort 8000` 으로 PID 전부 종료 후 재시작.
- 카카오 지도 JS 키는 브라우저 localStorage 에만 있다(설정 패널). 헤드리스에서는 지도가 안 뜨지만 검색·심사 실행·심사표 열기는 된다.
- Codex CLI 는 세션 중 **사용량 한도**에 걸렸다(마지막 재리뷰 미완). 한도 해제 후 `powershell -File ~/.claude/hooks/codex-review.ps1` 로 §2-④ 재리뷰 권장.
- 이 PC 에는 로컬 원천 파일(`LH_LOCAL_RAW_PATH` 등)이 배선돼 있었으나 검수 시점엔 등록공장·CNG 가 미적재로 나왔다(칩 빈칸). 다른 PC 에서 로컬 원천이 없으면 「데이터」 열에 로컬 칩이 안 보이는 게 정상이다.

---

## 2. 이번 변경 내역 (커밋 4개)

### ① 지도 뷰포트 (`frontend/src/App.tsx`, `components/MapPanel.tsx`)
- 뷰포트 재-fit 키에서 선택 필지 목록을 뺐다. 필지를 눌러도 축척·중심이 유지된다.
- `viewportRequest` 카운터(App → MapPanel prop): 검색 성공 시 후보가 같아도 올리고, 자동 필지 로드가 도형 있는 필지를 받았을 때도 올린다. 수동 필지 토글로는 올리지 않는다.
- `loadSiteParcel` 에 요청 순번 가드 — 늦게 온 옛 검색의 필지 응답을 버린다.
- 알려진 기존 동작: 검색 직후 큰 필지가 화면 밖으로 일부 잘릴 수 있다(고정 미리보기 레벨). 원하면 필지 경계 기준 fit 으로 바꾼다.

### ② 심사표 「데이터」 열
- 백엔드: `hazard_review/data_sources.py`(신규, provider → 라벨·URL 정본), `HazardCategorySummary.data_sources`, `_category_data_sources()`(연결된 원천만, **이번 요청에서 실패한 provider 는 제외**, 로컬 칩은 `LocalSourcesBundle.source_files` 의 **실제 적재 파일명**), `ScreeningExclusionItem.data_sources` pass-through. `services/local_wiring.py` 에 `LocalSourceFile`·`source_files` 추가.
- 프런트: `screening/ScreeningSheet.tsx` `renderDataSources()` — api=라벨+외부링크(`title` 에 URL), local="로컬"(`title` 에 파일명), demo="데모", 빈 배열=빈 셀. `data_sources` 누락(구버전 서버·캐시)에도 죽지 않게 방어. `screening.css` 칩 스타일 + 열 최소 폭(근거 240px·데이터 168px, 모바일은 표 가로 스크롤).
- 실측(건국대): 액화가스·고압가스 = 행안부 인허가 3종 API 칩, 주유소 = 석유판매업 + 오피넷, 화장장 = 복지부 API. 유흥·단란·숙박은 「일반」 유형에서 미적용이라 빈칸(정상).
- 표준본 등록공장 칩의 부연이 `facilities.xlsx · 표준 공장(PNU 보유)` 로 길다. 줄이려면 `STANDARD_SOURCE_FILTERS` 라벨을 따로 노출해야 한다(설계 판단 필요).

### ③ 원천 API 조사 (`docs/API_SOURCE_RESEARCH_2026-09-14.md`, `docs/research/`)
§3 참조.

### ④ 지도 핀 색·지적도 3km (`components/MapPanel.tsx`, `hazard-review/cadastralTiles.ts`(신규), `hazard-review.css`, `backend/app/hazard_review/router.py`)
- 검은 핀 원인: 2차·기준 밖 핀이 배경에 다크 테마 토큰 `--raise`(#161e30)를 써서 밝은 지도 타일 위에서 검게 보였다. 지도 위 요소는 **테마와 무관한 고정색**을 쓴다.
- 1차 = 빨강(저촉 채움 "!", 검토 채움 "?", 도형 미확보 점선 "?"), 2차 = 파랑 채움, 기준 밖 = 흰 배경 회색 윤곽. 도형 있는 1차 시설은 **항상** 빨강 폴리곤(fill 0.12, 선택 시 0.22, 폴리곤 클릭 = 시설 선택). 2차는 도형 데이터가 없어 핀만. 범례 순서 1차→2차→기준 밖→거리.
- 지적도: 사업지 3km 안을 0.008° 타일로 나눠 `idle`(0.3초 디바운스) 마다 뷰포트에 걸린 타일만 조회. 캐시·동시 4개·확대 게이트(레벨 ≤4)·3km 필터·폴리곤 2,500 상한·800건에서 잘리면 쿼드 분할(최대 0.002°). 키 없음·API 오류(200+빈 목록+note)는 캐시하지 않고 범례에 note 표시, 3회 연속 실패 시 중단. 사업지 변경 시 세대 번호로 옛 응답 폐기. `MAX_MAP_PARCELS` 600→800.
- 순수 계산은 `cadastralTiles.ts` 에 분리(SDK 없이 node 로 검증 가능).
- 잔여 리스크: 타일 도착마다 오버레이 전체 재드로우 → 도심에서 버벅일 수 있음. 필요 시 증분 렌더.

---

## 3. 원천 API 조사 결론 (요약 — 상세는 `docs/API_SOURCE_RESEARCH_2026-09-14.md`)

| 판정 | 항목 | 다음 행동 |
|---|---|---|
| **API 있음, 인증·배선만 남음** | 소음진동 `15139233`, CNG `15001508`(위경도 있음), 도시가스·고압가스(LOCALDATA), 테마파크 3종·무도장·무도학원(LOCALDATA), 건축물대장 `15134735`, 등록공장 `15087611`(키 동작, 좌표 없음), LPG 판매소 `15091481`(2024 일회성) | 공공데이터포털 활용신청 8종 → 실호출로 401/403 원인 분리 → 어댑터 |
| **전국 시설별 위치 API 미확인** | LPG 저장소, 위험물 취급소, 화약류, 카지노, 특정대기유해물질, 유독물, 도료류, 도시가스 제조시설 | 기관 협의·수기 유지. 후보 탐색용 보조(PRTR, 생활안전지도 IF_0049, 상가정보)는 판정 확정에 쓰지 않음 |

기존 문서 정정 필요: 매트릭스 #17 CNG 「공개 API 없음」 틀림 · `LH_CRITERIA_COVERAGE.md`(8-29) 가 테마파크·무도업·건축물대장을 「연결」로 적어 매트릭스와 충돌 · `services/localdata.py` 가 좌표 없는 행을 버려 「주소 지오코딩 가능」 범위와 「현재 적재」 범위가 다름.

두 도구 모두 **인증키로 실호출은 안 했다.** 소방청·경찰청 본문 접근이 막혀 「미확인」은 부존재 증명이 아니다.

---

## 4. 다음 작업

### 4-1. ★ 브라우저 검수 (지도 키 있는 환경)
전주 덕진구 용산3길 6-4(청년) 검색 → 심사 실행 후:
- 1차 핀 빨강·2차 핀 파랑·기준 밖 흰 원 회색 테두리인지, 다크 테마에서도 검은 원이 없는지.
- 1차 시설 폴리곤이 항상 빨갛게 깔리고 클릭하면 선택되는지.
- 지도를 끌면 필지 경계가 따라오는지, 축소하면 "확대하면 필지 경계가 표시됩니다", 3km 밖으로 나가면 안내가 뜨는지, 도심에서 버벅임 정도.
- 심사표 「데이터」 열 칩 링크가 새 탭으로 열리는지.

### 4-2. 활용신청 (사용자 계정)
공공데이터포털에서 §3 첫 줄 8종 신청. 승인 후 `.env` 키 확인 → 실호출 → 401/403 이 남으면 키 전달 방식·인코딩·경로 변경 순으로 분리.

### 4-3. 승인과 무관하게 지금 할 수 있는 코드
1. CNG ODcloud 어댑터(`services/cng.py` 신규) + `_category_connected` 의 로컬 전용 분기 제거 + 룰북·`service.py` 의 「CSV만」 주석 정정.
2. 건축물대장 교차확인 배선(어댑터는 있음) → 단란주점·테마파크 AND 조건.
3. 등록공장 생산정보 v2 어댑터 + 카카오 지오코딩 + 필지 API PNU 검증.
4. `HAZARD_SOURCE_MATRIX.md` #17·`LH_CRITERIA_COVERAGE.md` 정정.

### 4-4. 9/13 §4 그대로
네이버 검색 키 → 15·17건 재실행, 118건 재실행, 박진주 데이터 교체, LH 회신 반영.

---

## 5. 커밋 (2026-09-14)

| 커밋 | 포함 |
|---|---|
| ① 지도 뷰포트·핀 색·지적도 3km | `frontend/src/App.tsx`, `components/MapPanel.tsx`, `hazard-review/cadastralTiles.ts`, `hazard-review.css`, `backend/app/hazard_review/router.py` |
| ② 심사표 데이터 열 | `backend/app/hazard_review/{data_sources.py,models.py,service.py}`, `screening/{models.py,service.py}`, `services/local_wiring.py`, 테스트 2개, `frontend/src/screening/*`, `screening.css` |
| ③ 문서 | `docs/API_SOURCE_RESEARCH_2026-09-14.md`, `docs/research/*`, 이 문서, `.gitignore` 예외 |
| ④ 도구·정리본 | `backend/tools/lh_baseline/*_0911.py` 4개, 유해요소 정리본 폴더(정본은 `docs/hazards/` — 이후 삭제됨) |

---

## 6. 주의사항 (이번에 새로 물린 것)

1. API 응답에 새 필드를 붙이면 프런트 렌더 함수는 **필드 누락을 방어**한다. 구버전 서버가 돌던 중 심사표 전체가 하얗게 죽은 사례가 있다.
2. 지도 위 HTML 오버레이(핀·라벨·범례 스와치)는 테마 토큰을 쓰지 않는다. 지도 타일은 항상 밝다.
3. 디자인 훅(impeccable)이 `screening.css` 176·936행, `hazard-review.css` 360행의 좌측 색 테두리를 매번 지적한다. 모두 기존 코드의 판정 결과 색 표시라 그대로 뒀고 억제 규칙도 넣지 않았다. 없앨지는 사용자 결정.
4. Codex 리뷰 훅은 `git diff` 만 보내서 **신규 파일을 못 본다.** "모듈 없음" P1 은 오탐이다. 신규 파일이 있으면 그 사실을 먼저 확인한다.
