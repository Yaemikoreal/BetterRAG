# -*- coding: utf-8 -*-
"""BetterRAG 服务运行时 —— 把引擎装配成可服务状态（规格 §11/§13）。

职责：
  * 虚拟时钟（ADR-0004）：真实时间映射虚拟天（1 真实日 = 1 虚拟日），
    支持快进 jump（逐日重放 reconcile——被使用的块保鲜、无人问津的衰减）；
  * 协调任务：每虚拟日幂等物化（棘轮/重锚/跨界迁移/举证期挑刺回调）；
  * 仲裁阶梯结算：N=3 → 铁律红灯 / 对冲判负（自动）/ 影子双答（待点选）；
  * 持久化：事件增量落盘 + 全量快照（每次变更后刷盘）；
  * 面向 HTTP 层的原子操作（内部加锁，ThreadingHTTPServer 多线程安全）。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, Optional

from core import (Engine, Block, CONFIG, ZONE_LEARN, ZONE_PRELIM, ZONE_STABLE,
                  ZONE_PENDING, ZONE_ARCHIVED, ZONE_SUSPENDED)
from llm import LLM
from store import Store
from retrieve import Retriever
from ingest import Ingestor
from qa import QA
import promote

ZONE_NAMES = {0: "学习区", 1: "预备区", 2: "稳定区", 3: "待处置", 4: "归档", 5: "挂起"}

# config 白名单：可通过 API 调整的运行参数（其余为结构常量）
TUNABLE = {k for k in CONFIG if not k.startswith("conf.zone")}


class Runtime:
    def __init__(self, db_path: str, workspace: str,
                 base_day: float = 0.0, virtual: bool = False) -> None:
        self.lock = threading.RLock()
        self.store = Store(db_path)
        self.eng = Engine()
        self.workspace = workspace
        self.virtual = virtual          # virtual=True：时间只经 jump/advance 推进
        # 真实模式：base_day 对应 base_ts；每真实日 = 1 虚拟日
        self._base_day = float(base_day)
        self._base_ts = time.time()
        saved = self.store.get_meta("clock", None)
        if saved:
            self._base_day = float(saved["base_day"])
        self.llm = LLM(self.store.load_provider())
        if self.llm.provider is None:
            env = {k: os.environ.get(k, "") for k in
                   ("BETTERRAG_LLM_BASE_URL", "BETTERRAG_LLM_API_KEY", "BETTERRAG_LLM_MODEL")}
            if env["BETTERRAG_LLM_BASE_URL"] and env["BETTERRAG_LLM_MODEL"]:
                self.llm.configure({"name": "env", "base_url": env["BETTERRAG_LLM_BASE_URL"],
                                    "api_key": env["BETTERRAG_LLM_API_KEY"], "model": env["BETTERRAG_LLM_MODEL"],
                                    "ctx_window": 8192, "max_output": 2048, "ok": True,
                                    "last_error": "", "checked_at": 0.0})
        # 载入已存配置覆盖（CONFIG 为内存单例，加载后进程内生效）
        overrides = self.store.load_config()
        CONFIG.update(overrides)
        self.ret = Retriever(self.eng)
        self.qa = QA(self.eng, self.llm, self.ret, self.store)
        self.ing = Ingestor(self.eng, self.llm)
        # 引擎钩子：举证期窗满 → AI 挑刺；冲突 → 仲裁阶梯结算
        self.eng.on_probation_expire = promote.make_probation_callback(
            self.eng, self.llm, self.ret)
        self.qa.on_conflict = self._on_conflict
        # 载入快照与事件水位（事件全量回装：阈值判定/案卷/异常窗依赖完整内存链）
        self._has_snapshot = self.store.load(self.eng)
        self._flushed = self.store.event_count()
        if self._flushed:
            eng_events = list(reversed(self.store.events(limit=1_000_000)))
            self.eng.events = eng_events
        self.ret.invalidate()
        self._tick()

    # ================================================================ 时钟
    def _current_day(self) -> float:
        if self.virtual:
            return self._base_day
        return self._base_day + (time.time() - self._base_ts) / 86400.0

    def _tick(self) -> None:
        """推进到当前虚拟日：逐日 reconcile（幂等物化，含举证期挑刺回调）。"""
        target = self._current_day()
        self._advance_to(target)

    def _advance_to(self, day: float) -> None:
        prev = int(self.eng.now + 1e-9)
        target = int(day + 1e-9)
        for d in range(prev + 1, target + 1):
            self.eng.now = float(d)
            self.eng.reconcile(float(d))
        self.eng.now = float(day)
        self._flush()

    def jump(self, days: int) -> dict:
        """⏩ 快进：jump + 逐日重放。真实模式通过平移锚点保持继续流逝。"""
        with self.lock:
            days = int(days)
            if days <= 0:
                raise ValueError("days 必须为正整数")
            self._advance_to(self.eng.now + days)
            self._base_day = self.eng.now
            self._base_ts = time.time()
            self.store.set_meta("clock", {"base_day": self._base_day})
            return {"day": int(self.eng.now), "jumped": days}

    def set_clock(self, day: float) -> None:
        """测试/种子用：直接设定虚拟日（不走逐日重放）。"""
        with self.lock:
            self._advance_to(float(day))
            self._base_day = self.eng.now
            self._base_ts = time.time()

    # ================================================================ 持久化
    def _flush(self) -> None:
        self.store.append_events(self.eng.events, self._flushed)
        self._flushed = len(self.eng.events)
        self.store.save_blocks(self.eng)

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        with self.lock:
            self._flush()
            self.store.close()

    # ================================================================ 问答面
    def ask(self, question: str) -> dict:
        with self.lock:
            self._tick()
            out = self.qa.ask(question)
            self._flush()
            return out

    def feedback(self, question_id: str, kind: str) -> dict:
        with self.lock:
            self._tick()
            out = self.qa.feedback(question_id, kind)
            self._flush()
            return out

    # ================================================================ 收纳
    def scan(self) -> dict:
        with self.lock:
            self._tick()
            report = self.ing.scan(self.workspace)
            self.ret.invalidate()
            self._flush()
            return report

    def admin_import(self, path: str, tier0: bool = False) -> dict:
        with self.lock:
            self._tick()
            out = self.ing.admin_import(path, tier0=tier0)
            self.ret.invalidate()
            self._flush()
            return out

    # ================================================================ 例外裁决台
    def exceptions(self) -> dict:
        with self.lock:
            self._tick()
            eng = self.eng
            queue, pending, probation = [], [], []
            for b in eng.blocks.values():
                if b.queued:
                    queue.append(self._block_view(b, extra={
                        "pending_reason": b.pending_reason,
                        "vet_sources": b.vet_sources}))
                elif b.pending:
                    pending.append(self._block_view(b, extra={
                        "pending_reason": b.pending_reason}))
                elif b.frozen:
                    probation.append(self._block_view(b, extra={
                        "probation_until": int(b.probation_until)}))
            return {"queue": queue, "pending": pending, "probation": probation,
                    "day": int(eng.now)}

    def adjudicate(self, block_id: str, action: str) -> dict:
        with self.lock:
            self._tick()
            if block_id not in self.eng.blocks:
                raise KeyError(f"未知知识块：{block_id}")
            if action not in ("pass", "reject", "restore", "demote", "archive"):
                raise ValueError(f"未知裁决动作：{action}")
            self.eng.adjudicate(block_id, action)
            self.ret.invalidate()
            self._flush()
            return {"ok": True, "block": block_id, "action": action,
                    "day": int(self.eng.now)}

    def admin_op(self, op: str, **kw) -> dict:
        with self.lock:
            self._tick()
            eng = self.eng
            if op == "kickback":
                eng.adjudicate(kw["block_id"], "demote")
            elif op == "full_review":
                n = eng.full_review(kw["material_id"])
                out = {"ok": True, "blocks": n}
            elif op == "tier0":
                eng.set_tier0(kw["block_id"], bool(kw.get("on", True)))
                out = {"ok": True}
            elif op == "restore":
                eng.adjudicate(kw["block_id"], "restore")
                out = {"ok": True}
            elif op == "import":
                out = self.admin_import(kw["path"], tier0=bool(kw.get("tier0", False)))
            else:
                raise ValueError(f"未知管理操作：{op}")
            self._flush()
            return out

    # ================================================================ 红灯与修正案
    def alerts(self) -> dict:
        with self.lock:
            self._tick()
            eng = self.eng
            reds, suspended = [], []
            thr = CONFIG["iron.dissent_threshold"]
            for b in eng.blocks.values():
                if b.tier0 and not b.archived:
                    if b.iron_dissent >= thr:
                        reds.append(self._block_view(b, extra={
                            "iron_dissent": b.iron_dissent, "threshold": thr,
                            "reason": f"铁律异议 {b.iron_dissent}/{thr}"}))
                if b.suspended:
                    suspended.append(self._block_view(b))
            return {"red": reds, "suspended": suspended, "threshold": thr,
                    "day": int(eng.now)}

    def amend(self, action: str, old_id: str, new_id: str) -> dict:
        with self.lock:
            self._tick()
            if action == "replace":
                self.eng.amend_replace(old_id, new_id)
            elif action == "reject":
                self.eng.amend_reject(old_id, new_id)
            else:
                raise ValueError(f"未知修正案动作：{action}")
            self._flush()
            return {"ok": True, "action": action, "old": old_id, "new": new_id,
                    "day": int(self.eng.now)}

    # ================================================================ 冲突/影子对决
    def conflicts(self) -> dict:
        with self.lock:
            self._tick()
            out = []
            for r in self.eng.conflicts:
                if r["status"] in ("open", "duel", "iron"):
                    item = dict(r)
                    d = self.eng.blocks.get(r["defender"])
                    c = self.eng.blocks.get(r["challenger"])
                    item["defender_view"] = self._block_view(d) if d else None
                    item["challenger_view"] = self._block_view(c) if c else None
                    item["stage"] = self.eng.arbitration_stage(r)
                    out.append(item)
            return {"conflicts": out, "threshold": CONFIG["conflict.threshold_n"],
                    "day": int(self.eng.now)}

    def vote(self, defender: str, challenger: str, choice: str) -> dict:
        with self.lock:
            self._tick()
            rec = next((r for r in self.eng.conflicts
                        if r["defender"] == defender and r["challenger"] == challenger
                        and r["status"] == "duel"), None)
            if rec is None:
                raise KeyError("不存在待点选的影子对决")
            self.eng.duel_vote(rec, choice)
            self._flush()
            return {"ok": True, "verdict": rec.get("verdict", ""), "day": int(self.eng.now)}

    def _on_conflict(self, rec: dict) -> None:
        """阶梯结算：N=3 → 铁律红灯（人工）/ 对冲判负（自动）/ 影子双答（备答案待点选）。"""
        eng = self.eng
        if rec["count"] < CONFIG["conflict.threshold_n"]:
            return
        stage = eng.arbitration_stage(rec)
        if stage == "hedge":
            eng.hedge_resolve(rec)
        elif stage == "shadow_duel":
            rec["status"] = "duel"
            d, c = eng.blocks[rec["defender"]], eng.blocks[rec["challenger"]]
            q = rec.get("question", "")
            rec["defender_answer"] = self.qa._compose(q, [{"block": d, "sim": 1.0,
                                                           "exploration": False,
                                                           "neighbors": []}])[0]
            rec["challenger_answer"] = self.qa._compose(q, [{"block": c, "sim": 1.0,
                                                             "exploration": False,
                                                             "neighbors": []}])[0]
            eng._log("ARB_STAGE", d, 0.0,
                     f"N={rec['count']} 触发仲裁阶梯 → 影子双答点选（新旧并排）", "engine")

    # ================================================================ 视图/仪表盘
    def _block_view(self, b: Block, extra: Optional[dict] = None) -> dict:
        v = {
            "id": b.id, "material_id": b.material_id, "title": b.material_title,
            "chunk": b.chunk_index, "conf": round(b.conf_stored, 3),
            "eff": round(b.effective(self.eng.now), 3), "zone": b.zone_code(),
            "frozen": b.frozen, "pending": b.pending, "queued": b.queued,
            "suspended": b.suspended, "archived": b.archived, "tier0": b.tier0,
            "iron_dissent": b.iron_dissent, "adopted_count": b.adopted_count,
            "tenure": b.tenure, "created": int(b.created_at),
            "idle_days": int(b.idle_days(self.eng.now)),
            "summary": (b.summary or (self.eng.materials.get(b.material_id).summary
                                      if b.material_id in self.eng.materials else ""))[:200],
        }
        if extra:
            v.update(extra)
        return v

    def state(self) -> dict:
        with self.lock:
            self._tick()
            eng = self.eng
            dist: Dict[str, int] = {ZONE_NAMES[z]: 0 for z in
                                    (0, 1, 2, 3, 4, 5)}
            for b in eng.blocks.values():
                dist[ZONE_NAMES[b.zone_code()]] += 1
            return {
                "day": int(eng.now), "virtual": self.virtual,
                "blocks": [self._block_view(b) for b in
                           sorted(eng.blocks.values(), key=lambda x: x.id)],
                "materials": [{"id": m.id, "title": m.title, "summary": m.summary,
                               "admin": m.imported_by_admin,
                               "summary_version": m.summary_version}
                              for m in eng.materials.values()],
                "distribution": dist,
                "config": dict(CONFIG),
                "provider": self.llm.status(),
                "counts": {
                    "events": len(eng.events),
                    "open_conflicts": sum(1 for r in eng.conflicts
                                          if r["status"] in ("open", "duel", "iron")),
                    "queue": sum(1 for b in eng.blocks.values() if b.queued),
                },
            }

    def starmap(self) -> dict:
        with self.lock:
            self._tick()
            eng = self.eng
            nodes = []
            for b in eng.blocks.values():
                nodes.append({
                    "id": b.id, "title": b.material_title, "chunk": b.chunk_index,
                    "eff": round(b.effective(eng.now), 3), "zone": b.zone_code(),
                    "flags": (1 if b.frozen else 0) | (2 if b.pending else 0) |
                             (4 if b.suspended else 0) | (8 if b.tier0 else 0) |
                             (16 if b.archived else 0) | (32 if b.queued else 0),
                    "created": int(b.created_at),
                })
            links = [{"defender": r["defender"], "challenger": r["challenger"],
                      "count": r["count"], "status": r["status"]}
                     for r in eng.conflicts
                     if r["status"] in ("open", "duel", "iron")]
            return {"nodes": nodes, "links": links, "day": int(eng.now),
                    "rings": [CONFIG["conf.archive_line"], CONFIG["conf.zone.prelim_min"],
                              CONFIG["conf.zone.stable_min"]]}

    def events(self, limit: int = 120, block_id: str = "") -> dict:
        with self.lock:
            self._tick()
            return {"events": self.store.events(limit=limit, block_id=block_id),
                    "day": int(self.eng.now)}

    def biography(self, block_id: str) -> dict:
        with self.lock:
            self._tick()
            b = self.eng.blocks.get(block_id)
            if not b:
                raise KeyError(f"未知知识块：{block_id}")
            evs = list(reversed(self.store.events(limit=400, block_id=block_id)))
            narrative = self.llm.biography_local(b.material_title, evs)
            text = self.llm.call("biography", "审计事件：" + str(evs[:40])[:3000],
                                 max_tokens=200)
            if text:
                narrative = text.strip()
            return {"block": self._block_view(b), "events": evs, "story": narrative,
                    "content": b.content[:2000]}

    # ================================================================ 配置
    def set_config(self, items: Dict[str, float]) -> dict:
        with self.lock:
            bad = [k for k in items if k not in TUNABLE]
            if bad:
                raise ValueError(f"不可调参数：{bad}")
            for k, v in items.items():
                if k == "promotion.policy":
                    if str(v) not in ("auto", "gated"):
                        raise ValueError("promotion.policy 只接受 auto | gated")
                    CONFIG[k] = str(v)
                    continue
                v = float(v)
                if v < 0 or v > 400:
                    raise ValueError(f"{k} 超出范围")
                CONFIG[k] = v
            self.store.set_config({k: CONFIG[k] for k in items})
            return {"ok": True, "config": dict(CONFIG)}

    def get_config(self) -> dict:
        return {"config": dict(CONFIG), "tunable": sorted(TUNABLE)}

    # ================================================================ 引擎接入
    def get_provider(self) -> dict:
        return self.llm.status()

    def set_provider(self, p: dict) -> dict:
        with self.lock:
            cur = self.llm.provider or {}
            cur.update({k: p.get(k, cur.get(k)) for k in
                        ("name", "base_url", "api_key", "model", "ctx_window", "max_output")})
            cur.setdefault("ok", False)
            self.llm.configure(cur)
            self.store.save_provider(cur)
            return self.llm.status()

    def test_provider(self) -> dict:
        with self.lock:
            out = self.llm.test_connect()
            if self.llm.provider:
                self.store.save_provider(self.llm.provider)
            return out
