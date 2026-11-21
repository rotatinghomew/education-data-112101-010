"""执行反馈录入与下一轮再评估的测试。"""
import csv
import shutil
import tempfile
import unittest
from pathlib import Path

from review_alloc.allocator import analyze
from review_alloc.feedback import record_relation, record_review
from review_alloc.loader import load_dataset
from review_alloc.model import (
    PLAN_ASSIGNED,
    PLAN_LOCKED,
    PLAN_SUPPRESSED,
    UserError,
)

ROOT = Path(__file__).resolve().parents[1]


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


class FeedbackTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="review-alloc-"))
        self.data = self.tmp / "data"
        shutil.copytree(ROOT / "data", self.data)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def analyze(self):
        dataset = load_dataset(self.data)
        capacity = {"T-1": {"slots": 1, "specialty": "sales"},
                    "T-2": {"slots": 2, "specialty": "credentials"},
                    "T-3": {"slots": 3, "specialty": "refund"},
                    "T-4": {"slots": 1, "specialty": "general"}}
        return analyze(dataset, "2026-W13", capacity, "2026-W12")

    def plans(self, result):
        return {item.signal_ref: item for item in result.items}


class RecordReviewTests(FeedbackTestBase):
    def test_full_lifecycle_changes_next_round(self):
        # S-4 初始为候选；启动核查后锁定；确认后了结不再占位。
        self.assertEqual(self.plans(self.analyze())["S-4"].plan, PLAN_ASSIGNED)

        record_review(self.data, "R-40", signal_ref="S-4", state="in_progress",
                      assignee="T-3", started="2026-04-01", reason="影响面核查",
                      today="2026-04-01")
        item = self.plans(self.analyze())["S-4"]
        self.assertEqual(item.plan, PLAN_LOCKED)
        self.assertEqual(item.team_ref, "T-3")
        self.assertTrue(any("影响面核查" in reason for reason in item.reasons))

        record_review(self.data, "R-40", state="closed", outcome="confirmed",
                      closed="2026-04-03", reason="确认违规", today="2026-04-03")
        item = self.plans(self.analyze())["S-4"]
        self.assertEqual(item.plan, PLAN_SUPPRESSED)
        self.assertEqual(item.outcome, "confirmed")

    def test_waiting_evidence_remains_discoverable(self):
        record_review(self.data, "R-41", signal_ref="S-4",
                      state="waiting_evidence", reason="待补交易凭证",
                      today="2026-04-01")
        item = self.plans(self.analyze())["S-4"]
        self.assertEqual(item.plan, PLAN_ASSIGNED)
        self.assertTrue(any("证据待补" in reason for reason in item.reasons))

    def test_close_requires_outcome(self):
        with self.assertRaises(UserError):
            record_review(self.data, "R-42", signal_ref="S-4", state="closed")

    def test_in_progress_requires_assignee(self):
        with self.assertRaises(UserError):
            record_review(self.data, "R-43", signal_ref="S-4", state="in_progress")

    def test_started_review_keeps_assignee_unless_forced(self):
        with self.assertRaises(UserError):
            record_review(self.data, "R-1", assignee="T-9")
        message = record_review(self.data, "R-1", assignee="T-9", force=True)
        self.assertIn("更新", message)
        rows = read_csv(self.data / "reviews.csv")
        self.assertEqual(next(r for r in rows if r["review_ref"] == "R-1")
                         ["assigned_to"], "T-9")

    def test_cannot_open_second_review_on_locked_signal(self):
        with self.assertRaises(UserError):
            record_review(self.data, "R-99", signal_ref="S-1",
                          state="waiting_evidence")

    def test_unknown_signal_rejected(self):
        with self.assertRaises(UserError):
            record_review(self.data, "R-50", signal_ref="S-999",
                          state="waiting_evidence")


class RecordRelationTests(FeedbackTestBase):
    def test_relation_correction_updates_existing_pair(self):
        message = record_relation(self.data, "M-2", "M-1", "shared_payment",
                                  "rejected", reviewed="2026-04-02")
        self.assertIn("更新", message)
        rows = read_csv(self.data / "relationships.csv")
        updated = [row for row in rows
                   if {row["left_ref"], row["right_ref"]} == {"M-1", "M-2"}
                   and row["relation"] == "shared_payment"]
        self.assertTrue(updated)
        self.assertTrue(all(row["state"] == "rejected" for row in updated))

    def test_new_relation_appended(self):
        record_relation(self.data, "M-1", "M-5", "shared_device", "confirmed",
                        reviewed="2026-04-02")
        rows = read_csv(self.data / "relationships.csv")
        self.assertTrue(any(row["relation"] == "shared_device" for row in rows))

    def test_rejected_relation_regroups_chains(self):
        # 把环上所有 shared_payment 关系否决后，每个主体应自成一链。
        rows = read_csv(self.data / "relationships.csv")
        pairs = {(row["left_ref"], row["right_ref"]) for row in rows
                 if row["relation"] == "shared_payment"}
        for left, right in pairs:
            record_relation(self.data, left, right, "shared_payment", "rejected")
        dataset = load_dataset(self.data)
        from review_alloc.analysis import build_chains
        chains, _ = build_chains(dataset)
        self.assertEqual(len(set(chains.values())), len(chains))


if __name__ == "__main__":
    unittest.main()
