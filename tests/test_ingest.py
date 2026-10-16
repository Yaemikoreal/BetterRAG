# -*- coding: utf-8 -*-
"""
单元测试：文件摄取、AST 切片与管理员冷启动通道
"""
from pathlib import Path
import pytest

from src.engine.storage import Storage
from src.engine.ingest import MarkdownParser, IngestionEngine


@pytest.fixture
def env(tmp_path):
    db_file = tmp_path / "test_ingest.db"
    store = Storage(db_file)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    official = workspace / "official"
    official.mkdir()
    engine = IngestionEngine(store)
    yield store, workspace, official, engine
    store.close()


def test_parser_condition_header_and_sections():
    parser = MarkdownParser()
    md_content = """# 员工考勤制度

## 第一章 假期规定

前置条件：凡试用期员工不享受带薪年假。

转正员工每满一年可享有 5 天带薪年假，最长不超过 15 天。

## 第二章 报销细则

普通打车需晚上 21:00 之后。
"""
    sections = parser.parse_to_sections(md_content)
    assert len(sections) == 2
    assert sections[0]["header_path"] == ["员工考勤制度", "第一章 假期规定"]
    assert len(sections[0]["paragraphs"]) == 2

    assert sections[1]["header_path"] == ["员工考勤制度", "第二章 报销细则"]
    assert len(sections[1]["paragraphs"]) == 1


def test_parser_split_and_merge():
    parser = MarkdownParser()
    # 1. 碎段 (< 50 字符) 自动并入上一段
    short1 = "第一句很短。"
    short2 = "第二句也很短但合并后变长许多，应该与第一句合并为一个知识块以消除噪声。"
    merged = parser.split_paragraphs_and_normalize([short1, short2])
    assert len(merged) == 1
    assert "第一句很短。\n第二句也很短" in merged[0]

    # 2. 超长段 (> 600 字符) 贪心切分成 300~500 字符子块
    long_para = "这是一个超长的段落测试。" * 40  # 480 汉字
    long_para_huge = "关于公司企业年金计划的实施细则与各部门员工缴费基数核算方法以及相关社保公积金统筹账户划拨流程的具体规定与细化条款说明。" * 15  # ~800 汉字
    split_chunks = parser.split_paragraphs_and_normalize([long_para_huge])
    assert len(split_chunks) >= 2
    for sc in split_chunks:
        assert len(sc) <= 500


def test_cold_start_admin_channel(env):
    store, workspace, official, engine = env

    # 1. 放入普通目录：初始置信度 0.30 (学习区)，imported_by = 'agent'
    normal_file = workspace / "draft_policy.md"
    normal_file.write_text("# 试运行制度草案\n\n试运行打车打卡规则详情说明，供全员讨论参考。", encoding="utf-8")
    mat_normal = engine.ingest_file(normal_file, workspace_root=workspace)
    assert mat_normal.imported_by == "agent"

    blocks_normal = store.get_blocks_by_material(mat_normal.id)
    assert len(blocks_normal) > 0
    assert blocks_normal[0].conf_stored == 0.30

    # 审计日志应记 INGEST
    events_normal = store.get_events(block_id=blocks_normal[0].id)
    assert any(e.event_type == "INGEST" for e in events_normal)

    # 2. 放入 official/ 专属目录：初始置信度 0.85 (直进稳定区)，imported_by = 'admin'
    official_file = official / "company_charter.md"
    official_file.write_text("# 公司总章程\n\n这是公司正式权威制度，入库直接赋予稳定区最高信任。", encoding="utf-8")
    mat_official = engine.ingest_file(official_file, workspace_root=workspace)
    assert mat_official.imported_by == "admin"

    blocks_official = store.get_blocks_by_material(mat_official.id)
    assert len(blocks_official) > 0
    assert blocks_official[0].conf_stored == 0.85

    # 审计日志应记 ADMIN_IMPORT
    events_official = store.get_events(block_id=blocks_official[0].id)
    assert any(e.event_type == "ADMIN_IMPORT" for e in events_official)
