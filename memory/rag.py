#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Unified RAG & Long-Term Memory Manager
==========================================================================
Combines `SQLiteMemoryStore` (structured preferences & recent turns) and
`ChromaMemoryStore` (semantic vector recall) into a single, sub-millisecond
context builder for `Brain.generate()` and direct voice memory commands
("remember that my project deadline is Friday", "what did I tell you about my deadline?").
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from memory.chroma_store import ChromaMemoryStore
from memory.sqlite_store import SQLiteMemoryStore


class RAGMemoryManager:
    """
    Coordinates structured SQLite storage and ChromaDB vector retrieval.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, base_dir: Optional[Path] = None) -> None:
        self.config = config or {}
        self.base_dir = base_dir or Path.cwd()
        self.logger = logging.getLogger("ev.rag")

        self.enabled: bool = bool(config.get("memory", {}).get("enabled", True))
        self.sqlite = SQLiteMemoryStore(config=config, base_dir=base_dir)
        self.chroma = ChromaMemoryStore(config=config, base_dir=base_dir)

    def remember_fact(self, key: str, value: str, category: str = "user_note") -> str:
        """Store a fact in both SQLite (`preferences`) and ChromaDB vector memory."""
        clean_k = (key or "note").strip()
        clean_v = (value or "").strip()
        if not clean_v:
            return "Please tell me what you'd like me to remember."

        self.sqlite.set_preference(clean_k, clean_v, category=category)
        fact_sentence = f"{clean_k}: {clean_v}"
        self.chroma.add_memory(fact_sentence, metadata={"key": clean_k, "category": category})
        return f"I'll remember that {clean_k} is {clean_v}."

    def recall(self, query: str) -> Optional[str]:
        """Look up a specific fact from SQLite or semantic vector memory."""
        sql_matches = self.sqlite.search_facts(query, limit=3)
        if sql_matches:
            formatted = "; ".join(f"{k} is {v}" for k, v in sql_matches)
            return f"From your saved memory: {formatted}."

        vec_matches = self.chroma.query_similar(query, top_k=2)
        if vec_matches:
            return f"From your saved memory: {' | '.join(vec_matches)}."
        return None

    def build_context_for_prompt(self, user_prompt: str) -> str:
        """
        Assemble a compact context string combining matching SQLite preferences
        and top-k ChromaDB semantic memories for `Brain.generate(extra_context=...)`.
        """
        if not self.enabled:
            return ""

        lines: List[str] = []
        for k, v in self.sqlite.search_facts(user_prompt, limit=3):
            lines.append(f"- Preference ({k}): {v}")

        for snippet in self.chroma.query_similar(user_prompt, top_k=2):
            entry = f"- Memory: {snippet}"
            if entry not in lines:
                lines.append(entry)

        return "\n".join(lines)

    def record_interaction(
        self,
        user_text: str,
        assistant_text: str,
        intent_name: str = "unknown",
    ) -> None:
        """Persist a completed interaction in SQLite history."""
        if not self.enabled:
            return
        self.sqlite.log_turn(user_text, assistant_text, intent_name=intent_name)

# Backward compatibility alias
MemoryRAG = RAGMemoryManager
