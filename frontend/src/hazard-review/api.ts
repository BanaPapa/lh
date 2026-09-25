import type { GeocodeCandidate } from "../types";
import type {
  CadastralParcelsResponse,
  HazardApplicationTypesResponse,
  HazardParcelResolveResponse,
  HazardRulePack,
} from "./types";
import { apiRequest } from "../api-base";

/**
 * 유해요소 판정 자체를 시작하는 작업 API(`/jobs`)는 이 앱에서 부르지 않는다.
 * 판정은 심사(`/api/screening/jobs`)가 같은 엔진을 호출해 결과를 함께 싣는다.
 * 여기 남은 것은 규칙팩·신청유형·필지처럼 심사가 입력으로 쓰는 조회뿐이다.
 */
function request<T>(path: string, init?: RequestInit): Promise<T> {
  return apiRequest<T>(path, {
    ...init,
    connectionErrorMessage:
      "유해시설 검토 서버에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
  });
}

export function getHazardRulePacks(): Promise<HazardRulePack[]> {
  return request<HazardRulePack[]>("/api/hazard-review/rule-packs");
}

/** 주택×신청유형 조합과 조합별 적용 임계거리 매트릭스를 가져온다. */
export function getHazardApplicationTypes(): Promise<HazardApplicationTypesResponse> {
  return request<HazardApplicationTypesResponse>(
    "/api/hazard-review/application-types",
  );
}

/**
 * 사업지 필지를 확보한다. `multiParcelAddress` 를 주면(「363-2, -4, 364-1」·「외 N필지(…)」
 * 같이 여러 지번을 적은 검색어) 그 원문으로 필지 합집합을 푼다(#9).
 */
export function resolveHazardParcel(
  site: GeocodeCandidate,
  multiParcelAddress?: string,
): Promise<HazardParcelResolveResponse> {
  return request<HazardParcelResolveResponse>(
    "/api/hazard-review/parcels/resolve",
    {
      method: "POST",
      body: JSON.stringify({
        name: site.name,
        address: multiParcelAddress || site.road_address || site.address,
        coordinates: site.coordinates,
      }),
    },
  );
}

/** 검색어에 지번이 여럿(쉼표 목록·외 N필지) 들어 있는지. */
export function isMultiParcelQuery(query: string): boolean {
  return /[,，]|외\s*\d+\s*필지/.test(query);
}

/** 지도에서 누른 좌표의 필지를 조회한다. 사업지 다중 선택에 쓴다. */
export function resolveHazardParcelAt(
  lat: number,
  lng: number,
): Promise<HazardParcelResolveResponse> {
  return request<HazardParcelResolveResponse>(
    "/api/hazard-review/parcels/resolve",
    {
      method: "POST",
      body: JSON.stringify({
        name: "선택 필지",
        address: "",
        coordinates: { lat, lng },
      }),
    },
  );
}

/** 지도 화면에 보이는 지적도 필지 경계를 가져온다. */
export function getParcelsInBounds(bounds: {
  south: number;
  west: number;
  north: number;
  east: number;
}): Promise<CadastralParcelsResponse> {
  const query = new URLSearchParams({
    south: String(bounds.south),
    west: String(bounds.west),
    north: String(bounds.north),
    east: String(bounds.east),
  });
  return request<CadastralParcelsResponse>(
    `/api/hazard-review/parcels/in-bounds?${query.toString()}`,
  );
}
