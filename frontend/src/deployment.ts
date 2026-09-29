/**
 * 이 화면이 이 PC(localhost)에서 떠 있는지. 배포 사이트(Vercel)는 누구나 들어오는 테스트
 * 서버라 키 입력·기준 저장 같은 관리 기능을 막고, 테스트 안내를 띄운다.
 */
export const IS_LOCAL_APP =
  import.meta.env.DEV ||
  ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);

/** 배포 테스트 서버의 일괄 심사 한 번 상한. 백엔드 BATCH_MAX_ROWS 와 같아야 한다. */
export const TEST_BATCH_LIMIT = 20;
