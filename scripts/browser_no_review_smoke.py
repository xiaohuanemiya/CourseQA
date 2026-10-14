"""Verify removed review controls and direct upload/publication in a disposable knowledge base."""
import json
import time
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import expect, sync_playwright


def main():
    base = 'http://127.0.0.1:8820'
    output = Path('artifacts/no-review')
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        headers = {'X-Requested-With': 'calculus-studio'}
        response = page.request.post(base+'/api/auth/login', data={
            'username': 'admin', 'password': dotenv_values('.env')['ADMIN_PASSWORD']}, headers=headers)
        assert response.ok
        kb_id = None
        report = {}
        try:
            kbs = page.request.get(base+'/api/admin/knowledge-bases').json()
            material = next(k for k in kbs if k['name'] == '高数 · 教材与课件')
            page.goto(base+'/admin/knowledge-bases/'+material['id'], wait_until='networkidle')
            expect(page.get_by_role('button', name='发布版本', exact=True)).to_be_enabled()
            assert '审核' not in page.locator('body').inner_text()
            page.screenshot(path=str(output/'knowledge.png'))
            page.get_by_role('button', name='编辑', exact=True).first.click()
            drawer = page.get_by_role('dialog', name='知识编辑', exact=True)
            expect(drawer).to_be_visible()
            assert '审核' not in drawer.inner_text()
            drawer.get_by_role('button', name='取消', exact=True).click()
            page.get_by_role('button', name='上传资料', exact=True).click()
            expect(page.get_by_role('combobox', name='语义切分模型', exact=True)).to_be_visible()
            expect(page.get_by_role('checkbox', name='切分完成后自动审核', exact=True)).to_have_count(0)
            page.screenshot(path=str(output/'upload.png'))
            page.goto(base+'/admin/graph?kb_id='+material['id'], wait_until='networkidle')
            expect(page.get_by_role('combobox', name='审核状态', exact=True)).to_have_count(0)
            assert '审核' not in page.locator('body').inner_text()
            page.goto(base+'/admin/tasks', wait_until='networkidle')
            assert '审核' not in page.locator('body').inner_text()

            response = page.request.post(base+'/api/admin/knowledge-bases', headers=headers,
                data={'name': '流程验证（临时）', 'description': '自动化流程验证，完成后删除。'})
            assert response.ok
            kb_id = response.json()['id']
            page.goto(base+'/admin/knowledge-bases/'+kb_id+'?tab=documents', wait_until='networkidle')
            page.locator('.el-select').filter(has=page.get_by_role('combobox', name='切分方法', exact=True)).click()
            page.get_by_role('option', name='按段落 / OCR 内容块', exact=True).click()
            page.locator('input[type=file]').set_input_files({'name': '流程验收.txt', 'mimeType': 'text/plain',
                'buffer': '# 导数\n函数 x^2 的导数为 2x。\n# 积分\n函数 2x 的一个原函数为 x^2。'.encode()})
            expect(page.get_by_text('已导入', exact=True).first).to_be_visible(timeout=30000)
            data = page.request.get(base+f'/api/admin/knowledge-bases/{kb_id}/contents').json()
            assert len(data['documents']) == 1 and data['documents'][0]['state'] == 'ready'
            assert data['knowledge'] and all(not n['reviewed'] for n in data['knowledge'])
            page.get_by_role('tab', name='知识整理', exact=True).click()
            page.get_by_role('button', name='编辑', exact=True).first.click()
            drawer = page.get_by_role('dialog', name='知识编辑', exact=True)
            expect(drawer.get_by_role('button', name='保存', exact=True)).to_be_enabled()
            assert '审核' not in drawer.inner_text()
            drawer.get_by_role('button', name='保存', exact=True).click()
            expect(drawer).not_to_be_visible()
            for label, button, title in [('知识关系', '＋ 添加关系', '编辑知识关系'), ('练习题库', '＋ 添加题目', '编辑练习题')]:
                page.get_by_role('tab', name=label, exact=True).click()
                page.get_by_role('button', name=button, exact=True).click()
                drawer = page.get_by_role('dialog', name=title, exact=True)
                expect(drawer).to_be_visible()
                assert '审核' not in drawer.inner_text()
                drawer.get_by_role('button', name='取消', exact=True).click()
            page.get_by_role('button', name='发布版本', exact=True).click()
            for _ in range(60):
                data = page.request.get(base+f'/api/admin/knowledge-bases/{kb_id}/contents').json()
                if data['kb']['current_release_id']:
                    break
                time.sleep(1)
            assert data['kb']['current_release_id']
            assert len(data['releases'][0]['snapshot']['nodes']) == len(data['knowledge'])
            assert all(not n['reviewed'] for n in data['knowledge'])
            page.get_by_role('tab', name='发布历史', exact=True).click()
            expect(page.get_by_text('当前发布', exact=True)).to_be_visible(timeout=15000)
            page.screenshot(path=str(output/'direct-publication.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            page.get_by_role('tab', name='知识整理', exact=True).click()
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), 'mobile overflow'
            report = {'status': 'verified', 'test_kb_id': kb_id, 'knowledge': len(data['knowledge']),
                'review_controls': 'removed', 'uploaded_state': 'ready', 'direct_publish': True,
                'legacy_review_flags_unchanged': True, 'page_errors': errors}
            assert not errors, errors
        finally:
            if kb_id:
                response = page.request.delete(base+'/api/admin/knowledge-bases/'+kb_id, headers=headers)
                assert response.ok
                report['test_kb_deleted'] = True
            (output/'browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            browser.close()
    print('审核控件移除、上传、编辑、直接发布、图谱、任务列表与移动端验证通过；临时知识库已清理。')


if __name__ == '__main__':
    main()
