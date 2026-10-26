# -*- coding: utf-8 -*-
"""BetterRAG 收纳管线 —— 工作区扫描 → 归纳摘要 → 原文切片 → 0.30 入库（规格 §9）。

照搬原则（ADR-0003）：知识块内容必须取自材料原文切片；AI 只写归纳摘要标签，
不得改写、归纳或生成入库内容——幻觉进不了门。
幂等：材料内容 sha1 未变 → 跳过；变了 → 新切片按内容指纹生成新块
（旧块自然失去采纳而衰减——系统自己的新陈代谢解决污染，无需删除）。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Dict, List, Optional

from core import Engine, Material, CONFIG
from llm import LLM

_HEADING = re.compile(r"^#{1,6}\s+", re.M)
_CH = re.compile(r"[\u4e00-\u9fff]")


def _visible_len(s: str) -> int:
    """中文按 1 计、ASCII 按 0.5 计的近似长度（切片用）。"""
    return int(len(s) - 0.5 * (len(s) - len(_CH.findall(s))))


def _hard_split(para: str, size: int, overlap: int) -> List[str]:
    """超长段落按句滑窗（拼接保持原文子串，重叠取尾部原文）。"""
    sents = [s for s in re.split(r"(?<=[。！？；.!?])\s*", para) if s]
    out, buf = [], ""
    for s in sents:
        if buf and _visible_len(buf) + _visible_len(s) > size:
            out.append(buf)
            buf = (buf[-overlap:] if overlap > 0 else "") + s
        else:
            buf += s
    if buf:
        out.append(buf)
    return out


def split_chunks(content: str, size: int = 400, overlap: int = 50) -> List[str]:
    """结构优先：按 ## 小节切；小节内段落聚积，超长段落按句滑窗。
    全部为原文切片（照搬原则），不做任何改写。"""
    content = content.strip()
    if not content:
        return []
    marks = [m.start() for m in _HEADING.finditer(content)]
    if marks:
        parts: List[str] = []
        if marks[0] > 0:
            parts.append(content[:marks[0]])
        for i, s in enumerate(marks):
            e = marks[i + 1] if i + 1 < len(marks) else len(content)
            parts.append(content[s:e])
    else:
        parts = [content]
    chunks: List[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if _visible_len(p) <= size:
            chunks.append(p)
            continue
        buf = ""
        for para in [x for x in p.split("\n\n") if x.strip()]:
            if _visible_len(para) > size:
                if buf.strip():
                    chunks.append(buf.strip())
                    buf = ""
                chunks.extend(_hard_split(para, size, overlap))
            elif buf and _visible_len(buf) + _visible_len(para) > size:
                chunks.append(buf.strip())
                buf = para
            else:
                buf = buf + "\n\n" + para if buf else para
        if buf.strip():
            chunks.append(buf.strip())
    return [c for c in chunks if _visible_len(c) >= 20]


def read_material(path: str) -> dict:
    """读文件 → {title, content}。标题取首个 # 标题，否则文件名。"""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    title = ""
    for line in content.splitlines():
        if line.strip().startswith("#"):
            title = line.lstrip("#").strip()
            break
    if not title:
        title = os.path.splitext(os.path.basename(path))[0]
    return {"title": title, "content": content.strip()}


def material_id_for(path: str) -> str:
    base = os.path.splitext(os.path.basename(path))[0].strip()
    return re.sub(r"\s+", "-", base) or hashlib.sha1(path.encode()).hexdigest()[:12]


class Ingestor:
    """工作区收纳器。scan() 幂等可重复调用。"""

    def __init__(self, eng: Engine, llm: LLM) -> None:
        self.eng = eng
        self.llm = llm

    def summary_for(self, mid: str, content: str) -> str:
        """归纳摘要：LLM 可用则生成（1 次调用/材料），否则本地抽取式。"""
        text = self.llm.call("summarize", content[:3000], max_tokens=200)
        if text:
            return text.strip()[:300]
        return self.llm.summarize_local(content)

    def admin_import(self, path: str, tier0: bool = False) -> dict:
        """管理员导入：直进稳定区 0.85（冷启动通道，带背书）。"""
        mat = read_material(path)
        mid = material_id_for(path)
        if mid in self.eng.materials and self.eng.materials[mid].content_hash == self._hash(mat["content"]):
            return {"material": mid, "changed": False}
        summary = self.summary_for(mid, mat["content"])
        self._replace_material(mid, mat, summary, admin=True, tier0=tier0,
                               source_path=path)
        return {"material": mid, "changed": True}

    def scan(self, workspace: str) -> dict:
        """扫描工作区（.md/.txt），新/变材料切片入库 0.30。返回扫描报告。"""
        report = {"scanned": 0, "ingested": [], "unchanged": 0, "chunks": 0}
        if not os.path.isdir(workspace):
            report["error"] = f"工作区不存在：{workspace}"
            return report
        for name in sorted(os.listdir(workspace)):
            path = os.path.join(workspace, name)
            if not os.path.isfile(path) or not name.lower().endswith((".md", ".txt")):
                continue
            report["scanned"] += 1
            mat = read_material(path)
            h = self._hash(mat["content"])
            mid = material_id_for(path)
            if mid in self.eng.materials and self.eng.materials[mid].content_hash == h:
                report["unchanged"] += 1
                continue
            summary = self.summary_for(mid, mat["content"])
            blocks = self._replace_material(mid, mat, summary, admin=False,
                                            tier0=False, source_path=path)
            report["ingested"].append({"material": mid, "title": mat["title"],
                                       "blocks": len(blocks)})
            report["chunks"] += len(blocks)
        return report

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha1(content.encode("utf-8")).hexdigest()

    def _replace_material(self, mid: str, mat: dict, summary: str,
                          admin: bool, tier0: bool, source_path: str) -> list:
        eng = self.eng
        chunks = split_chunks(mat["content"], size=int(CONFIG["ingest.chunk"])) or [mat["content"]]
        h = self._hash(mat["content"])
        if mid not in eng.materials:
            eng.materials[mid] = Material(
                id=mid, title=mat["title"], source_path=source_path, summary=summary,
                summary_version=1, imported_by_admin=admin, content_hash=h,
                created_at=eng.now)
        else:
            m = eng.materials[mid]
            m.summary_version += 1
            m.summary = summary
            m.content_hash = h
            m.source_path = source_path
            m.imported_by_admin = m.imported_by_admin or admin
        # 逐块幂等：内容指纹相同的块保留原状态（生命周期不因重扫重置）
        existing = {b.chunk_index: b for b in eng.material_blocks(mid)}
        by_hash = {self._hash(b.content): b for b in existing.values() if b.content}
        created = []
        for i, chunk in enumerate(chunks):
            ch = self._hash(chunk)
            same = by_hash.get(ch)
            if same is not None and same.chunk_index == i:
                continue          # 未变化的切片：状态原封不动
            if same is not None:
                # 内容相同但序号变化：仅移动序号，保状态
                same.chunk_index = i
                continue
            bid = f"{mid}#{i}:{ch[:8]}"      # 内容指纹 id：内容变 → 新块新生命周期
            if bid in eng.blocks:
                continue
            if admin and i == 0:
                b = eng.add_material(mid, mat["title"], mat["title"], admin=True,
                                     tier0=tier0, content=chunk, summary=summary,
                                     source_path=source_path)
            else:
                # 管理员导入的材料：全部切片直进稳定区（带背书），不是只有首块；
                # 铁律材料整篇皆铁律（治理单元=块，每块独立不可挑战降级）
                b = eng.add_chunk(mid, i, chunk, title=mat["title"],
                                  admin=admin, tier0=tier0, bid=bid)
            created.append(b)
        # 首块标题块：非管理员导入时，材料级摘要挂在 #0 块的 summary 字段供展示
        if created or (mid in eng.materials and not eng.materials[mid].imported_by_admin):
            first = eng.material_blocks(mid)
            if first:
                first[0].summary = summary
        return created
