import json
from types import SimpleNamespace

import pytest

from app.services import splitting as split
from app.services.providers import ProviderError
from .conftest import post


def profile():
    return SimpleNamespace(id="model", name="ds-flash", model="deepseek-flash", provider="openai",
                           purpose="generation", base_url="https://example.com", temperature=0.1)


def blocks():
    return [{"text": "拉格朗日中值定理：", "page": 1, "locator": "第1页块1"},
            {"text": "设 f 在 [a,b] 连续，在 (a,b) 可导。", "page": 1, "locator": "第1页块2"},
            {"text": r"$$f'(c)=\frac{f(b)-f(a)}{b-a}$$", "page": 2, "locator": "第2页块1", "label": "formula"},
            {"text": "练习：计算 x² 的导数。", "page": 2, "locator": "第2页块2"}]


@pytest.mark.parametrize("method, count", [("document", 1), ("page", 2), ("paragraph", 4), ("fixed", 1), ("recursive", None)])
def test_rules_preserve_source_characters_without_model(monkeypatch, method, count):
    monkeypatch.setattr(split, "generate", lambda *a, **kw: pytest.fail("规则方法不应调用模型"))
    chunks, report = split.split_blocks(blocks(), {"method": method, "chunk_size": 100})
    if count is not None:
        assert len(chunks) == count
    assert report["coverage"] == 1 and report["calls"] == 0
    text, _ = split.normalize(blocks())
    for chunk in chunks:
        assert chunk["content"] == text[chunk["start"]:chunk["end"]].strip()
        assert all(s["excerpt"] in blocks()[s["block_index"]]["text"] for s in chunk["sources"])


def test_semantic_groups_cross_page_and_cache_is_model_specific(tmp_path, monkeypatch):
    calls = []
    def generate(*args, **kwargs):
        calls.append(args)
        return {"groups": [{"end": 2, "title": "拉格朗日中值定理及适用条件", "kind": "theorem"},
                           {"end": 3, "title": "幂函数求导练习", "kind": "example"}]}
    monkeypatch.setattr(split, "generate", generate)
    chunks, report = split.split_blocks(blocks(), {"method": "semantic"}, profile(), tmp_path)
    assert len(chunks) == 2 and len(chunks[0]["sources"]) == 3
    assert "连续" in chunks[0]["content"] and r"\frac" in chunks[0]["content"]
    assert report["model"] == "deepseek-flash" and report["calls"] == 1
    repeated, report = split.split_blocks(blocks(), {"method": "semantic"}, profile(), tmp_path)
    assert repeated == chunks and report["cache_hits"] == 1 and len(calls) == 1
    changed = profile()
    changed.model = "different"
    split.split_blocks(blocks(), {"method": "semantic"}, changed, tmp_path)
    assert len(calls) == 2


def test_model_windows_carry_last_group_without_loss_or_duplicates(monkeypatch):
    data = [{"text": f"概念{i} " + "内容"*150, "locator": f"段落{i}"} for i in range(130)]
    requests = []
    def generate(_profile, _system, prompt, **kwargs):
        ids = [b["id"] for b in json.loads(prompt)["blocks"]]
        requests.append(ids)
        groups = [{"end": ids[i+1] if i+1 < len(ids) else ids[i], "title": "知识单元", "kind": "note"}
                  for i in range(0, len(ids), 2)]
        return {"groups": groups}
    monkeypatch.setattr(split, "generate", generate)
    chunks, report = split.split_blocks(data, {"method": "semantic"}, profile())
    assert len(requests) > 1 and requests[1][0] in requests[0]
    assert [s["block_index"] for c in chunks for s in c["sources"]] == list(range(130))
    assert report["coverage"] == 1


@pytest.mark.parametrize("groups", [[], [{"end": 2, "title": "遗漏", "kind": "note"}],
    [{"end": 8, "title": "越界", "kind": "note"}], [{"end": True, "title": "类型", "kind": "note"}],
    [{"end": 2, "title": "重复", "kind": "note"}, {"end": 1, "title": "重复", "kind": "note"}]])
def test_invalid_model_boundaries_never_fall_back(groups, monkeypatch):
    monkeypatch.setattr(split, "generate", lambda *a, **kw: {"groups": groups})
    monkeypatch.setattr(split.time, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="未退回普通切分"):
        split.split_blocks(blocks(), {"method": "semantic"}, profile())


def test_unavailable_model_cannot_create_rule_chunks(monkeypatch):
    def failure(*a, **kw):
        raise ProviderError("模型请求超时")
    monkeypatch.setattr(split, "generate", failure)
    monkeypatch.setattr(split.time, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="模型请求超时"):
        split.split_blocks(blocks(), {"method": "semantic"}, profile())


def test_word_has_no_fake_pages_and_options_are_validated():
    with pytest.raises(ValueError, match="稳定页码"):
        split.split_blocks([{"text": "文本", "locator": "段落1"}], {"method": "page"})
    with pytest.raises(ValueError):
        split.split_blocks(blocks(), {"method": "semantic", "overlap": 20}, profile())


def test_api_split_dispatch_and_pdf_requires_ocr(client, platform, monkeypatch):
    url = f"/api/admin/knowledge-bases/{platform['bundles'][0]['kb']['id']}/documents"
    bad = client.post(url, data={"splitting": json.dumps({"method": "semantic"})},
                      files={"file": ("note.txt", b"source")})
    assert bad.status_code == 422
    model = post(client, "/admin/model-profiles", {"name": "Real", "provider": "openai", "model": "flash", "base_url": "https://example.com/v1"})
    monkeypatch.setattr(split, "generate", lambda *a, **kw: {"groups": [{"end": 0, "title": "原文概念", "kind": "concept"}]})
    good = client.post(url, data={"splitting": json.dumps({"method": "semantic", "model_profile_id": model["id"]})},
                       files={"file": ("note.txt", "函数的极限定义".encode())})
    assert good.status_code == 200 and good.json()["task"]["state"] == "succeeded", good.text
    doc = good.json()["document"]["id"]
    report = client.get(f"/api/admin/documents/{doc}/splitting").json()["report"]
    assert report["method"] == "semantic" and report["model_profile_id"] == model["id"]
    pdf = client.post(url, data={"mode": "text"}, files={"file": ("file.pdf", b"dummy pdf")})
    assert pdf.status_code == 200 and pdf.json()["task"]["state"] == "failed"
    assert "必须先进行 OCR" in pdf.json()["task"]["error"]
    post(client, "/auth/register", {"username": "student", "password": "test-password"})
    assert client.get(f"/api/admin/documents/{doc}/splitting").status_code == 403
