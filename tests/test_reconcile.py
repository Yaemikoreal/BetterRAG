# -*- coding: utf-8 -*-
"""
单元测试：每日定时衰减协调任务与完整三段式降级
"""
from datetime import datetime, timedelta
import pytest

from src.engine.storage import Storage
from src.engine.models import Material, Block
from src.engine.reconcile import DailyReconciler


@pytest.fixture
def store(tmp_path):
    db_file = tmp_path / "test_reconcile.db"
    s = Storage(db_file)
    mat = Material(id="m_rec", title="协调测试", source_path="rec.md", file_hash="h_rec")
    s.save_material(mat)
    yield s
    s.close()


def test_reconcile_evergreen_and_frozen_skip(store):
    now = datetime(2026, 10, 15, 3, 0, 0)
    reconciler = DailyReconciler(store)

    # 1. 常青块：闲置 200 天不衰减
    b_ever = Block(
        id="b_ever",
        material_id="m_rec",
        content="常青总章程",
        content_hash="h_ever",
        is_evergreen=True,
        conf_stored=0.85,
        last_engagement_at=(now - timedelta(days=200)).isoformat(),
        status="active"
    )
    store.save_block(b_ever)

    # 2. 举证期冻结块：闲置 30 天处于冻结期内
    b_froz = Block(
        id="b_froz",
        material_id="m_rec",
        content="举证期新知识",
        content_hash="h_froz",
        conf_stored=0.65,
        last_engagement_at=(now - timedelta(days=30)).isoformat(),
        probation_until=(now + timedelta(days=5)).isoformat(),
        status="active"
    )
    store.save_block(b_froz)

    stats = reconciler.reconcile(now=now)
    assert stats["skipped_evergreen"] == 1
    assert stats["skipped_frozen"] == 1

    # 验证置信度与状态完全不受影响
    b_ever_after = store.get_block("b_ever")
    assert b_ever_after.conf_stored == 0.85
    assert b_ever_after.status == "active"

    b_froz_after = store.get_block("b_froz")
    assert b_froz_after.conf_stored == 0.65
    assert b_froz_after.status == "active"


def test_reconcile_three_stage_demotion(store):
    """
    完整三段式降级与棘轮重锚验证：
    稳定区 (0.85) -> 闲置超期跌破 0.75 -> 待处置 (0.749, pending)
    -> 待处置期满无证据跌破 0.60 -> 滑回学习区 (0.599, active)
    -> 长期闲置跌破 0.05 -> 自动归档 (archived) 并级联挂起子块
    """
    t0 = datetime(2026, 1, 1, 3, 0, 0)
    reconciler = DailyReconciler(store)

    # 创建 tenure=0 的稳定区块 (宽限期 80 天，半衰期 365 天)
    parent_block = Block(
        id="b_parent",
        material_id="m_rec",
        content="限制前置条款",
        content_hash="h_parent",
        conf_stored=0.85,
        tenure=0,
        last_engagement_at=t0.isoformat(),
        status="active",
        is_condition_header=True
    )
    child_block = Block(
        id="b_child",
        material_id="m_rec",
        parent_scope_id="b_parent",
        content="依附于父块的细则",
        content_hash="h_child",
        conf_stored=0.85,
        tenure=0,
        last_engagement_at=t0.isoformat(),
        status="active"
    )
    store.save_block(parent_block)
    store.save_block(child_block)

    # 1. 宽限期内 (第 50 天)：不衰减
    t1 = t0 + timedelta(days=50)
    stats1 = reconciler.reconcile(now=t1)
    assert stats1["demoted_pending"] == 0
    assert store.get_block("b_parent").conf_stored == 0.85

    # 2. 第一段降级：闲置 160 天
    # 宽限 80 天，超期 80 天。Freshness = 0.5^(80/365) = 0.859
    # eff_conf = 0.85 * 0.859 = 0.730 < 0.75
    # 触发第一段降级：status -> 'pending', conf_stored -> 0.730 (<= 0.749)
    t2 = t0 + timedelta(days=160)
    stats2 = reconciler.reconcile(now=t2)
    assert stats2["demoted_pending"] >= 1

    b_pending = store.get_block("b_parent")
    assert b_pending.status == "pending"
    assert b_pending.conf_stored <= 0.749
    # 确认重锚了基线
    assert b_pending.last_engagement_at == t2.isoformat()

    # 3. 第二段降级：在待处置状态下继续闲置
    # 从 t2 起算，半衰期变为学习区 90 天，宽限期 14 天
    # 闲置 45 天后：超期 31 天，Freshness = 0.5^(31/90) = 0.787
    # eff_conf = 0.730 * 0.787 = 0.574 < 0.60
    # 触发第二段降级：status -> 'active' (退回学习区), conf_stored -> 0.574 (<= 0.599)
    t3 = t2 + timedelta(days=45)
    stats3 = reconciler.reconcile(now=t3)
    assert stats3["demoted_learn"] >= 1

    b_learn = store.get_block("b_parent")
    assert b_learn.status == "active"
    assert b_learn.conf_stored <= 0.599
    # 确认再次重锚基线
    assert b_learn.last_engagement_at == t3.isoformat()

    # 4. 第三段降级：父块长期无有效证据，跌破 0.05 废弃线
    # 闲置 350 天，eff_conf 远低于 0.05
    t4 = t3 + timedelta(days=350)
    # 子块在此期间依然活跃（闲置仅 10 天，原本属于活跃稳定区）
    b_child_cur = store.get_block("b_child")
    b_child_cur.last_engagement_at = (t4 - timedelta(days=10)).isoformat()
    store.save_block(b_child_cur)

    stats4 = reconciler.reconcile(now=t4)
    assert stats4["archived"] >= 1

    b_archived = store.get_block("b_parent")
    assert b_archived.status == "archived"

    # 验证级联挂起孤儿子块
    b_child_after = store.get_block("b_child")
    assert b_child_after.status == "suspended"
    assert "已归档，级联挂起保全" in b_child_after.pending_reason
