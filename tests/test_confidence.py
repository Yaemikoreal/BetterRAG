# -*- coding: utf-8 -*-
"""
单元测试：置信度数学模型与纯函数计算
"""
from datetime import datetime, timedelta
import pytest

from src.engine.config import (
    CONF_ZONE_STABLE_MIN,
    CONF_ZONE_PRELIM_MIN,
    CONF_ARCHIVE_LINE,
    TENURE_GRACE_MIN,
    TENURE_GRACE_MAX,
    TENURE_STEP,
    DECAY_GRACE_DAYS_LEARN,
    DECAY_HALF_LIFE_LEARN,
    DECAY_HALF_LIFE_STABLE,
)
from src.engine.models import Block
from src.engine.confidence import (
    calc_grace_days,
    calc_half_life,
    calc_idle_days,
    calc_freshness,
    calc_effective_conf,
    get_zone_name,
)


def test_calc_grace_days():
    # 学习区/预备区知识 (conf < 0.75)：固定 14 天
    assert calc_grace_days(0.30, tenure=0) == 14.0
    assert calc_grace_days(0.60, tenure=5) == 14.0

    # 稳定区知识 (conf >= 0.75)：min(165.0, 80.0 + 10.0 * tenure)
    assert calc_grace_days(0.85, tenure=0) == 80.0
    assert calc_grace_days(0.85, tenure=2) == 100.0
    assert calc_grace_days(0.85, tenure=5) == 130.0
    assert calc_grace_days(0.85, tenure=10) == 165.0
    # 上限 165 天
    assert calc_grace_days(0.85, tenure=20) == 165.0


def test_calc_half_life():
    assert calc_half_life(0.30) == 90.0
    assert calc_half_life(0.749) == 90.0
    assert calc_half_life(0.75) == 365.0
    assert calc_half_life(0.85) == 365.0


def test_calc_freshness_exemptions():
    now = datetime(2026, 10, 15, 12, 0, 0)
    last = now - timedelta(days=200)

    # 1. 常青知识免除衰减
    assert calc_freshness(0.85, 0, last, now, is_evergreen=True) == 1.0

    # 2. 举证期冻结知识免除衰减
    assert calc_freshness(0.65, 0, last, now, is_frozen=True) == 1.0

    # 3. 铁律知识免除衰减
    assert calc_freshness(0.85, 0, last, now, is_tier0=True) == 1.0


def test_calc_freshness_within_grace():
    now = datetime(2026, 10, 15, 12, 0, 0)
    # 学习区宽限期 14 天，闲置 10 天
    last_learn = now - timedelta(days=10)
    assert calc_freshness(0.30, 0, last_learn, now) == 1.0

    # 稳定区 tenure=5 宽限期 130 天，闲置 120 天
    last_stable = now - timedelta(days=120)
    assert calc_freshness(0.85, 5, last_stable, now) == 1.0


def test_calc_freshness_decay():
    now = datetime(2026, 10, 15, 12, 0, 0)
    # 学习区 (半衰期 90 天，宽限 14 天)
    # 闲置 14 + 90 = 104 天时，保鲜度刚好为 0.5
    last = now - timedelta(days=104)
    freshness = calc_freshness(0.30, 0, last, now)
    assert abs(freshness - 0.5) < 1e-4

    # 稳定区 tenure=0 (半衰期 365 天，宽限 80 天)
    # 闲置 80 + 365 = 445 天时，保鲜度刚好为 0.5
    last_stable = now - timedelta(days=445)
    freshness_stable = calc_freshness(0.85, 0, last_stable, now)
    assert abs(freshness_stable - 0.5) < 1e-4


def test_calc_effective_conf():
    now = datetime(2026, 10, 15, 12, 0, 0)
    b = Block(
        id="b1",
        material_id="m1",
        content="测试文本",
        content_hash="h1",
        conf_stored=0.85,
        tenure=5,
        last_engagement_at=(now - timedelta(days=120)).isoformat(),
    )
    # 闲置 120 天在 130 天宽限期内，有效置信度维持 0.85
    eff = calc_effective_conf(b, now)
    assert abs(eff - 0.85) < 1e-4

    # 归档态块有效置信度始终为 0
    b.status = "archived"
    assert calc_effective_conf(b, now) == 0.0


def test_get_zone_name():
    b_stable = Block(id="1", material_id="m", content="x", content_hash="h", conf_stored=0.85)
    assert get_zone_name(b_stable) == "stable"

    b_prelim = Block(id="2", material_id="m", content="x", content_hash="h", conf_stored=0.65)
    assert get_zone_name(b_prelim) == "prelim"

    b_learn = Block(id="3", material_id="m", content="x", content_hash="h", conf_stored=0.30)
    assert get_zone_name(b_learn) == "learn"

    b_pending = Block(id="4", material_id="m", content="x", content_hash="h", conf_stored=0.74, status="pending")
    assert get_zone_name(b_pending) == "pending"

    b_archived = Block(id="5", material_id="m", content="x", content_hash="h", conf_stored=0.85, status="archived")
    assert get_zone_name(b_archived) == "archived"
