"""Inspect 30 actual pages with the pinned Semantica parser; never infer formula correctness."""
import argparse
import json
from pathlib import Path

from semantica.parse import DocumentParser

parser=argparse.ArgumentParser()
parser.add_argument("pdf",type=Path)
parser.add_argument("--pages",type=int,default=30)
args=parser.parse_args()
parsed=DocumentParser().parse(str(args.pdf),pages=list(range(args.pages)),extract_tables=False,extract_images=False)
report={"file":args.pdf.name,"parser":"Semantica DocumentParser 0.7.0","total_pages":parsed["total_pages"],
        "checked_pages":len(parsed["pages"]),"formula_correctness":"未人工核验，不能声称公式保真",
        "pages":[{"locator":f"PDF 第 {p['page_number']} 页","characters":len(p['text']),
                  "private_use_glyphs":sum('\ue000'<=c<='\uf8ff' for c in p['text']),
                  "text":p['text']} for p in parsed["pages"]]}
output=Path("artifacts/semantica-parser-check.json")
output.parent.mkdir(parents=True,exist_ok=True)
output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(f"已用 Semantica 实际解析 {len(parsed['pages'])} 页，定位与提取结果：{output}")
