# LH 서류심사

LH 신축매입약정 서류심사를 공공데이터로 자동화하는 앱입니다. 사업지를 검색하면
연속지적도에서 대지경계를 받아 **1차 매입제외 판정**(주거환경 저해시설 거리 기준)과
**2차 생활편의성 배점**을 심사표 한 장으로 냅니다.

- `frontend`: React + TypeScript + Vite
- `backend`: FastAPI
- `docs`: 판정 정본(`docs/hazards/`)과 원천 대조·조사 문서

아래 명령은 모두 저장소 루트를 기준으로 합니다.

```powershell
git clone https://github.com/BanaPapa/lh.git
cd lh
```

## 빠른 시작

### 1. 백엔드

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --port 8000
```

### 2. 프런트엔드

```powershell
cd ..\frontend
npm install
Copy-Item .env.example .env.local
npm run dev            # http://localhost (80번 포트)
```

기본 설정은 `DEMO_MODE=true`입니다. API 키 없이도 화면과 데이터 흐름을 확인할 수
있지만, 모든 결과에 데모 데이터임을 표시합니다.

실데이터 사용 시 `backend/.env`:

| 키 | 용도 |
|---|---|
| `KAKAO_REST_API_KEY` | 주소 검색·좌표 변환, 정문 후보 |
| `NAVER_SEARCH_CLIENT_ID` / `NAVER_SEARCH_CLIENT_SECRET` | 대학·종합병원 정문, 역 출구 후보 |
| `TAGO_SERVICE_KEY` | 2차 배점의 버스정류장 |
| `PUBLIC_DATA_SERVICE_KEY` | 공공데이터포털(행안부 인허가·가스안전공사·복지부·건축HUB). 비우면 TAGO 키를 재사용 |
| `VWORLD_API_KEY` | 연속지적도 필지 경계·용도지역 |
| `OPINET_API_KEY` · `SAFEMAP_API_KEY` | 주유소·충전소 보조 원천 |
| `DEMO_MODE=false` | 실데이터 호출 |

프런트 `.env.local`: `VITE_KAKAO_JAVASCRIPT_KEY`(필수), 선택 `VITE_NAVER_MAP_CLIENT_ID`·
`VITE_NAVER_MAP_STYLE_ID`. 카카오 JavaScript 키는 개발자 콘솔 Web 플랫폼에 등록된
도메인(`http://localhost`)과 정확히 일치해야 합니다.

### 화면에서 키 넣기 (로컬 전용)

상단 우측 설정(톱니) → **API 키 설정**에서도 키를 넣을 수 있습니다.

- **브라우저 키**(카카오 JavaScript 키 · 네이버 지도 Client ID · Style ID)는 이
  브라우저의 `localStorage`에만 저장되고 지도가 즉시 새 키로 다시 로드됩니다.
- **서버 키**는 백엔드 `.env`와 실행 중인 환경변수에 저장되며 재시작 없이 반영됩니다.
  저장한 값은 브라우저로 다시 내려오지 않고 `****abcd` 마스킹 힌트만 표시됩니다.

> **경고 — 로컬 전용 기능입니다.** 설정 API(`/api/settings/keys`)는 소켓 상대가
> 루프백(127.0.0.1 / ::1)일 때만 동작하고 그 외에는 403을 돌려줍니다. 인증·권한
> 제어가 없으므로 외부에 배포할 때는 이 라우터를 제거하거나 잠가야 합니다.

## 심사 흐름

1. 사업지 검색 → 카카오 주소검색으로 좌표를 잡고, 국토교통부 연속지적도에서
   PNU 기준 대지경계를 받습니다(다필지는 합집합). 지도에서 필지를 눌러 더하거나 뺍니다.
2. 주택유형·신청유형을 고르면 규칙팩(`docs/hazards/`)이 항목별 임계거리(25·50·500m)를
   정합니다.
3. **심사 실행** → 1차 매입제외 판정(대지경계 ↔ 시설 거리)과 2차 생활편의성 배점을
   한 작업으로 돌리고, 심사표에서 항목별 근거·원천(API/로컬)·데이터 칩을 봅니다.
4. 지도에는 1차 시설(빨강)·2차 근거 시설(파랑)·기준 밖 시설(회색)과 최단거리선,
   사업지 3km 지적편집도, 용도지역 레이어가 표시됩니다.

판정 원칙: 조회하지 못한 원천은 「충돌 없음」으로 위장하지 않고 「판정 미적용·검토
필요」로 남깁니다. 연속지적도는 참고도형이라 최종 배제 판단 전에는 지적공부 확인과
경계복원측량이 필요합니다.

## 주요 API

- `GET /api/health`
- `GET /api/geocode?query=...`
- `GET /api/hazard-review/rule-packs` · `GET /api/hazard-review/rule-packs/{rule_pack_id}`
- `GET /api/hazard-review/application-types`
- `POST /api/hazard-review/parcels/resolve` · `GET /api/hazard-review/parcels/in-bounds`
- `POST /api/hazard-review/jobs` · `GET /api/hazard-review/jobs/{job_id}` · `POST /api/hazard-review/jobs/{job_id}/cancel`
- `GET /api/hazard-review/reviews/{review_id}` · `GET /api/hazard-review/reviews/{review_id}/evidence.json`
- `GET /api/screening/scoresheets`
- `GET /api/screening/front-doors` · `POST /api/screening/front-doors` · `DELETE /api/screening/front-doors/{facility}`
- `POST /api/screening` · `POST /api/screening/jobs` · `GET /api/screening/jobs/{job_id}` · `POST /api/screening/jobs/{job_id}/cancel`
- `GET /api/settings/keys` · `PUT /api/settings/keys` (로컬 전용)

Swagger 문서: `http://localhost:8000/docs`

## 데이터 원천

- **인허가 원장**: 행정안전부 LOCALDATA(석유판매업·고압가스·도시가스·유흥·단란·숙박·
  테마파크·무도장 등)를 `backend/app/sync_facilities.py`로 `backend/data/facilities.db`에
  동기화합니다(저장소에는 포함하지 않습니다).
- **공개 API**: 가스안전공사 LPG·CNG 충전소, 복지부 화장시설, 건축HUB 표제부, 오피넷,
  생활안전지도, VWorld 지적도·용도지역.
- 원천별 연결 상태와 남은 항목은 `docs/HAZARD_SOURCE_MATRIX.md`,
  `docs/LH_CRITERIA_COVERAGE.md`, `docs/API_SOURCE_RESEARCH_2026-09-14.md`에 있습니다.

## 검증

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q

cd ..\frontend
npm run lint          # tsc
npm run build
```

LH 실제 심사 결과와의 대조 도구는 `backend/tools/lh_baseline/`(README 참조)에 있습니다.

## 문서

- `docs/hazards/` — 유해요소 판정 정본(INDEX·TIMELINE·MEASUREMENT + 항목 문서)
- `docs/SCREENING_ENGINE_GUIDE.md` — 심사 엔진 설명
- `docs/COORDINATES_PRIMER_2026-09-13.md` — 좌표계·PNU·지적도 교재
- `docs/HANDOFF_260914.md` — 최신 작업 인계
- `docs/PRODUCTION_ROADMAP_2026-09-14.md` — 상용화 수정 목록
