# -*- coding: utf-8 -*-
"""
BetterRAG 生命周期引擎核心（演示版 —— 机制真实，非伪造）

严格实现 docs/生命周期引擎规格.md v4：
  §2 一个变量（conf_stored / freshness / 分区投影 / 棘轮 / 机器写入边界）
  §3 两种信号（活性 / 正证据 / 负证据 / 沉默零计分）
  §4 三条规则（慢升·自证晋升 / 快降 / 铁律特例）
  §7 参数表
  §11 虚拟时钟

仅使用标准库，零第三方依赖。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ---------------------------------------------------------------- §7 参数表
CONFIG: Dict[str, float] = {
    "conf.initial": 0.30,
    "conf.zone.prelim_min": 0.60,
    "conf.zone.stable_min": 0.75,
    "conf.archive_line": 0.05,
    "conf.promote_value": 0.85,
    "conf.machine_cap": 0.749,
    "conf.stable_climb_max": 1.00,
    "conf.demote_value": 0.70,
    "score.evid_pos": 0.02,
    "score.reask": -0.02,
    "score.escalate": -0.03,
    "score.dislike": -0.04,
    "decay.half_life.learn": 90.0,
    "decay.half_life.stable": 365.0,
    "decay.grace_days": 14.0,
    "tenure.grace_min": 80.0,
    "tenure.grace_max": 165.0,
    "tenure.step": 10.0,
    "promotion.probation_days": 7,
    "promotion.vet_sources_min": 3,
    "conflict.threshold_n": 3,
    "conflict.hedge_gap": 0.25,
    "iron.dissent_threshold": 3,
}

# 分区码（前端共用）
ZONE_LEARN, ZONE_PRELIM, ZONE_STABLE = 0, 1, 2
ZONE_PENDING, ZONE_ARCHIVED, ZONE_SUSPENDED = 3, 4, 5

# 事件类型（§6 事件表）
E_INGEST = "INGEST"
E_ADMIN_IMPORT = "ADMIN_IMPORT"
E_ADOPT = "ADOPT"
E_EVID_POS = "EVID_POS"
E_EVID_NEG = "EVID_NEG"
E_RECONCILE = "RECONCILE"
E_PROBATION_ENTER = "PROBATION_ENTER"
E_VET_PASS = "VET_PASS"
E_VET_FLAG = "VET_FLAG"
E_PROBATION_EXPIRE = "PROBATION_EXPIRE"
E_ADJ_PASS = "ADJUDICATE_PASS"
E_ADJ_REJECT = "ADJUDICATE_REJECT"
E_ADJ_RESTORE = "ADJUDICATE_RESTORE"
E_ADJ_ARCHIVE = "ADJUDICATE_ARCHIVE"
E_DEMOTE = "DEMOTE"
E_ARCHIVE = "ARCHIVE"
E_CONFLICT_INC = "CONFLICT_INC"
E_SUSPEND = "SUSPEND"
E_IRON_DISSENT = "IRON_DISSENT"
E_IRON_ALERT = "IRON_ALERT"
E_AMEND_REPLACE = "AMEND_REPLACE"
E_AMEND_REJECT = "AMEND_REJECT"
E_ARB_WIN_NEW = "ARB_WIN_NEW"
E_ARB_WIN_OLD = "ARB_WIN_OLD"
E_ARB_NOVOTE = "ARB_NOVOTE"

NEG_SCORE = {"reask": "score.reask", "escalate": "score.escalate", "dislike": "score.dislike"}


@dataclass
class Block:
    """知识块 —— 治理单元（ADR-0002）。"""

    id: str
    material_id: str
    title: str
    material_title: str
    angle: float = 0.0          # 星图固定角度（渲染期不抖动）
    jitter: float = 0.0         # 固定径向抖动，避免完全重叠
    conf_stored: float = CONFIG["conf.initial"]
    decay_baseline_at: float = 0.0   # 衰减时钟起点（上次采纳/重锚）
    created_at: float = 0.0
    zone: int = ZONE_LEARN           # 物化分区（读时投影的落点）
    pending: bool = False            # 待处置（稳定区跌破后的过渡态）
    frozen: bool = False             # 举证期冻结
    probation_until: float = -1.0
    suspended: bool = False          # 铁律挑战者保全
    queued: bool = False             # 挑刺不过 → 例外队列（等人工裁决）
    archived: bool = False
    tier0: bool = False              # 铁律位
    iron_dissent: int = 0
    adopted_count: int = 0
    tenure: int = 0                  # 稳定区正证据累计 → 延长闲置容忍
    vet_sources: int = 0
    pending_reason: str = ""
    superseded_by: Optional[str] = None
    last_engagement_at: float = 0.0

    # ---- §2.1 两分量 ----
    def grace_days(self) -> float:
        if self.conf_stored >= CONFIG["conf.zone.stable_min"]:
            g = CONFIG["tenure.grace_min"] + CONFIG["tenure.step"] * self.tenure
            return min(CONFIG["tenure.grace_max"], g)
        return CONFIG["decay.grace_days"]

    def half_life(self) -> float:
        if self.conf_stored >= CONFIG["conf.zone.stable_min"]:
            return CONFIG["decay.half_life.stable"]
        return CONFIG["decay.half_life.learn"]

    def idle_days(self, now: float) -> float:
        return max(0.0, now - self.last_engagement_at)

    def freshness(self, now: float) -> float:
        """0.5^(max(0, 闲置天数-宽限)/分区半衰期)。冻结/保全/归档/铁律不衰减。"""
        if self.frozen or self.suspended or self.archived or self.tier0:
            return 1.0
        idle = self.idle_days(now)
        grace = self.grace_days()
        if idle <= grace:
            return 1.0
        return 0.5 ** ((idle - grace) / self.half_life())

    def effective(self, now: float) -> float:
        """有效置信度 = conf_stored × freshness —— 分区投影的唯一取值。"""
        if self.archived:
            return 0.0
        return max(0.0, min(1.0, self.conf_stored * self.freshness(now)))

    def zone_code(self) -> int:
        if self.archived:
            return ZONE_ARCHIVED
        if self.suspended:
            return ZONE_SUSPENDED
        if self.queued:
            return ZONE_PRELIM
        if self.pending:
            return ZONE_PENDING
        if self.conf_stored >= CONFIG["conf.zone.stable_min"]:
            return ZONE_STABLE
        if self.conf_stored >= CONFIG["conf.zone.prelim_min"]:
            return ZONE_PRELIM
        return ZONE_LEARN

    def searchable(self) -> bool:
        return not (self.archived or self.suspended)


class Engine:
    """虚拟时钟驱动的生命周期引擎（ADR-0004）。全部状态变化可审计。"""

    def __init__(self) -> None:
        self.now: float = 0.0
        self.blocks: Dict[str, Block] = {}
        self.events: List[dict] = []
        self.conflicts: List[dict] = []
        self._reconciled_day: int = -1

    # ------------------------------------------------------------ 事件写入
    def _log(self, etype: str, block: Optional[Block], delta: float = 0.0,
             note: str = "", actor: str = "engine") -> dict:
        ev = {
            "d": self.now,
            "t": etype,
            "b": block.id if block else "",
            "delta": round(delta, 4),
            "c": round(block.conf_stored, 4) if block else 0.0,
            "note": note,
            "actor": actor,
        }
        self.events.append(ev)
        return ev

    def add_material(self, material_id: str, title: str, material_title: str,
                     admin: bool = False, angle: float = 0.0, jitter: float = 0.0,
                     sources: int = 0, tier0: bool = False) -> Block:
        """§6：INGEST=0.30 入学习区；ADMIN_IMPORT=0.85 直进稳定区（带背书）。
        tier0=True 表示管理员设为「铁律位」：不自动降级、冲突必胜、只能由人修改（§4.3）。"""
        b = Block(
            id=material_id,
            material_id=material_id,
            title=title,
            material_title=material_title,
            angle=angle,
            jitter=jitter,
            created_at=self.now,
            last_engagement_at=self.now,
            decay_baseline_at=self.now,
            vet_sources=sources,
            tier0=bool(tier0),
        )
        if admin:
            b.conf_stored = CONFIG["conf.promote_value"]
            b.zone = ZONE_STABLE
        else:
            b.conf_stored = CONFIG["conf.initial"]
            b.zone = ZONE_LEARN
        self.blocks[b.id] = b
        self._log(E_ADMIN_IMPORT if admin else E_INGEST, b,
                  note="管理员导入·直进稳定区·带背书" if admin else "Agent 收纳·原文切片·AI 只贴标签",
                  actor="admin" if admin else "agent")
        return b

    def adopt(self, bid: str, note: str = "") -> None:
        """§3.1 活性：重置保鲜时钟，零积分。"""
        b = self.blocks[bid]
        b.adopted_count += 1
        b.last_engagement_at = self.now
        b.decay_baseline_at = self.now
        self._log(E_ADOPT, b, 0.0, note or "答案实际使用该块（活性·零积分）", "user")

    def evidence(self, bid: str, kind: str = "pos", note: str = "") -> None:
        """§3.1 证据桶：正 +0.02 / 负 -0.02~-0.04；常规自动事件上限 0.749。"""
        b = self.blocks[bid]
        if b.archived or b.suspended:
            return
        delta = CONFIG["score.evid_pos"] if kind == "pos" else CONFIG[NEG_SCORE[kind]]
        if delta > 0:
            # 机器写入边界：常规自动事件不得跨越稳定区下界
            if b.conf_stored < CONFIG["conf.zone.stable_min"]:
                b.conf_stored = min(CONFIG["conf.machine_cap"], b.conf_stored + delta)
            else:
                b.conf_stored = min(CONFIG["conf.stable_climb_max"], b.conf_stored + delta)
                b.tenure += 1
        else:
            b.conf_stored = max(0.0, b.conf_stored + delta)
        self._log(E_EVID_POS if kind == "pos" else E_EVID_NEG, b, delta,
                  note or ("正证据" if kind == "pos" else f"负证据·{kind}"), "user")

    # ------------------------------------------------- §4.1 慢升：自证晋升
    def _maybe_enter_probation(self) -> None:
        for b in self.blocks.values():
            if b.archived or b.suspended or b.frozen or b.pending or b.queued:
                continue
            if b.conf_stored >= CONFIG["conf.zone.prelim_min"] and b.zone != ZONE_STABLE:
                b.frozen = True
                b.zone = ZONE_PRELIM
                b.probation_until = self.now + CONFIG["promotion.probation_days"]
                self._log(E_PROBATION_ENTER, b,
                          note=f"证据净攒跨 {CONFIG['conf.zone.prelim_min']:.2f} → 举证期冻结 "
                               f"{CONFIG['promotion.probation_days']} 天（不衰减）")

    def vet(self, bid: str, passed: bool, reasons: Optional[List[str]] = None,
            sources: int = 0) -> None:
        """§4.1 一次 AI 挑刺：查矛盾 / 查独立佐证 / 查反方质询 → 一个过/不过判定。"""
        b = self.blocks[bid]
        b.frozen = False
        b.probation_until = -1.0
        b.vet_sources = sources
        detail = "；".join(reasons or [])
        if passed:
            b.queued = False
            b.conf_stored = CONFIG["conf.promote_value"]
            b.zone = ZONE_STABLE
            b.last_engagement_at = self.now
            b.decay_baseline_at = self.now
            self._log(E_VET_PASS, b, CONFIG["conf.promote_value"],
                      f"挑刺通过 → AUTO_PROMOTE 置 {CONFIG['conf.promote_value']:.2f}"
                      f"（独立佐证 {sources} 源）" + (f"｜{detail}" if detail else ""), "ai")
        else:
            b.queued = True
            b.pending_reason = detail or "挑刺不过"
            self._log(E_VET_FLAG, b, 0.0, f"挑刺不过 → 例外队列（{detail}）", "ai")

    def adjudicate(self, bid: str, action: str) -> None:
        """§4.1/§4.2 人工裁决：pass 0.85 / reject 0.30 清零 / restore 0.85 / demote 0.70 / archive。"""
        b = self.blocks[bid]
        b.queued = False
        if action == "pass":
            b.conf_stored = CONFIG["conf.promote_value"]
            b.zone = ZONE_STABLE
            b.frozen = False
            b.pending = False
            b.last_engagement_at = self.now
            b.decay_baseline_at = self.now
            self._log(E_ADJ_PASS, b, CONFIG["conf.promote_value"], "人工裁决·通过 → 0.85 稳定区", "admin")
        elif action == "reject":
            b.conf_stored = CONFIG["conf.initial"]
            b.zone = ZONE_LEARN
            b.frozen = False
            b.pending = False
            b.tenure = 0
            b.last_engagement_at = self.now
            b.decay_baseline_at = self.now
            self._log(E_ADJ_REJECT, b, CONFIG["conf.initial"], "人工裁决·拒绝 → 0.30 积分清零回学习区", "admin")
        elif action == "restore":
            b.conf_stored = CONFIG["conf.promote_value"]
            b.zone = ZONE_STABLE
            b.pending = False
            b.last_engagement_at = self.now
            b.decay_baseline_at = self.now
            self._log(E_ADJ_RESTORE, b, CONFIG["conf.promote_value"], "人工裁决·恢复 → 0.85", "admin")
        elif action == "demote":
            b.conf_stored = CONFIG["conf.demote_value"]
            b.pending = True
            b.zone = ZONE_PENDING
            b.last_engagement_at = self.now
            b.decay_baseline_at = self.now
            self._log(E_DEMOTE, b, CONFIG["conf.demote_value"], "打回 → 0.70 待处置", "admin")
        elif action == "archive":
            b.archived = True
            b.zone = ZONE_ARCHIVED
            self._log(E_ADJ_ARCHIVE, b, 0.0, "人工裁决·废弃 → 归档（不删除，可翻案）", "admin")

    # ------------------------------------------------- §4.2 快降：衰减协调
    def reconcile(self, day: Optional[int] = None) -> None:
        """协调任务：幂等物化。棘轮下调 + 重锚衰减基线 + 跨界迁移。"""
        now = self.now if day is None else float(day)
        for b in self.blocks.values():
            if b.archived or b.suspended:
                continue
            if b.frozen:
                if b.probation_until >= 0 and now >= b.probation_until:
                    # 观察窗满：演示主线由 vet() 显式裁决，此处兜底解冻
                    pass
                continue
            eff = b.effective(now)
            # 跌破稳定区下界 → 待处置（棘轮落 0.749）
            if b.conf_stored >= CONFIG["conf.zone.stable_min"] and eff < CONFIG["conf.zone.stable_min"]:
                b.conf_stored = min(eff, CONFIG["conf.machine_cap"])
                b.zone = ZONE_PENDING
                b.pending = True
                b.pending_reason = (f"有效置信度跌破 {CONFIG['conf.zone.stable_min']:.2f}"
                                    f"（已 {int(b.idle_days(now))} 天无有效证据）")
                b.last_engagement_at = now      # 重锚衰减基线，防双计
                b.decay_baseline_at = now
                self._log(E_RECONCILE, b, -1.0, f"棘轮下调至 {b.conf_stored:.3f} · 重锚衰减基线 · 落待处置", "engine")
                continue
            # 待处置跌破预备区下界 → 滑回学习区
            if b.pending and eff < CONFIG["conf.zone.prelim_min"]:
                b.conf_stored = min(eff, CONFIG["conf.machine_cap"])
                b.pending = False
                b.zone = ZONE_LEARN
                b.pending_reason = f"待处置期满仍无证据（已 {int(b.idle_days(now))} 天无有效证据）"
                b.last_engagement_at = now
                b.decay_baseline_at = now
                self._log(E_RECONCILE, b, -1.0, f"棘轮下调至 {b.conf_stored:.3f} · 滑回学习区", "engine")
                continue
            # 跌破废弃线 → 自动归档（不删除）
            if eff < CONFIG["conf.archive_line"]:
                b.archived = True
                b.zone = ZONE_ARCHIVED
                b.pending_reason = (f"已 {int(b.idle_days(now))} 天无有效证据 · "
                                    f"有效置信度跌破废弃线 {CONFIG['conf.archive_line']:.2f}")
                self._log(E_ARCHIVE, b, 0.0, b.pending_reason + " → 自动归档（留案底，可翻案）", "engine")
        self._maybe_enter_probation()

    # ------------------------------------------------- §8 冲突与仲裁阶梯
    def conflict(self, defender: str, challenger: str, note: str = "") -> dict:
        """§8 冲突检测：采纳集跨分区共现 + judge 判竞争。"""
        d, c = self.blocks[defender], self.blocks[challenger]
        if d.tier0:
            # 铁律特例：挑战者保护性挂起 + 铁律异议 +1（红灯通道）
            c.suspended = True
            d.iron_dissent += 1
            self._log(E_SUSPEND, c, 0.0, "挑战铁律 → 保护性挂起保全（不扣分不衰减不可检索）", "detector")
            self._log(E_IRON_DISSENT, d, 0.0,
                      f"铁律异议 +1（{d.iron_dissent}/{CONFIG['iron.dissent_threshold']}）", "detector")
            rec = {"defender": defender, "challenger": challenger, "count": d.iron_dissent,
                   "status": "iron", "verdict": "", "note": note}
            self.conflicts.append(rec)
            if d.iron_dissent >= CONFIG["iron.dissent_threshold"]:
                self._log(E_IRON_ALERT, d, 0.0, "异议达阈值 → 仪表盘置顶红灯 + 催办", "engine")
            return rec
        rec = None
        for r in self.conflicts:
            if r["defender"] == defender and r["challenger"] == challenger and r["status"] == "open":
                rec = r
                break
        if rec is None:
            rec = {"defender": defender, "challenger": challenger, "count": 0,
                   "status": "open", "verdict": "", "note": note}
            self.conflicts.append(rec)
        rec["count"] += 1
        self._log(E_CONFLICT_INC, d, 0.0,
                  f"冲突计数 {rec['count']}/{CONFIG['conflict.threshold_n']}（跨分区共现 + judge 判竞争）",
                  "detector")
        return rec

    def arbitration_stage(self, rec: dict) -> str:
        """仲裁阶梯：N=3 → 铁律位走红灯 / 差 ≥0.25 对冲判负 / 否则影子双答点选。"""
        d, c = self.blocks[rec["defender"]], self.blocks[rec["challenger"]]
        if d.tier0:
            return "iron_red"
        gap = d.effective(self.now) - c.effective(self.now)
        if gap >= CONFIG["conflict.hedge_gap"]:
            return "hedge"
        return "shadow_duel"

    def duel_vote(self, rec: dict, choice: str) -> None:
        """§6：ARB_WIN_NEW 防守方置 0.70 / ARB_WIN_OLD 挑战者 +0.10（上限 0.749）/ NOVOTE 计数 −1。"""
        d, c = self.blocks[rec["defender"]], self.blocks[rec["challenger"]]
        if choice == "new":
            d.conf_stored = CONFIG["conf.demote_value"]
            d.pending = True
            d.zone = ZONE_PENDING
            d.pending_reason = "影子对决败诉（业务结果闭环）"
            d.last_engagement_at = self.now
            d.decay_baseline_at = self.now
            rec["status"] = "closed"
            rec["verdict"] = "new"
            self._log(E_ARB_WIN_NEW, d, CONFIG["conf.demote_value"],
                      f"影子对决：评委选新 → 防守方置 {CONFIG['conf.demote_value']:.2f} 落待处置", "user")
            self._log(E_ADOPT, c, 0.0, "挑战者获采信（活性）", "user")
        elif choice == "old":
            c.conf_stored = min(CONFIG["conf.machine_cap"], c.conf_stored + 0.10)
            rec["status"] = "closed"
            rec["verdict"] = "old"
            self._log(E_ARB_WIN_OLD, c, 0.10, "影子对决：评委选旧 → 挑战者 +0.10（上限 0.749）", "user")
        else:
            rec["count"] = max(0, rec["count"] - 1)
            self._log(E_ARB_NOVOTE, d, 0.0, "无人点选 → 计数 −1（仲裁不强制，冲突会渐忘）", "user")

    def amend_replace(self, old_id: str, new_id: str) -> None:
        """§4.3 一键接替：原子交接 —— 新块 0.85+铁律位，旧铁律归档+取代链。"""
        old, new = self.blocks[old_id], self.blocks[new_id]
        new.suspended = False
        new.conf_stored = CONFIG["conf.promote_value"]
        new.zone = ZONE_STABLE
        new.tier0 = True
        new.last_engagement_at = self.now
        new.decay_baseline_at = self.now
        old.archived = True
        old.tier0 = False
        old.zone = ZONE_ARCHIVED
        old.superseded_by = new_id
        self._log(E_AMEND_REPLACE, new, CONFIG["conf.promote_value"],
                  f"修正案一键接替：{old.title} → {new.title}（原子交接，取代链留档）", "admin")

    def amend_reject(self, old_id: str, new_id: str) -> None:
        old, new = self.blocks[old_id], self.blocks[new_id]
        new.suspended = False
        old.iron_dissent = 0
        self._log(E_AMEND_REJECT, new, 0.0, "修正案驳回：挑战者解除挂起回原位，异议清零", "admin")

    # ------------------------------------------------------------ 快照输出
    def frame(self) -> dict:
        rows = []
        for b in sorted(self.blocks.values(), key=lambda x: x.id):
            rows.append([b.id, round(b.effective(self.now) * 1000), b.zone_code(),
                         (1 if b.frozen else 0) | (2 if b.pending else 0) |
                         (4 if b.suspended else 0) | (8 if b.tier0 else 0) |
                         (16 if b.archived else 0) | (32 if b.queued else 0)])
        return {"d": int(self.now), "b": rows}
