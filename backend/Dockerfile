# LH 서류심사 백엔드 — Google Cloud Run 배포 이미지.
#
# 로컬(Windows)에서 `gcloud run deploy --source backend` 로 올린다(docs/DEPLOY.md).
# data/facilities.db(인허가 원장 58MB)는 git 에는 없지만 .gcloudignore 가 업로드에
# 포함시켜 이미지에 구워진다. 배포 서버는 기동 시 원장을 다시 받지 않는다
# (FACILITY_SYNC_ON_STARTUP=false) — 원장을 새로 하려면 로컬에서 갱신 후 다시 배포한다.
# 로컬 테스트(737건)를 돌린 것과 같은 파이썬 버전.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY data ./data

# Cloud Run 이 PORT 를 준다. 프록시 헤더는 믿지 않는다(uvicorn 기본) — 설정 API 의
# 루프백 검사가 X-Forwarded-For 로 우회되지 않게 소켓 주소를 그대로 둔다.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
