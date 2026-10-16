# -*- coding: utf-8 -*-
"""
BetterRAG 管理员审批、制度修正案与飞书卡片回调路由 (Admin & Exceptions API)
严格实现 docs/BetterRAG生产级开发实施方案.md §5.4, §5.5, §5.6
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any, List
from fastapi import APIRouter, Request, HTTPException, Body
from pydantic import BaseModel, Field

from src.engine.storage import Storage
from src.engine.arbitrate import ConflictManager
from src.engine.probation import ProbationManager
from src.engine.models import Block, ConflictRecord

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Admin & Exceptions"])


class AdjudicateRequest(BaseModel):
    action: str = Field(..., description="裁决动作: 'pass' (通过转正) | 'reject' (驳回重置)")
    note: str = Field("", description="管理员审核批注")
    actor: str = Field("admin", description="审核人标识")


def get_managers(request: Request) -> tuple[Storage, ConflictManager, ProbationManager]:
    storage: Storage = request.app.state.storage
    conflict_manager = getattr(request.app.state, "conflict_manager", None)
    if conflict_manager is None:
        conflict_manager = ConflictManager(storage)
        request.app.state.conflict_manager = conflict_manager

    probation_manager = getattr(request.app.state, "probation_manager", None)
    if probation_manager is None:
        probation_manager = ProbationManager(storage)
        request.app.state.probation_manager = probation_manager

    return storage, conflict_manager, probation_manager


@router.get("/api/v1/exceptions")
async def list_exceptions(request: Request) -> Dict[str, Any]:
    """获取待审核的异常知识块清单（包含待处置、挑刺存疑与举证期知识）"""
    storage, _, _ = get_managers(request)
    cursor = storage.conn.cursor()
    cursor.execute("""
    SELECT * FROM blocks 
    WHERE status = 'pending' OR probation_until IS NOT NULL
    ORDER BY created_at DESC;
    """)
    rows = cursor.fetchall()
    exceptions = []
    for r in rows:
        b = storage._row_to_block(r)
        exceptions.append({
            "id": b.id,
            "material_id": b.material_id,
            "context_prefix": b.context_prefix,
            "content": b.content,
            "conf_stored": b.conf_stored,
            "status": b.status,
            "pending_reason": b.pending_reason,
            "probation_until": b.probation_until,
            "tenure": b.tenure,
            "created_at": b.created_at,
        })
    return {"status": "ok", "count": len(exceptions), "data": exceptions}


@router.post("/api/v1/exceptions/{block_id}/adjudicate")
async def adjudicate_exception_endpoint(
    block_id: str,
    payload: AdjudicateRequest,
    request: Request,
) -> Dict[str, Any]:
    """人工裁决例外知识：pass 转正至 0.85，reject 驳回重置至 0.30"""
    storage, _, probation_manager = get_managers(request)
    success = probation_manager.adjudicate_exception(
        block_id=block_id,
        action=payload.action,
        actor=payload.actor,
        note=payload.note,
    )
    if not success:
        raise HTTPException(status_code=404, detail=f"未找到目标待裁决知识块: {block_id}")

    block = storage.get_block(block_id)
    return {
        "status": "ok",
        "block_id": block_id,
        "action": payload.action,
        "conf_stored": block.conf_stored if block else 0.0,
        "block_status": block.status if block else "unknown"
    }


@router.get("/api/v1/conflicts")
async def list_conflicts(
    request: Request,
    verdict: Optional[str] = None,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    """获取系统检测到的全部冲突/兼容台账"""
    storage, _, _ = get_managers(request)
    conflicts = storage.get_all_conflicts(verdict=verdict, status=status)
    res = []
    for c in conflicts:
        defender = storage.get_block(c.defender_id)
        challenger = storage.get_block(c.challenger_id)
        res.append({
            "id": c.id,
            "defender_id": c.defender_id,
            "challenger_id": c.challenger_id,
            "defender_content": defender.content if defender else "[已删除]",
            "challenger_content": challenger.content if challenger else "[已删除]",
            "clash_count": c.clash_count,
            "verdict": c.verdict,
            "status": c.status,
            "shadow_answer_old": c.shadow_answer_old,
            "shadow_answer_new": c.shadow_answer_new,
            "updated_at": c.updated_at,
        })
    return {"status": "ok", "count": len(res), "data": res}


@router.post("/api/v1/amend/{conflict_id}/replace")
async def amend_replace_endpoint(
    conflict_id: str,
    request: Request,
) -> Dict[str, Any]:
    """原子性制度修正案接替：挑战者升 0.85 稳定区，旧制度自动降级归档"""
    _, conflict_manager, _ = get_managers(request)
    success = conflict_manager.amend_replace(conflict_id)
    if not success:
        raise HTTPException(status_code=400, detail=f"修正案接替失败，冲突记录不存在或双方知识块已被移除: {conflict_id}")

    return {
        "status": "ok",
        "conflict_id": conflict_id,
        "message": "制度修正案原子接替完成：新规已晋升稳定区，旧制度已自动归档并建立取代链"
    }


@router.post("/api/v1/webhook/feishu/interactive")
async def feishu_interactive_callback(
    request: Request,
    payload: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """
    飞书卡片交互回调端点：
    - 支持 URL 校验 Challenge 握手
    - 支持卡片内【一键接替】与【驳回新规】按钮交互回调
    """
    # 1. 飞书首次配置 Webhook 挑战验签
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    storage, conflict_manager, probation_manager = get_managers(request)

    # 2. 交互式卡片点击行为解析
    action_data = payload.get("action", {}).get("value", {})
    action_type = action_data.get("action")
    conflict_id = action_data.get("conflict_id")
    challenger_id = action_data.get("challenger_id")

    if action_type == "replace" and conflict_id:
        success = conflict_manager.amend_replace(conflict_id)
        if success:
            return {
                "toast": {
                    "type": "success",
                    "content": "✅ 制度修正案已成功执行！新规晋升 0.85 稳定区，旧规已归档。"
                }
            }
        else:
            return {
                "toast": {
                    "type": "error",
                    "content": "⚠️ 执行失败：目标冲突记录不存在或已处理。"
                }
            }

    elif action_type == "reject" and challenger_id:
        success = probation_manager.adjudicate_exception(challenger_id, action="reject", note="飞书卡片管理员点击驳回")
        # 同步将冲突标记为 rejected
        if conflict_id:
            c = storage.get_conflict(action_data.get("defender_id", ""), challenger_id)
            if c:
                c.status = "rejected"
                storage.save_conflict(c)

        return {
            "toast": {
                "type": "info",
                "content": "❌ 已驳回该新知识，维持权威基石生效。"
            }
        }

    return {"status": "ignored", "detail": "未识别的卡片事件或缺少关键参数"}


@router.get("/api/v1/dashboard/stats")
async def get_dashboard_stats(request: Request) -> Dict[str, Any]:
    """提供给极简管理看板的实时统计数据"""
    storage, _, _ = get_managers(request)
    cursor = storage.conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM materials;")
    total_materials = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM blocks;")
    total_blocks = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM blocks WHERE conf_stored >= 0.75 AND status = 'active';")
    stable_blocks = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM blocks WHERE conf_stored < 0.75 AND status = 'active';")
    learning_blocks = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM blocks WHERE status = 'pending';")
    pending_blocks = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM blocks WHERE status = 'archived';")
    archived_blocks = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM blocks WHERE probation_until IS NOT NULL;")
    probation_blocks = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM conflicts WHERE status IN ('open', 'notified');")
    active_conflicts = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM events;")
    total_events = cursor.fetchone()[0]

    return {
        "status": "ok",
        "data": {
            "total_materials": total_materials,
            "total_blocks": total_blocks,
            "stable_blocks": stable_blocks,
            "learning_blocks": learning_blocks,
            "pending_blocks": pending_blocks,
            "archived_blocks": archived_blocks,
            "probation_blocks": probation_blocks,
            "active_conflicts": active_conflicts,
            "total_events": total_events,
        }
    }


@router.get("/api/v1/materials")
async def list_materials(request: Request) -> Dict[str, Any]:
    """获取所有录入的制度文件材料列表及切片统计"""
    storage, _, _ = get_managers(request)
    mats = storage.get_all_materials()
    cursor = storage.conn.cursor()

    res = []
    for m in mats:
        cursor.execute("SELECT COUNT(*) FROM blocks WHERE material_id = ?;", (m.id,))
        b_count = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM blocks WHERE material_id = ? AND conf_stored >= 0.75 AND status = 'active';", (m.id,))
        stable_count = cursor.fetchone()[0]

        res.append({
            "id": m.id,
            "title": m.title,
            "source_path": m.source_path,
            "file_hash": m.file_hash,
            "file_type": m.file_type,
            "is_evergreen": m.is_evergreen,
            "imported_by": m.imported_by,
            "total_blocks": b_count,
            "stable_blocks": stable_count,
            "updated_at": m.updated_at,
        })
    return {"status": "ok", "count": len(res), "data": res}


@router.get("/api/v1/blocks")
async def list_blocks(
    request: Request,
    material_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 150,
) -> Dict[str, Any]:
    """获取知识切片详细信息（包含实时有效置信度与分区状态）"""
    from src.engine.confidence import calc_effective_conf, get_zone_name

    storage, _, _ = get_managers(request)
    cursor = storage.conn.cursor()

    query = "SELECT * FROM blocks WHERE 1=1"
    params = []
    if material_id:
        query += " AND material_id = ?"
        params.append(material_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY CASE status WHEN 'pending' THEN 0 WHEN 'active' THEN 1 ELSE 2 END, id ASC LIMIT ?;"
    params.append(limit)

    cursor.execute(query, tuple(params))
    rows = cursor.fetchall()

    res = []
    for r in rows:
        b = storage._row_to_block(r)
        eff_conf = calc_effective_conf(b)
        zone = get_zone_name(b)
        res.append({
            "id": b.id,
            "material_id": b.material_id,
            "context_prefix": b.context_prefix or "正文",
            "content": b.content,
            "content_hash": b.content_hash,
            "is_condition_header": b.is_condition_header,
            "parent_scope_id": b.parent_scope_id,
            "conf_stored": round(b.conf_stored, 4),
            "eff_conf": round(eff_conf, 4),
            "zone": zone,
            "tenure": b.tenure,
            "status": b.status,
            "probation_until": b.probation_until,
            "pending_reason": b.pending_reason,
            "last_engagement_at": b.last_engagement_at,
        })
    return {"status": "ok", "count": len(res), "data": res}


@router.get("/api/v1/events")
async def list_events(
    request: Request,
    block_id: Optional[str] = None,
    limit: int = 60,
) -> Dict[str, Any]:
    """获取系统不可变审计事实流"""
    storage, _, _ = get_managers(request)
    events = storage.get_events(block_id=block_id, limit=limit)
    res = []
    for e in events:
        res.append({
            "id": e.id,
            "ts": e.ts,
            "block_id": e.block_id,
            "material_id": e.material_id,
            "event_type": e.event_type,
            "actor": e.actor,
            "delta": round(e.delta, 4),
            "confidence_after": round(e.confidence_after, 4),
            "note": e.note,
        })
    return {"status": "ok", "count": len(res), "data": res}


class TestRetrieveRequest(BaseModel):
    query: str
    top_k: int = 5
    alpha: float = 0.75


@router.post("/api/v1/test_retrieve")
async def test_retrieve_endpoint(
    request: Request,
    payload: TestRetrieveRequest,
) -> Dict[str, Any]:
    """在线检索体验测试端点：返回稠密分、BM25分、置信度加权、探索席位与来源标牌"""
    from src.engine.retrieve import HybridRetriever

    storage, _, _ = get_managers(request)
    retriever: HybridRetriever = getattr(request.app.state, "retriever", None)
    if retriever is None:
        retriever = HybridRetriever(storage)

    hits = retriever.retrieve(query=payload.query, top_k=payload.top_k, alpha=payload.alpha)
    res = []
    for block, final_score, meta in hits:
        badge = retriever.generate_source_badge([block])
        res.append({
            "block_id": block.id,
            "material_id": block.material_id,
            "context_prefix": block.context_prefix or "正文",
            "content": block.content,
            "conf_stored": round(block.conf_stored, 4),
            "eff_conf": round(meta.get("effective_conf", block.conf_stored), 4),
            "final_score": round(final_score, 4),
            "dense_sim": round(meta.get("dense_sim", 0.0), 4),
            "bm25_score": round(meta.get("bm25_score", 0.0), 4),
            "is_exploration_seat": meta.get("is_exploration_seat", False),
            "parent_scope_id": block.parent_scope_id,
            "source_badge": badge,
        })
    return {"status": "ok", "query": payload.query, "count": len(res), "data": res}
