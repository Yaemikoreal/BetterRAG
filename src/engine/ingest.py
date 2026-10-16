# -*- coding: utf-8 -*-
"""
BetterRAG 文件解析、父子切片与增量继承管线 (Ingestion Pipeline)
严格实现 docs/BetterRAG生产级开发实施方案.md §4.1 与 Agent讨论 #003/#004/#008
"""
from __future__ import annotations

import os
import re
import hashlib
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Dict, Tuple, Optional, Any, Set
from datetime import datetime

import markdown_it
from markdown_it.tree import SyntaxTreeNode

from src.engine.config import (
    CONF_INITIAL_LEARN,
    CONF_INITIAL_ADMIN,
    CHUNK_MIN_LEN,
    CHUNK_MAX_LEN,
    CHUNK_TARGET_MIN,
    CHUNK_TARGET_MAX,
    FUZZY_ALIGN_SIM,
    FUZZY_ALIGN_LEN_DIFF,
    DEFAULT_EMBEDDING_MODEL,
)
from src.engine.models import Material, Block, AuditEvent, utcnow_str
from src.engine.storage import Storage

logger = logging.getLogger(__name__)

try:
    from fastembed import TextEmbedding
    HAS_FASTEMBED = True
except ImportError:
    HAS_FASTEMBED = False


def sha256_text(text: str) -> str:
    """计算规范化文本的 SHA256"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


# 前置条件/限制性条款关键词模式
CONDITION_KEYWORDS = [
    "前置条件", "限制条件", "除外条款", "不适用", "以下人员不享受",
    "适用范围", "特别规定", "准入条件", "资格要求", "前提要求"
]

LIST_ITEM_PATTERN = re.compile(
    r"^(\d+[\.\、\)]|\([0-9a-zA-Z]+\)|（[0-9a-zA-Z一二三四五六七八九十]+）|[-*+•]|[一二三四五六七八九十]+[、\.])\s*"
)


def is_condition_text(text: str) -> bool:
    """判断文本是否具有前置限制条件特征"""
    clean = text.strip()
    return any(kw in clean for kw in CONDITION_KEYWORDS)


def is_list_item(text: str) -> bool:
    """判断文本是否以列表编号或无序列表符开头"""
    clean = text.strip()
    return bool(LIST_ITEM_PATTERN.match(clean))


class MarkdownParser:
    """基于语义 AST 的 Markdown 标题层级与段落树分块器"""

    def __init__(self):
        self.md = markdown_it.MarkdownIt("commonmark")

    def parse_frontmatter(self, text: str) -> Tuple[Dict[str, Any], str]:
        """解析简单的 YAML frontmatter"""
        frontmatter = {}
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                fm_raw = parts[1]
                body = parts[2].strip()
                for line in fm_raw.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k = k.strip()
                        v = v.strip().strip("\"'")
                        if v.lower() in ("true", "1", "yes"):
                            frontmatter[k] = True
                        elif v.lower() in ("false", "0", "no"):
                            frontmatter[k] = False
                        else:
                            frontmatter[k] = v
        return frontmatter, body

    def split_paragraphs_and_normalize(self, raw_paragraphs: List[str]) -> List[str]:
        """
        段落规范化：
        1. 碎段合并：< 50 字符自动并入相邻段落，消除低语义噪声；
           - 关键防线：若本节已出现前置限制条件头，其后续所有条款必须保持独立成块，严禁并入条件头；
           - 列表项（1. 2. - 等）保持独立，不向前合并。
        2. 超长段二次切分：> 600 字符按标点贪心切成 300~500 字子块，规避 BGE 截断。
        """
        merged_paras: List[str] = []
        seen_condition = False

        for p in raw_paragraphs:
            p = p.strip()
            if not p:
                continue

            is_cond = is_condition_text(p)
            if is_cond:
                seen_condition = True
                merged_paras.append(p)
                continue

            # 若本节已有条件头，或当前为列表项，保持独立子块，不向前合并
            if seen_condition or is_list_item(p):
                merged_paras.append(p)
                continue

            if (
                len(p) < CHUNK_MIN_LEN
                and merged_paras
                and not is_condition_text(merged_paras[-1])
                and not is_list_item(merged_paras[-1])
            ):
                merged_paras[-1] = merged_paras[-1] + "\n" + p
            else:
                merged_paras.append(p)

        # 第二步：超长段二次拆分
        normalized: List[str] = []
        for p in merged_paras:
            if len(p) <= CHUNK_MAX_LEN:
                normalized.append(p)
            else:
                sub_chunks = self._split_long_paragraph(p, max_len=CHUNK_TARGET_MAX)
                normalized.extend(sub_chunks)
        return normalized

    def _split_long_paragraph(self, text: str, max_len: int = 400) -> List[str]:
        """按句号/分号/换行等标点贪心拆分超长文本"""
        sentences = re.split(r"([。！？；\n]+)", text)
        chunks = []
        cur = ""
        i = 0
        while i < len(sentences):
            sent = sentences[i]
            punct = sentences[i + 1] if i + 1 < len(sentences) else ""
            part = sent + punct
            i += 2

            if len(cur) + len(part) > max_len and len(cur) >= CHUNK_TARGET_MIN:
                chunks.append(cur.strip())
                cur = part
            else:
                cur += part
        if cur.strip():
            chunks.append(cur.strip())
        return chunks if chunks else [text]

    def parse_to_sections(self, md_text: str) -> List[Dict[str, Any]]:
        """
        将 Markdown 解析为带层级路径的 Section 列表
        每个 Section 包含:
          - header_path: List[str] (例如: ["第一章 考勤管理", "第二节 请假规定"])
          - paragraphs: List[str] (自然段落)
        """
        lines = md_text.splitlines()
        sections: List[Dict[str, Any]] = []
        header_stack: List[Tuple[int, str]] = []  # (level, title)
        cur_paragraphs: List[str] = []
        cur_buf: List[str] = []

        def flush_buf():
            nonlocal cur_buf
            if cur_buf:
                text = "\n".join(cur_buf).strip()
                if text:
                    cur_paragraphs.append(text)
                cur_buf = []

        def flush_section():
            nonlocal cur_paragraphs
            flush_buf()
            if cur_paragraphs:
                path = [h[1] for h in header_stack]
                sections.append({
                    "header_path": path,
                    "paragraphs": list(cur_paragraphs)
                })
                cur_paragraphs = []

        for line in lines:
            header_match = re.match(r"^(#{1,6})\s+(.*)$", line.strip())
            if header_match:
                flush_section()
                level = len(header_match.group(1))
                title = header_match.group(2).strip()
                while header_stack and header_stack[-1][0] >= level:
                    header_stack.pop()
                header_stack.append((level, title))
            elif line.strip() == "":
                flush_buf()
            else:
                cur_buf.append(line)

        flush_section()
        return sections


class IngestionEngine:
    """增量摄取与父子绑定处理引擎"""

    def __init__(self, storage: Storage, embedding_model_name: Optional[str] = None):
        self.storage = storage
        self.parser = MarkdownParser()
        self.embedding_model = None
        if HAS_FASTEMBED:
            try:
                self.embedding_model = TextEmbedding(embedding_model_name or DEFAULT_EMBEDDING_MODEL)
            except Exception as e:
                logger.debug("FastEmbed 初始化跳过: %s", e)
                self.embedding_model = None

    def compute_embedding(self, text: str) -> Optional[bytes]:
        """使用 FastEmbed 计算文本的向量并序列化为 BLOB"""
        if not self.embedding_model:
            return None
        try:
            generator = self.embedding_model.embed([text])
            vector = list(generator)[0]
            # 存为 float32 的 bytes
            return vector.astype("float32").tobytes()
        except Exception as e:
            logger.debug("向量计算失败: %s", e)
            return None

    def ingest_file(
        self,
        file_path: str | Path,
        workspace_root: str | Path,
        as_admin: Optional[bool] = None,
        as_evergreen: Optional[bool] = None,
        enable_fuzzy_align: bool = False,
    ) -> Material:
        """
        完整文件摄取与增量状态继承
        - 增量比对旧块 SHA256，精确命中 100% 沿用旧块 ID 与置信度、tenure
        - 新块基于 material_id + content_hash 生成稳定全局唯一 ID，杜绝位置漂移
        - 一对多子块通过唯一 marker 精准绑定父块 ID，绝不错绑或悬空
        - 被删块：自动标记为 archived 并建立 superseded_by
        - 全量操作在单个数据库事务内执行
        """
        path = Path(file_path).resolve()
        workspace_root = Path(workspace_root).resolve()
        rel_path = path.relative_to(workspace_root).as_posix()

        with open(path, "r", encoding="utf-8") as f:
            raw_text = f.read()

        file_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
        frontmatter, body = self.parser.parse_frontmatter(raw_text)

        title = frontmatter.get("title") or path.stem

        is_official_dir = "official/" in rel_path or rel_path.startswith("official")
        is_admin = as_admin if as_admin is not None else (
            is_official_dir or frontmatter.get("imported_by") == "admin"
        )
        is_evergreen = as_evergreen if as_evergreen is not None else bool(
            frontmatter.get("is_evergreen", False)
        )
        is_tier0 = bool(frontmatter.get("is_tier0", False))

        material_id = f"mat_{path.stem}_{hashlib.md5(rel_path.encode('utf-8')).hexdigest()[:8]}"
        initial_conf = CONF_INITIAL_ADMIN if is_admin else CONF_INITIAL_LEARN
        imported_by = "admin" if is_admin else "agent"

        existing_mat = self.storage.get_material_by_path(rel_path)
        if existing_mat:
            material_id = existing_mat.id

        material = Material(
            id=material_id,
            title=title,
            source_path=rel_path,
            file_hash=file_hash,
            file_type=path.suffix.lstrip(".").lower() or "md",
            is_evergreen=is_evergreen,
            is_tier0=is_tier0,
            imported_by=imported_by,
            summary=frontmatter.get("summary"),
            updated_at=utcnow_str()
        )
        self.storage.save_material(material)

        # 获取已有旧块，用于增量 Hash 对比继承
        old_blocks = self.storage.get_blocks_by_material(material_id)
        old_blocks_map = {b.content_hash: b for b in old_blocks}
        unmatched_old = list(old_blocks)

        # 解析 Section 并生成新候选块
        sections = self.parser.parse_to_sections(body)
        raw_chunks: List[Tuple[Optional[str], str, bool, Optional[str], Optional[str]]] = []
        # format: (parent_marker, content, is_condition_header, context_prefix, my_marker)

        for sec_idx, sec in enumerate(sections):
            header_path_str = " > ".join(sec["header_path"]) if sec["header_path"] else title
            normalized_paras = self.parser.split_paragraphs_and_normalize(sec["paragraphs"])

            current_parent_marker: Optional[str] = None
            for p_idx, para in enumerate(normalized_paras):
                is_cond = is_condition_text(para)
                if is_cond:
                    # 本段被识别为前置限制条款
                    marker = f"__sec_{sec_idx}_p_{p_idx}__"
                    current_parent_marker = marker
                    raw_chunks.append((None, para, True, header_path_str, marker))
                elif current_parent_marker is not None:
                    # 作为该前置条件下的子块，携带其唯一的 parent_marker
                    raw_chunks.append((current_parent_marker, para, False, header_path_str, None))
                else:
                    raw_chunks.append((None, para, False, header_path_str, None))

        # 实例化待保存的 New Blocks 并执行 Hash 继承
        new_blocks: List[Block] = []
        matched_old_ids: Set[str] = set()
        marker_to_real_id: Dict[str, str] = {}
        used_block_ids: Set[str] = set()

        # 阶段一：精确 Content Hash 匹配与稳定 ID 派生 (解决 P0-2: 实体同一性与 ID 漂移)
        for idx, (parent_marker, content, is_cond, ctx_prefix, my_marker) in enumerate(raw_chunks):
            chash = sha256_text(content)

            inherited_conf = initial_conf
            inherited_tenure = 0
            last_eng = utcnow_str()
            decay_base = utcnow_str()
            status = "active"
            probation = None
            pending_reason = None

            if chash in old_blocks_map:
                old_b = old_blocks_map[chash]
                block_id = old_b.id  # 精确命中 100% 沿用旧块 ID，杜绝 ID 漂移！
                inherited_conf = old_b.conf_stored
                inherited_tenure = old_b.tenure
                last_eng = old_b.last_engagement_at
                decay_base = old_b.decay_baseline_at
                status = old_b.status
                probation = old_b.probation_until
                pending_reason = old_b.pending_reason
                matched_old_ids.add(old_b.id)
                if old_b in unmatched_old:
                    unmatched_old.remove(old_b)
            else:
                # 全新块：基于 material_id + chash 派生稳定全局唯一 ID
                base_id = f"{material_id}_{chash[:12]}"
                block_id = base_id
                dup_count = 1
                while block_id in used_block_ids:
                    block_id = f"{base_id}_{dup_count}"
                    dup_count += 1

            used_block_ids.add(block_id)

            if my_marker:
                marker_to_real_id[my_marker] = block_id

            b = Block(
                id=block_id,
                material_id=material_id,
                content=content,
                content_hash=chash,
                chunk_index=idx,
                parent_scope_id=parent_marker,
                is_condition_header=is_cond,
                context_prefix=ctx_prefix,
                is_evergreen=is_evergreen,
                embedding=None,
                conf_stored=inherited_conf,
                tenure=inherited_tenure,
                last_engagement_at=last_eng,
                decay_baseline_at=decay_base,
                status=status,
                probation_until=probation,
                pending_reason=pending_reason,
            )
            new_blocks.append(b)

        # 阶段二：同 Material 范围内的模糊对齐 (Fuzzy Alignment)
        if enable_fuzzy_align:
            for b in new_blocks:
                if b.content_hash not in old_blocks_map and unmatched_old:
                    best_match: Optional[Block] = None
                    best_sim = 0.0
                    for old_b in unmatched_old:
                        len_ratio = abs(len(b.content) - len(old_b.content)) / max(len(old_b.content), 1)
                        if len_ratio <= FUZZY_ALIGN_LEN_DIFF:
                            sim = SequenceMatcher(None, b.content, old_b.content).ratio()
                            if sim >= FUZZY_ALIGN_SIM and sim > best_sim:
                                best_sim = sim
                                best_match = old_b

                    if best_match:
                        b.conf_stored = best_match.conf_stored
                        b.tenure = best_match.tenure
                        b.last_engagement_at = best_match.last_engagement_at
                        b.status = best_match.status
                        matched_old_ids.add(best_match.id)
                        unmatched_old.remove(best_match)

        # 阶段三：解析父子真实 ID 绑定 (解决 P0-1: 多子块一对多精确映射，绝不错绑)
        for b in new_blocks:
            if b.parent_scope_id and b.parent_scope_id in marker_to_real_id:
                b.parent_scope_id = marker_to_real_id[b.parent_scope_id]
            elif b.parent_scope_id and b.parent_scope_id.startswith("__sec_"):
                # 兜底清理未匹配的临时 marker
                b.parent_scope_id = None

        # 阶段四与阶段五：单事务内原子落库，按拓扑序优先存父块再存子块
        with self.storage.conn:
            # 1. 处理被删/被取代的旧块 (标记为 archived 并建立 superseded_by)
            for old_b in unmatched_old:
                if old_b.id not in matched_old_ids:
                    old_b.status = "archived"
                    old_b.pending_reason = "文件内容更新，原段落已移除"
                    if new_blocks:
                        old_b.superseded_by = new_blocks[0].id
                    self.storage.save_block(old_b)
                    self.storage.log_event(
                        block_id=old_b.id,
                        material_id=material_id,
                        event_type="ARCHIVE",
                        actor="engine",
                        delta=0.0,
                        confidence_after=0.0,
                        note=f"段落被删，自动归档并建立取代链 -> {old_b.superseded_by}"
                    )

            # 2. 拓扑排序：先存无 parent_scope_id 的根块/父块，再存子块，确保外键约束始终满足
            root_blocks = [b for b in new_blocks if not b.parent_scope_id]
            child_blocks = [b for b in new_blocks if b.parent_scope_id]

            for b in root_blocks + child_blocks:
                if b.content_hash in old_blocks_map and old_blocks_map[b.content_hash].embedding:
                    b.embedding = old_blocks_map[b.content_hash].embedding
                else:
                    b.embedding = self.compute_embedding(b.content)

                self.storage.save_block(b)

                if b.content_hash not in old_blocks_map:
                    event_type = "ADMIN_IMPORT" if is_admin else "INGEST"
                    self.storage.log_event(
                        block_id=b.id,
                        material_id=material_id,
                        event_type=event_type,
                        actor=imported_by,
                        delta=b.conf_stored,
                        confidence_after=b.conf_stored,
                        note="管理员冷启动直进稳定区" if is_admin else "文档新段落收纳入学习区"
                    )

        return material

    def sync_workspace(self, workspace_dir: str | Path) -> List[Material]:
        """扫描 workspace 目录完成全量同步（按 file_hash 增量跳过未变文件）"""
        root = Path(workspace_dir).resolve()
        materials = []
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in (".md", ".txt"):
                rel_path = path.relative_to(root).as_posix()
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    file_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                    existing_mat = self.storage.get_material_by_path(rel_path)
                    if existing_mat and existing_mat.file_hash == file_hash:
                        materials.append(existing_mat)
                        continue
                except Exception as e:
                    logger.debug("比对文件哈希异常，继续执行摄取: %s", e)

                mat = self.ingest_file(path, workspace_root=root)
                materials.append(mat)
        return materials
