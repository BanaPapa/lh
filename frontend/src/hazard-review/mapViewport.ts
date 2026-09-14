/**
 * 지도 뷰포트 계산의 순수 함수 모음. SDK 객체를 만지지 않으므로 node 로 바로
 * 검증할 수 있다(cadastralTiles.ts 와 같은 원칙).
 *
 * 배경(2026-09-14 브라우저 검수): 심사 후 fit 은 2차 근거 시설까지 bounds 에 넣는데
 * 시설이 한쪽(예: 북동 2km)에 몰리면 사업지가 bounds 모서리에 놓였다. 게다가 결과
 * 드로어가 지도 우측을 덮어 SDK 의 "지도 중심"(컨테이너 중심)과 사용자가 보는
 * 영역의 중심이 다르므로, +/- 버튼으로 확대할수록 사업지가 왼쪽 가장자리로 밀려
 * 화면 밖으로 나갔다. 두 함수가 각각 그 원인을 하나씩 없앤다.
 */

import type { LatLng, LatLngBox } from "./cadastralTiles";

export interface ViewportPadding {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

export interface PixelSize {
  width: number;
  height: number;
}

export interface PixelPoint {
  x: number;
  y: number;
}

/** 결과 드로어가 지도 우측을 덮으므로 심사 모드에서는 오른쪽 여백을 크게 둔다. */
export function viewportPadding(hazardMode: boolean): ViewportPadding {
  return { top: 90, right: hazardMode ? 500 : 60, bottom: 70, left: 60 };
}

/**
 * 사업지가 정중앙에 오도록 박스를 사업지 기준 대칭으로 넓힌다. 결과는 원래 박스를
 * 항상 포함하므로 시설이 잘리는 일은 없고, 한쪽으로 몰린 만큼 반대쪽이 비어 보인다.
 */
export function symmetricBoxAroundSite(box: LatLngBox, site: LatLng): LatLngBox {
  const dLat = Math.max(
    Math.abs(box.north - site.lat),
    Math.abs(site.lat - box.south),
  );
  const dLng = Math.max(
    Math.abs(box.east - site.lng),
    Math.abs(site.lng - box.west),
  );
  return {
    south: site.lat - dLat,
    north: site.lat + dLat,
    west: site.lng - dLng,
    east: site.lng + dLng,
  };
}

/**
 * 여백(드로어·툴바)을 뺀 실제 보이는 영역의 중심 픽셀. 줌 앵커로 쓴다.
 * 컨테이너가 여백보다 작은 좁은 화면에서는 컨테이너 중심으로 되돌린다.
 */
export function visibleCenterPoint(
  size: PixelSize,
  padding: ViewportPadding,
): PixelPoint {
  const innerWidth = size.width - padding.left - padding.right;
  const innerHeight = size.height - padding.top - padding.bottom;
  if (innerWidth <= 0 || innerHeight <= 0) {
    return { x: size.width / 2, y: size.height / 2 };
  }
  return {
    x: padding.left + innerWidth / 2,
    y: padding.top + innerHeight / 2,
  };
}
