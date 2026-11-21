"""容量分配规则的单元测试。"""
import unittest

from review_alloc.allocator import analyze
from review_alloc.model import (
    AllocationConfig,
    Dataset,
    PLAN_ASSIGNED,
    PLAN_DEFERRED,
    PLAN_LOCKED,
    PLAN_SUPPRESSED,
    Relation,
    Review,
    Signal,
    Weights,
)


def signal(ref, merchant, category="sales_pressure", confidence=0.8,
           scope="wide"):
    return Signal(signal_ref=ref, merchant_ref=merchant, session_ref=f"L-{ref}",
                  category=category, confidence=confidence, scope_band=scope,
                  created_at="2026-03-01", source_ref=f"SRC-{ref}")


def run(dataset, capacity, config=None, weights=None):
    return analyze(dataset, "2026-W13", capacity, "2026-W13",
                   weights=weights or Weights(),
                   config=config or AllocationConfig())


def plans(result):
    return {item.signal_ref: item for item in result.items}


class MerchantCapTests(unittest.TestCase):
    def test_same_merchant_sessions_do_not_fill_queue(self):
        dataset = Dataset(signals=[
            signal("S-1", "M-1", confidence=0.9),
            signal("S-2", "M-1", confidence=0.8),
            signal("S-3", "M-1", confidence=0.7),
            signal("S-4", "M-2", confidence=0.6),
        ])
        result = run(dataset, {"T-1": {"slots": 5, "specialty": "sales"}})
        items = plans(result)
        self.assertEqual(items["S-1"].plan, PLAN_ASSIGNED)
        self.assertEqual(items["S-4"].plan, PLAN_ASSIGNED)
        for ref in ("S-2", "S-3"):
            self.assertEqual(items[ref].plan, PLAN_DEFERRED)
            self.assertIn("同主体", items[ref].reasons[0])

    def test_chain_cap_protects_against_single_chain_domination(self):
        dataset = Dataset(
            signals=[signal("S-1", "M-1", confidence=0.9),
                     signal("S-2", "M-2", confidence=0.8),
                     signal("S-3", "M-3", confidence=0.7)],
            relations=[Relation("M-1", "M-2", "shared_payment", "unreviewed", ""),
                       Relation("M-2", "M-3", "shared_payment", "unreviewed", "")],
        )
        config = AllocationConfig(max_per_chain=2)
        result = run(dataset, {"T-1": {"slots": 5, "specialty": "sales"}}, config)
        items = plans(result)
        assigned = [ref for ref, item in items.items() if item.plan == PLAN_ASSIGNED]
        self.assertEqual(len(assigned), 2)
        deferred = [item for item in items.values() if item.plan == PLAN_DEFERRED]
        self.assertEqual(len(deferred), 1)
        self.assertIn("经营链", deferred[0].reasons[0])


class CategoryReserveTests(unittest.TestCase):
    def test_low_score_category_still_represented(self):
        dataset = Dataset(signals=[
            signal("S-1", "M-1", "sales_pressure", 0.9),
            signal("S-2", "M-2", "sales_pressure", 0.85),
            signal("S-3", "M-3", "credential_question", 0.4, scope="unknown"),
        ])
        result = run(dataset, {"T-1": {"slots": 2, "specialty": "sales"}})
        items = plans(result)
        self.assertEqual(items["S-3"].plan, PLAN_ASSIGNED)
        self.assertTrue(any("覆盖保留" in reason for reason in items["S-3"].reasons))
        self.assertEqual(items["S-2"].plan, PLAN_DEFERRED)

    def test_reserve_can_be_disabled(self):
        dataset = Dataset(signals=[
            signal("S-1", "M-1", "sales_pressure", 0.9),
            signal("S-2", "M-2", "sales_pressure", 0.85),
            signal("S-3", "M-3", "credential_question", 0.4, scope="unknown"),
        ])
        config = AllocationConfig(reserve_per_category=False)
        result = run(dataset, {"T-1": {"slots": 2, "specialty": "sales"}}, config)
        self.assertEqual(plans(result)["S-3"].plan, PLAN_DEFERRED)


class CapacityTests(unittest.TestCase):
    def test_zero_capacity_defers_everything_with_reason(self):
        dataset = Dataset(signals=[signal("S-1", "M-1"), signal("S-2", "M-2")])
        result = run(dataset, {"T-1": {"slots": 0, "specialty": "sales"}})
        for item in result.items:
            self.assertEqual(item.plan, PLAN_DEFERRED)
            self.assertIn("名额不足", item.reasons[0])
        self.assertEqual(result.metrics.assigned, 0)
        self.assertEqual(result.metrics.deferred, 2)

    def test_specialty_match_preferred(self):
        dataset = Dataset(signals=[signal("S-1", "M-1", "credential_question", 0.9)])
        result = run(dataset, {"T-1": {"slots": 1, "specialty": "sales"},
                               "T-2": {"slots": 1, "specialty": "credentials"}})
        self.assertEqual(plans(result)["S-1"].team_ref, "T-2")

    def test_general_team_fallback_noted(self):
        dataset = Dataset(signals=[signal("S-1", "M-1", "credential_question", 0.9)])
        result = run(dataset, {"T-4": {"slots": 1, "specialty": "general"}})
        item = plans(result)["S-1"]
        self.assertEqual(item.plan, PLAN_ASSIGNED)
        self.assertTrue(any("通用团队" in reason for reason in item.reasons))


class LockAndSuppressTests(unittest.TestCase):
    def test_in_progress_keeps_owner_and_reason(self):
        dataset = Dataset(
            signals=[signal("S-1", "M-1")],
            reviews=[Review("R-1", "S-1", "in_progress", "T-9", "2026-03-05",
                            "", "", "scope check")],
        )
        result = run(dataset, {"T-1": {"slots": 2, "specialty": "sales"}})
        item = plans(result)["S-1"]
        self.assertEqual(item.plan, PLAN_LOCKED)
        self.assertEqual(item.team_ref, "T-9")
        self.assertTrue(any("scope check" in reason for reason in item.reasons))
        self.assertIsNone(item.score)

    def test_confirmed_signal_is_suppressed_not_requeued(self):
        dataset = Dataset(
            signals=[signal("S-1", "M-1")],
            reviews=[Review("R-1", "S-1", "closed", "T-1", "2026-03-05",
                            "2026-03-07", "confirmed", "confirmed claim")],
        )
        result = run(dataset, {"T-1": {"slots": 2, "specialty": "sales"}})
        item = plans(result)["S-1"]
        self.assertEqual(item.plan, PLAN_SUPPRESSED)
        self.assertEqual(result.metrics.eligible, 0)
        self.assertTrue(any("了结" in reason for reason in item.reasons))

    def test_waiting_evidence_stays_discoverable(self):
        dataset = Dataset(
            signals=[signal("S-1", "M-1", confidence=0.9)],
            reviews=[Review("R-1", "S-1", "waiting_evidence", "", "",
                            "", "", "source_check")],
        )
        result = run(dataset, {"T-1": {"slots": 1, "specialty": "sales"}})
        item = plans(result)["S-1"]
        self.assertEqual(item.plan, PLAN_ASSIGNED)
        self.assertTrue(any("证据待补" in reason for reason in item.reasons))


class AccountingTests(unittest.TestCase):
    def test_every_signal_accounted_exactly_once(self):
        dataset = Dataset(
            signals=[signal(f"S-{i}", f"M-{i % 3}", confidence=0.1 * i)
                     for i in range(1, 10)],
            reviews=[Review("R-1", "S-1", "in_progress", "T-1", "2026-03-05",
                            "", "", "scope check"),
                     Review("R-2", "S-2", "closed", "T-1", "2026-03-05",
                            "2026-03-07", "confirmed", "done")],
        )
        result = run(dataset, {"T-1": {"slots": 3, "specialty": "sales"}})
        refs = [item.signal_ref for item in result.items]
        self.assertEqual(sorted(refs), [f"S-{i}" for i in range(1, 10)])
        metrics = result.metrics
        self.assertEqual(metrics.assigned + metrics.deferred, metrics.eligible)
        self.assertEqual(metrics.assigned + metrics.deferred
                         + metrics.locked + metrics.suppressed, 9)

    def test_omission_flags_for_wide_scope_and_confirmed_chain(self):
        dataset = Dataset(
            signals=[signal("S-1", "M-1", confidence=0.9),
                     signal("S-2", "M-2", confidence=0.8),
                     signal("S-3", "M-3", confidence=0.7)],
            relations=[Relation("M-2", "M-3", "shared_payment", "unreviewed", "")],
            reviews=[Review("R-1", "S-1", "closed", "T-1", "2026-03-05",
                            "2026-03-07", "confirmed", "done")],
        )
        # M-2 与 M-3 同链且链内无确认；让 S-1 的确认落在另一条链上。
        result = run(dataset, {"T-1": {"slots": 0, "specialty": "sales"}})
        flags = result.metrics.omission_flags
        self.assertTrue(any("wide" in flag and "S-2" in flag for flag in flags))
        self.assertTrue(any("wide" in flag and "S-3" in flag for flag in flags))


if __name__ == "__main__":
    unittest.main()
