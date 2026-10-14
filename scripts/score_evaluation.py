import argparse
import json
from pathlib import Path

from app.evaluation import summarize

parser=argparse.ArgumentParser()
parser.add_argument("results",type=Path)
parser.add_argument("judgments",type=Path)
parser.add_argument("--output",type=Path,required=True)
args=parser.parse_args()
data=json.loads(args.results.read_text())
judgments={r["id"]:r for r in map(json.loads,args.judgments.read_text().splitlines())}
report=summarize(data["results"],judgments)
args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(f"已评分 {report['human_judged']} / {report['count']} 条；未评分项不计入正确率。")
