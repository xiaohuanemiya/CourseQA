"""Admin read-only graph workbench over the platform's PostgreSQL source of truth."""
from collections import Counter

import networkx as nx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from ..models import Document, EmbeddingIndex, Knowledge, KnowledgeBase, Relation, Release, ReleaseVector
from .knowledge import kb_get, serialize, valid_snapshot


def overview(db):
    items=[]
    for kb in db.scalars(select(KnowledgeBase).where(~KnowledgeBase.deleted).order_by(KnowledgeBase.name)):
        def count(cls, *conditions):
            return db.scalar(select(func.count()).select_from(cls).where(cls.kb_id==kb.id,*conditions))
        types=dict(db.execute(select(Knowledge.kind,func.count()).where(Knowledge.kb_id==kb.id,
            ~Knowledge.withdrawn).group_by(Knowledge.kind)).all())
        releases=list(db.scalars(select(Release).where(Release.kb_id==kb.id)))
        release_ids=[r.id for r in releases]
        indexes=list(db.scalars(select(EmbeddingIndex).where(EmbeddingIndex.release_id.in_(release_ids))))
        vector_count=db.scalar(select(func.count()).select_from(ReleaseVector).where(ReleaseVector.release_id.in_(release_ids)))
        items.append({"id":kb.id,"name":kb.name,"revision":kb.revision,"current_release_id":kb.current_release_id,
            "nodes":count(Knowledge,~Knowledge.withdrawn),
            "edges":count(Relation,~Relation.withdrawn),"documents":count(Document,~Document.withdrawn),
            "node_types":types,"releases":[{"id":r.id,"revision":r.revision,"nodes":len(r.snapshot["nodes"]),
                "edges":len(r.snapshot["edges"]),"current":r.id==kb.current_release_id} for r in releases],
            "indexes":[serialize(i) for i in indexes],"vectors":vector_count})
    return {"storage":{"engine":db.bind.dialect.name,"nodes_table":"knowledge","edges_table":"relations",
        "vectors_table":"release_vectors","graph_view":"Semantica ContextGraph",
        "description":"节点和关系存储在业务数据库；发布版本构建 Semantica 图视图。来源连接由文档溯源生成，不是数学前置关系。"},
        "knowledge_bases":items,"totals":{key:sum(k[key] for k in items) for key in ["nodes","edges","documents","vectors"]}}


def graph_scope(db,kb_id=None,version="draft",include_sources=True):
    if kb_id:
        kb_get(db,kb_id)
    kbs=list(db.scalars(select(KnowledgeBase).where(~KnowledgeBase.deleted,
        *([KnowledgeBase.id==kb_id] if kb_id else []))))
    kb_ids={k.id for k in kbs}
    documents={d.id:d for d in db.scalars(select(Document).where(Document.kb_id.in_(kb_ids),~Document.withdrawn))}
    nodes,edges=[],[]
    if version=="draft":
        query=select(Knowledge).options(load_only(Knowledge.id,Knowledge.title,Knowledge.kind,Knowledge.kb_id,
            Knowledge.revision,Knowledge.sources)).where(Knowledge.kb_id.in_(kb_ids),~Knowledge.withdrawn)
        for n in db.scalars(query):
            if any(s.get("document_id") and s["document_id"] not in documents for s in n.sources):
                continue
            nodes.append({k:getattr(n,k) for k in ["id","title","kind","kb_id","revision","sources"]})
        for e in db.scalars(select(Relation).where(Relation.kb_id.in_(kb_ids),~Relation.withdrawn)):
            edges.append(serialize(e)|{"derived":False})
    else:
        for kb in kbs:
            release=db.get(Release,kb.current_release_id) if kb.current_release_id else None
            if release:
                snap=valid_snapshot(db,release)
                nodes.extend({k:n.get(k) for k in ["id","title","kind","kb_id","revision","sources"]} for n in snap["nodes"])
                edges.extend({**e,"derived":False} for e in snap["edges"])
    valid={n["id"] for n in nodes}
    edges=[e for e in edges if e["source_id"] in valid and e["target_id"] in valid]
    if include_sources:
        used=set()
        for node in nodes:
            for doc_id in {s.get("document_id") for s in node["sources"]} & documents.keys():
                used.add(doc_id)
                edges.append({"id":f"source:{node['id']}:{doc_id}","source_id":node["id"],"target_id":"document:"+doc_id,
                    "kb_id":node["kb_id"],"kind":"source_document","derived":True})
        nodes.extend({"id":"document:"+key,"title":documents[key].filename,"kind":"document",
            "kb_id":documents[key].kb_id,"sources":[],"revision":documents[key].version} for key in sorted(used))
    return nodes,edges


def query_graph(db,kb_id=None,version="draft",include_sources=True,q="",kind="",
                focus=None,hops=1,limit=200,offset=0):
    nodes,edges=graph_scope(db,kb_id,version,include_sources)
    graph=nx.Graph()
    graph.add_nodes_from(n["id"] for n in nodes)
    graph.add_edges_from((e["source_id"],e["target_id"]) for e in edges)
    near=None
    if focus:
        if focus not in graph:
            raise HTTPException(404,"节点不属于当前知识范围或已撤回")
        near=nx.single_source_shortest_path_length(graph,focus,cutoff=hops)
    matches=[n for n in nodes if (near is None or n["id"] in near) and
        (not q or q.casefold() in n["title"].casefold()) and (not kind or n["kind"]==kind)]
    matches.sort(key=lambda n:((near or {}).get(n["id"],0),n["title"],n["id"]))
    selected=matches[offset:offset+limit]
    ids={n["id"] for n in selected}
    # Keep the provenance context on every page of the canvas, not only the first page.
    parents={e["target_id"] for e in edges if e["derived"] and e["source_id"] in ids}
    selected=selected+[n for n in nodes if n["id"] in parents-ids]
    ids.update(parents)
    shown=[e for e in edges if e["source_id"] in ids and e["target_id"] in ids]
    semantic=[e for e in edges if not e["derived"]]
    isolated=sum(graph.degree(n["id"])==0 for n in nodes if n["kind"]!="document")
    return {"nodes":[{k:v for k,v in n.items() if k!="sources"}|{"degree":graph.degree(n["id"])} for n in selected],
        "edges":shown,"matched":len(matches),"offset":offset,"limit":limit,"truncated":len(matches)>len(selected),
        "stats":{"nodes":sum(n["kind"]!="document" for n in nodes),"semantic_edges":len(semantic),
            "source_edges":len(edges)-len(semantic),"components":nx.number_connected_components(graph),"isolated":isolated,
            "node_types":dict(Counter(n["kind"] for n in nodes)),"relation_types":dict(Counter(e["kind"] for e in edges))}}


def node_detail(db,node_id,kb_id=None,version="draft"):
    nodes,edges=graph_scope(db,kb_id,version,True)
    node=next((n for n in nodes if n["id"]==node_id),None)
    if not node:
        raise HTTPException(404,"节点不属于当前知识范围或已撤回")
    if node_id.startswith("document:"):
        detail=serialize(db.get(Document,node_id.removeprefix("document:")))
        detail.update(id=node_id,title=detail["filename"],kind="document",sources=[])
    elif version=="draft":
        detail=serialize(db.get(Knowledge,node_id))
    else:
        kb=db.get(KnowledgeBase,node["kb_id"])
        detail=next(n for n in db.get(Release,kb.current_release_id).snapshot["nodes"] if n["id"]==node_id)
    related=[e for e in edges if node_id in {e["source_id"],e["target_id"]}]
    titles={n["id"]:n["title"] for n in nodes}
    return {"node":detail,"relations":[{**e,"source_title":titles[e["source_id"]],
        "target_title":titles[e["target_id"]]} for e in related[:200]],"relation_count":len(related)}


def find_path(db,source,target,kb_id=None,version="draft",include_sources=False,directed=False):
    nodes,edges=graph_scope(db,kb_id,version,include_sources)
    graph=nx.DiGraph() if directed else nx.Graph()
    graph.add_nodes_from(n["id"] for n in nodes)
    graph.add_edges_from((e["source_id"],e["target_id"]) for e in edges)
    if source not in graph or target not in graph:
        raise HTTPException(404,"路径端点不属于当前知识范围")
    try:
        path=nx.shortest_path(graph,source,target)
    except nx.NetworkXNoPath:
        return {"found":False,"nodes":[],"edges":[],"hops":None}
    pairs=set(zip(path,path[1:]))
    return {"found":True,"nodes":[next({k:v for k,v in n.items() if k!="sources"} for n in nodes if n["id"]==key) for key in path],
        "edges":[e for e in edges if (e["source_id"],e["target_id"]) in pairs or
            (not directed and (e["target_id"],e["source_id"]) in pairs)],"hops":len(path)-1}
