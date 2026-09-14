import {
  AlertCircle,
  Check,
  ExternalLink,
  Eye,
  EyeOff,
  Globe,
  Info,
  KeyRound,
  Loader2,
  RotateCcw,
  Server,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import {
  getHealth,
  getSettingsKeys,
  updateSettingsKeys,
  type HealthResponse,
  type KeysStatusResponse,
  type KeysUpdatePayload,
  type ServerKeyStatus,
} from "../api";
import {
  getRuntimeKey,
  hasRuntimeKeyOverride,
  setRuntimeKey,
  type RuntimeKeyName,
} from "../runtime-keys";

interface ApiKeysPanelProps {
  open: boolean;
  onClose: () => void;
}

interface BrowserKeyMeta {
  name: RuntimeKeyName;
  label: string;
  description: string;
  hint?: string;
}

// 브라우저 키(공개 식별자)는 백엔드가 아니라 지도 SDK 용이라 여기서 메타를 둔다.
const BROWSER_KEYS: BrowserKeyMeta[] = [
  {
    name: "VITE_KAKAO_JAVASCRIPT_KEY",
    label: "카카오 JavaScript 키",
    description: "카카오 지도를 그리는 데 씁니다.",
    hint: "카카오 개발자 콘솔의 Web 플랫폼에 등록된 도메인과 정확히 일치해야 동작합니다. 지금 접속 주소는 http://localhost 입니다.",
  },
  {
    name: "VITE_NAVER_MAP_CLIENT_ID",
    label: "네이버 지도 Client ID",
    description: "네이버 지도를 그리는 데 씁니다.",
  },
  {
    name: "VITE_NAVER_MAP_STYLE_ID",
    label: "네이버 지도 Style ID",
    description: "네이버 커스텀 지도 스타일에 씁니다. 없어도 기본 지도는 나옵니다.",
  },
];

// 서버 헬스에서 연결 상태를 읽어 화면에 칩으로 보여준다.
const HEALTH_ITEMS: { key: keyof HealthResponse; label: string }[] = [
  { key: "kakao_configured", label: "카카오 주소검색" },
  { key: "tago_configured", label: "TAGO 대중교통" },
  { key: "public_data_configured", label: "공공데이터포털" },
  { key: "naver_search_configured", label: "네이버 검색" },
];

// demo_mode 를 뺀 문자열 키 필드만. 서버 키는 모두 여기에 속한다.
type ServerKeyField = Exclude<keyof KeysUpdatePayload, "demo_mode">;

/** 서버 키 이름(KAKAO_REST_API_KEY)을 요청 본문 필드(kakao_rest_api_key)로 바꾼다. */
function payloadField(spec: ServerKeyStatus): ServerKeyField {
  return spec.key.toLowerCase() as ServerKeyField;
}

export function ApiKeysPanel({ open, onClose }: ApiKeysPanelProps) {
  const [status, setStatus] = useState<KeysStatusResponse | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [savingBrowser, setSavingBrowser] = useState(false);
  const [savingServer, setSavingServer] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // 브라우저 키 입력값. 모달을 열 때 현재 런타임 값으로 채운다.
  const [browserInputs, setBrowserInputs] = useState<
    Record<RuntimeKeyName, string>
  >({
    VITE_KAKAO_JAVASCRIPT_KEY: "",
    VITE_NAVER_MAP_CLIENT_ID: "",
    VITE_NAVER_MAP_STYLE_ID: "",
  });

  // 서버 키 입력값(필드명 → 값). 비워 두면 변경하지 않는다.
  const [serverInputs, setServerInputs] = useState<Record<string, string>>({});
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const [demoMode, setDemoMode] = useState(false);

  const loadState = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [keys, healthResponse] = await Promise.all([
        getSettingsKeys(),
        getHealth(),
      ]);
      setStatus(keys);
      setHealth(healthResponse);
      setDemoMode(keys.demo_mode);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "설정 상태를 불러오지 못했습니다.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  // 열릴 때 상태를 새로 불러오고 입력값을 초기화한다.
  useEffect(() => {
    if (!open) return;
    setNotice("");
    setServerInputs({});
    setRevealed({});
    setBrowserInputs({
      VITE_KAKAO_JAVASCRIPT_KEY: getRuntimeKey("VITE_KAKAO_JAVASCRIPT_KEY"),
      VITE_NAVER_MAP_CLIENT_ID: getRuntimeKey("VITE_NAVER_MAP_CLIENT_ID"),
      VITE_NAVER_MAP_STYLE_ID: getRuntimeKey("VITE_NAVER_MAP_STYLE_ID"),
    });
    void loadState();
  }, [open, loadState]);

  // Esc 로 닫는다.
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  const handleSaveBrowserKeys = () => {
    setSavingBrowser(true);
    setError("");
    // localStorage 반영 → 구독 알림 → 지도가 새 키로 다시 로드된다.
    (Object.keys(browserInputs) as RuntimeKeyName[]).forEach((name) => {
      setRuntimeKey(name, browserInputs[name]);
    });
    // 저장 후 정규화된 현재 값으로 입력값을 다시 맞춘다.
    setBrowserInputs({
      VITE_KAKAO_JAVASCRIPT_KEY: getRuntimeKey("VITE_KAKAO_JAVASCRIPT_KEY"),
      VITE_NAVER_MAP_CLIENT_ID: getRuntimeKey("VITE_NAVER_MAP_CLIENT_ID"),
      VITE_NAVER_MAP_STYLE_ID: getRuntimeKey("VITE_NAVER_MAP_STYLE_ID"),
    });
    setNotice("브라우저 키를 저장하고 지도를 다시 불러왔습니다.");
    setSavingBrowser(false);
  };

  const sendServerUpdate = async (
    payload: KeysUpdatePayload,
    successMessage: string,
  ) => {
    setSavingServer(true);
    setError("");
    try {
      const updated = await updateSettingsKeys(payload);
      setStatus(updated);
      setDemoMode(updated.demo_mode);
      // 키가 먹었는지 바로 알 수 있게 헬스를 다시 읽는다.
      try {
        setHealth(await getHealth());
      } catch {
        // 헬스 갱신 실패는 저장 자체를 무효로 하지 않는다.
      }
      setServerInputs({});
      setNotice(successMessage);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "서버 키 저장에 실패했습니다.",
      );
    } finally {
      setSavingServer(false);
    }
  };

  const handleSaveServerKeys = () => {
    if (!status) return;
    const payload: KeysUpdatePayload = {};
    for (const spec of status.keys) {
      const value = (serverInputs[spec.key] ?? "").trim();
      if (value) payload[payloadField(spec)] = value;
    }
    payload.demo_mode = demoMode;
    void sendServerUpdate(payload, "서버 키를 저장했습니다.");
  };

  const handleDeleteServerKey = (spec: ServerKeyStatus) => {
    void sendServerUpdate(
      { [payloadField(spec)]: null } as KeysUpdatePayload,
      `${spec.label}을(를) 삭제했습니다.`,
    );
  };

  return createPortal(
    <div
      className="api-keys-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className="api-keys-modal"
        role="dialog"
        aria-modal="true"
        aria-label="API 키 설정"
      >
        <header className="api-keys-head">
          <div>
            <h2>
              <KeyRound size={18} aria-hidden="true" /> API 키 설정
            </h2>
            <p>지도 SDK 브라우저 키와 서버 데이터 키를 넣습니다.</p>
          </div>
          <button
            type="button"
            className="api-keys-close"
            onClick={onClose}
            aria-label="닫기"
          >
            <X size={18} />
          </button>
        </header>

        <div className="api-keys-body">
          {error && (
            <div className="api-keys-alert is-error" role="alert">
              <AlertCircle size={15} aria-hidden="true" />
              <span>{error}</span>
            </div>
          )}
          {notice && !error && (
            <div className="api-keys-alert is-ok" role="status">
              <Check size={15} aria-hidden="true" />
              <span>{notice}</span>
            </div>
          )}

          {/* ── A. 브라우저 키 ─────────────────────────── */}
          <section className="api-keys-group">
            <header className="api-keys-group-head">
              <span className="api-keys-badge is-browser">
                <Globe size={13} aria-hidden="true" /> 브라우저 키
              </span>
              <p>지도 SDK 용 공개 식별자입니다. 이 브라우저에만 저장됩니다.</p>
            </header>

            <div className="api-keys-fields">
              {BROWSER_KEYS.map((meta) => {
                const overridden = hasRuntimeKeyOverride(meta.name);
                return (
                  <div className="api-keys-field" key={meta.name}>
                    <label htmlFor={`bk-${meta.name}`}>
                      <span className="api-keys-field-label">
                        {meta.label}
                        {overridden && (
                          <span className="api-keys-tag">이 브라우저 저장됨</span>
                        )}
                      </span>
                      <small>{meta.description}</small>
                    </label>
                    <input
                      id={`bk-${meta.name}`}
                      type="text"
                      autoComplete="off"
                      spellCheck={false}
                      value={browserInputs[meta.name]}
                      placeholder="미설정"
                      onChange={(event) =>
                        setBrowserInputs((current) => ({
                          ...current,
                          [meta.name]: event.target.value,
                        }))
                      }
                    />
                    {meta.hint && (
                      <p className="api-keys-note">
                        <Info size={12} aria-hidden="true" />
                        <span>{meta.hint}</span>
                      </p>
                    )}
                  </div>
                );
              })}
            </div>

            <div className="api-keys-actions">
              <span className="api-keys-actions-hint">
                비우고 저장하면 빌드 기본값으로 되돌아갑니다.
              </span>
              <button
                type="button"
                className="api-keys-btn is-primary"
                onClick={handleSaveBrowserKeys}
                disabled={savingBrowser}
              >
                브라우저 키 저장
              </button>
            </div>
          </section>

          {/* ── B. 서버 키 ─────────────────────────────── */}
          <section className="api-keys-group">
            <header className="api-keys-group-head">
              <span className="api-keys-badge is-server">
                <Server size={13} aria-hidden="true" /> 서버 키
              </span>
              <p>서버에만 저장되며 브라우저로 다시 내려오지 않습니다.</p>
            </header>

            {loading && !status ? (
              <p className="api-keys-loading">
                <Loader2 size={15} className="api-keys-spin" aria-hidden="true" />
                설정 상태를 불러오는 중…
              </p>
            ) : (
              <div className="api-keys-fields">
                {status?.keys.map((spec) => {
                  const value = serverInputs[spec.key] ?? "";
                  const show = revealed[spec.key] ?? false;
                  return (
                    <div className="api-keys-field" key={spec.key}>
                      <label htmlFor={`sk-${spec.key}`}>
                        <span className="api-keys-field-label">
                          {spec.label}
                          <span
                            className={`api-keys-state ${
                              spec.configured ? "is-on" : "is-off"
                            }`}
                          >
                            {spec.configured ? "설정됨" : "미설정"}
                          </span>
                        </span>
                        <small>{spec.description}</small>
                      </label>
                      <div className="api-keys-input-row">
                        <input
                          id={`sk-${spec.key}`}
                          type={show ? "text" : "password"}
                          autoComplete="off"
                          spellCheck={false}
                          value={value}
                          placeholder={spec.hint || "미설정"}
                          onChange={(event) =>
                            setServerInputs((current) => ({
                              ...current,
                              [spec.key]: event.target.value,
                            }))
                          }
                        />
                        <button
                          type="button"
                          className="api-keys-icon-btn"
                          onClick={() =>
                            setRevealed((current) => ({
                              ...current,
                              [spec.key]: !show,
                            }))
                          }
                          aria-label={show ? "값 숨기기" : "값 보기"}
                          title={show ? "값 숨기기" : "값 보기"}
                        >
                          {show ? <EyeOff size={15} /> : <Eye size={15} />}
                        </button>
                        <button
                          type="button"
                          className="api-keys-icon-btn is-danger"
                          onClick={() => handleDeleteServerKey(spec)}
                          disabled={!spec.configured || savingServer}
                          aria-label={`${spec.label} 삭제`}
                          title="서버에서 이 키를 삭제"
                        >
                          <Trash2 size={15} />
                        </button>
                      </div>
                      {/* 발급처 링크. 서버가 URL 을 준 키만 렌더한다(없으면 숨김). */}
                      {spec.issuer_url ? (
                        <a
                          className="api-keys-issuer"
                          href={spec.issuer_url}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          <ExternalLink size={12} aria-hidden="true" />
                          {spec.issuer_name
                            ? `${spec.issuer_name}에서 발급받기`
                            : "발급처 열기"}
                        </a>
                      ) : (
                        <span className="api-keys-issuer is-empty" aria-hidden="true" />
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            <div className="api-keys-demo">
              <div>
                <strong>데모 모드</strong>
                <small>
                  켜면 예시 데이터로만 동작합니다. 끄면 위 서버 키로 실데이터를
                  호출합니다.
                </small>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={demoMode}
                className={`api-keys-switch ${demoMode ? "is-on" : ""}`}
                onClick={() => setDemoMode((current) => !current)}
              >
                <span className="api-keys-switch-thumb" />
              </button>
            </div>

            <div className="api-keys-actions">
              <span className="api-keys-actions-hint">
                빈 칸은 변경되지 않습니다. 지우려면 삭제 버튼을 쓰세요.
              </span>
              <button
                type="button"
                className="api-keys-btn is-primary"
                onClick={handleSaveServerKeys}
                disabled={savingServer || loading || !status}
              >
                {savingServer && (
                  <Loader2
                    size={14}
                    className="api-keys-spin"
                    aria-hidden="true"
                  />
                )}
                서버 키 저장
              </button>
            </div>
          </section>

          {/* ── 연결 상태 ──────────────────────────────── */}
          <section className="api-keys-group is-status">
            <header className="api-keys-group-head">
              <span className="api-keys-badge is-health">연결 상태</span>
              <button
                type="button"
                className="api-keys-refresh"
                onClick={() => void loadState()}
                disabled={loading}
              >
                <RotateCcw size={13} aria-hidden="true" /> 새로고침
              </button>
            </header>
            <div className="api-keys-chips">
              {HEALTH_ITEMS.map((item) => {
                const on = Boolean(health?.[item.key]);
                return (
                  <span
                    key={String(item.key)}
                    className={`api-keys-chip ${on ? "is-on" : "is-off"}`}
                  >
                    <i aria-hidden="true" />
                    {item.label}
                  </span>
                );
              })}
              <span
                className={`api-keys-chip ${
                  health?.demo_mode ? "is-warn" : "is-on"
                }`}
              >
                <i aria-hidden="true" />
                {health?.demo_mode ? "데모 모드 켜짐" : "실데이터 모드"}
              </span>
            </div>
          </section>
        </div>
      </section>
    </div>,
    document.body,
  );
}
