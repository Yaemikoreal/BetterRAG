# -*- coding: utf-8 -*-
"""
BetterRAG 两级冲突检测流水线 (Silent Shadow Pipeline)
严格实现 docs/BetterRAG生产级开发实施方案.md §4.5
- Pipe-1: 摄取期静态冲突粗筛与确诊
- Pipe-2: 检索期动态粗筛、判决缓存复用与 clash_count 计数
- 影子回答快照生成与红灯告警判定
"""
from __future__ import annotations

import logging
from typing import List, Tuple, Optional, Callable, Dict, Any
from datetime import datetime

from src.engine.config import (
    CONF_ZONE_STABLE_MIN,
    CONFLICT_COARSE_SIM,
    CONFLICT_THRESHOLD_N,
    DEFAULT_EMBEDDING_MODEL,
)
from src.engine.models import Block, ConflictRecord, utcnow_str
from src.engine.storage import Storage
from src.engine.arbitrate import ConflictManager
from src.engine.retrieve import text_overlap_similarity as compute_text_similarity

logger = logging.getLogger(__name__)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

try:
    from fastembed import TextEmbedding
    HAS_FASTEMBED = True
except ImportError:
    HAS_FASTEMBED = False


def default_llm_or_heuristic_judge(
    defender: Block,
    challenger: Block
) -> Tuple[str, str]:
    """
    冲突仲裁判定器：
    - 输出 ('conflict', '冲突理由') 或 ('compatible', '兼容互补说明')
    - 针对常见规章的数值调整或排他性条款做启发式识别
    """
    import re
    # 若两者内容完全一致，显然兼容
    if defender.content_hash == challenger.content_hash:
        return "compatible", "内容完全一致，兼容"

    # 提取关键数值与动作词
    def_nums = set(re.findall(r"\d+", defender.content))
    chal_nums = set(re.findall(r"\d+", challenger.content))

    contradiction_keywords = ["调整为", "修改为", "不再", "变更为", "取消", "执行新标准", "废止"]
    has_contra_word = any(kw in challenger.content for kw in contradiction_keywords)

    if has_contra_word and def_nums != chal_nums:
        return "conflict", f"新规调整了制度标准/数值，与旧规存在场景排他性互斥 (旧:{def_nums} vs 新:{chal_nums})"

    if "不得" in challenger.content and "可以" in defender.content:
        return "conflict", "新规禁止了旧规允许的行为，存在逻辑冲突"

    if "可以" in challenger.content and "不得" in defender.content:
        return "conflict", "新规放开了旧规禁止的行为，存在逻辑冲突"

    return "compatible", "条款互为补充或适用不同细分场景，判定兼容"


class ConflictPipeline:
    """两级冲突检测流水线"""

    def __init__(
        self,
        storage: Storage,
        conflict_manager: Optional[ConflictManager] = None,
        embedding_model: Optional[Any] = None,
    ):
        self.storage = storage
        self.conflict_manager = conflict_manager or ConflictManager(storage)
        self.embedding_model = embedding_model
        if self.embedding_model is None and HAS_FASTEMBED:
            try:
                self.embedding_model = TextEmbedding(DEFAULT_EMBEDDING_MODEL)
            except Exception:
                self.embedding_model = None

    def compute_block_similarity(self, b1: Block, b2: Block) -> float:
        """计算两个知识块的语义相似度（优先向量余弦，降级混合文本）"""
        if self.embedding_model and HAS_NUMPY:
            try:
                vectors = list(self.embedding_model.embed([b1.content, b2.content]))
                v1, v2 = vectors[0], vectors[1]
                n1, n2 = np.linalg.norm(v1), np.linalg.norm(v2)
                if n1 > 0 and n2 > 0:
                    cos = float(np.dot(v1, v2) / (n1 * n2))
                    return max(0.0, min(1.0, cos))
            except Exception:
                pass
        return compute_text_similarity(b1.content, b2.content)

    def pipe1_static_ingest_scan(
        self,
        new_blocks: List[Block],
        judge_fn: Optional[Callable[[Block, Block], Tuple[str, str]]] = None,
        sim_threshold: float = 0.80,
    ) -> List[ConflictRecord]:
        """
        Pipe-1: 摄取期静态冲突检测
        新块入库时，检索与之高语义相似度（>= 0.80）的跨文档稳定区块，调用 Judge 判定互斥。
        """
        judge = judge_fn or default_llm_or_heuristic_judge
        active_blocks = self.storage.get_all_active_blocks()
        stable_blocks = [
            b for b in active_blocks
            if b.conf_stored >= CONF_ZONE_STABLE_MIN and b.status == "active"
        ]

        detected_conflicts: List[ConflictRecord] = []
        new_ids = {b.id for b in new_blocks}

        for new_b in new_blocks:
            # 防御性保证 new_b 已在库中以满足外键约束
            if self.storage.get_block(new_b.id) is None:
                self.storage.save_block(new_b)

            for old_b in stable_blocks:
                # 排除同一文档内的相邻块与自身
                if old_b.id in new_ids or old_b.material_id == new_b.material_id:
                    continue

                sim = self.compute_block_similarity(new_b, old_b)
                if sim >= sim_threshold:
                    # 触发仲裁检查
                    rec = self.conflict_manager.check_or_record_conflict(
                        defender=old_b,
                        challenger=new_b,
                        judge_fn=judge,
                    )
                    if rec.verdict == "conflict":
                        detected_conflicts.append(rec)

        return detected_conflicts

    def pipe2_retrieval_dynamic_check(
        self,
        hit_blocks: List[Block],
        query: str = "",
        judge_fn: Optional[Callable[[Block, Block], Tuple[str, str]]] = None,
        sim_threshold: Optional[float] = None,
    ) -> List[ConflictRecord]:
        """
        Pipe-2: 检索期动态冲突判定
        在单次 Top-K 召回块中，若同时命中【稳定区块 B_old】与【学习区/预备区块 B_new】，
        且两者语义相似度 >= CONFLICT_COARSE_SIM (0.82)，触发缓存校验与计数。
        """
        judge = judge_fn or default_llm_or_heuristic_judge
        threshold = sim_threshold if sim_threshold is not None else CONFLICT_COARSE_SIM

        # 分流为稳定区旧块与学习区新块
        stable_hits = [b for b in hit_blocks if b.conf_stored >= CONF_ZONE_STABLE_MIN]
        candidate_hits = [b for b in hit_blocks if b.conf_stored < CONF_ZONE_STABLE_MIN]

        detected_conflicts: List[ConflictRecord] = []

        for defender in stable_hits:
            for challenger in candidate_hits:
                if defender.material_id == challenger.material_id:
                    continue

                sim = self.compute_block_similarity(defender, challenger)
                if sim >= threshold:
                    rec = self.conflict_manager.check_or_record_conflict(
                        defender=defender,
                        challenger=challenger,
                        judge_fn=judge,
                    )
                    # 如果生成了影子回答，记录快照
                    if query and rec.verdict == "conflict" and not rec.shadow_answer_new:
                        rec.shadow_answer_old = f"[旧规依据·{defender.context_prefix}]: {defender.content[:150]}"
                        rec.shadow_answer_new = f"[新规依据·{challenger.context_prefix}]: {challenger.content[:150]}"
                        self.storage.save_conflict(rec)

                    detected_conflicts.append(rec)

        return detected_conflicts

    def get_red_alert_conflicts(self) -> List[ConflictRecord]:
        """获取已达到推送红灯阈值（clash_count >= 3 且待处理）的冲突清单"""
        all_conflicts = self.storage.get_all_conflicts(verdict="conflict")
        return [
            c for c in all_conflicts
            if c.clash_count >= CONFLICT_THRESHOLD_N and c.status in ("open", "notified")
        ]
