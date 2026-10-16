# -*- coding: utf-8 -*-
"""
BetterRAG OpenAI 兼容问答接口与三轨信号端点
严格实现 docs/BetterRAG生产级开发实施方案.md §5.1 与 §5.2
"""
from __future__ import annotations

import os
import json
import time
import uuid
import asyncio
from typing import List, Optional, Dict, Any, AsyncGenerator

from fastapi import APIRouter, HTTPException, Request, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from src.engine.storage import Storage
from src.engine.retrieve import HybridRetriever
from src.engine.credit import CreditEngine
from src.engine.models import Citation, utcnow_str

router = APIRouter()


# ==================== 请求与响应模型 ====================

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = "deepseek-chat"
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    stream: Optional[bool] = False
    session_id: Optional[str] = None


class FeedbackRequest(BaseModel):
    session_id: str
    outcome: str = Field(..., description="pos | neg_dislike | neg_escalate | neg_reask")
    note: Optional[str] = ""


class SyncRequest(BaseModel):
    workspace_dir: Optional[str] = "data/workspace"


# ==================== 接口实现 ====================

def get_services(request: Request) -> Tuple[Storage, HybridRetriever, CreditEngine]:
    storage: Storage = request.app.state.storage
    retriever: HybridRetriever = request.app.state.retriever
    credit_engine: CreditEngine = request.app.state.credit_engine
    return storage, retriever, credit_engine


async def generate_mock_or_llm_response(
    messages: List[ChatMessage],
    context_str: str,
    stream: bool = False,
) -> AsyncGenerator[str, None] | str:
    """
    大模型生成逻辑：
    若环境变量配置了 OPENAI_API_KEY，通过 httpx 调用真实远端服务；
    否则平稳降级为本地上下文智能归纳输出（确保零外部依赖下立即可用）。
    """
    api_key = os.getenv("OPENAI_API_KEY")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("CHAT_MODEL", "deepseek-chat")

    # 优先调用远端真实大模型
    if api_key and api_key != "sk-xxxxxxxxxxxxxxxxxxxxxxxx":
        import httpx

        system_instruction = (
            "你是一个严谨的企业知识库助理。请严格基于以下参考制度内容如实回答员工问题，"
            "切勿编造没有依据的规定：\n\n"
            f"【参考制度内容】：\n{context_str}\n"
        )
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system_instruction}] + [
                {"role": m.role, "content": m.content} for m in messages
            ],
            "stream": stream,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            if not stream:
                resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
                data = resp.json()
                return data["choices"][0]["message"]["content"]
            else:
                async def stream_remote():
                    async with client.stream(
                        "POST", f"{base_url}/chat/completions", json=payload, headers=headers
                    ) as r:
                        async for line in r.aiter_lines():
                            if line.startswith("data: "):
                                yield line[6:]

                return stream_remote()

    # 无 API Key 时的本地智能保真归纳（保证离线与单测 100% 可用）
    user_query = messages[-1].content if messages else ""
    local_answer = (
        f"根据相关知识库规定，关于您咨询的问题：“{user_query}”，主要规章细则如下：\n\n"
        f"{context_str.strip()}\n\n"
        "以上规定请参照公司制度严格执行。"
    )

    if not stream:
        return local_answer
    else:
        async def stream_local():
            chunk_size = 15
            for i in range(0, len(local_answer), chunk_size):
                yield local_answer[i : i + chunk_size]
                await asyncio.sleep(0.01)

        return stream_local()


@router.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    """
    OpenAI 规范问答聊天端点
    支持流式输出 (SSE)。自动执行自适应混合检索、探索召回、父子捆绑，
    并在生成答案文末自动追加入库制度与权威置信度标牌。
    """
    storage, retriever, credit_engine = get_services(request)

    # 1. 会话 ID 管理与提问提取
    session_id = req.session_id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
    user_query = ""
    for m in reversed(req.messages):
        if m.role == "user":
            user_query = m.content
            break

    if not user_query:
        raise HTTPException(status_code=400, detail="未检测到有效的用户提问内容 (role='user')")

    # 2. 轨 3 隐式负反馈检测：60s 内同 Session 重问自动扣分
    credit_engine.detect_and_handle_reask(session_id=session_id, current_question=user_query)

    # 3. 自适应混合检索 + 探索召回保全席位
    hit_results = retriever.retrieve(query=user_query, top_k=5)
    hit_blocks = [h[0] for h in hit_results]

    # 4. 上下文父子捆绑组装（子块自动引入前置条件父块）
    assembled_blocks = retriever.assemble_context(hit_blocks)

    # 5. 记录问答引用 (citations) 并执行活性保鲜 (adopt)
    now_ts = utcnow_str()
    for rank, b in enumerate(assembled_blocks):
        # 显着性加权：主干引用承担 80%，背景参考承担 20%
        salience = 0.8 if rank == 0 else 0.2
        cite_id = f"cite_{session_id}_{b.id}_{rank}"
        cite = Citation(
            id=cite_id,
            session_id=session_id,
            question=user_query,
            block_id=b.id,
            salience_weight=salience,
            adopted=True,
            created_at=now_ts,
        )
        storage.save_citation(cite)

    # 活性保鲜（重置保鲜时钟，零积分）
    credit_engine.adopt_citations(session_id=session_id)

    # 6. 生成透明来源置信度标牌
    source_badge = retriever.generate_source_badge(assembled_blocks)

    # 7. 构建上下文提示文本
    context_parts = []
    for idx, b in enumerate(assembled_blocks):
        prefix = f"【参考 {idx + 1}】"
        if b.context_prefix:
            prefix += f" [{b.context_prefix}]"
        context_parts.append(f"{prefix}\n{b.content}")
    context_str = "\n\n".join(context_parts)

    created_timestamp = int(time.time())

    # 8. 处理流式与非流式响应
    if not req.stream:
        answer_body = await generate_mock_or_llm_response(req.messages, context_str, stream=False)
        final_content = f"{answer_body}{source_badge}"

        return JSONResponse(
            content={
                "id": session_id,
                "object": "chat.completion",
                "created": created_timestamp,
                "model": req.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": final_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": len(context_str),
                    "completion_tokens": len(final_content),
                    "total_tokens": len(context_str) + len(final_content),
                },
                "citations": [
                    {
                        "block_id": b.id,
                        "salience_weight": 0.8 if idx == 0 else 0.2,
                        "conf_stored": b.conf_stored,
                    }
                    for idx, b in enumerate(assembled_blocks)
                ],
            }
        )
    else:
        stream_gen = await generate_mock_or_llm_response(req.messages, context_str, stream=True)

        async def sse_event_stream():
            # 逐步推送大模型内容
            if hasattr(stream_gen, "__aiter__"):
                async for chunk in stream_gen:
                    chunk_payload = {
                        "id": session_id,
                        "object": "chat.completion.chunk",
                        "created": created_timestamp,
                        "model": req.model,
                        "choices": [
                            {"index": 0, "delta": {"content": chunk}, "finish_reason": None}
                        ],
                    }
                    yield f"data: {json.dumps(chunk_payload, ensure_ascii=False)}\n\n"

            # 追加来源透明标牌
            badge_payload = {
                "id": session_id,
                "object": "chat.completion.chunk",
                "created": created_timestamp,
                "model": req.model,
                "choices": [
                    {"index": 0, "delta": {"content": source_badge}, "finish_reason": None}
                ],
            }
            yield f"data: {json.dumps(badge_payload, ensure_ascii=False)}\n\n"

            # 推送终止符
            stop_payload = {
                "id": session_id,
                "object": "chat.completion.chunk",
                "created": created_timestamp,
                "model": req.model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield f"data: {json.dumps(stop_payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse_event_stream(), media_type="text/event-stream")


@router.post("/api/v1/feedback")
async def submit_feedback(req: FeedbackRequest, request: Request):
    """
    显式反馈收集端点（轨 1 原生点赞/点踩与轨 2 回调）
    """
    storage, _, credit_engine = get_services(request)
    results = credit_engine.process_feedback(
        session_id=req.session_id,
        outcome=req.outcome,
        note=req.note or f"API用户反馈·{req.outcome}"
    )
    return {
        "status": "ok",
        "session_id": req.session_id,
        "outcome": req.outcome,
        "affected_blocks": len(results),
        "details": results,
    }


@router.post("/api/v1/sync")
async def trigger_sync(req: SyncRequest, request: Request):
    """
    目录增量同步端点
    """
    storage: Storage = request.app.state.storage
    from src.engine.ingest import IngestionEngine

    ingestion_engine = IngestionEngine(storage)
    synced = ingestion_engine.sync_workspace(req.workspace_dir or "data/workspace")
    return {
        "status": "ok",
        "synced_materials": len(synced),
        "active_blocks": len(storage.get_all_active_blocks()),
    }


@router.get("/api/v1/health")
async def health_check(request: Request):
    """
    服务健康检查端点
    """
    storage: Storage = request.app.state.storage
    return {
        "status": "healthy",
        "materials_count": len(storage.get_all_materials()),
        "active_blocks_count": len(storage.get_all_active_blocks()),
        "events_count": len(storage.get_events(limit=10)),
        "timestamp": utcnow_str(),
    }
