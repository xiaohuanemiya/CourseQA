"""Verify semantic import against every original OCR/native character, without model calls."""
import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

import httpx
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"backend"))
from app.services.ingestion import parse_blocks
from app.services.splitting import normalize


def main(progress_only=False, output_path=Path("artifacts/semantic-reimport-summary.json")):
    run = json.loads(Path("artifacts/semantic-reimport-tasks.json").read_text())
    expected_documents = {entry["document_id"] for entry in run["documents"]}
    originals = Path("AdvancedMathematicsData(1)")
    with httpx.Client(base_url="http://127.0.0.1:8820/api", timeout=180,
                      headers={"X-Requested-With": "calculus-studio"}) as client:
        response = client.post("/auth/login", json={"username": "admin", "password": dotenv_values(".env")["ADMIN_PASSWORD"]})
        response.raise_for_status()
        tasks = []
        for entry in run["documents"]:
            response = client.get("/admin/tasks/"+entry["task_id"])
            response.raise_for_status()
            tasks.append({"filename": entry["filename"], **response.json()})
        if progress_only:
            print(json.dumps({"states": dict(Counter(t["state"] for t in tasks)),
                "tasks": [{k: t.get(k) for k in ["id", "filename", "state", "progress", "error"]} for t in tasks]}, ensure_ascii=False, indent=2))
            return
        assert all(t["state"] == "succeeded" for t in tasks), "还有未成功的语义切分任务"
        kbs = client.get("/admin/knowledge-bases").json()
        summary = {"status": "verified", "method": "semantic", "model": run["model"],
                   "removed_nonsemantic": run["removed"]["knowledge"], "pdf_pages": 0,
                   "documents": [], "knowledge": 0, "kinds": Counter(), "cross_page_chunks": 0,
                   "model_calls": 0, "cache_hits": 0, "source_assets": 0, "workflow": "direct_publish",
                   "outside_batch_documents": []}
        seen = set()
        for kb in kbs:
            response = client.get(f"/admin/knowledge-bases/{kb['id']}/contents")
            response.raise_for_status()
            data = response.json()
            # Verify the recorded import batch, without treating later uploads as batch members.
            summary["outside_batch_documents"].extend(
                {"document_id": d["id"], "filename": d["filename"], "withdrawn": d["withdrawn"]}
                for d in data["documents"] if d["id"] not in expected_documents)
            data["documents"] = [d for d in data["documents"] if d["id"] in expected_documents]
            data["knowledge"] = [n for n in data["knowledge"] if any(
                s.get("document_id") in expected_documents for s in n["sources"])]
            for node in data["knowledge"]:
                assert not node["withdrawn"]
                assert node["attributes"]["splitting"]["method"] == "semantic"
                assert node["attributes"]["splitting"]["model_profile_id"] == run["model_profile_id"]
            for doc in data["documents"]:
                assert not doc["withdrawn"] and doc["state"] in {"ready", "review"}
                path = originals/doc["filename"]
                assert hashlib.sha256(path.read_bytes()).hexdigest() == doc["checksum"]
                seen.add(doc["id"])
                if path.suffix.lower() == ".pdf":
                    from pypdf import PdfReader
                    response = client.get(f"/admin/documents/{doc['id']}/recognition")
                    response.raise_for_status()
                    report = response.json()
                    blocks = report["blocks"]
                    count = len(PdfReader(path).pages)
                    assert [p["page"] for p in report["pages"]] == list(range(1, count+1))
                    summary["pdf_pages"] += count
                    summary["source_assets"] += len(report["assets"])
                else:
                    blocks = [{"text": text, "locator": where} for where, text in parse_blocks(path, path.name)]
                text, spans = normalize(blocks)
                split_report = doc["recognition"]["splitting"]
                assert hashlib.sha256(text.encode()).hexdigest() == split_report["input_sha256"]
                chunks = sorted([n for n in data["knowledge"] if n["sources"][0]["document_id"] == doc["id"]],
                                key=lambda n: n["attributes"]["splitting"]["chunk_index"])
                assert len(chunks) == split_report["chunk_count"]
                cursor, cross_page = 0, 0
                for node in chunks:
                    attributes = node["attributes"]["splitting"]
                    start, end = attributes["range"]
                    assert start == cursor and end > start
                    assert node["content"] == text[start:end].strip()
                    cursor = end
                    indices = attributes["source_block_indices"]
                    assert len(indices) == len(node["sources"])
                    for index, source in zip(indices, node["sources"]):
                        assert source["document_id"] == doc["id"]
                        assert source["locator"] == blocks[index]["locator"]
                        assert source["excerpt"] in blocks[index]["text"]
                        if path.suffix.lower() == ".pdf":
                            assert source["page"] == blocks[index]["page"]
                            assert source["asset_id"] == blocks[index]["asset_id"]
                            assert source["asset_id"] in report["assets"]
                    cross_page += len({s.get("page") for s in node["sources"]}) > 1
                assert cursor == len(text) and split_report["coverage"] == 1
                summary["documents"].append({"filename": path.name, "document_id": doc["id"],
                    "input_blocks": len(spans), "chunks": len(chunks), "cross_page_chunks": cross_page,
                    "input_characters": len(text), "model_calls": split_report["calls"],
                    "cache_hits": split_report["cache_hits"], "coverage": 1.0})
                summary["knowledge"] += len(chunks)
                summary["cross_page_chunks"] += cross_page
                summary["model_calls"] += split_report["calls"]
                summary["cache_hits"] += split_report["cache_hits"]
                summary["kinds"].update(n["kind"] for n in chunks)
        assert seen == {entry["document_id"] for entry in run["documents"]}
        output = output_path
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in summary.items() if k != "documents"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("artifacts/semantic-reimport-summary.json"))
    args = parser.parse_args()
    main(args.progress, args.output)
