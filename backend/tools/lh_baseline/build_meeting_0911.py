"""Build a self-contained, evidence-backed report in the supplied record's format."""
from __future__ import annotations
import base64, html, json, shutil, zipfile
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup
from meeting_0911 import OUT, INBOX, REF, MEETING, readj, normalize, digest, save

E=lambda s: html.escape(str(s))
V={'pass':'통과*','fail':'제외','review':'검토'}
H={'house':'주택','officetel':'주거용 오피스텔'}
A={'general':'일반','youth':'청년','newlywed':'신혼1'}
NAME='PoC_결과물_검증_기록부_정민재앱_2026-09-11'

def table(headers,rows,cls=''):
    return '<div class="tblwrap"><table class="'+cls+'"><thead><tr>'+''.join('<th>'+E(x)+'</th>' for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(x)+'</td>' for x in r)+'</tr>' for r in rows)+'</tbody></table></div>'
def info(rows): return '<table class="docinfo">'+''.join('<tr><td class="k">'+E(k)+'</td><td>'+v+'</td></tr>' for k,v in rows)+'</table>'
def call(s,cls=''): return '<div class="callout '+cls+'">'+s+'</div>'
def score(r):
    if r['score'] is not None:return str(r['score'])+('†' if r['reference_only'] else '')
    return '<b class="undetermined">미확정</b><br><span class="src">'+str(r['score_min'])+'~'+str(r['score_max'])+'점'+(' · 참고†' if r['reference_only'] else '')+'</span>'
def axes(r):return ' / '.join('미확정' if r['axes'].get(x) is None else str(r['axes'][x]) for x in ['transit','living','education'])
def raw(app,key):return readj(OUT/'raw'/app/(key+'.json'))
def figure(name,caption):
    p=OUT/'screens'/name
    assert p.exists(),p
    data='data:image/png;base64,'+base64.b64encode(p.read_bytes()).decode()
    return '<figure><a href="'+data+'" target="_blank"><img src="'+data+'" alt="'+E(caption)+'"></a><figcaption>'+E(caption)+'</figcaption></figure>'

def build():
    d=readj(OUT/'comparison.json'); m=d['metrics']; g=d['groups']; inp=readj(OUT/'inputs.json'); manifest=readj(OUT/'manifest.json')
    old={r[0]:r for r in inp['reference_tables'][23][1:]}
    reference=BeautifulSoup(REF.read_text(encoding='utf8'),'html.parser')
    meeting=BeautifulSoup(MEETING.read_text(encoding='utf8'),'html.parser')
    css=reference.style.get_text()+'''\nnav.quick{padding:10px 0;border-bottom:1px solid var(--line);font-size:12px}nav.quick a{margin-right:13px;color:var(--info)} .undetermined{color:var(--warn)} .num{font-variant-numeric:tabular-nums} .widefig{display:block;max-width:940px} .widefig figure{margin:14px 0} .widefig img{max-height:none} .hash{font-family:Consolas,monospace;font-size:10px;overflow-wrap:anywhere;word-break:break-all} th{white-space:normal} .appendix td{font-size:11px} .src{overflow-wrap:anywhere} .toolbar{font-size:12px;margin:10px 0} button{padding:5px 12px;cursor:pointer} @media print{.toolbar,.quick{display:none}.widefig img{max-height:155mm;object-fit:contain;object-position:left top} .widefig figure{break-inside:avoid;page-break-inside:avoid} a{color:inherit;text-decoration:none} h2{break-before:auto}.appendix td{font-size:7.4pt}}'''
    out=['<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PoC 결과물 검증 기록부 — 정민재 API 교차검증 앱</title><style>'+css+'</style></head><body><main>']
    add=out.append
    add('<header class="doc"><div class="eyebrow">LH 신축매입약정 심사 지원 PoC · 2026.09.11. 2차 보고 회의자료 대조</div><h1>PoC 결과물 검증 기록부</h1><p>정민재 API 교차검증 앱 기준 · 진용성 로컬 앱 실구동 대조</p><div class="meta">실측일 2026.09.11. · 문서 정리 2026.09.12. | 담당 정민재 · 자동 검증/문서 작성 보조 | 상태: 실측 기록 완료 · 담당자 및 LH 확인 전</div></header>')
    add(call('이 문서의 “현재”는 <b>2026.09.11. 원본 응답과 캡처를 확보한 실행 시점</b>을 뜻한다. 이후 변경된 프로젝트 코드를 이번 실측과 섞지 않았다. 09/11 회의 후 실제 결정·회신은 제공받지 않아 반영하지 않았으며, 제공된 회의안의 미결 상태를 기준으로 작성했다.'))
    add(info([('작성 근거','제공된 「PoC 결과물 검증 기록부_2609.html」의 문서 양식과 A부·B부 기록 구조를 사용. 「LH_회의록_용역2차보고_2026-09-11.html」의 15개 확인 요청에 현재 앱의 실측 근거를 연결했다.'),('실측 범위','정민재 앱과 제공받은 진용성 앱을 실제 실행한 결과. 118건 기본형 + 17건 유형별 표본(혼합유형 포함 18조건) + 동일 PNU 통제 7건. 조준환 LOCUS·LH 기존 HTML 앱은 이번에 재실행하지 않았고, 기존 기록부 수치만 별도 인용한다.'),('운영 원칙','박진주·협회 시설 파일을 정민재 앱에 새로 적재하지 않았다. 현재 설정된 API와 기존 원천 캐시/DB를 그대로 사용했다. 진용성 앱의 동봉 스냅샷도 변경·재적재하지 않았다. 원본 응답을 보존하고 미확정 값을 임의 점수로 채우지 않았다.'),('판독 주의','이 문서는 교차검증 기록이지 LH 최종 승인서가 아니다. 앱의 “통과/적격”은 조회·적용 범위 내 확정 저촉이 없다는 뜻이며, 원천 미연결·수기 확인·필지 불일치를 해소했다는 뜻이 아니다.')]))
    add('<nav class="quick"><a href="#s1">1 기준선</a><a href="#s2">2 표본</a><a href="#s3">3 행렬</a><a href="#s4">4 실측 기록</a><a href="#s5">5 납품 검증</a><a href="#agenda">회의 15항목</a><a href="#appendix">118건 전체</a><a href="#s6">일정·증빙</a></nav><div class="toolbar"><button onclick="window.print()">인쇄 / PDF 저장</button> · 모든 표와 캡처는 문서 안에 포함되어 인터넷 없이 열람 가능</div>')
    add('<div class="sum"><p><b>118건 1차 판정:</b> 정민재 통과 110 · 제외 6 · 검토 2 / 진용성 통과 110 · 제외 8 · 검토 0. 판정 일치 <b>112/118(94.9%)</b>. 서로 다른 6건은 010·025·029·034·084·085.</p><p><b>17건 생활편의성:</b> 정민재 총점 확정 13건 · 미확정 4건 / 진용성 17건 확정. 총점 비교 가능 13건 중 <b>9건 일치</b>. LH 기록 점수와 정확히 같은 건 정민재 <b>5/17</b>, 진용성 <b>9/17</b>.</p><p><b>최우선 확인:</b> 085는 동일 대표필지만 넣으면 진용성도 제외→통과. 084는 주소 “신동”과 원장 좌표로 해소한 “신용동”이 달라 동일 사업지 대조로 확정할 수 없다. 060은 현재 교육 5점·총점 37점으로 과거 33점 기록과 다르다.</p></div>')
    add(call('<b>검증 완료로 단정하지 않는 이유</b><br>118건 기본형에서 정민재 응답에는 사업지마다 13개 유해요소 범주의 dataset_missing이 남았다. 60건은 두 앱의 PNU 집합이 다르며 7건은 첫 PNU부터 다르다. 점수·판정 일치율만으로 원천 완전성, 법적 적격성 또는 납품 전체의 통과를 선언할 수 없다.','warn'))

    add('<h2 id="s1">1. 기준선 — 이번 실행의 고정값</h2>')
    add(table(['항목','정민재 API 교차검증 앱','진용성 LH 심사 지원 앱'],[
      ['실행 대상','C:\\Dev\\lh_mvp · API :8000 / 화면 :80','제공 폴더 LH 심사 지원 앱_v3/app · :3000'],
      ['버전',E(manifest['git_commit'])+'<br>응답 규칙팩 version 1.5 / id lh-rulebook-v1.4','engine offline-1.0 · rule_pack_version 1.0'],
      ['시설 원천','기존 API 연결 + 기존 동기화 캐시/DB. 행안부 인허가는 응답상 로컬 캐시 조회이며 최신 동기화일과 조회일이 다름. “모든 시설을 매번 실시간 호출”로 표현하지 않음.','시설 스냅샷 JB-2026-09-10 · 시설 30,032건(유해 12,587 / 편의 17,445). 폴리곤 14,987 / 점만 15,045.'],
      ['진용성 스냅샷 시각','해당 없음','builtAt 2026-09-10T03:02:00.360Z (한국시간 12:02). 메타데이터 원천명 facilities.xlsx. 기존 0908 실험과 동일 파일로 간주하지 않음.'],
      ['사업지 경계','원장 주소·좌표 → parcels/resolve 결과. 이번 일괄 요청은 118건 모두 1필지. 자동 다필지 합산 대신 현재 해소 결과를 사용.','주소 문자열의 쉼표·생략지번을 기본 엔진이 확장. 118건 중 57건이 2필지 이상. parcel snapshot LSMD_CONT_LDREG_52_202608.'],
      ['거리·출처','응답에 시설 점 좌표 거리와 시설 필지 경계 거리가 섞임. 시설별 measurement_method로 구분.','시설 스냅샷의 폴리곤·기준점에 따라 POLYGON_TO_POLYGON / POLYGON_TO_POINT 사용.'],
      ['승인 표기','응답 status approved는 앱 내부 메타데이터. 이번 회의의 미결 15항목 승인 증거가 아님.','응답 APPROVED와 화면의 “신청유형 코드표 확정 전 임시 매핑”이 공존. 최종 승인 여부 별도 확인 필요.'],
      ['환경 변경','앱 코드·설정·시설 데이터 변경 없음. 진단 스크립트 및 보고서만 추가. 테스트 실행으로 정상 캐시·작업 이력 생성 가능.','기존 실행 인스턴스 사용. ingest·데이터 교체·규칙 편집 없음.'],
    ]))
    add('<p class="src">근거: manifest.json, raw/ours/*의 hazard_review.rule_pack·sources·calculation_note, raw/jys/*의 summary, facilities.meta.json. 원본·코드 주요 파일 9개 SHA-256을 기록했다. API 키·환경변수 값은 보고서에 담지 않았다.</p>')

    add('<h2 id="s2">2. 용어와 표본 — 분모를 섞지 않는다</h2>')
    add(table(['표본/용어','정의·이번 처리'],[
      ['118건 기본형','01_ 매입약정 신청자 전체파일(260430).xlsx의 좌표 시트에서 유효 접수번호·주소·좌표 118행을 추출. 전건 주택·일반으로 실행한 비교 실험이며 실제 신청유형 118건을 재현한 것이 아님. 접수번호는 연속번호가 아니며 119도 포함된다.'],
      ['LH 17건','2026 매입약정 서류심사_생활편의성 35점 이상 17건_26.09.07.작업.xlsx의 신청유형·건물유형·LH 총점을 사용. 사업지 주소/좌표는 118건 원장에 접수번호로 연결. 094 혼합건물은 주택·오피스텔 둘 다 실행하되 통계는 주택 1건만 사용.'],
      ['15건 부분집합','기존 기록부 표 A-12의 15건. 이번 17건에서 111·115를 제외한 부분집합이며 추가 독립 표본이 아니다.'],
      ['실구동 5건','기존 기록부와 같은 001·004·006·041·105. 전건 주택·일반 조건. 17건의 004/006은 오피스텔 및 청년 등 조건이 달라 별도 결과로 취급.'],
      ['실행 건수','두 앱 각각 133개 고유 (접수번호×건물유형×신청유형) 요청. 겹치는 표본은 원본 결과 재사용. 진용성 동일 PNU 통제 실험 7회 추가. 브라우저 대표 화면은 별도 재실행 캡처.'],
      ['통과*','앱 결과 pass / NO_CONFLICT_IN_SNAPSHOT을 한 용어로 표시. 수기 대상·원천 누락·법적 적격을 모두 해결했다는 의미가 아님.'],
      ['미확정 / 참고†','score=null은 총점 미확정. 엔진이 제공한 min~max만 표시. 1차 제외 후 계산한 생활편의성 점수는 참고†이며 매입 가능을 의미하지 않음.'],
      ['점수 일치율','동일 조건에서 확정된 총점끼리만 비교. LH 일치 건수는 전체 표본을 분모로 하고 확정 표본 수도 병기. 거리 KPI 허용오차 통계와 별개.'],
    ]))
    add(call('<b>원장 충돌 — 002</b><br>17건 점수표는 “전주시 완산구 효자동 1243-2”, 118건 원장은 “효자동2가 1243-3”이다. 이번 실행은 후자를 사용했다. 표의 LH 40점 연결은 접수번호 기준 잠정 대조이며, 동일 필지의 수기 정답지임을 확인한 것은 아니다. 002를 제외하면 정민재 LH 정확 일치는 5/16, 진용성 8/16이다.','warn'))

    add('<h2 id="s3">3. A부 — 앱 × 데이터 교차검증 행렬</h2>')
    add('<p>원본 기록부의 행렬 형식을 유지하되 이번 실행과 과거 인용을 구분한다. 정민재 앱에 협회/박진주 시설 파일을 넣은 실험으로 표시하지 않는다.</p>')
    add(table(['앱','현재 API·기존 원천 캐시','진용성 동봉 09/10 스냅샷','협회 v3·박진주 0908 과거 실험'],[
      ['정민재 API 앱','<span class="st st-ok">실측 완료</span><br>133조건 · 118/17/15/5건 대조','<span class="st st-ref">결과 대조만</span><br>시설 스냅샷을 정민재 앱에 주입하지 않음.','<span class="st st-ref">기존 기록 인용</span><br>과거 수치와 현재 수치를 별도 열로 제시.'],
      ['진용성 앱','<span class="st st-na">미실행</span><br>API 원천으로 데이터 교체하지 않음.','<span class="st st-ok">실측 완료</span><br>133조건 + 동일 PNU 7조건','<span class="st st-ref">기존 기록 인용</span><br>기존 기록부의 재적재 실험을 이번 결과로 재사용하지 않음.'],
      ['조준환 LOCUS','<span class="st st-na">미실행</span>','<span class="st st-na">미실행</span>','<span class="st st-ref">인용</span><br>v3 기본형: 제외 8 · 검토 3 · 미발견 107. 이번 재실행 수치 아님.'],
      ['LH 기존 HTML 앱','<span class="st st-na">미실행</span>','<span class="st st-na">미실행</span>','<span class="st st-ref">배경자료</span><br>기존 MVP 결과는 현재 정답 판정이나 이번 실행 증빙으로 사용하지 않음.'],
    ],'mx'))

    add('<h2 id="s4">4. A부 — 셀별 기록</h2><h3>4-1. 정민재 API 앱 × 현재 원천 연결 상태</h3>')
    add(info([('상태·표본','실측 완료 · 2026.09.11. · 133 고유 조건, 오류 응답 0건. 원본 JSON은 raw/ours/에 저장.'),('무엇을 넣었나','사업지 주소·좌표와 건물/신청유형만 전달했다. 시설 데이터셋 파일은 신규 업로드하지 않았다. API·기존 DB·캐시·룰북은 현재 앱 설정을 그대로 사용했다.'),('어떻게 실행','POST /api/hazard-review/parcels/resolve → POST /api/screening/jobs → 완료 응답 저장. include_stage_two_on_fail=true로 제외건도 참고 점수를 보존. 060·085는 화면 검색·유형 선택·분석 실행·심사표 열기를 별도로 수행했다.'),('실측 결과','118 기본형: 통과 110 · 제외 6 · 검토 2. 생활편의성 확정 105 / 미확정 13. 17건: 통과 16 · 제외 1, 총점 확정 13 / 미확정 4. LH 총점 정확 일치 5/17(확정분 5/13), ±2점 9/17.'),('이것의 의미','독립 원천에서 실행되는 현재 앱의 결과를 확보했다. 응답 성공은 데이터 완전성의 보증이 아니다. 118 기본형은 13종 dataset_missing이 전건에 남고, 일부 교통 축도 확정되지 않는다.'),('증빙 파일','raw/ours/*.json · inputs.json · comparison.json · screens/ours_060*.png · screens/ours_085*.png')]))
    missing=g['118'][0]['ours']['missing_labels']
    add(table(['118건 기본형에서 남은 원천 미확보 범주','판독 방법'],[[E(' / '.join(missing[:5])),'공장 상세 분류와 정밀조사 범주. 코드 준비 여부와 실제 데이터 확보 여부는 별개.'],[E(' / '.join(missing[5:])),'“대상 없음”이 아니라 해당 범주 데이터 미확보. 원천 연결 가능 여부에 관한 별도 조사와 구분.']]))
    add('<p class="src">오피스텔은 일부 유해요소 규칙이 적용되지 않아 missing 목록이 줄어든다. 17건에서 missing 발생 사업지가 7건인 것은 118건 기본형보다 데이터가 더 확보되었다는 의미가 아니다. 화면의 connected 표기도 모든 개별 원천의 실시간 HTTP 성공을 이번에 독립 확인했다는 뜻으로 사용하지 않았다.</p>')

    add('<h3>4-2. 진용성 앱 × 제공 폴더의 동봉 스냅샷</h3>')
    add(info([('상태·표본','실측 완료 · 2026.09.11. · 133 고유 조건, 오류 응답 0건. 동일 PNU 7조건 추가.'),('무엇을 넣었나','같은 원장 주소·좌표와 대응 신청유형을 /api/screening/review에 전달. 스냅샷은 JB-2026-09-10 그대로 사용. 기본 실험에서는 명시 PNU를 넣지 않아 주소 기반 다필지 처리가 작동하도록 했다.'),('어떻게 실행','앱 자체 Next.js 검토 API를 직접 호출해 원본 응답 저장. 건물유형 주택/주거용 오피스텔, 신청유형 GENERAL/YOUTH/NEWLYWED, 규칙팩 COMMON/YOUTH_DORM을 대응시킴. 060·085는 실제 브라우저에서도 실행.'),('실측 결과','118 기본형: 통과 110 · 제외 8 · 검토 0. 총점 118건 확정. 17건: 통과 16 · 제외 1, 총점 17건 확정. LH 총점 정확 일치 9/17, ±2점 10/17.'),('이것의 의미','제공된 현행 번들의 출력은 확보했다. dataset_missing 상태 0건이더라도 군부대·사격장 수기 확인 등 제외 범위가 있어 “모든 원천 완비”로 쓰지 않는다. 인터넷 차단 검증이나 빈 PC 재설치 시험은 수행하지 않았다.'),('증빙 파일','raw/jys/*.json · raw/jys_same_pnu/*.json · facilities.meta.json 해시 · screens/jys_060*.png · screens/jys_085*.png')]))

    add('<h3>4-3. LH 생활편의성 17건 — 현재 양 앱 실측</h3>')
    rows=[]
    for r in g['17']:
        c,a,b=r['case'],r['ours'],r['jys']; oldr=old.get(c['seq'])
        note=[]
        if a['score'] is None:note.append('교통 미확정')
        if c['seq']=='002':note.append('원장 주소 충돌')
        if c['seq']=='094':note.append('혼합형: 오피스텔도 36')
        if a['reference_only']:note.append('1차 제외·참고')
        rows.append([c['seq'],E(H[c['housing_type']]+' / '+A[c['application_type']]),c['lh_actual'],score(a),axes(a),score(b),axes(b),E(V[a['verdict']])+' / '+E(V[b['verdict']]),'<br>'.join(note) or '—'])
    add(table(['접수','건물 / 신청','LH','정민재 현재','교통/주거/교육','진용성 현재','교통/주거/교육','1차 정/진','주의'],rows))
    add(call('확정 총점 비교 13건 중 9건 일치(69.2%). 불일치 002·060·077·108. 미확정 079(28~40)·082(25~37)·091(29~38)·095(32~35)는 비교 분모에서 제외했다. 095는 현재 상한 35도 진용성 38보다 낮아 교통 미확정 이외의 주거 축 차이도 남는다.','gold'))
    add('<p class="src">LH 기준점은 제공 점수표의 총점 행에서 읽었다(표본별 정확한 셀은 inputs.json score_source). 094는 건물유형 두 가지를 실행했지만 위 표와 모든 “17건” 통계에서 중복 집계하지 않았다. 082의 생활편의성은 양 앱 모두 1차 제외 이후 참고값이다.</p>')

    add('<h3>4-4. 조준환 국장 기록부와 무엇이 다른가</h3>')
    add('<p>아래 “기존” 열은 제공된 기록부 표 A-12에서 인용했다. 과거 LOCUS·진용성의 사용 데이터/실행시점과 현재 환경이 같지 않으므로 순위나 정확도 개선으로 단정하지 않는다.</p>')
    rows=[]
    for r in g['15']:
        c,a,b=r['case'],r['ours'],r['jys'];o=old[c['seq']]
        rows.append([c['seq'],c['lh_actual'],o[3],o[4],o[5],score(a),score(b),'미확정 전환' if a['score'] is None else ('동일' if str(a['score'])==o[5] else E(o[5])+' → '+score(a))])
    add(table(['접수','LH','기존 LOCUS','기존 진용성','기존 정민재','현재 정민재','현재 진용성','정민재 과거 대비'],rows))
    add(table(['구분','기존 기록부','이번 실측 / 해석'],[
      ['15건 LH 총점 일치','정민재·LOCUS·진용성 각 8/15','정민재 4/15(확정 11건), 진용성 8/15. 미확정 4건을 과거 총점으로 보충하지 않음.'],
      ['060 청년','정민재 교육 1점·총점 33점, LOCUS/진용성 40점','현재 정민재 교육 5점·총점 37점. 폴리텍 반영됨. 남은 진용성 대비 3점 차이는 주거 축 12↔15.'],
      ['002·077 청년','정민재·진용성 각 40점','현재 정민재 39점, 진용성 40점. 정민재 대학 기준점과 진용성 정문 후보 좌표가 달라 교육 4↔5.'],
      ['041 주택·일반','기존 진용성 v3 제외 / 정민재 통과','동봉 09/10 스냅샷 진용성·현재 정민재 모두 통과. 과거 v3 주유소 수록 여부와 구분.'],
      ['095','대표필지와 다필지가 주거 12↔15의 설명으로 제시됨','동일 대표 PNU를 진용성에 넣어도 주거 15 유지. 이번에는 필지 범위만으로 설명 불가. 병원 후보 구성 차이도 확인됨.'],
      ['여유구간','회의안건 15: 정민재 없음','현재 코드 기본 100m이며 실제 응답 calculation_note도 100m. 후보 조회·경계 검토와 최종 판정 전파를 구분해 LH 방식 결정 필요.'],
      ['데이터 시점','박진주 0908 진용성 스냅샷 29,781건(기존 기록 인용)','이번 제공 번들 30,032건. 과거 0908과 현재 번들의 단순 동일시 금지.'],
      ['원천 연결 방식','정민재를 판정 시점 실시간 API 조회로 설명','현재 응답상 행안부 인허가는 기존 동기화 캐시 조회. API+캐시 혼합형으로 정정.'],
    ]))
    add('<p class="src">기존 기록부 4-4·4-6·4-7·4-8, 표 A-12 및 회의자료 확인 요청 7·9·15를 대조했다. 과거 실험을 부정하는 것이 아니라 버전·원천·실행조건이 다른 현재 증거를 구분한 것이다.</p>')

    add('<h3>4-5. 118건 기본형 — 1차 판정이 다른 6건</h3>')
    interpretations={
      '010':'진용성: 한진가스 21.1m. 동일 대표 PNU 통제 후에도 제외 유지 → 다필지만으로 설명되지 않음. 후보/분류/시설 경계 대조 필요.',
      '025':'진용성: (주)케이씨 0m. 동일 대표 PNU 통제 후에도 제외 유지 → 원천 또는 시설 경계·업종 분류 대조 필요.',
      '029':'정민재: 고압가스 제조·충전·공란 업태로 시설종류 미확인. 확정 제외 대신 검토. 진용성은 통과.',
      '034':'정민재: 고압가스 시설종류 미확인으로 검토. 진용성 통과. 고압가스 범위/불확실 처리의 현재 차이.',
      '084':'원장 주소 신동 808-2 ↔ 좌표 해소 신용동 808-2 충돌. 진용성은 좌표가 필지에서 2,660m 떨어진다는 경고. 동일 부지 비교로 확정 불가.',
      '085':'정민재 대표 1필지 통과, 진용성 12필지 합산 근영하이테크 30.1m로 제외. 동일 대표 PNU만 넣으면 진용성도 통과 → 범위 영향 직접 확인.',
    }
    add(table(['접수·주소','정민재','진용성','필지 수 정/진','확인된 사실·남은 확인'],[[r['case']['seq']+'<br>'+E(r['case']['address']),V[r['ours']['verdict']],V[r['jys']['verdict']],str(len(r['ours']['pnus']))+' / '+str(len(r['jys']['pnus'])),E(interpretations[r['case']['seq']])] for r in g['118'] if r['ours']['verdict']!=r['jys']['verdict']]))
    add('<p>양 앱 모두 통과 110건이라는 집계가 같아도 같은 사업지가 통과한 것은 아니다. 1차 전체 일치 112/118은 입력조건을 포함한 출력 비교이며 유해시설 정답률이 아니다. 2차는 비교 가능한 105건 중 76건(72.4%) 총점 일치, 정민재 13건 미확정이다.</p>')

    add('<h3>4-6. 동일 대표 PNU 통제 — 원인을 분리한 7건</h3>')
    controls=[]
    for p in sorted((OUT/'raw/jys_same_pnu').glob('*.json')):
        rec=readj(p);k=p.stem;a=normalize('ours',raw('ours',k));b=normalize('jys',raw('jys',k));z=normalize('jys',rec)
        controls.append([k.split('_')[0]+'<br>'+E(H[rec['case']['housing_type']]+'·'+A[rec['case']['application_type']]),V[a['verdict']]+' / '+score(a),V[b['verdict']]+' / '+score(b),V[z['verdict']]+' / '+score(z),axes(z),str(len(b['pnus']))+' → '+str(len(z['pnus']))])
    add(table(['접수·조건','정민재','진용성 기본','진용성 동일 PNU','통제 후 교/주/교','진용성 필지 수'],controls))
    add(call('<b>통제 방법:</b> 진용성 원천·규칙은 그대로 두고 parcels를 정민재 결과의 PNU 목록만으로 교체해 주소 자동 확장을 막았다. 085의 제외 해소는 필지 범위의 직접적인 영향이다. 060·095·108은 점수 차이가 남아 시설 후보/기준점/분류의 추가 차이가 있다. 010·025도 제외가 유지된다. 084는 통제 후 40→33점으로 바뀌지만 정민재 제외·30점과 여전히 달라, 주소 충돌을 먼저 정정한 뒤 다시 검증해야 한다.','gold'))

    add('<h3>4-7. 대표 차이의 시설·거리 근거</h3>')
    add(table(['사례','정민재 현재 원본 응답','진용성 현재 원본 응답','해석'],[
      ['060 주택·청년','익산병원 2,030.48m · 사업지 경계→시설 점. 주거 12. 한국폴리텍 익산캠퍼스 731.46m, 교육 5. 총점 37.','익산병원 1,920.77m · 경계→경계. 주거 15, 교육 5. 총점 40.','2km 경계 부근 병원 측정 차이. 동일 PNU 통제 후에도 총점 40. “기능대학 제외”는 현행 결과 설명으로 부적절.'],
      ['108 오피스텔·일반','전북대학교병원 2,142.67m · 시설 점. 주거 12, 총점 35.','전북대학교병원 1,880.85m · 시설 폴리곤. 주거 15, 총점 38.','병원 기준점/경계 결정 필요. LH 35와 맞는다는 사실만으로 점 측정이 정답이라고 단정하지 않음.'],
      ['095 오피스텔·신혼1','병원 후보 정읍아산병원 2,481.88m. 주거 12. 교통 미확정, 총점 32~35.','정읍한국병원 777.43m와 정읍아산병원 2,282.77m 수록. 주거 15, 총점 38.','동일 대표 PNU에도 주거 15 유지. 다필지 외에 병원 후보 구성·시설 분류·거리 측정 차이를 확인할 필요.'],
      ['002·077 오피스텔·청년','전주대학교 1,266.60m / 1,284.16m. 교육 각 4점.','전주대학교 경영행정·교육·문화산업대학원 기준점 915.71m / 941.93m. 교육 각 5점.','서로 같은 정문 좌표인지 확인 필요. 대학원 행의 본교 정문 공유가 타당한지 서면 기준 확인. 002는 원장 주소 충돌도 별도 존재.'],
    ]))
    add('<p class="src">거리값은 이번 원본 응답에서 옮겼다. 병원의 “정문”임을 현장에서 확인한 것이 아니므로 정민재 시설 점을 일괄 정문이라고 부르지 않았다. API 장소검색 후보에 터미널 인근 영업점 등이 포함된 사례도 있어 후보 포함 여부와 실제 배점 채택 여부의 별도 QA가 필요하다.</p>')

    add('<h3>4-8. 실제 실행 화면 — 060·085 대표 사례</h3>')
    add('<p>격리된 브라우저에서 주소 검색·조건 선택·실행 후 촬영한 원본 화면이다. 숫자나 경고를 합성·수정하지 않았다. 정민재 화면의 지도 키 연결 대기는 촬영 환경의 실제 상태이며, 백엔드 필지/점수 계산과 지도 배경 표시를 구분한다. 화면은 대표 사례 증빙이고 118건 전체 증빙은 원본 JSON이다. 진용성 085는 화면 캡처를 완료하지 못했으므로 원본 API 응답과 동일 PNU 통제 결과로 제시한다.</p>')
    add('<div class="figs widefig">'+figure('ours_060.png','그림 A-1. 정민재 앱 · 060 주택/청년 · 대표 1필지 · 총점 37/40. 지도 키 연결 대기 상태 그대로 캡처.')+figure('ours_060_stage2.png','그림 A-2. 정민재 060 생활편의성 상세 · 교통 20 / 주거 12 / 교육 5.')+figure('jys_060.png','그림 A-3. 진용성 앱 · 060 주택/청년 · 주소의 4필지 합산 · 총점 40/40.')+figure('ours_085.png','그림 A-4. 정민재 앱 · 085 주택/일반 · 대표 1필지 · 통과·35점.')+'</div>')

    add('<h2 id="s5">5. B부 — 납품물 단계별 검증 기록</h2>')
    add('<p>원본의 0~8단계 구성을 유지한다. 이번 담당 범위는 독립 API 교차검증과 그 기록부이며, 수행하지 않은 납품물 전체 시험을 완료로 표시하지 않는다.</p>')
    add(table(['#','단계·산출물','검증 방법·판정 기준','이번 결과·증빙','상태·확인'],[
      ['0','기준 고정','소스·원장·동봉 스냅샷 해시, 실행 입력 및 버전 기록','manifest.json 주요 9파일 SHA-256, inputs.json, 원본 응답. 실시간 API 전체를 고정한 골든셋은 아님.','부분 완료 / 담당자 확인 전'],
      ['1','표준 데이터셋 납품형','신규 빈 환경 재적재 후 판정·양식 재현','이번에는 동봉 스냅샷을 읽기만 함. 협회 v3.1 재적재 성공률은 기존 보고서의 별도 실험.','이번 범위 미검증'],
      ['2','오프라인 웹앱','외부망 차단·외부 요청 0건·빈 PC 실행·출력 재현','진용성 로컬 번들 검토 API와 대표 화면 실행만 확인. 인터넷 차단 시험은 안 함. 정민재 앱은 외부 원천 교차검증용.','부분 확인 / 오프라인 보증 불가'],
      ['3','엑셀 출력 2종·결과지','118건 출력값↔원본 JSON 전건 대조·인쇄 검증','본 HTML와 원본 JSON 제공. LH 통합표 및 118시트 출력물은 새로 만들거나 검증하지 않음.','미검증'],
      ['4','KPI 측정표','서면 확정된 거리 허용오차·정답 필지로 측정','총점/판정 일치 통계만 확보. 주소 충돌·PNU 집합 차이·거리 기준 미확정. 이번 일치율은 정식 거리 KPI가 아님.','기준 결정 대기'],
      ['5','역량이전 도구 3종','문서만으로 제3자 재기동·심사·출력 성공','운영 매뉴얼·룰 편집 가이드·프롬프트 템플릿의 콜드리드 수행 안 함.','미검증'],
      ['6','독립 구현 교차검증·회귀','118/17/15건 입력·출력 비교, 상이 건 원인 분해','각 앱 133조건 + 동일 PNU 7조건. 118건 판정 112 일치. 대표 UI 재실행 확인. 전건 2회 반복 재현성 시험은 안 함.','이번 교차검증 실측 완료 / 회귀 확정 전'],
      ['7','최종 수행결과 보고서','LH 09/11 결정·회신·정식 KPI 반영','현재 문서는 회의 전 실측 기록부. 15개 미결사항을 승인된 것으로 표시하지 않음.','결정 반영 대기'],
      ['8','보안매체 인도 패키지','실물 목록·해시·자격증명 점검·빈 PC 재현','보고서와 검증 원본 증빙 패키지만 작성. 전체 납품 ZIP/보안매체·빈 PC 구동은 대상 아님.','전체 납품 미검증'],
    ]))

    add('<h3 id="agenda">5-1. 오늘 LH 확인 요청 15항목 — 현재 앱의 회신 근거</h3>')
    agenda=[
      ['1','공장 판정 모수','118 기본형의 공장 상세 범주가 미확보로 남음. 기존 회의자료의 “정민재 808행”을 현재 전수 모수로 재검증하지 않았음.','등록공장 전체/조건 성립분 중 판정 모수, 자동 제외와 검토 표시 범위를 서면 확정.'],
      ['2','휴업 시설','041 현재 양 앱 통과. 기존 LOCUS/v3의 휴업 주유소 제외 기록과 다름. 현행 조회 누락과 규칙상의 포함/보류 정책은 구분 필요.','휴업·상태 공란·폐업 각각의 포함/검토/제외 원칙 확정.'],
      ['3','고압가스 자가설비','현재 검토는 029·034(종류 미확인). 104·105는 양 앱 통과로 과거 LOCUS 검토와 다름.','자가설비와 제조·충전·저장·판매의 인정 범위, 미확인 시설 보류 방식 결정.'],
      ['4','자동차용 LPG','현재 정민재 적용 위험물 기준 50m. 084는 별도 주소 충돌이 있어 거리 기준 검증 정답 사례로 사용 불가.','25m/50m 선택을 문서화하고 경계 사례 재실행.'],
      ['5','석유대체연료','현재 정민재 석유판매·대체연료 계열 25m 표기. 004 주택·일반 제외 / 오피스텔·일반 통과로 건물유형 영향도 큼.','25m/50m와 건물유형별 적용을 확정.'],
      ['6','생활숙박업','이번 17건에 다자녀 없음. 일반형 통과 결과로 생활숙박 규칙을 검증했다고 볼 수 없음.','생활숙박 포함 여부 + 다자녀 실제 표본 제공.'],
      ['7','청년 교육·폴리텍','회의자료의 정민재 폴리텍 제외 설명은 현재와 다름. 060 현재 폴리텍 후보 731.46m, 교육 5점·총점 37.','대학/초중고, 기능대학 포함 여부 및 인정 정문을 확정.'],
      ['8','대형 시설 기준점','060 병원 점 2,030m↔경계 1,921m, 108 병원 점 2,143m↔경계 1,881m. 각 주거 3점 차이.','정문/시설점/필지경계 선택 및 인정 좌표 원천 확정.'],
      ['9','다필지 사업지','085 동일 PNU 통제에서 제외→통과 확인. 095는 동일 PNU에도 38점 유지하여 다필지만으로 설명되지 않음.','신청 필지 목록 원본 확정·합집합 적용 여부, 7건 대표 PNU 충돌 해소.'],
      ['10','신청서 시설/전수 탐지','17건에서 LH와 다른 점수가 다수 존재. 091은 이번 교통 미확정이므로 과거 “앱 +3”을 현재 확정 결과로 인용하지 않음.','전수 탐지 여부와 시설 인정 범위·중복 제거 기준 확정.'],
      ['11','대학 정문·역 출구','002·077에서 대학 기준점 차이로 교육 4↔5. 원본 응답만으로 좌표가 실제 정문인지 최종 확인 불가.','정문·다중출구 목록과 좌표 근거를 서면 확정.'],
      ['12','추가 신청유형','17건 표본에 다자녀·신혼2·고령자 없음. 이번 실행도 그 공백을 메우지 않음.','유형별 심사 사례·LH 배점과 근거 시설 제공.'],
      ['13','매입 공고문 원본','이번 기록은 제공 코드/회의자료의 현재 적용값을 기술했으며 법령·공고 해석의 확정 검토는 아님.','원본 수령 후 룰북 문언·임계거리 재대조.'],
      ['14','KPI 허용오차','이번은 판정·총점 비교. 제출 시설별 수기 거리의 ±50m/상대10% 매칭을 새로 계산하지 않음.','정답 거리와 허용오차 확정 후 별도 KPI 측정.'],
      ['15','여유구간','“정민재 없음”과 달리 현재 코드·실제 응답에 100m. 경계 후보 상태와 최종 판정 반영은 별도 단계.','150m/100m/기타 폭 및 검토 표시의 최종 상태 반영 기준 확정.'],
    ]
    add(table(['#','회의 확인 요청','현재 앱 기준 확인 사실','필요한 결정·후속'],agenda))
    add(call('<b>회의에서 먼저 공유할 정정 사항</b><br>① 060 현재 37점(교육 5) ② 정민재 여유구간 기본 100m ③ 현재 원천은 API+기존 캐시 혼합 ④ 095 총점 미확정 및 동일 PNU에서도 주거 차이 지속 ⑤ 084 주소·좌표 충돌 ⑥ 기존 스냅샷과 제공 번들 날짜/행 수가 다름. 이후 LH 결정에 따라 양 앱에 같은 조건을 적용하고 전건 다시 비교해야 한다.','lh'))

    add('<h2 id="appendix">붙임 1. 118건 기본형 전수 결과</h2><p>전건 주택·일반. 정민재/진용성의 실제 신청유형 전체 결과가 아니다. 점수 뒤 †는 1차 제외 후 참고값. 필지 집합 차이와 원천 미확보를 포함한 출력 비교다.</p>')
    add(table(['접수','원장 주소','정민재 1차','정민재 총점','진용성 1차','진용성 총점','필지 수 정/진','PNU 집합'],[[r['case']['seq'],E(r['case']['address']),V[r['ours']['verdict']],score(r['ours']),V[r['jys']['verdict']],score(r['jys']),str(len(r['ours']['pnus']))+' / '+str(len(r['jys']['pnus'])),'같음' if set(r['ours']['pnus'])==set(r['jys']['pnus']) else '다름'] for r in g['118']],'appendix'))
    add('<h3>붙임 1-1. 대표 PNU부터 다른 7건</h3>')
    rows=[]
    for r in g['118']:
        if r['ours']['pnus'][0]==r['jys']['pnus'][0]:continue
        a=raw('ours',r['key']);b=raw('jys',r['key']);par=a['result']['site']['parcels'][0]
        rows.append([r['case']['seq'],E(r['case']['address']),E(par.get('address',''))+'<br>'+E(r['ours']['pnus'][0]),E(r['jys']['pnus'][0]),E(' / '.join(b['result']['site'].get('notes',[])))])
    add(table(['접수','원장 주소','정민재 해소 주소·PNU','진용성 첫 PNU','진용성 원본 경고/설명'],rows))
    add('<p>첫 PNU가 다르다는 사실만으로 어느 앱이 잘못됐다고 단정하지 않는다. 다필지 순서·주소 해석·좌표 우선순위가 섞여 있으며, 특히 084는 법정동 자체가 다른 것으로 확인됐다. 승인된 신청 PNU 목록을 받은 뒤 재검증해야 한다.</p>')

    add('<h3>붙임 2. 기존 실구동 5건의 현재 재실행 결과</h3>')
    add(table(['접수','조건','정민재 1차 / 점수','진용성 1차 / 점수','주의'],[[r['case']['seq'],'주택·일반',V[r['ours']['verdict']]+' / '+score(r['ours']),V[r['jys']['verdict']]+' / '+score(r['jys']),'17건 표본과 건물/신청유형 다름' if r['case']['seq'] in ['004','006'] else '과거 총점으로 대체하지 않음' if r['ours']['score'] is None else '—'] for r in g['5']]))

    add('<h2 id="s6">6. 일정과 후속 작업</h2>')
    mt=meeting.find_all('table')[-1]
    schedule=[[c.get_text(' ',strip=True) for c in row.find_all(['th','td'])] for row in mt.find_all('tr')][1:]
    add(table(['회의자료상 시점','예정 업무(회의 확정 전)'],[[E(x) for x in r] for r in schedule]))
    add('<ol><li><b>회의 전:</b> 본 기록부와 118건·17건 JSON 공유, 담당자가 002·084 원장 충돌 및 수정된 060/여유구간 설명 확인.</li><li><b>LH 결정 직후:</b> 승인된 신청 필지·시설 기준점·휴업/가스/공장 범위로 검증조건을 고정. 이번 결과를 덮어쓰지 않고 새 실행일 폴더에 회귀 결과 생성.</li><li><b>원천 보완:</b> 정민재 미확정 4건의 교통 및 13종 유해요소 원천 공백을 구분해 해결. 원천 없음과 시설 없음의 표시를 혼동하지 않도록 검토.</li><li><b>납품 전:</b> 실제 납품 번들을 외부망 차단·빈 PC 조건으로 실행하고 B부 미검증 항목, 정식 거리 KPI와 추가 유형 표본을 별도로 완료.</li></ol>')
    add('<h3>6-1. 증빙 목록과 재현 방법</h3>')
    records=[readj(p) for app in ['ours','jys'] for p in (OUT/'raw'/app).glob('*.json')]
    assert len(records)==266 and all('result' in r for r in records)
    assert all(not r['result'].get('hazard_review',{}).get('demo',False) for r in records if r['app']=='ours')
    starts=[r['started_at'] for r in records]; ends=[r['finished_at'] for r in records]
    add('<p>기본 배치 실행 구간: '+E(min(starts))+' ~ '+E(max(ends))+' (KST). 대표 브라우저 캡처는 배치 이후 별도 실행했다. 파일명 규칙은 <code>접수번호_house|officetel_general|youth|newlywed.json</code>이다.</p>')
    add(table(['파일·폴더','내용'],[
      [NAME+'.html','본 기록부. 스크린샷 내장 · 외부 스타일/폰트/스크립트 요청 없음.'],
      ['정민재_118건_결과.json / 정민재_17건_결과.json','회의자료 요청용 원본 결과 묶음. 17건은 대표 유형 17개, 094 오피스텔 추가 결과는 raw에 별도 보관.'],
      ['comparison.json','118/17/18조건/15/5건 비교와 집계. 미확정·범위·원본 PNU 보존.'],
      ['inputs.json / manifest.json','원장 추출 입력·유형·LH 점수 원본 셀 / 주요 원본·코드 SHA-256. 매도자명 등 불필요한 신청자 정보는 추출하지 않음.'],
      ['raw/ours · raw/jys','각 133개 실행 원본 JSON. 요청·응답·시각·소요시간·정민재 작업 ID 포함.'],
      ['raw/jys_same_pnu','7개 통제 실험. 정민재와 같은 PNU만 넣은 진용성 원본 응답.'],
      ['screens · tools','원본 화면 캡처/읽은 텍스트 · 실행/캡처/집계/문서 생성 스크립트.'],
    ]))
    add('<p class="src">재집계: backend 가상환경에서 <code>python tools/lh_baseline/meeting_0911.py summarize</code>. 보고서 재생성: <code>python tools/lh_baseline/build_meeting_0911.py</code>. 외부 원천은 시점에 따라 달라질 수 있으므로 재실행 전 새 증빙 디렉터리를 사용해야 한다. 증빙 ZIP의 SHA256SUMS.json은 수록 파일 무결성 확인용이다.</p>')
    add('<h3>6-2. 원본·주요 코드 해시</h3>')
    add(table(['원본 파일','크기(bytes)','SHA-256'],[[E(Path(f['name']).name),str(f['size']),'<span class="hash">'+f['sha256']+'</span>'] for f in manifest['files']]))
    add('<div class="callout warn"><b>최종 판단</b><br>현재 정민재 앱의 독립 교차검증 결과를 확보했으며 진용성 번들과의 차이 및 원인 일부를 재현했다. 그러나 원천 미확보, 교통 미확정, 신청 필지 충돌, 측정 기준과 LH 미결 항목이 남아 있다. “두 앱이 완전히 동일하다” 또는 “최종 납품 검증이 끝났다”는 결론은 이번 증거로 내리지 않는다.</div>')
    add('<p class="end">위와 같이 실측 당시 실행 상태와 상이 사유를 기록함.</p><div class="sign">2026년 9월 12일 정리 (실측 9월 11일)<br>정민재 API 교차검증 앱 실측 기록<br><span class="src">담당자 확인·LH 결정 사항 반영 전</span></div><footer><span>PoC 결과물 검증 기록부 · 정민재 앱 기준</span><span>실측 / 기존 기록 인용 / 미검증을 구분한 기록</span></footer></main></body></html>')
    output=OUT/(NAME+'.html');output.write_text('\n'.join(out),encoding='utf8')
    for group,name in [('118','정민재_118건_결과.json'),('17','정민재_17건_결과.json')]:
        save(OUT/name,{'generated_at':d['generated_at'],'sample':group,'conditions_note':'118건은 주택·일반 공통 조건. 17건은 LH 표본 유형. 시설 파일 신규 적재 없음.','records':[raw('ours',r['key']) for r in g[group]]})
    shutil.copy2(output,INBOX/output.name)
    print(json.dumps({'report':str(output),'copy':str(INBOX/output.name),'bytes':output.stat().st_size},ensure_ascii=False))
    return output

def package():
    files=[p for p in OUT.rglob('*') if p.is_file() and p.suffix!='.zip' and p.name!='SHA256SUMS.json' and not p.name.startswith('qa_')]
    hashes={str(p.relative_to(OUT)).replace('\\','/'):digest(p) for p in files}
    save(OUT/'SHA256SUMS.json',hashes);files.append(OUT/'SHA256SUMS.json')
    target=OUT/(NAME+'_증빙.zip')
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as z:
        for p in files:z.write(p,p.relative_to(OUT))
        for name in ['meeting_0911.py','capture_0911.py','build_meeting_0911.py']:
            z.write(Path(__file__).with_name(name),'tools/'+name)
    shutil.copy2(target,INBOX/target.name)
    print('EVIDENCE',target,target.stat().st_size)

if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf8')
    build()
    if '--package' in sys.argv:package()
