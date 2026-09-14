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
  /** 분할 깊이. 0=0.008°, 1=0.004°, 2=0.002°. truncated 타일을 쿼드 분할한다. */
  depth: number;
  box: LatLngBox;
}

/** 사업지 중심에서 지적도를 받아올 최대 반경(m). 이 밖은 조회하지 않는다. */
export const CADASTRAL_MAX_RADIUS_M = 3000;

/**
 * 타일 한 칸의 위·경도 폭(deg). 위·경도 모두 0.008° 로 둔다.
 * 백엔드 parcels/in-bounds 는 요청당 MAX_BOUNDS_SPAN_DEG(0.01°) 를 넘으면 422 다.
 * 0.008 < 0.01 이라 위·경도 어느 쪽으로도 상한을 넘지 않는다. 경도를 위도
 * 보정(0.008/cos φ)으로 늘리면 서울 위도에서 0.0101° 가 되어 상한을 넘으므로
 * 늘리지 않는다. 대신 3km·뷰포트↔타일 거리 계산에서만 경도를 위도 보정한다.
 */
export const TILE_SPAN_DEG = 0.008;

export const METERS_PER_DEGREE_LAT = 111_320;

/** 동시에 화면에 그릴 필지 폴리곤 상한. 넘으면 뷰포트 중심에서 먼 타일부터 뺀다. */
export const MAX_CADASTRAL_POLYGONS = 2500;

/** 필지 경계가 읽히는 정규화 레벨 상한. 이보다 축소되면 조회·표시하지 않는다. */
export const CADASTRAL_MAX_LEVEL = 4;

/**
 * 타일 분할 최대 깊이. truncated(응답 상한 초과) 타일을 폭 절반의 하위 4타일로
 * 쿼드 분할해 재요청한다. 깊이 2면 0.008°→0.004°→0.002°(약 220m)까지 좁혀,
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
 * 좌표를 품은 필지를 지적도 타일 목록에서 찾는다. 시설은 점 좌표만 있고 경계는
 * 없는 경우가 많다(참고 시설·2차 근거 시설). 화면에 이미 깔린 지적도 필지에서
 * 그 점이 든 필지를 찾아 "영역"으로 칠하면 별도 조회 없이 경계를 보여줄 수 있다.
 * 타일이 아직 안 깔린 축척(레벨 5 이상)이나 3km 밖에서는 null 이다.
 */
export function parcelContaining<T extends { geometry: LatLng[] }>(
  point: LatLng,
  parcels: readonly T[],
): T | null {
  for (const parcel of parcels) {
    if (parcel.geometry.length >= 4 && pointInRing(point, parcel.geometry)) {
      return parcel;
    }
  }
  return null;
}
