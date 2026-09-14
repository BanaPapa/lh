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
  source: "kakao" | "demo";
}

export interface GeocodeResponse {
  query: string;
  candidates: GeocodeCandidate[];
  demo: boolean;
}

export type ProgressStatus = "pending" | "running" | "completed" | "failed";
