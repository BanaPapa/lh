# 야간 사본 갱신(Cloud Scheduler → Cloud Build → Cloud Run)을 만든다/고친다. 여러 번 돌려도 된다.
#
#   powershell -ExecutionPolicy Bypass -File deploy\setup-nightly.ps1 -Project <프로젝트ID>
#
# 무엇을 하나: Cloud Scheduler API 켜기 · 사본 버킷 만들기 · 이미지 정리 정책 ·
# 매일 새벽(기본 03:30 KST) deploy\nightly-refresh.yaml 빌드를 넣는 스케줄러 작업.
# 빌드 내용은 nightly-refresh.yaml 을 고친 뒤 이 스크립트를 다시 돌리면 반영된다.
param(
  [Parameter(Mandatory = $true)][string]$Project,
  [string]$Region = "asia-northeast3",
  [string]$Schedule = "0 9 * * *",         # 매일 09:00 (Asia/Seoul) — 새벽엔 정부 API 점검이 잦다
  [string]$JobName = "lh-nightly-snapshots"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
$yaml = Join-Path $PSScriptRoot "nightly-refresh.yaml"
$number = (gcloud projects describe $Project --format "value(projectNumber)")
$account = "$number-compute@developer.gserviceaccount.com"
$bucket = "$Project-snapshots"

gcloud services enable cloudscheduler.googleapis.com cloudbuild.googleapis.com --project $Project | Out-Null
$existing = gcloud storage buckets list --project $Project --format "value(name)"
if ($existing -notcontains $bucket) {
  gcloud storage buckets create "gs://$bucket" --project $Project --location $Region `
    --uniform-bucket-level-access --public-access-prevention | Out-Null
}

# 야간 이미지는 하루 한 장씩 쌓인다. 최근 것만 남긴다(:code 와 최신 5장은 지우지 않는다).
$policy = Join-Path $env:TEMP "lh-ar-cleanup.json"
@'
[
  {"name": "keep-code", "action": {"type": "Keep"}, "condition": {"tagState": "TAGGED", "tagPrefixes": ["code"]}},
  {"name": "keep-recent", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 5}},
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"tagState": "ANY", "olderThan": "7d"}}
]
'@ | Out-File -Encoding ascii $policy
gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy `
  --project $Project --location $Region --policy $policy --no-dry-run | Out-Null
Remove-Item $policy -ErrorAction SilentlyContinue

# 빌드 설정(YAML) → Cloud Build API 본문(JSON). 스케줄러가 이 본문으로 builds.create 를 부른다.
$body = Join-Path $env:TEMP "lh-nightly-build.json"
& $python -c "import json,sys,yaml; json.dump(yaml.safe_load(open(sys.argv[1],encoding='utf-8')), open(sys.argv[2],'w',encoding='utf-8'))" $yaml $body
if ($LASTEXITCODE -ne 0) { throw "nightly-refresh.yaml 을 읽지 못했습니다." }

$uri = "https://cloudbuild.googleapis.com/v1/projects/$Project/locations/$Region/builds"
$jobs = gcloud scheduler jobs list --project $Project --location $Region --format "value(name)"
$verb = if ($jobs -match "/$JobName$") { "update" } else { "create" }
gcloud scheduler jobs $verb http $JobName `
  --project $Project --location $Region `
  --schedule $Schedule --time-zone "Asia/Seoul" `
  --uri $uri --http-method POST `
  --message-body-from-file $body `
  --oauth-service-account-email $account `
  --attempt-deadline 180s | Out-Null
Remove-Item $body -ErrorAction SilentlyContinue

Write-Host "야간 사본 갱신: $JobName · $Schedule (Asia/Seoul) · 버킷 gs://$bucket"
Write-Host "지금 한 번 돌리기: gcloud scheduler jobs run $JobName --project $Project --location $Region"
