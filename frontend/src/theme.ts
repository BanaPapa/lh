/** 라이트/다크 테마 저장과 적용. 단독앱이라 호스트 셸이 아니라 앱이 직접 관리한다. */
import { useCallback, useState } from "react";

export type ThemeMode = "light" | "dark";

const STORAGE_KEY = "site-scope-theme";
const DEFAULT_THEME: ThemeMode = "light";

/** 저장된 테마를 읽는다. 값이 없거나 읽기가 막히면 라이트로 본다. */
export function readStoredTheme(): ThemeMode {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "dark"
      ? "dark"
      : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/** html[data-theme] 을 바꾸고 저장한다. 저장이 막혀도 화면은 바뀌어야 한다. */
export function applyTheme(mode: ThemeMode): void {
  document.documentElement.dataset.theme = mode;
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // 시크릿 모드 등에서 저장이 막힐 수 있다. 이번 세션만 적용된다.
  }
}

export function useTheme(): {
  theme: ThemeMode;
  toggleTheme: () => void;
} {
  const [theme, setTheme] = useState<ThemeMode>(readStoredTheme);

  const toggleTheme = useCallback(() => {
    setTheme((current) => {
      const next: ThemeMode = current === "dark" ? "light" : "dark";
      applyTheme(next);
      return next;
    });
  }, []);

  return { theme, toggleTheme };
}
