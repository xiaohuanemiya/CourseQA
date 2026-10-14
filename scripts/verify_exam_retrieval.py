"""Read-only acceptance against the imported course; never calls a generation model."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine
from app.models import ModelProfile
from app.schemas import ChatInput
from app.services.learning import generation_prompt, retrieve, scope, versions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    queries = [
        ("告诉我2021-2022一元B上月考考了什么\n", "2021-2022一元B上月考.pdf"),
        ("总结2021—2022学年第一学期一元微积分（B上）月考试卷", "2021-2022一元B上月考.pdf"),
        ("2020-2021一元B上月考考了什么", "2020-2021一元B上月考.pdf"),
        ("2022-2023一元微积分B上月考考了什么", "2022-2023一元微积分B上月考试题.pdf"),
        ("2021-2022一元B上A卷考了什么", "2021-2022一元B上A卷.pdf"),
        ("总结2021-2022一元B上月考答案", "2021-2022一元B上月考答案.pdf"),
        ("2024-2025一元B上月考考了什么", None),
        ("2021-2022线性代数月考考了什么", None),
    ]
    report = {"generation_model_called": False, "database_modified": False, "cases": []}
    with Session(engine()) as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        app, releases = scope(db, args.app_id, "chat")
        embedding = db.get(ModelProfile, app.published["embedding_profile_id"])
        assert embedding.provider == "ollama" and urlparse(embedding.base_url).hostname in {
            "127.0.0.1", "localhost", "::1"}, "Acceptance may only use local embeddings"
        report["versions"] = versions(releases)
        for question, expected_document in queries:
            evidence = retrieve(db, app, releases, question)
            if expected_document is None:
                assert not evidence and evidence.context["status"] == "not_found", question
            else:
                assert evidence and evidence.context["strategy"] == "document_overview", question
                assert {d["filename"] for n in evidence for d in n["documents"]} == {expected_document}, question
                assert evidence.context["coverage"]["complete"], question
                if expected_document == "2021-2022一元B上月考.pdf":
                    body = "\n".join(n["content"] for n in evidence)
                    for expected in ["共30分", "共40分", "共26分", "四、（4分）", "单调", "极限"]:
                        assert expected in body, expected
                _, prompt = generation_prompt(app, ChatInput(message=question), evidence)
                assert all(n["documents"] and n["documents"][0]["pages"] for n in json.loads(prompt)["evidence"])
            report["cases"].append({"question": question.strip(), "retrieval": evidence.context,
                "evidence": [{"title": n["title"], "knowledge_id": n["id"], "release_id": n["release_id"],
                              "characters": len(n["content"]), "documents": n["documents"]} for n in evidence]})
        evidence = retrieve(db, app, releases, "可导与连续有什么关系")
        assert evidence and evidence.context["strategy"] == "hybrid"
        assert any("连续" in n["content"] and "可导" in n["content"] for n in evidence)
        report["concept_search"] = {"passed": True, "titles": [n["title"] for n in evidence]}
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": len(queries)+1, "original_question_units": len(report["cases"][0]["evidence"]),
                      "generation_model_called": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
