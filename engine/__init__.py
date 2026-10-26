# -*- coding: utf-8 -*-
"""BetterRAG 生命周期引擎包。

模块地图（对应 docs/生命周期引擎规格.md §16）：
  core      置信度单变量 + 生命周期状态机 + 审计事件（demo 与生产共用）
  store     SQLite 持久化（§10 schema）
  llm       引擎接入：角色映射 + OpenAI 兼容调用 + 本地确定性兜底（§12）
  ingest    收纳管线：扫描 / 切片 / 归纳摘要 / 幂等入库（§9）
  retrieve  检索管线：打分 / 探索席 / 相邻块 / 来源透明（§8）
  qa        问答装配 + 证据结算（重问判定 / 幂等反馈）+ 冲突检测
  promote   举证期 AI 挑刺（案卷）与自证晋升（§4.1）
  runtime   服务运行时：虚拟时钟 / 协调任务 / 仲裁阶梯 / 快照持久化（§11）
  server    stdlib HTTP API（§13）
  cli       命令行入口：serve / seed / status / scan / jump
"""
