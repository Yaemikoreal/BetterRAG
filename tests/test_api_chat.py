# -*- coding: utf-8 -*-
"""
集成测试：FastAPI OpenAI 兼容接口 (/v1/chat/completions) 与反馈端点
"""
import pytest
import pytest_asyncio
import httpx
from fastapi import FastAPI

from src.engine.storage import Storage
from src.engine.retrieve import HybridRetriever
from src.engine.credit import CreditEngine
from src.engine.models import Material, Block
from src.api.routes_chat import router as chat_router


@pytest_asyncio.fixture
async def api_client(tmp_path):
    db_file = tmp_path / "test_api.db"
    store = Storage(db_file)
    retriever = HybridRetriever(store)
    credit_engine = CreditEngine(store)

    # 录入权威测试制度
    mat = Material(id="m_api", title="差旅管理办法", source_path="travel.md", file_hash="h_api")
    store.save_material(mat)

    b1 = Block(
        id="b_api_taxi",
        material_id="m_api",
        content="因公打车需在晚上21:00之后方可凭正式发票报销。",
        content_hash="h_api_1",
        conf_stored=0.85,
        context_prefix="第二章 > 交通报销",
        status="active"
    )
    b2 = Block(
        id="b_api_hotel",
        material_id="m_api",
        content="出差住宿标准上限为每人每天600元整。",
        content_hash="h_api_2",
        conf_stored=0.85,
        context_prefix="第一章 > 住宿标准",
        status="active"
    )
    store.save_blocks([b1, b2])

    test_app = FastAPI()
    test_app.state.storage = store
    test_app.state.retriever = retriever
    test_app.state.credit_engine = credit_engine
    test_app.include_router(chat_router)

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, store
    store.close()


@pytest.mark.asyncio
async def test_api_health_check(api_client):
    client, _ = api_client
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["active_blocks_count"] == 2


@pytest.mark.asyncio
async def test_chat_completions_json(api_client):
    """验证 OpenAI 格式 JSON 响应与文末来源标牌注入"""
    client, store = api_client

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "user", "content": "请问晚上几点之后可以打车报销？"}
        ],
        "stream": False,
        "session_id": "sess_api_test"
    }

    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    data = resp.json()

    assert data["object"] == "chat.completion"
    assert data["id"] == "sess_api_test"
    content = data["choices"][0]["message"]["content"]

    # 验证回答末尾成功注入了透明来源标牌
    assert "📌 **参考制度与来源**：" in content
    assert "[权威基石·已复核] 《差旅管理办法》 (第二章 > 交通报销)" in content

    # 验证 citations 记录落地且主干权重为 0.8
    cites = store.get_citations_by_session("sess_api_test")
    assert len(cites) > 0
    assert cites[0].salience_weight == 0.8
    assert cites[0].block_id == "b_api_taxi"


@pytest.mark.asyncio
async def test_chat_completions_streaming_sse(api_client):
    """验证 SSE 流式输出格式"""
    client, _ = api_client

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "user", "content": "出差住宿标准是多少？"}
        ],
        "stream": True,
        "session_id": "sess_stream_test"
    }

    resp = await client.post("/v1/chat/completions", json=payload)
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    lines = resp.text.split("\n")
    data_lines = [l for l in lines if l.startswith("data: ")]
    assert len(data_lines) > 2
    assert "data: [DONE]" in lines


@pytest.mark.asyncio
async def test_feedback_endpoint_positive(api_client):
    """验证通过 /api/v1/feedback 端点收集点赞"""
    client, store = api_client

    # 先发起一次问答生成 citation
    await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "打车报销"}],
        "session_id": "sess_fb"
    })

    b_before = store.get_block("b_api_taxi")
    initial_conf = b_before.conf_stored
    initial_tenure = b_before.tenure

    # 提交点赞反馈
    fb_resp = await client.post("/api/v1/feedback", json={
        "session_id": "sess_fb",
        "outcome": "pos",
        "note": "回答非常清楚准确"
    })
    assert fb_resp.status_code == 200

    b_after = store.get_block("b_api_taxi")
    # 主干引用承担 80%: +0.02 * 0.8 = +0.016
    assert abs(b_after.conf_stored - (initial_conf + 0.016)) < 1e-4
    assert b_after.tenure == initial_tenure + 1


@pytest.mark.asyncio
async def test_reask_detection_via_chat_api(api_client):
    """验证在同一 Session 连续发送同题重问自动触发隐式负反馈"""
    client, store = api_client

    session_id = "sess_reask_api"

    # 第一次提问
    await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "请问因公打车报销时间到底是几点之后？"}],
        "session_id": session_id
    })

    b_before = store.get_block("b_api_taxi")
    conf_before = b_before.conf_stored

    # 立即（<60秒）发送高度相似的问题
    await client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "因公打车报销时间到底是几点之后啊？"}],
        "session_id": session_id
    })

    # 验证前一次问答被自动标记为 neg_reask 并扣分: -0.02 * 0.8 = -0.016
    b_after = store.get_block("b_api_taxi")
    assert abs(b_after.conf_stored - (conf_before - 0.016)) < 1e-4

    # 检查审计日志中记录了隐式负反馈
    events = store.get_events(block_id="b_api_taxi")
    assert any("同题重问" in (e.note or "") for e in events)
