# -*- coding: utf-8 -*-
"""
BetterRAG 数据模型定义
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List


def utcnow_str() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串。"""
    return datetime.utcnow().isoformat()


@dataclass
class Material:
    """文档材料实体"""
    id: str
    title: str
    source_path: str
    file_hash: str
    file_type: str = "md"
    is_evergreen: bool = False
    is_tier0: bool = False
    imported_by: str = "agent"  # "admin" | "agent"
    summary: Optional[str] = None
    created_at: str = field(default_factory=utcnow_str)
    updated_at: str = field(default_factory=utcnow_str)


@dataclass
class Block:
    """知识块 —— 核心治理单元 (ADR-0002)"""
    id: str
    material_id: str
    content: str
    content_hash: str
    chunk_index: int = 0
    parent_scope_id: Optional[str] = None
    is_condition_header: bool = False
    context_prefix: Optional[str] = None
    is_evergreen: bool = False
    embedding: Optional[bytes] = None

    # 置信度与状态变量
    conf_stored: float = 0.30
    tenure: int = 0
    last_engagement_at: str = field(default_factory=utcnow_str)
    decay_baseline_at: str = field(default_factory=utcnow_str)

    # 状态标记位: 'active' | 'pending' | 'suspended' | 'archived'
    status: str = "active"
    probation_until: Optional[str] = None  # 举证期截止时间 ISO
    pending_reason: Optional[str] = None
    superseded_by: Optional[str] = None
    created_at: str = field(default_factory=utcnow_str)

    def is_frozen(self, now: Optional[datetime] = None) -> bool:
        """是否处于举证期冻结状态"""
        if not self.probation_until:
            return False
        if now is None:
            now = datetime.utcnow()
        try:
            prob_dt = datetime.fromisoformat(self.probation_until)
            return now < prob_dt
        except Exception:
            return False


@dataclass
class AuditEvent:
    """不可变审计事件 (Append-only Audit Log)"""
    id: Optional[int] = None
    ts: str = field(default_factory=utcnow_str)
    block_id: Optional[str] = None
    material_id: Optional[str] = None
    event_type: str = "INGEST"
    actor: str = "engine"
    delta: float = 0.0
    confidence_after: float = 0.0
    note: Optional[str] = None


@dataclass
class Citation:
    """问答引用与证据追踪记录"""
    id: str
    session_id: str
    question: str
    block_id: str
    salience_weight: float = 1.0  # 主干 0.8 / 背景 0.2
    adopted: bool = True
    outcome_feedback: Optional[str] = None
    created_at: str = field(default_factory=utcnow_str)


@dataclass
class ConflictRecord:
    """冲突仲裁与判决台账"""
    id: str
    defender_id: str
    challenger_id: str
    defender_hash: str
    challenger_hash: str
    clash_count: int = 1
    verdict: str = "conflict"       # 'conflict' (确诊互斥) | 'compatible' (兼容不互斥)
    status: str = "open"            # 'open' | 'notified' | 'resolved' | 'rejected'
    shadow_answer_old: Optional[str] = None
    shadow_answer_new: Optional[str] = None
    created_at: str = field(default_factory=utcnow_str)
    updated_at: str = field(default_factory=utcnow_str)
