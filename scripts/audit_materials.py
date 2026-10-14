"""Read source files without modifying them; flag layout/formula limitations."""
import argparse
import json
import unicodedata
from pathlib import Path

from docx import Document
from pypdf import PdfReader

parser = argparse.ArgumentParser()
parser.add_argument("directory", type=Path)
parser.add_argument("--output", type=Path, default=Path("artifacts/material-audit.json"))
args = parser.parse_args()
result = []
for path in sorted(args.directory.iterdir()):
    entry = {"file": path.name, "bytes": path.stat().st_size}
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(path)
        pages = [p.extract_text() or "" for p in reader.pages]
        entry.update(pages=len(pages), text_characters=sum(map(len,pages)),
                     private_use_characters=sum(unicodedata.category(c)=="Co" for text in pages for c in text),
                     low_text_pages=[i+1 for i,t in enumerate(pages) if len(t.strip()) < 80],
                     sample_30_pages_checked=min(30,len(pages)),
                     formula_review_required=True)
    elif path.suffix.lower() == ".docx":
        doc = Document(path)
        entry.update(paragraphs=len(doc.paragraphs), tables=len(doc.tables),
                     inline_shapes=len(doc.inline_shapes), math_objects=len(doc.element.xpath('//m:oMath')),
                     text_characters=sum(len(p.text) for p in doc.paragraphs), formula_review_required=True)
    result.append(entry)
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps({"documents":result,"note":"解析质量检查，不代表公式识别或人工审核已完成"},ensure_ascii=False,indent=2))
print(f"已检查 {len(result)} 份资料；报告：{args.output}")
