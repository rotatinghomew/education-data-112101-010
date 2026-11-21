"""读取本地合成资料，返回 ``Dataset``。

约定：
- 空字段表示“未提供”，不当作零值或否定状态；
- 容量表同一团队同一周期出现多行时按授予批次累加名额；
- 引用完整性问题（如核查指向不存在的线索）记入 ``Dataset.warnings``，
  不中断分析，但会在报告中列出。
"""
from __future__ import annotations

import csv
from pathlib import Path

from .model import (
    CapacitySlot,
    Dataset,
    Decision,
    Relation,
    Review,
    Signal,
    UserError,
)

SIGNAL_FIELDS = ["signal_ref", "merchant_ref", "session_ref", "category",
                 "confidence", "scope_band", "created_at", "source_ref"]
RELATION_FIELDS = ["left_ref", "right_ref", "relation", "state", "reviewed_at"]
REVIEW_FIELDS = ["review_ref", "signal_ref", "state", "assigned_to",
                 "started_at", "closed_at", "outcome", "reason"]
DECISION_FIELDS = ["allocation_ref", "period", "signal_ref", "team_ref",
                   "decision", "decided_at", "reason"]
CAPACITY_FIELDS = ["team_ref", "period", "review_slots", "specialty"]


def _read_rows(path: Path, expected: list[str]) -> list[dict[str, str]]:
    if not path.exists():
        raise UserError(f"缺少资料文件：{path}")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != expected:
            raise UserError(
                f"{path.name} 字段与预期不符：{reader.fieldnames}，预期 {expected}"
            )
        return [{(key or ""): (value or "").strip() for key, value in row.items()}
                for row in reader]


def _parse_confidence(raw: str, ref: str, warnings: list[str]) -> float | None:
    if raw == "":
        return None
    try:
        value = float(raw)
    except ValueError:
        warnings.append(f"{ref} 可信度无法解析：{raw!r}，按未提供处理")
        return None
    if not 0.0 <= value <= 1.0:
        warnings.append(f"{ref} 可信度超出 0–1：{value}，按未提供处理")
        return None
    return value


def load_dataset(data_dir: str | Path) -> Dataset:
    """从目录读取五份资料并做引用检查。"""
    data_dir = Path(data_dir)
    dataset = Dataset()
    warnings = dataset.warnings

    for row in _read_rows(data_dir / "signals.csv", SIGNAL_FIELDS):
        if not row["signal_ref"]:
            raise UserError("signals.csv 存在缺少 signal_ref 的行")
        dataset.signals.append(Signal(
            signal_ref=row["signal_ref"],
            merchant_ref=row["merchant_ref"],
            session_ref=row["session_ref"],
            category=row["category"],
            confidence=_parse_confidence(row["confidence"], row["signal_ref"], warnings),
            scope_band=row["scope_band"],
            created_at=row["created_at"],
            source_ref=row["source_ref"],
        ))

    for row in _read_rows(data_dir / "relationships.csv", RELATION_FIELDS):
        dataset.relations.append(Relation(
            left_ref=row["left_ref"], right_ref=row["right_ref"],
            relation=row["relation"], state=row["state"],
            reviewed_at=row["reviewed_at"],
        ))

    for row in _read_rows(data_dir / "reviews.csv", REVIEW_FIELDS):
        dataset.reviews.append(Review(
            review_ref=row["review_ref"], signal_ref=row["signal_ref"],
            state=row["state"], assigned_to=row["assigned_to"],
            started_at=row["started_at"], closed_at=row["closed_at"],
            outcome=row["outcome"], reason=row["reason"],
        ))

    for row in _read_rows(data_dir / "decisions.csv", DECISION_FIELDS):
        dataset.decisions.append(Decision(
            allocation_ref=row["allocation_ref"], period=row["period"],
            signal_ref=row["signal_ref"], team_ref=row["team_ref"],
            decision=row["decision"], decided_at=row["decided_at"],
            reason=row["reason"],
        ))

    for row in _read_rows(data_dir / "capacity.csv", CAPACITY_FIELDS):
        try:
            slots = int(row["review_slots"])
        except ValueError:
            raise UserError(
                f"capacity.csv 名额无法解析：{row['review_slots']!r}"
            ) from None
        dataset.capacity.append(CapacitySlot(
            team_ref=row["team_ref"], period=row["period"],
            slots=slots, specialty=row["specialty"],
        ))

    _check_references(dataset)
    return dataset


def _check_references(dataset: Dataset) -> None:
    signal_refs = {signal.signal_ref for signal in dataset.signals}
    if len(signal_refs) != len(dataset.signals):
        raise UserError("signals.csv 存在重复的 signal_ref")
    for review in dataset.reviews:
        if review.signal_ref not in signal_refs:
            dataset.warnings.append(
                f"核查 {review.review_ref} 指向未知线索 {review.signal_ref}，已忽略")
    for decision in dataset.decisions:
        if decision.signal_ref not in signal_refs:
            dataset.warnings.append(
                f"决策 {decision.allocation_ref} 指向未知线索 "
                f"{decision.signal_ref}，已忽略")
    merchant_refs = {signal.merchant_ref for signal in dataset.signals}
    for relation in dataset.relations:
        for ref in (relation.left_ref, relation.right_ref):
            if ref not in merchant_refs:
                dataset.warnings.append(
                    f"关系 {relation.left_ref}-{relation.right_ref} 引用未知主体 "
                    f"{ref}，该关系不参与归集")


def capacity_for_period(dataset: Dataset, period: str,
                        template_period: str | None = None) -> tuple[dict[str, dict], str, list[str]]:
    """汇总某周期各团队名额。

    返回 ``(capacity, capacity_period, notes)``：capacity 以团队为键，
    值为 ``{"slots", "specialty"}``。若目标周期无容量记录，则使用
    ``template_period``（缺省为资料中最近的有容量周期）并在 notes 中说明。
    """
    periods = sorted({slot.period for slot in dataset.capacity})
    if not periods:
        raise UserError("capacity.csv 没有任何容量记录")
    notes: list[str] = []
    chosen = period
    if period not in periods:
        chosen = template_period or periods[-1]
        if chosen not in periods:
            raise UserError(
                f"周期 {period} 与模板周期 {chosen} 均无容量记录；"
                f"可用周期：{', '.join(periods)}"
            )
        notes.append(f"周期 {period} 无容量记录，沿用 {chosen} 的名额模板")

    capacity: dict[str, dict] = {}
    for slot in dataset.capacity:
        if slot.period != chosen:
            continue
        entry = capacity.setdefault(slot.team_ref, {"slots": 0, "specialty": slot.specialty})
        entry["slots"] += slot.slots          # 同队同周期多行视为多批授予，累加
        if entry["specialty"] != slot.specialty:
            notes.append(
                f"团队 {slot.team_ref} 在 {chosen} 出现不同专长 "
                f"{entry['specialty']}/{slot.specialty}，取前者")
    if not capacity:
        raise UserError(f"周期 {chosen} 没有任何容量记录")
    return capacity, chosen, notes
