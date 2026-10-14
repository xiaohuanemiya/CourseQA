"""Read-only browser verification of the live administrator graph explorer."""
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright


def main():
    base='http://127.0.0.1:8820'
    output=Path('artifacts/graph')
    output.mkdir(parents=True,exist_ok=True)
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':1600,'height':1100})
        page.on('pageerror',lambda e:errors.append(str(e)))
        response=page.request.post(base+'/api/auth/login',data={'username':'admin',
            'password':dotenv_values('.env')['ADMIN_PASSWORD']},headers={'X-Requested-With':'calculus-studio'})
        assert response.ok
        page.goto(base+'/admin/graph',wait_until='networkidle')
        expect(page.get_by_role('heading',name='图数据库',exact=True)).to_be_visible()
        expect(page.locator('.graph-canvas canvas')).to_be_visible(timeout=20000)
        page.screenshot(path=str(output/'explorer.png'),full_page=True)
        page.get_by_role('tab',name='节点列表').click()
        rows=page.locator('.graph-main .el-table__body tbody tr')
        assert rows.count()>0
        rows.first.click()
        expect(page.get_by_role('button',name='探索邻域',exact=True)).to_be_visible()
        page.get_by_role('button',name='设为起点',exact=True).click()
        page.get_by_role('button',name='设为终点',exact=True).click()
        page.get_by_role('button',name='查找最短路径',exact=True).click()
        expect(page.get_by_role('button',name='退出路径',exact=True)).to_be_visible()
        page.get_by_role('button',name='退出路径',exact=True).click()
        page.get_by_role('button',name='探索邻域',exact=True).click()
        expect(page.get_by_role('button',name='退出邻域',exact=True)).to_be_visible()
        page.get_by_role('tab',name='图谱探索').click()
        page.screenshot(path=str(output/'neighborhood.png'),full_page=True)
        page.get_by_role('tab',name='存储与索引').click()
        expect(page.get_by_text('postgresql',exact=True)).to_be_visible()
        expect(page.get_by_text('Semantica ContextGraph',exact=True)).to_be_visible()
        page.screenshot(path=str(output/'storage.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        page.get_by_role('tab',name='图谱探索').click()
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'),'mobile overflow'
        page.screenshot(path=str(output/'mobile.png'),full_page=True)
        assert not errors,errors
        browser.close()
    print('图谱浏览器验证通过：数据库统计、节点详情、邻域、路径、索引视图及移动端。')


if __name__=='__main__':
    main()
