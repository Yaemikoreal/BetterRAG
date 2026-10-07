# -*- coding: utf-8 -*-
"""演示状态体检：打印关键日期的星图状态，用于验收。"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
d = json.load(open(os.path.join(ROOT, "web", "timeline.json"), encoding="utf-8"))
B = d["blocks"]
zc = {0: "学习", 1: "预备", 2: "稳定", 3: "待处置", 4: "归档", 5: "挂起"}
idx = {x[0]: x[1] for x in d["frames"]}

for day in (0, 90, 97, 103, 109, 300, 520):
    f = idx.get(day)
    if not f:
        continue
    print(f"=== day {day} ===")
    for i, eff, z, fl in sorted(f, key=lambda r: -r[1]):
        tags = "".join([
            " 铁律" if fl & 8 else "", " 举证期" if fl & 1 else "",
            " 待处置" if fl & 2 else "", " 归档" if fl & 16 else "",
            " 挂起" if fl & 4 else "", " 队列" if fl & 32 else "",
        ])
        print(f"  {B[i]['title']:<20} {eff/1000:.3f} {zc[z]}{tags}")

print(f"\nframes={len(d['frames'])} events={len(d['events'])} "
      f"branch_old={len(d['branches']['old'])} branch_novote={len(d['branches']['novote'])}")
print("events by type:")
from collections import Counter
print("  ", Counter(e["t"] for e in d["events"]).most_common())
