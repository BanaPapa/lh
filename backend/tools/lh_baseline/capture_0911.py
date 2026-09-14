"""Actual browser captures in an isolated profile; no edits to app UI or results."""
import argparse,json,sys
from pathlib import Path
from playwright.sync_api import sync_playwright
sys.stdout.reconfigure(encoding='utf-8')
OUT=Path(__file__).resolve().parents[3]/'docs/reports/meeting_20260911'

def main():
    p=argparse.ArgumentParser();p.add_argument('--app',default='both');p.add_argument('--mode',default='inspect');p.add_argument('--id',default='060');args=p.parse_args()
    OUT.joinpath('screens').mkdir(parents=True,exist_ok=True)
    with sync_playwright() as pw:
        b=pw.chromium.launch(channel='msedge',headless=True,timeout=30000)
        page=b.new_page(viewport={'width':1500,'height':1000},device_scale_factor=1)
        for app,url in [('ours','http://localhost/'),('jys','http://127.0.0.1:3000/analyze')]:
            if args.app!='both' and args.app!=app: continue
            page.goto(url,wait_until='domcontentloaded',timeout=60000);page.wait_for_timeout(3500)
            if args.mode=='execute':
                inp=json.loads((OUT/'inputs.json').read_text(encoding='utf8'))
                c=next(c for c in (inp['cases17'] if args.id=='060' else inp['pilot']) if c['seq']==args.id)
            if app=='ours' and args.mode in ('search','execute'):
                address=c['address'].split(',')[0] if args.mode=='execute' else '익산시 부송동 764-10'
                page.get_by_role('textbox',name='사업지 주소 또는 장소').fill(address)
                page.get_by_role('button',name='검색',exact=True).click(); page.wait_for_timeout(6000)
                if args.mode=='execute':
                    page.get_by_role('button',name='주택',exact=True).click()
                    page.get_by_role('button',name='청년' if c['application_type']=='youth' else '일반',exact=True).click()
                    page.get_by_role('button',name='분석 실행',exact=False).click()
                    page.wait_for_function("document.querySelector('.solo-run-button')?.textContent.includes('분석 실행')",timeout=180000)
                    page.wait_for_timeout(1000)
            if app=='jys' and args.mode=='execute':
                page.get_by_role('button',name='주택',exact=True).click()
                page.get_by_role('button',name='청년·기숙사형' if c['application_type']=='youth' else '일반위락',exact=False).click()
                page.get_by_role('textbox').first.fill(c['address'])
                print('JYS PREFILLED',page.get_by_role('button').all_text_contents(),flush=True)
                page.get_by_role('button',name='검토 실행',exact=False).click();page.wait_for_timeout(4000)
            print(app,page.url)
            print(page.locator('body').inner_text()[:13000])
            print('INPUTS',page.locator('input').evaluate_all('(els)=>els.map(e=>({type:e.type,placeholder:e.placeholder,name:e.name}))'))
            print('BUTTONS',page.get_by_role('button').all_text_contents())
            name=f'{app}_{args.id}' if args.mode=='execute' else f'{app}_initial'
            page.screenshot(path=str(OUT/'screens'/f'{name}.png'),full_page=app=='jys')
            (OUT/'screens'/f'{name}.txt').write_text(page.locator('body').inner_text(),encoding='utf8')
            if app=='ours' and args.mode=='execute':
                page.get_by_role('button',name='심사표 열기',exact=False).click();page.wait_for_timeout(800)
                page.screenshot(path=str(OUT/'screens'/f'{name}_stage1.png'))
                print('PANEL BUTTONS',page.get_by_role('button').all_text_contents(),flush=True)
                print('PANEL TABS',page.get_by_role('tab').all_text_contents(),flush=True)
                page.get_by_role('tab',name='2차 생활편의성 배점',exact=False).click();page.wait_for_timeout(800)
                page.screenshot(path=str(OUT/'screens'/f'{name}_stage2.png'))
            if app=='jys' and args.mode=='execute':
                page.get_by_role('button',name='판정 근거 펼치기',exact=False).click();page.wait_for_timeout(600)
                page.screenshot(path=str(OUT/'screens'/f'{name}_detail.png'),full_page=True)
                (OUT/'screens'/f'{name}_detail.txt').write_text(page.locator('body').inner_text(),encoding='utf8')
        b.close()
if __name__=='__main__':main()
