"""执行反馈录入：核查结果与主体关系更正。

团队按建议队列开展核查后，用本模块把结果写回资料；下一轮分析会自动
吸收这些变化：

- 确认 / 排除的线索不再占位；
- 待补证的线索保留在候选池中；
- 已启动的核查保留负责人，除非显式 ``force`` 否则不允许改派；
- 主体关系被否决后，经营链重新归并，相关线索重新评估。
"""
from __future__ import annotations

import csv
from pathlib import Path

from .loader import (
    DECISION_FIELDS,
    RELATION_FIELDS,
    REVIEW_FIELDS,
    SIGNAL_FIELDS,
)
from .model import UserError

REVIEW_STATES = ("waiting_evidence", "in_progress", "closed")
REVIEW_OUTCOMES = ("confirmed", "excluded")
RELATION_STATES = ("unreviewed", "confirmed", "rejected")


def _load_rows(path: Path, fields: list[str]) -> list[dict[str, str]]:
    if not path.exists():
        raise UserError(f"缺少资料文件：{path}")
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != fields:
            raise UserError(f"{path.name} 字段与预期不符：{reader.fieldnames}")
        return [{(key or ""): (value or "").strip() for key, value in row.items()}
                for row in reader]


def _write_rows(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def record_review(data_dir: str | Path, review_ref: str,
                  signal_ref: str | None = None, state: str | None = None,
                  assignee: str | None = None, started: str | None = None,
                  closed: str | None = None, outcome: str | None = None,
                  reason: str | None = None, force: bool = False,
                  today: str | None = None) -> str:
    """新增或更新一条核查记录，返回操作说明。

    校验规则：
    - 新记录必须提供线索与状态；
    - ``in_progress`` 必须有负责人（缺省开始日期为当天）；
    - ``closed`` 必须给出结论 confirmed / excluded（缺省关闭日期为当天）；
    - 已启动的核查不允许改派负责人，除非 ``force=True``；
    - 同一线索已有进行中的核查时，不允许另开新核查。
    """
    from datetime import date

    data_dir = Path(data_dir)
    today = today or date.today().isoformat()
    path = data_dir / "reviews.csv"
    rows = _load_rows(path, REVIEW_FIELDS)

    if state is not None and state not in REVIEW_STATES:
        raise UserError(f"未知核查状态 {state!r}，可选：{', '.join(REVIEW_STATES)}")
    if outcome is not None and outcome not in REVIEW_OUTCOMES:
        raise UserError(f"未知核查结论 {outcome!r}，可选：{', '.join(REVIEW_OUTCOMES)}")

    signals = {row["signal_ref"]
               for row in _load_rows(data_dir / "signals.csv", SIGNAL_FIELDS)}
    existing = next((row for row in rows if row["review_ref"] == review_ref), None)

    if existing is None:
        if not signal_ref or not state:
            raise UserError("新增核查记录必须提供 --signal 与 --state")
        if signal_ref not in signals:
            raise UserError(f"线索 {signal_ref} 不存在")
        for row in rows:
            if row["signal_ref"] == signal_ref and row["state"] == "in_progress":
                raise UserError(
                    f"线索 {signal_ref} 已有进行中的核查 {row['review_ref']}"
                    f"（负责人 {row['assigned_to']}），不能另开新核查")
        existing = {field: "" for field in REVIEW_FIELDS}
        existing["review_ref"] = review_ref
        existing["signal_ref"] = signal_ref
        rows.append(existing)
        action = "新增"
    else:
        if signal_ref and signal_ref != existing["signal_ref"]:
            raise UserError("不允许修改核查记录对应的线索")
        action = "更新"

    if existing["state"] == "in_progress" and assignee \
            and assignee != existing["assigned_to"] and not force:
        raise UserError(
            f"核查 {review_ref} 已启动，负责人为 {existing['assigned_to']}；"
            "如需改派请使用 --force 并在理由中说明")

    if state is not None:
        existing["state"] = state
    if assignee is not None:
        existing["assigned_to"] = assignee
    if started is not None:
        existing["started_at"] = started
    if closed is not None:
        existing["closed_at"] = closed
    if outcome is not None:
        existing["outcome"] = outcome
    if reason is not None:
        existing["reason"] = reason

    if existing["state"] == "in_progress":
        if not existing["assigned_to"]:
            raise UserError("启动核查必须指定负责人（--assignee）")
        if not existing["started_at"]:
            existing["started_at"] = today
    if existing["state"] == "closed":
        if not existing["outcome"]:
            raise UserError("关闭核查必须给出结论（--outcome confirmed|excluded）")
        if not existing["closed_at"]:
            existing["closed_at"] = today
    if existing["outcome"] and existing["state"] != "closed":
        raise UserError("有结论的核查必须处于 closed 状态")

    _write_rows(path, REVIEW_FIELDS, rows)
    return (f"{action}核查 {review_ref}：线索 {existing['signal_ref']}，"
            f"状态 {existing['state']}"
            + (f"，结论 {existing['outcome']}" if existing["outcome"] else ""))


def record_relation(data_dir: str | Path, left: str, right: str,
                    relation: str, state: str,
                    reviewed: str | None = None,
                    today: str | None = None) -> str:
    """新增或更正一条主体关系（如同名核实不成立），返回操作说明。"""
    from datetime import date

    data_dir = Path(data_dir)
    today = today or date.today().isoformat()
    if state not in RELATION_STATES:
        raise UserError(f"未知关系状态 {state!r}，可选：{', '.join(RELATION_STATES)}")
    if left == right:
        raise UserError("主体关系两端不能是同一主体")

    path = data_dir / "relationships.csv"
    rows = _load_rows(path, RELATION_FIELDS)
    pair = frozenset({left, right})
    matched = [
        row for row in rows
        if frozenset({row["left_ref"], row["right_ref"]}) == pair
        and row["relation"] == relation
    ]
    if not matched:
        rows.append({"left_ref": left, "right_ref": right, "relation": relation,
                     "state": state, "reviewed_at": reviewed or today})
        action = "新增"
    else:
        # 同一主体对的重复记录一并更正，保持资料一致。
        for row in matched:
            row["state"] = state
            row["reviewed_at"] = reviewed or today
        action = f"更新（{len(matched)} 条重复记录）" if len(matched) > 1 else "更新"
    _write_rows(path, RELATION_FIELDS, rows)
    return f"{action}关系 {left}-{right}（{relation}），状态 {state}"


def record_allocation_decisions(data_dir: str | Path, period: str,
                                items: list, today: str) -> tuple[int, int]:
    """把建议队列写入 decisions.csv，返回 (写入条数, 替换条数)。

    仅记录 assigned / deferred 两种去向；锁定与已了结的线索由核查记录
    本身承载，不重复写入。同一周期内由本工具生成的记录
    （编号形如 ``A-{period}-...``）在重复运行时整体替换，保证可重复执行。
    """
    from .model import PLAN_ASSIGNED, PLAN_DEFERRED

    data_dir = Path(data_dir)
    path = data_dir / "decisions.csv"
    rows = _load_rows(path, DECISION_FIELDS)
    prefix = f"A-{period}-"
    kept = [row for row in rows if not row["allocation_ref"].startswith(prefix)]
    replaced = len(rows) - len(kept)

    written = 0
    for item in items:
        if item.plan == PLAN_ASSIGNED:
            decision, team = "assigned", item.team_ref or ""
            reason = f"建议安排：{item.score:.1f} 分"
        elif item.plan == PLAN_DEFERRED:
            decision, team = "deferred", ""
            reason = item.reasons[0] if item.reasons else "未入队"
        else:
            continue
        kept.append({
            "allocation_ref": f"{prefix}{item.signal_ref}",
            "period": period,
            "signal_ref": item.signal_ref,
            "team_ref": team,
            "decision": decision,
            "decided_at": today,
            "reason": reason,
        })
        written += 1
    _write_rows(path, DECISION_FIELDS, kept)
    return written, replaced
