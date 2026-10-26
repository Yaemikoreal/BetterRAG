# BetterRAG 运行手册（Runbook）

> 生产版 v1.0 · 零第三方依赖（Python ≥3.10 标准库）· SQLite 单文件持久化 · 虚拟时钟（ADR-0004）
> 本手册覆盖：启动、种子、日常运维、LLM 接入、快进演示、备份恢复、故障排查。

---

## 1. 快速启动

```bash
# 1) 种子（首次）：管理员导入 8 篇（含 2 铁律）+ 扫描 data/workspace 收纳其余材料
python -m engine.cli seed            # 干净库：稳定区 + 学习区，无预置历史
python -m engine.cli seed --demo     # 演示库：额外预置挑战者预成长/例外队列/铁律异议 2/3

# 2) 启动服务（含自动种子检查）
python -m engine.cli serve --port 8787
# 打开 http://127.0.0.1:8787/app

# 3) 运行测试（49 项，全部离线确定性）
python -m unittest discover -s tests
```

Windows 一键：双击 `启动服务.cmd`（首次自动种子 + 启动服务）。

## 2. 命令速查

| 命令 | 作用 |
|---|---|
| `python -m engine.cli serve [--port 8787] [--virtual] [--demo-seed]` | 启动 HTTP 服务；`--virtual` 虚拟时钟模式（时间只经快进推进，演示/测试用） |
| `python -m engine.cli seed [--demo]` | 种子；库内已有快照时跳过（重置请删除 `data/betterrag.db*`） |
| `python -m engine.cli scan` | 立即扫描工作区（幂等：内容指纹未变的材料跳过） |
| `python -m engine.cli status` | 打印分区分布 / 例外队列 / 红灯 |
| `python -m engine.cli jump N` | 虚拟时钟快进 N 天（逐日重放 reconcile） |

## 3. API 速查（规格 §13 全端点）

| 端点 | 方法 | 用途 |
|---|---|---|
| `/api/ask` | POST | 问答：`{"question":"…"}` → 答案+引用（分区标注/探索标注）+冲突提示 |
| `/api/feedback` | POST | 证据结算：`{"question_id","kind":"up/close/down/escalate/reask"}`，同键幂等 |
| `/api/scan` | POST | 立即扫描工作区 |
| `/api/exceptions` | GET | 例外队列 + 待处置 + 举证期（含挑刺案卷） |
| `/api/adjudicate` | POST | `{"block_id","action":"pass/reject/restore/demote/archive"}` |
| `/api/admin/{op}` | POST | `kickback / full_review / tier0 / restore / import` |
| `/api/alerts` | GET | 红灯（异议达阈值 / 负证据异常）+ 挂起挑战者 |
| `/api/amend` | POST | `{"action":"replace/reject","old_id","new_id"}` 修正案 |
| `/api/conflicts` | GET | 未决冲突（含影子双答） |
| `/api/conflict/vote` | POST | `{"defender","challenger","choice":"new/old/novote"}` |
| `/api/clock/jump` | POST | `{"days":N}` 快进 |
| `/api/dashboard` · `/api/starmap` · `/api/events` · `/api/biography` | GET | 仪表盘 / 星图 / 审计流 / 知识传记 |
| `/api/config` | GET/POST | 参数表（白名单键可调；`promotion.policy=auto\|gated`） |
| `/api/providers` · `/api/providers/test` | GET/POST | LLM 接入配置 + 连通测试 |

## 4. LLM 接入（可选，坏了有退路）

四个 LLM 角色（作答/摘要/挑刺/判定）全部有本地确定性兜底：未配置供应商时
零调用、零网络，核心生命周期不受影响（规格 §3.3/§12）。

- 环境变量：`BETTERRAG_LLM_BASE_URL` / `BETTERRAG_LLM_API_KEY` / `BETTERRAG_LLM_MODEL`
  （OpenAI-compatible，填到 `/v1` 一级）；
- 或 UI「仪表盘 → 引擎接入」表单保存后点「测试连通」；配置持久化于 SQLite。
- 供应商连通失败会自动降级本地兜底并记录 `last_error`，服务不中断。

## 5. 日常运维

- **收材料**：把 `.md`/`.txt` 放进 `data/workspace/`（顶栏「立即扫描」或 `POST /api/scan`）。
  照搬原则：原文切片入库，AI 只写归纳摘要标签；内容未变的材料幂等跳过；
  内容变更生成新块（新生命周期），旧块因失去采纳自然衰减——不删除、留案底。
- **管理员导入**：冷启动通道，直进稳定区 0.85；铁律文件整篇 tier0
  （不自动降级、冲突必胜、记账零豁免，红灯+修正案通道，规格 §4.3）。
- **虚拟时钟**：默认 1 真实日 = 1 虚拟日（锚点持久化，重启续走）；`--virtual` 模式
  时间只经快进推进。快进=逐日重放 reconcile（幂等物化：棘轮/重锚/跨界迁移/举证期挑刺）。
- **举证期挑刺**：证据净攒跨 0.60 → 冻结 7 天 → 窗满自动挑刺（AI 或本地规则）→
  过则自证晋升 0.85；不过进例外队列；佐证不足解冻回学习区（积分保留）。
- **promotion.policy**：合规域可切 `gated`（人工门：一律进例外队列）。

## 6. 备份与恢复

- 全部状态在 `data/betterrag.db`（SQLite，WAL）；备份=复制该文件（连同 `-wal/-shm`）。
- 恢复：停服 → 替换文件 → 重启。事件为 append-only，重启后全量回装内存
  （案卷/异常窗/传记依赖完整链）。
- 重置：删除 `data/betterrag.db*` 后重新 `seed`。

## 7. 故障排查

| 症状 | 处置 |
|---|---|
| 答案「暂无材料」 | 工作区为空或语言差异大：先 `scan`，再在仪表盘看分区分布 |
| 「端口被占用」 | `serve --port 8788`；或 `netstat -ano | findstr 8787` 找到并结束旧进程 |
| LLM 调用失败 | 自动降级本地兜底（UI 引擎位显示「本地兜底」）；点「测试连通」看 `last_error` |
| 数据库锁死 | 只允许一个服务进程共享一个 db；多余进程结束即可 |
| 时钟不对 | 查 `meta` 表 `clock`（锚点）；测试/演示用 `--virtual` + `jump` |

## 8. 质量门

- `python -m unittest discover -s tests` → 49 项全绿（状态机/棘轮/晋升/铁律/仲裁/持久化/收纳/检索/运行时集成）。
- 端到端 17 项（种子→问答→反馈→重问→裁决→红灯→修正案→快进→传记→星图）见 `docs/demo演示脚本.md` 同款链路。
- 回归：`python engine/build_demo.py` 可随时重建单文件演示（`BetterRAG-demo.html`）。
