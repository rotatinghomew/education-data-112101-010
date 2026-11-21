"""命令行端到端测试：分析、记录、情景对比在真实合成数据上跑通。"""
import csv
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from review_alloc.cli import main

ROOT = Path(__file__).resolve().parents[1]


class CliTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="review-alloc-cli-"))
        self.data = self.tmp / "data"
        shutil.copytree(ROOT / "data", self.data)
        self.out = self.tmp / "out"
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_cli(self, *argv):
        return main(list(argv))


class AnalyzeCommandTests(CliTestBase):
    def test_analyze_writes_markdown_and_json(self):
        code = self.run_cli("analyze", "--data", str(self.data),
                            "--period", "2026-W13",
                            "--capacity-from", "2026-W12",
                            "--out", str(self.out))
        self.assertEqual(code, 0)
        markdown = (self.out / "queue-2026-W13.md").read_text(encoding="utf-8")
        payload = json.loads((self.out / "queue-2026-W13.json")
                             .read_text(encoding="utf-8"))
        for section in ("建议安排", "延续中的核查", "未入队候选",
                        "已了结", "覆盖与遗漏"):
            self.assertIn(section, markdown)
        refs = [item["signal_ref"] for item in payload["items"]]
        self.assertEqual(sorted(refs, key=lambda r: int(r.split("-")[1])),
                         [f"S-{i}" for i in range(1, 36)])
        # 每条线索恰好出现在一个去向中，没有在汇总里消失。
        self.assertEqual(len(refs), len(set(refs)))

    def test_record_is_idempotent(self):
        for _ in range(2):
            code = self.run_cli("analyze", "--data", str(self.data),
                                "--period", "2026-W13",
                                "--capacity-from", "2026-W12",
                                "--out", str(self.out), "--record")
            self.assertEqual(code, 0)
        with (self.data / "decisions.csv").open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        generated = [row for row in rows
                     if row["allocation_ref"].startswith("A-2026-W13-")]
        refs = [row["allocation_ref"] for row in generated]
        self.assertEqual(len(refs), len(set(refs)))
        self.assertEqual(len(generated), 2)  # S-3、S-4 两条候选

    def test_record_then_feedback_then_reanalyze(self):
        self.run_cli("analyze", "--data", str(self.data), "--period", "2026-W13",
                     "--capacity-from", "2026-W12", "--out", str(self.out),
                     "--record")
        code = self.run_cli("record-review", "--data", str(self.data),
                            "--review", "R-40", "--signal", "S-4",
                            "--state", "in_progress", "--assignee", "T-3",
                            "--reason", "影响面核查")
        self.assertEqual(code, 0)
        code = self.run_cli("analyze", "--data", str(self.data),
                            "--period", "2026-W14",
                            "--capacity-from", "2026-W12",
                            "--out", str(self.out))
        self.assertEqual(code, 0)
        payload = json.loads((self.out / "queue-2026-W14.json")
                             .read_text(encoding="utf-8"))
        item = next(i for i in payload["items"] if i["signal_ref"] == "S-4")
        self.assertEqual(item["plan"], "locked")
        self.assertEqual(item["team_ref"], "T-3")

    def test_unknown_capacity_period_fails_with_message(self):
        code = self.run_cli("analyze", "--data", str(self.data),
                            "--period", "2026-W13",
                            "--capacity-from", "2026-W99")
        self.assertEqual(code, 1)


class ScenarioCommandTests(CliTestBase):
    def test_scenarios_monotonic_coverage(self):
        code = self.run_cli("scenarios", "--data", str(self.data),
                            "--period", "2026-W13",
                            "--capacity-from", "2026-W12",
                            "--scales", "0.1,0.5,1,2",
                            "--out", str(self.out))
        self.assertEqual(code, 0)
        payload = json.loads((self.out / "scenarios-2026-W13.json")
                             .read_text(encoding="utf-8"))
        assigned = [entry["metrics"]["assigned"] for entry in payload["scenarios"]]
        self.assertEqual(assigned, sorted(assigned))
        self.assertEqual(assigned[0], 0)
        self.assertEqual(assigned[-1], 2)
        low = payload["scenarios"][0]["metrics"]
        self.assertGreater(len(low["omission_flags"]), 0)


class RecordRelationCommandTests(CliTestBase):
    def test_relation_correction_via_cli(self):
        code = self.run_cli("record-relation", "--data", str(self.data),
                            "--left", "M-1", "--right", "M-3",
                            "--relation", "same_name", "--state", "rejected")
        self.assertEqual(code, 0)

    def test_invalid_relation_state_rejected(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("record-relation", "--data", str(self.data),
                         "--left", "M-1", "--right", "M-3",
                         "--relation", "same_name", "--state", "maybe")
        self.assertEqual(caught.exception.code, 2)  # argparse 参数错误


if __name__ == "__main__":
    unittest.main()
