"""
E.V. (Enhanced Virtual Assistant) — Long-Term Memory Subsystem
==============================================================
Persistent SQLite structured storage, ChromaDB vector RAG embeddings,
and context assembly for cross-session assistant memory.
"""

<<<<<<< HEAD
from memory.chroma_store import ChromaMemoryStore
from memory.rag import RAGMemoryManager
from memory.sqlite_store import SQLiteMemoryStore

# Backwards-compatible aliases
ChromaStore = ChromaMemoryStore
MemoryRAG = RAGMemoryManager
SQLiteStore = SQLiteMemoryStore
=======
from memory.chroma_store import ChromaMemoryStore, ChromaStore
from memory.rag import MemoryRAG, RAGMemoryManager
from memory.sqlite_store import SQLiteMemoryStore, SQLiteStore
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

__all__ = [
    "ChromaMemoryStore",
    "ChromaStore",
    "MemoryRAG",
    "RAGMemoryManager",
    "SQLiteMemoryStore",
    "SQLiteStore",
]
