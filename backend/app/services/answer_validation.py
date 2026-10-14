"""Deterministic citation and excerpt checks, without semantic-entailment claims."""
import re

from .providers import ProviderError

MAX_SUPPORTS = 32
MAX_CLAIM_CHARS = 2000
MAX_QUOTE_CHARS = 4000
MAX_SUPPORT_TEXT = 32000
CITATION = re.compile(r"(?<!\\)\[([0-9]+)\]")
IDENTIFIER = re.compile(r"[1-9][0-9]*\Z")
CODE = re.compile(r"```[\s\S]*?(?:```|\Z)|~~~[\s\S]*?(?:~~~|\Z)|`[^`\n]+`")
MATH = re.compile(r"\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$|(?<!\\)\$(?!\$)[^\n]*?(?<!\\)\$")
NUMERIC_LINK = re.compile(r"!?\[[0-9]+\]\s*(?:\([^\n)]*\)|\[(?![0-9]+\])[^\n\]]*\])")
BIBLIOGRAPHY = re.compile(
    r"^(?:参考(?:资料|文献)?|引用(?:来源|资料|文献)?|资料来源|来源|出处|references|sources|bibliography)"
    r"\s*(?:[:：].*|(?:\[[0-9]+\]\s*[,，、]?\s*)+)?$",
    re.I,
)
LIST_PREFIX = re.compile(r"^\s*(?:[-+*]|[0-9]+[.)、])\s*")


def _mask(text):
    # Keep offsets and line boundaries intact for matching claims to displayed prose.
    return "".join("\n" if char == "\n" else " " for char in text)


def _normal(text):
    return re.sub(r"\s+", " ", text).strip()


def _views(answer):
    prose = CODE.sub(lambda match: _mask(match[0]), answer)
    citation_text = MATH.sub(lambda match: _mask(match[0]), prose)
    citation_text = NUMERIC_LINK.sub(lambda match: _mask(match[0]), citation_text)
    body, in_bibliography = [], False
    for line in prose.splitlines(keepends=True):
        title = line.strip().strip("#* ")
        if BIBLIOGRAPHY.fullmatch(title):
            in_bibliography = True
        elif re.match(r"^\s*#{1,6}\s+", line):
            in_bibliography = False
        body.append(_mask(line) if in_bibliography else line)
    return "".join(body), citation_text


def _inline_citations(answer):
    body, citation_text = _views(answer)
    found, inline = set(), set()
    for match in CITATION.finditer(citation_text):
        identifier = match[1]
        found.add(identifier)
        if not body[match.start():match.end()].strip():
            continue  # A trailing bibliography alone does not support the answer.
        start = max(body.rfind("\n", 0, match.start()), body.rfind("\r", 0, match.start())) + 1
        prefix = CITATION.sub("", LIST_PREFIX.sub("", body[start:match.start()]))
        if any(char.isalnum() for char in prefix):
            inline.add(identifier)
    return body, found, inline


def _supports(value, body, ids, evidence):
    if not isinstance(value, list) or len(value) > MAX_SUPPORTS:
        raise ProviderError("模型原文对应记录必须是有长度限制的数组，请重试", code="invalid_supports")
    checked, total, seen = [], 0, set()
    normalized_body = _normal(body)
    for support in value:
        if not isinstance(support, dict) or set(support) != {"claim", "citation_id", "quote"}:
            raise ProviderError("模型原文对应记录格式不正确，请重试", code="invalid_supports")
        claim, identifier, quote = support["claim"], support["citation_id"], support["quote"]
        if (not isinstance(claim, str) or not 1 <= len(claim) <= MAX_CLAIM_CHARS
                or not isinstance(quote, str) or not 1 <= len(quote) <= MAX_QUOTE_CHARS
                or not isinstance(identifier, str) or identifier not in ids):
            raise ProviderError("模型原文对应记录字段或引用无效，请重试", code="invalid_supports")
        total += len(claim) + len(quote)
        if total > MAX_SUPPORT_TEXT:
            raise ProviderError("模型原文对应记录过长，请重试", code="invalid_supports")
        claim, quote = _normal(claim), _normal(quote)
        if (not claim or not quote or not any(char.isalnum() for char in claim)
                or not any(char.isalnum() for char in quote)):
            raise ProviderError("模型原文对应记录不能为空，请重试", code="invalid_supports")
        if claim not in normalized_body:
            raise ProviderError("原文对应记录中的结论未出现在回答正文，请重试", code="unsupported_claim")
        source = evidence[identifier].get("content", "")
        if not isinstance(source, str) or quote not in _normal(source):
            raise ProviderError("原文摘录与所引用的资料不一致，请重试", code="invalid_support_quote")
        key = (claim, identifier, quote)
        if key in seen:
            raise ProviderError("模型原文对应记录包含重复条目，请重试", code="invalid_supports")
        seen.add(key)
        checked.append({"claim": claim, "citation_id": identifier, "quote": quote})
    return checked


def validate_answer(response, evidence):
    if not isinstance(response, dict):
        raise ProviderError("模型回答格式不正确，请重试", code="invalid_answer")
    status, answer = response.get("status"), response.get("answer")
    if (not isinstance(status, str) or status not in {"answered", "needs_input", "insufficient_evidence"}
            or not isinstance(answer, str) or not answer.strip()):
        raise ProviderError("模型回答状态或正文无效，请重试", code="invalid_answer")
    ids = response.get("citation_ids", [])
    allowed = {str(index + 1): item for index, item in enumerate(evidence)}
    if (not isinstance(ids, list) or len(ids) > len(allowed)
            or any(not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier)
                   or identifier not in allowed for identifier in ids)
            or len(set(ids)) != len(ids)):
        raise ProviderError("模型回答引用必须是不重复的有效编号字符串，请重试", code="invalid_citations")
    declared = set(ids)
    body, found, inline = _inline_citations(answer)
    if not found <= declared:
        raise ProviderError("模型正文包含未声明或无效的引用，请重试", code="undeclared_citation")
    if declared != inline or (status == "answered" and not inline):
        raise ProviderError("引用列表须与正文结论中的编号一致，不能仅列出资料编号，请重试", code="invalid_citations")
    supports = _supports(response["supports"], body, declared, allowed) if "supports" in response else []
    validation = {
        "kind": "exact_excerpt" if supports else "not_provided",
        "note": ("仅核对结论出现在正文及摘录来自对应资料，不代表语义正确性审查。" if supports
                 else "模型未提供原文对应记录；已核对正文与引用编号，未进行语义正确性审查。"),
    }
    return {"status": status, "answer": answer, "citation_ids": ids,
            "supports": supports, "support_validation": validation}
