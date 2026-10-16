# -*- coding: utf-8 -*-
"""
BetterRAG 三轨信号源与显著性加权归因结算引擎 (Evidence Pipeline)
严格实现 docs/BetterRAG生产级开发实施方案.md §4.3
"""
from __future__ import annotations

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta
from difflib import SequenceMatcher
import jieba

from src.engine.config import (
    SCORE_MAP,
    CONF_ZONE_PRELIM_MIN,
    CONF_ZONE_STABLE_MIN,
    CONF_MACHINE_CAP,
    CONF_STABLE_MAX,
    PROBATION_DAYS,
    DEFAULT_EMBEDDING_MODEL,
)
from src.engine.models import Block, Citation, utcnow_str
from src.engine.storage import Storage
from src.engine.confidence import parse_datetime

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


class CreditEngine:
    """证据积分归因与会话活性管理引擎"""

    def __init__(self, storage: Optional[Storage] = None, embedding_model: Optional[Any] = None):
        self.storage = storage
        self.embedding_model = embedding_model
        if self.embedding_model is None and HAS_FASTEMBED:
            try:
                self.embedding_model = TextEmbedding(DEFAULT_EMBEDDING_MODEL)
            except Exception as e:
                logger.debug("FastEmbed 初始化跳过: %s", e)
                self.embedding_model = None

    def compute_query_similarity(self, q1: str, q2: str) -> float:
        """
        计算两次提问的语义相似度（用于 60s 隐式重问检测）
        - 优先使用 BGE 向量余弦相似度（捕捉同义句、重组句）
        - 降级支持词袋 Jaccard 与字符编辑距离
        """
        clean1 = q1.strip()
        clean2 = q2.strip()
        if not clean1 or not clean2:
            return 0.0

        if self.embedding_model and HAS_NUMPY:
            try:
                vectors = list(self.embedding_model.embed([clean1, clean2]))
                v1, v2 = vectors[0], vectors[1]
                norm1 = np.linalg.norm(v1)
                norm2 = np.linalg.norm(v2)
                if norm1 > 0 and norm2 > 0:
                    cos = float(np.dot(v1, v2) / (norm1 * norm2))
                    return max(0.0, min(1.0, cos))
            except Exception as e:
                logger.debug("向量相似度计算回退: %s", e)

        # 降级纯文本计算
        w1 = set(jieba.cut(clean1))
        w2 = set(jieba.cut(clean2))
        jaccard = len(w1 & w2) / max(len(w1 | w2), 1)
        seq = SequenceMatcher(None, clean1, clean2).ratio()
        return max(jaccard, seq)

    def adopt_citations(self, session_id: str, note: str = "") -> None:
        """
        会话活性保鲜：重置保鲜时钟，零积分（ADR-0001 §3.1）
        """
        citations = self.storage.get_citations_by_session(session_id)
        now_str = utcnow_str()

        for cite in citations:
            block = self.storage.get_block(cite.block_id)
            if not block or block.status in ("archived", "suspended"):
                continue

            block.last_engagement_at = now_str
            block.decay_baseline_at = now_str
            self.storage.save_block(block)
            self.storage.log_event(
                block_id=block.id,
                material_id=block.material_id,
                event_type="ADOPT",
                actor="user",
                delta=0.0,
                confidence_after=block.conf_stored,
                note=note or "问答采纳·活性保鲜·零积分"
            )

    def process_feedback(
        self,
        session_id: str,
        outcome: str,
        note: str = "",
        now: Optional[str | datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        显著性加权证据归因结算算法（§4.3.2）
        - 主干引用承担 80%，背景参考承担 20%
        - 机器写入边界：常规正反馈不能跨过 0.749（稳定区下界之下）
        - 稳定区内正反馈提升置信度并累加 tenure
        """
        citations = self.storage.get_citations_by_session(session_id)
        base_delta = SCORE_MAP.get(outcome, 0.0)
        now_str = parse_datetime(now).isoformat() if now else utcnow_str()

        audit_results = []

        for cite in citations:
            block = self.storage.get_block(cite.block_id)
            if not block or block.status in ("archived", "suspended"):
                continue

            # 显着性加权归因
            actual_delta = base_delta * cite.salience_weight

            # 机器写入边界与 tenure 结算
            if actual_delta > 0:
                if block.conf_stored < CONF_ZONE_STABLE_MIN:
                    block.conf_stored = min(CONF_MACHINE_CAP, block.conf_stored + actual_delta)
                    # 达到 0.60 且未在举证期，进入 7 天举证冻结期 (§4.4)
                    if block.conf_stored >= CONF_ZONE_PRELIM_MIN and not block.probation_until:
                        now_dt = parse_datetime(now_str)
                        until_dt = now_dt + timedelta(days=PROBATION_DAYS)
                        block.probation_until = until_dt.isoformat()
                        self.storage.log_event(
                            block_id=block.id,
                            material_id=block.material_id,
                            event_type="PROBATION_START",
                            actor="engine",
                            delta=0.0,
                            confidence_after=block.conf_stored,
                            note=f"置信度达标({block.conf_stored:.2f})，进入7天举证冻结期"
                        )
                else:
                    block.conf_stored = min(CONF_STABLE_MAX, block.conf_stored + actual_delta)
                    block.tenure += 1
            else:
                block.conf_stored = max(0.0, block.conf_stored + actual_delta)
                # 出现负反馈：若处于举证期，立即退出并退回普通学习区 (§4.4)
                if block.probation_until:
                    block.probation_until = None
                    self.storage.log_event(
                        block_id=block.id,
                        material_id=block.material_id,
                        event_type="PROBATION_EXIT",
                        actor="engine",
                        delta=0.0,
                        confidence_after=block.conf_stored,
                        note="举证期内出现负反馈，解除举证期并退回普通学习区"
                    )

            block.last_engagement_at = now_str
            block.decay_baseline_at = now_str
            self.storage.save_block(block)

            # 更新 citation 反馈记录
            cite.outcome_feedback = outcome
            self.storage.save_citation(cite)

            self.storage.log_event(
                block_id=block.id,
                material_id=block.material_id,
                event_type="EVID_OUTCOME",
                actor="user",
                delta=actual_delta,
                confidence_after=block.conf_stored,
                note=note or f"反馈归因·{outcome}·显着性{cite.salience_weight:.2f}"
            )

            audit_results.append({
                "block_id": block.id,
                "delta": actual_delta,
                "confidence_after": block.conf_stored,
                "tenure": block.tenure,
            })

        return audit_results

    def detect_and_handle_reask(
        self,
        session_id: str,
        current_question: str,
        now: Optional[str | datetime] = None,
    ) -> bool:
        """
        轨 3 隐式负反馈检测：
        60 秒内同 Session 出现相似度 >= 0.85 的同题重问，自动将前次回答标记为 neg_reask 并扣分 (-0.02)
        """
        citations = self.storage.get_citations_by_session(session_id)
        if not citations:
            return False

        now_dt = parse_datetime(now) if now else datetime.utcnow()

        # 取前一次提问的问题内容与时间
        last_cite = citations[-1]
        last_question = last_cite.question
        last_time_dt = parse_datetime(last_cite.created_at)

        time_diff_sec = (now_dt - last_time_dt).total_seconds()
        if 0.0 <= time_diff_sec <= 60.0:
            sim = self.compute_query_similarity(current_question, last_question)
            if sim >= 0.85:
                # 触发隐式负反馈
                self.process_feedback(
                    session_id=session_id,
                    outcome="neg_reask",
                    note=f"轨3隐式负反馈·{int(time_diff_sec)}秒内同题重问(相似度{sim:.2f})"
                )
                return True
        return False


_default_credit_engine: Optional[CreditEngine] = None


def compute_query_similarity(q1: str, q2: str) -> float:
    """模块级辅助函数：计算两句语义相似度"""
    global _default_credit_engine
    if _default_credit_engine is None:
        _default_credit_engine = CreditEngine(storage=None)
    return _default_credit_engine.compute_query_similarity(q1, q2)
