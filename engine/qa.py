# -*- coding: utf-8 -*-
"""BetterRAG 问答装配 + 证据结算（规格 §3/§8）。

ask：检索 → 作答（LLM / 本地摘句兜底）→ 引用与分区标注（来源透明：学习区块
强制渲染「新知识」标注）→ ADOPT 活性记账 → 冲突检测（跨分区共现 + judge）。
feedback：👍/👎/转人工/工单闭环 → 幂等结算到该问题采纳的全部未归档块。
沉默零计分：无反馈不产生任何事件（ADR-0005 证据不对称）。
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Dict, List, Optional

from core import Block, Engine, CONFIG
from llm import LLM
from retrieve import Retriever, tokens, content_overlap
from store import Store

# UX 术语分层（用户侧体验评估 §4）：内部术语永不原样泄漏进用户界面
ZONE_UI = {
    0: ("g", "新知识"),
    1: ("b", "复核中"),
    2: ("y", "已验证"),
    3: ("", "待处置"),
    4: ("", "已归档"),
    5: ("r", "保全待审"),
}

FEEDBACK_KINDS = {
    "up": ("pos", "score.evid_pos", "👍（弱正证据）"),
    "close": ("pos", "score.evid_pos", "工单一次闭环（+0.02）"),
    "down": ("dislike", "score.dislike", "👎"),
    "escalate": ("escalate", "score.escalate", "转人工"),
    "reask": ("reask", "score.reask", "同题重问"),
}


def _qid(question: str) -> str:
    return uuid.uuid4().hex[:12]


def _sentences(text: str) -> List[str]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "---")):
            continue
        if line.startswith("|"):                      # 表格行整体作为一句（照搬）
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):   # 分隔行 |---|---| 跳过
                continue
            row = "；".join(c for c in cells if c)
            if len(row) >= 4:
                out.append(row)
            continue
        for s in re.split(r"(?<=[。！？；.!?])\s*", line):
            s = s.strip().strip("|- ")
            if len(s) >= 6:
                out.append(s)
    return out


class QA:
    def __init__(self, eng: Engine, llm: LLM, retriever: Retriever, store: Store) -> None:
        self.eng = eng
        self.llm = llm
        self.ret = retriever
        self.store = store
        self.on_conflict = None      # runtime 注入：冲突发生后处理（阶梯/双答）

    # ------------------------------------------------------------ 问答
    def ask(self, question: str) -> dict:
        eng = self.eng
        now = eng.now
        question = question.strip()
        reask = self._detect_reask(question)
        hits = self.ret.search(question, now)
        context: List[dict] = []
        for h in hits:
            b = h["block"]
            item = {"block": b, "sim": h["sim"], "exploration": h["exploration"]}
            nbrs = self.ret.neighbors(b)
            item["neighbors"] = nbrs if not h["exploration"] else []
            context.append(item)

        answer, used = self._compose(question, context)
        # ADOPT：活性记账（零积分，重置保鲜）
        for b in used:
            eng.adopt(b.id, "答案实际使用该块（活性·零积分）")
        qid = _qid(question)
        ts = time.time()
        self.store.add_question(qid, ts, now, question, " ".join(tokens(question))[:400])
        adopted_ids = [b.id for b in used]
        self.store.add_citations(qid, question, adopted_ids, ts)
        citations = [self._citation(b, exploration=(b.id in [
            c["block"].id for c in context if c["exploration"]]),
            in_answer=(b.id in [u.id for u in used]))
            for c in context for b in [c["block"]]]
        exploration = any(c["exploration"] for c in context)
        conflict = self._detect_conflict(used, question)
        payload = {
            "question_id": qid, "question": question, "answer": answer,
            "citations": citations, "exploration": exploration,
            "day": int(now), "reask_of": reask,
            "engine_mode": self.llm.status()["mode"],
        }
        if conflict:
            payload["conflict"] = conflict
        self.ret.invalidate()
        return payload

    def _compose(self, question: str, context: List[dict]) -> tuple:
        """作答：LLM 依据原文生成；兜底=原文摘句拼接（照搬精神，绝不虚构）。"""
        blocks = [c["block"] for c in context]
        if not blocks:
            return ("知识库中暂无可回答该问题的材料。"
                    "可将文档放入工作区后点击「立即扫描」，或由管理员导入。"), []
        # 上下文拼装：命中块 + 同材料相邻块（召回单元=材料，不切断上下文）
        ctx_parts = []
        for i, c in enumerate(context):
            b = c["block"]
            ctx_parts.append(f"[{i+1}] 《{b.material_title}》#{b.chunk_index}（"
                             f"{ZONE_UI[b.zone_code()][1]}）：\n{b.content[:600]}")
            for nb in c.get("neighbors", []):
                ctx_parts.append(f"[{i+1}+] 《{nb.material_title}》#{nb.chunk_index}"
                                 f"（相邻块）：\n{nb.content[:400]}")
        ctx_text = "\n\n".join(ctx_parts)
        text = self.llm.call("answer", f"问题：{question}\n\n知识块：\n{ctx_text}\n\n"
                             "请回答问题并标注使用了哪些编号。", max_tokens=700)
        if text:
            used = [b for i, b in enumerate(blocks)
                    if f"[{i+1}]" in text or f"［{i+1}］" in text] or blocks[:1]
            return text.strip(), used
        # 本地兜底：每个引用块摘 1-2 句原文
        parts = []
        for i, c in enumerate(context):
            b = c["block"]
            sents = _sentences(b.content)[:2]
            if sents:
                joined = "；".join(sents).rstrip("。")
                parts.append(f"（{i+1}）{joined}。——摘自《{b.material_title}》")
        answer = "依据知识库原文：\n" + "\n".join(parts) if parts else "知识库中暂无相关材料。"
        used = [c["block"] for c in context if _sentences(c["block"].content)[:2]]
        return answer, used or [context[0]["block"]]

    def _citation(self, b: Block, exploration: bool, in_answer: bool) -> dict:
        zc = b.zone_code()
        tone, label = ZONE_UI.get(zc, ("", "未知"))
        days = int(self.eng.now - b.created_at)
        if zc == 0:
            label = f"新知识（入库 {days} 天）"
        elif b.tier0:
            label = "官方权威"
        return {
            "block_id": b.id, "material_id": b.material_id,
            "title": b.material_title, "chunk": b.chunk_index,
            "zone": zc, "tone": tone, "label": label,
            "conf": round(b.effective(self.eng.now), 3),
            "exploration": exploration, "adopted": in_answer,
            "quote": b.content[:180],
        }

    # ------------------------------------------------------------ 冲突检测
    def _detect_conflict(self, used: List[Block], question: str) -> Optional[dict]:
        """§8 冲突：采纳集跨分区共现 + judge 判竞争。每次问答最多报一对。
        防守方=对中较高有效置信者。配对遍历全部跨材料组合（本地判定近零成本）；
        仅当接入 LLM 时按「机制五问·成本声明」限 6 次调用预算（高×低优先）。"""
        if len(used) < 2:
            return None
        now = self.eng.now
        ranked = sorted(used, key=lambda b: b.effective(now), reverse=True)
        llm_on = bool(self.llm.provider and self.llm.provider.get("ok"))
        budget = 6 if llm_on else len(ranked) * len(ranked)
        for i in range(len(ranked)):
            for j in range(i + 1, len(ranked)):
                if budget <= 0:
                    return None
                a, b = ranked[i], ranked[j]
                if a.material_id == b.material_id:
                    continue                              # 同材料相邻块是互补不是冲突
                budget -= 1
                verdict = self._judge(a, b)
                if verdict.get("competing"):
                    defender, challenger = (a, b) if a.effective(now) >= \
                        b.effective(now) else (b, a)
                    rec = self.eng.conflict(
                        defender.id, challenger.id,
                        note=f"问答共现·judge 判竞争（{verdict.get('reason', '')}）")
                    rec["question"] = question
                    rec["created_day"] = self.eng.now
                    if self.on_conflict:
                        self.on_conflict(rec)
                    return {"defender": defender.id, "challenger": challenger.id,
                            "count": rec["count"], "status": rec["status"],
                            "verdict_reason": verdict.get("reason", ""),
                            "engine": verdict.get("engine", "")}
        return None

    def _judge(self, a: Block, b: Block) -> dict:
        res = self.llm.call("judge",
                            f"问题语境下两个知识块是否竞争？\n[A] {a.title}：{a.content[:500]}"
                            f"\n[B] {b.title}：{b.content[:500]}", max_tokens=120)
        if res:
            try:
                import json as _json
                v = _json.loads(res[res.find("{"):res.rfind("}") + 1])
                if isinstance(v.get("competing"), bool):
                    v["engine"] = "llm"
                    return v
            except Exception:
                pass
        return self.llm.judge_local(a.title, a.content, b.title, b.content)

    # ------------------------------------------------------------ 证据结算
    def _detect_reask(self, question: str) -> Optional[dict]:
        """§3.2 重问判定：与近 30 天问题词元 Jaccard ≥ 阈值 → 对原采纳块记负证据。"""
        qt = set(tokens(question))
        if not qt:
            return None
        thr = float(CONFIG["signal.reask_sim_threshold"])
        for q in self.store.recent_questions(30):
            if self.eng.now - q["day"] > 30:
                continue
            pt = set((q["tokens"] or "").split())
            if not pt:
                continue
            sim = len(qt & pt) / len(qt | pt)
            if sim >= thr:
                adopted = [bid for bid in self.store.adopted_blocks(q["id"])
                           if bid in self.eng.blocks]
                for bid in adopted:
                    b = self.eng.blocks[bid]
                    if not b.archived:
                        self.eng.evidence(bid, "reask",
                                          f"同题重问（相似度 {sim:.2f}，原问题：{q['question'][:40]}）")
                return {"question_id": q["id"], "question": q["question"],
                        "sim": round(sim, 3), "blocks": adopted}
        return None

    def feedback(self, question_id: str, kind: str) -> dict:
        """显式证据：幂等结算（同一问题同一信号只记一次）。沉默零计分。"""
        if kind not in FEEDBACK_KINDS:
            raise ValueError(f"未知反馈类型：{kind}")
        if not self.store.mark_signal(question_id, kind):
            return {"ok": False, "reason": "重复信号已忽略（幂等去重）", "idempotent": True}
        ev_kind = FEEDBACK_KINDS[kind][0]
        note = FEEDBACK_KINDS[kind][2]
        adopted = [bid for bid in self.store.adopted_blocks(question_id)
                   if bid in self.eng.blocks]
        settled = []
        for bid in adopted:
            b = self.eng.blocks[bid]
            if not b.archived:
                self.eng.evidence(bid, ev_kind, note)
                settled.append(bid)
        self.ret.invalidate()
        return {"ok": True, "kind": kind, "settled": settled,
                "day": int(self.eng.now)}
