# -*- coding: utf-8 -*-
"""
单元测试：自适应混合检索、探索召回席位与父子上下文组装
"""
from datetime import datetime
import pytest

from src.engine.storage import Storage
from src.engine.models import Material, Block
from src.engine.retrieve import (
    HybridRetriever,
    cosine_similarity,
    text_overlap_similarity,
)


@pytest.fixture
def retrieve_env(tmp_path):
    db_file = tmp_path / "test_retrieve.db"
    store = Storage(db_file)
    mat = Material(id="m_ret", title="差旅与报销制度", source_path="travel.md", file_hash="h_ret")
    store.save_material(mat)
    retriever = HybridRetriever(store)
    yield store, retriever
    store.close()


def test_text_similarity_metrics():
    # 测试余弦相似度与重叠度纯函数
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]
    v3 = [0.0, 1.0, 0.0]
    assert abs(cosine_similarity(v1, v2) - 1.0) < 1e-4
    assert abs(cosine_similarity(v1, v3) - 0.0) < 1e-4

    s1 = "员工因公打车需在晚上21点之后"
    s2 = "因公打车需在晚上21点之后方可报销"
    sim = text_overlap_similarity(s1, s2)
    assert sim > 0.6


def test_retrieve_status_filter(retrieve_env):
    """验证检索过滤：仅 active 和 pending 态参与召回，archived 和 suspended 绝对不参与"""
    store, retriever = retrieve_env

    b_active = Block(id="b_act", material_id="m_ret", content="正常打车制度说明", content_hash="h1", status="active", conf_stored=0.85)
    b_pending = Block(id="b_pen", material_id="m_ret", content="待处置打车打卡细则", content_hash="h2", status="pending", conf_stored=0.74)
    b_archived = Block(id="b_arc", material_id="m_ret", content="已作废打车旧标准", content_hash="h3", status="archived", conf_stored=0.0)
    b_suspended = Block(id="b_sus", material_id="m_ret", content="挂起保全打车新规", content_hash="h4", status="suspended", conf_stored=0.30)
    store.save_blocks([b_active, b_pending, b_archived, b_suspended])

    hits = retriever.retrieve("打车制度", top_k=10)
    hit_ids = [h[0].id for h in hits]

    assert "b_act" in hit_ids
    assert "b_pen" in hit_ids
    assert "b_arc" not in hit_ids
    assert "b_sus" not in hit_ids


def test_exploration_seats(retrieve_env):
    """
    验证探索召回保全席位 (Exploration Seats)：
    当 Top K-1 均为高置信度的老知识时，强制将第 K 席位保留给语义相关度高的新知识块，
    打破新知识由于置信度低无法曝光的死锁。
    """
    store, retriever = retrieve_env

    # 3 个高置信度稳定区老知识 (conf=0.85)，均与打车高度相关
    b_s1 = Block(id="bs1", material_id="m_ret", content="因公出差打车交通发票报销规定", content_hash="hs1", conf_stored=0.85)
    b_s2 = Block(id="bs2", material_id="m_ret", content="因公出差打车市内交通报销细则", content_hash="hs2", conf_stored=0.85)
    b_s3 = Block(id="bs3", material_id="m_ret", content="因公出差打车平台专车报销要求", content_hash="hs3", conf_stored=0.85)

    # 1 个学习区新知识 (conf=0.30)，同样高相关，但因置信度低，纯分数只能排第 4
    b_new = Block(id="b_new_taxi", material_id="m_ret", content="因公出差打车滴滴企业版补充通知", content_hash="h_new", conf_stored=0.30)

    store.save_blocks([b_s1, b_s2, b_s3, b_new])

    # Top-K = 3，查询出差打车报销
    hits = retriever.retrieve("因公出差打车报销", top_k=3)
    assert len(hits) == 3

    hit_ids = [h[0].id for h in hits]
    # 前两席属于稳定区老知识 bs1, bs2
    assert hits[0][0].conf_stored >= 0.75
    assert hits[1][0].conf_stored >= 0.75
    # 第 3 席位 (hits[2]) 由探索机制保全给高相似度的学习区新块 b_new_taxi
    assert hits[2][0].id == "b_new_taxi"
    assert "b_new_taxi" in hit_ids


def test_assemble_context_with_parent_condition(retrieve_env):
    """验证上下文组装：命中的子块含有父级前置条件块时，强制把父块捆绑加入上下文"""
    store, retriever = retrieve_env

    parent_block = Block(
        id="p_rule",
        material_id="m_ret",
        content="前置条件：凡试用期员工不享受打车报销补贴",
        content_hash="hp",
        is_condition_header=True,
        status="active",
        conf_stored=0.85
    )
    child_block = Block(
        id="c_rule",
        material_id="m_ret",
        parent_scope_id="p_rule",
        content="转正员工晚上21点后可凭发票报销市内打车费",
        content_hash="hc",
        status="active",
        conf_stored=0.85
    )
    store.save_blocks([parent_block, child_block])

    # 仅命中子块时，组装上下文会自动引入父块
    assembled = retriever.assemble_context([child_block])
    assert len(assembled) == 2
    assert assembled[0].id == "p_rule"
    assert assembled[1].id == "c_rule"


def test_generate_source_badge(retrieve_env):
    """验证来源透明化标牌生成"""
    store, retriever = retrieve_env

    b_stable = Block(id="b1", material_id="m_ret", content="权威细则", content_hash="h1", conf_stored=0.85, context_prefix="第四章 > 报销")
    b_learn = Block(id="b2", material_id="m_ret", content="试用细则", content_hash="h2", conf_stored=0.30, context_prefix="通知")

    badge = retriever.generate_source_badge([b_stable, b_learn])
    assert "📌 **参考制度与来源**：" in badge
    assert "[权威基石·已复核] 《差旅与报销制度》 (第四章 > 报销)" in badge
    assert "[新知识·试用期] 《差旅与报销制度》 (通知)" in badge
