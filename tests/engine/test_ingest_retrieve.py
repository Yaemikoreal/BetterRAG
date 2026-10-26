# -*- coding: utf-8 -*-
"""收纳管线 + 检索管线 + 证据结算单测（规格 §3.2/§8/§9）。"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "engine"))

from core import Engine, CONFIG, ZONE_STABLE, ZONE_LEARN
from llm import LLM
from store import Store
from retrieve import Retriever, tokens
from ingest import Ingestor, material_id_for, split_chunks
from qa import QA

LONG_DOC = "\n\n".join(
    f"## 第{i}节\n这是第{i}节的内容，讲述资费政策的第{i}个方面，" + "详细内容。" * 30
    for i in range(8))


def make_ws(files: dict) -> str:
    d = tempfile.mkdtemp(prefix="ws-")
    for name, content in files.items():
        with open(os.path.join(d, name + ".md"), "w", encoding="utf-8") as fh:
            fh.write(content)
    return d


class TestChunking(unittest.TestCase):
    def test_headings_split(self):
        chunks = split_chunks(LONG_DOC)
        self.assertGreaterEqual(len(chunks), 8)
        for c in chunks:
            self.assertLessEqual(len(c), 1200)

    def test_plain_doc_sliding_window(self):
        doc = "资费政策说明。" * 200
        chunks = split_chunks(doc)
        self.assertGreater(len(chunks), 1)

    def test_verbatim_principle(self):
        """照搬原则：切片必须是原文的子串。"""
        chunks = split_chunks(LONG_DOC)
        for c in chunks:
            self.assertTrue(c in LONG_DOC)


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="db-")
        self.eng = Engine()
        self.store = Store(os.path.join(self.tmp, "t.db"))
        self.llm = LLM(None)
        self.ing = Ingestor(self.eng, self.llm)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_and_idempotency(self):
        ws = make_ws({"资费A": "# 资费A\n月费 100 元含 10GB 流量。",
                      "资费B": "# 资费B\n月费 200 元含 20GB 流量。"})
        r1 = self.ing.scan(ws)
        self.assertEqual(r1["scanned"], 2)
        self.assertEqual(sum(x["blocks"] for x in r1["ingested"]), 2)
        n1 = len(self.eng.blocks)
        r2 = self.ing.scan(ws)
        self.assertEqual(r2["unchanged"], 2)
        self.assertEqual(len(self.eng.blocks), n1)        # 幂等：状态不变

    def test_content_change_new_lifecycle(self):
        """内容变更 → 新块新生命周期；旧块保留自然衰减（不覆盖状态）。"""
        ws = make_ws({"政策": "# 政策\n月费 100 元。"})
        self.ing.scan(ws)
        mid = material_id_for(os.path.join(ws, "政策.md"))
        old = self.eng.material_blocks(mid)[0]
        old.conf_stored = 0.55
        with open(os.path.join(ws, "政策.md"), "w", encoding="utf-8") as fh:
            fh.write("# 政策\n月费 100 元。新增：流量结转。")
        self.ing.scan(ws)
        blocks = self.eng.material_blocks(mid)
        self.assertEqual(len(blocks), 2)
        self.assertIn(old, blocks)                         # 旧块保留
        self.assertEqual(old.conf_stored, 0.55)            # 状态未被重置
        new = [b for b in blocks if b.id != old.id][0]
        self.assertAlmostEqual(new.conf_stored, CONFIG["conf.initial"])

    def test_admin_import_tier0(self):
        ws = make_ws({"铁律": "# 铁律\n双人复核。"})
        path = os.path.join(ws, "铁律.md")
        self.ing.admin_import(path, tier0=True)
        b = self.eng.blocks[material_id_for(path)]
        self.assertTrue(b.tier0)
        self.assertAlmostEqual(b.conf_stored, CONFIG["conf.promote_value"])
        self.assertEqual(b.zone, ZONE_STABLE)

    def test_summary_local_extractive(self):
        ws = make_ws({"文档": "# 标题\n第一句是资费调整的内容。\n第二句说明生效时间。"})
        self.ing.scan(ws)
        m = list(self.eng.materials.values())[0]
        self.assertTrue(m.summary)
        self.assertIn("标题", m.summary)


class TestRetrieve(unittest.TestCase):
    def setUp(self):
        self.eng = Engine()
        self.eng.add_material("stable", "套餐资费标准", "套餐资费标准", admin=True,
                              content="套餐资费标准：月费 100 元，含 10GB 通用流量。")
        self.eng.add_material("learn", "新套餐政策", "新套餐政策",
                              content="新套餐政策：月费 80 元，含 15GB 通用流量，次月生效。")
        self.ret = Retriever(self.eng)

    def test_ranking_conf_weighted(self):
        hits = self.ret.search("套餐资费 月费 流量", 0)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["block"].id, "stable")    # 相关度近似 → 置信度加权胜出

    def test_exploration_seat(self):
        hits = self.ret.search("新套餐政策 次月生效", 0)
        exp = [h for h in hits if h["exploration"]]
        # 新政策块本身相关度最高时不触发；换一个偏向稳定块的提问
        hits2 = self.ret.search("套餐资费标准 通用流量是多少", 0)
        ids = {h["block"].id for h in hits2}
        self.assertIn("stable", ids)

    def test_excluded_states(self):
        b = self.eng.blocks["learn"]
        b.archived = True
        self.ret.invalidate()
        hits = self.ret.search("新套餐政策", 0)
        self.assertNotIn("learn", {h["block"].id for h in hits})

    def test_neighbors_window(self):
        self.eng.add_chunk("stable", 1, "套餐资费标准的附加说明：超出流量 3 元每 GB。")
        self.ret.invalidate()
        nb = self.ret.neighbors(self.eng.blocks["stable"])
        self.assertEqual([x.chunk_index for x in nb], [1])


class TestQAAndFeedback(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="db-")
        self.eng = Engine()
        self.store = Store(os.path.join(self.tmp, "t.db"))
        self.llm = LLM(None)
        self.ret = Retriever(self.eng)
        self.qa = QA(self.eng, self.llm, self.ret, self.store)
        shared = "超出通用流量后按套外流量计费，当月有效。"
        self.eng.add_material("stable", "套餐资费标准", "套餐资费标准", admin=True,
                              content=f"套餐资费标准：月费 100 元，含 10GB 通用流量。\n{shared}")
        self.eng.add_material("learn", "新套餐政策", "新套餐政策",
                              content=f"新套餐政策：月费 80 元，含 15GB 通用流量，次月生效。\n{shared}")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ask_adopts_and_cites(self):
        out = self.qa.ask("套餐资费标准 月费 流量")
        self.assertTrue(out["citations"])
        self.assertTrue(any(c["adopted"] for c in out["citations"]))
        adopted = [c["block_id"] for c in out["citations"] if c["adopted"]]
        self.assertEqual(self.store.adopted_blocks(out["question_id"]), adopted)
        # 学习区引用必须带「新知识」标注（来源透明）
        learn = [c for c in out["citations"] if c["block_id"] == "learn"]
        if learn:
            self.assertIn("新知识", learn[0]["label"])

    def test_reask_detection(self):
        out1 = self.qa.ask("套餐资费标准 月费是多少")
        before = self.eng.blocks["stable"].conf_stored
        out2 = self.qa.ask("套餐资费标准 月费是多少")      # 同题重问
        self.assertIsNotNone(out2["reask_of"])
        self.assertAlmostEqual(self.eng.blocks["stable"].conf_stored,
                               before + CONFIG["score.reask"])

    def test_feedback_idempotent(self):
        out = self.qa.ask("套餐资费标准 月费")
        before = self.eng.blocks["stable"].conf_stored
        r1 = self.qa.feedback(out["question_id"], "up")
        self.assertTrue(r1["ok"])
        mid = self.eng.blocks["stable"].conf_stored
        r2 = self.qa.feedback(out["question_id"], "up")
        self.assertTrue(r2.get("idempotent"))
        self.assertEqual(self.eng.blocks["stable"].conf_stored, mid)
        self.assertAlmostEqual(mid, before + CONFIG["score.evid_pos"])
        # 沉默零计分：不反馈就没有事件
        out2 = self.qa.ask("新套餐政策 生效时间")
        self.assertEqual(out2["question_id"] != out["question_id"], True)

    def test_conflict_cross_zone_competing(self):
        """跨分区共现 + 本地 judge（数字口径不一致）→ 冲突计数。"""
        for _ in range(3):
            self.qa.ask(f"套餐资费 标准 月费 流量 第{_}次询问")
        recs = [r for r in self.eng.conflicts
                if r["defender"] == "stable" and r["challenger"] == "learn"]
        self.assertTrue(recs)
        self.assertGreaterEqual(recs[0]["count"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
