# -*- coding: utf-8 -*-
"""
BetterRAG 两级冲突检测器与判决缓存引擎 (Silent Shadow Pipeline)
严格实现 docs/BetterRAG生产级开发实施方案.md §4.5 与 Agent讨论 #003/#004
"""
from __future__ import annotations

import hashlib
from typing import Optional, Callable, Tuple, Dict, Any
from datetime import datetime

from src.engine.config import (
    CONFLICT_THRESHOLD_N,
    CONF_PROMOTE_VALUE,
    CONF_DEMOTE_PENDING,
)
from src.engine.models import Block, ConflictRecord, utcnow_str
from src.engine.storage import Storage


class ConflictManager:
    """冲突仲裁与全量判决缓存管理器"""

    def __init__(self, storage: Storage):
        self.storage = storage

    def check_or_record_conflict(
        self,
        defender: Block,
        challenger: Block,
        judge_fn: Optional[Callable[[Block, Block], Tuple[str, str]]] = None,
    ) -> ConflictRecord:
        """
        核心两级冲突判决与缓存复用逻辑：
        1. 先查缓存：若 (defender, challenger) 存在且双方 Hash 未变：
           - verdict = 'compatible': 直接复用历史判决，零 LLM 开销，不累加 clash_count
           - verdict = 'conflict': 直接复用历史结论，仅累加 clash_count += 1
        2. 未命中缓存或 Hash 失效：调用 judge_fn 进行判定，并将结果全量落库缓存
        """
        existing = self.storage.get_conflict(defender.id, challenger.id)

        # 检查缓存是否有效 (双方 content_hash 均未变更)
        if existing is not None:
            hashes_match = (
                existing.defender_hash == defender.content_hash
                and existing.challenger_hash == challenger.content_hash
            )
            if hashes_match:
                if existing.verdict == "compatible":
                    # 历史判定为兼容：直接复用，零 LLM 调用，不增加计数
                    return existing
                elif existing.verdict == "conflict":
                    # 历史判定为冲突：累加计数器并更新时间戳
                    existing.clash_count += 1
                    existing.updated_at = utcnow_str()
                    if existing.clash_count >= CONFLICT_THRESHOLD_N and existing.status == "open":
                        existing.status = "notified"
                    self.storage.save_conflict(existing)
                    self.storage.log_event(
                        block_id=defender.id,
                        material_id=defender.material_id,
                        event_type="CONFLICT_INC",
                        actor="detector",
                        delta=0.0,
                        confidence_after=defender.conf_stored,
                        note=f"互斥冲突计数累加至 {existing.clash_count}/{CONFLICT_THRESHOLD_N}"
                    )
                    return existing

        # 缓存未命中或 Hash 发生变动：触发 Judge 判定
        if judge_fn is not None:
            verdict, note = judge_fn(defender, challenger)
        else:
            # 默认规则：若没有传入 judge_fn，默认按 compatible 处理
            verdict, note = "compatible", "默认兼容"

        verdict = verdict.lower()
        if verdict not in ("conflict", "compatible"):
            verdict = "conflict"

        conflict_id = f"conf_{defender.id}_{challenger.id}_{hashlib.md5(f'{defender.id}:{challenger.id}'.encode()).hexdigest()[:6]}"
        rec = ConflictRecord(
            id=conflict_id,
            defender_id=defender.id,
            challenger_id=challenger.id,
            defender_hash=defender.content_hash,
            challenger_hash=challenger.content_hash,
            clash_count=1,
            verdict=verdict,
            status="open",
            shadow_answer_old=note,
            created_at=utcnow_str(),
            updated_at=utcnow_str(),
        )
        self.storage.save_conflict(rec)
        self.storage.log_event(
            block_id=defender.id,
            material_id=defender.material_id,
            event_type="CONFLICT_JUDGE",
            actor="judge",
            delta=0.0,
            confidence_after=defender.conf_stored,
            note=f"首次冲突判定落库: verdict={verdict}, note={note}"
        )
        return rec

    def amend_replace(self, conflict_id: str) -> bool:
        """
        制度修正案一键接替 (原子交接)
        - 挑战者升至 0.85 稳定区
        - 旧防守者自动归档并建立 superseded_by 取代链
        """
        all_conflicts = self.storage.get_all_conflicts()
        target: Optional[ConflictRecord] = None
        for c in all_conflicts:
            if c.id == conflict_id:
                target = c
                break

        if not target:
            return False

        defender = self.storage.get_block(target.defender_id)
        challenger = self.storage.get_block(target.challenger_id)
        if not defender or not challenger:
            return False

        # 1. 挑战者晋升至 0.85 稳定区
        challenger.conf_stored = CONF_PROMOTE_VALUE
        challenger.status = "active"
        challenger.last_engagement_at = utcnow_str()
        challenger.decay_baseline_at = utcnow_str()
        self.storage.save_block(challenger)

        # 2. 防守者归档并标记被取代
        defender.status = "archived"
        defender.superseded_by = challenger.id
        defender.pending_reason = f"制度修正案接替，被 {challenger.id} 取代"
        self.storage.save_block(defender)

        # 3. 更新冲突记录状态
        target.status = "resolved"
        target.updated_at = utcnow_str()
        self.storage.save_conflict(target)

        self.storage.log_event(
            block_id=challenger.id,
            material_id=challenger.material_id,
            event_type="AMEND_REPLACE",
            actor="admin",
            delta=CONF_PROMOTE_VALUE,
            confidence_after=CONF_PROMOTE_VALUE,
            note=f"修正案一键接替：{defender.id} -> {challenger.id} 原子交接"
        )
        return True
