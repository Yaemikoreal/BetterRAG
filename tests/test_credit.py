# -*- coding: utf-8 -*-
"""
单元测试：三轨信号源、显著性加权归因与 60s 隐式重问检测
"""
from datetime import datetime, timedelta
import pytest

from src.engine.storage import Storage
from src.engine.models import Material, Block, Citation
from src.engine.credit import CreditEngine, compute_query_similarity


@pytest.fixture
def credit_env(tmp_path):
    db_file = tmp_path / "test_credit.db"
    store = Storage(db_file)
    mat = Material(id="m_cred", title="考勤管理制度", source_path="attend.md", file_hash="h_cred")
    store.save_material(mat)
    credit_engine = CreditEngine(store)
    yield store, credit_engine
    store.close()


def test_query_similarity():
    q1 = "请问因公打车报销的具体时段是几点之后？"
    q2 = "请问因公打车几点之后可以报销？"
    sim = compute_query_similarity(q1, q2)
    assert sim >= 0.85

    q3 = "公司年假每年几天？"
    sim_diff = compute_query_similarity(q1, q3)
    assert sim_diff < 0.65


def test_salience_weighted_attribution(credit_env):
    """
    验证显着性加权归因结算（§4.3.2）：
    - 主干引用承担 80%，背景参考承担 20%
    - 稳定区累加 tenure
    - 学习区受机器写入边界 0.749 保护
    """
    store, credit_engine = credit_env

    # 块 1：稳定区老知识 (0.85, tenure=2)
    b_stable = Block(id="b_st", material_id="m_cred", content="打车核心规章", content_hash="h1", conf_stored=0.85, tenure=2)
    # 块 2：学习区新知识 (0.745)
    b_learn = Block(id="b_le", material_id="m_cred", content="打车补充背景", content_hash="h2", conf_stored=0.745, tenure=0)
    store.save_blocks([b_stable, b_learn])

    # 模拟一次会话的 citations：b_st 为主干 (0.8)，b_le 为背景 (0.2)
    session_id = "sess_pos_test"
    c1 = Citation(id="c1", session_id=session_id, question="打车报销时段", block_id="b_st", salience_weight=0.8)
    c2 = Citation(id="c2", session_id=session_id, question="打车报销时段", block_id="b_le", salience_weight=0.2)
    store.save_citation(c1)
    store.save_citation(c2)

    # 用户点赞 (pos: +0.02)
    # b_st 增加: +0.02 * 0.8 = +0.016 -> 0.866, tenure 变为 3
    # b_le 增加: +0.02 * 0.2 = +0.004 -> 0.745 + 0.004 = 0.749 (不超过 0.749 边界)
    results = credit_engine.process_feedback(session_id=session_id, outcome="pos")
    assert len(results) == 2

    b_st_after = store.get_block("b_st")
    assert abs(b_st_after.conf_stored - 0.866) < 1e-4
    assert b_st_after.tenure == 3

    b_le_after = store.get_block("b_le")
    assert abs(b_le_after.conf_stored - 0.749) < 1e-4

    # 再次点赞，验证机器写入边界硬上限 0.749
    credit_engine.process_feedback(session_id=session_id, outcome="pos")
    b_le_capped = store.get_block("b_le")
    assert b_le_capped.conf_stored == 0.749  # 绝不突破 0.749 跨入稳定区


def test_negative_feedback_dislike_and_escalate(credit_env):
    """验证点踩 (-0.02) 与转人工 (-0.03) 的加权扣分"""
    store, credit_engine = credit_env

    b = Block(id="b_neg", material_id="m_cred", content="错误答案制度块", content_hash="h_neg", conf_stored=0.60)
    store.save_block(b)

    session_id = "sess_neg_test"
    c = Citation(id="c_neg", session_id=session_id, question="提问测试", block_id="b_neg", salience_weight=1.0)
    store.save_citation(c)

    # 用户转人工 (neg_escalate: -0.03)
    credit_engine.process_feedback(session_id=session_id, outcome="neg_escalate")
    b_after = store.get_block("b_neg")
    assert abs(b_after.conf_stored - 0.57) < 1e-4


def test_implicit_reask_detection_within_60s(credit_env):
    """
    验证轨 3 隐式负反馈检测：
    60 秒内同 Session 出现相似度 >= 0.85 的同题重问，自动将前次回答标记为 neg_reask 并扣分 (-0.02)
    """
    store, credit_engine = credit_env

    b = Block(id="b_reask", material_id="m_cred", content="某模糊政策", content_hash="h_r", conf_stored=0.50)
    store.save_block(b)

    session_id = "sess_reask_test"
    t0 = datetime(2026, 10, 15, 14, 0, 0)

    # 首次提问
    c1 = Citation(
        id="c_r1",
        session_id=session_id,
        question="请问公司的打车报销时间到底是晚上几点之后？",
        block_id="b_reask",
        salience_weight=1.0,
        created_at=t0.isoformat()
    )
    store.save_citation(c1)

    # 1. 20 秒后同 Session 几乎同题重问（相似度 > 0.85）
    t_20s = t0 + timedelta(seconds=20)
    reask_q = "公司的打车报销时间具体是晚上几点之后啊？"
    handled = credit_engine.detect_and_handle_reask(
        session_id=session_id,
        current_question=reask_q,
        now=t_20s
    )
    assert handled is True

    # 检查扣分：0.50 - 0.02 = 0.48
    b_penalized = store.get_block("b_reask")
    assert abs(b_penalized.conf_stored - 0.48) < 1e-4

    # 2. 超过 60 秒（如 90 秒后）的重问不触发负反馈扣分
    t_90s = t_20s + timedelta(seconds=90)
    handled_late = credit_engine.detect_and_handle_reask(
        session_id=session_id,
        current_question="公司的打车报销时间具体是晚上几点之后啊？",
        now=t_90s
    )
    assert handled_late is False
    assert store.get_block("b_reask").conf_stored == 0.48  # 不再扣分


def test_adopt_citations_freshness(credit_env):
    """验证会话采纳活性保鲜：刷新 last_engagement_at，零积分"""
    store, credit_engine = credit_env

    t_old = "2026-01-01T00:00:00"
    b = Block(id="b_ad", material_id="m_cred", content="常用规章", content_hash="h_ad", conf_stored=0.85, last_engagement_at=t_old)
    store.save_block(b)

    session_id = "sess_adopt"
    c = Citation(id="c_ad", session_id=session_id, question="查询常用规章", block_id="b_ad")
    store.save_citation(c)

    credit_engine.adopt_citations(session_id=session_id)

    b_after = store.get_block("b_ad")
    assert b_after.conf_stored == 0.85  # 零积分
    assert b_after.last_engagement_at != t_old  # 刷新保鲜时钟
