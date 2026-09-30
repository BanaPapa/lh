/**
 * 지적편집도를 사업지 반경 3km 안에서 "타일"로 나눠 받기 위한 순수 계산 모듈.
 *
 * 왜 순수 함수로 뺐나: 지도 SDK 키가 없는 환경에서도 타일 분할·3km 필터·뷰포트
 * 교차 계산을 `node` 로 바로 검증할 수 있어야 한다. React·SDK 의존이 없으므로
 * 렌더 없이 로직만 시험할 수 있다.
 */

export interface LatLngBox {
  south: number;
  west: number;
  north: number;
  east: number;
}

export interface LatLng {
  lat: number;
  lng: number;
}

export interface CadastralTile {
  key: string;
  row: number;
  col: number;
  /** 분할 깊이. 0=0.004°, 1=0.002°, 2=0.001°. truncated 타일을 쿼드 분할한다. */
  depth: number;
  box: LatLngBox;
}

/** 사업지 중심에서 지적도를 받아올 최대 반경(m). 이 밖은 조회하지 않는다. */
export const CADASTRAL_MAX_RADIUS_M = 3000;

/**
 * 타일 한 칸의 위·경도 폭(deg). 위·경도 모두 0.004°(약 440m×360m)로 둔다.
 * 0.008° 칸은 전주 도심에서 응답 상한(800필지)에 걸려 2.1초 걸린 뒤 다시 4칸으로 쪼개
 * 받았다. 0.004° 는 0.8초에 잘리지 않고 온다(2026-09-30 실측). 백엔드
 * parcels/in-bounds 의 요청당 상한(0.01°)보다 작다. 3km·뷰포트↔타일 거리
 * 계산에서만 경도를 위도 보정한다.
 */
export const TILE_SPAN_DEG = 0.004;

export const METERS_PER_DEGREE_LAT = 111_320;

/** 동시에 화면에 그릴 필지 폴리곤 상한. 넘으면 뷰포트 중심에서 먼 타일부터 뺀다. */
export const MAX_CADASTRAL_POLYGONS = 2500;

/** 필지 경계가 읽히는 정규화 레벨 상한. 이보다 축소되면 조회·표시하지 않는다. */
/** 4 에서는 한 화면에 필지가 너무 많아 그리기가 느렸다(2026-09-30). 한 단계 더 확대해야 그린다. */
export const CADASTRAL_MAX_LEVEL = 3;

/**
 * 타일 분할 최대 깊이. truncated(응답 상한 초과) 타일을 폭 절반의 하위 4타일로
 * 쿼드 분할해 재요청한다. 깊이 2면 0.004°→0.002°→0.001°(약 110m)까지 좁혀,
 * 도심에서도 나머지 필지를 확대해 볼 수 있다. 깊이 2에서도 잘리면 그때만
 * "일부만 표시" 로 남긴다.
 */
export const MAX_TILE_DEPTH = 2;

/** 분할 깊이별 타일 한 칸 폭(deg). 깊이마다 절반으로 줄어든다. */
export function spanForDepth(depth: number): number {
  return TILE_SPAN_DEG / 2 ** depth;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

/** 위·경도 두 점 사이 거리(m). 3km 규모에선 등거리 근사로 충분하다. */
export function metersBetween(a: LatLng, b: LatLng): number {
  const latM = (a.lat - b.lat) * METERS_PER_DEGREE_LAT;
  const lngM =
    (a.lng - b.lng) *
    METERS_PER_DEGREE_LAT *
    Math.cos(((a.lat + b.lat) / 2) * (Math.PI / 180));
  return Math.hypot(latM, lngM);
}

/** 점에서 박스까지의 최단거리(m). 점이 박스 안이면 0. */
export function nearestDistanceToBoxM(point: LatLng, box: LatLngBox): number {
  const nearest: LatLng = {
    lat: clamp(point.lat, box.south, box.north),
    lng: clamp(point.lng, box.west, box.east),
  };
  return metersBetween(point, nearest);
}

export function tileRow(lat: number, depth = 0): number {
  return Math.floor(lat / spanForDepth(depth));
}

export function tileCol(lng: number, depth = 0): number {
  return Math.floor(lng / spanForDepth(depth));
}

/** 깊이까지 키에 넣어야 깊이가 다른 타일이 같은 키로 충돌하지 않는다. */
export function tileKey(row: number, col: number, depth = 0): string {
  return `${depth}:${row}:${col}`;
}

/** 각 깊이의 격자도 절대 0,0 에 정렬돼 하위 타일이 부모와 정확히 맞물린다. */
export function tileBox(row: number, col: number, depth = 0): LatLngBox {
  const span = spanForDepth(depth);
  return {
    south: row * span,
    north: (row + 1) * span,
    west: col * span,
    east: (col + 1) * span,
  };
}

export function makeTile(row: number, col: number, depth = 0): CadastralTile {
  return {
    key: tileKey(row, col, depth),
    row,
    col,
    depth,
    box: tileBox(row, col, depth),
  };
}

/**
 * truncated 타일을 폭 절반의 하위 4타일로 나눈다. 각 깊이 격자가 0,0 정렬이라
 * 부모 (row,col) 은 하위 깊이에서 (2r,2c)~(2r+1,2c+1) 4칸과 정확히 겹친다.
 * 최대 깊이를 넘으면 더 나누지 않는다(빈 배열).
 */
export function subdivideTile(tile: CadastralTile): CadastralTile[] {
  if (tile.depth >= MAX_TILE_DEPTH) return [];
  const depth = tile.depth + 1;
  const r = tile.row * 2;
  const c = tile.col * 2;
  return [
    makeTile(r, c, depth),
    makeTile(r, c + 1, depth),
    makeTile(r + 1, c, depth),
    makeTile(r + 1, c + 1, depth),
  ];
}

/** 두 박스가 (경계 접촉 이상으로) 겹치는지. */
export function boxesOverlap(a: LatLngBox, b: LatLngBox): boolean {
  return (
    a.north > b.south && a.south < b.north && a.east > b.west && a.west < b.east
  );
}

export function tileCenter(tile: CadastralTile): LatLng {
  return {
    lat: (tile.box.south + tile.box.north) / 2,
    lng: (tile.box.west + tile.box.east) / 2,
  };
}

/** 뷰포트 박스와 겹치는 타일 전부. */
export function tilesForViewport(box: LatLngBox): CadastralTile[] {
  const rowStart = tileRow(box.south);
  const rowEnd = tileRow(box.north);
  const colStart = tileCol(box.west);
  const colEnd = tileCol(box.east);
  const tiles: CadastralTile[] = [];
  for (let row = rowStart; row <= rowEnd; row += 1) {
    for (let col = colStart; col <= colEnd; col += 1) {
      tiles.push(makeTile(row, col));
    }
  }
  return tiles;
}

/** 타일이 사업지 반경 안에 (일부라도) 걸치는지. */
export function tileWithinRadius(
  center: LatLng,
  tile: CadastralTile,
  radiusM: number = CADASTRAL_MAX_RADIUS_M,
): boolean {
  return nearestDistanceToBoxM(center, tile.box) <= radiusM;
}

/** 뷰포트 전체가 사업지 반경 밖인지(어느 구석도 반경에 못 미침). */
export function viewportOutsideRadius(
  center: LatLng,
  box: LatLngBox,
  radiusM: number = CADASTRAL_MAX_RADIUS_M,
): boolean {
  return nearestDistanceToBoxM(center, box) > radiusM;
}

export function boxCenter(box: LatLngBox): LatLng {
  return {
    lat: (box.south + box.north) / 2,
    lng: (box.west + box.east) / 2,
  };
}

/** 점이 링(닫힌 다각형) 안에 있는지. 레이 캐스팅. 경계 위는 안으로 본다. */
export function pointInRing(point: LatLng, ring: LatLng[]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const a = ring[i];
    const b = ring[j];
    const crosses =
      a.lat > point.lat !== b.lat > point.lat &&
      point.lng <
        ((b.lng - a.lng) * (point.lat - a.lat)) / (b.lat - a.lat) + a.lng;
    if (crosses) inside = !inside;
  }
  return inside;
}

/**
 * 시설 경계로 쓰면 안 되는 지목(지번 끝 글자). 백엔드 parcel_sanity.NON_FACILITY_JIMOK 와 같다.
 *   도=도로 · 천=하천 · 구=구거 · 제=제방 · 철=철도용지 · 유=유지 · 광=광천지
 */
export const NON_FACILITY_JIMOK: ReadonlySet<string> = new Set([
  "도",
  "천",
  "구",
  "제",
  "철",
  "유",
  "광",
]);

/** 지번 끝의 지목 글자. 없으면 빈 문자열. */
export function jimokOf(jibun: string): string {
  const last = (jibun ?? "").trim().slice(-1);
  return last >= "가" && last <= "힣" ? last : "";
}

/** 도로·하천 등 건축물이 설 수 없는 지목의 필지인지. 사업지·시설 경계로 쓰지 않는다. */
export function isNonFacilityParcel(parcel: { jibun: string }): boolean {
  return NON_FACILITY_JIMOK.has(jimokOf(parcel.jibun));
}

/**
 * 좌표를 품은 필지 중 가장 작은 것. 필지는 바깥 링만 오므로 블록을 둘러싼 도로
 * 필지(그물 모양)의 링은 그 안 블록의 점도 품는다. 가장 작은 필지를 골라야 점이
 * 실제로 떨어진 필지가 나온다.
 */
export function smallestParcelContaining<T extends { geometry: LatLng[]; area_m2: number }>(
  point: LatLng,
  parcels: readonly T[],
): T | null {
  let best: T | null = null;
  for (const parcel of parcels) {
    if (parcel.geometry.length < 4 || !pointInRing(point, parcel.geometry)) continue;
    if (!best || parcel.area_m2 < best.area_m2) best = parcel;
  }
  return best;
}

/**
 * 좌표를 품은 필지를 지적도 타일 목록에서 찾는다. 시설은 점 좌표만 있고 경계는
 * 없는 경우가 많다(참고 시설·2차 근거 시설). 화면에 이미 깔린 지적도 필지에서
 * 그 점이 든 필지를 찾아 "영역"으로 칠하면 별도 조회 없이 경계를 보여줄 수 있다.
 * 타일이 아직 안 깔린 축척(레벨 5 이상)이나 3km 밖에서는 null 이다.
 *
 * 좌표가 도로·하천 등 필지에 떨어지면 null 이다. 도로 필지는 블록을 둘러싼 그물
 * 모양이라 바깥 링만 칠하면 여러 블록이 통째로 시설 영역처럼 보인다
 * (2026-09-28 전주 금암동 473-6 검수). 백엔드 parcel_sanity 와 같은 기준이다.
 */
export function parcelContaining<
  T extends { geometry: LatLng[]; jibun: string; area_m2: number },
>(point: LatLng, parcels: readonly T[]): T | null {
  const parcel = smallestParcelContaining(point, parcels);
  return parcel && !isNonFacilityParcel(parcel) ? parcel : null;
}
