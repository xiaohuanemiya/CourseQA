import hashlib
import json
from collections import Counter

import networkx as nx
from fastapi import HTTPException
from sqlalchemy import select, update

from ..config import settings
from ..models import Application, Audit, Document, EmbeddingIndex, Knowledge, KnowledgeBase, ModelProfile, Question, Relation, Release, ReleaseVector, uid
from .providers import embed, embedding_signature


def serialize(item):
    return {column.name: getattr(item, column.name) for column in item.__table__.columns
            if column.name not in {"created_at", "secret", "password_hash", "path"}}


def kb_get(db, kb_id):
    kb = db.get(KnowledgeBase, kb_id)
    if not kb or kb.deleted:
        raise HTTPException(404, "知识库不存在")
    return kb


def touch(db, kb_id):
    db.execute(update(KnowledgeBase).where(KnowledgeBase.id == kb_id).values(revision=KnowledgeBase.revision + 1))


def audit(db, user_id, entity_id, action, before=None, after=None):
    db.add(Audit(actor_id=user_id, entity_id=entity_id, action=action, before=before, after=after))


def active_items(db, kb_id):
    return db.scalars(select(Knowledge).where(Knowledge.kb_id == kb_id, ~Knowledge.withdrawn)).all()


def validate_sources(db, kb_id, sources):
    for source in sources:
        doc_id = source.get("document_id")
        if doc_id:
            doc = db.get(Document, doc_id)
            if not doc or doc.kb_id != kb_id or doc.withdrawn:
                raise HTTPException(422, "来源文档不属于此知识库或已撤回")
            if source.get("asset_id"):
                from .recognition import asset_path
                asset_path(doc_id,source["asset_id"])
        elif not source.get("note"):
            raise HTTPException(422, "人工知识必须填写来源说明")


def valid_snapshot(db, release):
    """Immediate withdrawals apply even to the currently published immutable snapshot."""
    snapshot = release.snapshot
    live = {x.id for x in db.scalars(select(Knowledge).where(Knowledge.kb_id == release.kb_id, ~Knowledge.withdrawn))}
    docs = {x.id for x in db.scalars(select(Document).where(Document.kb_id == release.kb_id, Document.withdrawn))}
    nodes = [n for n in snapshot["nodes"] if n["id"] in live and
             not any(s.get("document_id") in docs for s in n["sources"])]
    node_ids = {n["id"] for n in nodes}
    live_edges = {e.id for e in db.scalars(select(Relation).where(Relation.kb_id == release.kb_id, ~Relation.withdrawn))}
    edges = [e for e in snapshot["edges"] if e["id"] in live_edges and e["source_id"] in node_ids and e["target_id"] in node_ids]
    live_questions = {q.id for q in db.scalars(select(Question).where(Question.kb_id == release.kb_id, ~Question.withdrawn))}
    questions = [q for q in snapshot["questions"] if q["id"] in live_questions and set(q["knowledge_ids"]) <= node_ids]
    return {**snapshot, "nodes": nodes, "edges": edges, "questions": questions}


def make_graph(nodes, edges, bidirectional=False):
    graph = nx.DiGraph()
    graph.add_nodes_from(n["id"] for n in nodes)
    graph.add_edges_from((e["source_id"], e["target_id"]) for e in edges)
    if settings().semantica_enabled:
        from semantica.context import ContextGraph
        semantic = ContextGraph(advanced_analytics=True, node_embeddings=False)
        for n in nodes:
            semantic.add_node(n["id"], n["kind"], n["title"], metadata={"kb_id": n["kb_id"]})
        for e in edges:
            semantic.add_edge(e["source_id"], e["target_id"], e["kind"])
            if bidirectional:
                semantic.add_edge(e["target_id"], e["source_id"], "inverse_" + e["kind"])
        return graph, semantic
    return graph, None


def normalize(values):
    maximum = max(values.values(), default=0) or 1
    return {k: v/maximum for k, v in values.items()}


def importance(nodes, edges, questions):
    graph, semantic = make_graph(nodes, edges)
    concept_ids = {n["id"] for n in nodes if n["kind"] in {"concept", "theorem", "formula"}}
    concept_graph = graph.subgraph(concept_ids)
    centrality = nx.pagerank(concept_graph) if len(concept_graph) else {}
    frequency = {n["id"]: len({s.get("document_id") or s["locator"] for s in n["sources"]}) for n in nodes}
    exam = Counter(k for q in questions if q["attributes"].get("past_exam") for k in q["knowledge_ids"])
    f, p, e = normalize(frequency), normalize(centrality), normalize(exam)
    result = {}
    for n in nodes:
        k = n["id"]
        score = (.3*f.get(k, 0)+.3*p.get(k, 0)+.4*e.get(k, 0)) if exam else (.5*f.get(k, 0)+.5*p.get(k, 0))
        result[k] = {"score": round(score, 4), "document_frequency": frequency[k],
                     "pagerank": centrality.get(k, 0), "past_exam_frequency": exam[k]}
    return result, semantic


def publish(db, kb_id, actor_id):
    kb = kb_get(db, kb_id)
    # Serialize publication with application embedding switches for this KB.
    # Both publication paths acquire KB locks before creating/selecting indexes.
    db.refresh(kb, with_for_update=True)
    expected_revision, old_release = kb.revision, kb.current_release_id
    nodes = [serialize(n) for n in active_items(db, kb_id)]
    if not nodes:
        raise ValueError("知识库没有可发布的知识")
    for n in nodes:
        validate_sources(db, kb_id, n["sources"])
    ids = {n["id"] for n in nodes}
    edges = [serialize(r) for r in db.scalars(select(Relation).where(Relation.kb_id == kb_id, ~Relation.withdrawn))]
    if any(e["source_id"] not in ids or e["target_id"] not in ids for e in edges):
        raise ValueError("关系引用了已撤回的知识")
    prerequisites = nx.DiGraph((e["source_id"], e["target_id"]) for e in edges if e["kind"] == "prerequisite")
    if not nx.is_directed_acyclic_graph(prerequisites):
        names = {n["id"]: n["title"] for n in nodes}
        cycle = nx.find_cycle(prerequisites)
        raise ValueError("前置关系存在循环：" + " → ".join(names[a] for a, _ in cycle) + " → " + names[cycle[0][0]])
    questions = [serialize(q) for q in db.scalars(select(Question).where(Question.kb_id == kb_id, ~Question.withdrawn))]
    if any(not set(q["knowledge_ids"]) <= ids for q in questions):
        raise ValueError("题目引用了无效知识点")
    ranking, semantic = importance(nodes, edges, questions)
    snapshot = {"nodes": nodes, "edges": edges, "questions": questions, "importance": ranking}
    release_id = uid()
    if semantic:
        from semantica.change_management import TemporalVersionManager
        from semantica.provenance import ProvenanceManager
        artifact_dir = settings().storage_dir / "semantic" / kb_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        provenance = ProvenanceManager(storage_path=str(artifact_dir / "provenance.db"))
        for n in nodes:
            provenance.track_entity(entity_id=f"{release_id}:{n['id']}", source=json.dumps(n["sources"], ensure_ascii=False),
                                    entity_type=n["kind"], metadata={"release_id": release_id, "revision": n["revision"]})
        manager = TemporalVersionManager(storage_path=str(artifact_dir / "versions.db"))
        manager.create_snapshot(graph=semantic.to_dict(), version_label=release_id, author=f"{actor_id}@calculus.local", description="Knowledge publication")
        snapshot["semantic_graph"] = semantic.to_dict()
    checksum = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    release = Release(id=release_id, kb_id=kb_id, revision=expected_revision, snapshot=snapshot,
                      embedding_signature="per-application-index", checksum=checksum, author_id=actor_id)
    db.add(release)
    db.flush()
    for application in db.scalars(select(Application).where(Application.active, ~Application.deleted)):
        if kb_id in application.published["kb_ids"]:
            profile = db.get(ModelProfile, application.published["embedding_profile_id"])
            ensure_index(db, release, profile)
    result = db.execute(update(KnowledgeBase).where(KnowledgeBase.id == kb_id,
        KnowledgeBase.revision == expected_revision, KnowledgeBase.current_release_id == old_release,
        ~KnowledgeBase.deleted).values(current_release_id=release.id))
    if result.rowcount != 1:
        raise ValueError("构建期间知识库已被修改或发布，请重试；原发布版本未变")
    audit(db, actor_id, kb_id, "publish", {"release_id": old_release}, {"release_id": release.id})
    db.commit()
    db.refresh(kb)
    return {"release_id": release.id, "nodes": len(nodes), "edges": len(edges), "questions": len(questions)}


def ensure_index(db, release, profile):
    signature = embedding_signature(profile)
    existing = db.scalar(select(EmbeddingIndex).where(EmbeddingIndex.release_id == release.id,
                                                     EmbeddingIndex.signature == signature))
    if existing:
        return signature
    nodes = release.snapshot["nodes"]
    for i in range(0, len(nodes), 16):
        batch = nodes[i:i+16]
        vectors = embed([n["title"] + "\n" + n["content"] for n in batch], profile)
        for n, v in zip(batch, vectors, strict=True):
            db.add(ReleaseVector(release_id=release.id, knowledge_id=n["id"], embedding=v, signature=signature))
    db.add(EmbeddingIndex(release_id=release.id, profile_id=profile.id, signature=signature,
                          dimension=profile.dimension, count=len(nodes)))
    db.flush()
    return signature
