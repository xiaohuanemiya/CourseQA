import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Knowledge, KnowledgeBase, Task
from app.services import relation_generation as rg
from app.services.providers import ProviderError
from .conftest import login, post


@pytest.fixture
def bundle(client):
    login(client)
    profile = post(client, '/admin/model-profiles', {'name': 'ds-flash', 'provider': 'openai',
        'model': 'test-generation', 'base_url': 'http://127.0.0.1:9999', 'purpose': 'generation'})
    kb = post(client, '/admin/knowledge-bases', {'name': '课程知识', 'extraction_profile_id': profile['id']})
    base = f"/admin/knowledge-bases/{kb['id']}"
    nodes = []
    for kind, title, content in [('concept', '极限', '极限描述函数在某点附近的趋势。'),
        ('concept', '导数', '导数通过差商的极限定义，需要先学习极限。'),
        ('formula', '求导公式', '幂函数求导公式为 d(x²)/dx=2x。'),
        ('example', '平方求导', '求 x² 的导数，使用幂函数求导公式得到 2x。')]:
        nodes.append(post(client, base+'/knowledge', {'kind': kind, 'title': title, 'content': content,
            'sources': [{'locator': '示例正文', 'excerpt': content, 'note': '测试'}]}))
    return SimpleNamespace(kb=kb, base=base, profile=profile, nodes=nodes, url=base+'/relation-generation')


def edge(a, b, kind='related', **overrides):
    return {'source_id': a['id'], 'target_id': b['id'], 'kind': kind, 'reason': '正文说明这些知识之间的联系',
            'source_quote': a['content'], 'target_quote': b['content'], **overrides}


def relations(client, bundle):
    return client.get('/api'+bundle.base+'/contents').json()['relations']


def test_generate_evidence_filters_cycles_scope_and_publish(client, bundle, monkeypatch):
    a, b, c, d = bundle.nodes
    other = post(client, '/admin/knowledge-bases', {'name': '另一库'})
    foreign = post(client, f"/admin/knowledge-bases/{other['id']}/knowledge", {
        'kind': 'concept', 'title': '秘密知识', 'content': '仅另一知识库可用的内容', 'sources': [{'locator': '别库', 'note': '测试'}]})
    release = post(client, bundle.base+'/publish')['result']['release_id']

    def fake(profile, system, prompt, **kwargs):
        request = json.loads(prompt)
        assert foreign['id'] not in prompt and foreign['content'] not in prompt
        assert len(request['knowledge']) == 4 and kwargs['structured'] and 'max_tokens' not in kwargs
        return {'relations': [edge(a, b, 'prerequisite'), edge(b, a, 'prerequisite'), edge(d, c, 'uses_formula'),
            edge(d, b, 'assesses'), edge(a, a), edge(a, foreign), edge(a, b, source_quote='编造的依据文字'),
            edge(a, b, 'uses_formula'), edge(a, b, 'invented'), edge(a, b, 'prerequisite'), {'source_id': []}]}
    monkeypatch.setattr(rg, 'generate', fake)
    task = post(client, bundle.url, {})
    assert task['state'] == 'succeeded', task
    assert task['result']['generated_count'] == 3
    assert task['result']['skipped'] == {'cycle': 1, 'self_relation': 1, 'invalid_reference': 1,
        'missing_evidence': 1, 'invalid_direction': 1, 'invalid_kind': 1, 'duplicate_or_withdrawn': 1, 'invalid_format': 1}
    edges = relations(client, bundle)
    assert len(edges) == 3
    assert all(not e['reviewed'] and e['attributes']['model_name'] == 'ds-flash' for e in edges)
    assert edges[0]['attributes']['evidence'][0]['revision'] == 1
    assert client.get('/api'+bundle.base+'/contents').json()['kb']['current_release_id'] == release
    assert client.get('/api'+bundle.url).json()['id'] == task['id']
    task = post(client, bundle.base+'/publish')
    assert task['state'] == 'succeeded', task
    release = client.get('/api'+bundle.base+'/contents').json()['releases'][-1]
    assert len(release['snapshot']['edges']) == 3 and release['snapshot']['edges'][0]['attributes']['evidence']
    history = client.get('/api/admin/audit/'+edges[0]['id']).json()
    assert history[0]['action'] == 'generate_relation'


def test_regeneration_deduplicates_symmetric_and_respects_withdrawn(client, bundle, monkeypatch):
    a, b, *_ = bundle.nodes
    monkeypatch.setattr(rg, 'generate', lambda *a_, **k: {'relations': [edge(a,b), edge(b,a)]})
    assert post(client, bundle.url, {})['result']['generated_count'] == 1
    assert post(client, bundle.url, {})['result']['generated_count'] == 0
    existing = relations(client, bundle)[0]
    assert client.delete('/api'+bundle.base+'/relations/'+existing['id']).status_code == 200
    assert post(client, bundle.url, {})['result']['generated_count'] == 0
    assert len(relations(client, bundle)) == 1 and relations(client, bundle)[0]['withdrawn']


def test_stale_knowledge_rejects_atomic_save(client, bundle, monkeypatch):
    a,b,*_ = bundle.nodes
    def fake(*args, **kwargs):
        with Session(engine()) as db:
            db.get(Knowledge, a['id']).content = '已经修改的正文'
            db.get(KnowledgeBase, bundle.kb['id']).revision += 1
            db.commit()
        return {'relations':[edge(a,b)]}
    monkeypatch.setattr(rg,'generate',fake)
    task = post(client,bundle.url,{})
    assert task['state']=='failed' and '生成期间知识库已修改' in task['error']
    assert not relations(client,bundle)


def test_failure_retry_reuses_completed_batches_and_saves_atomically(client,bundle,monkeypatch):
    for i in range(5):
        post(client,bundle.base+'/knowledge',{'kind':'concept','title':f'补充 {i}','content':f'额外课程知识内容 {i}',
            'sources':[{'locator':'示例','note':'测试'}]})
    calls=[]
    def fake(profile,system,prompt,**kwargs):
        request=json.loads(prompt)
        calls.append(request['focus_ids'])
        if len(calls)==2:
            raise ProviderError('模型请求超时，请稍后重试')
        a,b=request['knowledge'][:2]
        return {'relations':[edge(a,b)]}
    monkeypatch.setattr(rg,'generate',fake)
    failed=post(client,bundle.url,{})
    assert failed['state']=='failed' and not relations(client,bundle)
    retry=post(client,'/admin/tasks/'+failed['id']+'/retry')
    assert retry['state']=='succeeded',retry
    assert retry['result']['cache_hits']==1 and len(calls)==3
    assert len(relations(client,bundle))==2


@pytest.mark.parametrize('result',[{}, {'relations':'not-array'}])
def test_malformed_output_fails_without_edges(client,bundle,monkeypatch,result):
    monkeypatch.setattr(rg,'generate',lambda *a,**k:result)
    task=post(client,bundle.url,{})
    assert task['state']=='failed' and 'relations' in task['error']
    assert not relations(client,bundle)


def test_no_evidence_is_honest_empty_success(client,bundle,monkeypatch):
    monkeypatch.setattr(rg,'generate',lambda *a,**k:{'relations':[]})
    task=post(client,bundle.url,{})
    assert task['state']=='succeeded' and task['result']['generated_count']==0
    assert task['result']['message']=='未发现可新增的有效关系'


def test_admin_model_validation_and_active_task_reuse(client,bundle,monkeypatch):
    assert client.post('/api'+bundle.url,json={'model_profile_id':'absent'}).status_code==422
    emb=post(client,'/admin/model-profiles',{'name':'embedding','provider':'demo','model':'hash','purpose':'embedding','dimension':128})
    assert client.post('/api'+bundle.url,json={'model_profile_id':emb['id']}).status_code==422
    monkeypatch.setattr('app.tasks.enqueue',lambda *a:None)
    first=post(client,bundle.url,{})
    assert first['state']=='pending'
    assert post(client,bundle.url,{})['id']==first['id']
    post(client,'/auth/logout')
    post(client,'/auth/register',{'username':'student','password':'student-test-password'})
    assert client.post('/api'+bundle.url,json={}).status_code==403
    assert client.get('/api'+bundle.url).status_code==403
    with Session(engine()) as db:
        assert len(list(db.scalars(select(Task).where(Task.kind=='generate_relations'))))==1


def test_manual_edit_removes_stale_model_evidence_keeps_audit(client,bundle,monkeypatch):
    a,b,c,*_=bundle.nodes
    monkeypatch.setattr(rg,'generate',lambda *args,**kwargs:{'relations':[edge(a,b)]})
    post(client,bundle.url,{})
    original=relations(client,bundle)[0]
    response=client.put('/api'+bundle.base+'/relations/'+original['id'],json={
        'source_id':a['id'],'target_id':c['id'],'kind':'related'})
    assert response.status_code==200 and response.json()['attributes']=={}
    history=client.get('/api/admin/audit/'+original['id']).json()
    assert history[0]['before']['attributes']['origin']=='model'


def test_batches_cover_full_text_and_cross_distant_boundaries():
    nodes=[SimpleNamespace(id=str(i),title=f'知识 {i}',kind='concept',content=f'不同课程说明 {i}',attributes={}) for i in range(30)]
    nodes[0].content=nodes[-1].content='特殊术语：牛顿迭代法的收敛条件与误差估计。'
    nodes[10].content='完整公式和条件'*4000
    requests=list(rg.batches(nodes))
    assert [i for r in requests for i in r['focus_ids']]==[n.id for n in nodes]
    assert nodes[-1].id in {n['id'] for n in requests[0]['knowledge']}
    assert any(n['content']==nodes[10].content for r in requests for n in r['knowledge'])
    assert all(len(json.dumps(r,ensure_ascii=False))<66000 for r in requests)
