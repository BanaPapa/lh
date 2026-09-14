# 유해요소 판정 데이터 원천 대조 감사 (2026-08-27)

박진주 대표님(데이터 담당) 확정 체크리스트 정본(`jeonbuk_checklist_260826.xlsx`,
시트 `기준`, 57행)과 현재 코드(`backend/app/hazard_review/rulebook.py` 25종,
`backend/app/services/localdata.py` 데이터셋 레지스트리)를 1:1로 대조한 결과다.

분류 태그
- `[원천 불일치]` 코드의 엔드포인트/슬러그가 체크리스트와 다름 (추정으로 넣은 값 포함)
- `[미연결]` 체크리스트 "가능" + 원천 명시인데 코드에 어댑터/데이터셋 없음
- `[누락]` 체크리스트에 있는데 25종에 아예 없음
- `[코드에만]` 체크리스트에 없는데 코드에 있음
- `[미확보 확정]` 체크리스트가 "불가/찾지못함"으로 확정 (코드 `missing`이면 정상)

---

## A. 핵심 발견 요약

1. **주유소(`gas_station`)·CNG(`cng_station`) 슬러그는 추정값이었다.** 코드에 있던
   LOCALDATA 슬러그 `gas_stations`, `cng_stations`는 명명규칙으로 추정한 것으로,
   `apis.data.go.kr/1741000`에 실제로 존재하는지 확인된 바 없다. 체크리스트상 두
   원천은 **API가 아니라 CSV**다(주유소 = `산업통상부_전국 주유소 등록현황`,
   CNG = `25_cng_stations.csv`). → 추정 슬러그 제거함.
2. **석유대체연료판매업(`petroleum_alt_fuel_retailers`) 미연결.** 체크리스트가
   가목(석유대체연료)·사목(도료류 판매소) 양쪽에서 명시했는데 코드에 데이터셋이
   없었다. → 연결함.
3. **테마파크가 체크리스트상 3종(종합/일반/기타)인데 코드는 1종으로 뭉쳐 있었다.**
   각기 다른 엔드포인트다. → 3종으로 분리·연결함.
4. **무도가 체크리스트상 2종(무도장/무도학원)인데 코드는 1종이었다.** → 2종 분리·연결함.
5. **"사. 도료류 판매소"가 룰북 §6.2 위험물 8종에 아예 없었다.** → `paint_retailer`
   카테고리로 추가함(석유및석유대체연료판매업 부분 확인).
6. **군부대·사격장은 체크리스트에 아예 없다.** 룰북 v1.4 매트릭스는
   "화장장·군부대 500m"를 두고 있어 코드에 `military_base`·`shooting_range`
   `missing` 카테고리가 있다. **원천 없음이 체크리스트로도 재확인됨**(아래 D절).

---

## B. 위험시설(공장/위험물/주유·가스/위락/숙박/화장장) 행별 대조

| 체크리스트 각목·세부 | 판정가능 | 체크리스트 원천 | 현재 코드 매핑 | 분류 | 조치 |
| --- | --- | --- | --- | --- | --- |
| 화장장 | 가능 | CSV `보건복지부_화장시설 현황` | `crematorium` applied·CSV(미적재) | 정상(CSV 대기) | 유지. 적재 전 `dataset_missing` 정상 |
| 공장 가. 특정대기유해물질 | 불가 | 미확보 | `factory_air_specific` manual | [미확보 확정] | 유지 |
| 공장 나. 대기배출 1~3종 AND 공장 | 가능 | API `air_pollution_facility_installation` + factoryON(CSV) | `factory_air_1_3` applied datasets=`air_pollution` | 일치 | 유지 |
| 공장 다. 4~5종 고시업종 AND 공장 | 가능 | 위 API + factoryON + 고시업종목록(CSV) | `factory_air_4_5` applied datasets=`air_pollution` | 일치(고시업종 CSV 후처리 미연결) | 유지 |
| 공장 라. 소음배출시설 AND 공장 | 가능 | API `tn_pubr_public_noise_vibration_emission_fclt_api`(data.go.kr openapi) + factoryON | `factory_noise` applied, 소음 API 어댑터 없음 | [미연결] | 문서화(확인 필요). 소음 표준데이터 어댑터 미구현 |
| (추가) 지식산업센터 제외 | 가능 | CSV `한국산업단지공단_전국지식산업센터현황` | `service.py` 예외로직만, 원천 없음 | [미연결](CSV) | 문서화. CSV 적재 경로 명시 |
| 위험물 가. 주유소 | 가능 | **CSV** `산업통상부_전국 주유소 등록현황` | `gas_station` datasets=`gas_stations`(추정) | **[원천 불일치]** | 추정 슬러그 제거. CSV 원천 명시 + 오피넷 보조 |
| 위험물 가. 석유대체연료판매 | 가능 | API `petroleum_alt_fuel_retailers` | 없음(oil_retailer가 oil만) | **[미연결]** | 연결. `oil_retailer` datasets에 추가 |
| 위험물 가. 석유판매소 | 가능 | API `oil_retailers` | `oil_retailer` datasets=`oil_retailers` | 일치 | 유지 |
| 위험물 나. LPG 충전소 | 부분가능 | API `B410019/kgsapi`(가스안전공사) | `lpg_station` applied·KGS 어댑터 | 일치 | 유지 |
| 위험물 나. LPG 판매소 | 가능 | CSV `가스안전공사 LPG판매소 현황` | `lpg_retailer` applied·CSV(미적재) | 정상(CSV 대기) | 유지 |
| 위험물 나. LPG 저장소 | 불가 | 미확보 | `lpg_storage` missing | [미확보 확정] | 유지 |
| 위험물 다. 위험물 제조·저장·취급소 | 불가 | 미확보 | `hazmat_facility` missing | [미확보 확정] | 유지 |
| 위험물 라. 일반도시가스업 | 가능 | API `city_gas_companies` | `high_pressure_gas` partial, 데이터셋 없음 | **[미연결]** | 연결. datasets에 `city_gas_companies` 추가 |
| 위험물 라/바. 고압가스업 | 가능/부분 | API `high_pressure_gas` | `high_pressure_gas` partial, 데이터셋 없음 | **[미연결]** | 연결. datasets에 `high_pressure_gas` 추가 |
| 위험물 마. 유독물 | 불가 | 미확보 | `toxic_substance` missing | [미확보 확정] | 유지 |
| **위험물 사. 도료류 판매소** | 부분가능 | API `petroleum_alt_fuel_retailers` | **없음** | **[누락]** | `paint_retailer` 카테고리 추가·연결 |
| 위험물 아. 도시가스 제조시설 | 가능 | CSV `가스안전공사 가스제품 제조업소` | `city_gas_plant` applied·CSV(미적재) | 정상(CSV 대기) | 유지 |
| 위험물 자. 화약류 저장소 | 불가 | 미확보(연도별 시설수만) | `explosive_storage` manual | [미확보 확정] | 유지 |
| 위험물 차. 유사시설 | 불가 | 미확보 | (룰북에 별도 없음) | [미확보 확정] | 조치 없음 |
| (LH기준) CNG 충전소 | 부분가능 | **CSV** `25_cng_stations.csv` | `cng_station` datasets=`cng_stations`(추정) | **[원천 불일치]** | 추정 슬러그 제거. CSV 원천 명시 |
| (LH기준) LNG 충전소 | 불가 | 미확보 | (룰북에 별도 없음) | [미확보 확정] | 조치 없음 |
| 일반 숙박시설 | 가능 | API `lodgings` | `general_lodging` datasets=`lodgings` | 일치 | 유지 |
| 위락 가. 단란주점 | 가능 | API `singing_bars` **AND** 건축물대장 `1613000/BldRgstHubService` | `singing_bar` datasets=`singing_bars`, 건축물대장 교차확인 미구현 | 부분 [미연결] | 데이터셋 유지. 건축물대장 AND는 미구현(확인 필요) |
| 위락 나. 유흥주점 | 가능 | API `entertainment_bars` | `entertainment_bar` datasets=`entertainment_bars` | 일치 | 유지 |
| 위락 다-1. 종합테마파크업 | 가능 | API `comprehensive_amusement_facilities` **AND** 건축물대장 | `theme_park` 1종으로 뭉침 | **[원천 불일치/누락]** | 분리·연결. 건축물대장 AND 미구현 |
| 위락 다-2. 일반테마파크업 | 가능 | API `general_amusement_facilities` | 없음 | **[누락]** | 분리·연결 |
| 위락 다-3. 기타테마파크업 | 가능 | API `amusement_facilities_other` | 없음 | **[누락]** | 분리·연결 |
| 위락 마-1. 무도장 | 가능 | API `dance_halls` | `dance_hall`에 무도학원과 뭉침 | [원천 불일치] | 분리·연결 |
| 위락 마-2. 무도학원 | 가능 | API `dance_academies` | 없음 | **[누락]** | 분리·연결 |
| 위락 바. 카지노 | 불가 | 미확보 | `casino` missing | [미확보 확정] | 유지 |

## C. 코드에만 있는 것 (체크리스트에 없음)

| 코드 데이터셋/카테고리 | 근거 추정 | 분류 | 조치 |
| --- | --- | --- | --- |
| `localdata.gas_stations` (슬러그) | 직전 작업 추정값 | [코드에만/원천 불일치] | 제거 |
| `localdata.cng_stations` (슬러그) | 직전 작업 추정값 | [코드에만/원천 불일치] | 제거 |
| `localdata.tourist_accommodations` | 과거 관광숙박 카테고리 잔재(§6.5에서 판정 제외됨) | [코드에만] | 제거(어느 카테고리도 참조 안 함) |
| `localdata.construction_waste` (`construction_waste_disposal`) | 체크리스트에 없는 건설폐기물처리업. 어느 카테고리도 참조 안 함 | [코드에만] | 제거 |

## D. 룰북 v1.4 vs 체크리스트 불일치 (원천 없음 재확인)

| 룰북 v1.4 항목 | 체크리스트 유무 | 상태 |
| --- | --- | --- |
| `military_base` (군부대, 500m) | **체크리스트에 없음** | 룰북 매트릭스 `cremation_military`에는 군부대 500m가 있으나, 데이터 담당 체크리스트에는 군부대 원천 자체가 없다. **→ 2026-08-27 협의로 판정 제외 확정**(`judgment_excluded=true`, `not_applicable`). 리스트에는 "판정 제외" 배지로 남기되 판정·`status_counts`에는 불참. |
| `shooting_range` (사격장, 500m) | **체크리스트에 없음** | 룰북 라벨은 "화장장·군부대·사격장"이나 체크리스트엔 사격장 없음. **→ 2026-08-27 협의로 판정 제외 확정**(군부대와 동일 처리). `RB14-CREMATION-MILITARY`는 실질적으로 화장장만 판정. |
| `화장장` | 체크리스트 있음(CSV) | 일치. 룰북 500m 적용, 원천은 CSV 적재 대기 |

---

## E. 이번에 실제로 연결한 것 / 못한 것

### 연결함 (체크리스트 "가능" + 엔드포인트 명시)
- `petroleum_alt_fuel_retailers` (석유대체연료판매업) → `oil_retailer`, `paint_retailer`
- `city_gas_companies` (일반도시가스업) → `high_pressure_gas`
- `high_pressure_gas` (고압가스업) → `high_pressure_gas`
- `comprehensive_amusement_facilities` (종합테마파크) → `theme_park_comprehensive`
- `general_amusement_facilities` (일반테마파크) → `theme_park_general`
- `amusement_facilities_other` (기타테마파크) → `theme_park_other`
- `dance_halls` (무도장) → `dance_hall`
- `dance_academies` (무도학원) → `dance_academy`

### 원천 제거 (추정 슬러그)
- `gas_stations`, `cng_stations` — 실엔드포인트 미확인 추정값. 두 종류는 CSV 적재 경로로 전환. 적재 전 `dataset_missing` 정상.
- `tourist_accommodations`, `construction_waste` — 미참조 잔재 제거.

### 연결하지 못함 (원천 미확보이거나 API가 아님 → CSV/수기 대기)
- 화장장, LPG 판매소, 도시가스 제조시설, 주유소, CNG 충전소, 지식산업센터, 국토부 고시업종목록 : **모두 CSV** 원천. API 조작 금지 원칙에 따라 코드에 URL 넣지 않음. CSV 적재 경로로 남김.
- 소음진동배출시설 : 체크리스트는 `data.go.kr/openapi/tn_pubr_public_noise_vibration_emission_fclt_api`(1741000 계열 아님). 별도 어댑터 필요 → 미구현, 확인 필요.

## F. 확인 필요 (미결)
1. **테마파크 3종·무도학원 엔드포인트의 `/info` 접미사.** 체크리스트에 종합/일반/기타
   테마파크업과 무도학원업은 `/info` 없이 표기됨(예: `.../comprehensive_amusement_facilities`).
   1741000 계열 관례는 `/{slug}/info`라 코드에서 데이터셋별로 `info_suffix` 플래그를
   두어 체크리스트 표기 그대로(접미사 없음) 요청하도록 했다. 실제 적재 시 200 응답 확인 필요.
2. **건축물대장 교차확인(AND).** 단란주점·테마파크는 `1613000/BldRgstHubService`로
   제2종 근린생활시설/운동시설 비해당을 AND로 확인해야 한다. 현재 미구현. 별도 어댑터
   설계 필요 → 오케스트레이터 판단 요청.
3. **소음진동배출시설 어댑터**(공장 라목). 별도 API 계열이라 어댑터 미구현.
4. **군부대·사격장** 원천 존재 여부. 룰북엔 있으나 체크리스트엔 없음. → **2026-08-27 협의로 판정 제외 확정**(`judgment_excluded`). 원천 확보 시 재도입 여부만 후속 확인.
5. **factoryON 등록공장·국토부 고시업종목록** CSV 적재. 공장 AND 최종 매칭률에 직결.

---

## G. 미구현 / 미확정 / 검증필요 표기 규칙 (2026-08-27)

세 가지를 화면에 배지로 명시한다. `data_state`(원천 상태 축)와는 **별개 축**인
`implementation_state` 로 내보내고, 각 배지에 `implementation_note`(사유 한 줄)를 함께 싣는다.

| 배지 | `implementation_state` | 의미 | 해당 대상 |
| --- | --- | --- | --- |
| **미구현** | `not_implemented` | 어댑터/파이프라인이 아직 없음 | 건축물대장 교차확인(`singing_bar`·테마파크 3종), 소음진동배출시설(`factory_noise`), factoryON CSV 적재(공장 AND: `factory_air_1_3`·`factory_air_4_5`·`factory_noise`·`factory_adjacent`) |
| **미확정** | `unconfirmed` | LH/팀 확인 대기 | 룰북 §8 `pending_items` 7건(별도 패널) + 카테고리 단위 미확정 |
| **검증필요** | `needs_verification` | 엔드포인트/데이터는 등록됐으나 실응답 미확인 | `localdata` `verified=False`(테마파크 3종·무도학원 `dance_academy`) |

- 우선순위: **미구현 > 검증필요**. 두 사유가 겹치는 종류(테마파크 3종)는 배지를 `미구현`으로 내되 `implementation_note`에 검증필요 사유도 함께 적는다.
- 미확정은 카테고리 배지가 아니라 룰북 §8 `pending_items` 패널의 "미확정" 라벨로 노출한다.
- 판정 제외(군부대·사격장)는 위 세 축과 별개인 `judgment_excluded` 로 "판정 제외" 배지를 단다.

## G. 이번 리뷰 반영 (2026-08-27)

Codex 리뷰 지적(원천 준비상태·위락 AND·공장 factoryON)을 코드로 확정했다.

1. **원천 준비상태를 데이터셋별로 판정.** `FacilityStore.ready_datasets()` 를 두고
   `service._category_connected` 가 전역 `available` 이 아니라 카테고리가 의존하는
   데이터셋의 적재 여부를 본다. 다른 데이터셋만 적재돼 있으면 해당 카테고리는
   `dataset_missing` 이며 `no_conflict_in_snapshot` 으로 둔갑하지 않는다(룰북 §11).
2. **위락 건축물대장 AND(§F.2) 반영.** 단란주점·테마파크 3종은 `requires_building_register=True`
   로 표시하고, 교차확인 어댑터가 미구현이라 25m 이내여도 `exclusion_match` 가 아니라
   `review_required` 로 남긴다. 유흥주점·무도장·무도학원은 AND 조건이 없어 그대로 둔다.
   건축물대장 어댑터 자체는 여전히 미구현(별도 설계 필요).
3. **미검증 엔드포인트(§F.1) 플래그.** `LocalDataSet.verified` 를 추가해 테마파크 3종·
   무도학원의 `/info` 없는 경로를 `verified=False` 로 표시한다. 적재 전까지 해당 카테고리는
   `dataset_missing` 이되 note 를 `엔드포인트 미검증 — 적재 시 응답 확인 필요` 로 붙여
   "장애"와 "미검증"을 구분한다.
4. **factoryON(§F.5) dataset_missing 확정.** 공장 나·다·라목과 `factory_adjacent` 에
   `required_datasets=("factory_registry",)` 를 걸어, factoryON 등록공장 CSV 가 적재되기
   전까지 전부 `dataset_missing` 이 되게 했다. note = `factoryON 등록공장 CSV 미적재 — 적재 시 AND 판정`.
   factoryON 어댑터는 지어내지 않았다(CSV 수동 다운로드 원천).

여전히 미결(별도 설계·확인 필요): 건축물대장 교차확인 어댑터, 소음진동배출시설 어댑터,
군부대·사격장 원천, factoryON·국토부 고시업종목록 CSV 적재.

---

## H. 2026-08-28 실호출 갱신 — 신규 원천 연결·버그 수정

앞선 감사(2026-08-27)는 스냅샷이다. 2026-08-28 실호출로 아래를 확정했다.
확인한 것과 확인 못 한 것을 구분해 적는다.

### H.1 오피넷 인증 파라미터 버그 (실호출 확인)
- `opinet.py` 의 `stations_around`·`station_detail` 이 인증 파라미터를 `code` 로
  보냈다. 명세는 `certkey` 다. **파라미터 이름이 틀리면 200 을 주면서 결과만
  0건으로 비운다 — 예외가 안 나 조용히 실패한다.** → 두 메서드 모두 `certkey` 로 수정.
- 회귀 테스트(`tests/test_opinet.py::TestAuthParameter`)로 전송 파라미터 이름을 고정.
- 실호출 검증: 전주시청 좌표 기준 1km 1건 · 5km 72건(0건 아님 확인).

### H.2 생활안전지도 IF_0033 주유시설 (실호출 확인 · 연결)
- 경로 `http://www.safemap.go.kr/openapi2/IF_0033`. **http→https 302 리다이렉트라
  `follow_redirects=True` 필수**(안 하면 302 HTML 을 받아 JSON 파싱이 조용히 실패).
- 요청변수 `serviceKey·numOfRows(1000)·pageNo·returnType(JSON)`. `numOfRows=1` 은 실패.
- 좌표 `x·y` 는 **EPSG:3857**. 역산 검증(x=14108535→경도126.74, y=4501386→위도37.45)으로
  주소(인천 남동구 만수동)와 일치 확인. `gis_x_coor·gis_y_coor` 는 **좌표계 미상 · 미사용**.
- `lpg_yn`·`poll_div_co`(주유소 상표)·`gpoll_div_co`(충전소 상표)로 주유소(1차 23)·
  LPG충전소(1차 11)를 갈라 채운다. 겸업은 둘 다 표시.
- 실호출 검증: `resultCode 00`·totalCount 14,417. 어댑터 `services/safemap.py`,
  회귀 테스트 `tests/test_safemap.py`(302·EPSG:3857·분류).
- **미확인**: `uni_cd` 가 오피넷 `UNI_ID` 와 같은 체계로 보인다(A00xxxxx). 중복 제거에
  `source_record_id` 우선키로 쓰되, 확신이 없어 거리 기준을 보조로 함께 둔다.

### H.3 국립중앙의료원 종합병원 (실호출 확인 · 연결)
- 경로 `http://apis.data.go.kr/B552657/HsptlAsembySearchService/getHsptlMdcncListInfoInqire`
  (기관코드 B552657). 응답 XML. 지역 필터는 `Q0`(시도)뿐이라 시도 단위로 받아 캐시.
- `dutyDivNam` 이 종류. 심사표 '종류=종합병원'만 인정 → `종합병원`·`상급종합병원`만 남긴다.
  상급종합 인정 여부는 LH 미확정이라 `is_tertiary` 로 구분해 함께 계산.
- 좌표 `wgs84Lat·wgs84Lon`(WGS84). 시도는 카카오 역지오코딩 첫 토큰으로 구한다
  (전주 좌표 → `전북특별자치도`, API `Q0` 와 정확히 일치 확인).
- 실호출 검증: 세종특별자치시 종합병원 2건. 어댑터 `services/ncmc_hospital.py`,
  회귀 테스트 `tests/test_ncmc_hospital.py`. 2차 `hospital` 시설군을 카카오 근사에서
  이 원천으로 교체(원천 없으면 카카오 근사로 폴백).

### H.4 전국 화장시설 API (실호출 확인 · 연결, 1차 39)
- 경로 `https://apis.data.go.kr/1352000/ODMS_DATA_05_1/callData05_1Api`. **허용
  파라미터는 `serviceKey·pageNo·numOfRows·apiType` 넷뿐** — `type` 등을 더 넣으면
  `INVALID_REQUEST_PARAMETER_ERROR`. localdata 클라이언트(항상 `type=json`)를 재사용하지
  않고 별도 경로로 호출한다. `apiType=JSON`.
- **좌표 미제공** → 카카오로 주소 지오코딩(실측 샘플 8/8 성공). 지오코딩 실패 건은
  삭제하지 않고 `geocode_failures` 로 격리(룰북 §7⑤).
- 실호출 검증: `resultCode 00`·totalCount 62(전국). 어댑터 `services/crematorium.py`,
  회귀 테스트 `tests/test_crematorium.py`. **원칙: API 정본 · CSV(전북 5건) 보조.**

### H.5 LOCALDATA 추가 승인 (실호출 확인 · 승인·미적재)
- `large_scale_retail_stores`(대규모점포): 200 · totalCount 4,183. 2차 `retail`
  시설군을 이 원장(facility_store)에 직결하도록 `amenities.py` 수정. 전통시장은
  미확보라 대규모점포만 반영하고 note 로 남긴다. **sync 적재 전까지 미적재.**
- `specific_high_pressure_gas`(특정고압가스업, 바목): 200 · totalCount 11,250.
  `localdata.py` 레지스트리에 추가하고 `high_pressure_gas` 카테고리 datasets 에 연결.
  응답 스키마 표준(BPLC_NM·CRD_INFO_X/Y·SALS_STTS_CD·MNG_NO)이라 `parse_record` 재사용.
  **sync 적재 전까지 미적재.**

### H.6 우선순위 원칙 (사용자 명시)
- **데이터 적재와 API 연동이 둘 다 가능하면 API 를 먼저 적용한다.** CSV 는 API 가
  없거나 API 가 못 주는 범위를 메우는 보조다. 화장장(39)·주유소(23)·LPG충전소(11)를
  이 원칙에 따라 API 정본으로 두고 CSV 를 보조로 내렸다.

### H.7 건축물대장 교차확인 — 설계 확정 · 구현 대기 (미착수)
- 참고 구현: `BanaPapa/R8_AptReport` 의 `BldRgstHubService`
  (`BASE_BR="https://apis.data.go.kr/1613000/BldRgstHubService"`).
- 표제부 오퍼레이션 `getBrTitleInfo` 가 **`mainPurpsCdNm`(주용도명)·`etcPurps`(기타용도)**
  를 준다 — 「제2종 근린생활시설 비해당」 판정에 필요한 필드.
- 요청 파라미터 `sigunguCd(5)·bjdongCd(5)·platGbCd(0/1)·bun(4)·ji(4)`. 시설마다 PNU 가
  있으니 PNU 를 이 5개로 분해하면 그대로 호출된다.
- 함정: 참고 구현의 `api_call` 은 `serviceKey` 를 URL 인코딩하지 않고 붙인다(이미
  인코딩된 키 이중 인코딩 방지). 성공 판정은 `resultCode == "00"`.
- **이번 범위에 넣지 않았다.** 절반짜리 어댑터를 남기지 않기 위함. 단란주점(30)·
  테마파크 3종(§6.4)의 건축물대장 AND 는 여전히 `review_required` 로 남는다.
- **[2026-08-28 갱신]** 위 "미착수"는 8/27 스냅샷이다. 8/28 세션에서 `building_register.py`
  어댑터가 실제로 구현·배선됐다 — 아래 I절 참조.

---

## I. 2026-08-28 배선 완료 갱신 — 신규 연결·구조적 제약·Codex 리뷰

H절(2026-08-28 오전)은 개별 원천 실호출 확인 기록이다. 이후 같은 날 세션에서
어댑터들을 실제 판정 엔진에 배선했고, 코드 리뷰(Codex)를 거쳐 반영했다.
아래는 그 최종 상태다.

### I.1 이번 세션에 연결된 신규 원천

- **생활안전지도 IF_0033**(주유소·LPG충전소) — `services/safemap.py`. H.2 실호출
  확인 후 `hazard_review/service.py`에 배선해 1차 11(LPG충전소)·23(주유소) 판정에
  실제로 쓰인다.
- **보건복지부 전국 화장시설 `ODMS_DATA_05_1`**(62건) — `services/crematorium.py`.
  H.4 확인 후 1차 39(화장장) 판정에 API 정본으로 배선, CSV(전북 5건)는 보조.
- **국립중앙의료원 종합병원** — `services/ncmc_hospital.py`. H.3 확인 후 2차 50
  (종합병원) 판정에 배선, 카카오 근사를 대체.
- **오피넷**(`code`→`certkey` 버그 수정) — `services/opinet.py`. H.1 에서 확인한
  인증 파라미터 버그를 수정해 주유소(23) 보조 원천으로 배선.
- **LOCALDATA 10종 sync 적재** — `python -m app.sync_facilities` 로 15·16·18·19·
  25·36·37·32·33·34·48(고압가스업·특정고압가스업·도료류판매·일반테마파크·기타테마
  파크·무도학원·종합테마파크·일반테마파크·무도장·대규모점포 등)를 적재. `data/facilities.db`
  17종 172,749건 중 이번 세션에 신규 적재된 부분.
- **연속지적도**(`LSMD_CONT_LDREG_52_202608.shp` → `cadastral_jeonbuk.sqlite`,
  3,888,333필지 · 2.10GB · EPSG:5186) — `services/cadastral_local.py`. 사업지·시설
  경계를 임시 폴리곤이 아니라 실제 지적경계(`parcel_polygon`)로 판정하게 배선.
- **factoryON PNU 577** — `services/local_sources.py`. 표준본 공장 903행을
  PNU 기준 중복 제거해 577개로 배선. 공장 나목(2·3) AND·공장 인접(9) 판정에 사용.
- **건축물대장**(`getBrTitleInfo`) — `services/building_register.py`. H.7 "미착수"를
  뒤집고 실제로 구현·배선. 위락 가목 단란주점(30)·다목 테마파크 3종(35)의 「제2종
  근린생활시설·운동시설 비해당」AND 조건을 해당/비해당/확인불가 3값으로 판정.
- **소음진동(미승인 · CSV 보조)** — API(`tn_pubr_public_noise_vibration_emission_fclt_api`)는
  403 미승인 상태 그대로이나, 전북 CSV(`08_noise_vibration_facilities.csv`, 146건)를
  보조 원천으로 배선해 공장 라목(7) 카테고리를 `dataset_missing`에서 `review_required`로
  전환. exclusion_match 확정은 여전히 불가(I.3 참조).

### I.2 미승인으로 남은 것

- **소음진동배출시설 API** `api.data.go.kr/openapi/tn_pubr_public_noise_vibration_emission_fclt_api`
  — **403 · 활용신청 필요**(LH 회신 대기 #10). 전북 CSV 146건이 보조로 동작 중이나
  API 승인 전까지 전국 커버리지는 확보되지 않는다.

### I.3 구조적 제약 3건 (원천을 더 구해도 코드로 못 푸는 것)

1. **소음 원천 스키마에 소음도·방음시설 필드가 없다.** `noisVbrtMainCn` 146건이
   전건 공란 — 라목 "50dB 이하·방음시설 예외" 확인이 불가능하다. 따라서 소음
   AND 공장(factoryON)이 성립해도 `exclusion_match` 에 도달할 수 없고,
   `review_required` 로 남는 것이 정상 상태다(LH 회신 대기 #8).
2. **[2026-08-29 정정] 공장 다목은 PNU 로 AND 가 성립한다.** 8/28 스냅샷은 factoryON·
   표준본 공장·소음 원천을 잇는 키가 지번 주소뿐이라 §3 위반으로 다목·라목을 확정하지
   않는다고 적었다. 이는 **설계서 §7.2 절차를 문자열 유사매칭으로 오독**한 것이다. 실제
   절차는 법정동코드로 PNU 를 결정적으로 조립하고 좌표를 연속지적도에 얹어 단일 필지일
   때만 인정하는 것으로, §3 이 금지하는 것이 아니라 §3 이 요구하는 PNU 우선키를 외부
   조회 없이 만들어내는 절차다. `services/pnu_resolver.py` 로 구현해 factoryON 7,378/7,983
   (92.4%)·표준본 공장 PNU 5,582(고시업종 2,837)·소음 109/146 을 확정했다. 따라서 **다목은
   판정에 들어온다.** 라목만 아래 3-1 사유로 여전히 확정 불가다(J절 참조).
3. **건축물대장 표제부에 용도별 바닥면적이 없다.** `platArea`·`archArea`·`totArea`
   는 모두 건물 전체 면적이라, 단란주점 150㎡ 같은 용도별 면적 기준을 판정할
   수 없다. 「제2종 근린생활시설 비해당」(해당/비해당) 판정은 3값으로 나오지만,
   면적 종속 판정은 `확인불가`로 고정된다(LH 회신 대기 #5).

### I.4 Codex 리뷰 6건 반영 (2026-08-28)

1. 생활안전지도 미분류 시설(`poll_div_co`·`lpg_yn` 공란)을 주유소/LPG충전소로
   단정하던 로직 제거 — 임계거리 이내여도 `exclusion_match` 대신
   `review_required` + "원천에 시설 구분 정보 없음" note.
2. `factory_noise`(공장 라목) 연결 조건을 「소음 AND factoryON」으로 강화 —
   소음 원천만으로 확정하지 않고 factoryON PNU 매칭을 함께 요구.
3. NCMC(국립중앙의료원) 카카오 역지오코딩 실패를 빈 피드로 흡수하지 않고
   실패로 전파 — "병원 없음"과 "조회 못 함"(`missing`)을 구분.
4. `factory_adjacent`(공장 인접) 연결 판정 기준을 "파일 존재"에서
   "좌표·PNU 유효 레코드 1건 이상"(현재 PNU 577)으로 강화 — 나·다목
   factoryON AND 게이트도 동일 기준 적용.
5. 연속지적도(`cadastral_local`) 조회를 동기 I/O 그대로 이벤트 루프에서 돌리지
   않고 `asyncio.to_thread` 로 이동.
6. `SAFEMAP_API_KEY` 설정 저장 누락 수정(`config.py` `safemap_api_key` 필드
   추가) + 명세와 모델이 불일치하면 임포트 시점에 실패하도록 강화(조용한 스키마
   드리프트 방지).

### I.5 건축법 별표1 세부업종 매핑 — 미확정 (판정규칙 담당 확정 대기)

건축물대장 `etcPurps`(기타용도)에 세부업종명만 있고 별표1 근생 구분(제1종/제2종)이
명시되지 않는 경우가 있다. 아래는 후보 목록이며 **확정 전까지 전부 `확인불가`**로
남는다.

- 제2종 근린생활시설 후보: 일반음식점 · 노래연습장 · 안마시술소 · 다중생활시설 ·
  사진관 · 게임제공업 · 총포판매소
- 제1종 근린생활시설 후보: 의원 · 치과의원 · 한의원 · 이용원 · 미용원 · 일용품 소매점

LH 회신 대기 #9(건축법 별표1 세부업종의 근생 구분 확정)와 동일 사안이다.

---

## J. 2026-08-29 갱신 — 공장 다목 배선·PNU 확정·성능

I절(2026-08-28)까지 다목은 「주소매칭 §3 위반」으로 보류돼 있었다. 8/29 세션에서 그 판단이
오독이었음을 확인하고 다목을 배선했으며, 판정 성능을 실측·개선했다. 확인한 것만 적는다.

### J.1 공장 다목 배선 — PNU 확정 파이프라인 (정정)

- **정정 사유.** 종전 서술 「factoryON↔표준본 공장 연결키가 지번 주소뿐 → §3 위반」은
  박진주 대표님 표준 데이터셋 제작 절차(설계서 §7.2)를 문자열 유사매칭으로 오독한 것이다.
  §7.2 는 문자열 매칭이 아니라 **법정동코드로 PNU(19자리)를 결정적으로 조립하고, 좌표를
  연속지적도에 얹어 단일 필지일 때만 인정**하는 절차다. 외부 조회 없이 재현된다. 즉 §3 이
  요구하는 PNU 우선키를 실제로 만들어내는 방법이며, §3 이 금지하는 대상이 아니다.
- **구현·배선.** `services/pnu_resolver.py` 로 §7.2 를 구현해 판정 엔진에 배선했다.
  결과는 `data/factory_pnu_resolution.v1.json` 에 캐시된다(스키마 v1).
- **실측 (2026-08-29).**
  - factoryON PNU 확정 **7,983건 중 7,378건 (92.4%)** · 실패 605
    (`JIBUN_NOT_IN_CADASTRAL` 422 / `BONBUN_NOT_IN_CADASTRAL` 117 / `NO_JIBUN` 66) · 검토 0
  - 표준본 공장 PNU **577개 → 5,582개**(그중 국토부 고시업종 공장 **2,837개**)
  - 소음 PNU 확정 **146건 중 109건 (74.7%)** · 검토 37 · 실패 0
- **결과.** 다목(4~5종 AND 국토부 고시업종 AND 공장)이 PNU AND 로 판정에 들어온다.
  라목만 J.3 사유로 여전히 `exclusion_match` 불가다.

### J.2 성능 — 판정 1회 298.1초 → 7~8초 (콜드 1회차는 30초대)

세 가지를 고쳤다. 개선 전 298.1초였다. 2026-08-29 재측정(동일 프로세스에서 연속 3회)은
**31.3초 / 7.3초 / 7.6초** 였다.

- **1회차가 느린 것은 판정 로직이 아니라 외부 API 콜드 연결이다.** 프로파일에서 1회차는
  윈도우 IOCP 폴링(`GetQueuedCompletionStatus`) 에 24.5초·5,812회, `getaddrinfo` 158회,
  TLS 핸드셰이크 334회가 잡힌다. kakao·vworld·opinet·safemap·화장시설 등 다수 외부 API 에
  최초 DNS+TLS 연결이 몰리는 구간이다. 2회차부터는 연결이 재사용돼 폴링이 510회로 떨어진다.
- **따라서 정상 상태(steady state) 비용은 7~8초다.** 이 값이 실사용 기준이다.
- 콜드 1회차는 외부 API·망 상태에 따라 흔들린다. 앱 코드를 전부 되돌려 같은 스크립트로 재도
  32.0초가 나와, 우리 변경과 무관함을 확인했다.

> **정정(2026-08-29).** 종전 「12.5초, 3회 연속 동일」 서술은 콜드 구간이 유난히 빨랐던
> 측정을 정상 상태로 적은 것이다. 재현되지 않아 위 실측으로 바꾼다.

1. **SSL 컨텍스트 공유.** 어댑터 16개가 호출마다 httpx 클라이언트를 새로 만들어 인증서를
   매번 파싱했다. 판정 1회에 143회·176초로 **전체의 59%**였다. 프로세스당 1회로 바꿨다.
2. **지적도 R-Tree.** 복합 인덱스 `(min_lat, max_lat, min_lng, max_lng)` 는 첫 열만 범위를
   좁히고 나머지는 잔여 필터라, 전주 좌표 기준 390만 행 중 **63%를 훑어 1회당 1,204.9ms**
   였다. R-Tree 적용 후 **6.14ms**. 결과 동일(실파일 사본 300개 지점 누락 0건).
   인덱스 추가 후 DB 는 2.10GB → **2.50GB**.
3. **PNU 확정 캐시.** 원천·법정동표·지적도 서명으로 키를 만들어 하나라도 바뀌면 재계산한다.
   캐시를 데우는 중에는 공장 카테고리가 `dataset_missing` 으로 나온다.
   **해소(2026-08-29).** 종전 캐시 키는 `mtime:size` 뿐이라 내용이 바뀌었는데 타임스탬프·
   크기가 같으면 낡은 PNU 를 썼다. 지금은 **내용 해시(sha256)+절대경로**(`content_signature`)
   와 **resolver 알고리즘 버전**을 키에 넣는다(`factory_pnu_resolution.v2.json`). 64MiB 이하
   (법정동표·공장 원장)는 전량 해시, 연속지적도 색인(2.5GB)은 헤더·중간·말미 1MiB 부분 해시다.
   **[남은 한계] 부분 해시는 그 세 구간 밖의 변경을 이론상 놓칠 수 있다** — 주석·테스트에 명시했다.

### J.3 라목 — 여전히 `exclusion_match` 불가 (변함없음)

소음 원천에도 PNU 가 부여돼 공장 AND 는 성립한다(146 중 109 확정). 그럼에도 라목이 확정에
이르지 못하는 것은 **매칭 문제가 아니라 원천 스키마의 한계**다. 전국소음진동배출시설
표준데이터에 소음도·방음시설 필드가 없어(`noisVbrtMainCn` 146건 전건 공란) 50dB·방음시설
예외를 확인할 수 없다. §3 「확인 불가 조건은 추정하지 않는다」에 따라 `review_required` 가
정상이다(LH 회신 대기).

### J.4 함정 — `legal_dong_path` 결측이 다목을 조용히 죽인다

`legal_dong_path`(법정동코드표)가 비어 있으면 PNU 조립이 **전건 실패**(`NO_JIBUN` 7,983)해
다목이 조용히 죽는다. 오류도 나지 않고 카테고리가 `dataset_missing` 이 될 뿐이다. 실제로
이 때문에 J.2 의 확정 캐시가 「확정 0건」을 저장한 사고가 있었다. 재발 방지로 로컬 원천 경로
6종(법정동코드표·표준셋·원본·연속지적도 SHP/DB·소음 CSV)을 `backend/.env.example` 에 드러내고,
각 경로가 비면 어떤 판정이 죽는지 주석으로 적었다.

### J.5 시설 다필지 — 규칙은 확정, 실동작은 단일 필지 (구분해 적는다)

룰북 §3 은 「시설이 여러 필지를 점유하면 각 필지까지 재고 **최소값**」으로 규칙을 확정했다
(조준환 국장님 2026-08-26 요청). 그러나 **엔진 실동작은 아직 단일 필지다.**

- 엔진에 다필지 분기(`occupied_parcels`)는 있으나, 이를 **프로덕션 경로에서 채우는 곳이 없다.**
  원천이 「대표지번 외 N필지」의 나머지 필지를 열거해주지 않기 때문이다.
- 그래서 현재 실판정은 여전히 **대표 좌표가 포함된 단일 필지**만 본다.
- 「다필지 판정 적용 완료」로 읽히게 쓰지 않는다. **규칙 확정(§3)과 실동작(단일 필지)을 구분한다.**
- 점유 필지를 열거해주는 원천 확보가 후속 과제다(박진주 대표님 확인 필요).

### J.6 남은 리스크 (수정 진행 중 — 확정 수치는 나오면 재기록)

Codex 리뷰가 지적한 판정 안전망 결함이다. 수정이 진행 중이며 확정 전까지 리스크로만 남긴다.

1. **부분 스냅샷.** 한 카테고리가 여러 원천을 쓸 때 일부가 실패해도 다른 원천이 0건이면
   `no_conflict_in_snapshot` 이 날 수 있었다 — 조회 못 한 원천을 「충돌 없음」으로 둔갑.
2. **워밍업 중 번들 교체 레이스.** 판정이 빈 번들로 후보를 모은 뒤 워밍업이 끝나면, 연결성
   검사가 채워진 번들을 봐 조회하지 않은 원천이 「충돌 없음」이 될 수 있었다.
3. **PNU 캐시 키(J.2-3).** `mtime:size` 만이라 내용 동일 크기 변경을 놓친다 — 가장 위험한
   실패 지점. 내용 해시·알고리즘 버전 추가로 강화 중.
4. **정문 통필지 안전장치 역작동.** PNU 만으로 정문을 지정하면 5만㎡ 이상 필지의 임의 대표점을
   쓰는데, 실제 정문이 반대편이면 거리가 짧아진다 — 안전장치가 막으려던 왜곡이 재발할 수 있다.
5. **정문 대학명 매칭.** 수기 지정한 대학명과 카카오 `place_name` 이 다르면(예: 「전주대」 vs
   「전주대학교」) 지정이 **조용히 무시되고** 좌표 폴백으로 떨어진다. 거리를 짧게 만드는 방향은
   아니라 오판정은 아니나, 담당자 지정이 반영되지 않는 문제다.
