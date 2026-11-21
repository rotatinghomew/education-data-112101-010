"""容量情景对比：不同名额规模下的覆盖与遗漏风险。"""
from __future__ import annotations

from .allocator import analyze
from .model import (
    AllocationConfig,
    AllocationResult,
    Dataset,
    UserError,
    Weights,
)


def scale_capacity(capacity: dict[str, dict], scale: float) -> dict[str, dict]:
    """按比例缩放各团队名额（四舍五入到整数，不为负）。"""
    return {
        team: {"slots": max(0, int(info["slots"] * scale + 0.5)),
               "specialty": info["specialty"]}
        for team, info in capacity.items()
    }


def run_scenarios(dataset: Dataset, period: str, capacity: dict[str, dict],
                  capacity_period: str, scales: list[float],
                  weights: Weights | None = None,
                  config: AllocationConfig | None = None
                  ) -> list[tuple[float, AllocationResult]]:
    """对每个规模系数独立运行一次完整分析，返回 (系数, 结果) 列表。"""
    if not scales or any(scale < 0 for scale in scales):
        raise UserError("规模系数必须为非负数")
    results: list[tuple[float, AllocationResult]] = []
    for scale in scales:
        scaled = scale_capacity(capacity, scale)
        result = analyze(dataset, period, scaled, capacity_period,
                         weights=weights, config=config)
        results.append((scale, result))
    return results


def scenario_table(results: list[tuple[float, AllocationResult]]) -> str:
    """生成情景对比的文本表格。"""
    header = ("规模", "名额", "入队", "暂缓", "类别覆盖", "经营链覆盖",
              "wide 覆盖", "遗漏提示")
    rows = []
    for scale, result in results:
        metrics = result.metrics
        slots = sum(info["slots"] for info in result.capacity.values())
        rows.append((
            f"{scale:g}",
            str(slots),
            str(metrics.assigned),
            str(metrics.deferred),
            f"{len(metrics.categories_covered)}/{len(metrics.categories_active)}",
            f"{metrics.chains_covered}/{metrics.chains_active}",
            f"{metrics.wide_assigned}/{metrics.wide_eligible}",
            str(len(metrics.omission_flags)),
        ))
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def scenarios_to_dict(results: list[tuple[float, AllocationResult]]) -> dict:
    """机读格式：每个情景的容量、覆盖与遗漏明细。"""
    return {
        "scenarios": [
            {
                "scale": scale,
                "slots": {team: info["slots"] for team, info in result.capacity.items()},
                "metrics": result.metrics.to_dict(),
                "deferred": [
                    {"signal_ref": item.signal_ref, "score": item.score,
                     "reasons": item.reasons}
                    for item in result.items if item.plan == "deferred"
                ],
            }
            for scale, result in results
        ]
    }
