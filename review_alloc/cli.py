"""命令行入口：分析、情景对比与执行反馈录入。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from .allocator import analyze
from .feedback import (
    record_allocation_decisions,
    record_relation,
    record_review,
)
from .loader import capacity_for_period, load_dataset
from .model import AllocationConfig, UserError, Weights
from .report import render_markdown, result_to_dict
from .scenarios import run_scenarios, scenario_table, scenarios_to_dict


def _load_config(path: str | None) -> tuple[Weights, AllocationConfig]:
    weights = Weights()
    config = AllocationConfig()
    if not path:
        return weights, config
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UserError(f"配置文件无法读取：{exc}") from exc
    known_weights = set(vars(weights))
    known_config = set(vars(config))
    for key, value in raw.get("weights", {}).items():
        if key not in known_weights:
            raise UserError(f"未知权重项：{key}")
        setattr(weights, key, value)
    for key, value in raw.get("allocation", {}).items():
        if key not in known_config:
            raise UserError(f"未知分配参数：{key}")
        setattr(config, key, value)
    return weights, config


def _prepare(args) -> tuple:
    dataset = load_dataset(args.data)
    weights, config = _load_config(getattr(args, "config", None))
    capacity, capacity_period, notes = capacity_for_period(
        dataset, args.period, getattr(args, "capacity_from", None))
    return dataset, weights, config, capacity, capacity_period, notes


def _cmd_analyze(args) -> int:
    dataset, weights, config, capacity, capacity_period, notes = _prepare(args)
    result = analyze(dataset, args.period, capacity, capacity_period,
                     weights=weights, config=config)
    result.warnings.extend(notes)
    today = date.today().isoformat()

    if args.record:
        written, replaced = record_allocation_decisions(
            args.data, args.period, result.items, today)
        result.warnings.append(
            f"已写入 {written} 条安排记录到 decisions.csv"
            + (f"（替换同周期旧记录 {replaced} 条）" if replaced else ""))

    markdown = render_markdown(result, today)
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        md_path = out_dir / f"queue-{args.period}.md"
        json_path = out_dir / f"queue-{args.period}.json"
        md_path.write_text(markdown, encoding="utf-8")
        json_path.write_text(
            json.dumps(result_to_dict(result, today), ensure_ascii=False, indent=2),
            encoding="utf-8")
        metrics = result.metrics
        print(f"建议队列已生成：{md_path} / {json_path}")
        print(f"入队 {metrics.assigned} / 暂缓 {metrics.deferred} / "
              f"锁定 {metrics.locked} / 了结 {metrics.suppressed}；"
              f"类别覆盖 {len(metrics.categories_covered)}/"
              f"{len(metrics.categories_active)}")
    else:
        print(markdown)
    return 0


def _cmd_scenarios(args) -> int:
    dataset, weights, config, capacity, capacity_period, notes = _prepare(args)
    scales = [float(part) for part in args.scales.split(",") if part.strip()]
    results = run_scenarios(dataset, args.period, capacity, capacity_period,
                            scales, weights=weights, config=config)
    print(f"容量情景对比 — {args.period}（容量模板 {capacity_period}）")
    for note in notes:
        print(f"提示：{note}")
    print()
    print(scenario_table(results))
    print()
    base = dict(results).get(1.0)
    if base and base.metrics.omission_flags:
        print("基准容量（1.0）下的遗漏提示：")
        for flag in base.metrics.omission_flags:
            print(f"  - {flag}")
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"scenarios-{args.period}.json"
        path.write_text(json.dumps(scenarios_to_dict(results),
                                   ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"\n机读结果：{path}")
    return 0


def _cmd_record_review(args) -> int:
    message = record_review(
        args.data, review_ref=args.review, signal_ref=args.signal,
        state=args.state, assignee=args.assignee, started=args.started,
        closed=args.closed, outcome=args.outcome, reason=args.reason,
        force=args.force)
    print(message)
    print("提示：重新运行 analyze 即可让下一轮分配吸收该反馈。")
    return 0


def _cmd_record_relation(args) -> int:
    message = record_relation(
        args.data, left=args.left, right=args.right,
        relation=args.relation, state=args.state, reviewed=args.reviewed)
    print(message)
    print("提示：重新运行 analyze 即可按更正后的主体关系重新归集。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_alloc",
        description="复核资源配置与影响面覆盖分析（建议性质，不替代人工处置）")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument("--data", default="data", help="资料目录（默认 data）")
        target.add_argument("--period", required=True, help="分析周期，如 2026-W13")
        target.add_argument("--capacity-from", default=None,
                            help="容量模板周期（目标周期无容量记录时沿用）")
        target.add_argument("--config", default=None, help="权重与约束的 JSON 配置")

    analyze_parser = sub.add_parser("analyze", help="生成建议队列")
    add_common(analyze_parser)
    analyze_parser.add_argument("--out", default=None,
                                help="报告输出目录（缺省打印到标准输出）")
    analyze_parser.add_argument("--record", action="store_true",
                                help="把建议队列写入 decisions.csv（可重复执行）")
    analyze_parser.set_defaults(func=_cmd_analyze)

    scenarios_parser = sub.add_parser("scenarios", help="不同容量下的覆盖与遗漏对比")
    add_common(scenarios_parser)
    scenarios_parser.add_argument("--scales", default="0.5,1,1.5,2",
                                  help="逗号分隔的容量系数，默认 0.5,1,1.5,2")
    scenarios_parser.add_argument("--out", default=None, help="机读结果输出目录")
    scenarios_parser.set_defaults(func=_cmd_scenarios)

    review_parser = sub.add_parser("record-review", help="录入核查执行反馈")
    review_parser.add_argument("--data", default="data", help="资料目录（默认 data）")
    review_parser.add_argument("--review", required=True, help="核查编号")
    review_parser.add_argument("--signal", default=None, help="线索编号（新增时必填）")
    review_parser.add_argument("--state", default=None,
                               choices=["waiting_evidence", "in_progress", "closed"])
    review_parser.add_argument("--assignee", default=None, help="负责团队")
    review_parser.add_argument("--started", default=None, help="开始日期 ISO")
    review_parser.add_argument("--closed", default=None, help="关闭日期 ISO")
    review_parser.add_argument("--outcome", default=None,
                               choices=["confirmed", "excluded"])
    review_parser.add_argument("--reason", default=None, help="理由")
    review_parser.add_argument("--force", action="store_true",
                               help="允许改派已启动核查的负责人")
    review_parser.set_defaults(func=_cmd_record_review)

    relation_parser = sub.add_parser("record-relation", help="录入主体关系更正")
    relation_parser.add_argument("--data", default="data", help="资料目录（默认 data）")
    relation_parser.add_argument("--left", required=True, help="主体甲")
    relation_parser.add_argument("--right", required=True, help="主体乙")
    relation_parser.add_argument("--relation", required=True,
                                 help="关系类型，如 shared_payment")
    relation_parser.add_argument("--state", required=True,
                                 choices=["unreviewed", "confirmed", "rejected"])
    relation_parser.add_argument("--reviewed", default=None, help="核实日期 ISO")
    relation_parser.set_defaults(func=_cmd_record_relation)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UserError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
