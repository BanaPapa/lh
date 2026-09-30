"""한국 표준시. 배포 서버(Cloud Run)는 UTC 라 `astimezone()`(서버 지역시) 을 쓰면
결과지·파일명 시각이 UTC 로 찍혔다(2026-09-30 13:57 = KST 22:57). 화면·파일에 적는
시각은 늘 이 상수로 바꾼다.
"""

from datetime import timedelta, timezone

KST = timezone(timedelta(hours=9), "KST")
