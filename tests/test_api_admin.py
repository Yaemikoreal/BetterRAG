# -*- coding: utf-8 -*-
"""
集成测试：管理员审批路由、冲突与修正案接替、飞书卡片交互端点与治理看板
"""
import pytest
import pytest_asyncio
import httpx
from fastapi import FastAPI

from src.engine.storage import Storage
from src.engine.models import Material, Block, ConflictRecord
from src.engine.arbitrate import ConflictManager
from src.engine.probation import ProbationManager
from src.api.routes_admin import router as admin_router
from src.main import create_app


@pytest_asyncio.fixture
async def admin_client(tmp_path):
    db_file = tmp_path / "test_admin.db"
    store = Storage(db_file)
    cm = ConflictManager(store)
    pm = ProbationManager(store)

    mat = Material(id="m_adm", title="差旅与考勤", source_path="adm.md", file_hash="h_adm")
    store.save_material(mat)

    # 录入待审异常块
    b1 = Block(
        id="b_exc_taxi",
        material_id="m_adm",
        content="打车报销时段调整为晚上20:00之后。",
        content_hash="h_e1",
        conf_stored=0.65,
        status="pending",
        pending_reason="与权威基石存在时段排他性互斥"
    )
    # 录入权威基石
    b2 = Block(
        id="b_def_taxi",
        material_id="m_adm",
        content="打车报销时段严格限制为晚上21:00之后。",
        content_hash="h_e2",
        conf_stored=0.85,
        status="active"
    )
    store.save_blocks([b1, b2])

    # 录入冲突记录
    rec = ConflictRecord(
        id="conf_test_001",
        defender_id="b_def_taxi",
        challenger_id="b_exc_taxi",
        defender_hash="h_e2",
        challenger_hash="h_e1",
        clash_count=3,
        verdict="conflict",
        status="notified"
    )
    store.save_conflict(rec)

    app = create_app()
    app.state.storage = store
    app.state.conflict_manager = cm
    app.state.probation_manager = pm

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, store, cm, pm
    store.close()


@pytest.mark.asyncio
async def test_get_dashboard_stats_and_html(admin_client):
    """验证健康度统计 API 与看板 HTML 端点"""
    client, _, _, _ = admin_client

    # 1. 验证 HTML 看板页面
    resp_html = await client.get("/dashboard")
    assert resp_html.status_code == 200
    assert "BetterRAG · 自治知识库治理看板" in resp_html.text

    # 2. 验证统计接口
    resp_stats = await client.get("/api/v1/dashboard/stats")
    assert resp_stats.status_code == 200
    data = resp_stats.json()["data"]
    assert data["total_materials"] == 1
    assert data["total_blocks"] == 2
    assert data["stable_blocks"] == 1
    assert data["pending_blocks"] == 1
    assert data["active_conflicts"] == 1


@pytest.mark.asyncio
async def test_exceptions_list_and_adjudicate(admin_client):
    """验证获取异常列表与人工裁决（pass / reject）"""
    client, store, _, _ = admin_client

    # 1. 查询异常列表
    resp = await client.get("/api/v1/exceptions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert data["data"][0]["id"] == "b_exc_taxi"

    # 2. 裁决通过 (pass) -> 升至 0.85 稳定区
    adj_resp = await client.post("/api/v1/exceptions/b_exc_taxi/adjudicate", json={
        "action": "pass",
        "note": "总监核准特批"
    })
    assert adj_resp.status_code == 200
    assert adj_resp.json()["conf_stored"] == 0.85
    assert adj_resp.json()["block_status"] == "active"

    b_after = store.get_block("b_exc_taxi")
    assert b_after.conf_stored == 0.85
    assert b_after.status == "active"


@pytest.mark.asyncio
async def test_conflicts_list_and_amend_replace(admin_client):
    """验证冲突台账展示与一键原子接替"""
    client, store, _, _ = admin_client

    # 1. 获取冲突台账
    resp = await client.get("/api/v1/conflicts")
    assert resp.status_code == 200
    conf_list = resp.json()["data"]
    assert len(conf_list) >= 1
    assert conf_list[0]["id"] == "conf_test_001"

    # 2. 调用一键原子接替
    rep_resp = await client.post("/api/v1/amend/conf_test_001/replace")
    assert rep_resp.status_code == 200
    assert rep_resp.json()["status"] == "ok"

    # 验证挑战者晋升，防守者归档
    b_chal = store.get_block("b_exc_taxi")
    b_def = store.get_block("b_def_taxi")
    assert b_chal.conf_stored == 0.85
    assert b_chal.status == "active"
    assert b_def.status == "archived"
    assert b_def.superseded_by == "b_exc_taxi"


@pytest.mark.asyncio
async def test_feishu_webhook_interactive_callback(admin_client):
    """验证飞书 Challenge 握手与交互式卡片点击按钮回调"""
    client, store, _, _ = admin_client

    # 1. 飞书首次握手 Challenge
    resp_challenge = await client.post("/api/v1/webhook/feishu/interactive", json={
        "type": "url_verification",
        "challenge": "feishu_token_check_9988"
    })
    assert resp_challenge.status_code == 200
    assert resp_challenge.json()["challenge"] == "feishu_token_check_9988"

    # 2. 模拟卡片点击【一键接替】
    resp_action = await client.post("/api/v1/webhook/feishu/interactive", json={
        "action": {
            "value": {
                "action": "replace",
                "conflict_id": "conf_test_001"
            }
        }
    })
    assert resp_action.status_code == 200
    assert "toast" in resp_action.json()
    assert resp_action.json()["toast"]["type"] == "success"

    # 验证数据库原子交接生效
    b_chal = store.get_block("b_exc_taxi")
    assert b_chal.conf_stored == 0.85


@pytest.mark.asyncio
async def test_list_materials_blocks_and_events(admin_client):
    """验证获取材料列表、知识切片全景与审计事件流接口"""
    client, _, _, _ = admin_client

    # 1. 材料列表
    resp_mats = await client.get("/api/v1/materials")
    assert resp_mats.status_code == 200
    mats_data = resp_mats.json()["data"]
    assert len(mats_data) == 1
    assert mats_data[0]["title"] == "差旅与考勤"
    assert mats_data[0]["total_blocks"] == 2

    # 2. 切片列表
    resp_blocks = await client.get("/api/v1/blocks?limit=10")
    assert resp_blocks.status_code == 200
    blocks_data = resp_blocks.json()["data"]
    assert len(blocks_data) == 2
    assert "eff_conf" in blocks_data[0]
    assert "zone" in blocks_data[0]

    # 3. 审计事件
    resp_events = await client.get("/api/v1/events?limit=10")
    assert resp_events.status_code == 200
    assert "data" in resp_events.json()


@pytest.mark.asyncio
async def test_test_retrieve_endpoint(admin_client):
    """验证在线检索体验端点返回评分细分与来源标牌"""
    client, _, _, _ = admin_client

    resp = await client.post("/api/v1/test_retrieve", json={
        "query": "打车报销时段",
        "top_k": 3,
        "alpha": 0.75
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["count"] > 0
    hit = data["data"][0]
    assert "final_score" in hit
    assert "dense_sim" in hit
    assert "source_badge" in hit
