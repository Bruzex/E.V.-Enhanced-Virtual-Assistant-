"""
Unit tests for memory subsystem (SQLiteMemoryStore, ChromaMemoryStore, RAGMemoryManager).
"""

from pathlib import Path
from memory.sqlite_store import SQLiteMemoryStore
from memory.chroma_store import ChromaMemoryStore, compute_lightweight_embedding
from memory.rag import RAGMemoryManager


def test_sqlite_store_crud(tmp_path: Path):
    config = {"memory": {"sqlite_path": str(tmp_path / "memory.db")}}
    store = SQLiteMemoryStore(config=config, base_dir=tmp_path)

    store.set_preference("editor", "VS Code", category="tools")
    assert store.get_preference("editor") == "VS Code"

    facts = store.search_facts("editor")
    assert len(facts) >= 1
    assert facts[0][0] == "editor"


def test_lightweight_embedding():
    v1 = compute_lightweight_embedding("coding in Python")
    v2 = compute_lightweight_embedding("coding in Python")
    v3 = compute_lightweight_embedding("cooking a meal")

    assert len(v1) == 128
    # Deterministic output for same text
    assert v1 == v2
    # Different text produces different vector
    assert v1 != v3


def test_rag_remember_and_recall(tmp_path: Path):
    config = {
        "memory": {
            "sqlite_path": str(tmp_path / "memory.db"),
            "chroma_dir": str(tmp_path / "chroma"),
        }
    }
    rag = RAGMemoryManager(config=config, base_dir=tmp_path)

    msg = rag.remember_fact("favorite_drink", "black coffee")
    assert "black coffee" in msg

    recalled = rag.recall("favorite_drink")
    assert recalled is not None
    assert "black coffee" in recalled

