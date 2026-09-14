import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { applyTheme, readStoredTheme } from "./theme";
// Pretendard(SIL OFL 1.1). 자소 범위별 동적 서브셋이라 화면에 실제로 쓰인
// 글자만 내려받는다. 로컬 설치 여부와 무관하게 어느 PC에서나 같은 글꼴로 보인다.
import "pretendard/dist/web/variable/pretendardvariable-dynamic-subset.css";
import "./standalone.css";
import "./styles.css";
import "./hazard-review.css";
import "./screening.css";
import "./unified-layout.css";
import "./standalone-app.css";
import "./screening-progress.css";

// 첫 페인트 전에 테마를 확정한다. 기본값은 라이트다.
applyTheme(readStoredTheme());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
