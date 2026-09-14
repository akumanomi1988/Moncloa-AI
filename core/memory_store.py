import sqlite3
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


class MemoryStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issue_id TEXT UNIQUE,
                issue_title TEXT,
                issue_description TEXT,
                options TEXT,
                status TEXT DEFAULT 'pending',
                outcome TEXT,
                is_simulation INTEGER DEFAULT 0,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at TIMESTAMP,
                error_message TEXT,
                option_ids TEXT,
                summary TEXT,
                applied_at TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS interventions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                phase INTEGER NOT NULL,
                round INTEGER DEFAULT 1,
                councilor_role TEXT NOT NULL,
                councilor_name TEXT,
                vote_choice TEXT,
                reasoning TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS nation_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                fullname TEXT,
                approval REAL,
                population INTEGER,
                industry TEXT,
                economy TEXT,
                deficit TEXT,
                civil_rights TEXT,
                economy_freedom TEXT,
                political_freedom TEXT,
                economy_score REAL,
                freedom_score REAL,
                census_data TEXT,
                captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS vision_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                summary TEXT,
                author_role TEXT,
                session_id INTEGER,
                is_active INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
            CREATE INDEX IF NOT EXISTS idx_interventions_session ON interventions(session_id);
            CREATE INDEX IF NOT EXISTS idx_snapshots_time ON nation_snapshots(captured_at);
        """)
            columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")
            }
            for name, definition in {
            "option_ids": "TEXT",
            "summary": "TEXT",
            "applied_at": "TIMESTAMP",
            }.items():
                if name not in columns:
                    self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {definition}")
            snapshot_columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(nation_snapshots)")
            }
            for name in ("economy_score", "freedom_score"):
                if name not in snapshot_columns:
                    self._conn.execute(f"ALTER TABLE nation_snapshots ADD COLUMN {name} REAL")
            self._conn.commit()

    # --- Sessions ---

    def create_session(self, issue_id: str, title: str, description: str,
                       options: list[str], is_simulation: bool = False,
                       option_ids: list[str] | None = None) -> int:
        existing = self._conn.execute(
            "SELECT id, status FROM sessions WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        if existing:
            if existing["status"] in ("error", "completed"):
                self._conn.execute("DELETE FROM interventions WHERE session_id = ?", (existing["id"],))
                self._conn.execute(
                    "UPDATE sessions SET status='pending', outcome=NULL, error_message=NULL, "
                    "started_at=CURRENT_TIMESTAMP, ended_at=NULL, applied_at=NULL, summary=NULL, "
                    "options=?, option_ids=? WHERE id=?",
                    (json.dumps(options), json.dumps(option_ids or []), existing["id"]),
                )
                self._conn.commit()
                return existing["id"]
            return existing["id"]
        cur = self._conn.execute(
            "INSERT INTO sessions (issue_id, issue_title, issue_description, options, option_ids, status, is_simulation) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (issue_id, title, description, json.dumps(options),
             json.dumps(option_ids or []), int(is_simulation))
        )
        self._conn.commit()
        return cur.lastrowid

    def update_session_status(self, session_id: int, status: str, outcome: str = None, error: str = None):
        fields = {"status": status}
        if outcome is not None:
            fields["outcome"] = outcome
        if error is not None:
            fields["error_message"] = error
        if status in ("completed", "error"):
            fields["ended_at"] = datetime.utcnow().isoformat()
        if status == "completed":
            fields["applied_at"] = datetime.utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        self._conn.execute(
            f"UPDATE sessions SET {set_clause} WHERE id = ?",
            (*fields.values(), session_id)
        )
        self._conn.commit()

    def get_session(self, session_id: int) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_pending_sessions(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE status NOT IN ('completed', 'error') "
            "ORDER BY started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def recover_stale_sessions(self, max_age_minutes: int = 10):
        self._conn.execute(
            "UPDATE sessions SET status='error', error_message=? "
            "WHERE status IN ('analyzing', 'debating', 'voting') "
            "AND started_at < datetime('now', ?)",
            ("Proceso interrumpido; sesión recuperada al reiniciar", f"-{max_age_minutes} minutes"),
        )
        self._conn.commit()

    def recover_session(self, session_id: int):
        self._conn.execute(
            "UPDATE sessions SET status='pending', error_message=NULL, ended_at=NULL "
            "WHERE id = ?", (session_id,)
        )
        self._conn.commit()

    def update_session_summary(self, session_id: int, summary: str):
        self._conn.execute(
            "UPDATE sessions SET summary = ? WHERE id = ?", (summary, session_id)
        )
        self._conn.commit()

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Interventions ---

    def add_intervention(self, session_id: int, phase: int, round_num: int,
                         role: str, name: str, vote: str = None,
                         reasoning: str = None, content: str = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO interventions (session_id, phase, round, councilor_role, "
            "councilor_name, vote_choice, reasoning, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, phase, round_num, role, name, vote, reasoning, content)
        )
        self._conn.commit()
        return cur.lastrowid

    def get_session_interventions(self, session_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM interventions WHERE session_id = ? ORDER BY id",
            (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_interventions(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT i.*, s.issue_title FROM interventions i "
            "JOIN sessions s ON i.session_id = s.id "
            "ORDER BY i.id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Snapshots ---

    def save_snapshot(self, data: dict):
        self._conn.execute(
            "INSERT INTO nation_snapshots (name, fullname, approval, population, "
            "industry, economy, deficit, civil_rights, economy_freedom, "
            "political_freedom, economy_score, freedom_score, census_data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data.get("name"), data.get("fullname"), data.get("approval"),
                data.get("population"), data.get("industry"),
                data.get("economy"), data.get("deficit"),
                data.get("civil_rights"), data.get("economy_freedom"),
                data.get("political_freedom"), data.get("economy_score"),
                data.get("freedom_score"),
                json.dumps(data.get("census", {}))
            )
        )
        self._conn.commit()

    def get_latest_snapshot(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM nation_snapshots ORDER BY captured_at DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_snapshots(self, limit: int = 120) -> list[dict]:
        limit = max(1, min(int(limit), 1000))
        rows = self._conn.execute(
            "SELECT * FROM nation_snapshots ORDER BY captured_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def get_recent_completed_sessions(self, limit: int = 10) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE status = 'completed' "
            "ORDER BY ended_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- Vision ---

    def save_vision(self, content: str, summary: str = None,
                    author_role: str = None, session_id: int = None):
        self._conn.execute(
            "UPDATE vision_documents SET is_active = 0 WHERE is_active = 1"
        )
        cur = self._conn.execute(
            "INSERT INTO vision_documents (content, summary, author_role, session_id, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (content, summary, author_role, session_id)
        )
        self._conn.commit()
        return cur.lastrowid

    def get_active_vision(self) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM vision_documents WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    # --- Stats ---

    def get_stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        completed = self._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE status = 'completed'"
        ).fetchone()[0]
        last = self._conn.execute(
            "SELECT issue_title, outcome, ended_at FROM sessions "
            "WHERE status = 'completed' ORDER BY ended_at DESC LIMIT 1"
        ).fetchone()
        return {
            "total_sessions": total,
            "completed": completed,
            "last_decision": dict(last) if last else None,
        }

    def close(self):
        self._conn.close()
