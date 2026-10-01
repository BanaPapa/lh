# LH 서류심사 백엔드를 Google Cloud Run 에 배포한다(docs/DEPLOY.md 3단계).
#
#   powershell -ExecutionPolicy Bypass -File deploy\deploy-backend.ps1 -Project <프로젝트ID>
#
# backend/.env 의 API 키를 읽어 Cloud Run 환경변수로 넘긴다. 키 파일은 저장소에 남기지
# 않도록 임시 폴더에 만들었다가 지운다. 배포용 설정(기동 동기화 끄기·심사 횟수 제한 등)은
# 아래 $deploySettings 에서 바꾼다.
param(
  [Parameter(Mandatory = $true)][string]$Project,
  [string]$Region = "asia-northeast3",   # 서울
  [string]$Service = "lh-screening-api"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root "backend\.env"
if (-not (Test-Path $envFile)) { throw "backend\.env 가 없습니다. API 키가 든 .env 가 필요합니다." }

# .env 에서 옮길 키(값이 있는 것만). 파일 경로류(로컬 전용)는 옮기지 않는다.
$keyNames = @(
  "KAKAO_REST_API_KEY", "TAGO_SERVICE_KEY", "PUBLIC_DATA_SERVICE_KEY",
  "NAVER_SEARCH_CLIENT_ID", "NAVER_SEARCH_CLIENT_SECRET",
  "NAVER_MAP_CLIENT_ID", "NAVER_MAP_CLIENT_SECRET",
  "VWORLD_API_KEY", "VWORLD_DOMAIN", "OPINET_API_KEY", "SAFEMAP_API_KEY",
  "SEOUL_OPEN_DATA_KEY", "GG_OPEN_API_KEY", "HAZARD_CONTEXT_RADIUS_M"
)
$values = @{}
foreach ($line in Get-Content $envFile -Encoding UTF8) {
  if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') {
    $name = $Matches[1]; $value = $Matches[2].Trim().Trim('"').Trim("'")
    if ($keyNames -contains $name -and $value) { $values[$name] = $value }
  }
}

# 배포 전용 설정. 누구나 접속하는 테스트 서버라 공공 API 쿼터를 지키는 값을 둔다.
$deploySettings = [ordered]@{
  DEMO_MODE                 = "false"
  FACILITY_SYNC_ON_STARTUP  = "false"  # 원장은 이미지에 구워 넣는다
  RATE_LIMIT_PER_MINUTE     = "3"      # 접속자당 1분에 심사 시작 3건
  RATE_LIMIT_PER_DAY        = "40"     # 접속자당 하루 40건
  BATCH_MAX_ROWS            = "20"     # 일괄 심사 한 번에 20건 (frontend TEST_BATCH_LIMIT 와 같게)
  SNAPSHOT_BUCKET           = "$Project-snapshots"  # 사본 갱신 빌드의 「업데이트 중」 표시를 읽는 버킷
}
foreach ($k in $deploySettings.Keys) { $values[$k] = $deploySettings[$k] }

# 야간 사본 갱신(deploy\setup-nightly.ps1)과 같은 버킷을 쓴다. 버킷의 사본이 로컬보다
# 새것이면 먼저 받아, 수동 배포가 밤사이 갱신된 자료를 옛것으로 되돌리지 않게 한다.
$bucket = "$Project-snapshots"
$dataDir = Join-Path $root "backend\data"
$hasBucket = (gcloud storage buckets list --project $Project --format "value(name)") -contains $bucket
if ($hasBucket) {
  gcloud storage rsync "gs://$bucket/snapshots" $dataDir --skip-if-dest-has-newer-mtime --project $Project
}

$yaml = Join-Path $env:TEMP "lh-cloudrun-env.yaml"
$lines = foreach ($k in $values.Keys) { "{0}: '{1}'" -f $k, ($values[$k] -replace "'", "''") }
[System.IO.File]::WriteAllLines($yaml, $lines, (New-Object System.Text.UTF8Encoding $false))

try {
  gcloud run deploy $Service `
    --project $Project `
    --region $Region `
    --source (Join-Path $root "backend") `
    --env-vars-file $yaml `
    --allow-unauthenticated `
    --memory 1Gi --cpu 1 `
    --min-instances 0 --max-instances 1 `
    --timeout 900 `
    --cpu-boost `
    --no-cpu-throttling  # 심사는 요청이 끝난 뒤 백그라운드 작업으로 돈다. 기본값(요청 중에만 CPU)이면 진행 조회 때만 CPU 를 받아 10분 넘게 걸렸다
} finally {
  Remove-Item $yaml -ErrorAction SilentlyContinue
}
if ($LASTEXITCODE -ne 0) { throw "배포에 실패했습니다." }

# 방금 배포한 이미지를 :code 로 표시한다 — 야간 사본 갱신이 이 이미지 위에 사본만 덮는다.
$image = gcloud run services describe $Service --project $Project --region $Region `
  --format "value(spec.template.spec.containers[0].image)"
$repo = ($image -split "@")[0]
gcloud artifacts docker tags add $image "${repo}:code" --project $Project --quiet

# 로컬 사본(인허가 원장·등록공장 목록 등)을 버킷에 올려 야간 갱신의 출발점으로 삼는다.
if ($hasBucket) {
  $python = Join-Path $root "backend\.venv\Scripts\python.exe"
  Push-Location (Join-Path $root "backend")
  $files = & $python -m app.refresh_snapshots --list-files
  Pop-Location
  foreach ($f in $files) {
    $path = Join-Path $dataDir $f
    if (Test-Path $path) { gcloud storage cp $path "gs://$bucket/snapshots/$f" --project $Project --quiet }
  }
}

