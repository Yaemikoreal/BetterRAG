# -*- coding: utf-8 -*-
"""
BetterRAG 核心参数与配置定义
严格对齐 docs/BetterRAG生产级开发实施方案.md 与 docs/生命周期引擎规格.md
"""
from pathlib import Path

# 默认存储与工作目录路径
DEFAULT_DATA_DIR = Path("data")
DEFAULT_WORKSPACE_DIR = Path("data/workspace")
DEFAULT_OFFICIAL_DIR = Path("data/workspace/official")
DEFAULT_DB_PATH = Path("data/betterrag.db")

# 置信度分区阈值
CONF_INITIAL_LEARN = 0.30       # 普通收纳初始置信度 (学习区)
CONF_INITIAL_ADMIN = 0.85       # 管理员导入初始置信度 (稳定区)
CONF_PROMOTE_VALUE = 0.85       # 自证晋升/一键接替目标置信度 (稳定区)
CONF_ZONE_PRELIM_MIN = 0.60     # 预备区/举证期下界
CONF_ZONE_STABLE_MIN = 0.75     # 稳定区下界 (权威基石)
CONF_MACHINE_CAP = 0.749        # 机器与常规反馈写入边界 (不得跨越 0.75)
CONF_STABLE_MAX = 1.00          # 稳定区上限
CONF_ARCHIVE_LINE = 0.05        # 废弃线 (低于此线自动归档)

# 降级阶段锚定值 (三段式降级)
CONF_DEMOTE_PENDING = 0.749     # 第一段：跌破稳定区下界 -> 待处置 (落 0.749 并重锚)
CONF_DEMOTE_LEARN = 0.599       # 第二段：待处置期满跌破预备区 -> 滑回学习区 (落 0.599 并重锚)

# 衰减时间参数 (天)
DECAY_HALF_LIFE_LEARN = 90.0    # 学习区半衰期 (90天)
DECAY_HALF_LIFE_STABLE = 365.0  # 稳定区半衰期 (365天)
DECAY_GRACE_DAYS_LEARN = 14.0   # 学习区闲置宽限期 (14天)

# 老知识 tenure 动态延长宽限期 (80 ~ 165天，每积攒1次 tenure 增加 10天)
TENURE_GRACE_MIN = 80.0
TENURE_GRACE_MAX = 165.0
TENURE_STEP = 10.0

# 举证期参数
PROBATION_DAYS = 7              # 举证期冻结天数 (7个自然日)

# 反馈分值映射 (显着性加权基础分)
SCORE_MAP = {
    "pos": 0.02,                # 正反馈 (点赞/闭环)
    "neg_reask": -0.02,         # 隐式负反馈 (同题重问)
    "neg_dislike": -0.02,       # 点踩
    "neg_escalate": -0.03,      # 转人工
}

# 冲突相关配置
CONFLICT_THRESHOLD_N = 3        # 冲突触发推送阈值 (累积 3 次)
CONFLICT_COARSE_SIM = 0.82      # 跨分区粗筛余弦相似度下界
FUZZY_ALIGN_SIM = 0.90          # 同文件增量块模糊对齐相似度阈值
FUZZY_ALIGN_LEN_DIFF = 0.20     # 同文件增量块长度差异容忍比例 (20%)

# 块切片上下界规整 (BGE 512 Token 防御与碎段合并)
CHUNK_MIN_LEN = 50              # < 50 字符自动合并
CHUNK_MAX_LEN = 600             # > 600 字符二次贪心拆分
CHUNK_TARGET_MIN = 300
CHUNK_TARGET_MAX = 500

# 默认向量模型
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
EMBEDDING_DIM = 384
