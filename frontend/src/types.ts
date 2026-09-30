export type MapProvider = "kakao" | "naver";

export interface Coordinates {
  lat: number;
  lng: number;
}

export interface GeocodeCandidate {
  id: string;
  name: string;
  address: string;
  road_address: string;
  coordinates: Coordinates;
  source: "kakao" | "naver" | "vworld" | "demo";
}

export interface GeocodeResponse {
  query: string;
  candidates: GeocodeCandidate[];
  demo: boolean;
  // 카카오가 막혀 대체 원천(네이버·브이월드)으로 찾았을 때의 안내.
  notice?: string;
}

export type ProgressStatus = "pending" | "running" | "completed" | "failed";
