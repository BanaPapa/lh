import { useEffect, useState } from "react";
import { getHealth } from "../api";
import { IS_LOCAL_APP } from "../deployment";

// 테마 버튼 왼쪽의 상태 줄(최대 두 줄).
// 1) 배포판 서버는 쓰지 않을 때 꺼 두었다가 첫 요청에 켠다(Cloud Run 최소 인스턴스 0).
//    접속하면 잠깐 알리고, 서버가 응답한 뒤(최소 MIN_VISIBLE_MS) 사라진다.
// 2) 카카오 API 한도 초과로 지도를 네이버로 바꿨으면 짧게 적는다(전문은 마우스 올림).
const MIN_VISIBLE_MS = 5000;
const MAX_VISIBLE_MS = 30000;

export function ServerWakeNotice({ kakaoNotice = "" }: { kakaoNotice?: string }) {
  const [waking, setWaking] = useState(!IS_LOCAL_APP);

  useEffect(() => {
    if (IS_LOCAL_APP) return;
    const started = Date.now();
    let hideTimer: number | undefined;
    const hideAfterMinimum = () => {
      const remaining = Math.max(0, MIN_VISIBLE_MS - (Date.now() - started));
      hideTimer = window.setTimeout(() => setWaking(false), remaining);
    };
    const maxTimer = window.setTimeout(() => setWaking(false), MAX_VISIBLE_MS);
    getHealth().then(hideAfterMinimum, hideAfterMinimum);
    return () => {
      window.clearTimeout(maxTimer);
      if (hideTimer !== undefined) window.clearTimeout(hideTimer);
    };
  }, []);

  if (!waking && !kakaoNotice) return null;
  return (
    <div className="server-status-notes" role="status">
      {waking && (
        <span title="한동안 쓰지 않았다면 서버를 켜느라 첫 연결에 몇 초 더 걸릴 수 있습니다.">
          서버 준비 중 · 첫 연결은 몇 초 걸릴 수 있어요
        </span>
      )}
      {kakaoNotice && (
        <span className="is-warning" title={kakaoNotice}>
          카카오 API 한도 초과 · 네이버 지도로 전환됨
        </span>
      )}
    </div>
  );
}
