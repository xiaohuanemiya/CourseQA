"""Real UI/API/provider round trip against an isolated DB and local model fixture.

Run after npm --prefix frontend run build. No deployed data or cloud calls.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import uvicorn
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='relations-browser-') as temp:
        os.environ.update(DATABASE_URL='sqlite:///'+temp+'/test.db', STORAGE_DIR=temp,
            SECRET_KEY='local-browser-test-secret-not-a-real-credential', ALLOW_DEMO_PROVIDER='true',
            SEMANTICA_ENABLED='false', TASK_MODE='inline', OCR_ENABLED='false', COOKIE_SECURE='false')
        sys.path.insert(0,str(ROOT/'backend'))
        from app.db import Base, engine
        from app.main import app
        from app.models import User
        from app.security import password_hash
        from fastapi import Request
        from fastapi.responses import FileResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
        from sqlalchemy.orm import Session
        Base.metadata.create_all(engine())
        with Session(engine()) as db:
            db.add(User(username='admin',password_hash=password_hash('test-browser-password'),role='admin'))
            db.commit()
        control={'fail':False,'calls':0}

        @app.post('/test-model/chat/completions')
        async def fake_model(request: Request):
            data=await request.json()
            control['calls']+=1
            assert data['model']=='local-fixture' and 'max_tokens' not in data
            if control['fail']:
                control['fail']=False
                return JSONResponse({'error':'simulated failure'},status_code=503)
            knowledge=json.loads(data['messages'][1]['content'])['knowledge']
            nodes={n['title']:n for n in knowledge}
            a,b=nodes['极限'],nodes['导数']
            result={'relations':[{'source_id':a['id'],'target_id':b['id'],'kind':'prerequisite',
                'reason':'导数的定义依赖差商的极限，应先学习极限。',
                'source_quote':a['content'],'target_quote':b['content']}]}
            return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(result,ensure_ascii=False)}}],
                    'usage':{'prompt_tokens':100,'completion_tokens':60}}

        app.mount('/assets',StaticFiles(directory=ROOT/'frontend/dist/assets'),name='test-assets')

        @app.get('/{path:path}')
        def frontend(path: str):
            return FileResponse(ROOT/'frontend/dist/index.html')

        sock=socket.socket()
        sock.bind(('127.0.0.1',0))
        base=f'http://127.0.0.1:{sock.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
        thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        output=ROOT/'artifacts/relation-generation'
        output.mkdir(parents=True,exist_ok=True)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1080})
                errors=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                headers={'X-Requested-With':'calculus-studio'}

                def post(path,data=None):
                    response=page.request.post(base+'/api'+path,headers=headers,data=data)
                    assert response.ok,response.text()
                    return response.json()

                post('/auth/login',{'username':'admin','password':'test-browser-password'})
                model=post('/admin/model-profiles',{'name':'ds-flash','model':'local-fixture','provider':'openai',
                    'base_url':base+'/test-model','purpose':'generation','api_key':'local-test-fixture'})
                kb=post('/admin/knowledge-bases',{'name':'课程关系生成验收','extraction_profile_id':model['id']})
                kb_path='/admin/knowledge-bases/'+kb['id']
                for title,content in [('极限','极限描述函数在某点附近的变化趋势。'),
                    ('导数','导数是差商的极限，学习导数需要先理解极限。')]:
                    post(kb_path+'/knowledge',{'kind':'concept','title':title,'content':content,
                        'sources':[{'locator':'测试资料','excerpt':content,'note':'本地验收'}]})
                page.goto(base+kb_path+'?tab=relations',wait_until='networkidle')
                expect(page.get_by_role('combobox',name='关系生成模型')).to_be_visible()
                page.get_by_role('button',name='模型生成关系',exact=True).click()
                result=page.locator('.generation-result')
                expect(result).to_contain_text('新增 1 条',timeout=15000)
                page.locator('.el-table__expand-icon').first.click()
                expect(page.locator('.relation-evidence')).to_contain_text('应先学习极限')
                page.get_by_role('button',name='发布版本',exact=True).click()
                expect(page.get_by_text('发布成功，已同步应用检索索引',exact=True)).to_be_visible(timeout=10000)
                graph=page.request.get(base+'/api/admin/graph',params={'kb_id':kb['id'],'version':'published'}).json()
                assert graph['stats']['semantic_edges']==1
                assert graph['edges'][0]['attributes']['origin']=='model'
                page.screenshot(path=str(output/'desktop.png'),full_page=True)
                control['fail']=True
                page.get_by_role('button',name='模型生成关系',exact=True).click()
                expect(result).to_contain_text('生成失败',timeout=10000)
                page.get_by_role('button',name='重试生成',exact=True).click()
                expect(result).to_contain_text('生成完成',timeout=10000)
                expect(result).to_contain_text('新增 0 条')
                expect(result).to_contain_text('重复或已撤回 1')
                page.reload(wait_until='networkidle')
                expect(result).to_contain_text('生成完成')
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                page.screenshot(path=str(output/'mobile.png'),full_page=True)
                assert not errors,errors
                report={'ui_api_provider_round_trip':True,'provider_fixture_calls':control['calls'],
                    'evidence_visible':True,'published_graph_relation':True,'retry_without_duplicates':True,
                    'restored_after_reload':True,'mobile_layout':True,'page_errors':errors,'cloud_calls':0}
                (output/'browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
                print(json.dumps(report,ensure_ascii=False))
                browser.close()
        finally:
            server.should_exit=True
            thread.join(timeout=5)
            sock.close()
            engine().dispose()


if __name__=='__main__':
    main()
