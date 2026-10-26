# -*- coding: utf-8 -*-
"""BetterRAG 命令行入口。

  python -m engine.cli serve   [--port 8787] [--demo-seed] [--virtual]
  python -m engine.cli seed    [--demo]        # 管理员导入 + 工作区扫描（--demo 预置演示状态）
  python -m engine.cli scan
  python -m engine.cli status
  python -m engine.cli jump    N
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_DEFAULT = os.path.join(ROOT, "data", "betterrag.db")
WS_DEFAULT = os.path.join(ROOT, "data", "workspace")

# 冷启动语料角色（与 build_demo.CORPUS 对齐：8 管理员导入含 2 铁律）
ADMIN_FILES = {
    "计费稽核铁律": True, "入网合规规范": True,
    "5G 畅享套餐资费": False, "4G 全国流量包资费": False,
    "家庭宽带融合套餐": False, "副卡与共享规则": False,
    "停机保号与复机规则": False, "国际漫游资费": False,
}

# 演示预成长（材料 → 天数/证据笔数），复现「挑战者预成长 0.62」起点
PREGROW = {
    "老用户专属折扣政策": (145, 18),
    "5G 套餐升级指引": (105, 17),
    "停售公告：4G 全国流量包": (55, 16),
    "政策方向调整": (38, 8),
    "校园套餐暑期活动": (118, 3),
    "宽带提速不加价说明": (88, 5),
}
EXCEPTION_FILE = "5G 套餐升级指引"
EXCEPTION_REASONS = [
    "与稳定区《4G 全国流量包资费》存在未决矛盾（迁移路径与停售口径不一致）",
    "近 7 日同题重问聚集（3 次），疑似答案未解决用户问题",
    "独立佐证仅 1 源，低于下限 3 源",
]


def _runtime(args):
    from runtime import Runtime
    return Runtime(args.db, args.workspace, virtual=getattr(args, "virtual", False))


def _find_block(rt, material_id: str):
    bs = rt.eng.material_blocks(material_id)
    return bs[0] if bs else None


def cmd_seed(args) -> None:
    from ingest import material_id_for
    rt = _runtime(args)
    print(f"[seed] 数据库：{args.db}")
    if rt._has_snapshot:
        print("[seed] 库内已有快照 —— 跳过（如需重置请删除 data/betterrag.db*）")
        rt.close()
        return
    # 1) 管理员导入（冷启动通道：直进稳定区 0.85，铁律带 tier0）
    n_admin = 0
    for name, iron in ADMIN_FILES.items():
        path = os.path.join(args.workspace, name + ".md")
        if os.path.isfile(path):
            rt.admin_import(path, tier0=iron)
            n_admin += 1
    print(f"[seed] 管理员导入：{n_admin} 篇（其中铁律 "
          f"{sum(1 for v in ADMIN_FILES.values() if v)} 篇）")
    # 2) Agent 扫描收纳（0.30 入学习区）
    report = rt.scan()
    print(f"[seed] 工作区扫描：{report['scanned']} 篇，收纳 "
          f"{sum(x['blocks'] for x in report['ingested'])} 块，未变 {report['unchanged']} 篇")
    # 3) 演示预成长（可选）：合成历史证据，复现排期表的挑战者预成长
    if getattr(args, "demo", False):
        eng = rt.eng
        for name, (days, count) in PREGROW.items():
            mid = material_id_for(os.path.join(args.workspace, name + ".md"))
            b = _find_block(rt, mid)
            if b is None:
                continue
            step = max(1, days // count)
            for i in range(count):
                eng.now = float(-days + i * step)
                eng.evidence(b.id, "pos", "工单一次闭环（预成长合成）")
                eng.adopt(b.id, "被答案采纳（预成长合成·活性）")
        eng.now = -5.0
        b = _find_block(rt, material_id_for(
            os.path.join(args.workspace, EXCEPTION_FILE + ".md")))
        if b is not None:
            eng.vet(b.id, False, EXCEPTION_REASONS, 1)
        iron = _find_block(rt, material_id_for(
            os.path.join(args.workspace, "计费稽核铁律.md")))
        if iron is not None:
            for _ in range(2):
                iron.iron_dissent += 1
                eng._log("IRON_DISSENT", iron, 0.0,
                         f"铁律异议 +1（{iron.iron_dissent}/3）· 历史预置", "detector")
        rt.set_clock(0)
        print("[seed] 演示预成长完成：挑战者已预置（预备区/例外队列/铁律异议 2/3）")
    print(f"[seed] 完成：D{int(rt.eng.now)}，"
          f"{len(rt.eng.blocks)} 块 / {len(rt.eng.materials)} 材料 / "
          f"{len(rt.eng.events)} 事件")
    rt.close()


def cmd_serve(args) -> None:
    if getattr(args, "demo_seed", False):
        cmd_seed(argparse.Namespace(db=args.db, workspace=args.workspace,
                                    virtual=args.virtual, demo=True))
    rt = _runtime(args)
    from server import serve
    serve(rt, host=args.host, port=args.port)


def cmd_scan(args) -> None:
    rt = _runtime(args)
    report = rt.scan()
    print(f"扫描：{report['scanned']} 篇，收纳 "
          f"{sum(x['blocks'] for x in report['ingested'])} 块（{report['ingested']}），"
          f"未变 {report['unchanged']} 篇")
    rt.close()


def cmd_status(args) -> None:
    rt = _runtime(args)
    st = rt.state()
    print(f"虚拟日 D{st['day']}  引擎={st['provider']['mode']}")
    print(f"分区分布：{st['distribution']}")
    for b in sorted(st["blocks"], key=lambda x: -x["eff"])[:12]:
        tags = "".join([t for t, on in [
            (" 铁律", b["tier0"]), (" 举证期", b["frozen"]), (" 待处置", b["pending"]),
            (" 队列", b["queued"]), (" 挂起", b["suspended"]), (" 归档", b["archived"])]
            if on])
        print(f"  {b['title'][:20]:<22} eff={b['eff']:.3f}{tags}")
    ex = rt.exceptions()
    print(f"例外队列 {len(ex['queue'])} · 待处置 {len(ex['pending'])} · "
          f"举证期 {len(ex['probation'])}")
    al = rt.alerts()
    if al["red"]:
        print(f"红灯 {len(al['red'])} 条：{[r['title'] for r in al['red']]}")
    rt.close()


def cmd_jump(args) -> None:
    rt = _runtime(args)
    out = rt.jump(args.days)
    print(f"快进 {out['jumped']} 天 → D{out['day']}")
    rt.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="betterrag",
                                description="BetterRAG 自演进知识库 Agent")
    p.add_argument("--db", default=DB_DEFAULT)
    p.add_argument("--workspace", default=WS_DEFAULT)
    p.add_argument("--virtual", action="store_true",
                   help="虚拟时钟模式：时间只经 jump 推进（演示/测试）")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="启动 HTTP 服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--demo-seed", action="store_true",
                   help="首次启动时自动种子（含演示预成长）")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("seed", help="种子：管理员导入 + 扫描工作区")
    s.add_argument("--demo", action="store_true", help="预置演示状态（预成长/例外队列/异议）")
    s.set_defaults(fn=cmd_seed)
    s = sub.add_parser("scan", help="立即扫描工作区")
    s.set_defaults(fn=cmd_scan)
    s = sub.add_parser("status", help="查看分区分布与队列")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("jump", help="虚拟时钟快进 N 天")
    s.add_argument("days", type=int)
    s.set_defaults(fn=cmd_jump)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
