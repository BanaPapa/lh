/**
 * 백엔드 API 베이스 URL과 공용 fetch 헬퍼.
 *
 * 이 모듈이 존재하는 이유: 이전에는 `src/api.ts` 와 `src/hazard-review/api.ts`
 * 두 곳에 같은 베이스 URL 결정 로직이 복붙돼 있었고, 두 곳 모두 `??` 폴백을 써서
 * 환경변수가 빈 문자열("")일 때 폴백이 동작하지 않는 동일한 버그를 안고 있었다.
 * (`??` 는 null/undefined 만 폴백하므로 "" 이 그대로 채택된다.)
 * 그 결과 요청이 백엔드가 아니라 프런트 자신으로 가서 index.html(HTML)을 받고
 * JSON 파서가 `Unexpected token '<'` 로 터졌다. 한 곳에서 관리한다.
 */

const RAW_BASE_URL_CANDIDATES: Array<string | undefined> = [
  import.meta.env.VITE_LOCATION_ANALYSIS_API_BASE_URL,
  import.meta.env.VITE_API_BASE_URL,
];

/**
 * 후보 환경변수를 앞에서부터 훑어 **trim 후 비어 있지 않은** 첫 값을 채택한다.
 * 빈 문자열/공백만 있는 값은 "미설정"으로 취급하고 건너뛴다.
 */
function resolveApiBaseUrl(): string {
  for (const candidate of RAW_BASE_URL_CANDIDATES) {
    if (typeof candidate === "string") {
      const trimmed = candidate.trim();
      if (trimmed.length > 0) {
        return trimmed.replace(/\/+$/, "");
      }
    }
  }
  return import.meta.env.DEV ? "http://localhost:8000" : "/api/location";
}

export const API_BASE_URL = resolveApiBaseUrl();

export interface ApiRequestOptions extends RequestInit {
  /** 네트워크 연결 실패 시 사용자에게 보여줄 한국어 메시지. */
  connectionErrorMessage?: string;
}

const DEFAULT_CONNECTION_ERROR =
  "분석 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.";

const UNPARSEABLE = Symbol("unparseable");

function tryParseJson(raw: string): unknown | typeof UNPARSEABLE {
  if (raw.trim().length === 0) return null;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return UNPARSEABLE;
  }
}

/**
 * 공용 fetch 래퍼.
 * - 비JSON 응답(예: 잘못된 베이스 URL 로 index.html 을 받은 경우)을
 *   파서 예외 원문 대신 이해 가능한 한국어 메시지로 바꾼다.
 * - 어떤 주소로 요청했는지 메시지에 실어 진단을 돕는다.
 */
export async function apiRequest<T>(
  path: string,
  options: ApiRequestOptions = {},
): Promise<T> {
  const { connectionErrorMessage, headers, ...init } = options;
  const url = `${API_BASE_URL}${path}`;

  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...headers,
      },
    });
  } catch {
    throw new Error(connectionErrorMessage ?? DEFAULT_CONNECTION_ERROR);
  }

  const rawBody = await response.text();
  const parsed = tryParseJson(rawBody);

  if (!response.ok) {
    let message = `요청에 실패했습니다. (${response.status})`;
    if (parsed !== UNPARSEABLE && parsed && typeof parsed === "object") {
      const detail = (parsed as { detail?: unknown }).detail;
      if (typeof detail === "string" && detail.length > 0) message = detail;
    }
    throw new Error(message);
  }

  if (parsed === UNPARSEABLE) {
    throw new Error(
      `서버가 JSON 이 아닌 응답을 보냈습니다. 백엔드 주소 설정(VITE_API_BASE_URL)을 확인해 주세요. ` +
        `(요청 주소: ${url || "(빈 주소)"})`,
    );
  }

  return parsed as T;
}
