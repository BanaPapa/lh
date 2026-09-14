import { MapPin, PanelRightOpen, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { searchAddress } from "./api";
import { useTheme } from "./theme";
import { MapPanel } from "./components/MapPanel";
import { ResultRail } from "./components/ResultRail";
import { TopSearchBar } from "./components/TopSearchBar";
import {
  getHazardApplicationTypes,
  getHazardRulePacks,
  resolveHazardParcel,
  resolveHazardParcelAt,
} from "./hazard-review/api";
import { ScreeningProgressModal } from "./screening/ScreeningProgressModal";
import type {
  CadastralParcel,
  HazardApplicationType,
  HazardApplicationTypesResponse,
  HazardHousingType,
  HazardParcel,
  HazardRulePack,
} from "./hazard-review/types";
import {
  cancelScreeningJob,
  designateFrontDoor,
  getScreeningJob,
  listFrontDoors,
  removeFrontDoor,
  startScreeningJob,
} from "./screening/api";
import { ScreeningSheet } from "./screening/ScreeningSheet";
import {
  computeDesignationDiff,
  type ScreeningDiff,
} from "./screening/screeningOverlays";
import type {
  FrontDoorCandidate,
  FrontDoorView,
  ScreeningJobStatus,
  ScreeningResult,
} from "./screening/types";
import type { GeocodeCandidate, MapProvider } from "./types";

/** 지도 클릭이 폴리곤과 지도 양쪽에서 잡힐 때 중복 토글을 막는 시간(ms). */
const PARCEL_TOGGLE_GUARD_MS = 400;

/**
 * LH 서류심사 앱.
 * 상단 검색 바 + 전체 지도 + 우측 결과 레일 한 화면으로 구성한다.
 */
function App() {
  const { theme, toggleTheme } = useTheme();
  const [query, setQuery] = useState("");
  // 심사표(상세) 열림 여부.
  const [sheetOpen, setSheetOpen] = useState(false);
  const [railVisible, setRailVisible] = useState(true);
  const [selectedCandidate, setSelectedCandidate] =
    useState<GeocodeCandidate | null>(null);
  const [mapProvider, setMapProvider] = useState<MapProvider>(() =>
    window.localStorage.getItem("lh-screening-map-provider") === "naver"
      ? "naver"
      : "kakao",
  );
  const [searching, setSearching] = useState(false);
  // 지도를 사업지 기준으로 재-fit 하라는 요청 카운터. 두 경우에 증가한다.
  // (1) 검색 재클릭: 후보가 나오면 siteChanged 여부와 무관하게 올려, 스크롤로
  //     벗어난 지도를 검색 지역으로 되돌린다(버그2).
  // (2) 자동 필지 로드 성공: loadSiteParcel 이 도형 있는 필지를 확보하면 올려,
  //     그 필지에 맞춰 처음 화면을 잡는다. 필지 수동 토글로는 올리지 않는다.
  const [viewportRequest, setViewportRequest] = useState(0);
  const [searchError, setSearchError] = useState("");
  const [searchNotice, setSearchNotice] = useState("");
  const [rulePackId, setRulePackId] = useState("");
  const [rulePack, setRulePack] = useState<HazardRulePack | null>(null);
  const [hazardApplicationTypes, setHazardApplicationTypes] =
    useState<HazardApplicationTypesResponse | null>(null);
  // 룰북 §1 신청유형. 매트릭스가 이 두 값으로 임계거리를 정한다.
  const [hazardHousingType, setHazardHousingType] =
    useState<HazardHousingType>("house");
  const [hazardApplicationType, setHazardApplicationType] =
    useState<HazardApplicationType>("general");
  const [hazardParcels, setHazardParcels] = useState<HazardParcel[]>([]);
  // 연속지적도가 「외 N필지」를 다 풀지 못했을 때의 안내. 필지 영역 옆에 그대로 띄운다.
  const [parcelNote, setParcelNote] = useState("");
  const [screeningResult, setScreeningResult] =
    useState<ScreeningResult | null>(null);
  // 수기 지정된 기준점(문·필지) 목록. 심사표 배지·해제가 이 목록을 본다.
  const [frontDoors, setFrontDoors] = useState<FrontDoorView[]>([]);
  // 기준점 지정 모드 대상 시설명. 설정되면 지도가 지정 모드로 들어간다.
  const [designationTarget, setDesignationTarget] = useState<string | null>(
    null,
  );
  const [designationError, setDesignationError] = useState("");
  // 심사표에서 펼친 시설군(지도에 그 군의 hit 전부 표시) / 선택한 2차 시설.
  const [expandedScreeningGroupKey, setExpandedScreeningGroupKey] = useState<
    string | null
  >(null);
  const [selectedScreeningHitName, setSelectedScreeningHitName] = useState<
    string | null
  >(null);
  // 재계산 전후 차이 한 줄. 다음 일반 실행 때 사라진다.
  const [screeningDiff, setScreeningDiff] = useState<ScreeningDiff | null>(null);
  const [screeningProgress, setScreeningProgress] =
    useState<ScreeningJobStatus | null>(null);
  const [screeningRunning, setScreeningRunning] = useState(false);
  const [screeningError, setScreeningError] = useState("");
  const [selectedHazardFindingId, setSelectedHazardFindingId] =
    useState<string | null>(null);
  const [selectedHazardFacilityId, setSelectedHazardFacilityId] =
    useState<string | null>(null);
  // 지도의 유해요소 근거(필지 경계·시설 마커·최단거리선)는 심사 결과에서 온다.
  // 심사 응답에 유해요소 판정이 통째로 들어 있다. 연결하지 않으면 심사표에
  // "주유소 21m"라고 적히는데 지도에는 아무것도 안 나와, 거리를 눈으로 검증할
  // 수단이 사라진다.
  const mapHazardReview = screeningResult?.hazard_review ?? null;

  const screeningRunRef = useRef(0);
  const screeningJobIdRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getHazardRulePacks()
      .then((packs: HazardRulePack[]) => {
        if (cancelled) return;
        const pack = packs[0] ?? null;
        setRulePack(pack);
        setRulePackId((current) => current || pack?.id || "");
      })
      .catch((error) => {
        if (!cancelled) {
          setScreeningError(
            error instanceof Error
              ? error.message
              : "심사 규칙팩을 불러오지 못했습니다.",
          );
        }
      });
    getHazardApplicationTypes()
      .then((response) => {
        if (!cancelled) setHazardApplicationTypes(response);
      })
      .catch(() => {
        // 신청유형 매트릭스를 못 받아도 기본값으로 검토는 가능하다.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  /** 수기 지정 목록을 다시 읽는다. 지정·해제 직후와 첫 진입에 쓴다. */
  const refreshFrontDoors = useCallback(async () => {
    try {
      setFrontDoors(await listFrontDoors());
    } catch {
      // 지정 목록을 못 받아도 심사 자체는 가능하다. 배지만 비워 둔다.
    }
  }, []);

  useEffect(() => {
    void refreshFrontDoors();
  }, [refreshFrontDoors]);

  // 자동 필지 로드 요청 순번. 검색 응답이 늦게 도착한 사이 사용자가 다른 곳을
  // 검색하면 옛 후보의 결과가 현재 사업지와 달라진다. 최신 요청만 반영해 화면이
  // 옛 필지로 튀는 것을 막는다.
  const siteParcelRunRef = useRef(0);

  /** 검색 직후 지적도 필지를 받아 지도에 경계를 그린다. 분석 실행과 무관하다. */
  const loadSiteParcel = async (candidate: GeocodeCandidate) => {
    const runId = siteParcelRunRef.current + 1;
    siteParcelRunRef.current = runId;
    try {
      const resolved = await resolveHazardParcel(candidate);
      // 이미 다른 검색이 이 요청을 밀어냈으면 옛 결과를 버린다.
      if (siteParcelRunRef.current !== runId) return;
      // 대표 필지만 쓰던 것을 응답 필지 전부로 넓힌다(#9 다필지 합집합).
      setHazardParcels(resolved.parcels);
      setParcelNote(resolved.note);
      // 도형 있는 필지를 실제로 확보한 경우에만 그 필지에 맞춰 지도를 처음 잡는다.
      // 실패·0건이면 재-fit 을 요청하지 않는다. 그래야 이후 사용자가 확대·이동한
      // 상태에서 수동으로 필지를 처음 골라도 지도가 검색 지역으로 튀지 않는다.
      const hasParcelGeometry = resolved.parcels.some(
        (parcel) => parcel.geometry.length >= 4,
      );
      if (hasParcelGeometry) {
        setViewportRequest((seq) => seq + 1);
      }
    } catch {
      // 지적도를 못 받아도 검색 자체는 성공이므로 조용히 넘어간다.
      if (siteParcelRunRef.current !== runId) return;
      setHazardParcels([]);
      setParcelNote("");
    }
  };

  /** 같은 클릭이 지도와 폴리곤 양쪽에서 잡혀도 한 번만 반영되게 막는다. */
  const lastToggleRef = useRef<{ pnu: string; at: number }>({ pnu: "", at: 0 });

  // 지정 모드 여부·지정 동작은 아래에서 정의되지만, 지도 클릭 핸들러는 그보다
  // 먼저 선언돼야 한다(순환 참조 방지). 렌더마다 갱신되는 ref 로 잇는다.
  const designationTargetRef = useRef<string | null>(null);
  const designateFacilityRef = useRef<
    (
      facility: string,
      payload: { pnu?: string; lat?: number; lng?: number; source_label?: string },
    ) => void
  >(() => {});
  useEffect(() => {
    designationTargetRef.current = designationTarget;
  }, [designationTarget]);

  const applyParcelToggle = useCallback((parcel: HazardParcel) => {
    const now = Date.now();
    const previous = lastToggleRef.current;
    if (previous.pnu === parcel.pnu && now - previous.at < PARCEL_TOGGLE_GUARD_MS) {
      return;
    }
    lastToggleRef.current = { pnu: parcel.pnu, at: now };
    setHazardParcels((current) => {
      const exists = current.some((item) => item.pnu === parcel.pnu);
      return exists
        ? current.filter((item) => item.pnu !== parcel.pnu)
        : [...current, parcel];
    });
  }, []);

  /** 지도에 그려진 지적도 필지를 눌렀을 때. 도형을 이미 알고 있어 조회가 필요 없다. */
  const handleToggleParcel = useCallback(
    (parcel: CadastralParcel) => {
      // 지정 모드에서는 필지 토글이 아니라 기준점 지정으로 동작한다(모드 배타).
      if (designationTargetRef.current) {
        designateFacilityRef.current(designationTargetRef.current, {
          pnu: parcel.pnu,
          source_label: `지적필지 ${parcel.jibun || parcel.pnu}`,
        });
        return;
      }
      applyParcelToggle({
        parcel_id: `cadastral:${parcel.pnu}`,
        pnu: parcel.pnu,
        address: parcel.address || parcel.jibun,
        area_m2: parcel.area_m2,
        geometry: parcel.geometry,
        geometry_source: "parcel_polygon",
        geometry_note: "",
      });
    },
    [applyParcelToggle],
  );

  /** 지적도가 아직 안 깔린 자리를 눌렀을 때. 좌표로 필지를 조회한다. */
  const handleToggleParcelAt = useCallback(
    async (lat: number, lng: number) => {
      // 지정 모드에서는 빈 곳 클릭을 좌표 기준점 지정으로 받는다.
      if (designationTargetRef.current) {
        designateFacilityRef.current(designationTargetRef.current, {
          lat,
          lng,
          source_label: "지도 클릭 좌표",
        });
        return;
      }
      try {
        const resolved = await resolveHazardParcelAt(lat, lng);
        const parcel = resolved.parcels[0];
        if (!parcel || resolved.provisional) return;
        applyParcelToggle(parcel);
      } catch {
        // 필지를 못 찾으면 아무 것도 하지 않는다.
      }
    },
    [applyParcelToggle],
  );

  const handleSearch = async () => {
    if (query.trim().length < 2) return;
    setSearching(true);
    setSearchError("");
    setSearchNotice("");
    try {
      const response = await searchAddress(query);
      if (response.candidates.length === 0) {
        setSearchError(
          response.demo
            ? "데모 모드입니다. 카카오 API 키가 없어 실제 검색 결과를 가져오지 못했습니다. 설정에서 키를 등록해 주세요."
            : "검색 결과가 없습니다. 주소나 장소명을 바꿔 보세요.",
        );
      } else {
        if (response.demo) {
          setSearchNotice(
            "데모 모드: 실제 검색이 아니라 예시 후보를 보여주고 있습니다. 설정에서 카카오 API 키를 등록하면 실데이터로 전환됩니다.",
          );
        }
        const nextCandidate = response.candidates[0];
        const siteChanged =
          !selectedCandidate ||
          Math.abs(
            selectedCandidate.coordinates.lat -
              nextCandidate.coordinates.lat,
          ) > 0.000001 ||
          Math.abs(
            selectedCandidate.coordinates.lng -
              nextCandidate.coordinates.lng,
          ) > 0.000001;
        setSelectedCandidate(nextCandidate);
        // 같은 후보(siteChanged=false)라도 스크롤로 벗어난 지도를 검색 지역으로
        // 되돌려야 하므로 항상 recenter 요청을 올린다. 분석 결과·필지 등 다른
        // 상태는 아래 siteChanged 분기에서만 지운다.
        setViewportRequest((seq) => seq + 1);
        if (siteChanged) {
          setHazardParcels([]);
          setParcelNote("");
          void loadSiteParcel(nextCandidate);
          setScreeningResult(null);
          setScreeningProgress(null);
          setScreeningError("");
          setRailVisible(true);
          setSelectedHazardFindingId(null);
          setDesignationTarget(null);
          setDesignationError("");
          setScreeningDiff(null);
          setExpandedScreeningGroupKey(null);
          setSelectedScreeningHitName(null);
        }
      }
    } catch (error) {
      setSearchError(
        error instanceof Error ? error.message : "사업지 검색에 실패했습니다.",
      );
    } finally {
      setSearching(false);
    }
  };

  const handleSelectHazardFinding = useCallback((findingId: string | null) => {
    setSelectedHazardFindingId(findingId);
    if (findingId) setRailVisible(true);
  }, []);

  /**
   * LH 서류심사를 실행한다. 유해요소 검토와 같은 사업지·필지를 쓰고,
   * 신청유형은 인자로 덮어쓸 수 있다(심사표에서 유형을 바꾸면 그 값으로
   * 바로 다시 돌려야 하는데 setState 는 이번 렌더에 반영되지 않는다).
   */
  const runScreening = async (overrides?: {
    housingType?: HazardHousingType;
    applicationType?: HazardApplicationType;
    /** 기준점 변경으로 인한 재계산이면, 직전 결과와의 차이를 계산할 시설명. */
    diffFacility?: string;
    /** 차이 비교 기준이 되는 직전 결과(호출 시점에 고정). */
    previousResult?: ScreeningResult | null;
  }) => {
    if (!selectedCandidate) return;
    const housingType = overrides?.housingType ?? hazardHousingType;
    const applicationType = overrides?.applicationType ?? hazardApplicationType;
    const diffFacility = overrides?.diffFacility;
    const previousResult = overrides?.previousResult ?? null;
    const runId = screeningRunRef.current + 1;
    screeningRunRef.current = runId;
    setScreeningRunning(true);
    setScreeningError("");
    setScreeningProgress(null);
    // 일반 실행은 직전 차이 줄을 지운다. 기준점 변경 실행만 새 차이를 남긴다.
    if (!diffFacility) setScreeningDiff(null);
    try {
      // 사용자가 지도에서 고른 필지를 그대로 쓴다. 없으면 자동 확보한다.
      let parcels = hazardParcels;
      if (parcels.length === 0) {
        const resolved = await resolveHazardParcel(selectedCandidate);
        if (screeningRunRef.current !== runId) return;
        parcels = resolved.parcels;
        setHazardParcels(parcels);
        setParcelNote(resolved.note);
      }
      if (parcels.length === 0) {
        throw new Error("신청 필지를 확보하지 못했습니다.");
      }
      const started = await startScreeningJob({
        site: {
          name: selectedCandidate.name,
          address:
            selectedCandidate.road_address || selectedCandidate.address || "",
          coordinates: selectedCandidate.coordinates,
          housing_type: housingType,
          application_type: applicationType,
          parcels,
        },
        rule_pack_id: rulePackId,
        requested_by: "lh-screening",
        // 1차 부적격이어도 2차 배점을 참고용으로 함께 낸다.
        include_stage_two_on_fail: true,
      });
      if (screeningRunRef.current !== runId) return;
      screeningJobIdRef.current = started.job_id;
      let completed: ScreeningJobStatus | null = null;
      for (let attempt = 0; attempt < 400; attempt += 1) {
        const status = await getScreeningJob(started.job_id);
        if (screeningRunRef.current !== runId) return;
        setScreeningProgress(status);
        if (status.status === "completed") {
          completed = status;
          break;
        }
        if (status.status === "cancelled") {
          throw new Error("서류심사가 중단되었습니다.");
        }
        if (status.status === "failed") {
          throw new Error(status.error || status.message);
        }
        await new Promise((resolve) => window.setTimeout(resolve, 250));
      }
      if (!completed?.result) {
        throw new Error("서류심사 시간이 초과되었습니다. 다시 시도해 주세요.");
      }
      setScreeningResult(completed.result);
      if (diffFacility) {
        setScreeningDiff(
          computeDesignationDiff(previousResult, completed.result, diffFacility),
        );
      }
    } catch (error) {
      if (screeningRunRef.current !== runId) return;
      setScreeningResult(null);
      setScreeningError(
        error instanceof Error ? error.message : "서류심사에 실패했습니다.",
      );
    } finally {
      if (screeningRunRef.current === runId) {
        setScreeningRunning(false);
        screeningJobIdRef.current = null;
      }
    }
  };

  /** 심사표에서 주택유형을 바꾸면 판정 기준이 통째로 달라지므로 바로 다시 심사한다. */
  const handleScreeningHousingTypeChange = (value: HazardHousingType) => {
    setHazardHousingType(value);
    void runScreening({ housingType: value });
  };

  const handleScreeningApplicationTypeChange = (
    value: HazardApplicationType,
  ) => {
    setHazardApplicationType(value);
    void runScreening({ applicationType: value });
  };

  /**
   * 시설의 기준점을 지정한다(#7·#8·#9·#11). 저장 후 목록을 새로 읽고,
   * 같은 조건으로 심사를 다시 돌려 직전 결과와의 차이를 남긴다.
   */
  const designateFacility = async (
    facility: string,
    payload: { pnu?: string; lat?: number; lng?: number; source_label?: string },
  ) => {
    if (screeningRunning) return;
    setDesignationError("");
    const previous = screeningResult;
    try {
      await designateFrontDoor({ facility, ...payload });
    } catch (error) {
      setDesignationError(
        error instanceof Error ? error.message : "기준점 지정에 실패했습니다.",
      );
      return;
    }
    setDesignationTarget(null);
    await refreshFrontDoors();
    await runScreening({ diffFacility: facility, previousResult: previous });
  };
  // 지도 클릭 핸들러가 쓰는 ref 를 최신 함수로 잇는다.
  useEffect(() => {
    designateFacilityRef.current = (facility, payload) =>
      void designateFacility(facility, payload);
  });

  /** 시설의 수기 지정을 해제하고 다시 심사한다. */
  const removeFacilityDesignation = async (facility: string) => {
    if (screeningRunning) return;
    setDesignationError("");
    const previous = screeningResult;
    try {
      await removeFrontDoor(facility);
    } catch (error) {
      setDesignationError(
        error instanceof Error ? error.message : "지정 해제에 실패했습니다.",
      );
      return;
    }
    await refreshFrontDoors();
    await runScreening({ diffFacility: facility, previousResult: previous });
  };

  /** 지도의 문·출구 후보를 눌렀을 때. 그 좌표를 기준점으로 지정한다. */
  const handleSelectCandidate = (
    facility: string,
    candidate: FrontDoorCandidate,
  ) => {
    void designateFacility(facility, {
      lat: candidate.coordinates.lat,
      lng: candidate.coordinates.lng,
      source_label: `${candidate.label} 후보 선택`,
    });
  };

  const enterDesignationMode = (facility: string) => {
    setDesignationError("");
    setDesignationTarget(facility);
    setRailVisible(true);
  };

  const handleToggleScreeningGroup = useCallback((groupKey: string) => {
    setExpandedScreeningGroupKey((current) =>
      current === groupKey ? null : groupKey,
    );
  }, []);

  /** 심사를 실행한다. 결과 레일을 펼치고 열려 있던 심사표는 닫는다. */
  const handleRun = async () => {
    if (!selectedCandidate) return;
    setRailVisible(true);
    setSheetOpen(false);
    await runScreening();
  };

  const handleStopScreening = () => {
    const jobId = screeningJobIdRef.current;
    screeningRunRef.current += 1;
    setScreeningRunning(false);
    setScreeningProgress(null);
    setScreeningError("서류심사를 중단했습니다.");
    if (jobId) {
      void cancelScreeningJob(jobId).catch(() => {
        // 서버에 닿지 않아도 이 화면의 폴링은 이미 멈춘 상태다.
      });
    }
    screeningJobIdRef.current = null;
  };

  const showRail = Boolean(selectedCandidate) && railVisible;

  return (
    <main className="site-scope-module solo-app" data-module-view="solo">
      <TopSearchBar
        query={query}
        onQueryChange={(nextQuery) => {
          setQuery(nextQuery);
          setSearchError("");
          setSearchNotice("");
        }}
        onSearch={handleSearch}
        searching={searching}
        searchError={searchError}
        searchNotice={searchNotice}
        hasSite={Boolean(selectedCandidate)}
        mapProvider={mapProvider}
        onMapProviderChange={(nextProvider) => {
          window.localStorage.setItem("lh-screening-map-provider", nextProvider);
          setMapProvider(nextProvider);
        }}
        onRun={handleRun}
        running={screeningRunning}
        canPrint={Boolean(screeningResult)}
        theme={theme}
        onToggleTheme={toggleTheme}
      />

      <div className="solo-body">
        <div
          className="solo-map-stage"
          data-rail={showRail ? "open" : "closed"}
        >
          <MapPanel
            site={selectedCandidate}
            mapProvider={mapProvider}
            hazardMode
            hazardReview={mapHazardReview}
            hazardParcels={hazardParcels}
            onToggleParcelAt={handleToggleParcelAt}
            onToggleParcel={handleToggleParcel}
            selectedHazardFacilityId={selectedHazardFacilityId}
            onSelectHazardFacility={setSelectedHazardFacilityId}
            selectedHazardFindingId={selectedHazardFindingId}
            onSelectHazardFinding={handleSelectHazardFinding}
            screeningResult={screeningResult}
            expandedScreeningGroupKey={expandedScreeningGroupKey}
            selectedScreeningHitName={selectedScreeningHitName}
            onSelectScreeningHit={setSelectedScreeningHitName}
            onSelectCandidate={handleSelectCandidate}
            designationTarget={designationTarget}
            onCancelDesignation={() => setDesignationTarget(null)}
            viewportRequest={viewportRequest}
          />

          {!selectedCandidate && (
            <div className="solo-map-empty" role="status">
              <MapPin size={20} aria-hidden="true" />
              <strong>사업지를 먼저 검색하세요</strong>
              <p>
                주소나 장소를 검색하면 지도에 대지경계가 그려지고, 심사 결과가
                이 화면 위에 뜹니다.
              </p>
            </div>
          )}

          {selectedCandidate && !railVisible && (
            <button
              type="button"
              className="rail-reopen-button"
              onClick={() => setRailVisible(true)}
            >
              <PanelRightOpen size={16} />
              결과 패널 열기
            </button>
          )}

          {showRail && selectedCandidate && (
            <ResultRail
              site={selectedCandidate}
              siteParcels={hazardParcels}
              parcelNote={parcelNote}
              onResetParcels={() => {
                setHazardParcels([]);
                setParcelNote("");
              }}
              onCollapse={() => setRailVisible(false)}
              screeningResult={screeningResult}
              screeningRunning={screeningRunning}
              screeningError={screeningError}
              hazardApplicationTypes={hazardApplicationTypes}
              hazardRulePack={rulePack}
              hazardHousingType={hazardHousingType}
              hazardApplicationType={hazardApplicationType}
              onHazardHousingTypeChange={setHazardHousingType}
              onHazardApplicationTypeChange={setHazardApplicationType}
              onOpenSheet={() => setSheetOpen(true)}
              onRerun={handleRun}
            />
          )}
        </div>

        {sheetOpen && (
          <div className="module-detail-overlay">
            <header>
              <strong>심사</strong>
              <button
                type="button"
                onClick={() => setSheetOpen(false)}
                aria-label="상세 닫기"
              >
                <X size={17} />
              </button>
            </header>
            <div className="module-detail-body">
              <ScreeningSheet
                  result={screeningResult}
                  running={screeningRunning}
                  error={screeningError}
                  applicationTypes={hazardApplicationTypes}
                  housingType={hazardHousingType}
                  applicationType={hazardApplicationType}
                  onHousingTypeChange={handleScreeningHousingTypeChange}
                  onApplicationTypeChange={
                    handleScreeningApplicationTypeChange
                  }
                  selectedFacilityId={selectedHazardFacilityId}
                  onSelectFacility={setSelectedHazardFacilityId}
                  frontDoors={frontDoors}
                  designationTarget={designationTarget}
                  designationError={designationError}
                  onEnterDesignation={enterDesignationMode}
                  onCancelDesignation={() => setDesignationTarget(null)}
                  onRemoveDesignation={(facility) =>
                    void removeFacilityDesignation(facility)
                  }
                  onSelectCandidate={handleSelectCandidate}
                  expandedGroupKey={expandedScreeningGroupKey}
                  onToggleGroup={handleToggleScreeningGroup}
                  selectedHitName={selectedScreeningHitName}
                  onSelectHit={setSelectedScreeningHitName}
                  screeningDiff={screeningDiff}
                  onClose={() => setSheetOpen(false)}
                  onRerun={() => void runScreening()}
                />
            </div>
          </div>
        )}
      </div>

      <ScreeningProgressModal
        open={screeningRunning}
        progress={screeningProgress}
        siteName={selectedCandidate?.name}
        onStop={handleStopScreening}
      />
    </main>
  );
}

export default App;
