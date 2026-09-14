# 상용화 로드맵 — 수정·최적화 항목 전체 목록 (2026-09-14)

> 결정: 이 앱은 MVP에서 멈추지 않고 **상용화 수준**으로 간다. 적용 가능한 최적화는 다 적용한다.
> 이 문서는 2026-09-14 코드 감사(파일:행 근거)를 바탕으로 **고쳐야 할 것 전부**를 우선순위와 완료 기준과 함께 적는다. 항목을 끝내면 이 문서의 상태 칸을 고친다. 새 구멍을 찾으면 여기에 추가한다.
>
> 관련 문서: `HANDOFF_260914.md`(작업 인계), `API_SOURCE_RESEARCH_2026-09-14.md`(원천 API 현황), `COORDINATES_PRIMER_2026-09-13.md`(좌표계).

우선순위 기준:
- **P0** 배포하는 순간 사고가 나는 것. 외부에 여는 것 자체가 불가능한 이유.
- **P1** 첫 운영 달 안에 반드시. 없으면 장애를 못 보거나 못 고친다.
- **P2** 성능·비용·품질 최적화. 사용자 수가 늘면 체감된다.
- **P3** 장기 구조 개선.

---

## 0. 현재 상태 한눈에

| 영역 | 현황 | 근거 |
|---|---|---|
| 인증·인가 | 전 엔드포인트 무인증. 설정 API만 루프백 검사 | `settings_api/router.py:66-78` |
| 심사 작업 저장 | 프로세스 메모리 dict 3벌. TTL·상한·영속화 없음. 취소가 task.cancel() 안 함 | `screening/router.py:51-54,425-437`, `main.py:58-59`, `hazard_review/router.py:52-55` |
| 이벤트 루프 | 2.5GB·60MB sqlite 조회와 shapely 연산이 async 경로에서 동기 실행 | `screening/amenities.py:908,1029,1246`, `hazard_review/service.py:989,1592,1490`, `services/geo.py` |
| 외부 API | 타임아웃 O. 재시도 3곳. 회로차단·레이트리밋 0. 커넥션 풀 미사용 | `services/http_client.py:14-16`, `localdata.py:401` |
| 관측성 | 전역 예외 핸들러 0, 요청 ID 0, 구조화 로깅 0, APM 0. `/api/health`는 키 유무만 | `main.py:89-99` |
| CI·배포 | `.github/` 없음, Dockerfile 없음, 배포 스크립트 없음 | 저장소 전역 |
| 백엔드 테스트 | 35파일 10,993줄. pytest 설정 파일·conftest·커버리지 없음. 비동기 스타일 혼재 | `backend/tests` |
| 프론트 품질 | 테스트 러너 0, ESLint·Prettier 0, `lint`는 사실 `tsc -b` | `frontend/package.json:10` |
| 프론트 구조 | `MapPanel.tsx` 약 2,000행, `ScreeningSheet.tsx` 약 1,000행, `App.tsx` useState 30여 개 프롭 드릴링, 전역 상태 없음, 코드 분할 0, 에러 바운더리 0 | `frontend/src` |
| 프론트 번들 | JS 343KB·CSS 253KB 단일 청크, `styles.css` 147KB, 폰트 woff2 92개 3.1MB | `frontend/dist` |
| 데이터 파이프라인 | 인허가 동기화 수동 CLI, 스케줄러 0, 지적도 빌드 CLI 진입점 없음, 소요 시간 문서 불일치(8분 vs 30분) | `sync_facilities.py`, `cadastral_local.py` |
| 저장소 위생 | `facilities.db`(60MB)는 `.gitignore` 로 추적 제외, `sync_facilities.py` 로 재생성 | `.gitignore` |
| 의존성 | 백엔드 9개 `==` 고정, lock 없음. 프론트 `^` 범위. venv에 requirements 밖 패키지(playwright, bs4) | `requirements.txt`, `package.json` |

---

## 1. P0 — 배포 전 필수

### 1-1. 인증 레이어
- **문제**: 심사·유해요소 API 전부 무인증. 설정 API의 루프백 검사는 프록시·컨테이너 뒤에서는 모든 요청이 루프백으로 보여 무력화된다. 외부인이 `PUT /api/settings/keys`로 서버 키를 덮어쓰고 `DEMO_MODE`를 켤 수 있다.
- **조치**: (1) 설정 API는 환경 플래그(`SETTINGS_API_ENABLED=false` 기본)로 운영에서 비활성. (2) 나머지 API에 최소 API 키 헤더(서비스 간) 또는 세션 로그인(사용자용) 도입. 사용자 계정이 필요하면 OIDC(카카오/구글) 연동. (3) CORS `allowed_origins`를 환경별 값으로, 기본값 localhost 제거.
- **완료 기준**: 무인증 요청이 401. 설정 API가 운영 빌드에서 404. 테스트로 고정.

### 1-2. 비밀값 관리
- **문제**: 외부 API 키가 `backend/.env`에 평문, 설정 API가 파일에 직접 기록(`settings_api/store.py:139-169`).
- **조치**: 운영에서는 환경변수 주입만 허용(파일 쓰기 경로 차단). 시크릿 매니저(클라우드 KMS/Vault/Doppler 중 택1). 키 회전 절차 문서화.
- **완료 기준**: 운영 컨테이너에 `.env` 파일이 존재하지 않아도 기동.

### 1-3. 심사 작업 영속화와 큐
- **문제**: 잡·결과가 메모리 dict. 재시작 시 전량 유실. 무한 증식. 워커 2개면 `GET /jobs/{id}` 404. 취소가 협조적 플래그뿐이라 외부 API 쿼터를 계속 태운다.
- **조치**: (1) 잡 스토어를 Redis 또는 Postgres로(상태·진행률·결과 JSON·TTL 7일). (2) 워커 분리(arq/RQ/Celery 중 택1, asyncio 친화면 arq). (3) 동시 실행 상한과 사용자별 큐. (4) 취소 시 `task.cancel()` + 외부 호출 중단. (5) 결과 재조회 API(사용자별 이력).
- **완료 기준**: 서버 재시작 후 진행 중 잡이 재개 또는 실패로 표시되고 완료 결과는 남는다. 워커 N개에서 동작.

### 1-4. 이벤트 루프 블로킹 제거
- **문제**: 2.5GB 지적도 sqlite(`amenities.py:1246`), 60MB 인허가 db(`amenities.py:1029`, `hazard_review/service.py:989,1592`), shapely/pyproj(`geo.py` 전체)가 async 핸들러 안에서 동기로 돈다. 심사 1건이 서버 전체를 멈춘다. 동기 `httpx.Client`도 상주(`address_pnu_kakao.py:103`, `cadastral_vworld.py:150`).
- **조치**: (1) sqlite 조회는 전부 `asyncio.to_thread`(이미 `service.py:1557`에 선례). (2) 도형 연산 묶음은 `ProcessPoolExecutor`(GIL 회피). (3) 동기 httpx → `AsyncClient`. (4) 워커를 잡 프로세스로 옮기면 API 프로세스는 가벼워진다(1-3과 연계).
- **완료 기준**: 심사 실행 중 `/api/health` p99 < 50ms. 부하 테스트(동시 5건)에서 응답 지연 없음.

### 1-5. 저장소 위생
- **상태**: `facilities.db` 는 추적 제외(`sync_facilities.py` 로 재생성), 스크린샷·중복 폴더는 저장소에 두지 않는다.
- **완료 기준**: clone 크기 < 20MB 유지.

---

## 2. P1 — 첫 운영 달

### 2-1. 관측성
- 전역 예외 핸들러(`@app.exception_handler(Exception)`) + 표준 오류 응답 스키마.
- 요청 ID 미들웨어(`X-Request-Id` 생성·전파·로그 바인딩).
- 구조화 로깅(JSON, `logging.dictConfig`), 잡 ID·사업지 ID·provider를 필드로.
- OpenTelemetry 트레이싱(FastAPI·httpx 자동 계측) + Sentry(예외).
- `/api/health`를 liveness/readiness로 분리. readiness는 지적도 sqlite 존재·인허가 db 신선도(N일 이내)·필수 키를 검사.
- 메트릭: 외부 API별 호출 수·실패율·지연, 잡 큐 길이, 캐시 적중률.

### 2-2. 외부 API 회복력
- 모든 어댑터에 재시도(지수 백오프, 429/5xx만) — 현재 카카오·VWorld·오피넷·SafeMap·KGS 없음.
- 회로차단기(provider별, 연속 실패 N회 → open, 반개방 재시도).
- 호출 측 레이트리밋(토큰버킷, provider 일일 쿼터 반영).
- `httpx.AsyncClient` 공유 커넥션 풀(현재 요청마다 생성, `http_client.py:14-16`).
- VWorld 실패를 `""`로 삼키는 것(`vworld.py:175-195`) → 실패와 빈 결과 구분.
- 응답 캐시: 메모리 TTL(600초)만 있음 → Redis 캐시로 워커 간 공유, provider별 TTL(지적도 30일, 인허가 1일, 지오코딩 90일).

### 2-3. CI/CD
- GitHub Actions: 백엔드 `ruff` + `mypy` + `pytest --cov`(임계 70%→80%), 프론트 `eslint` + `tsc` + `vitest` + `vite build`. PR 필수 게이트.
- `backend/pyproject.toml`로 pytest·ruff·mypy 설정 통합, `conftest.py`, `asyncio_mode=auto`, 비동기 테스트 스타일 통일.
- 의존성: 백엔드 `uv`/`pip-tools`로 lock, 프론트 `^` 유지하되 lock 커밋 검증. venv에만 있는 playwright·bs4는 `requirements-tools.txt`로 분리.
- Dockerfile(멀티스테이지: 프론트 빌드 → 정적 서빙, 백엔드 uvicorn+gunicorn 워커), docker-compose(api·worker·redis·nginx). 환경별 설정(`dev/stage/prod`).
- 프론트 정적 배포(CDN) + 백엔드 리버스 프록시. `vite.config.ts`의 80번 포트·localhost 리다이렉트 장치 제거.

### 2-4. 데이터 파이프라인 자동화
- 인허가 동기화를 스케줄(일 1회, 워커 크론)로. 실패 알림. `sync_state`를 readiness에 연결.
- 지적도 빌드 CLI(`python -m app.services.cadastral_local build --shp ... --db ...`) 추가, 소요 시간 실측해 문서 하나로 통일(현재 8분 vs 30분).
- 원천 shp 출처 URL·배포 주기(월)를 문서화. 월 1회 재빌드 절차.
- 데이터 버전(스냅샷 ID)을 심사 결과에 이미 실음 → 결과 재현 시 같은 스냅샷을 찾을 수 있게 스냅샷 보관 정책.

### 2-5. 프론트 안전망
- 에러 바운더리(패널 단위) — 지금은 렌더 예외 1건이 화이트 스크린(실제 발생 사례: 심사표 `data_sources` 누락).
- `apiRequest`에 `AbortController` 전달, 세대 비교로 버리던 요청을 실제 취소.
- 폴링(400회 고정 간격) → SSE 또는 WebSocket 진행 스트림. 과도기엔 지수 백오프.
- CSP 메타·보안 헤더(프록시에서), 지도 SDK 스크립트 SRI 불가하므로 도메인 allowlist.

---

## 3. P2 — 성능·품질 최적화

### 3-1. 프론트 아키텍처
- `MapPanel.tsx`(약 2,000행)를 훅·모듈로 분해: `useMapRuntime`(SDK 로딩), `useSiteOverlays`(사업지·밴드), `useHazardMarkers`(1차 핀·폴리곤), `useScreeningMarkers`(2차), `useCadastralTiles`(지적도). 각 훅은 오버레이 수명을 스스로 관리.
- `App.tsx` useState 37개 → 도메인별 store(zustand 권장, 의존성 작음): `siteStore`, `screeningStore`, `mapStore`. 프롭 드릴링 제거.
- 데이터 페칭을 TanStack Query로: 중복 제거·캐시·재시도·폴링 통합. `api-base.ts`의 fetch 집중화는 유지.
- `ScreeningSheet.tsx`(약 1,000행) 섹션 단위 분리.
- 컴포넌트 테스트(vitest + Testing Library): 심사표 행 렌더, 데이터 칩, 범례, 뷰포트 훅.

### 3-2. 프론트 번들·로딩
- 코드 분할: 심사표·설정 패널·인쇄 뷰를 `React.lazy`. 초기 JS 목표 < 150KB gzip.
- `styles.css` 147KB → CSS 모듈 또는 컴포넌트별 파일로 분리, 미사용 규칙 제거.
- Pretendard woff2 92개 서브셋 → 실제 쓰는 유니코드 범위만(한글 완성형 + 라틴), `font-display: swap`, preload 2개.
- 이미지·아이콘: lucide 트리쉐이킹 확인.
- Lighthouse 성능 90 이상을 CI에서 측정.

### 3-3. 지도 렌더링
- 지적도 오버레이 증분 렌더: 타일 도착마다 전체 재드로우 → 타일별 오버레이 집합을 유지하고 diff만 add/remove.
- 폴리곤 단순화: 축소 레벨에서는 Douglas-Peucker로 꼭짓점 수 감축(서버에서 레벨별 사전 계산).
- 핀 클러스터링: 2차 근거 시설이 수십 개일 때 축소 레벨에서 묶음.
- 장기 옵션(P3): 지적도를 벡터 타일(MVT)로 서버 생성(tippecanoe/PostGIS ST_AsMVT) + MapLibre 레이어. 카카오·네이버 SDK와 병행이 어려우므로 지도 엔진 전환 결정과 함께.

### 3-4. 백엔드 데이터 계층
- sqlite → PostgreSQL + PostGIS. 지적도(388만 필지)·인허가·시설 좌표를 공간 인덱스로. `facilities_around`·`parcel_by_pnu`가 SQL 한 번으로. 워커 간 공유 가능.
- 인허가 원장에 좌표 없는 행이 버려지는 문제(`localdata.py`) → 주소 지오코딩 파이프라인(배치, 캐시)으로 보완.
- 판정 엔진(`hazard_review/service.py` 3,056행) 분해: 후보 수집 / 거리 측정 / 규칙 적용 / 요약 조립을 모듈로. 규칙팩은 데이터(JSON/YAML)로 외부화해 LH 결정 변경 시 코드 배포 없이 반영.

### 3-5. 테스트 심화
- 계약 테스트: OpenAPI 스키마 스냅샷을 프론트 타입과 대조(openapi-typescript로 타입 생성).
- 골든 테스트: 118건 기준선(`tools/lh_baseline`)을 CI 야간 잡으로, 판정 변화 diff 리포트.
- 부하 테스트(k6/locust): 동시 심사 10건, 외부 API 모킹.
- E2E(Playwright): 검색 → 심사 → 심사표 → 인쇄. 지도 키는 CI 시크릿.

---

## 4. P3 — 장기

- 다중 테넌트(LH 지역본부별)와 권한(열람/심사/관리).
- 심사 결과 이력·감사 로그(누가 언제 어떤 규칙팩·스냅샷으로 판정했는지) — 판정 근거 재현이 상품 가치.
- 규칙팩 버전 관리 UI와 승인 워크플로.
- 전국 확장: 지적도 전국(수천만 필지) → PostGIS 필수. 좌표계는 WGS84 유지(근거: `COORDINATES_PRIMER` B-4).
- 지도 엔진 전환 검토(MapLibre + 벡터 타일). 카카오·네이버 의존을 지오코딩·베이스맵으로 한정.

---

## 5. 기능 부채 (이번 세션까지 확인된 것)

| # | 항목 | 우선 | 근거 |
|---|---|---|---|
| F1 | 원천 API 8종 활용신청·어댑터(소음, CNG, 도시가스·고압가스, 테마파크 3·무도장·무도학원, 건축물대장, 등록공장, LPG 판매소) | P1 | `API_SOURCE_RESEARCH_2026-09-14.md §4` |
| F2 | `HAZARD_SOURCE_MATRIX.md` #17 CNG 「공개 API 없음」 정정, `LH_CRITERIA_COVERAGE.md` 연결 상태 충돌 해소, 룰북·`service.py`의 CNG 「CSV만」 주석 | P1 | 동일 |
| F3 | README API 목록 정정(`/api/hazard-screening` 존재하지 않음, `/api/screening` 7개·`/api/settings/keys` 누락) | P1 | `README.md:92-105` |
| F4 | 심사표 표준본 등록공장 칩 부연 문구 단축(`facilities.xlsx · 표준 공장(PNU 보유)`) | P2 | `local_wiring.py` `LocalSourceFile.chip_detail` |
| F5 | 검색 직후 큰 필지가 화면 밖으로 잘림 → 필지 경계 기준 fit | P2 | `MapPanel.tsx` 뷰포트 effect |
| F6 | 지적도 타일 도착 시 전체 재드로우 → 증분 | P2 | §3-3 |
| F7 | 디자인 훅이 지적하는 좌측 색 테두리(`screening.css` 176·936행, `hazard-review.css` 360행) 유지 여부 결정 | P3 | 판정 결과 색 표시. 유지 시 훅 억제 규칙 등록 |
| F8 | 지도 위 HTML 오버레이가 테마 토큰을 쓰지 않도록 규칙화(lint 또는 리뷰 체크리스트) | P2 | 9/14 검은 핀 버그 |
| F9 | 네이버 검색 키 등록 후 15·17건 재실행, 118건 재실행 | P1 | `HANDOFF_260913.md §4` |
| F10 | 우수 LPG판매·PRTR·생활안전지도 IF_0049 등 "후보 탐색용" 보조 원천 레이어(판정 확정에는 미사용) | P3 | `API_SOURCE_RESEARCH §4-6` |

---

## 6. 실행 순서 제안

| 스프린트 | 내용 | 완료 기준 |
|---|---|---|
| S1 (1~2주) | P0 전부: 인증·설정 API 플래그·CORS, 잡 스토어(Redis)+워커, 루프 블로킹 제거, 저장소 위생 | 외부에 열어도 안전. 재시작에도 결과 보존 |
| S2 (1~2주) | CI/CD + Docker + pyproject/ruff/mypy/vitest/eslint, 관측성(예외 핸들러·요청 ID·JSON 로그·Sentry·readiness) | PR 게이트 동작. 장애 시 원인 추적 가능 |
| S3 (2주) | 외부 API 회복력(재시도·회로차단·레이트리밋·풀·Redis 캐시), 데이터 파이프라인 스케줄·지적도 CLI, F1 원천 8종 중 승인된 것 연결 | 원천 장애가 판정 오류로 번지지 않음 |
| S4 (2~3주) | 프론트 구조(MapPanel 분해, store, TanStack Query, 에러 바운더리, SSE), 번들 최적화, 컴포넌트 테스트 | 초기 JS < 150KB gzip, Lighthouse 90 |
| S5 (2~3주) | PostGIS 이전, 판정 엔진 분해·규칙팩 외부화, 골든·부하·E2E 테스트 | 워커 N개 수평 확장. 118건 골든 통과 |
| 이후 | P3 | — |

---

## 7. 측정 지표

| 지표 | 현재 | 목표 |
|---|---|---|
| 초기 JS(gzip) | 107KB(단일 청크, 심사표·설정 포함) | < 150KB 유지하며 분할, 첫 화면 < 80KB |
| 심사 1건 중 API p99 지연 | 측정 안 됨(루프 블로킹) | < 50ms |
| 동시 심사 처리 | 1(단일 워커 사실상) | 워커당 4, 수평 확장 |
| 백엔드 테스트 커버리지 | 측정 안 됨 | 80% |
| 프론트 테스트 | 0 | 핵심 훅·컴포넌트 60% |
| 외부 API 실패 시 사용자 영향 | 빈 결과로 위장 가능(VWorld) | 실패 명시, 회로차단 후 자동 복구 |
| clone 크기 | 97MB | < 20MB |
| 인허가 원장 신선도 | 수동, 최근 9/12 | 일 1회 자동, readiness에 노출 |
