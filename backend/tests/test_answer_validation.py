import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db import engine
from app.models import ModelProfile
from app.schemas import ChatInput
from app.services import answer_validation, learning
from app.services.providers import ProviderError
from .test_streaming import events


@pytest.fixture
def evidence():
    return [
        {"id": "node-1", "title": "求导", "content": "当 x 为实数时，\nf(x)=x^2 的导数为 2x。\n可导函数一定连续。",
         "attributes": {}, "release_id": "release-1", "sources": [], "path": [], "score": .9},
        {"id": "node-2", "title": "积分", "content": "函数 2x 的一个原函数是 x^2。常数 C 表示任意积分常数。",
         "attributes": {}, "release_id": "release-1", "sources": [], "path": [], "score": .8},
    ]


def response(answer="导数为 2x [1]。", ids=None, **extra):
    return {"status": "answered", "answer": answer, "citation_ids": ["1"] if ids is None else ids, **extra}


def support(claim="导数为 2x", identifier="1", quote="f(x)=x^2 的导数为 2x。"):
    return {"claim": claim, "citation_id": identifier, "quote": quote}


def test_valid_supports_are_saved_with_explicit_nonsemantic_validation(evidence):
    result = {"retrieval": {"status": "matched"}}
    original = response(supports=[support()])
    learning.apply_answer(result, original, evidence)
    assert result["supports"] == [support()]
    assert result["support_validation"]["kind"] == "exact_excerpt"
    assert "不代表语义正确性审查" in result["support_validation"]["note"]
    assert result["citations"][0]["knowledge_id"] == "node-1"
    assert result["retrieval"] == {"status": "matched"}
    assert original == response(supports=[support()])


def test_legacy_answer_remains_compatible_but_not_marked_excerpt_checked(evidence):
    result = {}
    learning.apply_answer(result, response(), evidence)
    assert result["supports"] == []
    assert result["support_validation"]["kind"] == "not_provided"
    assert result["answer"] == "导数为 2x [1]。"


@pytest.mark.parametrize("text", ["", "  \n\t", None, 123, []])
def test_empty_or_nontext_answer_is_rejected_without_mutating_result(evidence, text):
    result = {"answer": "unchanged"}
    with pytest.raises(ProviderError) as error:
        learning.apply_answer(result, response(answer=text), evidence)
    assert error.value.code == "invalid_answer"
    assert result == {"answer": "unchanged"}


@pytest.mark.parametrize("ids", [[1], [True], [1.0], [None], [{}], [["1"]], ["01"], ["0"],
                                ["-1"], ["1.0"], [" 1"], ["999"], ["1", "1"], "1", None])
def test_citation_ids_are_strict_valid_unique_strings(evidence, ids):
    payload = response()
    payload["citation_ids"] = ids
    with pytest.raises(ProviderError) as error:
        learning.apply_answer({}, payload, evidence)
    assert error.value.code == "invalid_citations"


@pytest.mark.parametrize("answer,ids", [
    ("没有正文引用", ["1"]), ("正文只引用一条 [1]。", ["1", "2"]),
    ("正文有未声明引用 [2]。", ["1"]), ("正文含越界引用 [999]。", ["1"]),
    ("正文标记无效 [01]。", ["1"]), ("正文无引用", []),
    ("[1]", ["1"]), ("* [1] 求导资料", ["1"]), ("1. [1] 求导资料", ["1"]),
    ("没有依据的结论。\n\n参考资料：[1]", ["1"]),
    ("没有依据的结论。\n\n参考资料 [1]", ["1"]),
    ("没有依据的结论。\n\n参考：[1]", ["1"]),
    ("没有依据的结论。\n## 参考文献\n[1] 求导资料", ["1"]),
    ("没有依据的结论。\nReferences:\n[1] Course notes", ["1"]),
])
def test_citation_list_must_equal_references_attached_to_body(evidence, answer, ids):
    with pytest.raises(ProviderError):
        learning.apply_answer({}, response(answer, ids), evidence)


@pytest.mark.parametrize("expression", [
    r"\([999]\)", r"\[[999]\]", "$[999]$", "$$[999]$$", "`values[999]`",
    "```python\nvalues[999]\n```", "~~~text\n[999]\n~~~", "[999](https://example.invalid)",
    "[999][source]", r"\[999]", "区间 [0, 1] 和矩阵 [[1, 2], [3, 4]]",
])
def test_math_code_and_markdown_links_are_not_citations(evidence, expression):
    result = {}
    answer = f"示例：{expression}\n根据定义导数为 2x [1]。"
    learning.apply_answer(result, response(answer), evidence)
    assert [item["number"] for item in result["citations"]] == [1]


@pytest.mark.parametrize("answer", [r"只有数学 \([1]\)", "只有代码 `[1]`", "[1](https://example.invalid)"])
def test_only_numeric_math_or_links_cannot_satisfy_citation_requirement(evidence, answer):
    with pytest.raises(ProviderError):
        learning.apply_answer({}, response(answer), evidence)


def test_adjacent_citations_lists_and_math_conclusions_remain_valid(evidence):
    answer = "- 求导与积分相互关联 [1][2]。\n\\(f'(x)=2x\\) [1]。\n\n参考资料\n[1] 求导\n[2] 积分"
    result = {}
    learning.apply_answer(result, response(answer, ["2", "1"]), evidence)
    assert [item["number"] for item in result["citations"]] == [2, 1]


def test_support_quote_allows_whitespace_normalization_but_no_source_switch(evidence):
    result = {}
    mapping = support(quote="当 x 为实数时， f(x)=x^2 的导数为 2x。")
    learning.apply_answer(result, response(supports=[mapping]), evidence)
    assert result["supports"] == [mapping]
    mapping["quote"] = evidence[1]["content"]
    with pytest.raises(ProviderError) as error:
        learning.apply_answer({}, response(supports=[mapping]), evidence)
    assert error.value.code == "invalid_support_quote"


@pytest.mark.parametrize("quote", ["f(x)=x^2 的导数为 3x。", "求导", "f(x)=x^2 … 可导函数一定连续。",
                                  "当x为实数时，f(x)=x^2的导数为2x。"])
def test_fabricated_or_noncontiguous_quotes_are_rejected(evidence, quote):
    with pytest.raises(ProviderError) as error:
        learning.apply_answer({}, response(supports=[support(quote=quote)]), evidence)
    assert error.value.code == "invalid_support_quote"


@pytest.mark.parametrize("claim", ["导数为 3x", "根本没回答的结论", "参考资料里的伪结论", "代码内的伪结论"])
def test_support_claim_must_appear_in_actual_body(evidence, claim):
    answer = "导数为 2x [1]。\n`代码内的伪结论`\n## 参考资料\n[1] 参考资料里的伪结论"
    with pytest.raises(ProviderError) as error:
        learning.apply_answer({}, response(answer, supports=[support(claim=claim)]), evidence)
    assert error.value.code == "unsupported_claim"


@pytest.mark.parametrize("value", [None, {}, "support", [None], [support(identifier=1)], [support(identifier="2")],
                                  [support(claim=" ")], [support(quote=" ")], [support(claim="。")],
                                  [support(quote="。")], [{**support(), "trusted": True}],
                                  [{"claim": "导数为 2x", "citation_id": "1"}]])
def test_support_fields_cannot_grant_unchecked_provenance(evidence, value):
    with pytest.raises(ProviderError) as error:
        learning.apply_answer({}, response(supports=value), evidence)
    assert error.value.code == "invalid_supports"


def test_support_lengths_count_and_total_budget_are_enforced(evidence, monkeypatch):
    payloads = [[support(claim="x" * 2001)], [support(quote="x" * 4001)], [support()] * 33]
    for payload in payloads:
        with pytest.raises(ProviderError) as error:
            learning.apply_answer({}, response(supports=payload), evidence)
        assert error.value.code == "invalid_supports"
    monkeypatch.setattr(answer_validation, "MAX_SUPPORT_TEXT", 5)
    with pytest.raises(ProviderError) as error:
        learning.apply_answer({}, response(supports=[support()]), evidence)
    assert error.value.code == "invalid_supports"


def test_duplicate_supports_fail_and_failed_validation_is_atomic(evidence):
    result = {"answer": "previous", "citations": []}
    original = copy.deepcopy(result)
    with pytest.raises(ProviderError) as error:
        learning.apply_answer(result, response(supports=[support(), support()]), evidence)
    assert error.value.code == "invalid_supports"
    assert result == original


@pytest.mark.parametrize("status", ["needs_input", "insufficient_evidence"])
def test_uncertain_answers_can_have_no_citations_or_supports(evidence, status):
    result = {}
    learning.apply_answer(result, response("请补充题目条件。", [], status=status), evidence)
    assert result["status"] == status
    assert result["citations"] == result["supports"] == []


def test_original_text_correspondence_does_not_claim_semantic_entailment(evidence):
    result = {}
    # Matching text cannot establish that the source logically proves a claim.
    mapping = support(claim="所有连续函数都可导", quote="可导函数一定连续。")
    learning.apply_answer(result, response("所有连续函数都可导 [1]。", supports=[mapping]), evidence)
    assert result["support_validation"]["kind"] == "exact_excerpt"
    assert "不代表语义正确性审查" in result["support_validation"]["note"]


def test_long_answers_are_preserved_without_new_output_budget(evidence):
    answer = "导数为 2x [1]。\n" * 4000
    result = {}
    learning.apply_answer(result, response(answer), evidence)
    assert result["answer"] == answer


def test_generation_prompt_requests_exact_excerpt_contract(evidence):
    system, raw = learning.generation_prompt(SimpleNamespace(published={"prompt": "课程助教"}),
                                             ChatInput(message="求导"), evidence)
    assert "supports=[{claim,citation_id,quote}]" in system
    assert "逐字摘取对应evidence.content中的连续原文" in system
    assert "不得仅在参考资料列表挂编号" in system
    assert "不意味着系统已证明结论" in system
    assert json.loads(raw)["evidence"][0]["content"] == evidence[0]["content"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("valid", [False, True])
def test_only_verified_excerpt_mappings_are_saved_in_chat_history(client, platform, monkeypatch, stream, valid):
    bundle = platform["bundles"][0]
    app_id = bundle["app"]["id"]
    quote = bundle["node"]["content"] if valid else "这句话并没有出现在原文中。"
    payload = response("资料包含导数知识 [1]。", supports=[support("资料包含导数知识", quote=quote)])
    with Session(engine()) as db:
        db.get(ModelProfile, platform["generation"]["id"]).provider = "openai"
        db.commit()
    monkeypatch.setattr(learning, "generate", lambda *args, **kwargs: payload)

    async def provider_stream(*args, **kwargs):
        raw = json.dumps(payload, ensure_ascii=False)
        for start in range(0, len(raw), 7):
            yield raw[start:start + 7]

    monkeypatch.setattr("app.services.streaming.stream_generate", provider_stream)
    response_http = client.post(f"/api/apps/{app_id}/chat" + ("/stream" if stream else ""),
                                json={"message": "导数知识"})
    if stream:
        frames = events(response_http)
        assert frames[-1][0] == ("done" if valid else "error")
        result = frames[-1][1]
    else:
        assert response_http.status_code == (200 if valid else 503)
        result = response_http.json()
    conversations = client.get(f"/api/apps/{app_id}/conversations").json()
    assert len(conversations) == (1 if valid else 0)
    if valid:
        saved = client.get(f"/api/apps/{app_id}/conversations/{result['conversation_id']}").json()[0]["result"]
        assert saved["supports"] == payload["supports"]
        assert saved["support_validation"]["kind"] == "exact_excerpt"
