# -*- coding: utf-8 -*-
"""
单元测试：两级冲突检测流水线 (Pipe-1 静态摄取筛查 + Pipe-2 检索期动态对冲) 与制度修正案接替
严格对齐 docs/BetterRAG生产级开发实施方案.md §4.5
"""
import pytest

from src.engine.storage import Storage
from src.engine.models import Material, Block
from src.engine.arbitrate import ConflictManager
from src.engine.conflict_pipe import ConflictPipeline


@pytest.fixture
def conflict_env(tmp_path):
    db_file = tmp_path / "test_conf_pipe.db"
    store = Storage(db_file)
    mat_old = Material(id="m_old", title="2024差旅制度", source_path="old.md", file_hash="h_old")
    mat_new = Material(id="m_new", title="2025差旅制度", source_path="new.md", file_hash="h_new")
    store.save_material(mat_old)
    store.save_material(mat_new)

    cm = ConflictManager(store)
    pipe = ConflictPipeline(store, cm)

    yield store, cm, pipe
    store.close()


def test_pipe1_static_ingest_conflict_detection(conflict_env):
    """Pipe-1: 验证新块入库时，自动与存量稳定基石进行静态比对并捕获互斥冲突"""
    store, cm, pipe = conflict_env

    # 存量稳定基石 (0.85)
    b_old = Block(
        id="b_old_taxi",
        material_id="m_old",
        content="因公打车必须在晚上21:00之后方可报销，非紧急情况不得提前打车。",
        content_hash="h_old_taxi",
        conf_stored=0.85,
        context_prefix="差旅制度 > 打车规定",
        status="active"
    )
    store.save_block(b_old)

    # 待审新知识块 (调整时段)
    b_new = Block(
        id="b_new_taxi",
        material_id="m_new",
        content="因公打车时段调整为晚上20:00之后，修改为执行新标准并废止旧时段限制。",
        content_hash="h_new_taxi",
        conf_stored=0.30,
        context_prefix="补充通知 > 打车时段",
        status="active"
    )

    conflicts = pipe.pipe1_static_ingest_scan([b_new], sim_threshold=0.60)
    assert len(conflicts) == 1
    rec = conflicts[0]
    assert rec.defender_id == "b_old_taxi"
    assert rec.challenger_id == "b_new_taxi"
    assert rec.verdict == "conflict"
    assert rec.clash_count == 1
    assert rec.status == "open"

    # 再次执行扫描，双方 Hash 未变，验证直接命中缓存
    conflicts_cached = pipe.pipe1_static_ingest_scan([b_new], sim_threshold=0.60)
    assert len(conflicts_cached) == 1
    assert conflicts_cached[0].id == rec.id
    # 验证二次粗筛命中缓存时仅累加计数器至 2，零 LLM Judge 重复调用
    assert conflicts_cached[0].clash_count == 2


def test_pipe2_retrieval_dynamic_conflict_counting(conflict_env):
    """Pipe-2: 验证检索期多块同时召回时，动态检测跨分区互斥并累加 clash_count"""
    store, cm, pipe = conflict_env

    b_defender = Block(
        id="b_def",
        material_id="m_old",
        content="出差住宿标准上限为每人每天600元整，超出部分不予报销。",
        content_hash="h_def",
        conf_stored=0.85,
        status="active"
    )
    b_challenger = Block(
        id="b_chal",
        material_id="m_new",
        content="出差住宿标准调整为每人每天800元整，修改为执行新标准。",
        content_hash="h_chal",
        conf_stored=0.55,
        status="active"
    )
    store.save_blocks([b_defender, b_challenger])

    hit_blocks = [b_defender, b_challenger]

    # 第 1 次碰撞
    conf1 = pipe.pipe2_retrieval_dynamic_check(hit_blocks, query="住宿标准")
    assert len(conf1) == 1
    assert conf1[0].clash_count == 1

    # 第 2 次碰撞
    conf2 = pipe.pipe2_retrieval_dynamic_check(hit_blocks, query="住宿标准是多少")
    assert len(conf2) == 1
    assert conf2[0].clash_count == 2
    assert conf2[0].status == "open"

    # 第 3 次碰撞 -> 达到阈值，触发 notified
    conf3 = pipe.pipe2_retrieval_dynamic_check(hit_blocks, query="住宿报销上限")
    assert len(conf3) == 1
    assert conf3[0].clash_count == 3
    assert conf3[0].status == "notified"

    # 验证红灯告警获取
    red_alerts = pipe.get_red_alert_conflicts()
    assert len(red_alerts) >= 1
    assert any(a.id == conf3[0].id for a in red_alerts)


def test_amend_replace_atomic_transition(conflict_env):
    """验证制度修正案一键原子交接（新规转正 0.85，旧规归档并建立取代链）"""
    store, cm, pipe = conflict_env

    b_def = Block(
        id="b_sub_old",
        material_id="m_old",
        content="员工年假不可跨自然年累计使用，当年未休视为自愿放弃。",
        content_hash="h_s1",
        conf_stored=0.85,
        status="active"
    )
    b_chal = Block(
        id="b_sub_new",
        material_id="m_new",
        content="员工年假调整为可以跨自然年累计使用，当年未休顺延至次年，修改为执行新规。",
        content_hash="h_s2",
        conf_stored=0.45,
        status="active"
    )
    store.save_blocks([b_def, b_chal])

    # 触发冲突并入库
    conf = pipe.pipe2_retrieval_dynamic_check([b_def, b_chal], query="年假")
    conflict_id = conf[0].id

    # 执行原子接替
    success = cm.amend_replace(conflict_id)
    assert success is True

    # 验证挑战者晋升
    b_chal_after = store.get_block("b_sub_new")
    assert b_chal_after.conf_stored == 0.85
    assert b_chal_after.status == "active"

    # 验证防守者归档与取代链
    b_def_after = store.get_block("b_sub_old")
    assert b_def_after.status == "archived"
    assert b_def_after.superseded_by == "b_sub_new"

    # 验证冲突记录状态
    c_after = store.get_conflict("b_sub_old", "b_sub_new")
    assert c_after.status == "resolved"
