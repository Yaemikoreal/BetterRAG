# -*- coding: utf-8 -*-
"""BetterRAG 检索管线 —— 单通道 + 探索召回（规格 §8）。

score = 相关度 × 有效置信度；归档/挂起排除。
本地实现：CJK 二元组 + ASCII 词的 TF-IDF 余弦（零依赖、零网络、确定性），
接口与 embedding 实现同形（换向量后端只动本文件）。
探索召回：top-k 内为高相关低置信（学习区）块强制保留席位，答案须标注
「含探索性召回」——曝光是晋升机制的前提（解曝光悖论）。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List, Tuple

from core import Block, Engine, CONFIG

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9+\-./#]*")


def tokens(text: str) -> List[str]:
    """CJK 二元组 + ASCII 词元。小写归一。"""
    out: List[str] = []
    for m in _ASCII.finditer(text):
        out.append(m.group(0).lower())
    for ch in _CJK.finditer(text):
        seg = ch.group(0)
        if len(seg) == 1:
            out.append(seg)
        else:
            out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


def content_overlap(a: str, b: str) -> float:
    """两个文本的词元 Jaccard 重叠（本地 judge 用）。"""
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class Retriever:
    """全库块检索。构建索引廉价（千块级全量重算 <10ms），每次 ask 前刷新。"""

    def __init__(self, eng: Engine) -> None:
        self.eng = eng
        self._index: Dict[str, Counter] = {}
        self._idf: Dict[str, float] = {}
        self._dirty = True

    def invalidate(self) -> None:
        self._dirty = True

    def _build(self) -> None:
        tfs: Dict[str, Counter] = {}
        df: Counter = Counter()
        n = 0
        for b in self.eng.blocks.values():
            if not b.content:
                continue
            tf = Counter(tokens(b.title + " " + b.content))
            if not tf:
                continue
            tfs[b.id] = tf
            df.update(tf.keys())
            n += 1
        self._index = tfs
        self._idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
        self._dirty = False

    @staticmethod
    def _cosine(qtf: Counter, tf: Counter, idf: Dict[str, float]) -> float:
        num = sum(w * idf.get(t, 1.0) * tf.get(t, 0) for t, w in qtf.items())
        da = math.sqrt(sum((w * idf.get(t, 1.0)) ** 2 for t, w in qtf.items()))
        db = math.sqrt(sum((idf.get(t, 1.0) * c) ** 2 for t, c in tf.items()))
        if da <= 0 or db <= 0:
            return 0.0
        return num / (da * db)

    def search(self, question: str, now: float) -> List[dict]:
        """返回 [{block, sim, score, exploration}]，按 score 降序。
        score = sim × effective(now)；探索席：学习区高相关块保席并标记。"""
        if self._dirty:
            self._build()
        qtf = Counter(tokens(question))
        if not qtf or not self._index:
            return []
        k = int(CONFIG["retrieve.top_k_blocks"])
        seats = int(CONFIG["retrieve.exploration_seats"])
        scored: List[Tuple[float, float, str]] = []
        for bid, tf in self._index.items():
            b = self.eng.blocks[bid]
            if not b.searchable():
                continue
            sim = self._cosine(qtf, tf, self._idf)
            if sim <= 0.01:
                continue
            scored.append((sim * b.effective(now), sim, bid))
        scored.sort(reverse=True)
        out: List[dict] = []
        seen = set()
        best_sim = scored[0][1] if scored else 0.0
        for score, sim, bid in scored:
            if len(out) >= k:
                break
            b = self.eng.blocks[bid]
            out.append({"block": b, "sim": round(sim, 4), "score": round(score, 4),
                        "exploration": False})
            seen.add(bid)
        # 探索席：从余下候选中挑学习区高相关块（sim ≥ 0.75 × 最高 sim）
        if seats > 0 and best_sim > 0:
            for score, sim, bid in scored:
                if len(out) >= k + seats:
                    break
                if bid in seen:
                    continue
                b = self.eng.blocks[bid]
                if b.zone_code() != 0:      # 仅学习区补席
                    continue
                if sim < 0.75 * best_sim:
                    continue
                out.append({"block": b, "sim": round(sim, 4), "score": round(score, 4),
                            "exploration": True})
                seen.add(bid)
        return out

    def neighbors(self, b: Block) -> List[Block]:
        """同材料相邻块窗口（±1），用于上下文拼装（召回单元=材料）。"""
        sibs = [x for x in self.eng.material_blocks(b.material_id) if x.searchable()]
        i = next((j for j, x in enumerate(sibs) if x.id == b.id), -1)
        out = []
        for off in (-1, 1):
            j = i + off
            if 0 <= j < len(sibs) and sibs[j].id != b.id:
                out.append(sibs[j])
        return out
