# -*- coding: utf-8 -*-
"""
BetterRAG 单变量置信度数学模型与纯函数计算
严格实现 docs/BetterRAG生产级开发实施方案.md §4 与 ADR-0001
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.engine.config import (
    CONF_ZONE_STABLE_MIN,
    CONF_ZONE_PRELIM_MIN,
    CONF_ARCHIVE_LINE,
    DECAY_HALF_LIFE_LEARN,
    DECAY_HALF_LIFE_STABLE,
    DECAY_GRACE_DAYS_LEARN,
    TENURE_GRACE_MIN,
    TENURE_GRACE_MAX,
    TENURE_STEP,
)
from src.engine.models import Block


def parse_datetime(val: str | datetime) -> datetime:
    """统一将输入转为 datetime 对象"""
    if isinstance(val, datetime):
        return val
    try:
        # 支持 ISO 格式
        return datetime.fromisoformat(val)
    except Exception:
        # 兼容标准 sqlite timestamp 格式
        return datetime.strptime(val[:19], "%Y-%m-%d %H:%M:%S")


def calc_grace_days(conf_stored: float, tenure: int = 0) -> float:
    """
    计算闲置容忍宽限期 (天)
    - 稳定区知识根据 tenure 动态延长闲置容忍宽限期: min(165.0, 80.0 + 10.0 * tenure)
    - 学习区/预备区知识默认 14 天
    """
    if conf_stored >= CONF_ZONE_STABLE_MIN:
        grace = TENURE_GRACE_MIN + TENURE_STEP * max(0, tenure)
        return min(TENURE_GRACE_MAX, grace)
    return DECAY_GRACE_DAYS_LEARN


def calc_half_life(conf_stored: float) -> float:
    """
    计算半衰期 (天)
    - 稳定区 (>=0.75): 365 天
    - 学习区 (<0.75): 90 天
    """
    if conf_stored >= CONF_ZONE_STABLE_MIN:
        return DECAY_HALF_LIFE_STABLE
    return DECAY_HALF_LIFE_LEARN


def calc_idle_days(last_engagement_at: str | datetime, now: str | datetime) -> float:
    """计算自上次采纳或正向交互以来的闲置天数"""
    dt_last = parse_datetime(last_engagement_at)
    dt_now = parse_datetime(now)
    delta_sec = (dt_now - dt_last).total_seconds()
    return max(0.0, delta_sec / 86400.0)


def calc_freshness(
    conf_stored: float,
    tenure: int,
    last_engagement_at: str | datetime,
    now: str | datetime,
    is_evergreen: bool = False,
    is_frozen: bool = False,
    is_tier0: bool = False,
) -> float:
    """
    计算知识保鲜度 Freshness ∈ (0.0, 1.0]
    公式:
      - 若为常青知识 (is_evergreen)、举证期冻结 (is_frozen) 或铁律 (is_tier0): Freshness = 1.0 (免除时间衰减)
      - 若 idle_days <= grace_days: Freshness = 1.0
      - 否则: Freshness = 0.5 ** ((idle_days - grace_days) / half_life)
    """
    if is_evergreen or is_frozen or is_tier0:
        return 1.0

    idle_days = calc_idle_days(last_engagement_at, now)
    grace_days = calc_grace_days(conf_stored, tenure)

    if idle_days <= grace_days:
        return 1.0

    half_life = calc_half_life(conf_stored)
    return 0.5 ** ((idle_days - grace_days) / half_life)


def calc_effective_conf(block: Block, now: Optional[str | datetime] = None) -> float:
    """
    计算块在此时此刻的有效置信度 Effective Confidence
    公式: Effective = conf_stored * Freshness(now)
    归档态返回 0.0
    """
    if block.status == "archived":
        return 0.0

    if now is None:
        now = datetime.utcnow()
    else:
        now = parse_datetime(now)

    freshness = calc_freshness(
        conf_stored=block.conf_stored,
        tenure=block.tenure,
        last_engagement_at=block.last_engagement_at,
        now=now,
        is_evergreen=block.is_evergreen,
        is_frozen=block.is_frozen(now),
        is_tier0=False,
    )
    return max(0.0, min(1.0, block.conf_stored * freshness))


def get_zone_name(block: Block, now: Optional[str | datetime] = None) -> str:
    """获取知识块当前所属治理分区"""
    if block.status == "archived":
        return "archived"
    if block.status == "suspended":
        return "suspended"
    if block.status == "pending":
        return "pending"
    if block.is_frozen(parse_datetime(now) if now else datetime.utcnow()):
        return "probation"
    if block.conf_stored >= CONF_ZONE_STABLE_MIN:
        return "stable"
    if block.conf_stored >= CONF_ZONE_PRELIM_MIN:
        return "prelim"
    return "learn"
