/** 라이트/다크 테마 저장과 적용. 단독앱이라 호스트 셸이 아니라 앱이 직접 관리한다. */
import { useCallback, useState } from "react";

export type ThemeMode = "light" | "dark";

const STORAGE_KEY = "lh-screening-theme";
// 이전 이름으로 저장된 값은 처음 읽을 때 새 이름으로 옮긴다.
const LEGACY_STORAGE_KEY = "site-scope-theme";
const DEFAULT_THEME: ThemeMode = "light";

/** 저장된 테마를 읽는다. 값이 없거나 읽기가 막히면 라이트로 본다. */
export function readStoredTheme(): ThemeMode {
  try {
    let stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === null) {
      stored = window.localStorage.getItem(LEGACY_STORAGE_KEY);
      if (stored !== null) {
        window.localStorage.setItem(STORAGE_KEY, stored);
        window.localStorage.removeItem(LEGACY_STORAGE_KEY);
      }
    }
    return stored === "dark" ? "dark" : DEFAULT_THEME;
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
