"""Prepare an honest annotation queue from local source pages, with no invented gold labels."""
import argparse
import json
from pathlib import Path

from pypdf import PdfReader

parser=argparse.ArgumentParser()
parser.add_argument("directory",type=Path)
parser.add_argument("--output",type=Path,default=Path("artifacts/annotation"))
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=True)
pages=[]
for path in sorted(args.directory.glob("*.pdf")):
    reader=PdfReader(path)
    for i,page in enumerate(reader.pages):
        text=page.extract_text() or ""
        if len(text.strip())>=100:
            pages.append({"file":path.name,"page":i+1,"text":text})
sample=[pages[int(i*len(pages)/min(100,len(pages)))] for i in range(min(100,len(pages)))]
snippets=[{"id":f"snippet-{i+1:03}",**p,"gold_entities":[],"gold_relations":[],"formula_notes":""} for i,p in enumerate(sample)]
(args.output/"extraction.jsonl").write_text("\n".join(json.dumps(x,ensure_ascii=False) for x in snippets)+"\n")
questions=[]
for kind,count in [("single",80),("multi",60),("calculation",60),("unanswerable",40)]:
    for i in range(count):
        questions.append({"id":f"{kind}-{i+1:03}","group_id":f"{kind}-{i+1:03}","type":kind,
            "split":"dev" if i<count//4 else "test","question":"", "answerable":kind!="unanswerable",
            "gold_answer":"", "evidence_titles":[]})
(args.output/"questions.jsonl").write_text("\n".join(json.dumps(x,ensure_ascii=False) for x in questions)+"\n")
print(f"已准备 {len(snippets)} 个真实资料片段与 240 个问答标注槽位；标准答案和标签尚未填写，不能直接用于正式评测。")
