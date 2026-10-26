# -*- coding: utf-8 -*-
"""BetterRAG 持久化层 —— SQLite 单文件（规格 §10）。

表结构对齐规格 §10：materials / blocks / events / citations / conflicts /
llm_providers / config，另加 signals（反馈幂等去重）。
设计取舍：
  * 事件 append-only，按 seq 增量落盘（runtime 记录已刷盘水位）；
  * 块/材料/冲突在每次变更后整表 upsert（规模 ≤ 1e3 行，成本可忽略）；
  * WAL 模式 + 每次写提交，进程崩溃最多丢最后一笔内存态（可由扫描重建）。
仅使用标准库。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Dict, List, Optional

from core import Block, Engine, Material, ZONE_LEARN

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS materials (
  id TEXT PRIMARY KEY, title TEXT, source_path TEXT, summary TEXT,
  summary_version INTEGER, imported_by_admin INTEGER, tags TEXT,
  content_hash TEXT, created_at REAL
);
CREATE TABLE IF NOT EXISTS blocks (
  id TEXT PRIMARY KEY, material_id TEXT, chunk_index INTEGER, title TEXT,
  material_title TEXT, content TEXT, summary TEXT, tags TEXT,
  conf_stored REAL, decay_baseline_at REAL, last_engagement_at REAL,
  created_at REAL, frozen INTEGER, probation_until REAL, pending INTEGER,
  pending_reason TEXT, queued INTEGER, suspended INTEGER, archived INTEGER,
  tier0 INTEGER, iron_dissent INTEGER, iron_alert_at REAL,
  adopted_count INTEGER, tenure INTEGER, vet_sources INTEGER,
  superseded_by TEXT, angle REAL, jitter REAL, zone INTEGER
);
CREATE INDEX IF NOT EXISTS idx_blocks_material ON blocks(material_id);
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, day REAL, block_id TEXT,
  material_id TEXT, type TEXT, actor TEXT, delta REAL,
  confidence_after REAL, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_block ON events(block_id);
CREATE TABLE IF NOT EXISTS citations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, question_id TEXT,
  question TEXT, block_id TEXT, adopted INTEGER, outcome TEXT
);
CREATE TABLE IF NOT EXISTS conflicts (
  defender TEXT, challenger TEXT, count INTEGER, status TEXT,
  verdict TEXT, note TEXT, defender_answer TEXT, challenger_answer TEXT,
  created_day REAL, PRIMARY KEY (defender, challenger)
);
CREATE TABLE IF NOT EXISTS signals (
  question_id TEXT, kind TEXT, ts REAL, PRIMARY KEY (question_id, kind)
);
CREATE TABLE IF NOT EXISTS questions (
  id TEXT PRIMARY KEY, ts REAL, day REAL, question TEXT, tokens TEXT
);
CREATE TABLE IF NOT EXISTS llm_providers (
  id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT, base_url TEXT,
  api_key TEXT, model TEXT, ctx_window INTEGER, max_output INTEGER,
  ok INTEGER, last_error TEXT, checked_at REAL
);
CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY, value TEXT
);
"""


class Store:
    """SQLite 单文件存储。一个实例对应一个 betterrag.db。"""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.db.commit()

    def close(self) -> None:
        try:
            self.db.commit()
            self.db.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- meta
    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)))
        self.db.commit()

    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    # ---------------------------------------------------------------- config
    def set_config(self, items: Dict[str, float]) -> None:
        for k, v in items.items():
            self.db.execute("INSERT INTO config(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (k, json.dumps(v)))
        self.db.commit()

    def load_config(self) -> Dict[str, float]:
        return {k: json.loads(v) for k, v in
                self.db.execute("SELECT key,value FROM config")}

    # ---------------------------------------------------------------- 事件水位
    def event_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def append_events(self, events: List[dict], from_seq: int) -> None:
        """把 engine.events[from_seq:] 追加落盘。events 元素：d/t/b/delta/c/note/actor。"""
        rows = []
        for e in events[from_seq:]:
            bid = e.get("b") or ""
            mat = ""
            rows.append((e.get("d", 0.0), e.get("d", 0.0), bid, mat,
                         e.get("t", ""), e.get("actor", ""), e.get("delta", 0.0),
                         e.get("c", 0.0), e.get("note", "")))
        self.db.executemany(
            "INSERT INTO events(ts,day,block_id,material_id,type,actor,delta,"
            "confidence_after,note) VALUES(?,?,?,?,?,?,?,?,?)", rows)
        self.db.commit()

    def events(self, limit: int = 200, block_id: str = "", etype: str = "") -> List[dict]:
        q = "SELECT seq,day,type,block_id,actor,delta,confidence_after,note FROM events"
        cond, args = [], []
        if block_id:
            cond.append("block_id=?"); args.append(block_id)
        if etype:
            cond.append("type=?"); args.append(etype)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        out = []
        for seq, day, t, bid, actor, delta, conf, note in self.db.execute(q, args):
            out.append({"seq": seq, "d": day, "t": t, "b": bid, "actor": actor,
                        "delta": delta, "c": conf, "note": note})
        return out

    # ---------------------------------------------------------------- 快照
    def save_blocks(self, eng: Engine) -> None:
        mats = [(m.id, m.title, m.source_path, m.summary, m.summary_version,
                 int(m.imported_by_admin), json.dumps(m.tags, ensure_ascii=False),
                 m.content_hash, m.created_at)
                for m in eng.materials.values()]
        self.db.execute("DELETE FROM materials")
        self.db.executemany(
            "INSERT INTO materials(id,title,source_path,summary,summary_version,"
            "imported_by_admin,tags,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)", mats)

        blks = []
        for b in eng.blocks.values():
            blks.append((b.id, b.material_id, b.chunk_index, b.title, b.material_title,
                         b.content, b.summary, json.dumps(b.tags, ensure_ascii=False),
                         b.conf_stored, b.decay_baseline_at, b.last_engagement_at,
                         b.created_at, int(b.frozen), b.probation_until, int(b.pending),
                         b.pending_reason, int(b.queued), int(b.suspended),
                         int(b.archived), int(b.tier0), b.iron_dissent, b.iron_alert_at,
                         b.adopted_count, b.tenure, b.vet_sources, b.superseded_by,
                         b.angle, b.jitter, b.zone))
        self.db.execute("DELETE FROM blocks")
        self.db.executemany(
            "INSERT INTO blocks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            blks)

        confs = [(r["defender"], r["challenger"], r["count"], r["status"],
                  r.get("verdict", ""), r.get("note", ""),
                  r.get("defender_answer", ""), r.get("challenger_answer", ""),
                  r.get("created_day", 0.0)) for r in eng.conflicts]
        self.db.execute("DELETE FROM conflicts")
        self.db.executemany(
            "INSERT INTO conflicts VALUES(?,?,?,?,?,?,?,?,?)", confs)
        self.db.commit()

    def load(self, eng: Engine) -> bool:
        """把库内快照装回 engine。返回是否找到快照。"""
        mats = self.db.execute(
            "SELECT id,title,source_path,summary,summary_version,imported_by_admin,"
            "tags,content_hash,created_at FROM materials").fetchall()
        blks = self.db.execute(
            "SELECT id,material_id,chunk_index,title,material_title,content,summary,tags,"
            "conf_stored,decay_baseline_at,last_engagement_at,created_at,frozen,"
            "probation_until,pending,pending_reason,queued,suspended,archived,tier0,"
            "iron_dissent,iron_alert_at,adopted_count,tenure,vet_sources,superseded_by,"
            "angle,jitter,zone FROM blocks").fetchall()
        if not blks and not mats:
            return False
        for (mid, title, sp, summary, sv, admin, tags, chash, created) in mats:
            eng.materials[mid] = Material(
                id=mid, title=title, source_path=sp, summary=summary,
                summary_version=sv, imported_by_admin=bool(admin),
                tags=json.loads(tags or "[]"), content_hash=chash or "", created_at=created)
        for row in blks:
            (bid, mid, ci, title, mtitle, content, summary, tags, conf, dbase, leng,
             created, frozen, puntil, pending, preason, queued, susp, arch, tier0,
             dissent, alert_at, adopted, tenure, vsrc, sup, angle, jitter, zone) = row
            eng.blocks[bid] = Block(
                id=bid, material_id=mid, title=title, material_title=mtitle,
                chunk_index=ci, content=content or "", summary=summary or "",
                tags=json.loads(tags or "[]"), conf_stored=conf,
                decay_baseline_at=dbase, last_engagement_at=leng, created_at=created,
                frozen=bool(frozen), probation_until=puntil, pending=bool(pending),
                pending_reason=preason or "", queued=bool(queued), suspended=bool(susp),
                archived=bool(arch), tier0=bool(tier0), iron_dissent=dissent,
                iron_alert_at=alert_at if alert_at is not None else -1e9,
                adopted_count=adopted, tenure=tenure, vet_sources=vsrc,
                superseded_by=sup, angle=angle or 0.0, jitter=jitter or 0.0,
                zone=int(zone) if zone is not None else ZONE_LEARN)
        for (d, c, cnt, status, verdict, note, dans, chans, cday) in self.db.execute(
                "SELECT defender,challenger,count,status,verdict,note,defender_answer,"
                "challenger_answer,created_day FROM conflicts"):
            eng.conflicts.append({"defender": d, "challenger": c, "count": cnt,
                                  "status": status, "verdict": verdict, "note": note,
                                  "defender_answer": dans or "",
                                  "challenger_answer": chans or "",
                                  "created_day": cday})
        return True

    # ---------------------------------------------------------------- 引用/信号/问题
    def add_citations(self, question_id: str, question: str,
                      block_ids: List[str], ts: float) -> None:
        self.db.executemany(
            "INSERT INTO citations(ts,question_id,question,block_id,adopted,outcome) "
            "VALUES(?,?,?,?,1,'')",
            [(ts, question_id, question, bid) for bid in block_ids])
        self.db.commit()

    def adopted_blocks(self, question_id: str) -> List[str]:
        return [r[0] for r in self.db.execute(
            "SELECT block_id FROM citations WHERE question_id=? AND adopted=1",
            (question_id,))]

    def recent_questions(self, limit: int = 20) -> List[dict]:
        return [{"id": r[0], "ts": r[1], "day": r[2], "question": r[3], "tokens": r[4]}
                for r in self.db.execute(
                    "SELECT id,ts,day,question,tokens FROM questions "
                    "ORDER BY ts DESC LIMIT ?", (limit,))]

    def add_question(self, qid: str, ts: float, day: float, question: str,
                     tokens: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO questions(id,ts,day,question,tokens) "
                        "VALUES(?,?,?,?,?)", (qid, ts, day, question, tokens))
        self.db.commit()

    def mark_signal(self, question_id: str, kind: str) -> bool:
        """幂等反馈：同一 (question_id, kind) 只结算一次。返回是否首次。"""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO signals(question_id,kind,ts) VALUES(?,?,?)",
            (question_id, kind, time.time()))
        self.db.commit()
        return cur.rowcount > 0

    # ---------------------------------------------------------------- provider
    def save_provider(self, p: dict) -> None:
        self.db.execute(
            "INSERT INTO llm_providers(id,name,base_url,api_key,model,ctx_window,"
            "max_output,ok,last_error,checked_at) VALUES(1,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name,base_url=excluded.base_url,"
            "api_key=excluded.api_key,model=excluded.model,ctx_window=excluded.ctx_window,"
            "max_output=excluded.max_output,ok=excluded.ok,last_error=excluded.last_error,"
            "checked_at=excluded.checked_at",
            (p.get("name", ""), p.get("base_url", ""), p.get("api_key", ""),
             p.get("model", ""), p.get("ctx_window", 8192), p.get("max_output", 2048),
             int(p.get("ok", False)), p.get("last_error", ""), p.get("checked_at", 0.0)))
        self.db.commit()

    def load_provider(self) -> Optional[dict]:
        row = self.db.execute(
            "SELECT name,base_url,api_key,model,ctx_window,max_output,ok,last_error,"
            "checked_at FROM llm_providers WHERE id=1").fetchone()
        if not row:
            return None
        return {"name": row[0], "base_url": row[1], "api_key": row[2], "model": row[3],
                "ctx_window": row[4], "max_output": row[5], "ok": bool(row[6]),
                "last_error": row[7], "checked_at": row[8]}
