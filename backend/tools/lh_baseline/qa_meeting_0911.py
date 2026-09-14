"""Read-only report/evidence QA; screenshots are separate QA outputs."""
import json,re,sys
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from meeting_0911 import OUT,ROOT,readj,save
from build_meeting_0911 import NAME

sys.stdout.reconfigure(encoding='utf8')
def main():
    p=OUT/(NAME+'.html');soup=BeautifulSoup(p.read_text(encoding='utf8'),'html.parser')
    d=readj(OUT/'comparison.json');m=d['metrics']
    assert m['118']['verdict_equal']==112 and m['118']['score_equal']==76
    assert m['17']['ours']['lh_equal']==5 and m['17']['jys']['lh_equal']==9
    assert m['17']['score_comparable']==13 and m['17']['score_equal']==9
    assert len(soup.select('table.appendix tbody tr'))==118
    assert len(soup.select('img'))==4
    ids={x['id'] for x in soup.select('[id]')}
    assert all(a['href'][1:] in ids for a in soup.select('a[href^="#"]'))
    assert not soup.select('script[src],link[href^="http"],img[src^="http"]')
    assert '\ufffd' not in soup.get_text()
    # Detect current configured secrets without disclosing the values.
    secret_values=[]
    for env in [ROOT/'backend/.env',ROOT/'frontend/.env',ROOT/'frontend/.env.local']:
        if not env.exists():continue
        for line in env.read_text(encoding='utf8').splitlines():
            if not line.strip() or line.lstrip().startswith('#') or '=' not in line:continue
            k,v=line.split('=',1);v=v.strip().strip('"\'')
            if re.search(r'KEY|TOKEN|SECRET|PASSWORD',k,re.I) and len(v)>=16:
                secret_values.append(v)
    hits=[]
    for f in OUT.rglob('*'):
        if f.suffix.lower() not in ['.json','.txt','.html']:continue
        t=f.read_text(encoding='utf8')
        if any(v in t for v in secret_values):hits.append(f.name)
        if re.search(r'(?:serviceKey|api[_-]?key|access_token)=[A-Za-z0-9%+/]{16,}',t,re.I):hits.append(f.name+' (URL parameter)')
    assert not hits,{'credential_file_matches':hits}
    result={'tables':len(soup.select('table')),'images':len(soup.select('img')),'appendix_rows':118,'credential_matches':0}
    with sync_playwright() as pw:
        b=pw.chromium.launch(channel='msedge',headless=True)
        page=b.new_page(viewport={'width':1400,'height':1050},device_scale_factor=1)
        requests=[];page.on('request',lambda r:requests.append(r.url) if r.url.startswith(('http://','https://')) else None)
        page.goto(p.as_uri(),wait_until='load');page.wait_for_timeout(500)
        result['broken_images']=page.locator('img').evaluate_all('(es)=>es.filter(e=>!e.complete||!e.naturalWidth).length')
        result['horizontal_overflow']=page.evaluate('document.documentElement.scrollWidth>innerWidth')
        result['external_requests']=len(requests)
        page.screenshot(path=str(OUT/'qa_top.png'))
        h=page.get_by_role('heading',name='4-3. LH 생활편의성 17건 — 현재 양 앱 실측',exact=True)
        h.scroll_into_view_if_needed();page.screenshot(path=str(OUT/'qa_scores.png'))
        page.locator('.figs figure').nth(1).scroll_into_view_if_needed();page.screenshot(path=str(OUT/'qa_figures.png'))
        page.locator('#agenda').scroll_into_view_if_needed();page.screenshot(path=str(OUT/'qa_agenda.png'))
        page.set_viewport_size({'width':600,'height':900})
        page.goto(p.as_uri());result['mobile_page_overflow']=page.evaluate('document.documentElement.scrollWidth>innerWidth')
        b.close()
    assert not result['broken_images'] and not result['horizontal_overflow'] and not result['external_requests'],result
    save(OUT/'qa_validation.json',result);print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
