"""Prepare annotation candidates or compare baseline/tools/multi-agent on a reviewed set."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

# Support direct execution from a checkout without requiring an editable package install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    subcommands = command.add_subparsers(dest="command", required=True)
    prepare = subcommands.add_parser("prepare", help="从已发布知识生成待审核候选；不调用模型")
    prepare.add_argument("--app-id", required=True)
    prepare.add_argument("--count", type=int, default=40, help="30–50 个候选/槽位，默认 40")
    prepare.add_argument("--output", type=Path, default=Path("artifacts/annotation/agent-candidates.jsonl"))
    run = subcommands.add_parser("run", help="调用应用真实模型，对同一审核题集执行三模式比较")
    run.add_argument("dataset", type=Path)
    run.add_argument("--app-id", required=True)
    run.add_argument("--user-id", required=True, help="现有评测账号 ID；不创建会话或学生消息")
    run.add_argument("--split", choices=["dev", "test"], default="test")
    run.add_argument("--output", type=Path, default=Path("artifacts/evaluation/agents"))
    run.add_argument("--allow-small", action="store_true", help="允许 30–50 题之外的已审核数据集用于联调")
    score = subcommands.add_parser("score", help="导入独立人工评分；不调用模型")
    score.add_argument("results", type=Path, help="baseline.json、tools.json 或 multi_agent.json")
    score.add_argument("judgments", type=Path, help="独立人工评分 JSONL，沿用现有 judgment 格式")
    score.add_argument("--output", type=Path, required=True)
    return command


def write_reports(output, report):
    def write(path, data):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    for mode, data in report["modes"].items():
        write(output / f"{mode}.json", {"metadata": report["metadata"], "complete": report["complete"], **data})
    write(output / "comparison.json", {
        "metadata": report["metadata"], "complete": report["complete"],
        "modes": {mode: data["metrics"] for mode, data in report["modes"].items()},
    })


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "score":
        from app.services.agent_evaluation import score_report
        try:
            if args.output.exists():
                raise ValueError("评分输出已存在，请指定新文件以保留原结果")
            report = json.loads(args.results.read_text(encoding="utf-8"))
            judgments = [json.loads(line) for line in args.judgments.read_text(encoding="utf-8").splitlines() if line.strip()]
            scored = score_report(report, judgments)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(scored, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已评分 {scored['metrics']['human_judged']} / {scored['metrics']['count']} 条：{args.output}")
            return
        except (ValueError, KeyError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
    # --help works without constructing engines or loading model credentials.
    from sqlalchemy.orm import Session

    from app.db import engine
    from app.services.agent_evaluation import MODES, candidate_questions, evaluate_dataset, published_context

    with Session(engine(), autoflush=False) as db:
        try:
            if args.command == "prepare":
                manifest = args.output.with_suffix(".manifest.json")
                if args.output.exists() or manifest.exists():
                    raise ValueError("候选文件已存在，请指定新的输出文件，避免覆盖人工标注")
                metadata, releases = published_context(db, args.app_id)
                rows = candidate_questions(releases, args.count)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                                       encoding="utf-8")
                manifest.write_text(json.dumps({"metadata": metadata, "reviewed": False, "count": len(rows),
                    "purpose": "待人工填写和核验的候选；不是已审核的真实问题数据集"}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                print(f"已生成 {len(rows)} 个待审核候选/槽位：{args.output}；未调用模型，不能直接用于评测。")
                return
            if any((args.output / name).exists() for name in ["comparison.json", *[f"{mode}.json" for mode in MODES]]):
                raise ValueError("输出目录已有评测结果，请选择新目录以保留历史版本")
            rows = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
            args.output.mkdir(parents=True, exist_ok=True)
            report = asyncio.run(evaluate_dataset(db, args.app_id, args.user_id, rows,
                split=args.split, allow_small=args.allow_small,
                progress=lambda current: write_reports(args.output, current)))
            for mode, data in report["modes"].items():
                metrics = data["metrics"]
                print(f"{mode}: {metrics['count']} 题，失败 {metrics['error_count']}；"
                      f"平均耗时 {metrics['mean_latency_seconds']:.3f} 秒")
            print(f"结果已保存：{args.output}；准确率和引用支持率等待人工评分，未知 Token 用量保留空值。")
        except (ValueError, OSError) as exc:
            raise SystemExit(str(exc)) from exc
        finally:
            db.rollback()


if __name__ == "__main__":
    main()
