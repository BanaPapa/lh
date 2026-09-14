import {
  AlertCircle,
  GripVertical,
  Bus,
  BusFront,
  CheckCheck,
  ChevronDown,
  EyeOff,
  GraduationCap,
  Hospital,
  Landmark,
  Layers,
  LibraryBig,
  LoaderCircle,
  MapPinned,
  Minus,
  PencilRuler,
  Plus,
  RotateCcw,
  ShoppingCart,
  SlidersHorizontal,
  Store,
  TrainFront,
  Trees,
  Utensils,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { getParcelsInBounds } from "../hazard-review/api";
import {
  CADASTRAL_MAX_LEVEL,
  CADASTRAL_MAX_RADIUS_M,
  MAX_CADASTRAL_POLYGONS,
  MAX_TILE_DEPTH,
  boxCenter,
  boxesOverlap,
  metersBetween,
  parcelContaining,
  pointInRing,
  subdivideTile,
  tileCenter,
  tilesForViewport,
  tileWithinRadius,
  viewportOutsideRadius,
  type CadastralTile,
  type LatLngBox,
} from "../hazard-review/cadastralTiles";
import {
  ringCentroid,
  symmetricBoxAroundSite,
  viewportPadding,
  visibleCenterPoint,
  type PixelPoint,
} from "../hazard-review/mapViewport";
import { useRuntimeKey } from "../runtime-keys";
import type {
  CadastralParcel,
  HazardFinding,
  HazardParcel,
  HazardReviewResult,
} from "../hazard-review/types";
import type { GeocodeCandidate, MapProvider } from "../types";
import type {
  FrontDoorCandidate,
  ScreeningResult,
} from "../screening/types";
import {
  flattenScreeningHits,
  measurementShortLabel,
  visibleScreeningHits,
  type ScreeningHitRef,
} from "../screening/screeningOverlays";

declare global {
  interface Window {
    kakao?: any;
    naver?: any;
    navermap_authFailure?: () => void;
  }
}

type NaverMapStyle = "normal" | "ppt";

interface MapRuntime {
  provider: MapProvider;
  sdk: any;
}

interface MapViewport {
  lat: number;
  lng: number;
  level: number;
}

// 검색 직후 첫 화면에 즉시 요청할 사업지 주변 반경(m). 인접 필지를 고르기에
// 충분하다. 이후는 지도 idle 마다 뷰포트와 겹치는 타일을 반경 3km 까지 채운다.
const CADASTRAL_RADIUS_M = 350;
// 시설 영역이 아닌 자리를 눌렀을 때 그 주변으로 새로 받는 반경(m). 검색 직후
// 사업지 주변과 같은 폭이다.
const CADASTRAL_CLICK_RADIUS_M = 350;

const METERS_PER_DEGREE_LAT = 111_320;

// 지적도 타일 동시 요청 상한. 나머지는 큐에 두고 하나 끝날 때마다 채운다.
const CADASTRAL_MAX_CONCURRENT = 4;

// 연속 실패(키 없음·API 오류) 이 횟수에 이르면 그 세대 동안 조회를 멈춘다.
// 성공 한 번이면 카운터를 되돌려 다음 idle 에 다시 시도한다.
const CADASTRAL_MAX_FAILURES = 3;

const ZOOM_MIN_LEVEL = 1;
const ZOOM_MAX_LEVEL = 14;
const ZOOM_LEVEL_STEP = 0.5;

// 검색 직후, 아직 분석 결과가 없을 때의 축척. 반경 원에 맞춰 넓게 잡으면
// 어느 동네인지만 보이고 대지경계가 점이 된다. 필지가 보이는 정도로 든다.
const SITE_PREVIEW_LEVEL = 3;


let kakaoLoader: Promise<any> | null = null;
let kakaoLoadedKey: string | null = null;
let naverLoader: Promise<any> | null = null;
let naverLoadedKey: string | null = null;

/**
 * 이미 삽입된 카카오 SDK 를 완전히 걷어낸다.
 * 스크립트 태그와 전역(window.kakao)을 함께 지워야 새 키로 다시 로드했을 때
 * 옛 SDK 가 남아 동작이 꼬이지 않는다.
 */
function resetKakaoSdk(): void {
  kakaoLoader = null;
  kakaoLoadedKey = null;
  document.getElementById("kakao-map-sdk")?.remove();
  try {
    delete window.kakao;
  } catch {
    window.kakao = undefined;
  }
}

/** 이미 삽입된 네이버 SDK 를 완전히 걷어낸다. */
function resetNaverSdk(): void {
  naverLoader = null;
  naverLoadedKey = null;
  document.getElementById("naver-map-sdk")?.remove();
  try {
    delete window.naver;
  } catch {
    window.naver = undefined;
  }
}

function loadKakaoMap(key: string): Promise<any> {
  // 키가 바뀌었으면 옛 SDK 를 걷어내고 새 키로 다시 로드한다.
  if (kakaoLoadedKey !== null && kakaoLoadedKey !== key) resetKakaoSdk();
  if (window.kakao?.maps) {
    kakaoLoadedKey = key;
    return new Promise((resolve) =>
      window.kakao!.maps.load(() => resolve(window.kakao)),
    );
  }
  if (kakaoLoader) return kakaoLoader;

  kakaoLoader = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.id = "kakao-map-sdk";
    script.src = `https://dapi.kakao.com/v2/maps/sdk.js?appkey=${encodeURIComponent(
      key,
    )}&libraries=clusterer&autoload=false`;
    script.onload = () => {
      if (!window.kakao?.maps) {
        kakaoLoader = null;
        reject(new Error("카카오 지도 SDK를 초기화할 수 없습니다."));
        return;
      }
      window.kakao.maps.load(() => {
        kakaoLoadedKey = key;
        resolve(window.kakao);
      });
    };
    script.onerror = () => {
      // 실패한 로더를 남기면 재시도가 막힌다. 스크립트째 걷어낸다.
      resetKakaoSdk();
      reject(
        new Error(
          "카카오 지도 SDK를 불러오지 못했습니다. 현재 접속 주소가 카카오 Web 플랫폼 도메인과 일치하는지 확인해 주세요.",
        ),
      );
    };
    document.head.appendChild(script);
  });

  return kakaoLoader;
}

function loadNaverMap(clientId: string, includeGl = false): Promise<any> {
  // 키가 바뀌었으면 옛 SDK 를 걷어내고 새 키로 다시 로드한다.
  if (naverLoadedKey !== null && naverLoadedKey !== clientId) resetNaverSdk();
  if (window.naver?.maps) {
    naverLoadedKey = clientId;
    return Promise.resolve(window.naver);
  }
  if (naverLoader) return naverLoader;

  naverLoader = new Promise((resolve, reject) => {
    const existing = document.getElementById(
      "naver-map-sdk",
    ) as HTMLScriptElement | null;
    const finish = () => {
      if (!window.naver?.maps) {
        naverLoader = null;
        reject(new Error("네이버 지도 SDK를 초기화할 수 없습니다."));
        return;
      }
      naverLoadedKey = clientId;
      resolve(window.naver);
    };

    if (existing) {
      existing.addEventListener("load", finish, { once: true });
      existing.addEventListener(
        "error",
        () => {
          resetNaverSdk();
          reject(new Error("네이버 지도 SDK를 불러오지 못했습니다."));
        },
        { once: true },
      );
      return;
    }

    const script = document.createElement("script");
    script.id = "naver-map-sdk";
    script.src = `https://oapi.map.naver.com/openapi/v3/maps.js?ncpKeyId=${encodeURIComponent(
      clientId,
    )}${includeGl ? "&submodules=gl" : ""}`;
    script.onload = finish;
    script.onerror = () => {
      resetNaverSdk();
      reject(
        new Error(
          "네이버 지도 SDK를 불러오지 못했습니다. Dynamic Map 사용 설정과 Web 서비스 URL을 확인해 주세요.",
        ),
      );
    };
    document.head.appendChild(script);
  });

  return naverLoader;
}

function toMapPosition(runtime: MapRuntime, lat: number, lng: number): any {
  if (runtime.provider === "kakao") {
    return new runtime.sdk.maps.LatLng(lat, lng);
  }
  return new runtime.sdk.maps.LatLng(lat, lng);
}

function readPosition(position: any): { lat: number; lng: number } {
  const lat =
    typeof position?.getLat === "function"
      ? position.getLat()
      : typeof position?.lat === "function"
        ? position.lat()
        : position?.y;
  const lng =
    typeof position?.getLng === "function"
      ? position.getLng()
      : typeof position?.lng === "function"
        ? position.lng()
        : position?.x;
  return { lat: Number(lat), lng: Number(lng) };
}

/**
 * 현재 지도 뷰포트를 위·경도 박스로 읽는다. kakao 와 naver 의 bounds 접근을
 * 여기서 한 번만 추상화한다.
 * - kakao: LatLngBounds.getSouthWest()/getNorthEast() → getLat()/getLng()
 * - naver: LatLngBounds.getMin()/getMax() (남서/북동) → lat()/lng()
 * 좌표 추출은 provider 무관하게 readPosition 이 형태를 흡수한다.
 */
function readMapBounds(runtime: MapRuntime, map: any): LatLngBox | null {
  return readBoundsBox(runtime, map?.getBounds?.());
}

/** SDK 의 LatLngBounds 객체를 위·경도 박스로 읽는다(readMapBounds 의 본체). */
function readBoundsBox(runtime: MapRuntime, bounds: any): LatLngBox | null {
  if (!bounds) return null;
  let sw: { lat: number; lng: number };
  let ne: { lat: number; lng: number };
  if (runtime.provider === "kakao") {
    sw = readPosition(bounds.getSouthWest?.());
    ne = readPosition(bounds.getNorthEast?.());
  } else {
    const min = bounds.getMin?.() ?? bounds.getSW?.();
    const max = bounds.getMax?.() ?? bounds.getNE?.();
    sw = readPosition(min);
    ne = readPosition(max);
  }
  if (
    !Number.isFinite(sw.lat) ||
    !Number.isFinite(sw.lng) ||
    !Number.isFinite(ne.lat) ||
    !Number.isFinite(ne.lng)
  ) {
    return null;
  }
  return { south: sw.lat, west: sw.lng, north: ne.lat, east: ne.lng };
}

function normalizeZoomLevel(level: number): number {
  const clamped = Math.max(ZOOM_MIN_LEVEL, Math.min(ZOOM_MAX_LEVEL, level));
  return (
    Math.round(clamped / ZOOM_LEVEL_STEP) * ZOOM_LEVEL_STEP
  );
}

function getNativeLevelForZoom(level: number): number {
  return Math.ceil(normalizeZoomLevel(level) - Number.EPSILON);
}

function getFineZoomScale(level: number): number {
  const normalized = normalizeZoomLevel(level);
  return 2 ** (getNativeLevelForZoom(normalized) - normalized);
}

function getNormalizedLevel(runtime: MapRuntime, map: any): number {
  const raw =
    runtime.provider === "kakao" ? map.getLevel() : 20 - map.getZoom();
  return Math.max(
    ZOOM_MIN_LEVEL,
    Math.min(ZOOM_MAX_LEVEL, Math.round(raw)),
  );
}

/**
 * 축척을 바꾼다. anchor(컨테이너 픽셀)를 주면 그 지점의 좌표가 화면에서 움직이지
 * 않도록 확대·축소한다. 결과 드로어가 지도 우측을 덮는 심사 모드에서 SDK 기본
 * 앵커(컨테이너 중심)를 쓰면 보이는 영역의 중심이 아니라 드로어 뒤쪽을 기준으로
 * 확대돼 사업지가 왼쪽으로 밀려 나갔다(2026-09-14 검수).
 */
function setNormalizedLevel(
  runtime: MapRuntime,
  map: any,
  level: number,
  animate = false,
  anchor?: PixelPoint,
) {
  const normalized = getNativeLevelForZoom(level);
  if (runtime.provider === "kakao") {
    const anchorLatLng =
      anchor &&
      map.getProjection?.()?.coordsFromContainerPoint?.(
        new runtime.sdk.maps.Point(anchor.x, anchor.y),
      );
    map.setLevel(
      normalized,
      anchorLatLng ? { animate, anchor: anchorLatLng } : { animate },
    );
    return;
  }
  const targetZoom = 20 - normalized;
  // naver MapSystemProjection 은 fromOffsetToCoord(컨테이너 픽셀→좌표)를 쓴다.
  // SDK 버전에 따라 이름이 다를 수 있어 둘 다 시도하고, 없으면 앵커 없이 확대한다.
  const projection = anchor ? map.getProjection?.() : null;
  const anchorPoint = anchor
    ? new runtime.sdk.maps.Point(anchor.x, anchor.y)
    : null;
  const anchorCoord =
    projection?.fromOffsetToCoord?.(anchorPoint) ??
    projection?.fromContainerToCoord?.(anchorPoint);
  if (anchorCoord && typeof map.zoomBy === "function") {
    map.zoomBy(targetZoom - map.getZoom(), anchorCoord, animate);
    return;
  }
  map.setZoom(targetZoom, animate);
}

/** 박스의 남서·북동 모서리로 SDK LatLngBounds 를 만든다. */
function boundsFromBox(runtime: MapRuntime, box: LatLngBox): any {
  const bounds = createBounds(runtime);
  bounds.extend(toMapPosition(runtime, box.south, box.west));
  bounds.extend(toMapPosition(runtime, box.north, box.east));
  return bounds;
}

function createMapInstance(
  runtime: MapRuntime,
  container: HTMLDivElement,
  viewport: MapViewport,
  naverMapStyle: NaverMapStyle,
  naverStyleId?: string,
): any {
  const center = toMapPosition(runtime, viewport.lat, viewport.lng);
  const nativeLevel = getNativeLevelForZoom(viewport.level);
  if (runtime.provider === "kakao") {
    const map = new runtime.sdk.maps.Map(container, {
      center,
      level: nativeLevel,
    });
    map.setMinLevel?.(1);
    map.setMaxLevel?.(14);
    return map;
  }

  const useCustomStyle = naverMapStyle === "ppt" && Boolean(naverStyleId);
  return new runtime.sdk.maps.Map(container, {
    center,
    zoom: 20 - nativeLevel,
    size: new runtime.sdk.maps.Size(
      Math.max(container.clientWidth, 1),
      Math.max(container.clientHeight, 1),
    ),
    minZoom: 6,
    maxZoom: 19,
    scaleControl: false,
    zoomControl: false,
    mapTypeControl: false,
    mapDataControl: true,
    logoControl: true,
    ...(useCustomStyle
      ? {
          gl: true,
          customStyleId: naverStyleId,
        }
      : {}),
  });
}

function resizeMapInstance(
  runtime: MapRuntime,
  map: any,
  container: HTMLDivElement,
) {
  if (runtime.provider === "kakao") {
    map.relayout?.();
    return;
  }
  map.setSize?.(
    new runtime.sdk.maps.Size(
      Math.max(container.clientWidth, 1),
      Math.max(container.clientHeight, 1),
    ),
  );
}

function addMapListener(
  runtime: MapRuntime,
  target: any,
  eventName: string,
  listener: (event?: any) => void,
): any {
  if (runtime.provider === "kakao") {
    runtime.sdk.maps.event.addListener(target, eventName, listener);
    return { target, eventName, listener };
  }
  return runtime.sdk.maps.Event.addListener(target, eventName, listener);
}

function removeMapListener(runtime: MapRuntime, listener: any) {
  if (!listener) return;
  if (runtime.provider === "kakao") {
    runtime.sdk.maps.event.removeListener(
      listener.target,
      listener.eventName,
      listener.listener,
    );
    return;
  }
  runtime.sdk.maps.Event.removeListener(listener);
}

function createHtmlOverlay(
  runtime: MapRuntime,
  map: any,
  position: any,
  content: HTMLElement,
  options: { yAnchor?: number; zIndex?: number } = {},
): any {
  if (runtime.provider === "kakao") {
    const overlay = new runtime.sdk.maps.CustomOverlay({
      position,
      content,
      yAnchor: options.yAnchor,
      zIndex: options.zIndex,
    });
    overlay.setMap(map);
    return overlay;
  }

  const wrapper = document.createElement("div");
  wrapper.className = "naver-html-overlay";
  wrapper.style.setProperty(
    "--overlay-y",
    `${Math.max(45, (options.yAnchor ?? 1) * 100)}%`,
  );
  wrapper.appendChild(content);
  return new runtime.sdk.maps.Marker({
    map,
    position,
    zIndex: options.zIndex,
    icon: {
      content: wrapper,
      anchor: new runtime.sdk.maps.Point(0, 0),
    },
  });
}

function createBounds(runtime: MapRuntime): any {
  return new runtime.sdk.maps.LatLngBounds();
}

function fitMapBounds(
  runtime: MapRuntime,
  map: any,
  bounds: any,
  padding: { top: number; right: number; bottom: number; left: number },
) {
  if (runtime.provider === "kakao") {
    map.setBounds(
      bounds,
      padding.top,
      padding.right,
      padding.bottom,
      padding.left,
    );
    return;
  }
  map.fitBounds(bounds, { ...padding, maxZoom: 19 });
}

function clickCoordinates(
  event: any,
): { lat: number; lng: number } | null {
  const position = event?.latLng ?? event?.coord;
  if (!position) return null;
  return readPosition(position);
}

function addOverlayClick(
  runtime: MapRuntime,
  overlay: any,
  listener: () => void,
) {
  addMapListener(runtime, overlay, "click", listener);
}

/** 영역으로 그린 시설의 링과 그 이름표 노드. 지도 mousemove 가 호버를 판정한다. */
interface HoverArea {
  ring: Array<{ lat: number; lng: number }>;
  node: HTMLElement;
  /** 이름표를 세울 때 거리 라벨(zIndex 10·11) 위로 올릴 오버레이. */
  overlay: any;
  baseZIndex: number;
}

// 호버 중 이름표가 거리 라벨(zIndex 10·11) 위에 서도록 올리는 값.
const HOVER_Z_INDEX = 30;

/**
 * 포인터 위치로 호버 상태를 다시 계산한다. SDK 의 폴리곤 mouseout 은 커스텀
 * 오버레이 위로 빠져나갈 때 오지 않아 이름표가 남았다(2026-09-14 검수). 매번
 * 링 포함 판정으로 세우고 내리므로 남는 이름표가 없다.
 */
function applyHover(
  areas: readonly HoverArea[],
  point: { lat: number; lng: number } | null,
) {
  areas.forEach(({ ring, node, overlay, baseZIndex }) => {
    const hovered = point !== null && pointInRing(point, ring);
    if (node.classList.contains("is-hover") === hovered) return;
    node.classList.toggle("is-hover", hovered);
    overlay?.setZIndex?.(hovered ? HOVER_Z_INDEX : baseZIndex);
  });
}

interface MapPanelProps {
  site: GeocodeCandidate | null;
  mapProvider: MapProvider;
  hazardMode?: boolean;
  hazardReview?: HazardReviewResult | null;
  hazardParcels?: HazardParcel[];
  onToggleParcelAt?: (lat: number, lng: number) => void;
  onToggleParcel?: (parcel: CadastralParcel) => void;
  selectedHazardFindingId?: string | null;
  onSelectHazardFinding?: (findingId: string | null) => void;
  selectedHazardFacilityId?: string | null;
  onSelectHazardFacility?: (facilityId: string | null) => void;
  /** 2차 배점 근거 시설. stage_two 의 hits 를 핀·최단거리선으로 올린다. */
  screeningResult?: ScreeningResult | null;
  /** 펼친 시설군 키. 그 군의 hit 전부를 지도에 보여준다(기본은 항목별 최근접 1곳). */
  expandedScreeningGroupKey?: string | null;
  /** 지도에서 강조 중인 2차 시설명. 후보 문 핀을 펼칠 기준이 된다. */
  selectedScreeningHitName?: string | null;
  onSelectScreeningHit?: (name: string | null) => void;
  /** 문·출구 후보를 눌러 기준점으로 지정한다(#7·#11). */
  onSelectCandidate?: (facility: string, candidate: FrontDoorCandidate) => void;
  /** 기준점 지정 모드 대상 시설명(#8·#9). 설정되면 지도가 지정 모드로 들어간다. */
  designationTarget?: string | null;
  onCancelDesignation?: () => void;
  /**
   * 지적도가 아직 안 깔린 자리를 눌러 그 주변 타일을 새로 불러왔을 때. 심사 결과가
   * 있는 상태라면 App 이 결과를 내리고 필지를 다시 고를 수 있게 되돌린다.
   */
  onCadastralRevive?: () => void;
  /**
   * 지도를 사업지 기준으로 재-fit 하라는 요청 카운터. 두 경우에 증가한다.
   * (1) 같은 검색어로 검색 버튼을 다시 눌러 스크롤로 벗어난 지도를 되돌릴 때(버그2),
   * (2) 검색 직후 자동 필지 로드가 성공해 그 필지에 맞춰 처음 화면을 잡아야 할 때.
   * 값이 바뀌면 site 좌표·필지·밴드에 맞춰 재-fit 한다. 필지 수동 토글은 이 값을
   * 건드리지 않으므로 재-fit 을 유발하지 않는다. 첫 마운트(0)에서는 추가 fit 이
   * 없도록 초기 ref 도 0 이다.
   */
  viewportRequest?: number;
}

export function MapPanel({
  site,
  mapProvider,
  hazardMode = false,
  hazardReview = null,
  hazardParcels = [],
  onToggleParcelAt,
  onToggleParcel,
  selectedHazardFindingId = null,
  onSelectHazardFinding,
  selectedHazardFacilityId = null,
  onSelectHazardFacility,
  screeningResult = null,
  expandedScreeningGroupKey = null,
  selectedScreeningHitName = null,
  onSelectScreeningHit,
  onSelectCandidate,
  designationTarget = null,
  onCancelDesignation,
  onCadastralRevive,
  viewportRequest = 0,
}: MapPanelProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<any>(null);
  const runtimeRef = useRef<MapRuntime | null>(null);
  const overlaysRef = useRef<any[]>([]);
  const viewportKeyRef = useRef("");
  // 검색 재클릭·자동 필지 로드 성공 시 넘어오는 recenter 요청 카운터의 마지막
  // 소비값(버그2). 첫 필지 fit 은 더 이상 추론하지 않고 이 카운터로만 받는다.
  const viewportRequestRef = useRef(0);
  const viewportSnapshotRef = useRef<MapViewport | null>(null);
  const zoomListenerRef = useRef<any>(null);
  const mapClickListenerRef = useRef<any>(null);
  const cadastralOverlaysRef = useRef<any[]>([]);
  // 지적도 타일 상태. site 가 바뀌면 세대(gen)를 올려 옛 세대 응답을 버리고 캐시를
  // 비운다. 중복·경합은 캐시·인플라이트·큐 세 집합으로 막는다.
  const tileCacheRef = useRef<Map<string, CadastralParcel[]>>(new Map());
  const tileInflightRef = useRef<Set<string>>(new Set());
  const tileTruncatedRef = useRef<Set<string>>(new Set());
  // truncated 로 하위 4타일로 나눈 부모 키. recompute 가 하위가 다 오면 부모 대신
  // 하위를 그린다(그 전까지는 부모 부분 결과를 유지해 화면이 비지 않게 한다).
  const tileSupersededRef = useRef<Set<string>>(new Set());
  const tileQueueRef = useRef<CadastralTile[]>([]);
  const tileQueuedKeysRef = useRef<Set<string>>(new Set());
  const tileActiveRef = useRef(0);
  const cadastralGenRef = useRef(0);
  const cadastralErrorRef = useRef(false);
  // 연속 실패 카운터와 마지막 실패 note(범례에 그대로 노출).
  const cadastralFailCountRef = useRef(0);
  const cadastralFailNoteRef = useRef("");
  const cadastralViewportRef = useRef<LatLngBox | null>(null);
  // 사업지 중심(3km·타일 필터 기준). site-effect 에서 채운다.
  const cadastralCenterRef = useRef<{ lat: number; lng: number } | null>(null);
  // idle 리스너는 지도 생성 시 한 번만 붙으므로, 최신 refresh 콜백을 ref 로 건넨다.
  const cadastralRefreshRef = useRef<() => void>(() => {});
  const cadastralIdleListenerRef = useRef<any>(null);
  const [cadastralParcels, setCadastralParcels] = useState<CadastralParcel[]>([]);
  const [cadastralNote, setCadastralNote] = useState("");
  const toggleParcelRef = useRef(onToggleParcelAt);
  const toggleCadastralRef = useRef(onToggleParcel);
  const fineZoomTargetRef = useRef<number | null>(5);
  const fineZoomLevelRef = useRef(5);
  const [mapError, setMapError] = useState("");
  const [mapReady, setMapReady] = useState(false);
  const [mapLevel, setMapLevel] = useState(5);
  const [mapInstanceRevision, setMapInstanceRevision] = useState(0);
  const [naverMapStyle] = useState<NaverMapStyle>("normal");
  // 용도지역(지적편집도) 오버레이 토글 (LH 확정 2026-09-11 #5). 석유대체연료
  // 「확인 요청」 결과가 뜨면 자동으로 켜지되, 사용자가 끄면 이 세션 동안 다시
  // 자동으로 켜지 않는다(zoningDismissedRef).
  const [zoningLayerOn, setZoningLayerOn] = useState(false);
  const zoningDismissedRef = useRef(false);
  // 지적도 타일 자동 갱신(idle 마다 뷰포트 타일 조회). 심사가 끝나면 끈다 —
  // 심사 시점 뷰포트까지만 남기고, 이후 이동해도 새로 받지 않는다. 사용자가
  // 스위치로 다시 켤 수 있고, 타일 없는 자리를 누르면 그 주변만 따로 받는다.
  const [cadastralAutoOn, setCadastralAutoOn] = useState(true);
  // 레이어 스위치 패널의 드래그 이동량(px). 손잡이를 끌면 바뀐다.
  const [switchOffset, setSwitchOffset] = useState({ x: 0, y: 0 });
  const switchDragRef = useRef<{ startX: number; startY: number; baseX: number; baseY: number } | null>(null);
  const startSwitchDrag = useCallback(
    (event: React.PointerEvent<HTMLElement>) => {
      event.preventDefault();
      const base = switchOffset;
      switchDragRef.current = {
        startX: event.clientX,
        startY: event.clientY,
        baseX: base.x,
        baseY: base.y,
      };
      const onMove = (move: PointerEvent) => {
        const drag = switchDragRef.current;
        if (!drag) return;
        setSwitchOffset({
          x: drag.baseX + (move.clientX - drag.startX),
          y: drag.baseY + (move.clientY - drag.startY),
        });
      };
      const onUp = () => {
        switchDragRef.current = null;
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [switchOffset],
  );
  const cadastralAutoRef = useRef(true);
  useEffect(() => {
    cadastralAutoRef.current = cadastralAutoOn;
  }, [cadastralAutoOn]);
  const cadastralParcelsRef = useRef<CadastralParcel[]>([]);
  // 받아 둔 타일이 바뀔 때마다 올라가는 판. 레이어가 꺼져 있어도(cadastralParcels
  // 비어 있음) 시설 영역 채우기가 새 타일을 반영하게 오버레이 효과의 의존성으로 쓴다.
  const [tileRevision, setTileRevision] = useState(0);
  // 지도에 영역으로 그린 시설 링(유해시설·2차 근거 시설). 지도 클릭이 영역 안이면
  // 필지 토글·타일 로드를 하지 않는다(영역 클릭은 시설 선택이다).
  const facilityRingsRef = useRef<Array<{ lat: number; lng: number }[]>>([]);
  // 영역 시설의 링·이름표 쌍. 지도 mousemove 가 이걸로 호버를 판정한다.
  const hoverAreasRef = useRef<HoverArea[]>([]);
  const mapHoverListenerRef = useRef<any>(null);
  const onCadastralReviveRef = useRef(onCadastralRevive);
  useEffect(() => {
    onCadastralReviveRef.current = onCadastralRevive;
  }, [onCadastralRevive]);
  // 브라우저 키는 런타임(localStorage → env 폴백)에서 읽는다. 값이 바뀌면
  // 아래 지도 초기화 이펙트가 다시 돌아 새 키로 SDK 를 다시 로드한다.
  const kakaoJavascriptKey = useRuntimeKey("VITE_KAKAO_JAVASCRIPT_KEY");
  const naverClientId = useRuntimeKey("VITE_NAVER_MAP_CLIENT_ID");
  const naverStyleId = useRuntimeKey("VITE_NAVER_MAP_STYLE_ID");
  const selectedProviderConfigured =
    mapProvider === "kakao"
      ? Boolean(kakaoJavascriptKey)
      : Boolean(naverClientId);
  const hazardMarkers = useMemo(
    () =>
      hazardReview?.findings.flatMap((finding) => [
        ...finding.facilities.map((facility) => ({
          finding,
          facility,
          nearby: false,
        })),
        // 판정창 밖·참고 반경 이내 시설. 판정 핀보다 한 단계 낮게 회색으로 얹는다.
        ...(finding.nearby_facilities ?? []).map((facility) => ({
          finding,
          facility,
          nearby: true,
        })),
      ]) ?? [],
    [hazardReview],
  );
  const selectedHazardFinding = useMemo<HazardFinding | null>(
    () =>
      hazardReview?.findings.find(
        (finding) => finding.finding_id === selectedHazardFindingId,
      ) ?? null,
    [hazardReview, selectedHazardFindingId],
  );
  // 2차 배점 근거 시설. 기본은 평가항목별 최근접 1곳, 시설군 행을 펼치면 그 군 전부.
  const screeningHitRefs = useMemo(
    () => flattenScreeningHits(screeningResult),
    [screeningResult],
  );
  const visibleScreeningHitRefs = useMemo(
    () => visibleScreeningHits(screeningHitRefs, expandedScreeningGroupKey),
    [screeningHitRefs, expandedScreeningGroupKey],
  );
  // 후보 문을 펼칠 대상. 선택된 시설 중 후보가 2개 이상일 때만.
  const candidateHitRef = useMemo<ScreeningHitRef | null>(() => {
    if (!selectedScreeningHitName) return null;
    return (
      screeningHitRefs.find(
        (ref) =>
          ref.hit.name === selectedScreeningHitName &&
          (ref.hit.front_door_candidates?.length ?? 0) >= 2,
      ) ?? null
    );
  }, [screeningHitRefs, selectedScreeningHitName]);

  const fineZoomScale = getFineZoomScale(mapLevel);

  // 석유대체연료 「확인 요청」(주거지역·용도지역 미확인) 시설이 결과에 있는지.
  // 있으면 용도지역 오버레이를 자동으로 켜 담당자가 지적편집도와 대조하게 한다.
  const hasZoningReviewRequest = useMemo(
    () =>
      (hazardReview?.findings ?? []).some((finding) =>
        finding.facilities.some(
          (facility) =>
            facility.zoning_class === "residential" ||
            facility.zoning_class === "unknown",
        ),
      ),
    [hazardReview],
  );
  // 용도지역 오버레이는 카카오 지도에서만 제공된다(네이버 SDK 미지원). 키 없음·
  // 네이버 모드면 버튼을 비활성한다.
  const zoningLayerSupported =
    mapProvider === "kakao" && selectedProviderConfigured;

  const toggleZoningLayer = useCallback(() => {
    setZoningLayerOn((on) => {
      const next = !on;
      // 사용자가 직접 끄면 이 세션 동안 자동 재점등을 막는다.
      if (!next) zoningDismissedRef.current = true;
      return next;
    });
  }, []);

  // 자동 점등: 카카오 모드에서 석유대체연료 확인 요청 결과가 뜨고, 사용자가 끈 적이
  // 없으면 켠다.
  useEffect(() => {
    if (
      hazardMode &&
      zoningLayerSupported &&
      hasZoningReviewRequest &&
      !zoningDismissedRef.current
    ) {
      setZoningLayerOn(true);
    }
  }, [hazardMode, zoningLayerSupported, hasZoningReviewRequest]);

  // 네이버 모드·키 미설정으로 오버레이를 쓸 수 없으면 꺼진 상태로 되돌린다.
  useEffect(() => {
    if (!zoningLayerSupported && zoningLayerOn) {
      setZoningLayerOn(false);
    }
  }, [zoningLayerSupported, zoningLayerOn]);

  // 지적편집도(USE_DISTRICT) 오버레이를 카카오 지도에 켜고 끈다. 지도 재생성
  // (mapInstanceRevision)·준비완료(mapReady) 이후에도 상태를 다시 반영한다.
  // 레이어 로드 실패는 SDK 가 조용히 무시하므로 별도 처리가 없다.
  useEffect(() => {
    const map = mapRef.current;
    const runtime = runtimeRef.current;
    if (!map || !runtime || runtime.provider !== "kakao" || !mapReady) return;
    const typeId = runtime.sdk?.maps?.MapTypeId?.USE_DISTRICT;
    if (typeId == null) return;
    if (zoningLayerOn) {
      map.addOverlayMapTypeId(typeId);
    } else {
      map.removeOverlayMapTypeId(typeId);
    }
    return () => {
      try {
        map.removeOverlayMapTypeId?.(typeId);
      } catch {
        // 지도 파기 중이면 무시한다.
      }
    };
  }, [zoningLayerOn, mapReady, mapInstanceRevision, mapProvider]);

  useEffect(() => {
    toggleParcelRef.current = onToggleParcelAt;
  }, [onToggleParcelAt]);

  useEffect(() => {
    toggleCadastralRef.current = onToggleParcel;
  }, [onToggleParcel]);

  // 캐시에 있는 타일 중 "현재 뷰포트와 겹치는" 필지만 골라 화면에 그린다.
  // 뷰포트 밖 타일은 캐시에만 남기고 그리지 않는다. 폴리곤이 2,500 을 넘으면
  // 뷰포트 중심에서 먼 타일부터 자연히 빠지도록 가까운 타일부터 채운다.
  // 아직 캐시·인플라이트·큐에 없는 타일만 큐에 넣는다(중복·경합 방지).
  const queueTile = useCallback((tile: CadastralTile) => {
    if (
      tileCacheRef.current.has(tile.key) ||
      tileInflightRef.current.has(tile.key) ||
      tileQueuedKeysRef.current.has(tile.key)
    ) {
      return;
    }
    tileQueuedKeysRef.current.add(tile.key);
    tileQueueRef.current.push(tile);
  }, []);

  const recomputeVisibleParcels = useCallback(() => {
    const box = cadastralViewportRef.current;
    // 게이트·3km 밖 상태는 note 를 따로 세워 두므로 여기서 건드리지 않는다.
    if (!box) return;
    const center = boxCenter(box);

    // 뷰포트와 겹치는 leaf 타일을 모은다. superseded(쿼드 분할한) 부모는 하위가
    // 모두 준비됐을 때만 하위로 내려가고, 아직이면 부모 부분 결과를 그대로 쓴다
    // (하위가 오면 자연히 하위로 교체된다 → 화면이 비지 않는다).
    const leaves: CadastralTile[] = [];
    const walk = (tile: CadastralTile) => {
      if (!boxesOverlap(tile.box, box)) return;
      if (tileSupersededRef.current.has(tile.key)) {
        const children = subdivideTile(tile).filter((child) =>
          boxesOverlap(child.box, box),
        );
        const ready = children.every(
          (child) =>
            tileCacheRef.current.has(child.key) ||
            tileSupersededRef.current.has(child.key),
        );
        if (ready) {
          children.forEach(walk);
          return;
        }
      }
      if (!tileCacheRef.current.has(tile.key)) return;
      leaves.push(tile);
    };
    tilesForViewport(box).forEach(walk);

    leaves.sort(
      (a, b) =>
        metersBetween(center, tileCenter(a)) -
        metersBetween(center, tileCenter(b)),
    );
    const seen = new Map<string, CadastralParcel>();
    let truncatedVisible = false;
    for (const tile of leaves) {
      if (tileTruncatedRef.current.has(tile.key)) truncatedVisible = true;
      if (seen.size >= MAX_CADASTRAL_POLYGONS) break;
      for (const parcel of tileCacheRef.current.get(tile.key)!) {
        seen.set(parcel.pnu, parcel);
        if (seen.size >= MAX_CADASTRAL_POLYGONS) break;
      }
    }
    // 레이어가 꺼져 있으면 화면에는 그리지 않는다(받아 둔 타일은 그대로 남긴다).
    setCadastralParcels(cadastralAutoRef.current ? [...seen.values()] : []);
    setTileRevision((current) => current + 1);

    // note 우선순위: 로딩 → 실패(백엔드 note 그대로) → 상한/일부표시 → 없음.
    if (tileInflightRef.current.size > 0 && seen.size === 0) {
      setCadastralNote("필지 경계를 불러오는 중…");
    } else if (cadastralErrorRef.current) {
      setCadastralNote(
        cadastralFailNoteRef.current || "일부 필지 경계를 불러오지 못했습니다.",
      );
    } else if (truncatedVisible || seen.size >= MAX_CADASTRAL_POLYGONS) {
      setCadastralNote("필지가 많아 일부만 표시합니다. 더 확대하면 자세히 보입니다.");
    } else {
      setCadastralNote("");
    }
  }, []);

  // 타일 큐를 동시 4개 상한으로 흘려 보낸다. 하나 끝날 때마다 화면을 다시 그리고
  // 다음 타일을 채운다. 옛 세대(gen) 응답은 버린다.
  const pumpTileQueue = useCallback(
    (gen: number) => {
      while (
        tileActiveRef.current < CADASTRAL_MAX_CONCURRENT &&
        tileQueueRef.current.length > 0
      ) {
        if (cadastralGenRef.current !== gen) {
          tileQueueRef.current = [];
          tileQueuedKeysRef.current.clear();
          return;
        }
        // 연속 실패가 상한이면 이 세대 동안 조회를 멈춘다(무한 재시도 방지).
        if (cadastralFailCountRef.current >= CADASTRAL_MAX_FAILURES) {
          tileQueueRef.current = [];
          tileQueuedKeysRef.current.clear();
          recomputeVisibleParcels();
          return;
        }
        const tile = tileQueueRef.current.shift()!;
        tileQueuedKeysRef.current.delete(tile.key);
        if (
          tileCacheRef.current.has(tile.key) ||
          tileInflightRef.current.has(tile.key)
        ) {
          continue;
        }
        tileInflightRef.current.add(tile.key);
        tileActiveRef.current += 1;
        getParcelsInBounds(tile.box)
          .then((response) => {
            if (cadastralGenRef.current !== gen) return;
            // 백엔드는 키 없음·API 오류를 200 + parcels=[] + note 로 돌려준다.
            // 이를 정상 빈 타일로 캐시하면 재시도도 안 되고 안내도 없다. 실패로 본다.
            if (response.parcels.length === 0 && response.note) {
              cadastralErrorRef.current = true;
              cadastralFailNoteRef.current = response.note;
              cadastralFailCountRef.current += 1;
              return;
            }
            // 성공: 실패 상태를 되돌려 다음 요청이 정상 진행되게 한다.
            cadastralFailCountRef.current = 0;
            cadastralErrorRef.current = false;
            cadastralFailNoteRef.current = "";
            tileCacheRef.current.set(tile.key, response.parcels);
            if (response.truncated) {
              if (tile.depth < MAX_TILE_DEPTH) {
                // 하위 4타일로 나눠 재요청. 하위가 다 오면 recompute 가 부모 대신
                // 하위를 그린다. 하위도 3km·뷰포트 교차 규칙을 같이 탄다.
                tileSupersededRef.current.add(tile.key);
                const center = cadastralCenterRef.current;
                const viewport = cadastralViewportRef.current;
                subdivideTile(tile)
                  .filter(
                    (child) =>
                      (!center ||
                        tileWithinRadius(
                          center,
                          child,
                          CADASTRAL_MAX_RADIUS_M,
                        )) &&
                      (!viewport || boxesOverlap(child.box, viewport)),
                  )
                  .forEach(queueTile);
              } else {
                // 최대 깊이에서도 잘리면 그때만 "일부만 표시" 로 남긴다.
                tileTruncatedRef.current.add(tile.key);
              }
            }
          })
          .catch(() => {
            if (cadastralGenRef.current !== gen) return;
            // 네트워크 실패도 캐시하지 않아 다음 idle 에 재시도할 수 있다.
            cadastralErrorRef.current = true;
            cadastralFailCountRef.current += 1;
          })
          .finally(() => {
            // fix: 옛 세대는 새 세대 bookkeeping 을 건드리지 않는다. site 변경 시
            // site-effect 가 inflight/active 를 이미 0 으로 초기화하므로, 옛 요청이
            // 늦게 끝나도 여기서 카운터를 음수로 만들거나 새 inflight 를 지우면
            // 안 된다. 그래서 gen 확인을 delete/decrement 앞에 둔다.
            if (cadastralGenRef.current !== gen) return;
            tileInflightRef.current.delete(tile.key);
            tileActiveRef.current -= 1;
            recomputeVisibleParcels();
            pumpTileQueue(gen);
          });
      }
    },
    [recomputeVisibleParcels, queueTile],
  );

  // 타일을 큐에 넣고 펌프를 돌린다.
  const enqueueTiles = useCallback(
    (tiles: CadastralTile[], gen: number) => {
      tiles.forEach(queueTile);
      pumpTileQueue(gen);
    },
    [queueTile, pumpTileQueue],
  );

  // 지도 idle 마다(디바운스) 호출. 확대 수준·3km·뷰포트를 보고 타일을 조회·표시한다.
  const runCadastralRefresh = useCallback(() => {
    const map = mapRef.current;
    const runtime = runtimeRef.current;
    if (!runtime || !map || !mapReady || !site) return;

    // 확대 수준 게이트: 필지가 읽히는 수준(정규화 레벨 ≤ 4)에서만 조회·표시하고,
    // 더 축소되면 오버레이를 내린 뒤 안내만 남긴다.
    const level = getNormalizedLevel(runtime, map);
    if (level > CADASTRAL_MAX_LEVEL) {
      cadastralViewportRef.current = null;
      setCadastralParcels([]);
      setCadastralNote("확대하면 필지 경계가 표시됩니다.");
      return;
    }

    const box = readMapBounds(runtime, map);
    if (!box) return;

    const center = site.coordinates;
    // 뷰포트 전체가 사업지 3km 밖이면 조회하지 않는다.
    if (viewportOutsideRadius(center, box, CADASTRAL_MAX_RADIUS_M)) {
      cadastralViewportRef.current = null;
      setCadastralParcels([]);
      setCadastralNote("사업지 3km 밖은 필지를 표시하지 않습니다.");
      return;
    }

    cadastralViewportRef.current = box;
    // 지도를 옮긴다고 새로 받지 않는다. 타일은 검색 직후 사업지 주변 350m 와
    // 빈 자리 클릭 주변 350m 에서만 받고, 여기서는 받아 둔 것을 뷰포트에 맞춰 그린다.
    recomputeVisibleParcels();
  }, [mapReady, site, recomputeVisibleParcels]);

  /** 누른 자리 주변 350m 타일만 따로 받는다(스위치 상태와 무관). */
  const loadCadastralAround = useCallback(
    (point: { lat: number; lng: number }) => {
      const center = cadastralCenterRef.current;
      if (!center) return;
      const deltaLat = CADASTRAL_CLICK_RADIUS_M / METERS_PER_DEGREE_LAT;
      const deltaLng =
        CADASTRAL_CLICK_RADIUS_M /
        (METERS_PER_DEGREE_LAT *
          Math.max(Math.cos((point.lat * Math.PI) / 180), 0.2));
      const box: LatLngBox = {
        south: point.lat - deltaLat,
        north: point.lat + deltaLat,
        west: point.lng - deltaLng,
        east: point.lng + deltaLng,
      };
      const pending = tilesForViewport(box).filter(
        (tile) =>
          tileWithinRadius(point, tile, CADASTRAL_CLICK_RADIUS_M) &&
          tileWithinRadius(center, tile, CADASTRAL_MAX_RADIUS_M),
      );
      enqueueTiles(pending, cadastralGenRef.current);
    },
    [enqueueTiles],
  );
  const loadCadastralAroundRef = useRef(loadCadastralAround);
  useEffect(() => {
    loadCadastralAroundRef.current = loadCadastralAround;
  }, [loadCadastralAround]);

  // idle 리스너(지도 생성 시 1회 등록)가 항상 최신 콜백을 부르도록 ref 를 갱신한다.
  useEffect(() => {
    cadastralRefreshRef.current = runCadastralRefresh;
  }, [runCadastralRefresh]);

  // site 가 바뀌면 타일 상태를 전부 비우고, 첫 화면에 필지가 보이게 사업지 주변
  // 타일을 즉시 요청한다. 이후는 idle 마다 뷰포트를 따라 3km 까지 채운다.
  useEffect(() => {
    cadastralGenRef.current += 1;
    const gen = cadastralGenRef.current;
    tileCacheRef.current.clear();
    tileInflightRef.current.clear();
    tileTruncatedRef.current.clear();
    tileSupersededRef.current.clear();
    tileQueueRef.current = [];
    tileQueuedKeysRef.current.clear();
    tileActiveRef.current = 0;
    cadastralErrorRef.current = false;
    cadastralFailCountRef.current = 0;
    cadastralFailNoteRef.current = "";
    cadastralViewportRef.current = null;
    cadastralCenterRef.current = null;
    setCadastralParcels([]);
    setCadastralNote("");
    setCadastralAutoOn(true);
    cadastralAutoRef.current = true;
    facilityRingsRef.current = [];
    if (!site) return;
    cadastralCenterRef.current = site.coordinates;

    const { lat, lng } = site.coordinates;
    const deltaLat = CADASTRAL_RADIUS_M / METERS_PER_DEGREE_LAT;
    const deltaLng =
      CADASTRAL_RADIUS_M /
      (METERS_PER_DEGREE_LAT * Math.max(Math.cos((lat * Math.PI) / 180), 0.2));
    // 지도 idle 을 기다리지 않고 뷰포트 ref 를 사업지 박스로 미리 채워 즉시 표시.
    const box: LatLngBox = {
      south: lat - deltaLat,
      north: lat + deltaLat,
      west: lng - deltaLng,
      east: lng + deltaLng,
    };
    cadastralViewportRef.current = box;
    const pending = tilesForViewport(box).filter((tile) =>
      tileWithinRadius(site.coordinates, tile, CADASTRAL_MAX_RADIUS_M),
    );
    enqueueTiles(pending, gen);
  }, [site, enqueueTiles]);

  // 심사 결과가 오면 지적도 레이어를 끈다(숨김 + 조회 중단). 시설 영역은 받아 둔
  // 타일에서 계속 찾아 칠한다.
  useEffect(() => {
    if (!screeningResult) return;
    cadastralAutoRef.current = false;
    setCadastralAutoOn(false);
    setCadastralParcels([]);
  }, [screeningResult]);

  useEffect(() => {
    cadastralParcelsRef.current = cadastralParcels;
  }, [cadastralParcels]);

  // 지적도 경계는 마커·밴드와 수명이 달라 별도 레이어로 관리한다.
  useEffect(() => {
    const runtime = runtimeRef.current;
    const map = mapRef.current;
    cadastralOverlaysRef.current.forEach((overlay) => overlay.setMap?.(null));
    cadastralOverlaysRef.current = [];
    if (!runtime || !map || !mapReady) return;

    const selected = new Set(hazardParcels.map((parcel) => parcel.pnu));
    cadastralParcels.forEach((parcel) => {
      if (parcel.geometry.length < 4) return;
      const isSelected = selected.has(parcel.pnu);
      const path = parcel.geometry.map((point) =>
        toMapPosition(runtime, point.lat, point.lng),
      );
      const polygon = new runtime.sdk.maps.Polygon({
        map,
        ...(runtime.provider === "kakao" ? { path } : { paths: path }),
        strokeWeight: isSelected ? 3 : 1,
        strokeColor: isSelected ? "#2563eb" : "#8b5cf6",
        strokeOpacity: isSelected ? 1 : 0.55,
        strokeStyle: "solid",
        fillColor: isSelected ? "#3b82f6" : "#a78bfa",
        // 투명에 가깝게라도 채워야 필지 내부 클릭이 잡힌다.
        fillOpacity: isSelected ? 0.18 : 0.04,
        clickable: true,
      });
      addOverlayClick(runtime, polygon, () => {
        toggleCadastralRef.current?.(parcel);
      });
      cadastralOverlaysRef.current.push(polygon);
    });

    return () => {
      cadastralOverlaysRef.current.forEach((overlay) => overlay.setMap?.(null));
      cadastralOverlaysRef.current = [];
    };
  }, [cadastralParcels, hazardParcels, mapReady, mapInstanceRevision]);

  // 기준점 지정 모드에서는 Esc 로 언제든 빠져나올 수 있어야 한다.
  useEffect(() => {
    if (!designationTarget) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancelDesignation?.();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [designationTarget, onCancelDesignation]);

  const changeZoom = useCallback(
    (direction: "in" | "out") => {
      const map = mapRef.current;
      const runtime = runtimeRef.current;
      if (!map || !runtime) return;
      const currentLevel = fineZoomLevelRef.current;
      const nextLevel = normalizeZoomLevel(
        currentLevel +
          (direction === "in" ? -ZOOM_LEVEL_STEP : ZOOM_LEVEL_STEP),
      );
      fineZoomTargetRef.current = nextLevel;
      fineZoomLevelRef.current = nextLevel;
      setMapLevel(nextLevel);
      // 드로어·툴바를 뺀 "보이는 영역"의 중심을 앵커로 확대·축소한다.
      const container = containerRef.current;
      const anchor = container
        ? visibleCenterPoint(
            {
              width: container.clientWidth,
              height: container.clientHeight,
            },
            viewportPadding(hazardMode),
          )
        : undefined;
      setNormalizedLevel(runtime, map, nextLevel, true, anchor);
    },
    [hazardMode],
  );

  useEffect(() => {
    const container = containerRef.current;
    const providerKey =
      mapProvider === "kakao" ? kakaoJavascriptKey : naverClientId;
    if (!providerKey || !container) {
      setMapReady(false);
      return;
    }

    let cancelled = false;
    let createdMap: any = null;
    let createdRuntime: MapRuntime | null = null;
    let resizeObserver: ResizeObserver | null = null;
    let resizeFrame = 0;
    // 지적도 타일 조회 디바운스 타이머(팬·줌이 멈춘 뒤에만 조회).
    let cadastralIdleTimer = 0;
    const authFailureHandler = () => {
      if (cancelled) return;
      setMapReady(false);
      setMapError(
        "네이버 지도 인증에 실패했습니다. Dynamic Map 사용 설정과 현재 접속 주소의 Web 서비스 URL 등록을 확인해 주세요.",
      );
    };
    setMapReady(false);
    setMapError("");
    if (mapProvider === "naver") {
      window.navermap_authFailure = authFailureHandler;
    }

    const loader =
      mapProvider === "kakao"
        ? loadKakaoMap(providerKey)
        : loadNaverMap(
            providerKey,
            naverMapStyle === "ppt" && Boolean(naverStyleId),
          );

    loader
      .then((sdk) => {
        if (cancelled || !container.isConnected) return;
        const initial = viewportSnapshotRef.current ?? {
          ...(site?.coordinates ?? {
            lat: 37.5665,
            lng: 126.978,
          }),
          level: 5,
        };
        container.replaceChildren();
        createdRuntime = { provider: mapProvider, sdk };
        createdMap = createMapInstance(
          createdRuntime,
          container,
          initial,
          naverMapStyle,
          naverStyleId,
        );
        runtimeRef.current = createdRuntime;
        mapRef.current = createdMap;

        // 영역 시설 호버: 포인터가 링 안이면 이름표를 세우고, 벗어나면 내린다.
        mapHoverListenerRef.current = addMapListener(
          createdRuntime,
          createdMap,
          "mousemove",
          (event) => {
            applyHover(hoverAreasRef.current, clickCoordinates(event));
          },
        );
        container.addEventListener("pointerleave", () => {
          applyHover(hoverAreasRef.current, null);
        });

        // 지도 빈 곳을 누르면: 그 자리에 지적도가 깔려 있으면 필지 토글, 없으면
        // 주변 300m 타일을 새로 받는다(심사 뒤라면 App 이 결과를 내려 다시 고르게 한다).
        mapClickListenerRef.current = addMapListener(
          createdRuntime,
          createdMap,
          "click",
          (event) => {
            const point = clickCoordinates(event);
            if (!point) return;
            // 유해시설·편의시설 영역 안 클릭은 시설 선택이다(폴리곤 리스너가 처리).
            if (facilityRingsRef.current.some((ring) => pointInRing(point, ring))) {
              return;
            }
            // 심사 결과가 있으면 먼저 내려 필지 잠금을 푼다(App 이 동기로 푼다).
            onCadastralReviveRef.current?.();
            // 스위치 상태와 무관하게: 레이어를 켜고 누른 자리 주변 350m 타일을 받는다.
            cadastralAutoRef.current = true;
            setCadastralAutoOn(true);
            loadCadastralAroundRef.current(point);
            // 누른 자리의 필지를 사업지에 더한다(좌표로 필지를 조회한다).
            toggleParcelRef.current?.(point.lat, point.lng);
          },
        );

        zoomListenerRef.current = addMapListener(
          createdRuntime,
          createdMap,
          "zoom_changed",
          () => {
            if (!runtimeRef.current || !mapRef.current) return;
            const nativeLevel = getNormalizedLevel(
              runtimeRef.current,
              mapRef.current,
            );
            const targetLevel = fineZoomTargetRef.current;
            if (
              targetLevel !== null &&
              getNativeLevelForZoom(targetLevel) === nativeLevel
            ) {
              fineZoomLevelRef.current = targetLevel;
              setMapLevel(targetLevel);
              return;
            }
            fineZoomTargetRef.current = null;
            fineZoomLevelRef.current = nativeLevel;
            setMapLevel(nativeLevel);
          },
        );
        // 지도 팬·줌이 멈추면(idle) 뷰포트와 겹치는 지적도 타일을 채운다.
        // kakao·naver 모두 "idle" 이벤트를 지원하므로 addMapListener 로 통일한다.
        cadastralIdleListenerRef.current = addMapListener(
          createdRuntime,
          createdMap,
          "idle",
          () => {
            window.clearTimeout(cadastralIdleTimer);
            cadastralIdleTimer = window.setTimeout(() => {
              cadastralRefreshRef.current();
            }, 300);
          },
        );

        const initialFineLevel = normalizeZoomLevel(initial.level);
        fineZoomTargetRef.current = initialFineLevel;
        fineZoomLevelRef.current = initialFineLevel;
        setMapLevel(initialFineLevel);
        setMapReady(true);
        setMapInstanceRevision((current) => current + 1);

        resizeFrame = window.requestAnimationFrame(() => {
          if (cancelled || !createdRuntime || !createdMap) return;
          resizeMapInstance(createdRuntime, createdMap, container);
        });
        resizeObserver = new ResizeObserver(() => {
          if (cancelled || !createdRuntime || !createdMap) return;
          resizeMapInstance(createdRuntime, createdMap, container);
        });
        resizeObserver.observe(container);
      })
      .catch((error: Error) => {
        if (!cancelled) {
          setMapReady(false);
          setMapError(error.message);
        }
      });

    return () => {
      cancelled = true;
      if (resizeFrame) window.cancelAnimationFrame(resizeFrame);
      resizeObserver?.disconnect();
      if (window.navermap_authFailure === authFailureHandler) {
        delete window.navermap_authFailure;
      }
      if (createdMap && createdRuntime && mapRef.current === createdMap) {
        const center = readPosition(createdMap.getCenter());
        if (Number.isFinite(center.lat) && Number.isFinite(center.lng)) {
          viewportSnapshotRef.current = {
            ...center,
            level: fineZoomLevelRef.current,
          };
        }
        overlaysRef.current.forEach((overlay) => overlay.setMap?.(null));
        overlaysRef.current = [];
        removeMapListener(createdRuntime, zoomListenerRef.current);
        zoomListenerRef.current = null;
        removeMapListener(createdRuntime, mapClickListenerRef.current);
        mapClickListenerRef.current = null;
        removeMapListener(createdRuntime, mapHoverListenerRef.current);
        mapHoverListenerRef.current = null;
        window.clearTimeout(cadastralIdleTimer);
        removeMapListener(createdRuntime, cadastralIdleListenerRef.current);
        cadastralIdleListenerRef.current = null;
        if (createdRuntime.provider === "naver") createdMap.destroy?.();
        mapRef.current = null;
        runtimeRef.current = null;
        container.replaceChildren();
      }
    };
  }, [
    kakaoJavascriptKey,
    mapProvider,
    naverClientId,
    naverMapStyle,
    naverStyleId,
  ]);

  useEffect(() => {
    const map = mapRef.current;
    const runtime = runtimeRef.current;
    if (!selectedProviderConfigured || !mapReady || !site || !map || !runtime)
      return;
    // 필지 목록은 뷰포트 키에서 뺀다. 필지를 토글할 때마다 키가 바뀌면
    // shouldUpdateViewport 가 true 가 되어 축척·중심이 리셋됐다(버그1).
    // review_id 가 없으면 "draft" 로 고정한다. 이전 `?? join(",") ?? "draft"`
    // 체인은 join 이 빈 문자열이라도 값으로 취급돼 "draft" 로 떨어지지 않았다.
    const viewportKey = `${site.id}:hazard:${hazardReview?.review_id ?? "draft"}`;
    // 재-fit 은 두 명시적 신호로만 일어난다. (1) viewportKey 변경(사업지·심사
    // 결과 등 화면 성격이 바뀐 경우), (2) viewportRequest 카운터 변경(검색 재클릭
    // 또는 자동 필지 로드 성공). 필지 도형 등장을 추론해 fit 하지 않는다. 예전엔
    // "첫 필지 등장"을 추론했는데, 자동 로드가 실패·0건이면 그 표식이 남아 있다가
    // 사용자가 수동으로 처음 필지를 고른 순간 지도가 검색 지역으로 튀는 버그가 있었다.
    const shouldUpdateViewport =
      viewportKeyRef.current !== viewportKey ||
      viewportRequestRef.current !== viewportRequest;

    overlaysRef.current.forEach((overlay) => overlay.setMap?.(null));
    overlaysRef.current = [];
    facilityRingsRef.current = [];
    hoverAreasRef.current = [];
    // 시설 영역 후보 필지: 레이어 표시 여부와 무관하게 받아 둔 타일 전체.
    const parcelPool: CadastralParcel[] = [];
    tileCacheRef.current.forEach((parcels) => parcelPool.push(...parcels));

    const center = toMapPosition(
      runtime,
      site.coordinates.lat,
      site.coordinates.lng,
    );
    if (shouldUpdateViewport) {
      map.setCenter(center);
    }

    // 대지경계 기준 밴드가 있으면 중심점 원 대신 필지를 확장한 도형을 그린다.
    const ruleBands = hazardMode ? (hazardReview?.rule_bands ?? []) : [];
    const siteParcelRings = hazardParcels.filter(
      (item) =>
        item.geometry.length >= 4 &&
        (item.geometry_source === "parcel_polygon" ||
          item.geometry_source === "official_polygon"),
    );
    const hasCadastralParcel = siteParcelRings.length > 0;
    // 대지경계 밴드를 못 그릴 때만 중심점 기준 원으로 물러선다.
    const hazardBufferRules =
      !hazardMode || ruleBands.length > 0 || hasCadastralParcel
        ? []
        : [
            { radius: 500, color: "#f59e0b", opacity: 0.045, dashed: false },
            { radius: 50, color: "#fb7185", opacity: 0.075, dashed: true },
            { radius: 25, color: "#ef4444", opacity: 0.11, dashed: true },
          ];
    const bufferRules = hazardBufferRules;
    bufferRules.forEach((rule) => {
      const radiusCircle = new runtime.sdk.maps.Circle({
        center,
        radius: rule.radius,
        strokeWeight: 2,
        strokeColor: rule.color,
        strokeOpacity: 0.9,
        strokeStyle: rule.dashed ? "shortdash" : "solid",
        fillColor: rule.color,
        fillOpacity: rule.opacity,
      });
      radiusCircle.setMap(map);
      overlaysRef.current.push(radiusCircle);
    });

    // SITE 마커는 검색 좌표가 아니라 지금 고른 대표 필지(첫 항목) 위에 선다.
    // 검색 지점 필지를 빼고 다른 필지를 고르면 사업지가 그쪽으로 옮겨 간 것이다.
    const representative = hazardParcels.find((p) => p.geometry.length >= 4);
    const representativeCenter = representative
      ? ringCentroid(representative.geometry)
      : null;
    const siteMarkerPosition = representativeCenter
      ? toMapPosition(runtime, representativeCenter.lat, representativeCenter.lng)
      : center;
    const siteNode = document.createElement("button");
    siteNode.type = "button";
    siteNode.className = "site-map-marker";
    siteNode.textContent = "SITE";
    siteNode.title = site.name;
    const siteOverlay = createHtmlOverlay(runtime, map, siteMarkerPosition, siteNode, {
      yAnchor: 1.5,
      zIndex: 5,
    });
    overlaysRef.current.push(siteOverlay);

    const bounds = createBounds(runtime);
    bounds.extend(center);

    // 넓은 밴드부터 그려야 좁은 밴드와 필지가 위에 남는다.
    [...ruleBands]
      .sort((a, b) => b.threshold_m - a.threshold_m)
      .forEach((band) => {
        if (band.ring.length < 4) return;
        const bandColor =
          band.threshold_m >= 500
            ? "#f59e0b"
            : band.threshold_m >= 50
              ? "#fb7185"
              : "#ef4444";
        const path = band.ring.map((point) =>
          toMapPosition(runtime, point.lat, point.lng),
        );
        const polygon = new runtime.sdk.maps.Polygon({
          map,
          ...(runtime.provider === "kakao" ? { path } : { paths: path }),
          // 판정 기준선은 보이되 화면을 덮지 않게 채움 없이 얇은 점선만 그린다.
          strokeWeight: 1.5,
          strokeColor: bandColor,
          strokeOpacity: 0.85,
          strokeStyle: "shortdash",
          fillColor: bandColor,
          fillOpacity: 0,
        });
        overlaysRef.current.push(polygon);
        path.forEach((point) => bounds.extend(point));
      });

    hazardParcels.forEach((parcel, index) => {
      if (parcel.geometry.length < 4) return;
      const path = parcel.geometry.map((point) =>
        toMapPosition(runtime, point.lat, point.lng),
      );
      // 대표 필지(첫 항목)는 굵은 테두리로 구분한다(#9 다필지 합집합).
      const representative = index === 0;
      const parcelPolygon = new runtime.sdk.maps.Polygon({
        map,
        ...(runtime.provider === "kakao" ? { path } : { paths: path }),
        strokeWeight: representative ? 5 : 2,
        strokeColor: representative ? "#2563eb" : "#3b82f6",
        strokeOpacity: 1,
        strokeStyle: "solid",
        fillColor: "#3b82f6",
        fillOpacity: representative ? 0.18 : 0.1,
      });
      // 추가/제거는 모두 지도 클릭 리스너가 처리한다. 폴리곤에 별도 핸들러를
      // 달면 지도 클릭과 함께 발생해 토글이 두 번 일어난다.
      overlaysRef.current.push(parcelPolygon);
      path.forEach((point) => bounds.extend(point));
    });

    hazardMarkers.forEach(({ finding, facility, nearby }) => {
      const position = toMapPosition(
        runtime,
        facility.coordinates.lat,
        facility.coordinates.lng,
      );
      // 참고 시설은 자동 맞춤 범위를 넓히면 안 되므로 bounds 에 넣지 않는다.
      if (!nearby) {
        bounds.extend(position);
      }
      // 1차 유해요소는 색으로 status 를 가르지 않는다. 색만 보고 1차(빨강)를
      // 알아보게 톤을 빨강으로 통일하고, status 구분(!·?·점선 등)은 CSS 의
      // is-${status} 클래스가 채움·글리프로 처리한다. 참고 시설만 회색이다.
      // 기준 밖 시설도 유해시설이다. 회색이 아니라 연한 빨강으로 두어 축소 축척의
      // 핀에서도 빨강 계열로 읽히게 한다.
      const markerColor = nearby ? "#ef4444" : "#dc2626";
      // 같은 규칙에 후보가 여러 개면 누른 것 하나만 강조해야 거리가 구분된다.
      const selected = selectedHazardFacilityId
        ? facility.facility_id === selectedHazardFacilityId
        : finding.finding_id === selectedHazardFindingId;

      // 시설 필지/건물 도형이 있으면 건물 자체를 색으로 구분해 항상 그린다.
      // 1차=빨강(markerColor), 기준 밖=회색. 선택 시설은 더 진하게. 핀보다 낮은
      // zIndex 로 깔고 클릭은 그 시설 선택으로 잇는다. overlaysRef 에 넣어 재렌더
      // 시 함께 정리돼 누수가 없다. 판정창 안 시설만이라 개수는 많지 않다.
      // 시설 도형이 없으면(점 좌표만) 화면에 깔린 지적도 타일에서 그 점이 든
      // 필지를 찾아 영역으로 칠한다. 유해시설은 핀이 아니라 빨간 영역으로 읽혀야
      // 한다. 타일이 없는 축척에서는 핀으로 물러선다.
      const ownRing = facility.geometry ?? [];
      const ring =
        ownRing.length >= 4
          ? ownRing
          : (parcelContaining(facility.coordinates, parcelPool)?.geometry ??
            []);
      const hasArea = ring.length >= 4;
      let facilityHoverRing: Array<{ lat: number; lng: number }> | null = null;
      if (hasArea) {
        const ringPath = ring.map((point) =>
          toMapPosition(runtime, point.lat, point.lng),
        );
        const facilityPolygon = new runtime.sdk.maps.Polygon({
          map,
          ...(runtime.provider === "kakao"
            ? { path: ringPath }
            : { paths: ringPath }),
          strokeWeight: selected ? 2.5 : 1.5,
          strokeColor: "#dc2626",
          strokeOpacity: nearby ? 0.55 : 0.9,
          strokeStyle: nearby ? "shortdash" : "solid",
          fillColor: "#dc2626",
          fillOpacity: nearby ? (selected ? 0.18 : 0.09) : selected ? 0.3 : 0.18,
          clickable: true,
          zIndex: nearby ? 1 : 2,
        });
        addOverlayClick(runtime, facilityPolygon, () => {
          onSelectHazardFinding?.(finding.finding_id);
          onSelectHazardFacility?.(facility.facility_id);
        });
        overlaysRef.current.push(facilityPolygon);
        facilityHoverRing = ring;
        facilityRingsRef.current.push(ring);
      }

      const distanceText = `${Math.round(
        facility.distance_m,
      ).toLocaleString()}m`;
      // 참고 시설은 "기준 밖" 을 덧붙여 판정 시설과 한눈에 구분되게 한다.
      // 석유대체연료 판매업은 용도지역을 짧게 덧붙인다 (LH 확정 2026-09-11 #5).
      const zoningText = facility.zoning_name
        ? ` · ${facility.zoning_name}`
        : "";
      // 이 시설이 어떤 항목에서 어떤 결과를 냈는지 한 줄로. 기준거리 밖 시설은
      // 판정에 들어가지 않아 「통과」다. 판정창 안 시설은 항목의 판정 상태를 쓴다.
      const thresholdText =
        finding.threshold_m !== null ? `기준 ${finding.threshold_m}m` : "";
      const verdictText = nearby
        ? "기준거리 밖 → 통과"
        : finding.status === "exclusion_match"
          ? "저촉 → 매입제외"
          : finding.status === "review_required"
            ? "검토 필요"
            : finding.status_label;
      const metaText = [finding.label, thresholdText, distanceText, verdictText]
        .filter(Boolean)
        .join(" · ") + zoningText;

      // 이미지 마커 + 브라우저 기본 title 이었다. 호버하면 OS 툴팁이 조그맣게
      // 떠서 무슨 시설인지 읽기 어려웠다. HTML 오버레이로 바꿔 이름·항목·거리를
      // 한 칩에 담고, 평소에는 접어 둔다.
      const markerNode = document.createElement("button");
      markerNode.type = "button";
      markerNode.className = `hazard-map-marker is-${finding.status}${
        nearby ? " is-nearby" : ""
      }${hasArea ? " is-area" : ""}${selected ? " is-selected" : ""}`;
      markerNode.style.setProperty("--hazard-tone", markerColor);
      markerNode.setAttribute(
        "aria-label",
        `${finding.label} · ${facility.name} · ${distanceText} · ${verdictText}`,
      );

      const pinNode = document.createElement("span");
      pinNode.className = "hazard-map-pin";
      pinNode.setAttribute("aria-hidden", "true");
      markerNode.appendChild(pinNode);

      const cardNode = document.createElement("span");
      cardNode.className = "hazard-map-card";
      const nameNode = document.createElement("b");
      nameNode.textContent = facility.name || "이름 미확보";
      const metaNode = document.createElement("small");
      metaNode.textContent = metaText;
      cardNode.append(nameNode, metaNode);
      markerNode.appendChild(cardNode);

      const marker = createHtmlOverlay(runtime, map, position, markerNode, {
        yAnchor: 1,
        // 참고 시설은 판정 핀보다 낮게 깔아 판단을 방해하지 않게 한다.
        zIndex: selected ? 9 : nearby ? 4 : 6,
      });
      // 영역으로 그린 시설은 핀이 없어 호버할 곳이 없다. 영역 호버가 이름표를 세운다.
      if (facilityHoverRing) {
        hoverAreasRef.current.push({
          ring: facilityHoverRing,
          node: markerNode,
          overlay: marker,
          baseZIndex: selected ? 9 : nearby ? 4 : 6,
        });
      }
      markerNode.addEventListener("click", (event) => {
        event.stopPropagation();
        onSelectHazardFinding?.(finding.finding_id);
        onSelectHazardFacility?.(facility.facility_id);
      });
      overlaysRef.current.push(marker);
    });

    const selectedHazardFacility =
      hazardMarkers.find(
        ({ facility }) => facility.facility_id === selectedHazardFacilityId,
      )?.facility ?? selectedHazardFinding?.facilities[0];
    if (selectedHazardFacility) {
      const facilityPosition = toMapPosition(
        runtime,
        selectedHazardFacility.coordinates.lat,
        selectedHazardFacility.coordinates.lng,
      );
      // 선은 반드시 거리를 만든 지점에서 출발해야 한다. 중심점에서 그으면
      // 그림이 재는 것과 표시되는 숫자가 달라진다.
      const anchor = selectedHazardFacility.nearest_boundary_point;
      const originPosition = anchor
        ? toMapPosition(runtime, anchor.lat, anchor.lng)
        : center;
      // 시설 필지 폴리곤은 위 hazardMarkers 루프에서 시설마다 이미 그린다(선택
      // 시설은 더 진하게). 여기서는 라벨 문구("대지경계간")에만 도형 유무를 쓴다.
      const facilityRing = selectedHazardFacility.geometry ?? [];
      const facilityEnd = selectedHazardFacility.nearest_facility_point;
      const endPosition = facilityEnd
        ? toMapPosition(runtime, facilityEnd.lat, facilityEnd.lng)
        : facilityPosition;
      const distanceLine = new runtime.sdk.maps.Polyline({
        map,
        path: [originPosition, endPosition],
        strokeWeight: 3,
        strokeColor: "#ef4444",
        strokeOpacity: 0.95,
        strokeStyle: "shortdash",
      });
      overlaysRef.current.push(distanceLine);
      const labelNode = document.createElement("span");
      labelNode.className = "hazard-distance-label";
      labelNode.textContent = `${
        facilityRing.length >= 4
          ? "대지경계간"
          : anchor
            ? "대지경계"
            : "예비"
      }거리 ${Math.round(
        selectedHazardFacility.distance_m,
      ).toLocaleString()}m`;
      const originLat = anchor ? anchor.lat : site.coordinates.lat;
      const originLng = anchor ? anchor.lng : site.coordinates.lng;
      const endLat = facilityEnd
        ? facilityEnd.lat
        : selectedHazardFacility.coordinates.lat;
      const endLng = facilityEnd
        ? facilityEnd.lng
        : selectedHazardFacility.coordinates.lng;
      const midpoint = toMapPosition(
        runtime,
        (originLat + endLat) / 2,
        (originLng + endLng) / 2,
      );
      const distanceLabel = createHtmlOverlay(
        runtime,
        map,
        midpoint,
        labelNode,
        { yAnchor: 1.3, zIndex: 10 },
      );
      overlaysRef.current.push(distanceLabel);
    }

    // ── 2차 배점 근거 시설(교통·주거·교육·가점) ────────────────────────
    // 각 핀에 사업지 대지경계 최단점 ↔ 시설 기준점 최단거리선 + 거리 라벨을 얹는다.
    // 1차 유해요소 선과 형태는 같되(점선) 색만 파랑으로 달리해 판정 거리와 구분한다.
    visibleScreeningHitRefs.forEach((ref) => {
      const hit = ref.hit;
      if (!hit.coordinates) return;
      // 점으로 재는 시설(버스정류장·역 출입구·대학 정문 좌표)은 측정에 쓴 점에
      // 마커를 세운다 — 시설 대표점이 아니라 실제로 거리를 잰 자리가 보여야 한다.
      const pointBased =
        hit.measurement_tier === "coordinate" ||
        hit.measurement_tier === "front_door_point";
      const anchor =
        (pointBased && hit.nearest_facility_point) || hit.coordinates;
      const hitPosition = toMapPosition(runtime, anchor.lat, anchor.lng);
      if (ref.isCriterionNearest) bounds.extend(hitPosition);

      const selected = hit.name === selectedScreeningHitName;
      const distanceText = `${Math.round(hit.distance_m).toLocaleString()}m`;
      const hasNotice = Boolean(hit.front_door_notice);

      // 2차 근거 시설도 핀이 아니라 파란 영역으로. 지적도 타일에서 점이 든 필지를
      // 찾아 칠하고, 타일이 없는 축척에서는 핀으로 물러선다. 점으로 재는 시설
      // (버스정류장·역 출입구 등 measurement_tier=coordinate)은 필지를 칠하지
      // 않는다 — 정류장이 놓인 도로 필지 전체가 파랗게 칠해지면 오해를 낳는다.
      // 백엔드가 거리를 잰 필지 링을 주면 그것만 칠한다(지도에서 필지를 다시 찾지
      // 않는다 — 좌표가 도로 필지에 떨어져 블록 전체가 칠해지던 문제). 링이 없으면
      // 점 마커다.
      const measuredRing = hit.facility_ring ?? [];
      const hitParcel =
        !pointBased && measuredRing.length >= 4
          ? { geometry: measuredRing }
          : null;
      const hitHasArea = Boolean(hitParcel);
      let hitHoverRing: Array<{ lat: number; lng: number }> | null = null;
      if (hitParcel) {
        const hitPath = hitParcel.geometry.map((point) =>
          toMapPosition(runtime, point.lat, point.lng),
        );
        const hitPolygon = new runtime.sdk.maps.Polygon({
          map,
          ...(runtime.provider === "kakao" ? { path: hitPath } : { paths: hitPath }),
          strokeWeight: selected ? 2.5 : 1.5,
          strokeColor: "#2563eb",
          strokeOpacity: 0.9,
          strokeStyle: "solid",
          fillColor: "#2563eb",
          fillOpacity: selected ? 0.3 : 0.18,
          clickable: true,
          zIndex: 2,
        });
        addOverlayClick(runtime, hitPolygon, () => {
          onSelectScreeningHit?.(selected ? null : hit.name);
        });
        overlaysRef.current.push(hitPolygon);
        hitHoverRing = hitParcel.geometry;
        facilityRingsRef.current.push(hitParcel.geometry);
      }
      const metaText = `${ref.groupLabel} · ${distanceText} · ${measurementShortLabel(
        hit,
      )}${hasNotice ? " · ⚠" : ""}`;

      const markerNode = document.createElement("button");
      markerNode.type = "button";
      markerNode.className = `hazard-map-marker is-screening${
        hitHasArea ? " is-area" : ""
      }${selected ? " is-selected" : ""}`;
      markerNode.setAttribute(
        "aria-label",
        `${ref.criterionLabel} · ${hit.name || "이름 미확보"} · ${distanceText}`,
      );
      const pinNode = document.createElement("span");
      pinNode.className = "hazard-map-pin";
      pinNode.setAttribute("aria-hidden", "true");
      markerNode.appendChild(pinNode);
      const cardNode = document.createElement("span");
      cardNode.className = "hazard-map-card";
      const nameNode = document.createElement("b");
      nameNode.textContent = hit.name || "이름 미확보";
      const metaNode = document.createElement("small");
      metaNode.textContent = metaText;
      cardNode.append(nameNode, metaNode);
      markerNode.appendChild(cardNode);
      const screeningMarker = createHtmlOverlay(
        runtime,
        map,
        hitPosition,
        markerNode,
        { yAnchor: 1, zIndex: selected ? 9 : 6 },
      );
      markerNode.addEventListener("click", (event) => {
        event.stopPropagation();
        onSelectScreeningHit?.(selected ? null : hit.name);
      });
      overlaysRef.current.push(screeningMarker);
      if (hitHoverRing) {
        hoverAreasRef.current.push({
          ring: hitHoverRing,
          node: markerNode,
          overlay: screeningMarker,
          baseZIndex: selected ? 9 : 6,
        });
      }

      // 최단거리선 + 거리 라벨. nearest_boundary_point 가 없으면 사업지 중심에서 긋는다.
      const origin = hit.nearest_boundary_point;
      const originPosition = origin
        ? toMapPosition(runtime, origin.lat, origin.lng)
        : center;
      const end = hit.nearest_facility_point;
      const endPosition = end
        ? toMapPosition(runtime, end.lat, end.lng)
        : hitPosition;
      const line = new runtime.sdk.maps.Polyline({
        map,
        path: [originPosition, endPosition],
        strokeWeight: selected ? 3 : 2,
        strokeColor: "#2563eb",
        strokeOpacity: selected ? 0.95 : 0.7,
        strokeStyle: "shortdash",
      });
      overlaysRef.current.push(line);
      const lineLabel = document.createElement("span");
      lineLabel.className = "hazard-distance-label is-amenity";
      lineLabel.textContent = distanceText;
      const originLat = origin ? origin.lat : site.coordinates.lat;
      const originLng = origin ? origin.lng : site.coordinates.lng;
      const endLat = end ? end.lat : hit.coordinates.lat;
      const endLng = end ? end.lng : hit.coordinates.lng;
      const labelOverlay = createHtmlOverlay(
        runtime,
        map,
        toMapPosition(runtime, (originLat + endLat) / 2, (originLng + endLng) / 2),
        lineLabel,
        { yAnchor: 1.3, zIndex: selected ? 10 : 7 },
      );
      overlaysRef.current.push(labelOverlay);
    });

    // 후보 문·출구 핀(#7·#11). 선택된 시설에 후보가 2개 이상일 때만 펼친다.
    if (candidateHitRef?.hit.coordinates) {
      const facility = candidateHitRef.hit.name;
      (candidateHitRef.hit.front_door_candidates ?? []).forEach((candidate) => {
        const doorPosition = toMapPosition(
          runtime,
          candidate.coordinates.lat,
          candidate.coordinates.lng,
        );
        const doorNode = document.createElement("button");
        doorNode.type = "button";
        doorNode.className = `screening-door-pin${
          candidate.selected ? " is-selected" : ""
        }`;
        doorNode.textContent = candidate.label || "문";
        doorNode.title = `${candidate.label} · ${Math.round(
          candidate.distance_m,
        ).toLocaleString()}m · 이 문을 기준점으로`;
        doorNode.setAttribute(
          "aria-label",
          `${facility} ${candidate.label} 기준점으로 지정`,
        );
        doorNode.addEventListener("click", (event) => {
          event.stopPropagation();
          onSelectCandidate?.(facility, candidate);
        });
        const doorOverlay = createHtmlOverlay(runtime, map, doorPosition, doorNode, {
          yAnchor: 1.2,
          zIndex: 11,
        });
        overlaysRef.current.push(doorOverlay);
      });
    }

    if (shouldUpdateViewport) {
      fineZoomTargetRef.current = null;
      if (
        // 참고 시설은 bounds 에 넣지 않으므로 판정 시설만 세어 0건이면 반경 기준 고정 레벨을 유지한다.
        hazardMarkers.some((marker) => !marker.nearby) ||
        // 2차 근거 시설(평가항목별 최근접)도 화면에 들어와야 거리를 눈으로 검증한다.
        visibleScreeningHitRefs.some((ref) => ref.isCriterionNearest)
      ) {
        // 근거 시설이 한쪽으로 몰려도 사업지가 화면 중앙에 오도록 사업지 기준
        // 대칭 박스로 fit 한다. 읽기에 실패하면(SDK 차이) 원래 bounds 그대로.
        const box = readBoundsBox(runtime, bounds);
        const fitBounds = box
          ? boundsFromBox(
              runtime,
              symmetricBoxAroundSite(box, site.coordinates),
            )
          : bounds;
        fitMapBounds(runtime, map, fitBounds, viewportPadding(hazardMode));
        const fittedLevel = getNormalizedLevel(runtime, map);
        fineZoomLevelRef.current = fittedLevel;
        setMapLevel(fittedLevel);
      } else if (
        !hazardReview &&
        hazardParcels.some((parcel) => parcel.geometry.length >= 4)
      ) {
        // 분석 전 + 필지 경계 확보: 고정 미리보기 레벨은 큰 필지(합필·산지)를 화면
        // 밖으로 잘랐다(F5). 필지 경계 기준으로 fit 하되, 작은 필지에서 미리보기
        // 레벨보다 더 파고들지는 않는다(사업지 주변 맥락이 보여야 한다).
        const box = readBoundsBox(runtime, bounds);
        const fitBounds = box
          ? boundsFromBox(
              runtime,
              symmetricBoxAroundSite(box, site.coordinates),
            )
          : bounds;
        fitMapBounds(runtime, map, fitBounds, viewportPadding(hazardMode));
        const level = Math.max(
          getNormalizedLevel(runtime, map),
          SITE_PREVIEW_LEVEL,
        );
        fineZoomTargetRef.current = level;
        fineZoomLevelRef.current = level;
        setMapLevel(level);
        setNormalizedLevel(runtime, map, level);
      } else {
        // 분석 전에는 반경 원이 아니라 사업지를 봐야 한다.
        const level = !hazardReview ? SITE_PREVIEW_LEVEL : 5;
        fineZoomTargetRef.current = level;
        fineZoomLevelRef.current = level;
        setMapLevel(level);
        setNormalizedLevel(runtime, map, level);
      }
      viewportKeyRef.current = viewportKey;
    }
    // recenter 요청(검색 재클릭·자동 필지 로드 성공)은 fit 여부와 무관하게 소비한다.
    viewportRequestRef.current = viewportRequest;
  }, [
    hazardMarkers,
    hazardMode,
    hazardParcels,
    tileRevision,
    onToggleParcelAt,
    hazardReview?.review_id,
    hazardReview?.rule_bands,
    mapInstanceRevision,
    mapProvider,
    mapReady,
    onSelectHazardFinding,
    onSelectHazardFacility,
    selectedHazardFacilityId,
    selectedProviderConfigured,
    selectedHazardFinding,
    selectedHazardFindingId,
    site,
    visibleScreeningHitRefs,
    candidateHitRef,
    selectedScreeningHitName,
    onSelectScreeningHit,
    onSelectCandidate,
    viewportRequest,
  ]);

  useEffect(
    () => () => {
      overlaysRef.current.forEach((overlay) => overlay.setMap?.(null));
    },
    [],
  );

  return (
    <section
      className={`map-panel ${hazardMode ? "is-hazard-review" : ""}`}
      aria-label="심사 지도"
    >
      <div className="map-stage">
        <div
          key={`${mapProvider}:${naverMapStyle}`}
          ref={containerRef}
          className="kakao-map-canvas map-provider-canvas"
          data-provider={mapProvider}
          data-map-style={mapProvider === "naver" ? naverMapStyle : "normal"}
          style={
            {
              "--map-fine-zoom-scale": fineZoomScale,
            } as CSSProperties
          }
        />

        {designationTarget && (
          <div className="designation-banner" role="status">
            <PencilRuler size={16} aria-hidden="true" />
            <span>
              지도에서 <b>{designationTarget}</b>의 기준 필지 또는 문 위치를
              누르세요
              <small>Esc 또는 취소로 빠져나갑니다</small>
            </span>
            <button type="button" onClick={() => onCancelDesignation?.()}>
              <X size={15} />
              취소
            </button>
          </div>
        )}

        {hazardMode && (
          <div className="hazard-map-legend">
            {/* 읽는 순서대로: 1차(빨강) → 2차(파랑) → 기준 밖(회색) → 거리 밴드. */}
            <strong>지도 범례</strong>
            {hazardMarkers.some((marker) => !marker.nearby) && (
              <span>
                <i className="marker-primary" /> 1차 유해시설
              </span>
            )}
            {screeningHitRefs.length > 0 && (
              <span>
                <i className="marker-screening" /> 2차 근거 시설
              </span>
            )}
            {hazardMarkers.some((marker) => marker.nearby) && (
              <span>
                <i className="marker-nearby" /> 기준거리 밖 (통과)
              </span>
            )}
            <span>
              <i className="buffer-25" /> 25m
            </span>
            <span>
              <i className="buffer-50" /> 50m
            </span>
            <span>
              <i className="buffer-500" /> 500m
            </span>
            {candidateHitRef && (
              <span>
                <i className="marker-door" /> 문·출구 후보
              </span>
            )}
            {zoningLayerOn && (
              <span>
                <i className="marker-zoning" /> 용도지역 레이어 켜짐
              </span>
            )}
            {/* 지적편집도 타일 로딩·게이트·3km·일부표시 안내. 같은 범례 자리에 둔다. */}
            {cadastralNote && (
              <small className="cadastral-note">{cadastralNote}</small>
            )}
            <small>점 마커는 공식 경계가 아닌 시설 후보입니다.</small>
          </div>
        )}


        <div className="map-zoom-control" aria-label="지도 축척 조절">
          <button
            type="button"
            aria-label="지도 확대"
            title="지도 확대"
            disabled={mapLevel <= ZOOM_MIN_LEVEL}
            onClick={() => changeZoom("in")}
          >
            <Plus size={18} />
          </button>
          <span
            title={
              mapLevel === ZOOM_MIN_LEVEL
                ? "최대 확대"
                : "0.5레벨 단위이며 숫자가 작을수록 확대됩니다."
            }
          >
            <small>LEVEL</small>
            <strong>
              L{Number.isInteger(mapLevel) ? mapLevel : mapLevel.toFixed(1)}
            </strong>
          </span>
          <button
            type="button"
            aria-label="지도 축소"
            title="지도 축소"
            disabled={mapLevel >= ZOOM_MAX_LEVEL}
            onClick={() => changeZoom("out")}
          >
            <Minus size={18} />
          </button>
        </div>

        {hazardMode && (
          <div
            className="map-zoning-control"
            style={{
              transform: `translate(${switchOffset.x}px, ${switchOffset.y}px)`,
            }}
          >
            <button
              type="button"
              className="map-switch-handle"
              aria-label="레이어 스위치 패널 옮기기"
              title="끌어서 옮기기"
              onPointerDown={startSwitchDrag}
            >
              <GripVertical size={14} aria-hidden="true" />
              <span>레이어</span>
            </button>
            <button
              type="button"
              className={`map-zoning-toggle${cadastralAutoOn ? " is-on" : ""}`}
              role="switch"
              aria-checked={cadastralAutoOn}
              title="지적도(필지 경계) 레이어 켜기/끄기 — 받아 둔 필지를 보이거나 숨긴다"
              onClick={() => {
                const next = !cadastralAutoOn;
                setCadastralAutoOn(next);
                cadastralAutoRef.current = next;
                if (next) cadastralRefreshRef.current();
                else setCadastralParcels([]);
              }}
            >
              <Layers size={16} aria-hidden="true" />
              <span>지적도</span>
              <em>{cadastralAutoOn ? "ON" : "OFF"}</em>
            </button>
            <button
              type="button"
              className={`map-zoning-toggle${zoningLayerOn ? " is-on" : ""}`}
              role="switch"
              aria-checked={zoningLayerOn}
              disabled={!zoningLayerSupported}
              title={
                zoningLayerSupported
                  ? "지적편집도 용도지역 레이어 켜기/끄기"
                  : mapProvider === "naver"
                    ? "카카오 지도에서만 제공됩니다"
                    : "지도 키를 설정하면 사용할 수 있습니다"
              }
              onClick={toggleZoningLayer}
            >
              <Layers size={16} aria-hidden="true" />
              <span>용도지역</span>
              <em>{zoningLayerOn ? "ON" : "OFF"}</em>
            </button>
          </div>
        )}

        {!selectedProviderConfigured && (
          <div className="map-fallback">
            <div className="map-fallback-grid" aria-hidden="true" />
            <div className="fallback-card">
              <span className="fallback-icon">
                <MapPinned size={28} />
              </span>
              <span className="eyebrow">MAP CONNECTION</span>
              <h3>
                {mapProvider === "kakao"
                  ? "카카오 지도 키 연결 대기"
                  : "네이버 지도 클라이언트 ID 연결 대기"}
              </h3>
              <p>
                <code>
                  {mapProvider === "kakao"
                    ? "VITE_KAKAO_JAVASCRIPT_KEY"
                    : "VITE_NAVER_MAP_CLIENT_ID"}
                </code>
                를 설정하면 실제 지도와 심사 근거가 표시됩니다.
              </p>
            </div>
          </div>
        )}

        {mapError && (
          <div className="map-error">
            <AlertCircle size={20} />
            {mapError}
          </div>
        )}

      </div>
    </section>
  );
}
