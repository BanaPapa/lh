# 테스트 배포 — Vercel(프런트) + Google Cloud Run(백엔드)

한 달 테스트용 배포 절차다. 누구나 접속할 수 있고, 비용은 무료 사용량 안에서 0원 근처가
목표다(하루 심사 10~20건 기준).

```
브라우저 ──▶ Vercel (프런트 정적 파일)
              │  /api/location/*  (rewrite, 같은 주소라 CORS 불필요)
              ▼
         Cloud Run (FastAPI 백엔드, 서울 리전, 인스턴스 최대 1대)
              │
              ▼
         공공 API (공공데이터포털·카카오·네이버·VWorld·생활안전지도 …)
```

## 왜 이렇게 나눴나

- API 키는 서버에만 둔다. 누구나 접속하는 배포에서 브라우저에 키를 두면 가져가 쓸 수 있다.
- 공공 API 대부분이 브라우저 직접 호출을 막는다(CORS). 서버를 거쳐야 한다.
- 심사는 60~90초 걸리고 진행 상태를 서버 메모리에 둔다. 그래서 Cloud Run 인스턴스를
  **최대 1대**로 고정한다. 진행 상황을 0.25초마다 묻기 때문에 요청 기반 과금(요청 처리
  중에만 과금)으로도 심사가 끊기지 않는다.

## 1. 준비 (한 번만)

1. Google Cloud 프로젝트를 만들고 결제 계정을 연결한다(무료 사용량 안이면 청구 0원).
2. Google Cloud CLI(gcloud)를 설치하고 로그인한다.
   ```powershell
   gcloud auth login
   gcloud config set project <프로젝트ID>
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
   ```
3. `backend\.env` 에 API 키가 다 들어 있는지 확인한다. 배포 스크립트가 여기서 읽는다.

## 2. 인허가 원장 최신화 (배포할 때마다)

배포 서버는 켤 때 원장을 다시 받지 않는다(`FACILITY_SYNC_ON_STARTUP=false`). 서버가 수시로
새로 켜지고 디스크가 임시라서다. 대신 로컬 `backend/data/facilities.db` 가 이미지에 그대로
들어간다. 배포 전에 로컬에서 한 번 받아 둔다.

```powershell
cd backend
.venv\Scripts\python -m app.sync_facilities
```

## 3. 백엔드 배포 (Cloud Run)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\deploy-backend.ps1 -Project <프로젝트ID>
```

스크립트가 하는 일:

- `backend\.env` 의 API 키만 골라 Cloud Run 환경변수로 넘긴다(임시 파일은 배포 뒤 지움).
- 배포 전용 설정을 붙인다.

  | 설정 | 값 | 뜻 |
  |---|---|---|
  | `FACILITY_SYNC_ON_STARTUP` | false | 켤 때 원장 재다운로드 안 함 |
  | `RATE_LIMIT_PER_MINUTE` | 3 | 접속자(IP)당 1분에 심사 시작 3건 |
  | `RATE_LIMIT_PER_DAY` | 40 | 접속자당 하루 40건 |
  | `BATCH_MAX_ROWS` | 20 | 일괄 심사 한 번에 20건 |

- `backend/` 를 올려 Cloud Build 가 `Dockerfile` 로 이미지를 만든다. 업로드 목록은
  `backend/.gcloudignore` 를 따르므로 git 에 없는 `data/facilities.db`·`data/rule_overrides.json`
  도 들어간다.
- 서울 리전, 메모리 1GiB, 인스턴스 0~1대, 요청 제한시간 15분.

끝나면 `Service URL: https://lh-screening-api-xxxxx.a.run.app` 이 나온다. 확인:

```powershell
curl https://<Service URL>/api/health
```

## 4. 프런트 배포 (Vercel)

1. `frontend/vercel.json` 의 `CLOUD_RUN_URL` 을 3단계 주소(https:// 뒤 호스트)로 바꿔 커밋·푸시한다.
2. Vercel 에서 GitHub 저장소를 가져오고 **Root Directory 를 `frontend`** 로 둔다.
3. Environment Variables:

   | 이름 | 값 |
   |---|---|
   | `VITE_KAKAO_JAVASCRIPT_KEY` | 카카오 JavaScript 키 |
   | `VITE_NAVER_MAP_CLIENT_ID` | (네이버 지도를 쓸 때만) |
   | `VITE_API_BASE_URL` | **비워 둔다** — 비면 `/api/location` 으로 부르고 rewrite 가 Cloud Run 으로 넘긴다 |

4. 배포 후 나온 도메인(예: `lh-screening.vercel.app`)을 등록한다.
   - 카카오 디벨로퍼스 → 내 애플리케이션 → 플랫폼 → Web 사이트 도메인에 추가.
     빠지면 지도가 뜨지 않는다.
   - 네이버 지도를 쓰면 네이버 클라우드 콘솔의 Web 서비스 URL 에도 추가.

VWorld 키는 서버에서만 부르고 `VWORLD_DOMAIN`(지금 등록된 값)을 그대로 보내므로 따로
바꿀 것이 없다.

## 5. 테스트 중 알아 둘 것

- **첫 접속 지연**: 한동안 아무도 안 쓰면 서버가 꺼진다. 다음 첫 요청에서 켜지는 데 수 초,
  첫 심사는 전국 목록 예열 때문에 조금 더 걸린다.
- **메모리에만 남는 것**: 심사 진행 상태, 캐시, 정문 수기 지정(`front_doors.json`), 기준 편집값
  변경은 서버가 꺼지면 사라진다. 테스트에는 문제없지만 영구 보관이 필요하면 저장소를 붙여야 한다.
- **설정 API**(키·규칙 변경)는 서버 자신(루프백)에서만 열려 있어 배포 서버에서는 외부에서 쓸 수 없다.
- **횟수 제한**에 걸리면 화면에 「1분에 3건까지」·「하루 40건까지」 안내가 뜬다. 값은
  `deploy/deploy-backend.ps1` 의 `$deploySettings` 에서 바꿔 다시 배포한다.
- **비용 확인**: Google Cloud 콘솔 → 결제 → 예산 및 알림에서 예산(예: 월 5천원)과 알림을
  걸어 두면 예상 밖 과금을 바로 안다.

## 사본 갱신 — 매일 오전 9시 (GCP, 2026-10-01)

공공 API 로 받아 서버에 두는 사본(인허가 원장 `facilities.db`, 산단공 등록공장 목록, 생활안전지도
주유시설, LPG 충전소, 지오코딩 캐시 등)은 매일 오전 9시 GCP 에서 새로 만들어 배포한다(새벽에는
정부 API 가 점검으로 먹통인 일이 잦아 오전으로 잡았다).

- 구성: Cloud Scheduler `lh-nightly-snapshots`(매일 09:00 KST) → Cloud Build(`deploy/nightly-refresh.yaml`,
  asia-northeast3) → Cloud Run 새 리비전.
- 빌드가 하는 일: ① 서비스의 환경변수(API 키)와 버킷 `gs://<프로젝트>-snapshots/snapshots/` 의 지난
  사본을 가져온다 ② 코드 이미지(`:code`) 안에서 `python -m app.refresh_snapshots` 를 돌린다
  ③ 새 사본을 버킷에 올리고, 코드 이미지 위에 사본만 덮은 이미지(`:nightly-<빌드ID>`)를 배포한다.
  갱신이 전부 실패했거나 인허가 원장이 20% 넘게 줄면 배포하지 않는다(기존 리비전 유지).
- 업데이트 중 안내: 빌드가 시작할 때 버킷에 `status/maintenance.json` 을 두고 끝나면 지운다. 서버
  (`app/maintenance.py`, 환경변수 `SNAPSHOT_BUCKET`)가 이를 읽어 `GET /api/status/maintenance` 로 알리고,
  화면은 닫을 수 없는 안내 모달을 띄워 새 심사를 미룬다(끝나면 자동으로 닫힘). 서버도 새 심사 시작을
  503 으로 거절한다. 이미 돌던 심사는 가리지 않고, 빌드는 배포 전에 그 심사가 끝나길 최대 10분 기다린다.
  빌드가 죽어 표시가 남아도 45분 뒤에는 무시된다.
- 만들기/고치기: `powershell -ExecutionPolicy Bypass -File deploy\setup-nightly.ps1 -Project <프로젝트ID>`
  (`nightly-refresh.yaml` 을 고친 뒤 다시 돌리면 스케줄러에 반영된다).
- 지금 한 번 돌리기: `gcloud scheduler jobs run lh-nightly-snapshots --project <프로젝트ID> --location asia-northeast3`
- 수동 배포(`deploy-backend.ps1`)는 배포 전에 버킷의 더 새로운 사본을 `backend/data` 로 받고, 배포 뒤
  이미지에 `:code` 태그를 붙이고 로컬 사본을 버킷에 올린다. 그래서 수동 배포가 밤사이 갱신분을 되돌리지 않는다.
- git 에 있는 자료(CSV·LH 개별 맞춤)와 관리자 설정(`rule_overrides.json`)은 야간 작업이 건드리지 않는다.
  늘 마지막 수동 배포의 것을 쓴다.
- 비용: Cloud Build 무료 한도(월 2,500분) 안, 버킷·이미지 보관 월 수백 원 수준. 야간 이미지는 최근 5장과
  `:code` 만 남기고 7일 지난 것은 지운다.
