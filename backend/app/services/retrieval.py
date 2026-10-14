"""Retrieve published evidence, resolving named documents before searching their chunks.

Document metadata is joined at query time, so existing vector indexes remain usable.
Source membership is a retrieval constraint, never an invented semantic graph edge.
"""
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from types import SimpleNamespace

import networkx as nx
from fastapi import HTTPException
from sqlalchemy import select

from ..models import Document, EmbeddingIndex, ModelProfile, ReleaseVector
from .knowledge import make_graph
from .providers import embed, embedding_signature
from .retrieval_cache import indexes

MAX_CONTEXT_CHARACTERS = 24000
MAX_OVERVIEW_UNITS = 32
OVERVIEW = re.compile(r"考了什么|考什么|考了哪些|考[查察]|考点|考试范围|题型|分值|多少道|哪些内容|主要内容|总结|概述|概览|梳理|覆盖")
ANSWER = re.compile(r"参考答案|标准答案|答案|解答|解析")
EXAM = re.compile(r"月考|期中|期末|[a-d]\s*卷")
BOILERPLATE = re.compile(r"^(?:页码|页眉|页脚|目录|封面|版权声明|试卷头.*|考生信息|(?:试卷|文档|教材|课件)?(?:标题|抬头).*)$")
COMPARISON = re.compile(r"比较|对比|差异|异同|区别|\bcompare\b|\bversus\b|\bvs\b", re.I)


class Evidence(list):
    def __init__(self, items=(), context=None):
        super().__init__(items)
        self.context = context or {}


def normalized(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    value = re.sub(r"\.(?:pdf|docx?|pptx?|txt|md)$", "", value)
    # Both spellings occur in the filenames and questions for this course.
    value = value.replace("一元微积分", "一元")
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value)


def family(filename):
    return re.sub(r"试卷|试题|参考答案|标准答案|答案|解答|解析", "", normalized(filename))


def noise(node):
    content = node["content"].strip()
    return (len(content) < 500 and bool(BOILERPLATE.fullmatch(node["title"].strip()))) or (
        node["kind"] == "note" and bool(re.fullmatch(r"[-—]?\s*\d{1,4}\s*[-—]?", content)))


def _single_document_plan(question, documents):
    """Conservative resolution: exact names, or explicit year/exam identifiers.

    Unresolved named exams must not silently fall back to a different year's paper.
    Multiple unrelated document families require clarification, not a guessed course.
    """
    q = normalized(question)
    years = set(re.findall(r"(?:19|20)\d{2}", question))
    exams = set(EXAM.findall(q))
    quoted = re.findall(r"《([^》\n]+)》", question)
    quoted += re.findall(r'"([^"\n]+\.(?:pdf|docx?|pptx?|txt|md))"', question, re.I)
    named = [name for name in quoted if len(normalized(name)) >= 4]
    exact = [d for d in documents.values() if len(family(d.filename)) >= 4 and family(d.filename) in q]
    # A short filename must not defeat an explicit year, paper variant or course level.
    levels = set(re.findall(r"[a-d][上下]", q))
    exam_reference = re.search(r"月考|期中|期末|试卷|试题|考试|[a-d]卷", q)
    preceding_years = list(re.finditer(r"(?:19|20)\d{2}", q[:exam_reference.start()])) if exam_reference else []
    course_hint = q[preceding_years[-1].end():exam_reference.start()] if preceding_years else ""
    course_hint = re.sub(r"第[一二12]学期|[上下]学期|学年|年度|课程|年的|年|的", "", course_hint)
    def compatible(doc):
        title = normalized(doc.filename)
        return (years <= set(re.findall(r"(?:19|20)\d{2}", doc.filename)) and
                exams <= set(EXAM.findall(title)) and all(level in title for level in levels) and
                (not course_hint or course_hint in title))
    exact = [d for d in exact if compatible(d)]
    if exact:
        longest = max(len(family(d.filename)) for d in exact)
        exact = [d for d in exact if len(family(d.filename)) == longest]
    explicit_exam = bool(exam_reference and (years or levels or OVERVIEW.search(question)))
    candidates = exact or ([d for d in documents.values() if compatible(d)] if explicit_exam else [])
    if named:
        candidates = [d for d in documents.values() if compatible(d) and any(
            family(name) == family(d.filename) for name in named)]
    explicit_file = bool(re.search(r"\.(?:pdf|docx?|pptx?|txt|md)(?:\b|$)", question, re.I))
    if not candidates:
        return {"status": "not_found" if explicit_exam or named or explicit_file else "general", "documents": []}
    families = {family(d.filename) for d in candidates}
    status = "matched" if len(families) == 1 else "ambiguous"
    return {"status": status, "documents": [
        {"id": d.id, "filename": d.filename, "version": d.version} for d in sorted(
            candidates, key=lambda d: (bool(ANSWER.search(d.filename)), d.filename, -d.version, d.id))]}


def _comparison_targets(question):
    if not COMPARISON.search(question):
        return []
    quoted = re.findall(r"《([^》\n]+)》", question)
    quoted += re.findall(r'"([^"\n]+\.(?:pdf|docx?|pptx?|txt|md))"', question, re.I)
    if len(quoted) >= 2:
        return [f"《{name}》" for name in quoted]
    body = re.sub(r"^.*?(?:比较|对比|\bcompare\b)\s*", "", question, count=1, flags=re.I)
    parts = [part.strip() for part in re.split(r"和|与|以及|及|、|，|,|\b(?:and|versus|vs)\b", body, flags=re.I)
             if part.strip()]
    document_hint = re.compile(r"(?:19|20)\d{2}|月考|期中|期末|试卷|讲义|教材|课件|文档|\.(?:pdf|docx?|pptx?|txt|md)\b", re.I)
    return parts if len(parts) >= 2 and all(document_hint.search(part) for part in parts) else []


def _group_documents(documents):
    groups = {}
    for document in documents:
        key = family(document["filename"])
        groups.setdefault(key, {"family": key, "documents": []})["documents"].append(document)
    return list(groups.values())


def document_plan(question, documents):
    """Resolve explicit comparisons one target at a time, without mixing years."""
    targets = _comparison_targets(question)
    if not targets:
        return _single_document_plan(question, documents)
    if len(targets) > 3:
        return {"status": "ambiguous", "documents": [], "message": "一次请明确比较 2～3 份资料，其余资料可分批比较。"}
    resolved, groups = {}, []
    for target in targets:
        plan = _single_document_plan(target, documents)
        if plan["status"] != "matched":
            status = "ambiguous" if plan["status"] == "ambiguous" else "not_found"
            reason = "对应多份资料，请补充完整文件名" if status == "ambiguous" else "未在当前应用的已发布资料中找到"
            return {"status": status, "documents": plan["documents"], "comparison_target": target,
                    "message": f"比较对象 {target} {reason}；尚未开始比较。"}
        group = _group_documents(plan["documents"])[0]
        group["target"] = target
        if group["family"] not in {item["family"] for item in groups}:
            groups.append(group)
        resolved.update({item["id"]: item for item in plan["documents"]})
    if len(groups) < 2:
        return {"status": "ambiguous", "documents": list(resolved.values()),
                "message": "比较对象指向同一组资料，请明确另外一份资料的完整名称。"}
    return {"status": "comparison", "documents": list(resolved.values()), "comparison_groups": groups}


def catalog(db, releases):
    referenced = {s["document_id"] for _, snap in releases for n in snap["nodes"]
                  for s in n["sources"] if s.get("document_id")}
    kb_ids = {r.kb_id for r, _ in releases}
    documents = {d.id: d for d in db.scalars(select(Document).where(
        Document.id.in_(referenced), Document.kb_id.in_(kb_ids), ~Document.withdrawn))}
    records = {}
    for release, snap in releases:
        for node in snap["nodes"]:
            source_docs = []
            for doc_id in sorted({s.get("document_id") for s in node["sources"] if s.get("document_id")}):
                doc = documents.get(doc_id)
                if not doc or doc.kb_id != release.kb_id:
                    continue
                sources = [s for s in node["sources"] if s.get("document_id") == doc_id]
                source_docs.append({"id": doc.id, "filename": doc.filename, "version": doc.version,
                    "pages": sorted({s["page"] for s in sources if s.get("page")}),
                    "locators": list(dict.fromkeys(s["locator"] for s in sources))})
            records[node["id"]] = {**node, "release_id": release.id, "documents": source_docs,
                                   "path": [node["title"]]}
    return records, documents


def tokens(value):
    result = []
    for word in re.findall(r"[a-z]+|\d+|[\u4e00-\u9fff]+", unicodedata.normalize("NFKC", value).casefold()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", word) and len(word) > 1:
            result.extend(word[i:i+2] for i in range(len(word)-1))
        else:
            result.append(word)
    return result


def _fingerprint(records, scope=(), edges=None):
    """Fingerprint effective published content, including immediate withdrawals.

    Include content as well as release IDs so cached indexes remain safe when
    an administrator repairs metadata or a test uses synthetic snapshots.
    """
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode())
    for key, node in sorted(records.items()):
        value = [key, node.get("release_id"), node["title"], node["content"], node["attributes"].get("chapter", ""),
                 sorted((d["id"], d["filename"], d.get("version")) for d in node["documents"])]
        digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
    if edges is not None:
        digest.update(json.dumps(sorted((e.get("id"), e["source_id"], e["target_id"], e.get("kind"))
                                       for e in edges), sort_keys=True).encode())
    return digest.hexdigest()


def _lexical_index(records):
    counts = {key: Counter(tokens("\n".join([n["title"], n["content"],
        str(n["attributes"].get("chapter", "")), *[d["filename"] for d in n["documents"]]])))
        for key, n in records.items()}
    lengths = {k: sum(c.values()) for k, c in counts.items()}
    average = sum(lengths.values()) / max(len(counts), 1) or 1
    frequency = Counter(term for counter in counts.values() for term in counter)
    # Approximate Python mapping/string storage, bounded across both index kinds.
    weight = sum(96 + len(term.encode()) for counter in counts.values() for term in counter)
    weight += sum(96 + len(term.encode()) for term in frequency) + 128 * len(counts)
    return (counts, lengths, average, frequency), weight


def lexical_ranking(records, question, cache_scope=()):
    """BM25 over body, title, chapter and inherited source filenames (Chinese bigrams)."""
    terms = set(tokens(question))
    counts, lengths, average, frequency = indexes.get_or_create(
        ("lexical", _fingerprint(records, cache_scope)), lambda: _lexical_index(records))
    ranked = []
    for key, count in counts.items():
        score = sum(math.log(1 + (len(counts)-frequency[t]+.5)/(frequency[t]+.5)) *
                    count[t]*2.2/(count[t]+1.2*(.25+.75*lengths[key]/average)) for t in terms if count[t])
        if score > 0:
            ranked.append((key, score))
    return sorted(ranked, key=lambda item: (-item[1], item[0]))[:20]


def source_order(node):
    pages = [p for d in node["documents"] for p in d["pages"]]
    return (min(pages, default=0), node["attributes"].get("splitting", {}).get("chunk_index", 0), node["id"])


def overview_order(records):
    """Spread a bounded context across sections/pages rather than only the first pages."""
    sections = defaultdict(list)
    for n in sorted(records.values(), key=source_order):
        chapter = n["attributes"].get("chapter", "")
        if not chapter or re.search(r"第\s*\d+\s*页|内容块|段落\s*\d+", chapter):
            chapter = str(source_order(n)[0])
        sections[(tuple(d["id"] for d in n["documents"]), chapter)].append(n)
    groups = list(sections.values())
    count = min(len(groups), MAX_OVERVIEW_UNITS)
    while count > 1:
        sampled = [groups[round(i*(len(groups)-1)/(count-1))] for i in range(count)]
        if sum(len(group[0]["content"]) for group in sampled) <= MAX_CONTEXT_CHARACTERS:
            groups = sampled
            break
        count -= 1
    if count == 1:
        groups = groups[:1]
    return [group[i] for i in range(max(map(len, groups), default=0)) for group in groups if i < len(group)]


def bounded_evidence(ordered, context, limit, available, *, character_budget=None):
    selected, remaining, seen = [], MAX_CONTEXT_CHARACTERS if character_budget is None else character_budget, set()
    for node in ordered:
        if len(selected) == limit or remaining <= 0:
            break
        if node["id"] in seen:
            continue
        seen.add(node["id"])
        # Keep normal units intact. An oversized single unit is explicitly marked partial.
        if len(node["content"]) > remaining and selected:
            continue
        item = dict(node)
        if len(item["content"]) > remaining:
            item["content"] = item["content"][:remaining]
            item["excerpt_truncated"] = True
        remaining -= len(item["content"])
        selected.append(item)
    context["coverage"] = {"available_units": available, "selected_units": len(selected),
        "complete": len(selected) == available and not any(n.get("excerpt_truncated") for n in selected)}
    return Evidence(selected, context)


def _balanced_evidence(ordered_groups, context, limit):
    """Each requested document family owns its share of both input budgets."""
    selected, seen, coverage = [], set(), []
    count = len(ordered_groups)
    for index, (group, ordered, available) in enumerate(ordered_groups):
        group_limit = limit // count + (index < limit % count)
        budget = MAX_CONTEXT_CHARACTERS // count + (index < MAX_CONTEXT_CHARACTERS % count)
        items = bounded_evidence(ordered, {}, group_limit, available, character_budget=budget)
        coverage.append({"family": group["family"], "documents": group["documents"], **items.context["coverage"]})
        for item in items:
            if item["id"] not in seen:
                selected.append(item)
                seen.add(item["id"])
    context["comparison_coverage"] = coverage
    context["coverage"] = {"available_units": sum(item["available_units"] for item in coverage),
        "selected_units": len(selected), "complete": all(item["complete"] and item["selected_units"] for item in coverage)}
    if any(not item["selected_units"] for item in coverage):
        context["message"] = "部分比较对象未检索到可用正文；只能比较已有证据支持的部分，请补充具体题目或章节。"
    return Evidence(selected, context)


def assert_read_only(db):
    """Catch unflushed edits; the opt-in caller must own a dedicated read session.

    SQLAlchemy's pending collections do not reveal already-flushed writes. Such
    transactions must use the default release_read_connection=False path.
    """
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("Releasing a retrieval connection requires a dedicated read-only session")


def retrieve(db, app, releases, question, use_graph=True, *, vector_only=False, allowed_document_ids=None,
             topic_hint="", inherited_document_ids=None, release_read_connection=False):
    """The current question controls intent; historical topics only aid ranking.

    ``allowed_document_ids`` is a hard tool permission boundary. Inherited IDs
    are weaker dialogue references and apply only when the current question
    does not explicitly select a new target. Both are intersected with the
    caller's current, withdrawal-filtered publication scope.

    ``release_read_connection`` is opt-in for the dedicated chat read session.
    Other callers retain ownership of their transaction, including flushed writes.
    """
    if release_read_connection:
        assert_read_only(db)
    records, documents = catalog(db, releases)
    if allowed_document_ids is not None:
        documents = {k: d for k, d in documents.items() if k in allowed_document_ids}
        records = {k: n for k, n in records.items()
                   if any(d["id"] in allowed_document_ids for d in n["documents"])}
    plan = {"status": "general", "documents": []} if vector_only else document_plan(question, documents)
    if not vector_only and plan["status"] == "general":
        inherited = set(inherited_document_ids or ())
        constrained = inherited or (set(allowed_document_ids) if allowed_document_ids is not None else set())
        selected = [{"id": doc.id, "filename": doc.filename, "version": doc.version}
                    for doc in documents.values() if doc.id in constrained]
        if constrained and {doc["id"] for doc in selected} != constrained:
            plan = {"status": "not_found", "documents": [], "message": "此前指定的部分资料已不可用，请重新选择当前已发布资料。"}
        elif selected:
            groups = _group_documents(selected)
            if len(groups) > 3:
                plan = {"status": "ambiguous", "documents": selected,
                        "message": "当前关联多份资料，请明确本轮需要的 2～3 份资料。"}
            else:
                plan = {"status": "comparison" if len(groups) > 1 else "matched", "documents": selected,
                        "inherited_scope": True}
                if len(groups) > 1:
                    plan["comparison_groups"] = groups
    context = {"strategy": "vector" if vector_only else "hybrid", **plan}
    if plan["status"] in {"not_found", "ambiguous"}:
        context["strategy"] = "document_lookup"
        if plan.get("message"):
            pass
        elif plan["status"] == "ambiguous":
            names = "、".join(d["filename"] for d in plan["documents"][:6])
            context["message"] = f"找到多份可能的资料：{names}。请补充课程、年份或完整文件名。"
        else:
            context["message"] = "未在当前应用的已发布资料中定位到所指定的文档，请核对年份、课程或文件名。"
        return Evidence(context=context)
    scoped = plan["status"] in {"matched", "comparison"}
    if scoped:
        doc_ids = {d["id"] for d in plan["documents"]}
        records = {k: n for k, n in records.items() if any(d["id"] in doc_ids for d in n["documents"])}
        context["strategy"] = "document_search"
    comparison = plan["status"] == "comparison"
    if scoped and OVERVIEW.search(question):
        context["strategy"] = "comparison_overview" if comparison else "document_overview"
        body = {k: n for k, n in records.items() if not noise(n)}
        all_body = body
        want_answers = bool(re.search(r"答案|解析|解答(?!题)", question))
        preferred = {k: n for k, n in body.items() if any(
            bool(ANSWER.search(d["filename"])) == want_answers for d in n["documents"])}
        body = preferred or body
        if not body:
            context["message"] = "已定位到资料，但当前发布内容只有标题、页码或考生信息，尚不足以概括正文。"
        if comparison:
            grouped = []
            for group in plan["comparison_groups"]:
                doc_ids = {doc["id"] for doc in group["documents"]}
                members = {key: node for key, node in all_body.items()
                           if any(doc["id"] in doc_ids for doc in node["documents"])}
                preferred = {key: node for key, node in members.items() if any(
                    bool(ANSWER.search(doc["filename"])) == want_answers for doc in node["documents"])}
                members = preferred or members
                grouped.append((group, [{**node, "score": 1.0} for node in overview_order(members)], len(members)))
            return _balanced_evidence(grouped, context, MAX_OVERVIEW_UNITS)
        result = bounded_evidence([{**n, "score": 1.0} for n in overview_order(body)], context,
                                  MAX_OVERVIEW_UNITS, len(body))
        result.sort(key=source_order)
        return result

    profile = db.get(ModelProfile, app.published["embedding_profile_id"])
    signature = embedding_signature(profile)
    search_query = question + ("\n" + topic_hint if topic_hint else "")
    cache_scope = (app.id, app.version)
    if release_read_connection:
        # SQLAlchemy expires ORM attributes on commit by default. Nothing used
        # while awaiting embedding may lazy-load and reacquire this connection.
        profile = SimpleNamespace(**{key: getattr(profile, key) for key in (
            "id", "provider", "model", "base_url", "secret", "temperature", "dimension")})
        releases = [(SimpleNamespace(id=release.id, kb_id=release.kb_id), snapshot)
                    for release, snapshot in releases]
        assert_read_only(db)
        db.commit()
    qvec = embed([search_query], profile)[0]
    if comparison:
        context["strategy"] = "comparison_search"
        grouped = []
        for group in plan["comparison_groups"]:
            doc_ids = {doc["id"] for doc in group["documents"]}
            members = {key: node for key, node in records.items()
                       if any(doc["id"] in doc_ids for doc in node["documents"])}
            ordered = _ranked_records(db, releases, members, question, search_query, qvec, signature,
                                      use_graph, vector_only, cache_scope)
            grouped.append((group, ordered, len(members)))
        return _balanced_evidence(grouped, context, 8)
    ordered = _ranked_records(db, releases, records, question, search_query, qvec, signature,
                              use_graph, vector_only, cache_scope)
    return bounded_evidence(ordered, context, 8, len(records))


def _ranked_records(db, releases, records, question, search_query, qvec, signature,
                    use_graph, vector_only, cache_scope):
    scores = defaultdict(float)
    if not vector_only:
        for rank, (key, _) in enumerate(lexical_ranking(records, search_query, cache_scope), 1):
            scores[key] += .4 * 21/(20+rank)
    for release, _ in releases:
        nodes = {k: n for k, n in records.items() if n["release_id"] == release.id}
        if not nodes:
            continue
        index = db.scalar(select(EmbeddingIndex).where(EmbeddingIndex.release_id == release.id,
                                                       EmbeddingIndex.signature == signature))
        if not index:
            raise HTTPException(409, "Embedding 配置的索引尚未就绪，请管理员重新发布应用")
        stmt = select(ReleaseVector).where(ReleaseVector.release_id == release.id,
            ReleaseVector.signature == signature, ReleaseVector.knowledge_id.in_(nodes))
        if db.bind.dialect.name == "postgresql":
            stmt = stmt.order_by(ReleaseVector.embedding.cosine_distance(qvec)).limit(20)
        def cosine(vector):
            denom = math.sqrt(sum(x*x for x in vector)*sum(x*x for x in qvec)) or 1
            return sum(float(a)*b for a, b in zip(vector, qvec, strict=True))/denom
        ranked = sorted(((row.knowledge_id, cosine(row.embedding)) for row in db.scalars(stmt)),
                        key=lambda pair: (-pair[1], pair[0]))[:20]
        for rank, (key, similarity) in enumerate(ranked, 1):
            if similarity >= .2:
                scores[key] += similarity if vector_only else .6 * 21/(20+rank)
    if not vector_only and not re.search(r"标题|文件名|考生信息|姓名|学号", question):
        for key in scores:
            if noise(records[key]):
                scores[key] *= .05
    seeds = sorted(scores, key=lambda k: (-scores[k], k))[:8]
    if use_graph and not vector_only and seeds:
        for release, snapshot in releases:
            nodes = {k: n for k, n in records.items() if n["release_id"] == release.id}
            edges = [e for e in snapshot["edges"] if e["source_id"] in nodes and e["target_id"] in nodes]
            if not edges:
                continue
            def build_graph():
                graph, _ = make_graph(list(nodes.values()), edges, bidirectional=True)
                graph = nx.freeze(graph.to_undirected())
                return graph, 128 * len(nodes) + 256 * len(edges)
            graph = indexes.get_or_create(("graph", _fingerprint(nodes, cache_scope, edges)), build_graph)
            for key in [k for k in seeds if k in nodes][:4]:
                for neighbor, path in nx.single_source_shortest_path(graph, key, cutoff=2).items():
                    score = scores[key] * .82**(len(path)-1)
                    if score > scores.get(neighbor, 0):
                        scores[neighbor] = score
                        records[neighbor]["path"] = [nodes[p]["title"] for p in path]
    return [{**records[key], "score": round(score, 4)} for key, score in
            sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))]
