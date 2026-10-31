#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — SQLite Structured Memory Store
==================================================================
Zero-overhead persistent SQLite3 database (`data/memory.db`) storing:
  1. `preferences`: User key-value facts and preferences (e.g., favorite IDE,
     Mom's phone number, preferred music playlist, wake-word preferences).
  2. `conversation_history`: Timestamped user commands, matched intents, and
     E.V. responses for context recall across sessions.
  3. `command_audit`: Execution telemetry for workspaces, timed commands, and
     phone actions.

Uses WAL (Write-Ahead Logging) mode and thread-local/locked connections for
safe concurrent access from background voice and scheduler threads.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class SQLiteMemoryStore:
    """Thread-safe SQLite3 structured store for user facts, preferences, and history."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, base_dir: Optional[Path] = None) -> None:
        self.config = config or {}
        self.base_dir = base_dir or Path.cwd()
        self.logger = logging.getLogger("ev.sqlite")

        mem_cfg = config.get("memory", {})
        rel_db = str(mem_cfg.get("sqlite_path", "data/memory.db"))
        self.db_path: Path = (
            (base_dir / rel_db).resolve() if not Path(rel_db).is_absolute() else Path(rel_db)
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS preferences (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        category TEXT DEFAULT 'general',
                        updated_at REAL NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_text TEXT NOT NULL,
                        assistant_text TEXT NOT NULL,
                        intent_name TEXT DEFAULT 'unknown',
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()

    # =========================================================================
    # User Preferences & Facts CRUD
    # =========================================================================
    def set_preference(self, key: str, value: str, category: str = "general") -> None:
        clean_k = (key or "").strip().lower()
        clean_v = (value or "").strip()
        if not clean_k:
            return
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO preferences (key, value, category, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        category = excluded.category,
                        updated_at = excluded.updated_at
                    """,
                    (clean_k, clean_v, category, time.time()),
                )
                conn.commit()

    def get_preference(self, key: str, default: Optional[str] = None) -> Optional[str]:
        clean_k = (key or "").strip().lower()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM preferences WHERE key = ?",
                    (clean_k,),
                ).fetchone()
                return str(row["value"]) if row else default

    def list_preferences(self, limit: int = 20) -> Dict[str, str]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT key, value FROM preferences ORDER BY updated_at DESC LIMIT ?",
                    (max(1, limit),),
                ).fetchall()
                return {str(r["key"]): str(r["value"]) for r in rows}

    def search_facts(self, query: str, limit: int = 5) -> List[Tuple[str, str]]:
        """Keyword search across stored user preferences/facts."""
        tokens = [t.strip().lower() for t in (query or "").split() if len(t.strip()) >= 2]
        if not tokens:
            return list(self.list_preferences(limit=limit).items())

        results: List[Tuple[str, str]] = []
        with self._lock:
            with self._connect() as conn:
                for tok in tokens[:4]:
                    like_pat = f"%{tok}%"
                    rows = conn.execute(
                        "SELECT key, value FROM preferences WHERE key LIKE ? OR value LIKE ? LIMIT ?",
                        (like_pat, like_pat, limit),
                    ).fetchall()
                    for r in rows:
                        pair = (str(r["key"]), str(r["value"]))
                        if pair not in results:
                            results.append(pair)
        return results[:limit]

    # =========================================================================
    # Conversation History Logging & Recall
    # =========================================================================
    def log_turn(self, user_text: str, assistant_text: str, intent_name: str = "unknown") -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO conversation_history (user_text, assistant_text, intent_name, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_text.strip(), assistant_text.strip(), intent_name, time.time()),
                )
                conn.commit()

    def recent_turns(self, limit: int = 5) -> List[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT user_text, assistant_text, intent_name, created_at
                    FROM conversation_history
                    ORDER BY id DESC LIMIT ?
                    """,
                    (max(1, limit),),
                ).fetchall()
                return [dict(r) for r in reversed(rows)]

# Backward compatibility alias
SQLiteStore = SQLiteMemoryStore
