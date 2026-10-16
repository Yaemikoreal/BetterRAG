# -*- coding: utf-8 -*-
"""
BetterRAG 每日定时衰减协调任务 (Daily Cron Reconcile)
严格实现 docs/BetterRAG生产级开发实施方案.md §4.6 与三段式完整降级链
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Dict, Any, List

from src.engine.config import (
    CONF_ZONE_STABLE_MIN,
    CONF_ZONE_PRELIM_MIN,
    CONF_ARCHIVE_LINE,
    CONF_DEMOTE_PENDING,
    CONF_DEMOTE_LEARN,
)
from src.engine.models import Block, utcnow_str
from src.engine.storage import Storage
from src.engine.confidence import (
    calc_effective_conf,
    calc_idle_days,
    parse_datetime,
)


class DailyReconciler:
    """真实时间每日衰减协调器"""

    def __init__(self, storage: Storage):
        self.storage = storage

    def reconcile(self, now: Optional[str | datetime] = None) -> Dict[str, Any]:
        """
        执行单次幂等衰减协调任务
        - 每日 03:00 定时触发或单元测试/运维接口显式调用
        - 支持传入特定时间戳模拟时光流逝
        """
        if now is None:
            now_dt = datetime.utcnow()
        else:
            now_dt = parse_datetime(now)
        now_str = now_dt.isoformat()

        active_blocks = self.storage.get_all_active_blocks()

        stats = {
            "scanned": len(active_blocks),
            "skipped_evergreen": 0,
            "skipped_frozen": 0,
            "demoted_pending": 0,
            "demoted_learn": 0,
            "archived": 0,
            "children_suspended": 0,
            "unchanged": 0,
        }

        suspended_ids = set()

        for b in active_blocks:
            # 如果该块在此次批处理中已被其父块级联挂起，跳过衰减
            if b.id in suspended_ids:
                continue

            # 检查其依附的父级前置条件块是否已退场
            if b.parent_scope_id:
                parent_b = self.storage.get_block(b.parent_scope_id)
                if parent_b and parent_b.status in ("archived", "suspended"):
                    b.status = "suspended"
                    b.pending_reason = f"前置条件父块 {parent_b.id} 已退场，级联挂起保全"
                    self.storage.save_block(b)
                    suspended_ids.add(b.id)
                    stats["children_suspended"] += 1
                    continue

            # 1. 常青知识保护 (Evergreen) / 举证期冻结：免除时间衰减
            if b.is_evergreen:
                stats["skipped_evergreen"] += 1
                continue
            if b.is_frozen(now_dt):
                stats["skipped_frozen"] += 1
                continue

            idle_days = calc_idle_days(b.last_engagement_at, now_dt)
            eff_conf = calc_effective_conf(b, now_dt)

            changed = False

            # 2. 完整三段式降级链与棘轮重锚
            # 第一段：跌破稳定区下界 (0.75) -> 待处置 (落 0.749，重锚基线)
            if b.conf_stored >= CONF_ZONE_STABLE_MIN and eff_conf < CONF_ZONE_STABLE_MIN:
                b.conf_stored = min(eff_conf, CONF_DEMOTE_PENDING)
                b.status = "pending"
                b.pending_reason = f"闲置 {int(idle_days)} 天有效置信度跌破 0.75，降入待处置"
                b.last_engagement_at = now_str  # 重锚基线防重复双计
                b.decay_baseline_at = now_str
                self.storage.log_event(
                    block_id=b.id,
                    material_id=b.material_id,
                    event_type="DEMOTE_PENDING",
                    actor="engine",
                    delta=-1.0,
                    confidence_after=b.conf_stored,
                    note=f"闲置 {int(idle_days)} 天自动降入待处置，棘轮下调至 {b.conf_stored:.4f}"
                )
                stats["demoted_pending"] += 1
                changed = True

            # 第二段：待处置且闲置跌破预备区下界 (0.60) -> 滑回学习区
            elif b.status == "pending" and eff_conf < CONF_ZONE_PRELIM_MIN:
                b.conf_stored = min(eff_conf, CONF_DEMOTE_LEARN)
                b.status = "active"             # 退回普通学习区
                b.pending_reason = f"待处置期满无证据，滑回学习区"
                b.last_engagement_at = now_str  # 重锚基线防重复双计
                b.decay_baseline_at = now_str
                self.storage.log_event(
                    block_id=b.id,
                    material_id=b.material_id,
                    event_type="DEMOTE_LEARN",
                    actor="engine",
                    delta=-1.0,
                    confidence_after=b.conf_stored,
                    note=f"待处置期满无有效证据，滑回学习区，棘轮下调至 {b.conf_stored:.4f}"
                )
                stats["demoted_learn"] += 1
                changed = True

            # 第三段：跌破废弃线 (0.05) -> 自动归档 (不可检，保全审计)
            elif eff_conf < CONF_ARCHIVE_LINE:
                b.status = "archived"
                b.pending_reason = f"闲置 {int(idle_days)} 天跌破 0.05 废弃线，自动归档"
                self.storage.log_event(
                    block_id=b.id,
                    material_id=b.material_id,
                    event_type="ARCHIVE",
                    actor="engine",
                    delta=0.0,
                    confidence_after=0.0,
                    note=f"闲置 {int(idle_days)} 天有效置信度跌破废弃线 0.05，自动归档"
                )
                stats["archived"] += 1
                changed = True

                # 级联挂起孤儿子块，防止前置丢失断章取义
                newly_suspended = self.storage.suspend_child_blocks(
                    b.id, reason=f"前置条件父块 {b.id} 已归档，级联挂起保全"
                )
                suspended_ids.update(newly_suspended)
                stats["children_suspended"] += len(newly_suspended)

            if changed:
                self.storage.save_block(b)
            else:
                stats["unchanged"] += 1

        return stats
