"""经营链归集、核查状态判定与可解释评分。"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .model import (
    Dataset,
    Decision,
    Disposition,
    Review,
    SCOPE_WEIGHTS,
    ScoreBreakdown,
    Signal,
    SignalContext,
    Weights,
)

# 核查结论：有结论的核查不再占用名额，但结论会影响经营链风险。
OUTCOME_CONFIRMED = "confirmed"
OUTCOME_EXCLUDED = "excluded"

# 资料完整性检查的关键字段（空字段表示未提供，不视为零值）。
COMPLETENESS_FIELDS = (
    "merchant_ref", "session_ref", "category", "confidence",
    "scope_band", "created_at", "source_ref",
)


# ---------------------------------------------------------------- 经营链

def build_chains(dataset: Dataset) -> tuple[dict[str, str], list[str]]:
    """按主体关系把主体归并为经营链。

    仅 ``state != rejected`` 的关系参与归并；已否决的关系（例如同名核实
    不成立）不串联主体。同一对主体的重复关系记录只取一次。返回
    ``(merchant -> chain_ref, 数据问题说明列表)``。
    """
    merchants = sorted({signal.merchant_ref for signal in dataset.signals})
    parent = {merchant: merchant for merchant in merchants}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left == root_right:
            return
        # 以字典序较小者为根，保证链标识可重复。
        root = min(root_left, root_right)
        parent[root_left] = root
        parent[root_right] = root

    notes: list[str] = []
    seen: set[tuple[frozenset[str], str]] = set()
    for relation in dataset.relations:
        pair = frozenset({relation.left_ref, relation.right_ref})
        key = (pair, relation.relation)
        if key in seen:
            continue
        seen.add(key)
        if relation.left_ref not in parent or relation.right_ref not in parent:
            continue  # loader 已记录警告
        if relation.state == "rejected":
            notes.append(
                f"关系 {relation.left_ref}-{relation.right_ref}"
                f"（{relation.relation}）已否决，不参与归并")
            continue
        union(relation.left_ref, relation.right_ref)

    chains = {merchant: f"C-{find(merchant)}" for merchant in merchants}
    return chains, notes


# ---------------------------------------------------------------- 核查状态

def _review_sort_key(review: Review) -> tuple[str, int]:
    try:
        number = int(review.review_ref.rsplit("-", 1)[-1])
    except ValueError:
        number = 0
    return (review.closed_at or review.started_at or "", number)


def latest_review(signal_ref: str, reviews_by_signal: dict[str, list[Review]]) -> Review | None:
    rows = reviews_by_signal.get(signal_ref)
    if not rows:
        return None
    return max(rows, key=_review_sort_key)


def disposition_of(latest: Review | None, warnings: list[str]) -> str:
    """由最新一条核查记录推导线索所处阶段。

    结论字段优先：只要核查给出了 confirmed / excluded 结论，即视为了结，
    不再占用名额；状态字段滞后不影响该判定。
    """
    if latest is None:
        return Disposition.NEW
    if latest.outcome == OUTCOME_CONFIRMED:
        return Disposition.RESOLVED_CONFIRMED
    if latest.outcome == OUTCOME_EXCLUDED:
        return Disposition.RESOLVED_EXCLUDED
    if latest.state == "in_progress":
        return Disposition.IN_PROGRESS
    if latest.state == "closed":
        warnings.append(
            f"核查 {latest.review_ref} 已关闭但无结论，按待补证处理")
        return Disposition.WAITING
    return Disposition.WAITING


@dataclass
class ChainStats:
    chain_ref: str
    merchants: set[str] = field(default_factory=set)
    confirmed: int = 0
    excluded: int = 0
    in_progress: int = 0
    waiting: int = 0
    new: int = 0

    @property
    def open(self) -> int:
        return self.in_progress + self.waiting + self.new


def build_contexts(dataset: Dataset) -> tuple[list[SignalContext], dict[str, ChainStats], list[str]]:
    """为每条线索建立分析上下文，并汇总经营链统计。"""
    warnings: list[str] = []
    chains, chain_notes = build_chains(dataset)
    warnings.extend(chain_notes)

    reviews_by_signal: dict[str, list[Review]] = defaultdict(list)
    known = {signal.signal_ref for signal in dataset.signals}
    for review in dataset.reviews:
        if review.signal_ref in known:
            reviews_by_signal[review.signal_ref].append(review)

    decisions_by_signal: dict[str, list[Decision]] = defaultdict(list)
    for decision in dataset.decisions:
        if decision.signal_ref in known:
            decisions_by_signal[decision.signal_ref].append(decision)

    # 主体层面的历史：该主体任何一条线索有过核查或安排，即视为“见过”。
    signal_merchant = {signal.signal_ref: signal.merchant_ref
                       for signal in dataset.signals}
    merchant_history: set[str] = set()
    for record in list(dataset.reviews) + list(dataset.decisions):
        merchant = signal_merchant.get(record.signal_ref)
        if merchant:
            merchant_history.add(merchant)

    contexts: list[SignalContext] = []
    stats: dict[str, ChainStats] = {}
    for signal in dataset.signals:
        chain_ref = chains[signal.merchant_ref]
        latest = latest_review(signal.signal_ref, reviews_by_signal)
        disposition = disposition_of(latest, warnings)
        context = SignalContext(
            signal=signal,
            disposition=disposition,
            chain_ref=chain_ref,
            latest_review=latest,
            prior_decisions=sorted(
                decisions_by_signal.get(signal.signal_ref, []),
                key=lambda item: (item.period, item.allocation_ref)),
            merchant_seen=signal.merchant_ref in merchant_history,
        )
        contexts.append(context)
        chain_stats = stats.setdefault(chain_ref, ChainStats(chain_ref=chain_ref))
        chain_stats.merchants.add(signal.merchant_ref)
        if disposition == Disposition.RESOLVED_CONFIRMED:
            chain_stats.confirmed += 1
        elif disposition == Disposition.RESOLVED_EXCLUDED:
            chain_stats.excluded += 1
        elif disposition == Disposition.IN_PROGRESS:
            chain_stats.in_progress += 1
        elif disposition == Disposition.WAITING:
            chain_stats.waiting += 1
        else:
            chain_stats.new += 1
    return contexts, stats, warnings


# ---------------------------------------------------------------- 评分

def _completeness(signal: Signal, notes: list[str]) -> float:
    missing: list[str] = []
    for name in COMPLETENESS_FIELDS:
        value = getattr(signal, name)
        if value is None or value == "":
            missing.append(name)
    if missing:
        notes.append("资料缺项：" + "、".join(missing))
    return (len(COMPLETENESS_FIELDS) - len(missing)) / len(COMPLETENESS_FIELDS)


def _chain_risk(stats: ChainStats, notes: list[str]) -> float:
    """经营链风险：链内已确认结论越多，链上其余线索越值得优先核查。"""
    if stats.confirmed:
        notes.append(f"经营链内已确认 {stats.confirmed} 条")
        return min(1.0, 0.6 + 0.1 * stats.confirmed)
    if stats.in_progress:
        notes.append(f"经营链内有 {stats.in_progress} 条核查进行中")
        return 0.4
    if stats.waiting:
        notes.append(f"经营链内有 {stats.waiting} 条待补证")
        return 0.2
    return 0.0


def score_signal(context: SignalContext, stats: ChainStats,
                 weights: Weights) -> ScoreBreakdown:
    """计算 0–100 的综合优先级，并保留各分量以便解释。"""
    signal = context.signal
    notes: list[str] = []

    if signal.confidence is None:
        confidence = weights.missing_confidence
        notes.append(f"可信度未提供，按中性 {weights.missing_confidence} 计")
    else:
        confidence = signal.confidence

    scope = SCOPE_WEIGHTS.get(signal.scope_band, SCOPE_WEIGHTS["unknown"])
    if signal.scope_band not in SCOPE_WEIGHTS or signal.scope_band == "unknown":
        notes.append("影响范围未评估，按低档计")

    completeness = _completeness(signal, notes)
    chain_risk = _chain_risk(stats, notes)

    merchant_seen = context.merchant_seen
    novelty = 0.0 if merchant_seen else 1.0
    if novelty:
        notes.append("新主体，首次进入视野")

    penalty = weights.evidence_penalty if context.disposition == Disposition.WAITING else 0.0
    if penalty:
        reason = context.latest_review.reason if context.latest_review else ""
        suffix = f"（{reason}）" if reason else ""
        notes.append(f"证据待补{suffix}，减 {penalty:g} 分")

    total = 100.0 * (
        weights.confidence * confidence
        + weights.scope * scope
        + weights.completeness * completeness
        + weights.chain_risk * chain_risk
        + weights.novelty * novelty
    ) - penalty
    return ScoreBreakdown(
        confidence=confidence, scope=scope, completeness=completeness,
        chain_risk=chain_risk, novelty=novelty, evidence_penalty=penalty,
        total=round(max(0.0, min(100.0, total)), 2), notes=notes,
    )
