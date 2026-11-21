"""容量约束下的建议队列生成与覆盖指标。

分配原则：
- 已启动的核查锁定，保留负责人与理由，不重新评分、不重新指派；
- 已有结论（确认 / 排除）的线索不再占位，但保留在汇总与报告中；
- 其余线索按综合评分排序，受主体上限与经营链上限约束，避免同一
  经营链的重复场次占满队列；
- 每个活跃类别为最高分候选保留位置，避免低频但影响面广的新问题
  长期得不到核查；
- 未入队的候选记录原因，下一轮自动重新评估，不会在汇总中消失。
"""
from __future__ import annotations

from collections import Counter

from .analysis import build_contexts, score_signal
from .model import (
    AllocationConfig,
    AllocationResult,
    CoverageMetrics,
    Dataset,
    Disposition,
    ELIGIBLE,
    PLAN_ASSIGNED,
    PLAN_DEFERRED,
    PLAN_LOCKED,
    PLAN_SUPPRESSED,
    PlanItem,
    SignalContext,
    UserError,
    Weights,
)


def _team_preference(category: str, specialty: str, config: AllocationConfig) -> int:
    want = config.specialty_map.get(category)
    if specialty == want:
        return 0
    if specialty == config.general_specialty:
        return 1
    return 2


def _pick_team(category: str, remaining: dict[str, int],
               specialties: dict[str, str],
               config: AllocationConfig) -> tuple[str | None, int]:
    best_team: str | None = None
    best_key: tuple[int, str] | None = None
    for team in sorted(remaining):
        if remaining[team] <= 0:
            continue
        key = (_team_preference(category, specialties[team], config), team)
        if best_key is None or key < best_key:
            best_team, best_key = team, key
    if best_team is None:
        return None, -1
    return best_team, best_key[0]


def allocate(contexts: list[SignalContext], stats: dict,
             capacity: dict[str, dict], period: str, capacity_period: str,
             weights: Weights, config: AllocationConfig,
             warnings: list[str]) -> AllocationResult:
    """生成一个周期的建议队列。``capacity`` 为 team -> {slots, specialty}。"""
    remaining = {team: int(info["slots"]) for team, info in capacity.items()}
    specialties = {team: info["specialty"] for team, info in capacity.items()}

    items: list[PlanItem] = []
    eligible: list[SignalContext] = []
    for context in contexts:
        signal = context.signal
        if context.disposition == Disposition.IN_PROGRESS:
            review = context.latest_review
            reasons = ["核查已启动，保留负责人与理由"]
            if review and review.reason:
                reasons.append(f"启动理由：{review.reason}")
            items.append(PlanItem(
                signal_ref=signal.signal_ref, merchant_ref=signal.merchant_ref,
                chain_ref=context.chain_ref, category=signal.category,
                plan=PLAN_LOCKED,
                team_ref=(review.assigned_to if review else "") or None,
                reasons=reasons,
                review_ref=review.review_ref if review else None,
            ))
        elif context.disposition not in ELIGIBLE:
            review = context.latest_review
            outcome = review.outcome if review else ""
            reasons = [("已确认，了结不再占位" if outcome == "confirmed"
                        else "已排除，了结不再占位")]
            if review and review.reason:
                reasons.append(f"结论理由：{review.reason}")
            items.append(PlanItem(
                signal_ref=signal.signal_ref, merchant_ref=signal.merchant_ref,
                chain_ref=context.chain_ref, category=signal.category,
                plan=PLAN_SUPPRESSED, reasons=reasons,
                review_ref=review.review_ref if review else None,
                outcome=outcome or None,
            ))
        else:
            context.breakdown = score_signal(
                context, stats[context.chain_ref], weights)
            eligible.append(context)

    # 排序：分数降序，同分按线索编号，保证结果可重复。
    eligible.sort(key=lambda ctx: (-ctx.breakdown.total, ctx.signal.signal_ref))

    merchant_count: Counter[str] = Counter()
    chain_count: Counter[str] = Counter()
    chosen: dict[str, PlanItem] = {}
    blocked: dict[str, PlanItem] = {}

    def try_select(context: SignalContext, extra_reason: str | None = None) -> None:
        signal = context.signal
        ref = signal.signal_ref
        if ref in chosen or ref in blocked:
            return
        breakdown = context.breakdown

        def defer(reason: str) -> None:
            blocked[ref] = PlanItem(
                signal_ref=ref, merchant_ref=signal.merchant_ref,
                chain_ref=context.chain_ref, category=signal.category,
                plan=PLAN_DEFERRED, score=breakdown.total,
                breakdown=breakdown, reasons=[reason])

        if merchant_count[signal.merchant_ref] >= config.max_per_merchant:
            defer(f"同主体候选已达每场 {config.max_per_merchant} 条上限，"
                  "保留分数最高者，其余下轮再评")
            return
        if chain_count[context.chain_ref] >= config.max_per_chain:
            defer(f"经营链 {context.chain_ref} 本轮已安排 "
                  f"{config.max_per_chain} 条，防止单一经营链占满队列")
            return
        team, preference = _pick_team(signal.category, remaining,
                                      specialties, config)
        if team is None:
            defer("团队名额不足，下轮自动重新评估")
            return
        remaining[team] -= 1
        merchant_count[signal.merchant_ref] += 1
        chain_count[context.chain_ref] += 1
        reasons = list(breakdown.notes)
        if extra_reason:
            reasons.insert(0, extra_reason)
        if preference == 1:
            reasons.append(f"无专长匹配团队，由通用团队 {team} 承接")
        elif preference == 2:
            reasons.append(f"专长不匹配（{specialties[team]}），按剩余名额分配")
        if context.prior_decisions:
            last = context.prior_decisions[-1]
            reasons.append(f"历史安排：{last.period} {last.decision}"
                           f"（{last.reason}），本轮重新评估")
        chosen[ref] = PlanItem(
            signal_ref=ref, merchant_ref=signal.merchant_ref,
            chain_ref=context.chain_ref, category=signal.category,
            plan=PLAN_ASSIGNED, team_ref=team, score=breakdown.total,
            breakdown=breakdown, reasons=reasons)

    # 第一遍：类别覆盖保留——每个活跃类别的最高分候选优先占位。
    if config.reserve_per_category:
        best_by_category: dict[str, SignalContext] = {}
        for context in eligible:
            best_by_category.setdefault(context.signal.category, context)
        for category in sorted(best_by_category):
            try_select(best_by_category[category],
                       extra_reason=f"类别 {category} 覆盖保留")

    # 第二遍：按分数填充剩余名额。
    for context in eligible:
        try_select(context)

    items.extend(chosen.values())
    items.extend(blocked.values())
    items.sort(key=lambda item: item.signal_ref)

    capacity_view = {
        team: {"slots": int(info["slots"]),
               "used": int(info["slots"]) - remaining[team],
               "specialty": info["specialty"]}
        for team, info in sorted(capacity.items())
    }
    metrics = _coverage_metrics(period, contexts, items, stats)
    return AllocationResult(
        period=period, capacity_period=capacity_period, items=items,
        capacity=capacity_view, metrics=metrics, warnings=warnings,
        config_snapshot={
            "weights": vars(weights),
            "allocation": {
                "max_per_merchant": config.max_per_merchant,
                "max_per_chain": config.max_per_chain,
                "reserve_per_category": config.reserve_per_category,
                "specialty_map": dict(config.specialty_map),
                "general_specialty": config.general_specialty,
            },
        },
    )


def _coverage_metrics(period: str, contexts: list[SignalContext],
                      items: list[PlanItem], stats: dict) -> CoverageMetrics:
    ctx_by_ref = {context.signal.signal_ref: context for context in contexts}

    metrics = CoverageMetrics(period=period, total_signals=len(items))
    active_categories: set[str] = set()
    covered_categories: set[str] = set()
    active_chains: set[str] = set()
    covered_chains: set[str] = set()

    for item in items:
        context = ctx_by_ref[item.signal_ref]
        if item.plan == PLAN_ASSIGNED:
            metrics.assigned += 1
        elif item.plan == PLAN_DEFERRED:
            metrics.deferred += 1
        elif item.plan == PLAN_LOCKED:
            metrics.locked += 1
        elif item.plan == PLAN_SUPPRESSED:
            metrics.suppressed += 1
        if context.disposition in ELIGIBLE:
            metrics.eligible += 1
            active_categories.add(item.category)
            active_chains.add(item.chain_ref)
            if context.signal.scope_band == "wide":
                metrics.wide_eligible += 1
        if item.plan == PLAN_ASSIGNED:
            covered_categories.add(item.category)
            covered_chains.add(item.chain_ref)
            if context.signal.scope_band == "wide":
                metrics.wide_assigned += 1
        elif item.plan == PLAN_LOCKED:
            covered_chains.add(item.chain_ref)

    metrics.categories_active = sorted(active_categories)
    metrics.categories_covered = sorted(covered_categories)
    metrics.chains_active = len(active_chains)
    metrics.chains_covered = len(covered_chains & active_chains)

    flags: list[str] = []
    for item in items:
        if item.plan != PLAN_DEFERRED:
            continue
        context = ctx_by_ref[item.signal_ref]
        reason = item.reasons[0] if item.reasons else ""
        score_text = f"{item.score:.1f}" if item.score is not None else "—"
        if context.signal.scope_band == "wide":
            flags.append(f"影响面 wide 的 {item.signal_ref}（{item.category}，"
                         f"{score_text} 分）未入队：{reason}")
        if stats[item.chain_ref].confirmed:
            flags.append(f"经营链 {item.chain_ref} 已有确认结论，"
                         f"{item.signal_ref} 仍未入队：{reason}")
        if item.breakdown and item.breakdown.novelty:
            flags.append(f"新主体线索 {item.signal_ref} 未入队：{reason}")
    for category in sorted(active_categories - covered_categories):
        count = sum(1 for item in items
                    if item.plan in (PLAN_ASSIGNED, PLAN_DEFERRED)
                    and item.category == category)
        flags.append(f"类别 {category} 有 {count} 条候选但无一入队")
    metrics.omission_flags = flags
    return metrics


def analyze(dataset: Dataset, period: str, capacity: dict[str, dict],
            capacity_period: str, weights: Weights | None = None,
            config: AllocationConfig | None = None) -> AllocationResult:
    """从数据快照到建议队列的完整分析入口。"""
    if not dataset.signals:
        raise UserError("没有任何线索记录，无法分析")
    weights = weights or Weights()
    config = config or AllocationConfig()
    contexts, stats, context_warnings = build_contexts(dataset)
    warnings = list(dataset.warnings) + context_warnings
    return allocate(contexts, stats, capacity, period, capacity_period,
                    weights, config, warnings)
