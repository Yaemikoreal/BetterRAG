# -*- coding: utf-8 -*-
"""
单元测试：SQLite 存储层与模型持久化
"""
import pytest
from src.engine.storage import Storage
from src.engine.models import Material, Block, AuditEvent, Citation, ConflictRecord


@pytest.fixture
def storage(tmp_path):
    db_file = tmp_path / "test_betterrag.db"
    store = Storage(db_file)
    yield store
    store.close()


def test_storage_material_crud(storage):
    mat = Material(
        id="mat_1",
        title="差旅报销制度",
        source_path="official/travel.md",
        file_hash="hash_travel_1",
        file_type="md",
        is_evergreen=True,
        is_tier0=False,
        imported_by="admin",
        summary="公司全员差旅标准"
    )
    storage.save_material(mat)

    fetched = storage.get_material("mat_1")
    assert fetched is not None
    assert fetched.title == "差旅报销制度"
    assert fetched.is_evergreen is True
    assert fetched.imported_by == "admin"

    by_path = storage.get_material_by_path("official/travel.md")
    assert by_path is not None
    assert by_path.id == "mat_1"

    all_mats = storage.get_all_materials()
    assert len(all_mats) == 1


def test_storage_block_crud_and_cascade_suspend(storage):
    mat = Material(
        id="mat_parent_child",
        title="父子块测试制度",
        source_path="policy.md",
        file_hash="hash_policy",
    )
    storage.save_material(mat)

    # 创建父级前置条件块与子块
    parent_block = Block(
        id="b_parent",
        material_id="mat_parent_child",
        content="前置条件：凡试用期员工不享受此福利",
        content_hash="h_parent",
        is_condition_header=True,
        conf_stored=0.85,
        status="active"
    )
    child_block = Block(
        id="b_child",
        material_id="mat_parent_child",
        parent_scope_id="b_parent",
        content="每季度可申请团建补助 500 元",
        content_hash="h_child",
        conf_stored=0.85,
        status="active"
    )
    storage.save_block(parent_block)
    storage.save_block(child_block)

    # 验证查询
    b_p = storage.get_block("b_parent")
    assert b_p is not None
    assert b_p.is_condition_header is True

    b_c = storage.get_block("b_child")
    assert b_c is not None
    assert b_c.parent_scope_id == "b_parent"

    # 测试级联挂起孤儿子块
    storage.archive_block("b_parent", note="父级制度废止")
    b_p_archived = storage.get_block("b_parent")
    assert b_p_archived.status == "archived"

    b_c_suspended = storage.get_block("b_child")
    assert b_c_suspended.status == "suspended"
    assert "父块 b_parent 已归档" in b_c_suspended.pending_reason


def test_storage_cascade_delete_material(storage):
    mat = Material(
        id="mat_del",
        title="待删除材料",
        source_path="del.md",
        file_hash="hash_del",
    )
    storage.save_material(mat)

    block = Block(
        id="b_del",
        material_id="mat_del",
        content="将被级联删除的内容",
        content_hash="h_del"
    )
    storage.save_block(block)

    # 确认存在
    assert storage.get_block("b_del") is not None

    # 删除材料
    storage.delete_material("mat_del")
    assert storage.get_material("mat_del") is None
    # 级联删除 blocks
    assert storage.get_block("b_del") is None


def test_storage_audit_events_no_fk_bomb(storage):
    # 验证 ADR/讨论结论：events 表不设物理外键，即使 block 物理被删，审计日志依然事实留痕
    ev_id = storage.log_event(
        block_id="b_nonexistent",
        material_id="mat_x",
        event_type="INGEST",
        actor="agent",
        delta=0.30,
        confidence_after=0.30,
        note="冷启动测试"
    )
    assert ev_id > 0

    events = storage.get_events(block_id="b_nonexistent")
    assert len(events) == 1
    assert events[0].event_type == "INGEST"
    assert events[0].confidence_after == 0.30


def test_storage_conflicts(storage):
    mat = Material(id="m_conf", title="冲突测试材料", source_path="conf.md", file_hash="h")
    storage.save_material(mat)

    b1 = Block(id="b_def", material_id="m_conf", content="旧规则打车21点后", content_hash="h_def")
    b2 = Block(id="b_cha", material_id="m_conf", content="新规则打车20点后", content_hash="h_cha")
    storage.save_block(b1)
    storage.save_block(b2)

    rec = ConflictRecord(
        id="conf_1",
        defender_id="b_def",
        challenger_id="b_cha",
        defender_hash="h_def",
        challenger_hash="h_cha",
        clash_count=1,
        verdict="conflict",
        status="open"
    )
    storage.save_conflict(rec)

    fetched = storage.get_conflict("b_def", "b_cha")
    assert fetched is not None
    assert fetched.clash_count == 1
    assert fetched.verdict == "conflict"

    # 更新状态与计数
    fetched.clash_count += 1
    storage.save_conflict(fetched)
    updated = storage.get_conflict("b_def", "b_cha")
    assert updated.clash_count == 2
