# [원문] Claude 조사 — 미확보 원천 전국 공개 API (2026-09-15)

> 조사 도구: Claude Code (Fable 5.1, 웹 검색·페이지 열람 + 이 PC `backend/.env` 의 `PUBLIC_DATA_SERVICE_KEY`·`SAFEMAP_API_KEY` 로 **실호출**). 브리프: `docs/research/api_research_brief_2026-09-15.md`. 항목 그룹별로 5개 조사 에이전트를 병렬로 돌리고 종합했다. 키 값은 어디에도 적지 않았다.
>
> 표기: 「실호출」 = 2026-09-15 이 PC 키로 호출한 결과. 「미검증」 = 문서·검색으로만 확인했거나 확인하지 못한 것. 추측 URL 은 적지 않았고, 시험 삼아 넣어 본 경로가 400/코드 12(서비스 없음)를 돌려주면 그 경로는 본문에서 뺐다.

## 0. 조사 중 확인된 공통 사실

1. **localdata.go.kr 은 2026-04-16 서비스 종료**됐고 지방행정인허가 195종·생활편의 14종이 data.go.kr 로 통합됐다(ZDNet 2026-01-25, `localdata.go.kr/portal/end.do`; 이 PC 에서는 152.99.104.122:443 접속 거부라 스니펫으로 확인). 이제 「행정안전부_○○ 조회서비스」 오픈API 형태이며 경로는 그대로 `https://apis.data.go.kr/1741000/{slug}/info` 다. 프로젝트 `app/services/localdata.py` 의 `LOCALDATA_BASE` 가 이미 이 체계라 코드 영향은 없다.
2. **현재 키로 1741000 계열이 200 이다.** `high_pressure_gas/info`(totalCount 30,475)·`city_gas_companies/info`(86)·`lodgings/info` 실호출 200. 9/11·9/14 문서의 「LOCALDATA 403」은 해소된 상태다. 새 업종 slug(공연장 등)만 403/코드 30(존재하나 이 키로 활용신청 안 됨)이다.
3. data.go.kr 응답 코드 규칙(보정 호출로 확인): **403/코드 30** = 엔드포인트 존재·활용신청 미승인, **400/코드 12** = 그 경로에 서비스 없음, ODcloud **401 `{"code":-401}`** = 해당 파일데이터 활용신청 미승인, ODcloud 404 「등록되지 않은 서비스」 = 그 파일은 자동변환 API 미제공.
4. 표준데이터(`api.data.go.kr/openapi/tn_pubr_public_*_api`) 는 파라미터 `serviceKey·pageNo·numOfRows·type(xml|json)` + 컬럼명 필터, 좌표는 WGS84 십진 위경도, 활용신청은 개발·운영 자동승인·개발계정 10,000/일이 통례다(개별 데이터셋 수치는 각 항목에 미검증 표기).
5. 가스안전공사 `apis.data.go.kr/B410019/kgsapi` 는 `lpg_station` 오퍼레이션 하나만 200 이며, data.go.kr 에 노출된 공사 오픈API 도 LPG 충전소 `15155631` 하나뿐이다. 판매소·저장소·CNG 오퍼레이션은 없다.

## 1. 요약표

| # | 항목 | 등급 | 핵심 원천 (ID / 경로) | 실호출 상태 | 막고 있는 것 |
|---|---|---|---|---|---|
| 1 | 소음진동배출시설 공장 | **B** (소음도·방음시설은 D) | 표준데이터 `15139233` `api.data.go.kr/openapi/tn_pubr_public_noise_vibration_emission_fclt_api` | 403/30 | 활용신청. 50dB 예외 필드는 어디에도 없음 |
| 2 | LPG 판매소 | **B** (최신성 C) | 가스안전공사 ODcloud `15091481` uddi:f40c83ef-… | 401 | 활용신청. 2024-03 일회성·좌표 없음. 행안부에 「LPG 판매」 업종 없음 |
| 3 | LPG 저장소 | **C** | 행안부 고압가스업 `1741000/high_pressure_gas/info` 의 `BZSTAT_SE_NM=저장소` 행 | **200** | LPG(액법) 저장소 포함 여부 미검증. 공사 자료는 시도 집계뿐 |
| 4 | 위험물 제조소·저장소·취급소 | **D** | 전국 시설별 원장 없음. 광산소방서 `15055248`(730행, 주소 없음) 등 | — | 소방서 정보공개청구 |
| 5 | 유독물 보관·저장·판매 | **C** | 생활안전지도 `IF_0049`(주소·XY), 경기도 `15059062`(WGS84) | safemap 500/30 | safemap 키 재등록. 허가구분·유독물 여부 필드 없음 |
| 6 | 도료류 판매소 | **C** | 상가정보 `15012005` `apis.data.go.kr/B553077/api/open/sdsc2/storeListInUpjong` | **200** | 페인트 소매 전용 업종코드 없음(G21001 철물/공구 등 근사). 도매·제조는 미수록 |
| 7 | 도시가스 제조시설 | **D** (사업자 API 는 A) | 시설 좌표 API 없음. 사업자 `1741000/city_gas_companies/info` 86건 | 200(사업자) | 정압기·제조소는 보안시설, 정보공개청구 |
| 8 | 화약류 저장소 | **D** | 경찰청 `15048432` 연도별 건수뿐 | — | open.go.kr 정보공개청구(시도경찰청) |
| 9 | 특정대기유해물질 배출공장 | **D** (대기배출 전체는 C) | 행안부 대기배출시설 `15044957`/API `15154973`(종별·HAP 여부 없음), TMS `15074658`, PRTR `15024756` | TMS 403/30, PRTR 404 | HAP 여부는 시군구 인허가대장 청구 |
| 10 | 카지노영업소 | **C** | 문체부 `3075667`(HWP), 협회 회원사 18곳, 제주 `15010273`(401) | 401 | 전국 주소 CSV 없음 → 목록 파싱+지오코딩 |
| 11 | 자동차용 CNG 충전소 | **B** | ODcloud `15001508` uddi:928477d9-… 192행·위경도 | 401 | 활용신청만. CSV 는 즉시 다운로드 |
| 12 | 지하철역 출입구 | **C** (역 좌표는 B) | KRIC `openapi.kric.go.kr/openapi/convenientInfo/stationGateInfo`(출구번호·주소, 좌표 없음), `stationInfo`(역 위경도) | 200/resultCode 30 | 출구 좌표 원천 전국 어디에도 없음 → 출구 주소 지오코딩 |
| 13 | 철도역(코레일·KTX) | **B** | 국가철도공단 `15067652`(215역·주소·좌표·등급), 코레일 `15127532`(202역·좌표·출입구 수), 도시철도 표준 `15013205` | 401 | 활용신청. CSV 즉시 |
| 14 | 버스정류장 운행주기 15분 | **A** (완전성 C) | TAGO `1613000/BusRouteInfoInqireService/getRouteInfoIem` `intervaltime` | **200** | 전주 등 다수 도시 `intervaltime` 누락/0, 서울(11) 미포함 → TOPIS 별도 |
| 15 | 대중교통 터미널 | **A**(목록) + **C**(좌표) | TAGO `1613000/ExpBusInfo/GetExpBusTrminlList`(453), `SuburbsBusInfo/GetSuberbsBusTrminlList`(358) | **200** | ID·명칭뿐, 좌표·주소 없음 → 명칭 지오코딩 |
| 16 | 환승시설 | **B**(환승센터) / **D**(환승주차장·정류장) | 표준데이터 `15034541` `tn_pubr_public_pbtrnspt_rnsit_cnter_api`(14개 기관 등재) | 403/30 | 활용신청. 전국 아님 |
| 17 | 전통시장 | **B** | 표준데이터 `15012894` `tn_pubr_public_trdit_mrkt_api` | 403/30 | 활용신청 |
| 18 | 공원 | **B** | 표준데이터 `15012890` `tn_pubr_public_cty_park_info_api`(233 기관) | 403/30 | 활용신청 |
| 19 | 문화시설 | **B** | 행안부 `performance_halls`·`tourist_performance_halls`·`museums_and_art_galleries`·`movie_theaters` | 403/30 | 4개 slug 활용신청 |
| 20 | 공공시설 | **C**(주민센터) / **B**(도서관) | 읍면동 하부행정기관 `15059715`(주소만, ODcloud 401), 도서관 표준 `15013109` `tn_pubr_public_lbrry_api` | 401 / 403/30 | 주민센터는 지오코딩 필요 |
| 21 | 초·중·고 | **B** | 표준데이터 `15021148` `tn_pubr_public_elesch_mskul_lc_api`(좌표·운영상태) | 403/30 | 활용신청. NEIS 는 좌표 없음 |
| 22 | 대학교(정문) | **B**(본부) / **C**(정문) | 대학 표준 `15107736` `tn_pubr_public_univ_info_api`(주소, 좌표 없음) + 카카오 `{학교명} 정문`(category 입출구) | 403/30, 카카오 200 | 정문은 대학별 편차 → 미확인 표기 |

---

## 2. 항목별 상세

### 1. 소음진동배출시설 공장(라목) — B (소음도·방음시설은 D)

- **원천**: 전국소음진동배출시설표준데이터. 제공 지자체(46개 기관 등재), 소관 기후에너지환경부. https://www.data.go.kr/data/15139233/standard.do (수정 2026-09-02, 갱신 연간).
- **엔드포인트**: `https://api.data.go.kr/openapi/tn_pubr_public_noise_vibration_emission_fclt_api`. 필수 `serviceKey, pageNo, numOfRows(≤1000), type=xml|json`. 선택 컬럼 필터 `FCLT_NM, CTPV_NM, SGG_NM, LCTN_ROAD_NM_ADDR, LCTN_LOTNO_ADDR, LAT, LOT, NOIS_VBRT_SE_NM, NOIS_VBRT_MAIN_CN, GNRL_RGN_YN, RDSD_RGN_YN, TELNO, RPRSV_NM, DATA_CRTR_YMD, instt_code, instt_nm`.
  - 실호출 예: `GET …/tn_pubr_public_noise_vibration_emission_fclt_api?serviceKey=…&pageNo=1&numOfRows=2&type=json` → **HTTP 403** `SERVICE_KEY_IS_NOT_REGISTERED_ERROR` 코드 30 (엔드포인트 존재, 활용신청 미승인 — 프로젝트 `noise_emission.py` 의 8/28 관찰과 동일).
- **필드**: 시설명·시도·시군구·도로명/지번주소·위도 `LAT`·경도 `LOT`(WGS84 십진)·소음진동구분명·소음진동주요내용·일반지역여부·도로변지역여부·전화·대표자·데이터기준일자. **영업상태·소음도(dB)·방음시설 없음.**
- **커버리지**: 전국 표준데이터이나 46개 지자체만 등재(229 지자체 대비 일부). 지자체별 기준일 상이.
- **인증**: data.go.kr 일반 키. standard.do → 「오픈API」 탭 → 활용신청. 개발·운영 자동승인, 개발계정 10,000/일(페이지 표기).
- **50dB 예외 대안 조사 결과 — 없음(D)**: data.go.kr 「소음진동배출시설」 101건(오픈API 2건: 대구 북구 `15095989` 주소·업종·허가신고 여부만; 파일 51건: 고양 `15055503`, 김해 `15093331` 2,208행, 광주 `15054125` 집계) 모두 소음도·방음시설 컬럼 없음. 행안부 인허가 계열에는 소음진동배출시설 업종 자체가 없음(환경측정대행업 `15155030`·환경전문공사업 `15155027` 만). 국가소음정보시스템은 측정망 자료.
- **경로**: ① 활용신청 → 어댑터는 무수정으로 붙음 ② 소음진동구분명·주요내용으로 1차 스크리닝 ③ 50dB 는 관할 시군구 환경과 배출시설 설치신고 대장 정보공개청구(신고서에 배출허용기준·방음시설 기재) ④ 룰북대로 「예외 미확인」 유지.

### 2. LPG 판매소 — B (최신성은 C)

- **원천**: 한국가스안전공사_전국 LPG판매소 현황. https://www.data.go.kr/data/15091481/fileData.do — 기준일 **2024-03-05, 4,542행**, 갱신 「수시(1회성)」, 국민 제공신청으로 등록된 자료. 컬럼 `업소명·주소·일련번호` 뿐.
- **엔드포인트**(Swagger `https://infuser.odcloud.kr/oas/docs?namespace=15091481/v1` 로 확인): `GET https://api.odcloud.kr/api/15091481/v1/uddi:f40c83ef-d9dd-49b7-a912-bfbdfad65118?page=1&perPage=100&returnType=JSON`. 인증 `serviceKey` 쿼리 또는 `Authorization: Infuser {KEY}`. 응답 `{page, perPage, totalCount, currentCount, matchCount, data:[{업소명,주소,일련번호}]}`.
  - 실호출 → **HTTP 401** `{"code":-401,"msg":"유효하지 않은 인증키 입니다."}` = 활용신청 미승인.
- **필드**: 주소 있음·위경도 없음(지오코딩 필요)·영업상태 없음·기준일 2024-03.
- **인증**: fileData.do → 「오픈API」 → 활용신청. ODcloud 파일 API 는 통상 자동승인. 소요·트래픽 수치 페이지 미기재(미검증).
- **행안부 인허가에 「액화석유가스 판매」 업종 없음**(확인): 「액화석유가스」 검색 시 행안부 계열은 액화석유가스용품제조업체(`15044992` 파일 / `15154787` API)뿐. 고압가스업 API(`15155163`, `high_pressure_gas`, 실호출 200, `BZSTAT_SE_NM` 표본 200행에서 「제조」「저장소」만 관측)에 LPG 판매가 포함되는지는 미검증.
- **가스안전공사 자체 API**: kgsapi 는 `lpg_station`(충전소, 1,976건, `SECT_NM, BSES_NM, ADDR, LAT, LOT, MGT_NM, TELNO`)만. 판매소 오퍼레이션 없음.
- **지자체 파일(주소만)**: 광주 `15001884`(38행, 2025-01), 부산 남구 `15060276`·동래 `15025969`·영도 `15083305`, 인천 서구 `15105596`, 서울 `15048520`(2018, 저장/판매/충전 구분·좌표·영업상태), 오산 `15033608`. 우수 LPG판매 인증업체 `15001503`(106행).
- **경로**: ① ODcloud 활용신청 → 주소 지오코딩(카카오) ② 고압가스업 API 업태값 전수 확인 ③ 지자체 파일 보완 ④ 가스안전공사에 판매소 API 제공신청.

### 3. LPG 저장소 — C

- **집계 자료(판정 불가)**: 가스시설 현황 `15067840`(Swagger 확인, uddi 6개, 「구분·LPG저장·LPG판매·LPG충전…」 시도별 집계), 소형저장탱크 `15020467`(톤급별 집계), 시도별 소형저장탱크 `15067433`. 시설 주소 없음.
- **가장 가까운 원천**: 행안부 고압가스업 조회서비스 `15155163` — `https://apis.data.go.kr/1741000/high_pressure_gas/info?serviceKey=…&pageIndex=1&pageSize=100&type=json` 실호출 **200**, totalCount 30,475. 필드 `BPLC_NM, BZSTAT_SE_NM(제조/저장소 …), LOTNO_ADDR, ROAD_NM_ADDR, CRD_INFO_X/Y(EPSG:5174), SALS_STTS_NM(영업/정상·휴업), DTL_SALS_STTS_NM, LCPMT_YMD, CLSBIZ_YMD, DAT_UPDT_PNT, MNG_NO`. 전국·매일(D-2) 갱신, numOfRows 100 고정. **이미 `high_pressure_gas` 로 적재 중**이므로 `BZSTAT_SE_NM=저장소` 행을 후보로 쓸 수 있다. 다만 고압가스안전관리법 저장소이며 액화석유가스법 LPG 저장소가 포함되는지 미검증.
- 특정고압가스업 API `15154881` 의 slug 는 `special_high_pressure_gas` 로 시험 시 400/코드 12 — 프로젝트가 쓰는 `specific_high_pressure_gas` 와 다르니 프로젝트 slug 를 유지한다.
- **지자체 파일(전국 아님)**: 당진 `15029694`, 예산 `15029762`, 이천 `15037730`, 김포 `15037671`, 논산 `15029780`, 제천 `15029718`, 청양 `15039855`, 가평 `15029863`, 의정부 `15039882`(고압가스 저장탱크), 오산 `15033608`(총저장능력). 광주 동부소방서 가스관련업소 `15052475` 는 업소명만.
- **경로**: ① 고압가스업 저장소 행을 후보로 쓰되 LPG 여부는 상호·용도로 판별하고 「검토」로 표기 ② 지자체 파일 병합 ③ 가스안전공사 사고예방·검사 DB 에 시설 API 제공신청(data.go.kr 제공신청) ④ 관할 시군구 LPG 저장소 허가대장 정보공개청구.

### 4. 위험물 제조소·저장소·취급소(다목) — D

- **data.go.kr 「위험물」 오픈API 14건 전수 확인**: 소방청 국가위험물정보 `15061055`(`apis.data.go.kr/1661000/materialInfoSvc`, **물질** 정보), 소방청 위험물정보 `3059153`(404 폐기), 화재정보 `15077644`, 특정소방대상물정보 `15155780`(주소·X/Y 있으나 설명이 숙박시설 중심, 위험물 시설 포함 여부 미검증), 행안부 위험물제조소집계 `15153257`(safetydata 연계, 시도·시군구 **집계**, 운영 심의승인), 경기 `15059110`·광명 `15033032`(집계), KFI 위험물 업체정보 `15083460`(`apis.data.go.kr/B552486/opnMnfcCmpyDanger/opnMnfcCmpyDangerCC`, 이중벽탱크·운반용기 **제조업체**)·승인정보 `15078046`.
- **파일**: 소방청 `15124189`(2023-08, 1행), 전남 `15124035`(22행, 시군별 집계, 주소 없음 — 9/11 문서의 「전남은 위치 공개」는 틀림), 광주 광산소방서 `15055248`(730행: 관할서·센터·제조소등구분·대상명, **주소 없음**), 북부소방서 `15055249`(225행 동일 구조), 전북 `3081309`·서울 `15047086`·제주 `15011760`·경남 `15088750`(집계). 소방청 nfa.go.kr 개방목록은 data.go.kr 위임. 국가위험물통합정보시스템 hazmat.nfa.go.kr 은 인증서 오류로 미확인.
- **결론**: 주소·좌표가 붙은 제조소등 목록을 공개하는 기관이 없다.
- **경로**: ① 관할 소방서에 위험물 제조소등 허가대장 정보공개청구(광산소방서 형식에 소재지 추가 요청) ② `15155780` 특정소방대상물 API 에 위험물 대상 포함 여부 실호출 검증 ③ 주유취급소는 오피넷·행안부 `oil_retailers`(이미 배선)로 대체 ④ 소방청에 data.go.kr 제공신청.

### 5. 유독물 보관·저장·판매(마목) — C

- **전국 영업허가 원장 API 없음**: data.go.kr 「유해화학물질」(API 23건)·「취급사업장」·「영업허가」 검색 결과 환경부·화학물질안전원·환경공단 명의의 사업장 좌표 원장 없음. 안전원 API 는 `15072442`(`apis.data.go.kr/1480802/iciskischem`, 물질 CAS·노출증상)와 PRTR `15024756`(LINK 형)뿐. icis.mcee.go.kr 「화학물질 통계 정보공개」(`/search/searchTypeView6.do`)는 사업장명·소재지·업종·최대저장량을 **화면 검색**으로만 제공(다운로드·API 없음). 안전원 공공데이터 개방현황(`nics.mcee.go.kr/sub.do?menuId=113`)에 「목록에 없는 데이터 제공신청」 창구, 담당 사고예방심사1과(043-830-4210/4227).
- **생활안전지도 IF_0049 화학물취급시설**(`safemap.go.kr/opna/data/dataViewRenew.do?objtId=148`): 출처 환경부, 갱신 1년, 기준 2025-11-07. XML `https://safemap.go.kr/openapi2/IF_0049`, WMS `…/IF_0049_WMS`. 파라미터 `serviceKey`(필수), `numOfRows`(필수), `pageNo`, `returnType`. 응답 `objt_id, entrps_nm, induty_nm, adres, rn_adres, x, y`. XML 응답의 x,y 좌표계 미검증(프로젝트 IF_0033 실측은 EPSG:3857).
  - 실호출 → **HTTP 500** `{"header":{"resultCode":"30","resultMsg":"SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}}` — 현재 `SAFEMAP_API_KEY` 가 safemap 에 등록되지 않은 상태(IF_0033 배선 때와 키가 다르거나 만료. 재발급 `safemap.go.kr/opna/crtfc/keyAgreeRenew.do`, 약관 동의 → 신청서, 「원칙적으로 승낙」, 소요·한도 미명시). 전국 범위 명시 없음. 허가구분(보관·저장·판매)·유독물 해당·운영상태 필드 없음.
- **지자체**: 경기도 `15059062`(경기데이터드림 `infId=M37YD49AM5UFN6VJM2CZ29567068`, openapi.gg.go.kr KEY/pIndex/pSize, 시군명·업체명·도로명/지번·업종구분·연간취급량·**WGS84 위경도**, 자동승인, 수정 2025-09-25; 서비스 영문명 미검증). 파일: 군산 `15140198`(지번, 좌표 없음), 전남광주통합특별시 `15050660`, 평택 `15037437` 등 177건 산재.
- **경로**: ① safemap 키 재발급 → IF_0049 전량 페이징(후보 위치) ② 경기도 API 로 경기 보강 ③ 화학물질안전원에 영업허가 사업장 원장 제공신청 ④ 지자체 파일 병합. 판정 확정에는 쓰지 않고 후보 레이어로만.

### 6. 도료류 판매소(사목) — C

- **전용 원장**: data.go.kr 「도료」 검색 파일 18건·API 0건, 전부 무관. 행안부 인허가에도 도료 판매 업종 없음.
- **상가(상권)정보 API `15012005`**(소상공인시장진흥공단, 자동승인, 개발 10,000/일): 현행 경로 **`https://apis.data.go.kr/B553077/api/open/sdsc2/`**(구 `sdsc` 아님).
  - `storeListInUpjong?divId=indsSclsCd&key=G20501&pageNo=1&numOfRows=2&type=json` → **200**, totalCount 2,029. 필드: 상가업소번호·상호명·지점명·상권업종 대/중/소 코드·명·표준산업분류코드(KSIC 10차)·시도/시군구/행정동/법정동 코드·PNU·지번·도로명·우편번호·동/층/호·`lon`·`lat`(십진 경위도, EPSG 공식 표기 없음 → WGS84 미검증).
  - `storeListInRadius?radius=500&cx=127.05&cy=37.497&indsSclsCd=G21001` → 200.
  - `smallUpjongList` → 소분류 1,255개. 도료 관련: **G10906 도료 도매업**, G10999 기타 건축자재 도매업, G11009 기타 화학물질 도매업, C13601 일반용 도료 제조업; 소매는 **G21001 철물/공구 소매업**(15,279), G21003 건설/건축자재 소매업(1,363), G21099 기타 건설/건축자재 소매업(2,016). **「페인트 소매」 전용 소분류 없음.** G1·C1·G10906 조회는 `NODATA_ERROR` → 이 API 는 소매·서비스 업종만 수록.
  - 갱신: 파일판 `15083033` 분기(2026-06-30 기준, 다음 2026-10-31). 업종체계 2023-02-28 개편.
- **경로**: ① G21001/G21003/G21099 + 상호명 「페인트/도료」 필터로 후보 ② 위험물 판매취급소 여부는 4번 경로(소방서 대장)와 교차 ③ 파일판 전수 다운로드. 현재의 석유대체연료판매업 근사는 시설군 불일치이므로 정식 대체로 두지 않는다.

### 7. 도시가스 제조시설(아목) — D (사업자 API 는 A)

- **시설 좌표 API 없음**: 「정압기」 API 0건·파일 5건(가스기술공사 정비현황 `15103373`, PIC 규격·지진감지 `15156117` — 위치 없음). 가스공사 `15102905` 는 순번·지역·정압관리소 수 3컬럼 통계(연간), `15102897` 는 HWPX, 가스안전공사 `15067840` 시도 집계, `15001493` 지역관리소는 고객센터 연락처, 「LNG 생산기지」는 생산량 통계(`15049906`).
- **사업자 원장**: 행안부 일반도시가스업체 조회서비스 `15154835` — `https://apis.data.go.kr/1741000/city_gas_companies/info?serviceKey=…&pageNo=1&numOfRows=3&type=json` → **200, totalCount 86**. 필드 `BPLC_NM, GAS_KND_NM(LNG/도시가스외 가스공급시설설치자), LOTNO_ADDR, ROAD_NM_ADDR, CRD_INFO_X/Y(EPSG:5174, 일부 공란), SALS_STTS_NM, DTL_SALS_STTS_NM, LCPMT_YMD, CLSBIZ_YMD, DAT_UPDT_PNT, PIPE_LEN, MNG_NO`. 구 오퍼레이션 `getCityGasCompanies` 는 400(폐기). 사업자 본점 단위라 제조소·정압기 판정엔 부족(이미 `city_gas_companies` 로 적재 중).
- **경로**: ① 사업자 본점 위치만 현행 유지 ② 시도별 「도시가스 공급시설 공사계획 승인」 파일(광명 `15033186`, 대구 서구 `15110511`, 서귀포 `15053150`)로 위치 단서 ③ 한국가스공사·도시가스사에 정압기·공급관리소 좌표 정보공개청구(보안시설로 비공개 가능성 높음) ④ 국토부 지하시설물 통합체계는 비공개.

### 8. 화약류 저장소(자목) — D

- **공개 현황**: 경찰청 `15048432`「총포 및 화약류 관리 현황」(CSV+API) 은 2001~2021 연도별 건수 15컬럼(제조·판매·저장소·광산사용 수), 수정 2025-05-12. `15048433/15048434` 는 면허·자격 통계. 지방경찰청 명의는 불법무기 자진신고 현황뿐, **저장소 위치 데이터셋 전국·시도 0건**. 경찰청 공공데이터 페이지(`police.go.kr/www/open/publice/publice01.jsp`)는 리다이렉트 루프로 본문 미검증.
- **기관**: 총포화약안전기술협회 공식 도메인 **`www.gesta.or.kr`**(HTTP 200; kfsa.or.kr 은 무관). 경찰청 총포화약안전관리시스템 `www.knpgun.go.kr`(민원 처리, 로그인 필요). 협회의 저장소 위치 공개 여부 미검증(검색상 없음).
- **담당·청구**: 경찰청 범죄예방대응국 범죄예방정책과(02-3150-1361, 검색 결과 기반·미검증). 허가권자는 종류별 시도경찰청장 또는 경찰서장(정부24 `CappBizCD=13200000089`). open.go.kr 정보공개청구 → 「경찰청」 또는 각 시도경찰청(처리 10일). data.go.kr 「공공데이터 제공신청」 병행.
- **경로**: ① 시도경찰청별 「화약류 저장소 허가 현황(소재지 포함)」 청구(보안상 부분공개 가능) ② data.go.kr 제공신청 ③ 협회 안전검사 대상 목록 문의 ④ LH 수기 승인 유지.

### 9. 특정대기유해물질 배출공장(가목) — D (대기배출시설 전체는 C)

- **HAP 전용 원장 없음**: data.go.kr 파일·API 검색에서 「특정대기유해물질 배출시설 사업장」 목록 없음. SEMS(sems.air.go.kr)는 로그인 전용, 공개 API 없음.
- **행안부 대기오염물질배출시설설치사업장** `15044957`(파일, 131,992행, 매일 D-2) / 조회 API `15154973`(자동승인, 10,000/일; 프로젝트가 `air_pollution_facility_installation` 으로 이미 적재 중). 필드에 인허가일·영업상태·사업장명·주소·좌표(EPSG:5174)는 있으나 **종별(1~5종)·배출시설 종류·HAP 여부 없음** → 과포함.
- **PRTR** `15024756`: 오퍼레이션 `getPrtrList`(업체별)·`getPrtrMttrList`(물질별), 파라미터 `accessKey, searchYear`. 키는 `icis.mcee.go.kr/prtr/infoYard/openApi.do` 별도 신청·승인. 구 호스트 `icis.me.go.kr` 은 301 → `icis.mcee.go.kr`, 그러나 `https://icis.mcee.go.kr/openapi/service/prtr/getPrtrList` 는 **404**(경로 변경 여부 미검증). 응답의 주소·좌표 포함 여부는 활용가이드(hwp) 미열람으로 미검증. 모집단이 배출량 조사 대상이라 HAP 배출시설과 다름.
- **TMS/CleanSYS** `15074658`(한국환경공단): `https://apis.data.go.kr/B552584/cleansys/rltmMesureResult` 등 4개 오퍼레이션. 실호출 **403/30**(자동승인, 개발 5,000). 응답은 지역·사업장명·굴뚝코드·7개 물질 측정값 — 주소·종별·HAP 여부 없음. TMS 부착 대형 사업장만.
- 파일 `15136837`·`15122803` 은 ODcloud 미제공(404).
- **경로**: ① `15044957`(이미 적재)로 대기배출 사업장 전수 ② HAP 여부는 시군구 인허가대장 정보공개청구 또는 환경부 대기관리과(044-201-6905) 협조 ③ TMS·PRTR 사업장 목록 교차로 후보 압축 ④ LH 수기 승인 유지.

### 10. 카지노영업소 — C

- **TourAPI 4.0** `KorService2/searchKeyword2` 실호출 **403/30**(미승인). 카지노 전용 contentTypeId·cat3 코드는 코드표 미확인(미검증). 관광정보 성격이라 등록 원장 아님.
- **문체부 카지노 현황** `3075667`(수정 2026-04-30, 연간, **HWP 1건**): 시도별 업체명·허가일·운영형태·대표자·종사원·매출·입장객·허가면적. ODcloud 404(파일 전용). 주소·좌표 컬럼 여부 미검증(HWP 미열람).
- **문체부 게시물** 「2025년 카지노업 현황」(`mcst.go.kr/kor/s_policy/dept/deptView.jsp?pSeq=2001`, 2025-05-01, `★2025 카지노 통계(2025년 4월 기준).hwpx`). 브리프의 2026-04-29 게시(pSeq=2132)는 이번 검색에서 재확인 못 함(미검증).
- **제주 카지노업현황** `15010273`(uddi:f8c65028-…): 업체명·법인명·소재지·영업종류·기구 수·영업시간. 실호출 **401**. 제주 8곳.
- **한국카지노업관광협회 회원사**(`koreacasino.or.kr/kcasino/asso/members.do`, 200): 외국인전용 17 + 내외국인 1 = **18곳**, 업체명·입주 호텔명.
- **경로**: ① 협회 18곳 + 문체부 HWP 로 업체·소재지 확정 ② 입주 호텔 주소 지오코딩 ③ 제주는 `15010273` 활용신청 ④ 연 1회 갱신 배치. 전국 주소 CSV/JSON 은 없다.

### 11. 자동차용 CNG 충전소 — B

- **원천**: 한국가스안전공사_전국 도시가스충전소 현황 `15001508`(https://www.data.go.kr/data/15001508/fileData.do), 기준 **2026-06-29, 192행**, 분기 갱신(다음 2026-09-29). 컬럼 순번·행정구역·지사·시설명·우편·주소·**위도·경도**. 페이지 HTML 에서 uddi 재확인: `uddi:928477d9-ab3e-4b97-8731-d4ff962f0570`(프로젝트 `cng.py` 와 일치).
- **실호출**: `https://api.odcloud.kr/api/15001508/v1/uddi:928477d9-…?page=1&perPage=3&serviceKey=…` → **401** = 활용신청 미승인.
- **인증**: fileData.do 「오픈API」 탭 → 활용신청 → 마이페이지 확인. 자동승인·한도 수치는 페이지에 없음(미검증). CSV 는 로그인 없이 즉시 다운로드.
- **대안**: kgsapi 에 CNG 오퍼레이션 없음(`B410019/kgsapi` 루트는 400/12). 경기데이터드림 CNG(경기), 서울 정보소통광장(서울). 협회(kanfv.org) 2026-02 기준 245개소 vs 본 자료 192행 — 검사대상 기준 차이로 추정(미검증).
- **경로**: ① 활용신청 → 기존 어댑터 무수정 연결 ② 대기 중엔 CSV 적재.

### 12. 지하철역 출입구 — C (역 좌표는 B)

- **KRIC 레일포털**(data.kric.go.kr): 회원가입 → 인증키 발급(키 1개로 전 API). 승인 소요·일일 한도 수치 미기재(「과도한 트래픽 시 제한」만, 미검증). `stationInfo?serviceKey=test` → 200, `resultCode 30 등록되지 않은 서비스키`.
  - 역사별 정보 `https://openapi.kric.go.kr/openapi/convenientInfo/stationInfo` — 필수 `serviceKey, railOprIsttCd`; 선택 `format, lnCd, stinCd, stinNm`. 응답 `stinLocLat/stinLocLon`(역 위경도), `mapCordX/Y`. 전국 도시철도 운영기관.
  - 역사별 출구정보 `…/convenientInfo/stationGateInfo`(id=184) — 필수 `serviceKey, format, railOprIsttCd, lnCd, stinCd`. 응답 `exitNo`·`adr`(주소)·`impFaclNm`·`dst`·`telNo`. **출구 좌표 없음.**
  - 출입구 승강장 이동경로 `handicapped/stationMovement` — 텍스트·이미지, 좌표 없음.
- **TAGO 지하철정보** `15098554`(자동승인, 10,000/일): 역별 출구목록·출구별 버스노선·주변시설 — 출구 좌표 유무 미검증.
- **지역 원천**: 서울 열린데이터광장 OA-21211(리프트 있는 출입구만, WGS84, 2020), OA-21213(지하도 공간정보), OA-21699(보행자 출입구); 서울교통공사 역사 좌표 `15099316`(276역). 부산 역명정보 `3077187`(401, 역 단위). 대구·광주·대전 출구 좌표 원천 없음. **전국 출구 좌표 원천은 없다.**
- **경로**: ① 역 좌표는 `15013205` 표준데이터/KRIC stationInfo ② 출구 번호·주소는 KRIC stationGateInfo → 주소 지오코딩 ③ 서울은 OA-21213/OA-21699 보강 ④ 현행 카카오 「N번출구」 유지가 가장 현실적.

### 13. 철도역(코레일·KTX) — B

- **국가철도공단_철도역 정보** `15067652`(기준 2025-07-11, **215역**, 연간·차기 2026-11-30): 역명(한·중·영)·관련노선·`LATMAP`(위도)·`GRAMAP`(경도)·`ADDR`·`STATIONLVL`(등급). ODcloud `uddi:29222bc3-1bc1-44bd-9ad6-9210ddb9a6ca` 실호출 **401**. 좌표계 미명시(WGS84 추정, 미검증).
- **한국철도공사_역 위치 정보** `15127532`(2024-04-01, **202행**, 1회성): 지역본부·역명·위도·경도·출입구 개수. ODcloud `uddi:c1d09745-9e5c-48e4-b26c-c1833592509c` **401**. CSV 즉시.
- **한국철도공사_KTX 노선별 역정보** `15127571`(2025-11-21) 존재, 필드 미검증.
- **전국도시철도역사정보 표준데이터** `15013205`(국가철도공단, 2024-12-31, **1,073행**): 역번호·역사명·노선·환승·위도·경도·운영기관·도로명주소. 도시·광역철도 대상(KTX 일반철도역 제외). 표준데이터 API 엔드포인트는 페이지에 명시 없음(KRIC id=32 연결, 미검증).
- **인증**: data.go.kr 활용신청(자동승인 여부 미명시). 파일 즉시.
- **경로**: ① `15067652` CSV 즉시 적재 ② `15127532` 로 출입구 수 보강 ③ 광역·도시철도는 `15013205` ④ API 전환 시 두 uddi 활용신청.

### 14. 버스정류장 운행주기 15분 이내 — A (완전성 C)

- **원천**: 국토교통부_(TAGO)_버스노선정보 https://www.data.go.kr/data/15098529/openapi.do (자동승인, 10,000/일) + 이미 승인된 버스정류소정보.
- **실호출 흐름(모두 200)**: ① `1613000/BusSttnInfoInqireService/getCrdntPrxmtSttnList?gpsLati=35.8242&gpsLong=127.1480&_type=json` → `citycode 35010, nodeid JUB306100761` ② `getSttnThrghRouteList?cityCode=35010&nodeid=…` → `routeid, routeno, routetp`(18건) ③ `1613000/BusRouteInfoInqireService/getRouteInfoIem?cityCode=35010&routeId=JUB305001173` → 전주: `{endvehicletime:"0600", startvehicletime:"0600", routeno:752 …}` — **`intervaltime` 없음, 첫차=막차 값 불량**. 부산(21): `intervaltime:10`(정상). 천안(34010): `intervaltime:0`. 제주(39): 0·첫차막차 없음.
- **문서 필드**: `intervaltime`(평일)·`intervalsattime`(토)·`intervalsuntime`(일)·`startvehicletime`·`endvehicletime`. 운행횟수 없음. 실측상 토·일은 어느 도시도 안 옴.
- **도시코드**: `getCtyCodeList` 138개(광역시 8·경기 31·도 단위 시군). **서울(11) 미포함**(서울시청 좌표 정류소 0건).
- **보완**: 서울 TOPIS 노선정보 `15000193`(ws.bus.go.kr, 자동승인, 1,000/일; `term` 배차 필드는 미검증). 경기 GBIS `6410000/busrouteservice/v2/getBusRouteInfoItemv2`(`15080662`, `peekAlloc/nPeekAlloc` 평일 최소·최대 배차 확인). 국토교통부_버스노선 `15142030`(`1613000/BusRoute/getBusRoute`, 403/30 미신청, 스웨거 응답에 배차 없음 → 미검증). 전국 「노선별 배차간격」 표준데이터는 확인 못 함.
- **경로**: ① TAGO 로 즉시 배선(코드 변경만) ② 값이 0/누락이면 「운행주기 미확인」으로 표기하고 정류장은 후보로만 ③ 서울은 TOPIS 활용신청 ④ 경기는 GBIS 로 보강.

### 15. 대중교통 터미널 — A(목록·ID) + C(좌표 없음)

- **원천**: 국토교통부_(TAGO)_고속버스정보 `15098522`, 시외버스정보 `15098541`(자동승인, 10,000/일).
- **실제 경로(오퍼레이션 첫 글자 대문자)**: `https://apis.data.go.kr/1613000/ExpBusInfo/GetExpBusTrminlList?_type=json&numOfRows=2` → **200**, `{terminalId:"NAEK010", terminalNm:"서울경부"}`, totalCount **453**. `https://apis.data.go.kr/1613000/SuburbsBusInfo/GetSuberbsBusTrminlList` → **200**, `{terminalId:"NAI0162503", cityName:"서울특별시", terminalNm:"수락산역(직통)"}`, totalCount **358**(정류장급 혼재). (`ExpBusInfoService/getExpBusTrminlList` 표기는 400/12.) 선택 `terminalNm`(부분일치), `cityCode`(시외).
- **필드**: `terminalId·terminalNm(·cityName)` 뿐. **좌표·주소 없음.**
- **파일**: 교통안전공단 전국 대중교통 버스터미널 현황 `15066765` 는 시도별 개수 17행. 경기데이터드림 버스터미널(WGS84·주소, 경기 한정, 컬럼 미검증). 전국 터미널 표준데이터·협회 API 확인 못 함.
- **경로**: ① TAGO 터미널 ID·명칭 즉시 ② 명칭 지오코딩(카카오) ③ 버스정류소정보에서 「터미널」 명칭 정류소 좌표로 교차 ④ 지자체 파일 보정.

### 16. 환승시설 — B(환승센터) / D(환승주차장·환승정류장)

- **환승센터**: 전국대중교통환승센터표준데이터 https://www.data.go.kr/data/15034541/standard.do (소관 국토부, 제공 지자체 **14개 기관**, 연간, 기준 2026-09-01). `https://api.data.go.kr/openapi/tn_pubr_public_pbtrnspt_rnsit_cnter_api` 실호출 **403/30**. 필드: 환승센터명·유형코드·시도·시군구·도로명/지번주소·**위도·경도**·면적·버스정류소ID/명·도시철도노선/역사명·고속일반철도노선명·여객터미널명·공항터미널명·항만대합실명·주차단위구획수·편의시설·지정일자·운영유무·데이터기준일자. 14개 기관 등록분(건수 미검증) → 전국 커버리지 아님.
- **환승주차장**: 전국 위치 API·파일 없음. 코레일 역별 타 교통수단 환승현황 `15090378`(93행, 환승주차장 면수, 좌표 없음), 경기 GITS 환승주차장(gits.gg.go.kr, 좌표 컬럼 미확인). 전국주차장표준데이터 `15012896` 에 환승 구분 없음(미검증).
- **환승정류장**: 공개 데이터 없음.
- **경로**: ① 환승센터 표준 API 활용신청 ② `15090378` 보유역 목록을 철도역 좌표와 조인 ③ 경기 GITS 보정 ④ 나머지 「자료 없음」 명시.

### 17. 전통시장 — B

- **원천**: 전국전통시장표준데이터 https://www.data.go.kr/data/15012894/standard.do (소관 중기부, 제공 소상공인시장진흥공단, 연간, 수정 2025-11-26).
- **엔드포인트**: `https://api.data.go.kr/openapi/tn_pubr_public_trdit_mrkt_api?serviceKey=…&pageNo=1&numOfRows=100&type=json` → **403/30**(존재, 미승인). 필터 파라미터명 미검증.
- **필드**: 시장명·**시장유형**·도로명/지번주소·개설주기·**위도·경도**·점포수·취급품목·상품권·화장실·주차장·개설연도·전화·**데이터기준일자**. 영업상태 없음(지자체 인정 시장만). 전국(총 건수 미검증).
- **시장통통**: sbiz.or.kr / data.sbiz.or.kr 바로API(openApiId=A0000000000026) 는 접속 거부로 필드·좌표 미검증. 소진공 별도 ODcloud 전통시장 파일은 확인 못 함.
- **인증**: 로그인 → 「오픈API」 → 활용신청(자동승인·10,000/일 통례, 개별 수치 미검증).

### 18. 공원(국공립·생활권) — B

- **원천**: 전국도시공원정보표준데이터 https://www.data.go.kr/data/15012890/standard.do (소관 국토부, 제공 지자체 **233개 기관**, 연간).
- **엔드포인트**: `https://api.data.go.kr/openapi/tn_pubr_public_cty_park_info_api?serviceKey=…&pageNo=1&numOfRows=100&type=json` → **403/30**. 필터 파라미터명 미검증.
- **필드**: 관리번호·공원명·**공원구분**(근린/어린이/소공원/역사/문화/수변/체육 등, 허용값 목록 미검증)·도로명/지번주소·**위도·경도**·면적·보유시설(운동/유희/편익/교양/기타)·지정고시일·관리기관·전화·**데이터기준일자**.
- **커버리지**: 233 지자체 합산으로 사실상 전국(정본 건수 미검증). 지자체별 기준일 상이.
- **인증**: 활용신청(자동승인·10,000/일 통례, 미검증).

### 19. 문화시설(공연장·박물관/미술관·영화상영관) — B

- **원천·slug(실호출로 확정, 모두 403/30 = 존재·미승인)**:

| 업종 | data.go.kr ID | slug |
|---|---|---|
| 공연장 | `15154966` | `performance_halls` |
| 관광공연장업 | `15154970` | `tourist_performance_halls` |
| 박물관 및 미술관 | `15155146` | `museums_and_art_galleries` |
| 영화상영관 | `15154848` | `movie_theaters` |

  (`museums`·`art_galleries`·`cinemas`·`performance_venues` 는 400/12 → 없음.)
- **엔드포인트**: `https://apis.data.go.kr/1741000/{slug}/info`. 명세는 `serviceKey·pageNo·numOfRows`(필수), `returnType`, `cond[LCPMT_YMD::GTE]`, `cond[SALS_STTS_CD::EQ]`, `cond[BPLC_NM::LIKE]` 등. 프로젝트의 `pageIndex/pageSize/type` 도 lodgings 에서 200 이라 둘 다 통하는 것으로 보임(신규 slug 승인 후 재확인).
- **필드**: 기존과 동일 — `BPLC_NM, BZSTAT_SE_NM, DTL_SALS_STTS_CD/NM(01·02·03), LCPMT_YMD, CLSBIZ_YMD, ROAD_NM_ADDR, LOTNO_ADDR, CRD_INFO_X/Y(EPSG:5174), DAT_UPDT_PNT`. 일 단위(D-2) 갱신, 전국.
- **인증**: 각 ID 페이지 활용신청, 개발·운영 자동승인, 개발 10,000/일. 승인 즉시 `localdata.py` DATASETS 에 4줄 추가로 연결.
- **보조**: 전국박물관미술관정보표준데이터 `15017323`(`tn_pubr_public_museum_artgr_info_api`, 403/30, WGS84, 연간, 영업상태 없음), 전국영화상영관표준데이터 `15045008`(CSV), 문체부 등록공연장 `3075660`(2019 XLS), 문화기반시설 현황 `3075558`(XLSX), 한국문화정보원 총람 조회서비스 `15125097`(자동승인; 경로 `B553457/rgnCltrFcltExmnv1` 은 400/12 → 실제 경로 미검증).

### 20. 공공시설(관공서·행정복지센터·도서관) — C(주민센터) / B(도서관)

- **읍면동 하부행정기관 현황** `15059715`(행안부, `_20251231`, CSV 3,556건, 연 1회): 연번·시도·시군구·읍면동·우편번호·**주소**(좌표 없음). ODcloud `https://api.odcloud.kr/api/15059715/v1/uddi:ce3e099b-fb54-4418-9883-eee49e5a1f90?page=1&perPage=100` → **401**. 지오코딩 필요.
- **관공서(시·군·구청) 전국 좌표 표준데이터**: 찾지 못함(미검증). 「전국공공시설개방정보표준데이터」 `15013117` 은 대관 가능 유휴공간이라 부적합.
- **전국도서관표준데이터** `15013109`(문체부 소관, 지자체·교육청 제공): `https://api.data.go.kr/openapi/tn_pubr_public_lbrry_api` → **403/30**. 파라미터 `serviceKey, pageNo, numOfRows, type`, 필터 `LBRRY_NM, CTPRVN_NM, SIGNGU_NM, LBRRY_SE`. 응답 `LBRRY_NM, LBRRY_SE(공공/작은/어린이 …), RDNMADR, LATITUDE, LONGITUDE(WGS84), REFERENCE_DATE, 운영시간·좌석·장서`. 연간. 자동승인·10,000/일. 휴관·폐관 필드 없음.
- **libsta** 도서관 조회 API 안내 페이지(`libsta.go.kr/board/statapi`) 404, 회원가입 후 매뉴얼 방식 — 미검증.
- **경로**: ① `15013109` 활용신청(도서관) ② `15059715` 활용신청 + 주소 지오코딩(주민센터) ③ 관공서는 카카오 PO3 유지.

### 21. 초·중·고 — B

- **전국초중등학교위치표준데이터** `15021148`(한국교육시설안전원 학구도안내서비스): `https://api.data.go.kr/openapi/tn_pubr_public_elesch_mskul_lc_api` → **403/30**. 파라미터 `serviceKey, pageNo, numOfRows, type`, 필터 `schoolId, schoolNm, schoolSe, operSttus, rdnmadr, latitude, longitude …`. 응답 `schoolId, schoolNm, schoolSe(학교급), fondDate, fondType, bnhhSe(본교/분교), operSttus(운영상태), lnmadr, rdnmadr, cddcNm, edcSportNm, creatDate, changeDate, latitude, longitude(WGS84), referenceDate`. 「수시」 갱신, 전국, 자동승인·10,000/일.
- **NEIS schoolInfo** `https://open.neis.go.kr/hub/schoolInfo?KEY=…&Type=json&pIndex=1&pSize=100&ATPT_OFCDC_SC_CODE=P10` → 키 없이 샘플 200(전북 771건). 응답 `SCHUL_NM, SCHUL_KND_SC_NM, FOND_SC_NM, ORG_RDNMA(도로명주소) …` — **위경도 없음**. 키는 SNS 로그인 후 즉시 발급, 일일 한도 수치 미확인. data.go.kr 미러 `15122275`.
- **교육데이터플랫폼(edss.moe.go.kr → edmgr 이관 중)**: 인증서 불일치로 접근 실패, 「학교별 위치정보」 API 유무 미검증. 운영상태(폐교)는 `15021148` 의 `operSttus` 로 충분.
- **경로**: ① `15021148` 활용신청 → 즉시 연결.

### 22. 대학교(정문) — B(본부) / C(정문)

- **대학 목록**: 전국대학및전문대학정보표준데이터 `15107736`(대교협) `https://api.data.go.kr/openapi/tn_pubr_public_univ_info_api` → **403/30**. 응답 `SCHL_NM, MAINBRANCH_NM, UNIV_SE_NM, SCHL_SE_NM, FNDN_FORM_SE_NM, CTPV_NM, LCTN_ROAD_NM_ADDR, LCTN_LOTNO_ADDR, LCTN_ZIP, FNDN_YMD, CRTR_YMD` — **좌표 없음**, 수시, 자동승인·10,000/일.
- **대학알리미**: data.go.kr `15037507`/`15158963` `https://apis.data.go.kr/B340014/BasicInformationService_2/getComparisonUniversitySearchList?svyYr=2025` → **403/30**(자동승인, 개발 1,000). 구 `openapi.academyinfo.go.kr/openapi/service/rest/SchoolInfoService/getSchoolInfo` 는 200 이나 `resultCode 99 SERVICE KEY IS NOT REGISTERED`. 알리미 자체 OpenAPI 는 회원가입·키 신청. 「교육부_대학교개황리스트」 `15100330` 파일(주소·학교상태, 좌표 없음). 고등교육기관 위치 전용 API 없음(미검증).
- **정문 좌표(실호출)**: 카카오 키워드 `우석대학교 정문` → 200, category `입출구`(127.06738, 35.91487). 전주대·전주비전대·한국농수산대 정문도 `입출구` POI 있음. **전북대학교 정문은 미검색.** V-World 검색 2.0(`api.vworld.kr/req/search?…&type=place`, EPSG:4326) 도 `전북대학교 정문` NOT_FOUND. 국토지리정보원 국가관심지점 POI `15144087`(XLSX, 로그인 다운로드) 의 출입구 포함 여부 미검증.
- **경로**: ① `15107736` 활용신청으로 대학 목록·주소 ② 카카오 `{학교명} 정문` 중 category `입출구` 만 채택 ③ V-World 보강 ④ 나머지는 캠퍼스 대표 좌표 + 「정문 미확인」 표기 ⑤ 장기적으로 NGII POI 출입구 레이어 검토.

---

## 3. 종합 — 바로 연결 가능한 것과 기관 협의가 필요한 것

### 3-1. A — 현재 키로 이미 200 (코드 배선만)
- **#14 버스 운행주기**: TAGO `getSttnThrghRouteList` → `getRouteInfoIem` 의 `intervaltime`. 값이 0/누락인 도시가 많고 서울 미포함이므로 「미확인」 처리 규칙이 필요.
- **#15 터미널 목록**: TAGO `ExpBusInfo/GetExpBusTrminlList`·`SuburbsBusInfo/GetSuberbsBusTrminlList`. 좌표는 명칭 지오코딩.
- **#3 LPG 저장소 후보**·**#7 도시가스 사업자**: 이미 적재 중인 `high_pressure_gas`·`city_gas_companies` 에서 파생(판정 확정용은 아님).
- **#6 도료 후보**: 상가정보 `B553077/api/open/sdsc2`(200). 인허가 아님.

### 3-2. B — 활용신청(자동승인 통례)만 하면 붙는 것 — 신청 목록
| 순서 | 항목 | data.go.kr 페이지 | 승인 후 호출 경로 |
|---|---|---|---|
| 1 | #1 소음진동 | `data/15139233/standard.do` | `api.data.go.kr/openapi/tn_pubr_public_noise_vibration_emission_fclt_api` |
| 2 | #11 CNG | `data/15001508/fileData.do` | `api.odcloud.kr/api/15001508/v1/uddi:928477d9-ab3e-4b97-8731-d4ff962f0570` |
| 3 | #2 LPG 판매소 | `data/15091481/fileData.do` | `api.odcloud.kr/api/15091481/v1/uddi:f40c83ef-d9dd-49b7-a912-bfbdfad65118` |
| 4 | #19 문화시설 4종 | `15154966`·`15154970`·`15155146`·`15154848` | `apis.data.go.kr/1741000/{performance_halls, tourist_performance_halls, museums_and_art_galleries, movie_theaters}/info` |
| 5 | #18 공원 | `data/15012890/standard.do` | `tn_pubr_public_cty_park_info_api` |
| 6 | #17 전통시장 | `data/15012894/standard.do` | `tn_pubr_public_trdit_mrkt_api` |
| 7 | #21 초중고 | `data/15021148/standard.do` | `tn_pubr_public_elesch_mskul_lc_api` |
| 8 | #20 도서관 | `data/15013109/standard.do` | `tn_pubr_public_lbrry_api` |
| 9 | #13 철도역 | `15067652`·`15127532` | ODcloud uddi:29222bc3-…·uddi:c1d09745-… (CSV 즉시 가능) |
| 10 | #16 환승센터 | `data/15034541/standard.do` | `tn_pubr_public_pbtrnspt_rnsit_cnter_api` (14개 기관만) |
| 11 | #22 대학 목록 | `data/15107736/standard.do` | `tn_pubr_public_univ_info_api` (좌표 없음) |
| 12 | #20 주민센터 | `data/15059715/fileData.do` | ODcloud uddi:ce3e099b-… (주소만) |
| 13 | #12 역 좌표 | KRIC data.kric.go.kr 회원가입·키 | `openapi.kric.go.kr/openapi/convenientInfo/stationInfo`·`stationGateInfo` |
| 별도 | #5 safemap IF_0049 | `safemap.go.kr/opna/crtfc/keyAgreeRenew.do` 키 재발급 | `safemap.go.kr/openapi2/IF_0049` |

### 3-3. C/D — 기관 협의·정보공개청구·파일 파싱이 필요한 것
| 항목 | 등급 | 현실적 경로 |
|---|---|---|
| #4 위험물 제조소등 | D | 관할 소방서 허가대장 정보공개청구(open.go.kr) → `15155780` 특정소방대상물 API 포함 여부 검증 → 소방청 제공신청 |
| #8 화약류 저장소 | D | 시도경찰청 정보공개청구(보안상 부분공개) → LH 수기 유지 |
| #9 특정대기유해물질 | D | 대기배출시설(적재 중) 전수 + HAP 여부는 시군구 대장 청구 / 환경부 대기관리과 |
| #7 도시가스 제조소·정압기 | D | 가스공사·도시가스사 정보공개청구(비공개 가능성 높음) |
| #3 LPG 저장소 | C | 고압가스업 저장소 행 「검토」 + 지자체 파일 + 가스안전공사 제공신청 |
| #5 유독물 | C | safemap 키 재발급 → IF_0049 후보 + 경기도 API + 안전원 제공신청 |
| #6 도료류 | C | 상가정보 G21001/G21003/G21099 + 상호 필터(후보) |
| #10 카지노 | C | 협회 18곳 + 문체부 HWP 파싱 → 지오코딩, 연 1회 |
| #12 지하철 출구 좌표 | C | 전국 원천 없음. KRIC 출구 주소 지오코딩 또는 현행 카카오 「N번출구」 유지 |
| #15 터미널 좌표 | C | TAGO ID·명칭 → 지오코딩 |
| #16 환승주차장·정류장 | D | 코레일 `15090378` 역 목록 조인, 나머지 「자료 없음」 |
| #22 대학 정문 | C | 카카오 `입출구` POI → 미확인 표기 |

### 3-4. 기존 문서와 달라진 점
- 9/14 종합의 「LOCALDATA 8종 403」은 **해소됨**(`high_pressure_gas`·`city_gas_companies`·`lodgings` 200). 신규 slug 만 신청하면 된다.
- 9/11 「전남은 위험물 위치 공개」는 틀림 — `15124035` 는 22행 시군 집계, 주소 없음.
- localdata.go.kr 종료(2026-04-16)로 「LOCALDATA 업종 슬러그 확인」은 data.go.kr 「행정안전부_○○ 조회서비스」 페이지에서 해야 한다. 프로젝트 코드 경로는 변경 불필요.
- 가스안전공사 kgsapi 는 `lpg_station` 하나뿐 — 판매소·저장소·CNG 오퍼레이션을 기대하면 안 된다.
- TAGO 노선정보 `intervaltime` 은 문서상 존재하나 전주·천안·제주에서 0/누락 — 15분 판정을 「값 있는 도시 한정」으로 설계해야 한다.
- 원천 부재·조회 실패를 「주변 유해시설 없음」으로 해석할 근거는 없다(전 항목 공통).
