# -*- coding: utf-8 -*-
"""
BetterRAG W1 核心单测验收门禁测试套件 (Day 1 - 5 必测通过)
严格实现 docs/BetterRAG生产级开发实施方案.md §6 与 Agent讨论 #004 规定用例：
  1. Hash 继承单测 (用例 A/B/C)
  2. 冲突缓存单测 (用例 A/B)
  3. 三段式降级与 tenure 宽限单测
"""
from datetime import datetime, timedelta
from pathlib import Path
import pytest

from src.engine.config import (
    CONF_ZONE_STABLE_MIN,
    CONF_ZONE_PRELIM_MIN,
    CONF_ARCHIVE_LINE,
    CONF_DEMOTE_PENDING,
    CONF_DEMOTE_LEARN,
    CONFLICT_THRESHOLD_N,
)
from src.engine.storage import Storage
from src.engine.models import Material, Block, ConflictRecord
from src.engine.ingest import IngestionEngine
from src.engine.reconcile import DailyReconciler
from src.engine.arbitrate import ConflictManager


@pytest.fixture
def gate_env(tmp_path):
    db_file = tmp_path / "w1_gates.db"
    store = Storage(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    official = workspace / "official"
    official.mkdir()
    # 禁用在线 embedding 模型下载以保证单测毫秒级极速运行
    engine = IngestionEngine(store)
    reconciler = DailyReconciler(store)
    conf_mgr = ConflictManager(store)
    yield store, workspace, official, engine, reconciler, conf_mgr
    store.close()


# ==============================================================================
# 门禁 1：Hash 继承单测 (用例 A/B/C)
# ==============================================================================

def test_gate1_case_a_typo_edit(gate_env):
    """
    用例 A（改错别字）：
    段落 1/2/3 初始入库，段落 2 被修改错别字；
    验证：改动块重置 0.30，其余未改动块 100% 继承原有置信度与 tenure。
    """
    store, workspace, official, engine, _, _ = gate_env

    # 初始文件包含三个独立段落
    initial_text = """# 员工差旅管理办法

## 第一节 住宿标准
一线城市北京上海广州深圳出差住宿标准上限为每人每天 600 元整。

## 第二节 交通报销
市内出差优先乘坐公共交通，因公打车需晚上 21:00 之后方可凭发票报销。

## 第三节 差旅补助
出差期间按出差自然天数享有每日 100 元伙食与公杂综合定额补助。
"""
    file_path = workspace / "travel.md"
    file_path.write_text(initial_text, encoding="utf-8")
    mat = engine.ingest_file(file_path, workspace_root=workspace)

    # 模拟系统运行累积置信度与 tenure
    blocks = store.get_blocks_by_material(mat.id)
    assert len(blocks) == 3
    b1, b2, b3 = blocks[0], blocks[1], blocks[2]

    b1.conf_stored = 0.85
    b1.tenure = 6
    b2.conf_stored = 0.80
    b2.tenure = 4
    b3.conf_stored = 0.88
    b3.tenure = 8
    store.save_blocks([b1, b2, b3])

    # 修改第二节中的一个错别字（"21:00" 改为 "21点30分"）
    modified_text = """# 员工差旅管理办法

## 第一节 住宿标准
一线城市北京上海广州深圳出差住宿标准上限为每人每天 600 元整。

## 第二节 交通报销
市内出差优先乘坐公共交通，因公打车需晚上 21点30分 之后方可凭发票报销。

## 第三节 差旅补助
出差期间按出差自然天数享有每日 100 元伙食与公杂综合定额补助。
"""
    file_path.write_text(modified_text, encoding="utf-8")
    engine.ingest_file(file_path, workspace_root=workspace)

    # 重新读取并验证当前活跃知识块
    active_blocks = [b for b in store.get_blocks_by_material(mat.id) if b.status == "active"]
    assert len(active_blocks) == 3
    ub1, ub2, ub3 = active_blocks[0], active_blocks[1], active_blocks[2]

    # 段落 1：未改动，100% 继承置信度 0.85 与 tenure 6
    assert ub1.content == b1.content
    assert ub1.conf_stored == 0.85
    assert ub1.tenure == 6

    # 段落 2：内容变更，作为学习区新块录入 (置信度 0.30，tenure 0)
    assert ub2.content != b2.content
    assert ub2.conf_stored == 0.30
    assert ub2.tenure == 0

    # 段落 3：未改动，100% 继承置信度 0.88 与 tenure 8
    assert ub3.content == b3.content
    assert ub3.conf_stored == 0.88
    assert ub3.tenure == 8


def test_gate1_case_b_insert_paragraph(gate_env):
    """
    用例 B（插入新段落）：
    在段落 1 与段落 2 之间插入全新段落 X；
    验证：段落树分块下，新段落生成新块，后续段落哈希不漂移并成功继承置信度与 tenure。
    """
    store, workspace, official, engine, _, _ = gate_env

    initial_text = """# 办公设备管理细则

## 第一节 申领流程
入职第一天由行政部门统一配发标准办公笔记本电脑及配件设施。

## 第二节 保管责任
员工对所领用的办公设备负有妥善保管义务，非正常损坏须按折旧赔偿。
"""
    file_path = workspace / "asset.md"
    file_path.write_text(initial_text, encoding="utf-8")
    mat = engine.ingest_file(file_path, workspace_root=workspace)

    blocks = store.get_blocks_by_material(mat.id)
    assert len(blocks) == 2
    b1, b2 = blocks[0], blocks[1]
    b1.conf_stored = 0.85
    b1.tenure = 5
    b2.conf_stored = 0.82
    b2.tenure = 3
    store.save_blocks([b1, b2])

    # 在第一节和第二节之间插入全新规定：第 1.5 节 备用机申请
    inserted_text = """# 办公设备管理细则

## 第一节 申领流程
入职第一天由行政部门统一配发标准办公笔记本电脑及配件设施。

## 第 1.5 节 备用机申请
因设备维修或其他紧急公事，可向行政服务台申请最长为期两周的临时备用电脑。

## 第二节 保管责任
员工对所领用的办公设备负有妥善保管义务，非正常损坏须按折旧赔偿。
"""
    file_path.write_text(inserted_text, encoding="utf-8")
    engine.ingest_file(file_path, workspace_root=workspace)

    updated_blocks = store.get_blocks_by_material(mat.id)
    assert len(updated_blocks) == 3
    ub1, ub_new, ub2 = updated_blocks[0], updated_blocks[1], updated_blocks[2]

    # 第一块：未受影响，100% 继承
    assert ub1.conf_stored == 0.85
    assert ub1.tenure == 5

    # 新插入的块：初始置信度 0.30，tenure 0
    assert "备用机申请" in ub_new.content or "临时备用电脑" in ub_new.content
    assert ub_new.conf_stored == 0.30
    assert ub_new.tenure == 0

    # 原第二块：天然免疫切片漂移！content_hash 完全一致，成功继承 0.82 与 tenure 3
    assert ub2.content == b2.content
    assert ub2.content_hash == b2.content_hash
    assert ub2.conf_stored == 0.82
    assert ub2.tenure == 3


def test_gate1_case_c_delete_paragraph(gate_env):
    """
    用例 C（删除段落）：
    删除文档中的某一段落；
    验证：被删段落自动标记为 archived 并记录 superseded_by。
    """
    store, workspace, official, engine, _, _ = gate_env

    initial_text = """# 员工福利指南

## 第一节 节日礼品
公司在端午节、中秋节及春节向在职正式员工发放标准节日礼品一份。

## 第二节 生日津贴
员工生日当月享有由部门安排的生日会或 200 元等额礼品卡一张。
"""
    file_path = workspace / "welfare.md"
    file_path.write_text(initial_text, encoding="utf-8")
    mat = engine.ingest_file(file_path, workspace_root=workspace)

    blocks = store.get_blocks_by_material(mat.id)
    assert len(blocks) == 2
    b1_id, b2_id = blocks[0].id, blocks[1].id

    # 删去第二节生日津贴
    deleted_text = """# 员工福利指南

## 第一节 节日礼品
公司在端午节、中秋节及春节向在职正式员工发放标准节日礼品一份。
"""
    file_path.write_text(deleted_text, encoding="utf-8")
    engine.ingest_file(file_path, workspace_root=workspace)

    # 检查存活块
    active_blocks = [b for b in store.get_blocks_by_material(mat.id) if b.status == "active"]
    assert len(active_blocks) == 1
    assert active_blocks[0].content == blocks[0].content

    # 检查被删块状态
    deleted_block = store.get_block(b2_id)
    assert deleted_block.status == "archived"
    assert deleted_block.superseded_by is not None
    assert "原段落已移除" in deleted_block.pending_reason

    # 检查审计日志是否记录了 ARCHIVE 事件
    events = store.get_events(block_id=b2_id)
    assert any(e.event_type == "ARCHIVE" for e in events)


def test_gate1_multi_children_condition_binding(gate_env):
    """
    复验用例 ① (修复 P0-1)：
    一个条件头带有 2 段子项 + 另一节亦有条件头及多子项；
    验证：所有子块正确绑定到对应章节的父条件块，不发生错绑或外键 IntegrityError 崩溃。
    """
    store, workspace, official, engine, _, _ = gate_env

    multi_child_text = """# 综合合规细则

## 第一节 福利前置
前置条件：凡试用期员工不享受以下两项福利补贴：

转正员工每人每月享有 300 元通讯与流量补助金。

转正员工每人每年享有 1000 元企业年金补充配套资金。

## 第二节 差旅前置
限制条件：未通过合规在线考试人员不得申请差旅借款：

合规考试满 80 分方可通过审批系统提交借款申请单。

特殊紧急事项需分管副总裁与财务总监同时线下书面签字。
"""
    file_path = workspace / "multi_child.md"
    file_path.write_text(multi_child_text, encoding="utf-8")
    mat = engine.ingest_file(file_path, workspace_root=workspace)

    blocks = store.get_blocks_by_material(mat.id)
    assert len(blocks) == 6

    # 第一节条件头与两个子块
    parent1 = blocks[0]
    assert parent1.is_condition_header is True
    child1_1 = blocks[1]
    child1_2 = blocks[2]
    assert child1_1.parent_scope_id == parent1.id
    assert child1_2.parent_scope_id == parent1.id

    # 第二节条件头与两个子块
    parent2 = blocks[3]
    assert parent2.is_condition_header is True
    child2_1 = blocks[4]
    child2_2 = blocks[5]
    assert child2_1.parent_scope_id == parent2.id
    assert child2_2.parent_scope_id == parent2.id

    # 验证子块没有错绑到其他节的条件头
    assert child1_1.parent_scope_id != parent2.id
    assert child2_1.parent_scope_id != parent1.id


def test_gate1_block_id_stability_reference_integrity(gate_env):
    """
    复验用例 ② (修复 P0-2)：
    在中部插入段落后，内容未变的块 ID 绝不发生漂移，确保 citations 与 conflicts 引用完整性。
    """
    store, workspace, official, engine, _, _ = gate_env

    initial_text = """# 采购管理规范

## 第一节 办公用品采购
常规办公文具每月由各部门行政助理统一汇总后集中采购。

## 第二节 服务器硬件采购
服务器硬件设施须经技术委员会架构评审后方可发起立项审批。
"""
    file_path = workspace / "procure.md"
    file_path.write_text(initial_text, encoding="utf-8")
    mat = engine.ingest_file(file_path, workspace_root=workspace)

    blocks = store.get_blocks_by_material(mat.id)
    assert len(blocks) == 2
    b1_id, b2_id = blocks[0].id, blocks[1].id

    # 在中间插入新段落
    inserted_text = """# 采购管理规范

## 第一节 办公用品采购
常规办公文具每月由各部门行政助理统一汇总后集中采购。

## 第 1.5 节 软件系统采购
SaaS 软件采购须经信息安全部合规评估与数据出境风险核查。

## 第二节 服务器硬件采购
服务器硬件设施须经技术委员会架构评审后方可发起立项审批。
"""
    file_path.write_text(inserted_text, encoding="utf-8")
    engine.ingest_file(file_path, workspace_root=workspace)

    updated_blocks = store.get_blocks_by_material(mat.id)
    assert len(updated_blocks) == 3

    ub1 = updated_blocks[0]
    ub_new = updated_blocks[1]
    ub2 = updated_blocks[2]

    # 实体同一性验证：第一块与原第二块 ID 完全保持不变！
    assert ub1.id == b1_id
    assert ub2.id == b2_id
    assert ub_new.id != b1_id
    assert ub_new.id != b2_id


def test_gate1_short_condition_items_not_swallowed(gate_env):
    """
    复验用例 ③ (修复 P1-1)：
    <50 字的短列表子项在遇到前置条件头时，独立成子块，严禁被并入条件头段落。
    """
    store, workspace, official, engine, _, _ = gate_env

    short_child_text = """# 住房公积金与补贴标准

## 第一节 住房补贴限制
前置条件：以下人员不享受每月租房专项补贴：

1. 试用期员工；

2. 兼职与实习生；

3. 顾问外聘人员；
"""
    file_path = workspace / "housing.md"
    file_path.write_text(short_child_text, encoding="utf-8")
    mat = engine.ingest_file(file_path, workspace_root=workspace)

    blocks = store.get_blocks_by_material(mat.id)
    # 条件头 1 块 + 3 个独立子块 = 4 块，绝不被合并成 1 块！
    assert len(blocks) == 4

    parent = blocks[0]
    assert parent.is_condition_header is True
    assert "试用期员工" not in parent.content

    c1, c2, c3 = blocks[1], blocks[2], blocks[3]
    assert "试用期员工" in c1.content
    assert "兼职与实习生" in c2.content
    assert "顾问外聘人员" in c3.content

    assert c1.parent_scope_id == parent.id
    assert c2.parent_scope_id == parent.id
    assert c3.parent_scope_id == parent.id



# ==============================================================================
# 门禁 2：冲突缓存单测 (用例 A/B)
# ==============================================================================

def test_gate2_case_a_compatible_cache(gate_env):
    """
    用例 A（COMPATIBLE 判定）：
    首次判兼容全量落库缓存；
    二次粗筛命中时：零 LLM 调用（judge_fn 不执行），且 clash_count 不增加。
    """
    store, _, _, _, _, conf_mgr = gate_env

    mat = Material(id="m_gate2a", title="兼容测试", source_path="g2a.md", file_hash="h")
    store.save_material(mat)

    defender = Block(id="def_1", material_id="m_gate2a", content="总公司报销规则", content_hash="h_d1", conf_stored=0.85)
    challenger = Block(id="cha_1", material_id="m_gate2a", content="分公司补充细则", content_hash="h_c1", conf_stored=0.30)
    store.save_block(defender)
    store.save_block(challenger)

    judge_call_count = 0

    def mock_judge(d: Block, c: Block):
        nonlocal judge_call_count
        judge_call_count += 1
        return "compatible", "双方为总分公司补充关系，互不冲突"

    # 1. 首次触发判决
    rec1 = conf_mgr.check_or_record_conflict(defender, challenger, judge_fn=mock_judge)
    assert judge_call_count == 1
    assert rec1.verdict == "compatible"
    assert rec1.clash_count == 1

    # 2. 二次粗筛再次命中该 pair
    def unreachable_judge(d: Block, c: Block):
        raise AssertionError("缓存命中时不应当再次调用 LLM Judge！")

    rec2 = conf_mgr.check_or_record_conflict(defender, challenger, judge_fn=unreachable_judge)
    # 验证：零 LLM 调用，直接复用缓存，且 clash_count 依然为 1（不增加计数）
    assert judge_call_count == 1
    assert rec2.verdict == "compatible"
    assert rec2.clash_count == 1


def test_gate2_case_b_conflict_counting(gate_env):
    """
    用例 B（CONFLICT 计数）：
    首次判冲突入库；
    后续粗筛再次命中仅累加 clash_count，零 LLM Judge 重复调用；
    满 3 次时状态自动流转为 notified (推卡片待办)。
    """
    store, _, _, _, _, conf_mgr = gate_env

    mat = Material(id="m_gate2b", title="互斥测试", source_path="g2b.md", file_hash="h")
    store.save_material(mat)

    defender = Block(id="def_2", material_id="m_gate2b", content="旧政策：打车晚上21点后", content_hash="h_d2", conf_stored=0.85)
    challenger = Block(id="cha_2", material_id="m_gate2b", content="新政策：打车晚上20点后", content_hash="h_c2", conf_stored=0.30)
    store.save_block(defender)
    store.save_block(challenger)

    judge_call_count = 0

    def mock_conflict_judge(d: Block, c: Block):
        nonlocal judge_call_count
        judge_call_count += 1
        return "conflict", "时段冲突：同一报销场景下20点与21点直接互斥"

    # 1. 第一次命中：调用 Judge 并入库
    rec1 = conf_mgr.check_or_record_conflict(defender, challenger, judge_fn=mock_conflict_judge)
    assert judge_call_count == 1
    assert rec1.verdict == "conflict"
    assert rec1.clash_count == 1
    assert rec1.status == "open"

    def forbidden_judge(d: Block, c: Block):
        raise AssertionError("已确诊冲突 pair 粗筛命中不应重复调用 Judge！")

    # 2. 第二次粗筛命中：直接累加 clash_count 为 2
    rec2 = conf_mgr.check_or_record_conflict(defender, challenger, judge_fn=forbidden_judge)
    assert judge_call_count == 1
    assert rec2.clash_count == 2
    assert rec2.status == "open"

    # 3. 第三次粗筛命中：达到阈值 3 次，状态流转为 notified
    rec3 = conf_mgr.check_or_record_conflict(defender, challenger, judge_fn=forbidden_judge)
    assert judge_call_count == 1
    assert rec3.clash_count == 3
    assert rec3.status == "notified"


# ==============================================================================
# 门禁 3：三段式降级与 tenure 宽限单测
# ==============================================================================

def test_gate3_tenure_grace_and_three_stage_demotion(gate_env):
    """
    验证：
    1. tenure=5 的稳定块在闲置 120 天内不衰减 (宽限期 min(165, 80 + 10*5) = 130 天)；
    2. 闲置超期后依次经过 0.749 待处置 -> 0.599 学习区 -> 0.05 归档；
    3. 归档时级联挂起孤儿子块。
    """
    store, _, _, _, reconciler, _ = gate_env

    mat = Material(id="m_gate3", title="衰减门禁测试", source_path="g3.md", file_hash="h")
    store.save_material(mat)

    t0 = datetime(2026, 1, 1, 3, 0, 0)

    # 创建一个 tenure=5 的稳定区前置父块 (conf_stored=0.85, 宽限期 130 天)
    parent_block = Block(
        id="gate3_parent",
        material_id="m_gate3",
        content="公司报销前置限制：未提供正式发票者一律不予报销",
        content_hash="h_p_g3",
        conf_stored=0.85,
        tenure=5,
        last_engagement_at=t0.isoformat(),
        status="active",
        is_condition_header=True
    )
    # 创建一个依附于该父块的细则子块
    child_block = Block(
        id="gate3_child",
        material_id="m_gate3",
        parent_scope_id="gate3_parent",
        content="发票金额大于 5000 元需部门总监加签确认",
        content_hash="h_c_g3",
        conf_stored=0.85,
        tenure=5,
        last_engagement_at=t0.isoformat(),
        status="active"
    )
    store.save_block(parent_block)
    store.save_block(child_block)

    # ---------------- 检验 1：闲置 120 天内完全不衰减 ----------------
    t_120 = t0 + timedelta(days=120)
    stats1 = reconciler.reconcile(now=t_120)
    assert stats1["demoted_pending"] == 0
    p1 = store.get_block("gate3_parent")
    assert p1.conf_stored == 0.85
    assert p1.status == "active"

    # ---------------- 检验 2：闲置超期 (第 210 天)，第一段降级到 0.749 待处置 ----------------
    # 宽限期 130 天，闲置 210 天 (超期 80 天)，半衰期 365 天
    # Freshness = 0.5^(80/365) = 0.859
    # eff_conf = 0.85 * 0.859 = 0.730 < 0.75
    # 棘轮下调至 0.730 (<= 0.749)，status 置为 'pending'，重锚基线
    t_demote1 = t0 + timedelta(days=210)
    stats2 = reconciler.reconcile(now=t_demote1)
    assert stats2["demoted_pending"] >= 1

    p2 = store.get_block("gate3_parent")
    assert p2.status == "pending"
    assert p2.conf_stored <= CONF_DEMOTE_PENDING
    assert p2.last_engagement_at == t_demote1.isoformat()

    # ---------------- 检验 3：待处置且继续无证据 (自重锚起闲置 50 天)，第二段降级到 0.599 学习区 ----------------
    # 待处置学习半衰期 90 天，宽限期 14 天。闲置 50 天超期 36 天
    # Freshness = 0.5^(36/90) = 0.758
    # eff_conf = 0.730 * 0.758 = 0.553 < 0.60
    # 棘轮下调至 0.553 (<= 0.599)，status 置为 'active' (退回学习区)，重锚基线
    t_demote2 = t_demote1 + timedelta(days=50)
    stats3 = reconciler.reconcile(now=t_demote2)
    assert stats3["demoted_learn"] >= 1

    p3 = store.get_block("gate3_parent")
    assert p3.status == "active"
    assert p3.conf_stored <= CONF_DEMOTE_LEARN
    assert p3.last_engagement_at == t_demote2.isoformat()

    # ---------------- 检验 4：自重锚起长期闲置 (360 天)，跌破 0.05 废弃线，自动归档并级联挂起子块 ----------------
    t_demote3 = t_demote2 + timedelta(days=360)
    # 子块模拟在这段期间有业务采纳（保持活跃）
    child_cur = store.get_block("gate3_child")
    child_cur.last_engagement_at = (t_demote3 - timedelta(days=5)).isoformat()
    store.save_block(child_cur)

    stats4 = reconciler.reconcile(now=t_demote3)
    assert stats4["archived"] >= 1

    p4 = store.get_block("gate3_parent")
    assert p4.status == "archived"

    # 验证子块级联挂起
    c4 = store.get_block("gate3_child")
    assert c4.status == "suspended"
    assert "已归档，级联挂起保全" in c4.pending_reason
