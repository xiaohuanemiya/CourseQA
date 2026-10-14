import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import engine
from app.models import Document, Knowledge, KnowledgeReview, Task
from app.schemas import ParseOptions
from app.tasks import dispatch, run_task
from .conftest import post


def test_import_publish_chat_without_review_or_model_call(client, platform, monkeypatch):
    from app.services import providers
    monkeypatch.setattr(providers, "generate", lambda *a, **kw: pytest.fail("规则导入不能调用审核模型"))
    bundle = platform["bundles"][0]
    base = f"/admin/knowledge-bases/{bundle['kb']['id']}"
    result = client.post('/api'+base+'/documents',
        data={"auto_review": json.dumps({"enabled": True, "model_profile_id": "retired-profile"})},
        files={"file": ("new-notes.txt", "导数用于描述函数的局部变化率。".encode())})
    assert result.status_code == 200 and result.json()['task']['state'] == 'succeeded'
    doc_id = result.json()['document']['id']
    with Session(engine()) as db:
        doc = db.get(Document, doc_id)
        assert doc.state == 'ready' and 'auto_review' not in doc.parse_options
        assert not list(db.scalars(select(Task).where(Task.kind == 'auto_review')))
        assert not list(db.scalars(select(KnowledgeReview)))
    assert post(client, base+'/publish')['state'] == 'succeeded'
    answer = post(client, f"/apps/{bundle['app']['id']}/chat", {"message": "函数的局部变化率是什么？"})
    assert any(s.get('document_id') == doc_id for citation in answer['citations'] for s in citation['sources'])


def test_legacy_options_cannot_restore_auto_review(client, platform):
    assert 'auto_review' not in ParseOptions.model_validate({'auto_review': {'enabled': True}}).model_dump()
    kb = platform['bundles'][0]['kb']['id']
    base = f'/admin/knowledge-bases/{kb}'
    response = client.post('/api'+base+'/documents', files={'file': ('plain.txt', b'Plain notes')})
    doc_id = response.json()['document']['id']
    with Session(engine()) as db:
        db.get(Document, doc_id).parse_options = {'auto_review': {'enabled': True, 'model_profile_id': 'missing'}}
        db.commit()
    task = post(client, base+'/documents/'+doc_id+'/reprocess')
    assert task['state'] == 'succeeded', task
    with Session(engine()) as db:
        assert db.get(Document, task['target_id']).state == 'ready'
        assert 'auto_review' not in db.get(Document, task['target_id']).parse_options


def test_knowledge_relations_and_questions_publish_without_approval(client, platform):
    bundle = platform['bundles'][0]
    base = f"/admin/knowledge-bases/{bundle['kb']['id']}"
    node = post(client, base+'/knowledge', {'kind': 'formula', 'title': '平方导数', 'content': 'd(x²)/dx=2x',
        'sources': [{'locator': '自编练习', 'note': '测试直接保存与发布'}]})
    edge = post(client, base+'/relations', {'source_id': bundle['node']['id'], 'target_id': node['id'], 'kind': 'related'})
    question = post(client, base+'/questions', {'title': '求 x² 的导数', 'answer': '2*x', 'knowledge_ids': [node['id']]})
    assert not any(x['reviewed'] for x in [node, edge, question])
    task = post(client, base+'/publish')
    assert task['state'] == 'succeeded', task
    quiz = post(client, f"/apps/{bundle['app']['id']}/quizzes", {'count': 20})
    assert any(q['id'] == question['id'] for q in quiz['questions'])
    template = post(client, base+'/templates/generate', {'question': {'title': '求 x^{{n}} 的导数',
        'answer': '{{n}}*x^({{n}}-1)', 'knowledge_ids': [node['id']]}, 'parameters': {'n': [2, 3]}, 'count': 2})
    assert template['count'] == 2
    assert post(client, base+'/publish')['state'] == 'succeeded'
    assert client.post('/api'+base+'/questions', json={'title': '非法表达式', 'answer': '__import__("os")',
        'knowledge_ids': [node['id']]}).status_code == 422


def test_retired_review_jobs_cannot_be_started_or_retried(client, platform):
    kb = platform['bundles'][0]['kb']['id']
    assert client.post(f'/api/admin/knowledge-bases/{kb}/auto-review', json={}).status_code == 410
    with Session(engine()) as db:
        source = db.scalar(select(Task))
        old = Task(kind='auto_review', target_id=kb, actor_id=source.actor_id, payload={})
        db.add(old)
        db.commit()
        key = old.id
        with pytest.raises(ValueError, match='已移除'):
            dispatch(db, 'auto_review', kb, source.actor_id)
    run_task(key)
    with Session(engine()) as db:
        assert db.get(Task, key).state == 'cancelled'
        assert not list(db.scalars(select(KnowledgeReview)))
    assert client.post(f'/api/admin/tasks/{key}/retry').status_code == 410
    assert key not in {t['id'] for t in client.get('/api/admin/tasks').json()}
    assert key in {t['id'] for t in client.get('/api/admin/tasks?include_archived=true').json()}
    post(client, '/auth/register', {'username': 'no-review-student', 'password': 'test-password'})
    assert client.post(f'/api/admin/knowledge-bases/{kb}/auto-review', json={}).status_code == 403


def test_migration_retires_settings_without_fabricating_approvals(client, platform, monkeypatch):
    source_path = Path(__file__).parents[1]/'migrations/versions/d9a5b7c31012_remove_review_workflow.py'
    spec = importlib.util.spec_from_file_location('retire_review_migration', source_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with Session(engine()) as db:
        item = db.get(Knowledge, platform['bundles'][0]['node']['id'])
        item.reviewed = False
        doc = Document(kb_id=item.kb_id, filename='legacy.txt', path='/tmp/legacy.txt', checksum='0'*64,
            state='review', parse_options={'auto_review': {'enabled': True}, 'splitting': {'method': 'paragraph'}},
            recognition={'requires_review': True, 'splitting': {'requires_review': True, 'chunk_count': 1}})
        db.add(doc)
        db.commit()
        with engine().begin() as connection:
            monkeypatch.setattr(migration.op, 'get_bind', lambda: connection)
            migration.upgrade()
        db.expire_all()
        assert doc.state == 'ready' and 'auto_review' not in doc.parse_options
        assert 'requires_review' not in doc.recognition and 'requires_review' not in doc.recognition['splitting']
        assert not item.reviewed
