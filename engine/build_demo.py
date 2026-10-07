# -*- coding: utf-8 -*-
"""
BetterRAG 演示场景构建器

职责：
  1. 生成 15 篇资费语料到 data/workspace/（照搬原则：原文切片，AI 只贴标签）
  2. 用真实引擎跑虚拟时钟（day -300 → +520），预置事件史与分支
  3. 导出 web/timeline.json，并注入 web/template.html → BetterRAG-demo.html（单文件离线可跑）

运行：python engine/build_demo.py
"""
from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (  # noqa: E402
    CONFIG, Engine, E_IRON_DISSENT, E_IRON_ALERT, E_VET_PASS,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = os.path.join(ROOT, "data", "workspace")
WEB = os.path.join(ROOT, "web")

DAYS_START, DAYS_END = -300, 520
DUEL_DAY, AMEND_DAY = 100, 108
GOLDEN = 137.508

# --------------------------------------------------------------------- 语料
CORPUS = [
    # ---- 管理员导入（8 篇，直进稳定区 0.85，其中 2 篇铁律）----
    dict(id="iron-billing", title="计费稽核铁律", admin=True, iron=True, created=-300,
         tags=["铁律", "计费", "合规"],
         summary="计费差错实行「双人复核 + 全量留痕」，任何资费口径变更须经计费稽核备案后方可对客生效。",
         body="""# 计费稽核铁律

## 第一条 口径唯一
对客资费口径以本铁律备案版本为唯一准绳。任何系统、渠道、话术不得自行解释或变通。

## 第二条 双人复核
计费类变更须由计费稽核岗双人复核，复核记录保存不少于五年。

## 第三条 全量留痕
自变更生效时刻起，全量计费明细留痕，可回溯至单笔账单。

## 第四条 备案前置
未完成备案的资费口径，不得对客宣传、不得进入工单话术库、不得用于结算。
"""),
    dict(id="iron-compliance", title="入网合规规范", admin=True, iron=True, created=-300,
         tags=["铁律", "入网", "实名制"],
         summary="实名制入网与二次核验的强制要求，涉未成年人、政企客户与境外人士的差异化流程。",
         body="""# 入网合规规范

## 实名制
所有用户入网须完成实名登记与证件核验，禁止代办、代持。

## 二次核验
高风险号码（涉诈模型命中）须完成二次核验后方可复机。

## 特殊人群
未成年人入网须监护人陪同；政企客户须提供营业执照与经办人授权书。
"""),
    dict(id="pkg-5g-enjoy", title="5G 畅享套餐资费", admin=True, created=-300,
         tags=["资费", "5G", "主推"],
         summary="5G 畅享系列三档资费：129/159/199 元，含流量、语音与会员权益，合约期 24 个月。",
         body="""# 5G 畅享套餐资费

| 档位 | 月费 | 通用流量 | 语音 | 权益 |
|---|---|---|---|---|
| 畅享 129 | 129 元 | 60GB | 1000 分钟 | 视频会员 |
| 畅享 159 | 159 元 | 100GB | 1500 分钟 | 视频+音乐会员 |
| 畅享 199 | 199 元 | 150GB | 2000 分钟 | 全权益包 |

## 说明
- 超出流量按 3 元/GB 计费，当月累计 600 元封顶。
- 合约期 24 个月，提前解约按剩余月份 30% 收取违约金。
- 主推档位为畅享 159。
"""),
    dict(id="pkg-4g-data", title="4G 全国流量包资费", admin=True, created=-300,
         tags=["资费", "4G", "存量"],
         summary="4G 全国流量包 30 元 10GB / 50 元 20GB，当月有效不结转，可叠加订购。",
         body="""# 4G 全国流量包资费

| 档位 | 价格 | 流量 | 有效期 |
|---|---|---|---|
| 小包 | 30 元 | 10GB | 当月有效 |
| 大包 | 50 元 | 20GB | 当月有效 |

## 说明
- 当月有效，不结转次月。
- 可叠加订购，单月最多订购 5 次。
- 适用于 4G 套餐用户，5G 用户订购后按 4G 速率使用。
"""),
    dict(id="pkg-home-broadband", title="家庭宽带融合套餐", admin=True, created=-300,
         tags=["资费", "宽带", "融合"],
         summary="手机+宽带融合：月费满 129 元赠 500M 宽带，满 199 元赠 1000M 宽带，含光猫租用。",
         body="""# 家庭宽带融合套餐

## 融合规则
- 手机主套餐月费 ≥129 元：赠送 500M 宽带
- 手机主套餐月费 ≥199 元：赠送 1000M 宽带
- 光猫设备租用免费，退网时须归还

## 装机与移机
- 新装工料费 100 元，合约期内移机免费一次。
"""),
    dict(id="rule-subcard", title="副卡与共享规则", admin=True, created=-300,
         tags=["规则", "副卡", "共享"],
         summary="副卡 10 元/张/月，最多 4 张，共享主卡流量与语音，主卡可单独限制副卡权限。",
         body="""# 副卡与共享规则

- 副卡月功能费 10 元/张，单主卡最多办理 4 张。
- 副卡共享主卡套餐内流量与语音，不共享会员权益。
- 主卡可为每张副卡单独设置流量上限与呼叫限制。
"""),
    dict(id="rule-suspend", title="停机保号与复机规则", admin=True, created=-300,
         tags=["规则", "停机", "复机"],
         summary="停机保号 5 元/月，最长保号 6 个月，超期自动销户；复机须结清欠费并完成二次核验。",
         body="""# 停机保号与复机规则

- 停机保号功能费 5 元/月，最长保号 6 个月。
- 超 6 个月未复机，号码自动销户并进入 90 天冷冻期。
- 复机须结清全部欠费；高风险号码须完成二次核验。
"""),
    dict(id="pkg-roaming", title="国际漫游资费", admin=True, created=-300,
         tags=["资费", "漫游", "国际"],
         summary="国际漫游按方向分三档日包（28/48/88 元/天），含 1GB 高速流量，达量降速不限量。",
         body="""# 国际漫游资费

| 方向 | 日包价格 | 高速流量 |
|---|---|---|
| 亚洲周边 | 28 元/天 | 1GB |
| 欧美主流 | 48 元/天 | 1GB |
| 其他方向 | 88 元/天 | 1GB |

- 达量降速至 384kbps，不限量。
- 需提前开通国际漫游功能。
"""),

    # ---- 学习区（4 篇，Agent 收纳 0.30）----
    dict(id="new-5g-policy", title="5G 新套餐政策", admin=False, created=0,
         tags=["新政策", "5G", "待复核"],
         summary="5G 新套餐政策：三档资费下调 20 元，流量提升 50%，合约期由 24 个月缩短至 12 个月。",
         body="""# 5G 新套餐政策

## 调整要点
1. 三档月费统一下调 20 元：109 / 139 / 179 元。
2. 通用流量提升 50%：90GB / 150GB / 225GB。
3. 合约期由 24 个月缩短至 12 个月。
4. 新增「当月流量可结转次月」权益。

## 生效时间
自本政策发布次月 1 日起对新入网用户生效，存量用户可申请免费迁移。

## 备注
本政策与既有《5G 畅享套餐资费》存在口径重叠，需以本政策为准。
"""),
    dict(id="dir-adjust", title="政策方向调整", admin=False, created=-40,
         tags=["政策", "方向", "待复核"],
         summary="计费口径由「事后稽核」转向「事前备案 + 实时校验」，稽核岗职责相应调整。",
         body="""# 政策方向调整

## 调整方向
计费治理由「事后稽核」转向「事前备案 + 实时校验」。

## 对既有流程的影响
1. 资费变更须在生效前 5 个工作日完成线上备案。
2. 稽核岗由全量复核转为抽样复核 + 异常拦截。
3. 全量留痕要求保持不变。

## 说明
本调整不降低合规标准，仅改变执行时序。
"""),
    dict(id="promo-campus", title="校园套餐暑期活动", admin=False, created=-120,
         tags=["活动", "校园", "限时"],
         summary="校园套餐暑期促销：学生认证后月费 39 元享 50GB，活动期 7-8 月，需学信网认证。",
         body="""# 校园套餐暑期活动

- 学生认证通过后月费 39 元，含 50GB 通用流量 + 200 分钟语音。
- 活动期：7 月 1 日 — 8 月 31 日。
- 需通过学信网在线认证，认证有效期内可续订。
"""),
    dict(id="bb-speedup", title="宽带提速不加价说明", admin=False, created=-90,
         tags=["宽带", "提速", "说明"],
         summary="存量 300M 宽带用户免费提速至 500M，无需换设备，分批次于三个月内完成。",
         body="""# 宽带提速不加价说明

- 存量 300M 宽带用户免费提速至 500M，月费不变。
- 无需更换光猫设备（支持千兆的机型直接提速）。
- 分批次执行，三个月内完成全网升级。
"""),

    # ---- 预备区（3 篇，举证期中）----
    dict(id="notice-4g-stop", title="停售公告：4G 全国流量包", admin=False, created=-60,
         tags=["停售", "公告", "4G"],
         summary="4G 全国流量包自下月起停止销售，已订购用户当月权益不变，次月起不再支持续订。",
         body="""# 停售公告：4G 全国流量包

## 停售安排
1. 自下月 1 日起，4G 全国流量包（30 元 10GB / 50 元 20GB）停止销售。
2. 已订购用户当月权益不受影响。
3. 次月起不再支持续订，建议迁移至 5G 畅享套餐。

## 原因
配合 5G 网络迁移与资费体系整合。
"""),
    dict(id="discount-loyal", title="老用户专属折扣政策", admin=False, created=-150,
         tags=["折扣", "老用户", "权益"],
         summary="在网满 5 年的老用户可申请月费 8 折，合约期 12 个月，与终端补贴不可叠加。",
         body="""# 老用户专属折扣政策

- 在网满 5 年的用户可申请月费 8 折优惠。
- 合约期 12 个月，合约期内不可降档。
- 与终端补贴、其他折扣活动不可叠加。
"""),
    dict(id="guide-5g-up", title="5G 套餐升级指引", admin=False, created=-110,
         tags=["指引", "5G", "迁移"],
         summary="4G 用户升级 5G 的三种路径与资费对比，含终端适配检查与迁移回退说明。",
         body="""# 5G 套餐升级指引

## 三种路径
1. 直接变更套餐：次月生效，无违约金。
2. 合约期内升级：需结清原合约违约金。
3. 新办号码迁移：保留原号转网。

## 终端适配
需确认终端支持 n78/n41 频段，可在营业厅免费检测。

## 迁移回退
升级后 30 天内可无条件回退原套餐。
"""),
]

BY_ID = {m["id"]: m for m in CORPUS}

# ---------------------------------------------------------------- 故事数据
STORY = {
    "ingest": {
        "file": "5G 新套餐政策.md",
        "block": "new-5g-policy",
        "summary": "5G 新套餐政策：三档资费下调 20 元，流量提升 50%，合约期由 24 个月缩短至 12 个月。",
        "tags": ["新政策", "5G", "待复核"],
        "chunks": 4,
    },
    "ask": {
        "question": "现在有什么优惠套餐？",
        "answer": "当前主推三档 5G 套餐：畅享 129 / 159 / 199 元，分别含 60GB / 100GB / 150GB 通用流量。"
                  "另有一份刚入库的新政策：三档月费统一下调 20 元、流量提升 50%、合约期缩短至 12 个月，"
                  "预计次月起对新入网用户生效，存量用户可申请免费迁移。",
        "citations": [
            {"block": "pkg-5g-enjoy", "label": "已验证", "quote": "畅享 129/159/199 元，含 60/100/150GB 通用流量"},
            {"block": "new-5g-policy", "label": "新知识 · 入库 0 天 · 待专家复核",
             "quote": "三档月费统一下调 20 元，流量提升 50%，合约期缩短至 12 个月"},
        ],
        "feedback": "👍 代表工单一次闭环（真实系统由工单回调自动供证，用户零动作）",
    },
    "probation": {
        "evidence_events": 15,
        "note": "重放 15 笔工单一次闭环证据（+0.02 × 15）",
        "vet_reasons": ["与铁律及近邻稳定块无矛盾", "独立佐证 4 源（工单 / 采纳 / 会话 / 文档）", "反方质询未发现红旗"],
    },
    "exception": {
        "block": "guide-5g-up",
        "title": "5G 套餐升级指引",
        "reasons": [
            "与稳定区《4G 全国流量包资费》存在未决矛盾（迁移路径与停售口径不一致）",
            "近 7 日同题重问聚集（3 次），疑似答案未解决用户问题",
            "独立佐证仅 1 源，低于下限 3 源",
        ],
        "verdict_reject": "拒绝 → 置 0.30，积分清零回学习区",
    },
    "duel": {
        "question": "4G 全国流量包还能订购吗？",
        "defender": "pkg-4g-data",
        "challenger": "notice-4g-stop",
        "defender_answer": "4G 全国流量包仍在售：30 元 10GB、50 元 20GB，当月有效不结转，可叠加订购，单月最多 5 次。",
        "challenger_answer": "4G 全国流量包自下月 1 日起停止销售。已订购用户当月权益不变，次月起不再支持续订，建议迁移至 5G 畅享套餐。",
        "gap_note": "有效置信度差 0.85 − 0.62 = 0.23 < 0.25（对冲判负阈值）→ 进入影子双答点选",
    },
    "iron": {
        "iron_block": "iron-billing",
        "challenger": "dir-adjust",
        "preset_dissent": 2,
        "note": "共现冲突且防守方带铁律位 → 挑战者保护性挂起 + 铁律异议 +1 → 达阈值 3 亮红灯",
        "amend_note": "一键接替：新块 0.85 + 铁律位，旧铁律归档 + 取代链（原子交接）",
    },
    "archive": {
        "block": "pkg-4g-data",
        "title": "4G 全国流量包资费",
    },
    "quotes": {
        "open": "企业知识库只有记忆，没有新陈代谢——错误知识入库就永久污染。这个库不一样。",
        "close": "我们不造更强的模型，我们给知识库装免疫系统：被动结局供证、慢升快降、当场仲裁、宪法可修。越用越准，越用越干净。",
    },
}

SHOTS = [
    {"id": 1, "name": "开场 · 知识星图", "day": 0, "focus": "starmap",
     "title": "知识星图", "caption": "分区着色星图（金=稳定、蓝=预备、绿=学习）、冲突连线、滚动事件流"},
    {"id": 2, "name": "冷启动 · 管理员导入", "day": 0, "focus": "starmap",
     "title": "冷启动", "caption": "私有化部署，接你们自己的模型；存量知识管理员导入直进稳定区"},
    {"id": 3, "name": "收纳 · 只照搬", "day": 0, "focus": "ingest",
     "title": "收纳：只照搬", "caption": "原文切片入库，AI 只贴标签不改内容——幻觉进不了门"},
    {"id": 4, "name": "问答与证据", "day": 0, "focus": "ask",
     "title": "问答与证据", "caption": "新知识当天可用但必须亮明身份；采纳只是入场券，真实结局才是证据"},
    {"id": 5, "name": "举证期与自动转正", "day": 97, "focus": "promote",
     "title": "慢升：自证晋升", "caption": "证据净攒跨 0.60 → 冻结 7 天 → AI 挑刺 → AUTO_PROMOTE 0.85"},
    {"id": 6, "name": "例外裁决", "day": 97, "focus": "exception",
     "title": "例外裁决", "caption": "机器不敢担保的才轮到人——今天队列只有这一条"},
    {"id": 7, "name": "影子对决", "day": 100, "focus": "duel",
     "title": "影子对决", "caption": "N=3 触发仲裁阶梯；差 0.23 < 0.25 → 影子双答，请评委当场点选"},
    {"id": 8, "name": "铁律红灯 · 修正案", "day": 108, "focus": "iron",
     "title": "铁律红灯", "caption": "宪法只能由人修：挑战者保全、异议计数、一键接替留取代链"},
    {"id": 9, "name": "衰减归档 · 传记", "day": 520, "focus": "archive",
     "title": "免疫：衰减归档", "caption": "沉默不算满意，但沉默会让它过期；不删除，归档留案底"},
    {"id": 10, "name": "时间滑块 · 代谢史", "day": 520, "focus": "timeline",
     "title": "时间滑块", "caption": "拖动滑块看这个库的代谢史；换份语料 30 秒迁移"},
]


# ------------------------------------------------------------------ 事件助手
def op_adopt(bid: str, note: str):
    def fn(e: Engine):
        b = e.blocks.get(bid)
        if b is not None and not b.archived:
            e.adopt(bid, note)
    return fn


def op_evidence(bid: str, note: str, kind: str = "pos"):
    def fn(e: Engine):
        if bid in e.blocks:
            e.evidence(bid, kind, note)
    return fn


def op_vet(bid: str, passed: bool, reasons, sources: int):
    def fn(e: Engine):
        e.vet(bid, passed, reasons, sources)
    return fn


def op_log_stage():
    """在 N=3 时把仲裁阶梯的判定过程写进审计链（现场可展开）。"""
    def fn(e: Engine):
        rec = next((r for r in e.conflicts if r["defender"] == "pkg-4g-data"
                    and r["challenger"] == "notice-4g-stop"), None)
        if not rec:
            return
        d, c = e.blocks["pkg-4g-data"], e.blocks["notice-4g-stop"]
        stage = e.arbitration_stage(rec)
        gap = d.effective(e.now) - c.effective(e.now)
        label = {"shadow_duel": "影子双答点选", "hedge": "对冲判负", "iron_red": "铁律红灯"}[stage]
        sign = "<" if stage == "shadow_duel" else "≥"
        e._log("ARB_STAGE", d, 0.0,
               f"N={rec['count']} 触发仲裁阶梯：差 {d.effective(e.now):.2f}−{c.effective(e.now):.2f}"
               f"={gap:.2f} {sign} {CONFIG['conflict.hedge_gap']:.2f} → {label}", "engine")
    return fn


# ------------------------------------------------------------------ 场景跑批
def build(duel_choice: str = "new") -> dict:
    eng = Engine()
    sched: dict[int, list] = {}

    def at(day: int, fn):
        sched.setdefault(day, []).append(fn)

    # 固定角度：黄金角散布，渲染期不抖动
    order = [m["id"] for m in CORPUS]
    angles = {}
    for i, bid in enumerate(order):
        angles[bid] = (i * GOLDEN) % 360.0

    # --- 语料落库 ---
    for m in CORPUS:
        jitter = ((hash(m["id"]) % 1000) / 1000.0 - 0.5) * 0.055

        def make(mm, jj):
            def fn(e: Engine):
                e.add_material(mm["id"], mm["title"], mm["title"], admin=mm["admin"],
                               angle=angles[mm["id"]], jitter=jj, tier0=bool(mm.get("iron")))
            return fn

        at(m["created"], make(m, jitter))

    # --- 主流资费持续保鲜（活性，零积分；保持金色）---
    mainstream = ["pkg-5g-enjoy", "pkg-home-broadband", "rule-subcard",
                  "rule-suspend", "pkg-roaming", "iron-compliance", "iron-billing"]
    for d in range(-296, DAYS_END + 1, 20):
        for bid in mainstream:
            at(d, op_adopt(bid, "主流资费持续保鲜（活性·零积分）"))

    # 4G 流量包：对决败诉前一直保鲜，之后无人问津 → 衰减归档
    for d in range(-296, DUEL_DAY, 20):
        at(d, op_adopt("pkg-4g-data", "主流资费持续保鲜（活性·零积分）"))

    # --- 预成长：把学习区块养到目标置信度（每条证据配一次采纳以保鲜）---
    def grow(bid: str, days: int, count: int, note: str):
        step = max(1, days // max(1, count))
        for i in range(count):
            d = -days + i * step
            at(d, op_evidence(bid, note))
            at(d, op_adopt(bid, "被答案采纳（活性）"))
        at(-3, op_adopt(bid, "被答案采纳（活性）"))

    grow("discount-loyal", 145, 18, "工单一次闭环（+0.02）")
    grow("guide-5g-up", 105, 17, "工单一次闭环（+0.02）")
    grow("notice-4g-stop", 55, 16, "工单一次闭环（+0.02）")
    grow("dir-adjust", 38, 8, "工单一次闭环（+0.02）")
    grow("promo-campus", 118, 3, "工单一次闭环（+0.02）")
    grow("bb-speedup", 88, 5, "工单一次闭环（+0.02）")

    # 例外队列预置：挑刺红旗（镜 6 唯一一条）
    at(-5, op_vet("guide-5g-up", False, STORY["exception"]["reasons"], 1))
    # 队列条目保持新鲜，便于现场展示
    for d in range(0, 130, 25):
        at(d, op_adopt("guide-5g-up", "被答案采纳（活性）"))

    # 铁律历史异议 2 笔（镜 7.5 的现场第 3 笔点亮红灯）
    def preset_iron(e: Engine):
        ib = e.blocks["iron-billing"]
        for i in range(STORY["iron"]["preset_dissent"]):
            ib.iron_dissent += 1
            e._log(E_IRON_DISSENT, ib, 0.0,
                   f"铁律异议 +1（{ib.iron_dissent}/{CONFIG['iron.dissent_threshold']}）· 历史预置", "detector")
    at(-30, preset_iron)

    # ---------------- 演示主线（day 0 起）----------------
    at(0, (lambda e: e.add_material("new-5g-policy", "5G 新套餐政策", "5G 新套餐政策",
                                    admin=False, angle=angles["new-5g-policy"], jitter=0.0)))
    at(0, op_adopt("new-5g-policy", "镜 4：答案实际使用该块（活性·零积分）"))
    at(0, op_evidence("new-5g-policy", "👍 代表工单一次闭环（+0.02）"))

    # 镜 5：重放 15 笔闭环证据 → 跨 0.60 → 举证期 → 挑刺 → 自动转正
    n_ev = STORY["probation"]["evidence_events"]
    for i in range(1, n_ev):
        d = int(i * (90 / n_ev))
        at(d, op_evidence("new-5g-policy", "重放：工单一次闭环（+0.02）"))
        if i % 4 == 0:
            at(d, op_adopt("new-5g-policy", "重放：被答案采纳（活性）"))
    at(96, op_adopt("new-5g-policy", "重放：被答案采纳（活性）"))
    at(97, op_vet("new-5g-policy", True, STORY["probation"]["vet_reasons"], 4))
    at(98, op_vet("discount-loyal", True, ["与近邻稳定块无矛盾", "独立佐证 3 源"], 3))

    # 镜 6：例外裁决 —— 拒绝（唯一一条队列；放在 day 98，使 day 97 仍可看到「复核中」条目）
    at(98, (lambda e: e.adjudicate("guide-5g-up", "reject")))

    # 镜 7：影子对决（N=3 → 仲裁阶梯）
    for i in range(3):
        at(DUEL_DAY + i, (lambda e: e.conflict("pkg-4g-data", "notice-4g-stop",
                                                "采纳集跨分区共现 + judge 判竞争")))
    at(DUEL_DAY + 2, op_log_stage())
    at(DUEL_DAY + 3, (lambda e: e.duel_vote(
        next(r for r in e.conflicts if r["defender"] == "pkg-4g-data"
             and r["challenger"] == "notice-4g-stop"), duel_choice)))
    # 对决胜出后，挑战者攒够证据 → 挑刺通过转正；此后成为在用知识，持续保鲜
    at(DUEL_DAY + 6, op_vet("notice-4g-stop", True, ["影子对决获评委采信", "独立佐证 3 源"], 3))
    for d in range(DUEL_DAY + 20, DAYS_END + 1, 20):
        at(d, op_adopt("notice-4g-stop", "主流资费持续保鲜（活性·零积分）"))
    for d in range(110, DAYS_END + 1, 25):
        at(d, op_adopt("new-5g-policy", "主流资费持续保鲜（活性·零积分）"))
        at(d, op_adopt("discount-loyal", "主流资费持续保鲜（活性·零积分）"))

    # 镜 7.5：铁律红灯 + 修正案
    at(AMEND_DAY, (lambda e: e.conflict("iron-billing", "dir-adjust", "共现冲突 · 防守方带铁律位")))
    at(AMEND_DAY + 1, (lambda e: e.amend_replace("iron-billing", "dir-adjust")))

    # ---------------- 虚拟时钟推进 ----------------
    frames, events = [], []
    for day in range(DAYS_START, DAYS_END + 1):
        eng.now = float(day)
        for fn in sched.get(day, []):
            fn(eng)
        eng.reconcile(day)
        frames.append(eng.frame())
        events.extend(eng.events[len(events):])

    return {"frames": frames, "events": events, "blocks": list(eng.blocks.values())}


def m_created(bid: str) -> int:
    return BY_ID[bid]["created"]


def pack(frames, index_of, n_blocks):
    """帧内行序必须与 blocks 元数据下标严格对齐；未创建的块用 eff=-1 标记为「不可见」。"""
    out = []
    for f in frames:
        rows = [[i, -1, 0, 0] for i in range(n_blocks)]
        for r in f["b"]:
            i = index_of[r[0]]
            rows[i] = [i, r[1], r[2], r[3]]
        out.append([f["d"], rows])
    return out


# ------------------------------------------------------------------- 语料落盘
def write_corpus():
    os.makedirs(WORKSPACE, exist_ok=True)
    for m in CORPUS:
        with open(os.path.join(WORKSPACE, m["title"] + ".md"), "w", encoding="utf-8") as fh:
            fh.write(m["body"])
    return len(CORPUS)


def main():
    n = write_corpus()
    print(f"[1/3] 语料已生成：{n} 篇 → data/workspace/")

    main_run = build("new")
    branch_old = build("old")
    branch_novote = build("novote")
    print(f"[2/3] 虚拟时钟跑批完成：day {DAYS_START} → {DAYS_END}，"
          f"{len(main_run['frames'])} 帧，{len(main_run['events'])} 条审计事件")

    order = [m["id"] for m in CORPUS]
    index_of = {bid: i for i, bid in enumerate(order)}

    blocks_meta = []
    for b in sorted(main_run["blocks"], key=lambda x: index_of[x.id]):
        m = BY_ID[b.id]
        blocks_meta.append({
            "id": b.id, "title": b.title, "angle": round(b.angle, 3),
            "jitter": round(b.jitter, 4), "iron": bool(m.get("iron")),
            "admin": bool(m["admin"]), "summary": m["summary"], "tags": m["tags"],
            "created": m["created"],
        })

    def trim(run):
        return pack([f for f in run["frames"] if f["d"] >= DUEL_DAY + 3], index_of, len(order))

    ev = []
    for e in main_run["events"]:
        if e["t"] in ("ADOPT",) and "重放" not in e["note"] and "持续保鲜" not in e["note"]:
            pass
        ev.append({"d": e["d"], "t": e["t"], "b": e["b"], "delta": e["delta"],
                   "c": e["c"], "note": e["note"], "actor": e["actor"]})
    # 折叠高频活性事件，保留机制事件
    folded, adopt_bucket = [], {}
    for e in ev:
        if e["t"] == "ADOPT" and ("持续保鲜" in e["note"] or "重放" in e["note"]):
            key = (e["d"] // 30, e["b"])
            adopt_bucket[key] = adopt_bucket.get(key, 0) + 1
            continue
        folded.append(e)
    for (bucket, bid), cnt in adopt_bucket.items():
        folded.append({"d": bucket * 30, "t": "ADOPT", "b": bid, "delta": 0.0, "c": 0.0,
                       "note": f"活性 ×{cnt}（保鲜时钟重置，零积分）", "actor": "user"})
    folded.sort(key=lambda x: (x["d"], x["t"]))

    timeline = {
        "meta": {
            "title": "BetterRAG",
            "subtitle": "自演进知识库 Agent · 生命周期引擎演示",
            "dayStart": DAYS_START, "dayEnd": DAYS_END,
            "duelDay": DUEL_DAY + 3, "amendDay": AMEND_DAY + 1,
            "config": {k: v for k, v in CONFIG.items()},
            "zones": {"learn": [0.05, 0.60], "prelim": [0.60, 0.75], "stable": [0.75, 1.00]},
        },
        "blocks": blocks_meta,
        "frames": pack(main_run["frames"], index_of, len(order)),
        "branches": {"old": trim(branch_old), "novote": trim(branch_novote)},
        "events": folded[-1400:],
        "story": STORY,
        "shots": SHOTS,
    }

    os.makedirs(WEB, exist_ok=True)
    with open(os.path.join(WEB, "timeline.json"), "w", encoding="utf-8") as fh:
        json.dump(timeline, fh, ensure_ascii=False, separators=(",", ":"))

    tpl = os.path.join(WEB, "template.html")
    if os.path.exists(tpl):
        with open(tpl, "r", encoding="utf-8") as fh:
            html = fh.read()
        payload = json.dumps(timeline, ensure_ascii=False, separators=(",", ":"))
        html = html.replace("/*__TIMELINE__*/null", payload)
        out = os.path.join(ROOT, "BetterRAG-demo.html")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(f"[3/3] 单文件演示已生成：BetterRAG-demo.html（{len(html)//1024} KB，离线可跑）")
    else:
        print("[3/3] 未找到 web/template.html —— 仅输出 timeline.json")

    # 关键状态体检
    print("\n--- 演示起点（day 0）星图状态 ---")
    for f in main_run["frames"]:
        if f["d"] == 0:
            zc = {0: "学习区", 1: "预备区", 2: "稳定区", 3: "待处置", 4: "归档", 5: "挂起"}
            rows = sorted(f["b"], key=lambda r: -r[1])
            for r in rows:
                print(f"  {BY_ID[r[0]]['title']:<22} eff={r[1]/1000:.3f}  {zc[r[2]]}"
                      f"{'  [铁律]' if r[3] & 8 else ''}{'  [举证期]' if r[3] & 1 else ''}"
                      f"{'  [队列]' if r[3] & 32 else ''}")
            break

    for day in (97, DUEL_DAY + 3, AMEND_DAY + 1, DAYS_END):
        for f in main_run["frames"]:
            if f["d"] == day:
                zc = {0: "学习区", 1: "预备区", 2: "稳定区", 3: "待处置", 4: "归档", 5: "挂起"}
                focus = {97: ["new-5g-policy", "guide-5g-up"],
                         DUEL_DAY + 3: ["pkg-4g-data", "notice-4g-stop"],
                         AMEND_DAY + 1: ["iron-billing", "dir-adjust"],
                         DAYS_END: ["pkg-4g-data", "pkg-5g-enjoy"]}[day]
                print(f"\n--- day {day} ---")
                for r in f["b"]:
                    if r[0] in focus:
                        print(f"  {BY_ID[r[0]]['title']:<22} eff={r[1]/1000:.3f}  {zc[r[2]]}"
                              f"{'  [铁律]' if r[3] & 8 else ''}{'  [待处置]' if r[3] & 2 else ''}"
                              f"{'  [归档]' if r[3] & 16 else ''}")
                break


if __name__ == "__main__":
    main()
