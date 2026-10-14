import copy

from sqlalchemy.orm import Session

from app.db import engine
from app.models import Knowledge, Relation
from app.services import retrieval
from app.services.retrieval_cache import BoundedCache, indexes
from .test_document_retrieval import document, fetch, publish


def test_lexical_cache_reuses_statistics_but_separates_scope_content_and_withdrawals(monkeypatch):
    indexes.clear()
    records = {"one": {"id": "one", "release_id": "release", "title": "链式法则", "content": "复合函数求导方法",
                        "attributes": {}, "documents": [{"id": "doc", "filename": "函数讲义.pdf", "version": 1}]},
               "two": {"id": "two", "release_id": "release", "title": "积分", "content": "积分方法",
                        "attributes": {}, "documents": []}}
    builds = []
    original = retrieval._lexical_index

    def build(selected):
        builds.append(set(selected))
        return original(selected)

    monkeypatch.setattr(retrieval, "_lexical_index", build)
    first = retrieval.lexical_ranking(records, "链式法则", ("app-one", 1))
    assert first[0][0] == "one"
    assert retrieval.lexical_ranking(records, "积分", ("app-one", 1))[0][0] == "two"
    assert len(builds) == 1
    retrieval.lexical_ranking(records, "积分", ("app-two", 1))
    assert len(builds) == 2
    without_withdrawn = {"one": records["one"]}
    assert retrieval.lexical_ranking(without_withdrawn, "积分", ("app-one", 1)) == []
    assert len(builds) == 3
    repaired = copy.deepcopy(records)
    repaired["one"]["content"] = "替换后的积分方法"
    retrieval.lexical_ranking(repaired, "积分", ("app-one", 1))
    assert len(builds) == 4
    assert records["one"]["content"] == "复合函数求导方法"
    indexes.clear()


def test_cached_graph_cannot_retain_withdrawn_edges_or_nodes(client, platform, monkeypatch):
    indexes.clear()
    bundle = platform["bundles"][0]
    _, ids = document(bundle, "网络讲义.pdf", [("罕见锚词", "罕见锚词正文"), ("无关条目", "另一独立条目的内容")])
    with Session(engine()) as db:
        relation = Relation(kb_id=bundle["kb"]["id"], source_id=ids[0], target_id=ids[1], kind="related")
        db.add(relation)
        db.commit()
        relation_id = relation.id
    publish(client, bundle)
    graph_builds, lexical_builds = [], []
    graph_factory, lexical_factory = retrieval.make_graph, retrieval._lexical_index

    def graph(*args, **kwargs):
        graph_builds.append(True)
        return graph_factory(*args, **kwargs)

    def lexical(records):
        lexical_builds.append(True)
        return lexical_factory(records)

    monkeypatch.setattr(retrieval, "make_graph", graph)
    monkeypatch.setattr(retrieval, "_lexical_index", lexical)
    # Eliminate unrelated approximate vector matches; the second node is only
    # discoverable through the published graph edge in this regression.
    monkeypatch.setattr(retrieval, "embed", lambda *args: [[0.0] * 128])
    first = fetch(bundle, "罕见锚词")
    second = fetch(bundle, "解释罕见锚词")
    assert {node["id"] for node in first} == set(ids)
    assert {node["id"] for node in second} == set(ids)
    assert len(graph_builds) == len(lexical_builds) == 1
    with Session(engine()) as db:
        db.get(Relation, relation_id).withdrawn = True
        db.commit()
    assert [node["id"] for node in fetch(bundle, "罕见锚词")] == [ids[0]]
    with Session(engine()) as db:
        db.get(Knowledge, ids[0]).withdrawn = True
        db.commit()
    assert not fetch(bundle, "罕见锚词")
    assert len(lexical_builds) == 2
    indexes.clear()


def test_cache_bounds_total_weight_entries_and_skips_oversized_indexes():
    cache = BoundedCache(max_entries=2, max_weight=6)
    calls = []

    def value(key, weight=3):
        def factory():
            calls.append(key)
            return key, weight
        return cache.get_or_create(key, factory)

    value("first")
    value("second")
    value("first")
    value("third")
    assert cache.info() == {"entries": 2, "weight": 6}
    value("second")
    assert calls.count("first") == 1 and calls.count("second") == 2
    value("too-large", 7)
    value("too-large", 7)
    assert calls.count("too-large") == 2
    assert cache.info() == {"entries": 2, "weight": 6}
