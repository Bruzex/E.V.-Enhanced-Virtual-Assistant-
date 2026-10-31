#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — ChromaDB Semantic Vector Memory Store
=========================================================================
Local persistent vector store (`data/chroma`) for semantic long-term memory.

RAM-Constrained Design (MSI Bravo 15 <= 4.0 GB Ceiling):
  - Uses a custom, deterministic 256-dimensional feature-hashing + character
    n-gram embedding function by default so ChromaDB NEVER downloads or loads
    a 400 MB PyTorch/SentenceTransformers model onto the GPU or into RAM.
  - Falls back cleanly to in-memory/SQLite lexical similarity if `chromadb`
    is not installed in the current Python environment.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import chromadb  # type: ignore
    CHROMADB_AVAILABLE = True
except ImportError:
    chromadb = None  # type: ignore
    CHROMADB_AVAILABLE = False


def compute_lightweight_embedding(text: str, dim: int = 128) -> List[float]:
    """
    Zero-RAM, CPU-only deterministic L2-normalized embedding using word + character
    3-gram signed feature hashing. Requires 0 MB of neural weight files.
    """
    vec = [0.0] * dim
    cleaned = re.sub(r"[^a-z0-9\\s]", " ", (text or "").lower())
    words = [w for w in cleaned.split() if w]
    if not words:
        return vec

    features: List[str] = list(words)
    for w in words:
        padded = f"<{w}>"
        for i in range(max(0, len(padded) - 2)):
            features.append(padded[i : i + 3])

    for feat in features:
        digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[idx] += sign

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 1e-9:
        vec = [round(v / norm, 6) for v in vec]
    return vec


def cosine_similarity(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class ChromaMemoryStore:
    """
    Persistent vector memory store backed by ChromaDB (with zero-dependency
    local vector fallback when `chromadb` is not installed).
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None, base_dir: Optional[Path] = None) -> None:
        self.config = config or {}
        self.base_dir = base_dir or Path.cwd()
        self.logger = logging.getLogger("ev.chroma")

        mem_cfg = config.get("memory", {})
        rel_dir = str(mem_cfg.get("chroma_dir", "data/chroma"))
        self.chroma_dir: Path = (
            (base_dir / rel_dir).resolve() if not Path(rel_dir).is_absolute() else Path(rel_dir)
        )
        self.chroma_dir.mkdir(parents=True, exist_ok=True)
        self.collection_name: str = str(mem_cfg.get("collection_name", "ev_long_term_memory"))
        self.top_k: int = int(mem_cfg.get("top_k", 3))

        self._lock = threading.RLock()
        self._client: Optional[Any] = None
        self._collection: Optional[Any] = None
        self._fallback_docs: List[Dict[str, Any]] = []

        self._init_client()

    def _init_client(self) -> None:
        if not CHROMADB_AVAILABLE or chromadb is None:
            self.logger.info("chromadb package not installed; using built-in lightweight vector index.")
            return

        try:
            self._client = chromadb.PersistentClient(path=str(self.chroma_dir))
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            self.logger.info("ChromaDB persistent collection '%s' ready.", self.collection_name)
        except Exception as exc:
            self.logger.warning("ChromaDB initialization degraded to built-in vector store: %s", exc)
            self._client = None
            self._collection = None

    def add_memory(self, text: str, metadata: Optional[Dict[str, Any]] = None) -> str:
        """Embed and store a memory snippet in the vector index."""
        cleaned = (text or "").strip()
        if not cleaned:
            return ""

        doc_id = uuid.uuid4().hex[:12]
        meta = dict(metadata) if metadata else {"timestamp": time.time()}
        emb = compute_lightweight_embedding(cleaned)

        with self._lock:
            if self._collection is not None:
                try:
                    self._collection.add(
                        ids=[doc_id],
                        documents=[cleaned],
                        embeddings=[emb],
                        metadatas=[meta],
                    )
                    return doc_id
                except Exception as exc:
                    self.logger.debug("ChromaDB add fallback: %s", exc)

            self._fallback_docs.append(
                {"id": doc_id, "text": cleaned, "embedding": emb, "metadata": meta}
            )
            return doc_id

    def query_similar(self, query: str, top_k: Optional[int] = None) -> List[str]:
        """Retrieve the `top_k` most semantically relevant memory snippets."""
        cleaned = (query or "").strip()
        if not cleaned:
            return []

        k = max(1, top_k or self.top_k)
        q_emb = compute_lightweight_embedding(cleaned)

        with self._lock:
            if self._collection is not None:
                try:
                    count = self._collection.count()
                    if count == 0:
                        return []
                    res = self._collection.query(
                        query_embeddings=[q_emb],
                        n_results=min(k, count),
                    )
                    docs = res.get("documents", [[]])[0]
                    return [str(d) for d in docs if d]
                except Exception as exc:
                    self.logger.debug("ChromaDB query fallback: %s", exc)

            if not self._fallback_docs:
                return []

            scored = [
                (cosine_similarity(q_emb, item["embedding"]), str(item["text"]))
                for item in self._fallback_docs
            ]
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return [text for score, text in scored[:k] if score > 0.05]

# Backward compatibility alias
ChromaStore = ChromaMemoryStore
