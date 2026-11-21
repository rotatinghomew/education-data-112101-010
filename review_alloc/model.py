"""数据记录、配置与分析结果的结构定义。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


class UserError(Exception):
    """可由使用者修正的输入或状态错误（CLI 以退出码 1 报告）。"""


# ---------------------------------------------------------------- 输入记录

@dataclass
class Signal:
    signal_ref: str
    merchant_ref: str
    session_ref: str
    category: str
    confidence: Optional[float]      # 空字段按“未提供”处理，不为零
    scope_band: str
    created_at: str
    source_ref: str


@dataclass
class Relation:
    left_ref: str
    right_ref: str
    relation: str
    state: str                       # unreviewed / confirmed / rejected
    reviewed_at: str


@dataclass
class Review:
    review_ref: str
    signal_ref: str
    state: str                       # waiting_evidence / in_progress / closed
    assigned_to: str
    started_at: str
    closed_at: str
    outcome: str                     # 空 / confirmed / excluded
    reason: str


@dataclass
class Decision:
    allocation_ref: str
    period: str
    signal_ref: str
    team_ref: str
    decision: str                    # assigned / deferred / excluded
    decided_at: str
    reason: str


@dataclass
class CapacitySlot:
    team_ref: str
    period: str
    slots: int
    specialty: str


@dataclass
class Dataset:
    signals: list[Signal] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    capacity: list[CapacitySlot] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 核查状态

class Disposition:
    """线索在当前快照下所处的核查阶段。"""

    NEW = "new"                          # 无任何核查记录
    WAITING = "waiting_evidence"         # 待补证，仍可入队
    IN_PROGRESS = "in_progress"          # 已启动：锁定，保留负责人与理由
    RESOLVED_CONFIRMED = "resolved_confirmed"   # 已确认：了结，不再占位
    RESOLVED_EXCLUDED = "resolved_excluded"     # 已排除：了结，不再占位

ELIGIBLE = {Disposition.NEW, Disposition.WAITING}
RESOLVED = {Disposition.RESOLVED_CONFIRMED, Disposition.RESOLVED_EXCLUDED}


# ---------------------------------------------------------------- 配置

# 影响面分档的默认权重；未知或缺失按低档处理并在说明中标注。
SCOPE_WEIGHTS = {"wide": 1.0, "medium": 0.6, "unknown": 0.3}


@dataclass
class Weights:
    """评分权重（总分 0–100，另减去证据待补罚分）。"""

    confidence: float = 0.40
    scope: float = 0.25
    completeness: float = 0.15
    chain_risk: float = 0.15
    novelty: float = 0.05
    evidence_penalty: float = 10.0       # 待补证线索的减分
    missing_confidence: float = 0.5      # 可信度缺失时的中性取值


@dataclass
class AllocationConfig:
    """分配约束。"""

    max_per_merchant: int = 1      # 同一主体每场最多入队条数，防重复场次占位
    max_per_chain: int = 4         # 同一经营链每场最多入队条数
    reserve_per_category: bool = True   # 为每个活跃类别保留其最高分候选
    specialty_map: dict[str, str] = field(default_factory=lambda: {
        "sales_pressure": "sales",
        "renewal_claim": "sales",
        "credential_question": "credentials",
    })
    general_specialty: str = "general"  # 可承接任意类别的专长名


# ---------------------------------------------------------------- 分析结果

@dataclass
class ScoreBreakdown:
    """单条线索的评分明细（0–1 分量与最终 0–100 总分）。"""

    confidence: float
    scope: float
    completeness: float
    chain_risk: float
    novelty: float
    evidence_penalty: float
    total: float
    notes: list[str] = field(default_factory=list)


@dataclass
class SignalContext:
    """一条线索及其在分析时使用的全部上下文。"""

    signal: Signal
    disposition: str
    chain_ref: str
    latest_review: Optional[Review]
    prior_decisions: list[Decision] = field(default_factory=list)
    merchant_seen: bool = False
    breakdown: Optional[ScoreBreakdown] = None


# 计划去向
PLAN_ASSIGNED = "assigned"     # 建议安排
PLAN_DEFERRED = "deferred"     # 本轮未入队，下轮自动再评
PLAN_LOCKED = "locked"         # 已启动，保留负责人与理由
PLAN_SUPPRESSED = "suppressed" # 已了结，不再占位


@dataclass
class PlanItem:
    signal_ref: str
    merchant_ref: str
    chain_ref: str
    category: str
    plan: str
    team_ref: Optional[str] = None
    score: Optional[float] = None
    breakdown: Optional[ScoreBreakdown] = None
    reasons: list[str] = field(default_factory=list)
    review_ref: Optional[str] = None
    outcome: Optional[str] = None


@dataclass
class CoverageMetrics:
    """覆盖与遗漏指标，用于容量情景比较。"""

    period: str
    total_signals: int = 0
    eligible: int = 0
    assigned: int = 0
    deferred: int = 0
    locked: int = 0
    suppressed: int = 0
    categories_active: list[str] = field(default_factory=list)
    categories_covered: list[str] = field(default_factory=list)
    chains_active: int = 0
    chains_covered: int = 0
    wide_eligible: int = 0
    wide_assigned: int = 0
    omission_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AllocationResult:
    period: str
    capacity_period: str
    items: list[PlanItem]
    capacity: dict[str, dict[str, Any]]   # team -> {slots, used, specialty}
    metrics: CoverageMetrics
    warnings: list[str] = field(default_factory=list)
    config_snapshot: dict[str, Any] = field(default_factory=dict)

    def items_by_plan(self, plan: str) -> list[PlanItem]:
        return [item for item in self.items if item.plan == plan]
