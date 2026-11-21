"""经营链归集、核查状态判定与评分的单元测试。"""
import unittest

from review_alloc.analysis import (
    ChainStats,
    build_chains,
    build_contexts,
    disposition_of,
    score_signal,
)
from review_alloc.model import (
    Dataset,
    Decision,
    Disposition,
    Relation,
    Review,
    Signal,
    SignalContext,
    Weights,
)


def make_signal(ref="S-1", merchant="M-1", category="sales_pressure",
                confidence=0.8, scope="wide", created="2026-03-01",
                source="SRC-1", session="L-1"):
    return Signal(signal_ref=ref, merchant_ref=merchant, session_ref=session,
                  category=category, confidence=confidence, scope_band=scope,
                  created_at=created, source_ref=source)


def make_review(ref="R-1", signal="S-1", state="in_progress", assignee="T-1",
                outcome="", reason=""):
    return Review(review_ref=ref, signal_ref=signal, state=state,
                  assigned_to=assignee, started_at="2026-03-02",
                  closed_at="", outcome=outcome, reason=reason)


class ChainTests(unittest.TestCase):
    def test_rejected_relation_does_not_link(self):
        dataset = Dataset(signals=[make_signal("S-1", "M-1"),
                                   make_signal("S-2", "M-2"),
                                   make_signal("S-3", "M-3")],
                          relations=[
                              Relation("M-1", "M-2", "shared_payment", "unreviewed", ""),
                              Relation("M-2", "M-3", "same_name", "rejected", "2026-03-04"),
                          ])
        chains, notes = build_chains(dataset)
        self.assertEqual(chains["M-1"], chains["M-2"])
        self.assertNotEqual(chains["M-2"], chains["M-3"])
        self.assertTrue(any("已否决" in note for note in notes))

    def test_chain_ref_is_deterministic(self):
        dataset = Dataset(signals=[make_signal("S-1", "M-9"),
                                   make_signal("S-2", "M-1")],
                          relations=[Relation("M-9", "M-1", "shared_payment",
                                              "unreviewed", "")])
        chains, _ = build_chains(dataset)
        self.assertEqual(chains["M-9"], "C-M-1")
        self.assertEqual(chains["M-1"], "C-M-1")

    def test_duplicate_relation_rows_count_once(self):
        dataset = Dataset(signals=[make_signal("S-1", "M-1"),
                                   make_signal("S-2", "M-2")],
                          relations=[
                              Relation("M-1", "M-2", "shared_payment", "unreviewed", ""),
                              Relation("M-1", "M-2", "shared_payment", "unreviewed",
                                       "2026-03-20"),
                          ])
        chains, _ = build_chains(dataset)
        self.assertEqual(chains["M-1"], chains["M-2"])


class DispositionTests(unittest.TestCase):
    def test_no_review_is_new(self):
        self.assertEqual(disposition_of(None, []), Disposition.NEW)

    def test_outcome_overrides_state(self):
        review = make_review(state="waiting_evidence", outcome="excluded")
        self.assertEqual(disposition_of(review, []), Disposition.RESOLVED_EXCLUDED)

    def test_confirmed_is_resolved(self):
        review = make_review(state="closed", outcome="confirmed")
        self.assertEqual(disposition_of(review, []), Disposition.RESOLVED_CONFIRMED)

    def test_in_progress_is_locked(self):
        review = make_review(state="in_progress")
        self.assertEqual(disposition_of(review, []), Disposition.IN_PROGRESS)

    def test_waiting_without_outcome_stays_open(self):
        review = make_review(state="waiting_evidence")
        self.assertEqual(disposition_of(review, []), Disposition.WAITING)


class ScoringTests(unittest.TestCase):
    def make_context(self, signal, disposition=Disposition.NEW, review=None,
                     decisions=(), merchant_seen=False):
        return SignalContext(signal=signal, disposition=disposition,
                             chain_ref="C-M-1", latest_review=review,
                             prior_decisions=list(decisions),
                             merchant_seen=merchant_seen)

    def test_full_score_breakdown(self):
        context = self.make_context(make_signal(confidence=0.8))
        stats = ChainStats(chain_ref="C-M-1")
        breakdown = score_signal(context, stats, Weights())
        # 100 * (0.4*0.8 + 0.25*1.0 + 0.15*1.0 + 0.15*0 + 0.05*1.0)
        self.assertAlmostEqual(breakdown.total, 77.0)
        self.assertEqual(breakdown.novelty, 1.0)

    def test_chain_risk_from_confirmed(self):
        context = self.make_context(make_signal(), merchant_seen=True)
        stats = ChainStats(chain_ref="C-M-1", confirmed=2)
        breakdown = score_signal(context, stats, Weights())
        self.assertAlmostEqual(breakdown.chain_risk, 0.8)
        self.assertTrue(any("已确认 2 条" in note for note in breakdown.notes))

    def test_waiting_evidence_penalty(self):
        review = make_review(state="waiting_evidence", reason="source_check")
        context = self.make_context(make_signal(confidence=0.8),
                                    disposition=Disposition.WAITING,
                                    review=review, merchant_seen=True)
        stats = ChainStats(chain_ref="C-M-1")
        breakdown = score_signal(context, stats, Weights())
        self.assertEqual(breakdown.evidence_penalty, 10.0)
        self.assertTrue(any("证据待补" in note for note in breakdown.notes))

    def test_missing_confidence_is_neutral_not_zero(self):
        signal = make_signal(confidence=None)
        context = self.make_context(signal)
        breakdown = score_signal(context, ChainStats(chain_ref="C-M-1"), Weights())
        self.assertEqual(breakdown.confidence, 0.5)
        self.assertTrue(any("未提供" in note for note in breakdown.notes))

    def test_unknown_scope_flagged(self):
        signal = make_signal(scope="unknown")
        context = self.make_context(signal)
        breakdown = score_signal(context, ChainStats(chain_ref="C-M-1"), Weights())
        self.assertEqual(breakdown.scope, 0.3)
        self.assertTrue(any("影响范围未评估" in note for note in breakdown.notes))

    def test_merchant_history_comes_from_any_signal(self):
        # 主体 M-1 的另一条线索有过核查，新线索不再视为新主体。
        dataset = Dataset(
            signals=[make_signal("S-1", "M-1"), make_signal("S-2", "M-1")],
            reviews=[make_review("R-1", "S-1", state="closed", outcome="excluded")],
        )
        contexts, _, _ = build_contexts(dataset)
        by_ref = {context.signal.signal_ref: context for context in contexts}
        self.assertTrue(by_ref["S-2"].merchant_seen)
        self.assertFalse(by_ref["S-1"].merchant_seen is False and
                         by_ref["S-2"].merchant_seen is False)

    def test_prior_decision_marks_merchant_seen(self):
        dataset = Dataset(
            signals=[make_signal("S-1", "M-1")],
            decisions=[Decision("A-1", "2026-W10", "S-1", "T-1",
                                "deferred", "2026-03-04", "capacity limit")],
        )
        contexts, _, _ = build_contexts(dataset)
        self.assertTrue(contexts[0].merchant_seen)


if __name__ == "__main__":
    unittest.main()
