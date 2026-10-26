# -*- coding: utf-8 -*-
"""运行时集成测试：seed → ask → feedback → 快进 → 自证晋升 → 仲裁 → 持久化回路。

全部走本地兜底（无 LLM、无网络），虚拟时钟模式确定性推进（ADR-0004）。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine"))

from core import CONFIG, ZONE_STABLE, ZONE_PENDING, ZONE_LEARN
from runtime import Runtime

F_IRON = "# 计费铁律\n对客资费口径以备案版本为准，双人复核，复核记录保存不少于 5 年，全量留痕。"
F_STABLE = ("# 套餐资费标准\n套餐资费标准：月费 100 元，含 10GB 通用流量，合约期 24 个月。\n"
            "超出通用流量后按套外流量计费，当月有效。")
F_NEW = ("# 新套餐政策\n新套餐政策：月费 80 元，含 15GB 通用流量，合约期 12 个月，次月生效。\n"
         "超出通用流量后按套外流量计费，当月有效。")
# 自证晋升目标：与稳定区无重叠、无数字口径冲突；三份独立佐证同述不同源
F_TARGET = ("# 流量结转说明\n流量结转规则：当月未用完的通用流量可结转至次月使用，"
            "结转部分次月月底清零，仅限主套餐内通用流量。")
F_CORRO = [
    "# 结转速览一\n流量结转规则：当月未用完的通用流量可结转次月，结转部分次月月底清零。",
    "# 结转速览二\n按流量结转规则，当月未用完的通用流量可结转到次月使用，次月月底清零。",
    "# 结转速览三\n流量结转：当月没用完的通用流量可结转至次月，结转流量次月月底清零。",
]


class TestRuntime(unittest.TestCase):
    def setUp(self):
        self._config_backup = dict(CONFIG)                   # CONFIG 是全局单例，测试间隔离
        self.tmp = tempfile.mkdtemp(prefix="rt-")
        self.ws = os.path.join(self.tmp, "workspace")
        os.makedirs(self.ws)
        for name, content in [("计费铁律", F_IRON), ("套餐资费标准", F_STABLE),
                              ("新套餐政策", F_NEW), ("流量结转说明", F_TARGET)] + \
                             [(f"结转速览{i}", t) for i, t in enumerate(F_CORRO)]:
            with open(os.path.join(self.ws, name + ".md"), "w", encoding="utf-8") as fh:
                fh.write(content)
        self.db = os.path.join(self.tmp, "betterrag.db")
        self.rt = Runtime(self.db, self.ws, virtual=True)

    def tearDown(self):
        self.rt.close()
        CONFIG.clear()
        CONFIG.update(self._config_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        self.rt.admin_import(os.path.join(self.ws, "计费铁律.md"), tier0=True)
        self.rt.admin_import(os.path.join(self.ws, "套餐资费标准.md"))
        self.rt.scan()

    def test_seed_and_ask(self):
        self._seed()
        self.assertTrue(any(b["tier0"] for b in self.rt.state()["blocks"]))
        out = self.rt.ask("套餐资费标准 月费 流量 是多少")
        self.assertTrue(out["citations"])
        self.assertTrue(any(c["label"] == "已验证" for c in out["citations"]))
        self.assertTrue(any("新知识" in c["label"] for c in out["citations"]))

    def test_scan_idempotent(self):
        self._seed()
        r1 = self.rt.scan()
        base = len(self.rt.eng.blocks)
        r2 = self.rt.scan()
        self.assertEqual(r2["unchanged"], 7)               # 铁律+资费+新政策+结转+佐证×3
        self.assertEqual(len(self.rt.eng.blocks), base)

    def test_feedback_and_reask_settlement(self):
        self._seed()
        out = self.rt.ask("套餐资费标准 月费 流量 是多少")
        self.rt.feedback(out["question_id"], "up")
        conf = {b["id"]: b["conf"] for b in self.rt.state()["blocks"]}
        out2 = self.rt.ask("套餐资费标准 月费 流量 是多少")     # 同题重问
        self.assertIsNotNone(out2["reask_of"])
        conf2 = {b["id"]: b["conf"] for b in self.rt.state()["blocks"]}
        # 重问的负证据落在原采纳块（铁律记账零豁免：不改分，跳过）
        checked = 0
        for bid in out2["reask_of"]["blocks"]:
            if self.rt.eng.blocks[bid].tier0:
                continue
            self.assertLess(conf2[bid], conf[bid])
            checked += 1
        self.assertGreater(checked, 0)

    def test_auto_promotion_end_to_end(self):
        """收纳 → 证据净攒 → 举证期 → 窗满 AI 挑刺（本地规则）→ 自证晋升。"""
        self._seed()
        blk = self.rt.eng.material_blocks("流量结转说明")[0]
        for i in range(16):                                     # 0.30 → 0.62
            self.rt.eng.evidence(blk.id, "pos", "工单一次闭环")
            self.rt.eng.adopt(blk.id, "采纳")
        self.rt.jump(1)
        self.assertTrue(blk.frozen)                             # 举证期冻结
        until = blk.probation_until
        self.rt.jump(int(until - self.rt.eng.now) + 1)          # 窗满 → 挑刺
        self.assertEqual(blk.zone, ZONE_STABLE)                 # 自证晋升（佐证 3 源）
        self.assertAlmostEqual(blk.conf_stored, CONFIG["conf.promote_value"])
        types = [e["t"] for e in self.rt.eng.events]
        self.assertIn("VET_PASS", types)
        pass_ev = [e for e in self.rt.eng.events if e["t"] == "VET_PASS"][-1]
        self.assertIn("3 源", pass_ev["note"])                  # 案卷附佐证数

    def test_gated_policy_queues(self):
        self._seed()
        self.rt.set_config({"promotion.policy": "gated"})
        blk = self.rt.eng.material_blocks("流量结转说明")[0]
        for i in range(16):
            self.rt.eng.evidence(blk.id, "pos", "工单一次闭环")
        self.rt.jump(10)
        ex = self.rt.exceptions()
        self.assertIn(blk.id, [b["id"] for b in ex["queue"]])

    def test_jump_decay_demotion_and_archive(self):
        self._seed()
        self.rt.jump(150)                                        # 跌破 0.75 当日物化
        st = {b["id"]: b for b in self.rt.state()["blocks"]}
        self.assertEqual(st["套餐资费标准"]["zone"], ZONE_PENDING)
        self.rt.jump(70)                                         # 跌破 0.60 滑回学习区
        st = {b["id"]: b for b in self.rt.state()["blocks"]}
        self.assertEqual(st["套餐资费标准"]["zone"], ZONE_LEARN)
        self.rt.jump(400)                                        # 跌破 0.05 归档
        st = {b["id"]: b for b in self.rt.state()["blocks"]}
        self.assertTrue(st["套餐资费标准"]["archived"])

    def test_conflict_ladder_shadow_duel_and_vote(self):
        self._seed()
        # 预成长挑战者至 0.62（差 0.23 < 0.25 → 影子双答）
        blk = self.rt.eng.material_blocks("新套餐政策")[0]
        for i in range(16):
            self.rt.eng.evidence(blk.id, "pos", "工单一次闭环")
            self.rt.eng.adopt(blk.id, "采纳")
        # 三次共现提问（去重问题文本避免触发重问）
        for i in range(3):
            self.rt.ask(f"套餐资费标准 新套餐政策 月费 流量 对比 第{i}轮")
        cf = self.rt.conflicts()["conflicts"]
        duel = [c for c in cf if c["status"] == "duel"]
        self.assertTrue(duel)
        d = duel[0]
        self.assertTrue(d["defender_answer"] and d["challenger_answer"])
        self.rt.vote(d["defender"], d["challenger"], "new")
        st = {b["id"]: b for b in self.rt.state()["blocks"]}
        self.assertEqual(st[d["defender"]]["zone"], ZONE_PENDING)

    def test_iron_red_light_and_amendment(self):
        self._seed()
        iron = "计费铁律"
        # 三个挑战者（同主题高重叠、数字口径各异）：挂起后不可检索，
        # 异议须由不同挑战者累计——与规格 §4.3 一致
        challs = ["政策方向调整", "口径补充说明", "复核流程修订"]
        texts = [
            "对客资费口径以备案版本为准，双人复核，复核记录线上留存 90 天，全量留痕。",
            "对客资费口径以备案版本为准，双人复核，复核记录系统留存 180 天，全量留痕。",
            "对客资费口径以备案版本为准，双人复核，复核记录留存 1 年即可，全量留痕。",
        ]
        for name, text in zip(challs, texts):
            with open(os.path.join(self.ws, name + ".md"), "w", encoding="utf-8") as fh:
                fh.write(f"# {name}\n{text}")
        self.rt.scan()
        for i, name in enumerate(challs):
            self.rt.ask(f"资费口径备案 双人复核 {name} 第{i}次")
        al = self.rt.alerts()
        self.assertTrue(al["suspended"], "挑战者应被保护性挂起")     # 挑战者保全
        self.assertGreaterEqual(self.rt.eng.blocks[iron].iron_dissent, 3)
        self.assertIn("IRON_ALERT", [e["t"] for e in self.rt.eng.events])  # 达阈值自动亮红灯
        self.assertTrue(al["red"])                                    # 红灯置顶
        # 修正案接替
        chall = al["suspended"][0]["id"]
        self.rt.amend("replace", iron, chall)
        st = {x["id"]: x for x in self.rt.state()["blocks"]}
        self.assertTrue(st[chall]["tier0"])
        self.assertTrue(st[iron]["archived"])

    def test_persistence_round_trip(self):
        self._seed()
        out = self.rt.ask("套餐资费标准 月费 流量")
        self.rt.feedback(out["question_id"], "close")
        day = self.rt.eng.now
        n_blocks = len(self.rt.eng.blocks)
        n_events_db = self.rt.store.event_count()
        self.rt.close()
        rt2 = Runtime(self.db, self.ws, virtual=True)
        try:
            self.assertEqual(len(rt2.eng.blocks), n_blocks)
            self.assertEqual(rt2.store.event_count(), n_events_db)
            self.assertEqual(len(rt2.eng.events), n_events_db)    # 事件全量回装
            self.assertAlmostEqual(rt2.eng.now, day, places=2)
            st = {b["id"]: b for b in rt2.state()["blocks"]}
            self.assertTrue(any(b["tier0"] for b in st.values()))
            # 挑刺案卷依赖完整事件链（重启后仍可判定负证据）
            self.assertTrue(rt2.exceptions() is not None)
        finally:
            rt2.close()

    def test_reload_does_not_refreeze_stable_blocks(self):
        """回归：物化 zone 字段必须持久化——重启后稳定块/铁律块不得被误冻入举证期。"""
        self._seed()
        before = {b["id"]: (b["zone"], b["conf"]) for b in self.rt.state()["blocks"]}
        self.rt.close()
        rt2 = Runtime(self.db, self.ws, virtual=True)
        try:
            rt2.jump(10)                                          # 重启后首个协调周期
            for b in rt2.state()["blocks"]:
                if before[b["id"]][0] == 2:                       # 原稳定区（含铁律）
                    self.assertNotEqual(b["zone"], 1,
                                        f"{b['id']} 重启后被误冻入举证期")
                    self.assertGreaterEqual(b["conf"], 0.74)
        finally:
            rt2.close()

    def test_config_tunable_and_guard(self):
        self.rt.set_config({"score.evid_pos": 0.03})
        self.assertAlmostEqual(CONFIG["score.evid_pos"], 0.03)
        with self.assertRaises(ValueError):
            self.rt.set_config({"conf.zone.stable_min": 0.1})
        with self.assertRaises(ValueError):
            self.rt.set_config({"promotion.policy": "chaos"})
        self.rt.set_config({"promotion.policy": "gated"})
        self.assertEqual(CONFIG["promotion.policy"], "gated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
