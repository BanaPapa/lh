// 테마 버튼 왼쪽의 상태 줄(최대 두 줄, 사라지지 않고 그대로 둔다).
// 1) 배포판 서버는 쓰지 않을 때 꺼 두었다가 첫 요청에 켠다(Cloud Run 최소 인스턴스 0).
// 2) 카카오 API 한도 초과로 지도를 네이버로 바꿨으면 짧게 적는다(전문은 마우스 올림).
import { IS_LOCAL_APP } from "../deployment";

export function ServerWakeNotice({ kakaoNotice = "" }: { kakaoNotice?: string }) {
  if (IS_LOCAL_APP && !kakaoNotice) return null;
  return (
    <div className="server-status-notes" role="status">
      {!IS_LOCAL_APP && (
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
