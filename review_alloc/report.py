"""人读（Markdown）与机读（JSON）报告渲染。"""
from __future__ import annotations

from .model import (
    AllocationResult,
    PLAN_ASSIGNED,
    PLAN_DEFERRED,
    PLAN_LOCKED,
    PLAN_SUPPRESSED,
    PlanItem,
)

DISCLAIMER = ("本队列为资源分配建议，不替代人工处置；已启动的核查保留负责人与"
              "理由，未启动的安排在新证据、主体关系更正或结论变化时自动重新评估。")


def _fmt_score(item: PlanItem) -> str:
    return f"{item.score:.1f}" if item.score is not None else "—"


def _breakdown_text(item: PlanItem) -> str:
    breakdown = item.breakdown
    if breakdown is None:
        return ""
    return (f"可信 {breakdown.confidence:.2f} / 影响 {breakdown.scope:.2f} / "
            f"完整 {breakdown.completeness:.2f} / 链风险 {breakdown.chain_risk:.2f} / "
            f"新颖 {breakdown.novelty:.1f} / 减分 {breakdown.evidence_penalty:g}")


def render_markdown(result: AllocationResult, generated_at: str) -> str:
    metrics = result.metrics
    lines: list[str] = []
    lines.append(f"# 复核资源建议队列 — {result.period}")
    lines.append("")
    lines.append(f"- 生成日期：{generated_at}")
    lines.append(f"- 容量周期：{result.capacity_period}"
                 + ("（沿用模板）" if result.capacity_period != result.period else ""))
    lines.append(f"- 说明：{DISCLAIMER}")
    lines.append("")

    lines.append("## 团队容量")
    lines.append("")
    lines.append("| 团队 | 专长 | 名额 | 已用 | 剩余 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for team, info in result.capacity.items():
        lines.append(f"| {team} | {info['specialty']} | {info['slots']} | "
                     f"{info['used']} | {info['slots'] - info['used']} |")
    lines.append("")

    assigned = result.items_by_plan(PLAN_ASSIGNED)
    lines.append(f"## 建议安排（{len(assigned)} 条）")
    lines.append("")
    if assigned:
        for item in sorted(assigned, key=lambda it: (-(it.score or 0), it.signal_ref)):
            lines.append(f"### {item.signal_ref} → {item.team_ref}"
                         f"（{item.score:.1f} 分）")
            lines.append("")
            lines.append(f"- 主体 {item.merchant_ref} / 经营链 {item.chain_ref} / "
                         f"类别 {item.category}")
            lines.append(f"- 分数构成：{_breakdown_text(item)}")
            for reason in item.reasons:
                lines.append(f"- {reason}")
            lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    locked = result.items_by_plan(PLAN_LOCKED)
    lines.append(f"## 延续中的核查（锁定，{len(locked)} 条）")
    lines.append("")
    if locked:
        lines.append("| 线索 | 主体 | 负责人 | 核查 | 保留理由 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in locked:
            reason = "；".join(item.reasons[1:]) or item.reasons[0]
            lines.append(f"| {item.signal_ref} | {item.merchant_ref} | "
                         f"{item.team_ref or '未指派'} | {item.review_ref or '—'} | "
                         f"{reason} |")
        lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    deferred = result.items_by_plan(PLAN_DEFERRED)
    lines.append(f"## 未入队候选（{len(deferred)} 条，下轮自动再评）")
    lines.append("")
    if deferred:
        lines.append("| 线索 | 主体 | 类别 | 分数 | 未入队原因 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in sorted(deferred, key=lambda it: (-(it.score or 0), it.signal_ref)):
            lines.append(f"| {item.signal_ref} | {item.merchant_ref} | "
                         f"{item.category} | {_fmt_score(item)} | "
                         f"{'；'.join(item.reasons)} |")
        lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    suppressed = result.items_by_plan(PLAN_SUPPRESSED)
    lines.append(f"## 已了结，不再占位（{len(suppressed)} 条）")
    lines.append("")
    if suppressed:
        lines.append("| 线索 | 主体 | 结论 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for item in suppressed:
            lines.append(f"| {item.signal_ref} | {item.merchant_ref} | "
                         f"{item.outcome or '—'} | {'；'.join(item.reasons)} |")
        lines.append("")
    else:
        lines.append("（无）")
        lines.append("")

    lines.append("## 覆盖与遗漏")
    lines.append("")
    lines.append(f"- 候选 {metrics.eligible} 条：入队 {metrics.assigned}，"
                 f"暂缓 {metrics.deferred}；另有锁定 {metrics.locked}、"
                 f"了结 {metrics.suppressed}")
    lines.append(f"- 类别覆盖：{len(metrics.categories_covered)}/"
                 f"{len(metrics.categories_active)}"
                 f"（{', '.join(metrics.categories_covered) or '无'}）")
    lines.append(f"- 经营链覆盖：{metrics.chains_covered}/{metrics.chains_active}")
    lines.append(f"- 影响面 wide 覆盖：{metrics.wide_assigned}/{metrics.wide_eligible}")
    if metrics.omission_flags:
        lines.append("- 遗漏提示：")
        for flag in metrics.omission_flags:
            lines.append(f"  - {flag}")
    else:
        lines.append("- 遗漏提示：无")
    lines.append("")

    if result.warnings:
        lines.append("## 数据提示")
        lines.append("")
        for warning in result.warnings:
            lines.append(f"- {warning}")
        lines.append("")
    return "\n".join(lines)


def result_to_dict(result: AllocationResult, generated_at: str) -> dict:
    return {
        "period": result.period,
        "capacity_period": result.capacity_period,
        "generated_at": generated_at,
        "disclaimer": DISCLAIMER,
        "config": result.config_snapshot,
        "capacity": result.capacity,
        "items": [
            {
                "signal_ref": item.signal_ref,
                "merchant_ref": item.merchant_ref,
                "chain_ref": item.chain_ref,
                "category": item.category,
                "plan": item.plan,
                "team_ref": item.team_ref,
                "score": item.score,
                "breakdown": (vars(item.breakdown) if item.breakdown else None),
                "reasons": item.reasons,
                "review_ref": item.review_ref,
                "outcome": item.outcome,
            }
            for item in result.items
        ],
        "metrics": result.metrics.to_dict(),
        "warnings": result.warnings,
    }
