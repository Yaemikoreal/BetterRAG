# -*- coding: utf-8 -*-
"""BetterRAG 引擎接入 —— 角色映射 LLM 适配器（规格 §12）。

设计宪法「坏了有退路」：四个 LLM 角色（作答/摘要/挑刺/判定）全部配有
本地确定性兜底实现——未配置供应商、网络失败、返回格式异常时自动降级，
核心生命周期永不因 LLM 瘫痪（LLM 经济学 §3.3：信号/星图/仪表盘 = 0 调用）。

供应商配置来源（优先级）：runtime 内存 → 环境变量 BETTERRAG_LLM_* → SQLite。
OpenAI-compatible：POST {base_url}/chat/completions。
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from typing import Dict, List, Optional

ROLES = ("answer", "summarize", "vet", "judge", "biography")
ROLE_TEMPERATURE = {"answer": 0.4, "summarize": 0.2, "vet": 0.1, "judge": 0.1,
                    "biography": 0.5}

ROLE_PROMPTS = {
    "answer": (
        "你是企业知识库助手。仅依据给出的知识块原文回答问题；"
        "不得编造知识块之外的内容；引用内容保持原文。"),
    "summarize": (
        "为下列企业知识文档写一段 120 字以内的归纳摘要，"
        "只概括文档已有内容，不添加新事实。直接输出摘要正文。"),
    "vet": (
        "你是知识库晋升评审（挑刺）。依据案卷判断该知识块能否自动晋升稳定区。"
        "检查：1) 与铁律及稳定区近邻是否矛盾 2) 独立佐证是否充分 "
        "3) 有无反方红旗。只输出 JSON：{\"passed\": true/false, \"reasons\": [\"…\"]}"),
    "judge": (
        "判断两个知识块对同一问题是「竞争」（互相矛盾、只能取一）还是「互补」"
        "（可同时成立）。只输出 JSON：{\"competing\": true/false, \"reason\": \"…\"}"),
    "biography": (
        "根据审计事件链，为该知识块写一段 100 字以内的生平小传，"
        "按时间叙事，不虚构事件。"),
}


class LLM:
    """角色映射适配器。provider 为 None 或 ok=False 时走本地兜底。"""

    def __init__(self, provider: Optional[dict] = None) -> None:
        self.provider: Optional[dict] = provider
        self.last_error = ""

    # ------------------------------------------------------------ 供应商管理
    def configure(self, p: Optional[dict]) -> dict:
        self.provider = p or None
        return self.status()

    def status(self) -> dict:
        if not self.provider:
            return {"configured": False, "ok": False, "mode": "local-fallback"}
        p = self.provider
        return {"configured": True, "ok": bool(p.get("ok")), "name": p.get("name", ""),
                "base_url": p.get("base_url", ""), "model": p.get("model", ""),
                "mode": "openai-compatible", "last_error": p.get("last_error", "")}

    def test_connect(self) -> dict:
        """测试连通：一条 1-token 补全。成功置 ok=True，失败置 ok=False。"""
        if not self.provider:
            return {"ok": False, "error": "未配置供应商（本地兜底模式）"}
        try:
            text = self._chat_raw("ping——只回复 ok", max_tokens=8)
            self.provider["ok"] = bool(text)
            self.provider["last_error"] = ""
            return {"ok": True, "sample": text[:40]}
        except Exception as e:  # 网络不通报错不崩溃
            self.provider["ok"] = False
            self.provider["last_error"] = str(e)[:200]
            return {"ok": False, "error": self.provider["last_error"]}

    def _chat_raw(self, user: str, system: str = "", max_tokens: int = 512,
                  temperature: float = 0.2) -> str:
        p = self.provider
        url = p["base_url"].rstrip("/") + "/chat/completions"
        body = {"model": p["model"], "temperature": temperature, "max_tokens": max_tokens,
                "messages": ([{"role": "system", "content": system}] if system else [])
                + [{"role": "user", "content": user}]}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {p.get('api_key') or ''}"})
        timeout = float(p.get("timeout", 30))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")

    # ------------------------------------------------------------ 角色调用
    def call(self, role: str, payload: str, max_tokens: int = 512) -> Optional[str]:
        """按角色调用。失败/未配置返回 None（调用方走本地兜底）。"""
        if not self.provider or not self.provider.get("ok"):
            return None
        try:
            text = self._chat_raw(payload, ROLE_PROMPTS.get(role, ""),
                                  max_tokens=max_tokens,
                                  temperature=ROLE_TEMPERATURE.get(role, 0.2))
            return text
        except Exception as e:
            self.provider["ok"] = False
            self.provider["last_error"] = f"{role}: {e}"[:200]
            self.last_error = self.provider["last_error"]
            return None

    # ------------------------------------------------------------ 四个角色的本地兜底
    @staticmethod
    def summarize_local(content: str) -> str:
        """抽取式摘要：标题 + 各节首句拼接 ≤160 字（照搬原则：不生成新事实）。"""
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        title = ""
        body_lines = []
        for l in lines:
            if l.startswith("#"):
                if not title:
                    title = l.lstrip("#").strip()
                continue
            body_lines.append(l)
        sents: List[str] = []
        for l in body_lines:
            for s in re.split(r"(?<=[。！？；.!?])\s*", l):
                s = s.strip().strip("|-")
                if len(s) >= 8 and not s.startswith(("#", "|", "-", "*")):
                    sents.append(s)
        out = (title + "：" if title else "") + "；".join(sents[:3])
        return out[:160]

    @staticmethod
    def vet_local(dossier: dict) -> dict:
        """本地挑刺：纯规则检查（案卷字段见 promote.build_dossier）。"""
        reasons, passed = [], True
        if dossier["sources"] < dossier["sources_min"]:
            passed = False
            reasons.append(f"独立佐证 {dossier['sources']} 源，低于下限 {dossier['sources_min']} 源")
        else:
            reasons.append(f"独立佐证 {dossier['sources']} 源（≥{dossier['sources_min']}）")
        if dossier["neg_evidence"] > 0:
            passed = False
            reasons.append(f"举证期内有 {dossier['neg_evidence']} 笔负证据（红旗）")
        else:
            reasons.append("举证期内零负证据")
        if dossier["conflicts_with_stable"]:
            passed = False
            reasons.append("与稳定区近邻存在文本级矛盾（数字口径不一致）")
        else:
            reasons.append("与铁律及稳定区近邻无矛盾")
        if dossier["adopted_count"] == 0:
            reasons.append("提示：举证期内零采纳（无活性记录）")
        return {"passed": passed, "reasons": reasons, "engine": "local-rule"}

    @staticmethod
    def judge_local(a_title: str, a_content: str, b_title: str, b_content: str) -> dict:
        """本地冲突判定：同主题（token 重叠 ≥0.45）且口径可疑：
        双方都有数字且互不相交（改口径），或仅一方有数字（新增量化条款的修订）。
        重叠不足 → 互补（不同主题可并存）。"""
        from retrieve import tokens, content_overlap
        overlap = content_overlap(a_content, b_content)
        na = set(re.findall(r"\d+(?:\.\d+)?", a_content))
        nb = set(re.findall(r"\d+(?:\.\d+)?", b_content))
        if na and nb:
            suspect = not (na & nb)
            why = "数字口径不一致"
        elif na or nb:
            suspect = True
            why = "一方新增量化条款（疑似修订）"
        else:
            suspect = False
            why = "双方均无量化口径"
        competing = overlap >= 0.45 and suspect
        return {"competing": competing,
                "reason": f"文本重叠 {overlap:.2f}；{why}"
                          f"（本地规则兜底）", "engine": "local-rule"}

    @staticmethod
    def biography_local(title: str, events: List[dict]) -> str:
        """规则叙事：按里程碑事件串一段生平。"""
        if not events:
            return f"《{title}》尚无审计记录。"
        mile = [e for e in events if e["t"] in (
            "INGEST", "ADMIN_IMPORT", "PROBATION_ENTER", "VET_PASS", "VET_FLAG",
            "ADJUDICATE_PASS", "ADJUDICATE_REJECT", "ADJUDICATE_RESTORE", "DEMOTE",
            "RECONCILE", "ARCHIVE", "ADJUDICATE_ARCHIVE", "SUSPEND", "AMEND_REPLACE")]
        if not mile:
            return f"《{title}》平静无波：只有日常采纳与证据记账。"
        first, last = mile[0], mile[-1]
        return (f"《{title}》于第 {int(first['d'])} 天"
                f"{'由管理员导入' if first['t'] == 'ADMIN_IMPORT' else '被 Agent 收纳入库'}；"
                f"最近一次状态变化在第 {int(last['d'])} 天：{last['note'][:60]}。"
                f"全程 {len(events)} 条审计事件可回放。")
