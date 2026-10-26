# -*- coding: utf-8 -*-
"""生命周期状态机单测（规格 §2/§3/§4/§8 —— 虚拟时钟确定性，ADR-0004）。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine"))

from core import (Engine, CONFIG, ZONE_LEARN, ZONE_PRELIM, ZONE_STABLE,
                  ZONE_PENDING, ZONE_ARCHIVED, ZONE_SUSPENDED)


def grow(e: Engine, bid: str, count: int, day: float = 0.0, note: str = "t") -> None:
    e.now = day
    for _ in range(count):
        e.evidence(bid, "pos", note)


class TestProjection(unittest.TestCase):
    def test_zone_projection(self):
        e = Engine()
        e.add_material("a", "A", "A")
        b = e.blocks["a"]
        b.conf_stored = 0.30
        self.assertEqual(b.zone_code(), ZONE_LEARN)
        b.conf_stored = 0.60
        self.assertEqual(b.zone_code(), ZONE_PRELIM)
        b.conf_stored = 0.75
        self.assertEqual(b.zone_code(), ZONE_STABLE)
        b.conf_stored = 0.85
        self.assertEqual(b.zone_code(), ZONE_STABLE)
        b.archived = True
        self.assertEqual(b.zone_code(), ZONE_ARCHIVED)
        self.assertFalse(b.searchable())

    def test_freshness_and_grace(self):
        e = Engine()
        e.add_material("a", "A", "A")
        b = e.blocks["a"]
        b.conf_stored = 0.50
        b.last_engagement_at = 0.0
        e.now = 10
        self.assertEqual(b.freshness(10), 1.0)          # 宽限期内
        e.now = 14 + 90                                  # 半衰期 1 个
        self.assertAlmostEqual(b.freshness(e.now), 0.5, places=2)
        # 稳定区半衰期 365
        b.conf_stored = 0.85
        b.last_engagement_at = 0.0
        e.now = 80 + 365
        self.assertAlmostEqual(b.freshness(e.now), 0.5, places=2)

    def test_tenure_extends_grace(self):
        e = Engine()
        e.add_material("a", "A", "A", admin=True)        # 0.85 稳定区
        b = e.blocks["a"]
        self.assertEqual(b.grace_days(), CONFIG["tenure.grace_min"])
        for _ in range(5):
            e.evidence("a", "pos")                        # tenure +5
        self.assertEqual(b.grace_days(), CONFIG["tenure.grace_min"] + 5 * CONFIG["tenure.step"])


class TestSignals(unittest.TestCase):
    def test_machine_cap(self):
        e = Engine()
        e.add_material("a", "A", "A")
        grow(e, "a", 40)
        self.assertLessEqual(e.blocks["a"].conf_stored, CONFIG["conf.machine_cap"])

    def test_adopt_is_activity_only(self):
        e = Engine()
        e.add_material("a", "A", "A")
        before = e.blocks["a"].conf_stored
        e.adopt("a")
        self.assertEqual(e.blocks["a"].conf_stored, before)
        self.assertEqual(e.blocks["a"].adopted_count, 1)
        self.assertEqual(e.blocks["a"].last_engagement_at, e.now)

    def test_negative_evidence_scores(self):
        e = Engine()
        e.add_material("a", "A", "A")
        e.evidence("a", "reask")
        self.assertAlmostEqual(e.blocks["a"].conf_stored,
                               CONFIG["conf.initial"] + CONFIG["score.reask"])
        e.evidence("a", "dislike")
        self.assertAlmostEqual(e.blocks["a"].conf_stored,
                               CONFIG["conf.initial"] + CONFIG["score.reask"]
                               + CONFIG["score.dislike"])


class TestSlowRise(unittest.TestCase):
    def test_probation_enter_and_vet_pass(self):
        e = Engine()
        e.add_material("a", "A", "A")
        grow(e, "a", 16)                                  # 0.30 → 0.62
        e.reconcile(1)
        b = e.blocks["a"]
        self.assertTrue(b.frozen)
        self.assertEqual(b.probation_until, 1 + CONFIG["promotion.probation_days"])
        e.now = b.probation_until
        e.vet("a", True, ["无矛盾", "佐证 3 源"], 3)
        b = e.blocks["a"]
        self.assertEqual(b.zone, ZONE_STABLE)
        self.assertAlmostEqual(b.conf_stored, CONFIG["conf.promote_value"])
        types = [ev["t"] for ev in e.events]
        self.assertIn("PROBATION_ENTER", types)
        self.assertIn("VET_PASS", types)

    def test_vet_fail_queues(self):
        e = Engine()
        e.add_material("a", "A", "A")
        grow(e, "a", 16)
        e.reconcile(1)
        e.vet("a", False, ["红旗"], 1)
        b = e.blocks["a"]
        self.assertTrue(b.queued)
        self.assertFalse(b.frozen)
        self.assertIn("VET_FLAG", [ev["t"] for ev in e.events])

    def test_window_expiry_unfreezes_to_learn(self):
        e = Engine()
        fired = []
        e.add_material("a", "A", "A")
        grow(e, "a", 16)
        e.reconcile(1)
        until = e.blocks["a"].probation_until
        e.on_probation_expire = lambda b: (fired.append(b.id), e.probation_expire(b))
        e.reconcile(int(until))
        b = e.blocks["a"]
        self.assertIn("a", fired)
        self.assertFalse(b.frozen)
        self.assertEqual(b.zone_code(), ZONE_LEARN)
        self.assertGreater(b.conf_stored, 0.55)           # 积分保留


class TestFastFall(unittest.TestCase):
    def test_stable_decay_to_pending_to_learn_to_archive(self):
        e = Engine()
        e.add_material("a", "A", "A", admin=True)         # 0.85
        e.now = 0
        # 单次大幅跨越：800 天无人问津 → eff≈0.217，一次性物化（棘轮落当时有效值）
        e.reconcile(800)
        b = e.blocks["a"]
        self.assertTrue(b.pending)                         # 跌破 0.75 → 待处置
        self.assertAlmostEqual(b.conf_stored, b.effective(800), places=3)
        self.assertLessEqual(b.conf_stored, CONFIG["conf.machine_cap"])
        # 继续衰减跌破 0.60 → 学习区
        e.reconcile(1400)
        self.assertFalse(b.pending)
        self.assertEqual(b.zone_code(), ZONE_LEARN)
        # 跌破 0.05 → 归档
        e.reconcile(2200)
        self.assertTrue(b.archived)

    def test_daily_replay_materializes_at_ratchet(self):
        """逐日协调（真实节奏）：跌破 0.75 当日棘轮物化，落点贴近 0.749。"""
        e = Engine()
        e.add_material("a", "A", "A", admin=True)
        for d in range(1, 151):
            e.reconcile(d)
        b = e.blocks["a"]
        self.assertTrue(b.pending)
        self.assertGreater(b.conf_stored, 0.70)            # 稳定半衰期 365 天内刚跌破

    def test_ratchet_no_return(self):
        e = Engine()
        e.add_material("a", "A", "A", admin=True)
        e.reconcile(400)                                   # 衰减 → 棘轮下调
        dropped = e.blocks["a"].conf_stored
        self.assertLess(dropped, 0.85)
        # 采纳不返还分数（棘轮：掉的分须重新挣得）
        e.adopt("a")
        self.assertAlmostEqual(e.blocks["a"].conf_stored, dropped)

    def test_pending_recovery_by_admin(self):
        e = Engine()
        e.add_material("a", "A", "A", admin=True)
        e.reconcile(400)
        e.adjudicate("a", "restore")
        self.assertEqual(e.blocks["a"].zone, ZONE_STABLE)
        self.assertAlmostEqual(e.blocks["a"].conf_stored, 0.85)

    def test_full_review(self):
        e = Engine()
        e.add_material("mat", "M", "M", admin=True, content="x")
        e.add_chunk("mat", 1, "y")
        n = e.full_review("mat")
        self.assertEqual(n, 2)
        for bid in ("mat", "mat#1"):
            self.assertTrue(e.blocks[bid].pending)
            self.assertAlmostEqual(e.blocks[bid].conf_stored, 0.70)


class TestIronLaw(unittest.TestCase):
    def test_tier0_evidence_zero_effect(self):
        e = Engine()
        e.add_material("a", "A", "A", admin=True, tier0=True)
        before = e.blocks["a"].conf_stored
        e.evidence("a", "dislike", "踩")
        self.assertAlmostEqual(e.blocks["a"].conf_stored, before)   # 记账零豁免：不改分
        self.assertIn("EVID_NEG", [ev["t"] for ev in e.events])
        # 不衰减
        e.reconcile(1000)
        self.assertFalse(e.blocks["a"].pending)
        self.assertFalse(e.blocks["a"].archived)

    def test_iron_anomaly_red_light(self):
        e = Engine()
        e.add_material("a", "A", "A", admin=True, tier0=True)
        for _ in range(CONFIG["iron.anomaly_threshold"]):
            e.evidence("a", "dislike")
        self.assertIn("IRON_ALERT", [ev["t"] for ev in e.events])

    def test_iron_never_auto_demoted(self):
        e = Engine()
        e.add_material("a", "A", "A", admin=True, tier0=True)
        e.reconcile(3000)
        self.assertFalse(e.blocks["a"].archived)
        self.assertEqual(e.blocks["a"].zone_code(), ZONE_STABLE)


class TestArbitration(unittest.TestCase):
    def _pair(self, e: Engine):
        e.add_material("d", "D", "D", admin=True)          # 防守 0.85
        e.add_material("c", "C", "C")                      # 挑战 0.30
        return e.blocks["d"], e.blocks["c"]

    def test_ladder_hedge(self):
        e = Engine()
        d, c = self._pair(e)
        for _ in range(3):
            rec = e.conflict("d", "c", "共现")
        # 差 0.55 ≥ 0.25 → 对冲判负（runtime 阶梯结算）
        e.hedge_resolve(rec)
        self.assertEqual(rec["verdict"], "hedge")
        self.assertAlmostEqual(c.conf_stored,
                               CONFIG["conf.initial"] + CONFIG["score.dislike"])

    def test_shadow_duel_votes(self):
        e = Engine()
        d, c = self._pair(e)
        c.conf_stored = 0.62                                # 差 0.23 < 0.25
        self.assertEqual(e.arbitration_stage(
            {"defender": "d", "challenger": "c"}), "shadow_duel")
        rec = {"defender": "d", "challenger": "c", "count": 3, "status": "duel"}
        e.duel_vote(rec, "new")
        self.assertTrue(e.blocks["d"].pending)
        self.assertAlmostEqual(e.blocks["d"].conf_stored, 0.70)
        rec2 = {"defender": "d", "challenger": "c", "count": 3, "status": "duel"}
        e.duel_vote(rec2, "old")
        self.assertAlmostEqual(e.blocks["c"].conf_stored, 0.72)  # 0.62+0.10 上限内
        rec3 = {"defender": "d", "challenger": "c", "count": 3, "status": "duel"}
        e.duel_vote(rec3, "novote")
        self.assertEqual(rec3["count"], 2)

    def test_iron_conflict_suspends_challenger(self):
        e = Engine()
        e.add_material("d", "D", "D", admin=True, tier0=True)
        e.add_material("c", "C", "C")
        rec = e.conflict("d", "c", "挑战铁律")
        self.assertTrue(e.blocks["c"].suspended)
        self.assertFalse(e.blocks["c"].searchable())
        self.assertEqual(e.blocks["d"].iron_dissent, 1)

    def test_amend_replace_atomic(self):
        e = Engine()
        e.add_material("d", "D", "D", admin=True, tier0=True)
        e.add_material("c", "C", "C")
        e.conflict("d", "c", "挑战")
        e.amend_replace("d", "c")
        nd, nc = e.blocks["d"], e.blocks["c"]
        self.assertTrue(nc.tier0)
        self.assertAlmostEqual(nc.conf_stored, 0.85)
        self.assertFalse(nc.suspended)
        self.assertTrue(nd.archived)
        self.assertEqual(nd.superseded_by, "c")             # 取代链
        self.assertFalse(nd.tier0)

    def test_amend_reject_releases(self):
        e = Engine()
        e.add_material("d", "D", "D", admin=True, tier0=True)
        e.add_material("c", "C", "C")
        e.conflict("d", "c", "挑战")
        e.amend_reject("d", "c")
        self.assertFalse(e.blocks["c"].suspended)
        self.assertEqual(e.blocks["d"].iron_dissent, 0)


class TestRestoreArchive(unittest.TestCase):
    def test_archive_restore_to_learn(self):
        e = Engine()
        e.add_material("a", "A", "A")
        e.adjudicate("a", "archive")
        self.assertTrue(e.blocks["a"].archived)
        e.adjudicate("a", "restore")
        self.assertFalse(e.blocks["a"].archived)
        self.assertEqual(e.blocks["a"].zone_code(), ZONE_LEARN)
        self.assertAlmostEqual(e.blocks["a"].conf_stored, 0.30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
