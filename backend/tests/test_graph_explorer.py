from .conftest import login, post


def query(client,**kwargs):
    response=client.get('/api/admin/graph',params=kwargs)
    assert response.status_code==200,response.text
    return response.json()


def test_graph_admin_only_and_real_counts(client,platform):
    result=client.get('/api/admin/graph/overview').json()
    assert result['storage']['nodes_table']=='knowledge'
    assert result['totals']['nodes']==2
    assert result['totals']['vectors']==2
    post(client,'/auth/register',{'username':'graph-student','password':'student-password'})
    for url in ['/api/admin/graph','/api/admin/graph/overview','/api/admin/graph/nodes/any',
                '/api/admin/graph/path?source=a&target=b']:
        assert client.get(url).status_code==403


def test_graph_scope_draft_and_published_are_distinct(client,platform):
    a,b=platform['bundles']
    kb=a['kb']['id']
    new=post(client,f'/admin/knowledge-bases/{kb}/knowledge',{'kind':'concept','title':'尚未发布概念',
        'content':'只有草稿能看到','sources':[{'locator':'人工整理','note':'图谱测试'}]})
    assert {n['id'] for n in query(client,kb_id=kb)['nodes']}=={a['node']['id'],new['id']}
    assert {n['id'] for n in query(client,kb_id=kb,version='published')['nodes']}=={a['node']['id']}
    assert query(client,kb_id=kb,review='pending')['matched']==2  # Retired filter is ignored.
    assert query(client,kb_id=kb,q='尚未发布')['nodes'][0]['id']==new['id']
    assert client.get(f"/api/admin/graph/nodes/{b['node']['id']}",params={'kb_id':kb}).status_code==404
    assert client.get(f"/api/admin/graph/nodes/{new['id']}",params={'kb_id':kb,'version':'published'}).status_code==404


def test_neighbors_paths_and_withdrawals(client,platform):
    a=platform['bundles'][0]
    kb=a['kb']['id']
    target=post(client,f'/admin/knowledge-bases/{kb}/knowledge',{'kind':'concept','title':'后续概念',
        'content':'后续','sources':[{'locator':'人工','note':'测试'}]})
    relation=post(client,f'/admin/knowledge-bases/{kb}/relations',{'source_id':a['node']['id'],
        'target_id':target['id'],'kind':'prerequisite'})
    view=query(client,kb_id=kb,focus=a['node']['id'],hops=1,include_sources=False)
    assert len(view['nodes'])==2 and view['edges'][0]['id']==relation['id']
    params={'kb_id':kb,'source':a['node']['id'],'target':target['id'],'directed':True}
    path=client.get('/api/admin/graph/path',params=params).json()
    assert path['found'] and path['hops']==1
    assert not client.get('/api/admin/graph/path',params={**params,'source':target['id'],'target':a['node']['id']}).json()['found']
    assert client.delete(f"/api/admin/knowledge-bases/{kb}/knowledge/{target['id']}").status_code==200
    assert client.get('/api/admin/graph/path',params=params).status_code==404
    assert query(client,kb_id=kb)['stats']['semantic_edges']==0


def test_document_provenance_is_explicit_and_pageable(client,platform):
    kb=platform['bundles'][0]['kb']['id']
    response=client.post(f'/api/admin/knowledge-bases/{kb}/documents',files={'file':('notes.md',b'# A\nOne\n# B\nTwo','text/markdown')})
    assert response.json()['task']['state']=='succeeded'
    doc=response.json()['document']
    data=query(client,kb_id=kb,q='A',limit=1)
    assert data['matched']==1 and len(data['nodes'])==2
    assert data['edges'][0]['derived'] and data['edges'][0]['kind']=='source_document'
    assert data['stats']['semantic_edges']==0 and data['stats']['source_edges']==2
    data=query(client,kb_id=kb,include_sources=False)
    assert all(n['kind']!='document' for n in data['nodes']) and not data['edges']
    detail=client.get(f"/api/admin/graph/nodes/document:{doc['id']}").json()
    assert detail['node']['filename']=='notes.md' and 'path' not in detail['node']
    assert client.delete(f"/api/admin/knowledge-bases/{kb}/documents/{doc['id']}").status_code==200
    assert client.get(f"/api/admin/graph/nodes/document:{doc['id']}").status_code==404
    post(client,'/auth/logout')
    assert client.get('/api/admin/graph').status_code==401
    login(client)
    assert client.get('/api/admin/graph',params={'limit':501}).status_code==422
