# -*- coding: utf-8 -*-
"""BetterRAG 自证晋升 —— 举证期 AI 挑刺（规格 §4.1，ADR-0005）。

证据净攒跨 0.60 → 举证期冻结 7 天 → 窗满时一次 AI 挑刺：
  案卷 = 结局统计 + 独立佐证 + 矛盾检查 + 反方红旗
  auto 政策：通过 → AUTO_PROMOTE 0.85（附案卷）；不过 → 例外队列；
             佐证不足 → 解冻回学习区（积分保留）。
  gated 政策：一律进例外队列（合规域人工门）。
本地兜底挑刺 = 纯规则（佐证源数 / 负证据 / 与稳定区近邻数字口径比对）。
"""
from __future__ import annotations

import json
from typing import List

from core import Block, Engine, CONFIG
from llm import LLM
from retrieve import Retriever, content_overlap


def build_dossier(eng: Engine, b: Block, ret: Retriever) -> dict:
    """挑刺案卷：全部字段确定性可得（§4.1 事件附案卷）。"""
    entered = b.probation_until - CONFIG["promotion.probation_days"]
    neg = sum(1 for e in eng.events
              if e["b"] == b.id and e["t"] == "EVID_NEG" and entered <= e["d"] <= eng.now)
    adopted = sum(1 for e in eng.events
                  if e["b"] == b.id and e["t"] == "ADOPT" and entered <= e["d"] <= eng.now)
    # 独立佐证：其他材料中与本块内容高度重叠的未归档块（同述不同源）
    sources, corroborators = 0, []
    my_text = b.title + " " + b.content
    for other in eng.blocks.values():
        if other.id == b.id or other.archived or other.suspended:
            continue
        if other.material_id == b.material_id:
            continue
        ov = content_overlap(my_text, other.title + " " + other.content)
        if ov >= 0.30:
            sources += 1
            corroborators.append({"block": other.id, "title": other.material_title,
                                  "overlap": round(ov, 2), "zone": other.zone_code()})
    # 矛盾检查：稳定区近邻同主题但数字口径不一致
    conflicts = []
    for other in eng.blocks.values():
        if other.id == b.id or other.archived or other.suspended:
            continue
        if other.zone_code() != 2 or other.material_id == b.material_id:
            continue
        ov = content_overlap(my_text, other.title + " " + other.content)
        if ov < 0.40:
            continue
        nums_a = set(__import__("re").findall(r"\d+(?:\.\d+)?", b.content))
        nums_b = set(__import__("re").findall(r"\d+(?:\.\d+)?", other.content))
        if nums_a and nums_b and not (nums_a & nums_b):
            conflicts.append({"block": other.id, "title": other.material_title,
                              "overlap": round(ov, 2)})
    corroborators.sort(key=lambda x: -x["overlap"])
    return {
        "block": b.id, "title": b.material_title, "conf": round(b.conf_stored, 3),
        "probation_days": CONFIG["promotion.probation_days"],
        "sources": sources, "sources_min": CONFIG["promotion.vet_sources_min"],
        "corroborators": corroborators[:5],
        "neg_evidence": neg, "adopted_count": adopted,
        "conflicts_with_stable": conflicts,
    }


def vet_verdict(llm: LLM, dossier: dict) -> dict:
    """AI 挑刺：LLM 可用走模型（低温、JSON 输出），否则本地规则。"""
    res = llm.call("vet", "案卷：" + json.dumps(dossier, ensure_ascii=False),
                   max_tokens=300)
    if res:
        try:
            v = json.loads(res[res.find("{"):res.rfind("}") + 1])
            if isinstance(v.get("passed"), bool):
                v["engine"] = "llm"
                v.setdefault("reasons", [])
                return v
        except Exception:
            pass
    v = llm.vet_local(dossier)
    return v


def make_probation_callback(eng: Engine, llm: LLM, ret: Retriever):
    """engine.on_probation_expire 回调：窗满挑刺三路（转正 / 例外队列 / 解冻）。"""
    def on_expire(b: Block) -> None:
        dossier = build_dossier(eng, b, ret)
        b.vet_sources = dossier["sources"]
        policy = str(CONFIG.get("promotion.policy", "auto"))
        if policy == "gated":
            verdict = {"passed": False, "reasons": ["gated 政策：人工门（AI 案卷已附）"],
                       "engine": "policy"}
        else:
            verdict = vet_verdict(llm, dossier)
        reasons = list(verdict.get("reasons", []))
        engine_tag = f"挑刺引擎={verdict.get('engine', 'local-rule')}"
        if dossier["sources"] < dossier["sources_min"] and policy != "gated":
            # 佐证不足：不进队列，解冻回学习区（积分保留）
            eng.probation_expire(b)
            return
        if verdict.get("passed"):
            reasons.append(engine_tag)
            eng.vet(b.id, True, reasons, dossier["sources"])
        else:
            eng.vet(b.id, False, reasons, dossier["sources"])
    return on_expire
