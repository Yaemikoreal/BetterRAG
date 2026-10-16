# -*- coding: utf-8 -*-
"""
BetterRAG 自适应混合检索与父子上下文组装管线 (Retrieval Pipeline)
严格实现 docs/BetterRAG生产级开发实施方案.md §4.2 与 ADR-0002
"""
from __future__ import annotations

import re
import math
from typing import List, Dict, Tuple, Optional, Any, Set
from datetime import datetime
from difflib import SequenceMatcher
import jieba

from src.engine.config import (
    CONF_ZONE_STABLE_MIN,
    CONF_ZONE_PRELIM_MIN,
    DEFAULT_EMBEDDING_MODEL,
)
from src.engine.models import Block, Material
from src.engine.storage import Storage
from src.engine.confidence import calc_effective_conf, parse_datetime

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


def cosine_similarity(v1: bytes | list, v2: bytes | list) -> float:
    """计算两个 float32 向量的余弦相似度"""
    if isinstance(v1, bytes):
        arr1 = np.frombuffer(v1, dtype=np.float32)
    else:
        arr1 = np.array(v1, dtype=np.float32)

    if isinstance(v2, bytes):
        arr2 = np.frombuffer(v2, dtype=np.float32)
    else:
        arr2 = np.array(v2, dtype=np.float32)

    norm1 = np.linalg.norm(arr1)
    norm2 = np.linalg.norm(arr2)
    if norm1 == 0 or norm2 == 0:
        return 0.0
    dot = float(np.dot(arr1, arr2))
    return float(max(0.0, min(1.0, dot / (norm1 * norm2))))


def text_overlap_similarity(s1: str, s2: str) -> float:
    """纯文本字符与词级重合度（快速回退机制）"""
    w1 = set(jieba.cut(s1))
    w2 = set(jieba.cut(s2))
    if not w1 or not w2:
        return 0.0
    jaccard = len(w1 & w2) / len(w1 | w2)
    seq = SequenceMatcher(None, s1, s2).ratio()
    return 0.5 * jaccard + 0.5 * seq


compute_text_similarity = text_overlap_similarity


class HybridRetriever:
    """自适应混合检索器：向量 + BM25 + 有效置信度加权 + 探索召回席位"""

    def __init__(self, storage: Storage, embedding_model_name: Optional[str] = None):
        self.storage = storage
        self.embedding_model = None
        if HAS_FASTEMBED:
            try:
                self.embedding_model = TextEmbedding(embedding_model_name or DEFAULT_EMBEDDING_MODEL)
            except Exception:
                self.embedding_model = None

    def embed_query(self, query: str) -> Optional[bytes]:
        """计算 Query 的向量表示"""
        if not self.embedding_model:
            return None
        try:
            generator = self.embedding_model.embed([query])
            vector = list(generator)[0]
            return vector.astype("float32").tobytes()
        except Exception:
            return None

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        alpha: float = 0.75,
        now: Optional[str | datetime] = None,
    ) -> List[Tuple[Block, float, Dict[str, Any]]]:
        """
        核心混合检索算法：
        Score(B, Q) = [alpha * Sim_dense(B, Q) + (1 - alpha) * Score_BM25(B, Q)] * C_eff(B)
        - 只召回 status IN ('active', 'pending') 的块，归档与挂起态绝对不召回；
        - 支持探索召回席位（Top-K 第 K 席保全新知识曝光）。
        返回格式: [(block, final_score, debug_info), ...]
        """
        now_dt = parse_datetime(now) if now else datetime.utcnow()
        active_blocks = self.storage.get_all_active_blocks()
        if not active_blocks:
            return []

        query_vec = self.embed_query(query)
        query_words = set(jieba.cut(query))

        candidates: List[Tuple[Block, float, float, float, float]] = []
        # format: (block, dense_sim, bm25_sim, fused_sim, final_score)

        for b in active_blocks:
            # 1. 稠密向量相似度
            if query_vec and b.embedding and HAS_NUMPY:
                dense_sim = cosine_similarity(query_vec, b.embedding)
            else:
                dense_sim = text_overlap_similarity(query, b.content)

            # 2. 关键词 BM25 / 词频覆盖率打分
            block_words = set(jieba.cut(b.content))
            if query_words and block_words:
                overlap = len(query_words & block_words)
                bm25_score = overlap / math.sqrt(len(query_words) * len(block_words) + 1)
                bm25_score = min(1.0, bm25_score * 2.0)
            else:
                bm25_score = 0.0

            # 3. 融合相似度
            fused_sim = alpha * dense_sim + (1.0 - alpha) * bm25_score

            # 4. 有效置信度加权
            eff_conf = calc_effective_conf(b, now_dt)
            final_score = fused_sim * eff_conf

            candidates.append((b, dense_sim, bm25_score, fused_sim, final_score))

        # 按加权最终分从大到小排序
        candidates.sort(key=lambda x: x[4], reverse=True)

        # 5. 探索召回保全席位 (Exploration Seats)
        # 若前 K-1 个均为稳定区老知识 (conf_stored >= 0.75)，将第 K 席留给语义高相关但置信度较低的新知识
        exploration_ids: Set[str] = set()
        top_candidates = candidates[:top_k]
        if len(candidates) > top_k and top_k > 1:
            first_k_minus_1 = top_candidates[: top_k - 1]
            all_stable = all(c[0].conf_stored >= CONF_ZONE_STABLE_MIN for c in first_k_minus_1)

            if all_stable:
                max_sim = max((c[3] for c in candidates), default=1.0)
                sim_threshold = 0.75 * max_sim

                # 在排名 K 之后的候选块中寻找高相似的新知识块
                exploration_pick: Optional[Tuple[Block, float, float, float, float]] = None
                for c in candidates[top_k - 1 :]:
                    cand_block = c[0]
                    fused_sim = c[3]
                    # 是学习区/预备区新知识且相似度达到阈值
                    if cand_block.conf_stored < CONF_ZONE_STABLE_MIN and fused_sim >= sim_threshold:
                        exploration_pick = c
                        break

                if exploration_pick and exploration_pick not in first_k_minus_1:
                    top_candidates = first_k_minus_1 + [exploration_pick]
                    exploration_ids.add(exploration_pick[0].id)

        result = []
        for b, d_sim, bm_sim, f_sim, f_score in top_candidates:
            debug_meta = {
                "dense_sim": round(d_sim, 4),
                "bm25_score": round(bm_sim, 4),
                "fused_sim": round(f_sim, 4),
                "final_score": round(f_score, 4),
                "conf_stored": round(b.conf_stored, 4),
                "effective_conf": round(calc_effective_conf(b, now_dt), 4),
                "is_exploration_seat": b.id in exploration_ids,
            }
            result.append((b, f_score, debug_meta))

        return result

    def assemble_context(self, hit_blocks: List[Block]) -> List[Block]:
        """
        父子上下文捆绑组装管线：
        若命中的子块含有父级前置条件块（parent_scope_id），强制把父块捆绑加入上下文
        """
        final_blocks: List[Block] = []
        seen_ids: Set[str] = set()

        for b in hit_blocks:
            # 检查是否有父级前置条件块
            if b.parent_scope_id and b.parent_scope_id not in seen_ids:
                parent_b = self.storage.get_block(b.parent_scope_id)
                # 只有父块处于 active 状态时才捆绑注入
                if parent_b and parent_b.status == "active":
                    final_blocks.append(parent_b)
                    seen_ids.add(parent_b.id)

            if b.id not in seen_ids:
                final_blocks.append(b)
                seen_ids.add(b.id)

        return final_blocks

    def generate_source_badge(
        self,
        blocks: List[Block],
        materials_cache: Optional[Dict[str, Material]] = None,
        now: Optional[str | datetime] = None,
    ) -> str:
        """
        来源透明化标牌注入（§4.2.3）
        生成标准透明度标牌，标明权威基石与试用期新知识
        """
        if not blocks:
            return ""

        now_dt = parse_datetime(now) if now else datetime.utcnow()
        if materials_cache is None:
            materials_cache = {}

        lines = ["\n\n---\n📌 **参考制度与来源**："]

        for b in blocks:
            mat = materials_cache.get(b.material_id)
            if not mat:
                mat = self.storage.get_material(b.material_id)
                if mat:
                    materials_cache[b.material_id] = mat

            mat_title = mat.title if mat else b.material_id
            section_prefix = f" ({b.context_prefix})" if b.context_prefix else ""

            # 区分权威基石与新知识状态
            if b.conf_stored >= CONF_ZONE_STABLE_MIN:
                tag = "[权威基石·已复核]"
            elif b.is_frozen(now_dt):
                tag = "[新知识·举证观察期]"
            elif b.status == "pending":
                tag = "[待处置·建议核对]"
            else:
                tag = "[新知识·试用期]"

            lines.append(f"- {tag} 《{mat_title}》{section_prefix}")

        return "\n".join(lines)
