import { useSyncExternalStore } from "react";

/**
 * 브라우저에서 쓰는 지도 SDK 공개 식별자(브라우저 키)를 런타임에 관리한다.
 *
 * 이 키들은 비밀이 아니라 공개 식별자다. localStorage 에 저장한 값이 있으면
 * 그 값을 우선 쓰고, 없으면 빌드 시 주입된 `import.meta.env` 값으로 폴백한다.
 *
 * 저장·조회는 모두 try/catch 로 감싼다. 사생활 보호 모드나 저장이 막힌 환경에서
 * localStorage 접근이 예외를 던져도 앱이 죽지 않고 조용히 env 폴백을 쓴다.
 */
export type RuntimeKeyName =
  | "VITE_KAKAO_JAVASCRIPT_KEY"
  | "VITE_NAVER_MAP_CLIENT_ID"
  | "VITE_NAVER_MAP_STYLE_ID";

export const RUNTIME_KEY_NAMES: RuntimeKeyName[] = [
  "VITE_KAKAO_JAVASCRIPT_KEY",
  "VITE_NAVER_MAP_CLIENT_ID",
  "VITE_NAVER_MAP_STYLE_ID",
];

const STORAGE_PREFIX = "site-scope-runtime-key:";

// 빌드 시 주입된 폴백. localStorage 값이 없을 때만 쓴다.
const ENV_FALLBACK: Record<RuntimeKeyName, string> = {
  VITE_KAKAO_JAVASCRIPT_KEY: (
    import.meta.env.VITE_KAKAO_JAVASCRIPT_KEY ?? ""
  ).trim(),
  VITE_NAVER_MAP_CLIENT_ID: (
    import.meta.env.VITE_NAVER_MAP_CLIENT_ID ?? ""
  ).trim(),
  VITE_NAVER_MAP_STYLE_ID: (
    import.meta.env.VITE_NAVER_MAP_STYLE_ID ?? ""
  ).trim(),
};

function storageKey(name: RuntimeKeyName): string {
  return `${STORAGE_PREFIX}${name}`;
}

function safeGet(name: RuntimeKeyName): string | null {
  try {
    return window.localStorage.getItem(storageKey(name));
  } catch {
    return null;
  }
}

function safeSet(name: RuntimeKeyName, value: string): void {
  try {
    window.localStorage.setItem(storageKey(name), value);
  } catch {
    // 저장이 막혀 있어도 앱은 계속 동작해야 한다. env 폴백으로 남는다.
  }
}

function safeRemove(name: RuntimeKeyName): void {
  try {
    window.localStorage.removeItem(storageKey(name));
  } catch {
    // 삭제 실패는 무시한다.
  }
}

const listeners = new Set<() => void>();

function notify(): void {
  listeners.forEach((listener) => {
    try {
      listener();
    } catch {
      // 한 구독자가 던져도 나머지 구독자에게는 알려야 한다.
    }
  });
}

/** 값이 바뀌면 호출된다. 반환한 함수로 구독을 해제한다. */
export function subscribeRuntimeKeys(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// 다른 탭에서 값이 바뀌어도 이 탭이 반응하게 한다.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (!event.key || event.key.startsWith(STORAGE_PREFIX)) notify();
  });
}

/**
 * 브라우저 키 현재 값을 돌려준다.
 * localStorage 에 비어 있지 않은 값이 있으면 그것을, 없으면 env 폴백을 쓴다.
 * 어느 쪽에도 값이 없으면 빈 문자열.
 */
export function getRuntimeKey(name: RuntimeKeyName): string {
  const stored = safeGet(name);
  if (stored != null && stored.trim() !== "") return stored.trim();
  return ENV_FALLBACK[name];
}

/** env 폴백만 돌려준다. 화면에서 "빌드 기본값" 안내에 쓴다. */
export function getRuntimeKeyEnvFallback(name: RuntimeKeyName): string {
  return ENV_FALLBACK[name];
}

/** localStorage 에 이 브라우저만의 값이 저장돼 있는지. */
export function hasRuntimeKeyOverride(name: RuntimeKeyName): boolean {
  const stored = safeGet(name);
  return stored != null && stored.trim() !== "";
}

/**
 * 브라우저 키를 설정한다. 빈 값이면 저장값을 지워 env 폴백으로 되돌린다.
 * 변경 후 구독자에게 알려 지도가 새 키로 다시 로드되게 한다.
 */
export function setRuntimeKey(name: RuntimeKeyName, value: string): void {
  const trimmed = value.trim();
  if (trimmed) safeSet(name, trimmed);
  else safeRemove(name);
  notify();
}

/** React 컴포넌트에서 현재 브라우저 키를 구독한다. */
export function useRuntimeKey(name: RuntimeKeyName): string {
  return useSyncExternalStore(
    subscribeRuntimeKeys,
    () => getRuntimeKey(name),
    () => ENV_FALLBACK[name],
  );
}
