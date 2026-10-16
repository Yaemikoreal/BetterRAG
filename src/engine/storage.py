# -*- coding: utf-8 -*-
"""
BetterRAG SQLite + sqlite-vec 存储层实现
严格实现 docs/BetterRAG生产级开发实施方案.md §3 数据模型设计
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
import jieba

from src.engine.config import DEFAULT_DB_PATH, EMBEDDING_DIM
from src.engine.models import (
    Material, Block, AuditEvent, Citation, ConflictRecord, utcnow_str
)

try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False


class Storage:
    """SQLite 持久化引擎，支持 WAL 模式、外键约束、全文索引与向量扩展"""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        # 确保数据库父目录存在
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self.conn = self._create_connection()
        self.init_db()

    def _create_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        
        # 尝试加载 sqlite-vec 扩展
        if HAS_SQLITE_VEC:
            try:
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
            except Exception as e:
                # 即使加载失败也平稳降级使用 BLOB 向量存储
                pass
        return conn

    def get_connection(self) -> sqlite3.Connection:
        return self.conn

    def init_db(self) -> None:
        """执行 DDL 建表与核心性能索引"""
        cursor = self.conn.cursor()
        
        # 1. 文档材料表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS materials (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source_path TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            file_type TEXT NOT NULL,
            is_evergreen BOOLEAN DEFAULT 0,
            is_tier0 BOOLEAN DEFAULT 0,
            imported_by TEXT DEFAULT 'agent',
            summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 2. 知识块表 (核心治理单元)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS blocks (
            id TEXT PRIMARY KEY,
            material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
            parent_scope_id TEXT REFERENCES blocks(id),
            is_condition_header BOOLEAN DEFAULT 0,
            chunk_index INTEGER NOT NULL,
            context_prefix TEXT,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            is_evergreen BOOLEAN DEFAULT 0,
            embedding BLOB,
            
            conf_stored REAL DEFAULT 0.30,
            tenure INTEGER DEFAULT 0,
            last_engagement_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decay_baseline_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            
            status TEXT DEFAULT 'active',
            probation_until TIMESTAMP,
            pending_reason TEXT,
            superseded_by TEXT REFERENCES blocks(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 3. 不可变审计事件表 (Append-only Audit Log)
        # block_id 仅做逻辑记录，不加物理 FK，防材料级联删除时外键悬空炸弹
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            block_id TEXT,
            material_id TEXT,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL,
            delta REAL DEFAULT 0.0,
            confidence_after REAL NOT NULL,
            note TEXT
        );
        """)

        # 4. 问答采纳与证据追踪表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS citations (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            question TEXT NOT NULL,
            block_id TEXT NOT NULL REFERENCES blocks(id),
            salience_weight REAL DEFAULT 1.0,
            adopted BOOLEAN DEFAULT 1,
            outcome_feedback TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 5. 冲突仲裁与静默影子表
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS conflicts (
            id TEXT PRIMARY KEY,
            defender_id TEXT NOT NULL REFERENCES blocks(id),
            challenger_id TEXT NOT NULL REFERENCES blocks(id),
            defender_hash TEXT NOT NULL,
            challenger_hash TEXT NOT NULL,
            clash_count INTEGER DEFAULT 1,
            verdict TEXT NOT NULL DEFAULT 'conflict',
            status TEXT NOT NULL DEFAULT 'open',
            shadow_answer_old TEXT,
            shadow_answer_new TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 6. 核心性能索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_blocks_material ON blocks(material_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_blocks_status ON blocks(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_blocks_content_hash ON blocks(content_hash);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_citations_session ON citations(session_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_block ON events(block_id);")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_conflicts_pair ON conflicts(defender_id, challenger_id);")

        # 7. 中文分词 FTS5 全文搜索虚拟表 (分词空格虚列支持 BM25)
        try:
            cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(
                id UNINDEXED,
                content,
                content_segmented,
                tokenize='unicode61'
            );
            """)
        except Exception:
            pass

        # 8. sqlite-vec 向量索引表 (如果加载成功)
        if HAS_SQLITE_VEC:
            try:
                cursor.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_blocks USING vec0(
                    block_id TEXT PRIMARY KEY,
                    embedding float[{EMBEDDING_DIM}]
                );
                """)
            except Exception:
                pass

        self.conn.commit()

    # ==================== Material CRUD ====================

    def save_material(self, m: Material) -> None:
        with self.conn:
            self.conn.execute("""
            INSERT INTO materials (
                id, title, source_path, file_hash, file_type,
                is_evergreen, is_tier0, imported_by, summary, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                source_path = excluded.source_path,
                file_hash = excluded.file_hash,
                file_type = excluded.file_type,
                is_evergreen = excluded.is_evergreen,
                is_tier0 = excluded.is_tier0,
                imported_by = excluded.imported_by,
                summary = excluded.summary,
                updated_at = excluded.updated_at;
            """, (
                m.id, m.title, m.source_path, m.file_hash, m.file_type,
                int(m.is_evergreen), int(m.is_tier0), m.imported_by, m.summary,
                m.created_at, m.updated_at
            ))

    def get_material(self, material_id: str) -> Optional[Material]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM materials WHERE id = ?;", (material_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return Material(
            id=row["id"],
            title=row["title"],
            source_path=row["source_path"],
            file_hash=row["file_hash"],
            file_type=row["file_type"],
            is_evergreen=bool(row["is_evergreen"]),
            is_tier0=bool(row["is_tier0"]),
            imported_by=row["imported_by"],
            summary=row["summary"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"])
        )

    def get_material_by_path(self, source_path: str) -> Optional[Material]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM materials WHERE source_path = ?;", (source_path,))
        row = cursor.fetchone()
        if not row:
            return None
        return Material(
            id=row["id"],
            title=row["title"],
            source_path=row["source_path"],
            file_hash=row["file_hash"],
            file_type=row["file_type"],
            is_evergreen=bool(row["is_evergreen"]),
            is_tier0=bool(row["is_tier0"]),
            imported_by=row["imported_by"],
            summary=row["summary"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"])
        )

    def get_all_materials(self) -> List[Material]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM materials ORDER BY updated_at DESC;")
        res = []
        for row in cursor.fetchall():
            res.append(Material(
                id=row["id"],
                title=row["title"],
                source_path=row["source_path"],
                file_hash=row["file_hash"],
                file_type=row["file_type"],
                is_evergreen=bool(row["is_evergreen"]),
                is_tier0=bool(row["is_tier0"]),
                imported_by=row["imported_by"],
                summary=row["summary"],
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"])
            ))
        return res

    def delete_material(self, material_id: str) -> None:
        """删除材料及其级联知识块"""
        with self.conn:
            # 清理 FTS
            cursor = self.conn.cursor()
            cursor.execute("SELECT id FROM blocks WHERE material_id = ?;", (material_id,))
            block_ids = [r[0] for r in cursor.fetchall()]
            for bid in block_ids:
                try:
                    cursor.execute("DELETE FROM blocks_fts WHERE id = ?;", (bid,))
                except Exception:
                    pass
                if HAS_SQLITE_VEC:
                    try:
                        cursor.execute("DELETE FROM vec_blocks WHERE block_id = ?;", (bid,))
                    except Exception:
                        pass
            cursor.execute("DELETE FROM materials WHERE id = ?;", (material_id,))

    # ==================== Block CRUD ====================

    def _row_to_block(self, row: sqlite3.Row) -> Block:
        return Block(
            id=row["id"],
            material_id=row["material_id"],
            content=row["content"],
            content_hash=row["content_hash"],
            chunk_index=row["chunk_index"],
            parent_scope_id=row["parent_scope_id"],
            is_condition_header=bool(row["is_condition_header"]),
            context_prefix=row["context_prefix"],
            is_evergreen=bool(row["is_evergreen"]),
            embedding=row["embedding"],
            conf_stored=float(row["conf_stored"]),
            tenure=int(row["tenure"]),
            last_engagement_at=str(row["last_engagement_at"]),
            decay_baseline_at=str(row["decay_baseline_at"]),
            status=row["status"],
            probation_until=str(row["probation_until"]) if row["probation_until"] else None,
            pending_reason=row["pending_reason"],
            superseded_by=row["superseded_by"],
            created_at=str(row["created_at"])
        )

    def save_block(self, b: Block) -> None:
        with self.conn:
            self.conn.execute("""
            INSERT INTO blocks (
                id, material_id, parent_scope_id, is_condition_header,
                chunk_index, context_prefix, content, content_hash,
                is_evergreen, embedding, conf_stored, tenure,
                last_engagement_at, decay_baseline_at, status,
                probation_until, pending_reason, superseded_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                material_id = excluded.material_id,
                parent_scope_id = excluded.parent_scope_id,
                is_condition_header = excluded.is_condition_header,
                chunk_index = excluded.chunk_index,
                context_prefix = excluded.context_prefix,
                content = excluded.content,
                content_hash = excluded.content_hash,
                is_evergreen = excluded.is_evergreen,
                embedding = excluded.embedding,
                conf_stored = excluded.conf_stored,
                tenure = excluded.tenure,
                last_engagement_at = excluded.last_engagement_at,
                decay_baseline_at = excluded.decay_baseline_at,
                status = excluded.status,
                probation_until = excluded.probation_until,
                pending_reason = excluded.pending_reason,
                superseded_by = excluded.superseded_by;
            """, (
                b.id, b.material_id, b.parent_scope_id, int(b.is_condition_header),
                b.chunk_index, b.context_prefix, b.content, b.content_hash,
                int(b.is_evergreen), b.embedding, b.conf_stored, b.tenure,
                b.last_engagement_at, b.decay_baseline_at, b.status,
                b.probation_until, b.pending_reason, b.superseded_by, b.created_at
            ))

            # 更新 FTS5
            try:
                words = " ".join(jieba.cut(b.content))
                self.conn.execute("DELETE FROM blocks_fts WHERE id = ?;", (b.id,))
                self.conn.execute(
                    "INSERT INTO blocks_fts (id, content, content_segmented) VALUES (?, ?, ?);",
                    (b.id, b.content, words)
                )
            except Exception:
                pass

            # 更新 vec_blocks
            if HAS_SQLITE_VEC and b.embedding:
                try:
                    self.conn.execute("DELETE FROM vec_blocks WHERE block_id = ?;", (b.id,))
                    self.conn.execute(
                        "INSERT INTO vec_blocks (block_id, embedding) VALUES (?, ?);",
                        (b.id, b.embedding)
                    )
                except Exception:
                    pass

    def save_blocks(self, blocks: List[Block]) -> None:
        for b in blocks:
            self.save_block(b)

    def get_block(self, block_id: str) -> Optional[Block]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM blocks WHERE id = ?;", (block_id,))
        row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_block(row)

    def get_blocks_by_material(self, material_id: str) -> List[Block]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM blocks WHERE material_id = ? ORDER BY chunk_index ASC;", (material_id,))
        return [self._row_to_block(r) for r in cursor.fetchall()]

    def get_all_active_blocks(self) -> List[Block]:
        """获取所有 active 或 pending 态知识块（父块优先于子块）"""
        cursor = self.conn.cursor()
        cursor.execute("""
        SELECT * FROM blocks 
        WHERE status IN ('active', 'pending') 
        ORDER BY CASE WHEN parent_scope_id IS NULL THEN 0 ELSE 1 END, id ASC;
        """)
        return [self._row_to_block(r) for r in cursor.fetchall()]

    def archive_block(self, block_id: str, note: str = "") -> None:
        """标记归档知识块并级联挂起孤儿子块"""
        with self.conn:
            self.conn.execute(
                "UPDATE blocks SET status = 'archived', pending_reason = ? WHERE id = ?;",
                (note or "归档", block_id)
            )
            self.suspend_child_blocks(block_id, reason=f"前置父块 {block_id} 已归档")

    def suspend_child_blocks(self, parent_id: str, reason: str = "") -> List[str]:
        """级联挂起孤儿子块，防止前置条件丢失后断章取义"""
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("SELECT id FROM blocks WHERE parent_scope_id = ? AND status != 'archived';", (parent_id,))
            child_ids = [r[0] for r in cursor.fetchall()]
            if child_ids:
                placeholders = ",".join("?" for _ in child_ids)
                cursor.execute(f"""
                UPDATE blocks 
                SET status = 'suspended', pending_reason = ? 
                WHERE id IN ({placeholders});
                """, [reason or f"父级前置块 {parent_id} 已退场"] + child_ids)
            return child_ids

    # ==================== Audit Event Logging ====================

    def log_event(
        self,
        block_id: Optional[str],
        material_id: Optional[str],
        event_type: str,
        actor: str = "engine",
        delta: float = 0.0,
        confidence_after: float = 0.0,
        note: Optional[str] = None
    ) -> int:
        """写入不可变审计事件"""
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute("""
            INSERT INTO events (
                block_id, material_id, event_type, actor, delta, confidence_after, note
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (
                block_id, material_id, event_type, actor, delta, confidence_after, note
            ))
            return cursor.lastrowid

    def get_events(self, block_id: Optional[str] = None, limit: int = 100) -> List[AuditEvent]:
        cursor = self.conn.cursor()
        if block_id:
            cursor.execute(
                "SELECT * FROM events WHERE block_id = ? ORDER BY id DESC LIMIT ?;",
                (block_id, limit)
            )
        else:
            cursor.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?;",
                (limit,)
            )
        res = []
        for row in cursor.fetchall():
            res.append(AuditEvent(
                id=row["id"],
                ts=str(row["ts"]),
                block_id=row["block_id"],
                material_id=row["material_id"],
                event_type=row["event_type"],
                actor=row["actor"],
                delta=float(row["delta"]),
                confidence_after=float(row["confidence_after"]),
                note=row["note"]
            ))
        return res

    # ==================== Citations ====================

    def save_citation(self, c: Citation) -> None:
        with self.conn:
            self.conn.execute("""
            INSERT INTO citations (
                id, session_id, question, block_id, salience_weight, adopted, outcome_feedback, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                salience_weight = excluded.salience_weight,
                adopted = excluded.adopted,
                outcome_feedback = excluded.outcome_feedback;
            """, (
                c.id, c.session_id, c.question, c.block_id,
                c.salience_weight, int(c.adopted), c.outcome_feedback, c.created_at
            ))

    def get_citations_by_session(self, session_id: str) -> List[Citation]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM citations WHERE session_id = ?;", (session_id,))
        res = []
        for row in cursor.fetchall():
            res.append(Citation(
                id=row["id"],
                session_id=row["session_id"],
                question=row["question"],
                block_id=row["block_id"],
                salience_weight=float(row["salience_weight"]),
                adopted=bool(row["adopted"]),
                outcome_feedback=row["outcome_feedback"],
                created_at=str(row["created_at"])
            ))
        return res

    # ==================== Conflicts ====================

    def save_conflict(self, c: ConflictRecord) -> None:
        with self.conn:
            self.conn.execute("""
            INSERT INTO conflicts (
                id, defender_id, challenger_id, defender_hash, challenger_hash,
                clash_count, verdict, status, shadow_answer_old, shadow_answer_new,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(defender_id, challenger_id) DO UPDATE SET
                defender_hash = excluded.defender_hash,
                challenger_hash = excluded.challenger_hash,
                clash_count = excluded.clash_count,
                verdict = excluded.verdict,
                status = excluded.status,
                shadow_answer_old = excluded.shadow_answer_old,
                shadow_answer_new = excluded.shadow_answer_new,
                updated_at = excluded.updated_at;
            """, (
                c.id, c.defender_id, c.challenger_id, c.defender_hash, c.challenger_hash,
                c.clash_count, c.verdict, c.status, c.shadow_answer_old, c.shadow_answer_new,
                c.created_at, c.updated_at
            ))

    def get_conflict(self, defender_id: str, challenger_id: str) -> Optional[ConflictRecord]:
        cursor = self.conn.cursor()
        cursor.execute("""
        SELECT * FROM conflicts WHERE defender_id = ? AND challenger_id = ?;
        """, (defender_id, challenger_id))
        row = cursor.fetchone()
        if not row:
            return None
        return ConflictRecord(
            id=row["id"],
            defender_id=row["defender_id"],
            challenger_id=row["challenger_id"],
            defender_hash=row["defender_hash"],
            challenger_hash=row["challenger_hash"],
            clash_count=int(row["clash_count"]),
            verdict=row["verdict"],
            status=row["status"],
            shadow_answer_old=row["shadow_answer_old"],
            shadow_answer_new=row["shadow_answer_new"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"])
        )

    def get_all_conflicts(
        self,
        verdict: Optional[str] = None,
        status: Optional[str] = None
    ) -> List[ConflictRecord]:
        cursor = self.conn.cursor()
        query = "SELECT * FROM conflicts WHERE 1=1"
        params = []
        if verdict:
            query += " AND verdict = ?"
            params.append(verdict)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY clash_count DESC, updated_at DESC;"
        cursor.execute(query, tuple(params))
        res = []
        for row in cursor.fetchall():
            res.append(ConflictRecord(
                id=row["id"],
                defender_id=row["defender_id"],
                challenger_id=row["challenger_id"],
                defender_hash=row["defender_hash"],
                challenger_hash=row["challenger_hash"],
                clash_count=int(row["clash_count"]),
                verdict=row["verdict"],
                status=row["status"],
                shadow_answer_old=row["shadow_answer_old"],
                shadow_answer_new=row["shadow_answer_new"],
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"])
            ))
        return res

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass
