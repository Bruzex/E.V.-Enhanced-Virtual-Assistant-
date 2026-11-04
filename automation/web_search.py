#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Silent Background Web Search (`automation/web_search.py`)
=============================================================================================
Performs zero-API-key background web searches (never opens a browser window)
using `duckduckgo-search` with a built-in DuckDuckGo Instant Answer JSON fallback,
returning a clean 1–2 sentence spoken summary for factual questions.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    from duckduckgo_search import DDGS  # type: ignore

    DDGS_AVAILABLE = True
except ImportError:
    DDGS = None  # type: ignore
    DDGS_AVAILABLE = False


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str

    def get(self, key: str, default: Any = None) -> Any:
        if key == "title":
            return self.title
        if key in ("url", "href"):
            return self.url
        if key in ("snippet", "body"):
            return self.snippet
        return default

    def __getitem__(self, key: str) -> Any:
        res = self.get(key, None)
        if res is None:
            raise KeyError(key)
        return res


class WebSearchTool:
    """
    Lightweight, zero-API-key background DuckDuckGo search tool with automatic
    1–2 sentence TTS summarization for factual queries.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self.logger = logging.getLogger("ev.web_search")

        ws_cfg = self.config.get("automation", {}).get("web_search", {})
        self.enabled: bool = bool(ws_cfg.get("enabled", True))
        self.max_results: int = int(ws_cfg.get("max_results", 3))
        self.region: str = str(ws_cfg.get("region", "wt-wt"))
        self.safesearch: str = str(ws_cfg.get("safesearch", "moderate"))
        self.timeout_s: float = float(ws_cfg.get("timeout_seconds", 4.0))

    def search(self, query: str, max_results: Optional[int] = None) -> List[SearchHit]:
        """Run a silent DuckDuckGo text search and return structured hits."""
        cleaned = (query or "").strip()
        if not cleaned or not self.enabled:
            return []

        limit = max_results or self.max_results
        hits: List[SearchHit] = []

        # 1. Primary: duckduckgo-search library
        if DDGS_AVAILABLE and DDGS is not None:
            try:
                with DDGS() as ddgs:
                    raw_results = list(
                        ddgs.text(
                            cleaned,
                            region=self.region,
                            safesearch=self.safesearch,
                            max_results=limit,
                        )
                    )
                for item in raw_results:
                    hits.append(
                        SearchHit(
                            title=str(item.get("title", "")).strip(),
                            url=str(item.get("href", item.get("url", ""))).strip(),
                            snippet=str(item.get("body", item.get("snippet", ""))).strip(),
                        )
                    )
                if hits:
                    return hits
            except Exception as exc:
                self.logger.debug("DDGS text search fallback triggered for '%s': %s", cleaned, exc)

        # 2. Fallback: DuckDuckGo Instant Answer JSON API (stdlib urllib, zero browser window)
        try:
            url = (
                "https://api.duckduckgo.com/?q="
                + urllib.parse.quote_plus(cleaned)
                + "&format=json&no_html=1&skip_disambig=1"
            )
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "EV-Assistant/3.4 (Windows; Offline-First)"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore") or "{}")
            abstract = str(data.get("AbstractText") or data.get("Answer") or "").strip()
            heading = str(data.get("Heading") or cleaned).strip()
            abs_url = str(data.get("AbstractURL") or "").strip()
            if abstract:
                hits.append(SearchHit(title=heading, url=abs_url, snippet=abstract))
            for topic in data.get("RelatedTopics", [])[:limit]:
                if isinstance(topic, dict) and topic.get("Text"):
                    hits.append(
                        SearchHit(
                            title=heading,
                            url=str(topic.get("FirstURL", "")),
                            snippet=str(topic.get("Text", "")).strip(),
                        )
                    )
        except Exception as exc:
            self.logger.debug("DuckDuckGo Instant Answer API unreachable: %s", exc)

        return hits[:limit]

    @staticmethod
    def _clean_snippet_for_tts(snippet: str, max_sentences: int = 2) -> str:
        """Clean raw web snippet into 1–2 concise sentences suitable for TTS."""
        if not snippet:
            return ""
        text = re.sub(r"https?://\S+", "", snippet)
        text = re.sub(r"\[[^\]]*\]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        # Split into sentences
        sentences = [
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+", text)
            if len(s.strip()) >= 12
        ]
        if not sentences:
            return text[:220].rstrip(" ,;:-") + ("." if not text.endswith(".") else "")
        picked = " ".join(sentences[:max_sentences]).strip()
        if len(picked) > 260:
            picked = picked[:257].rsplit(" ", 1)[0] + "..."
        return picked

    def search_summary(self, query: str) -> str:
        """
        Execute a silent background search and return a concise 1–2 sentence
        spoken summary for Piper TTS.
        """
        hits = self.search(query, max_results=3)
        if not hits:
            return f"I couldn't reach the web right now to look up {query}, sir."

        for h in hits:
            cleaned = self._clean_snippet_for_tts(h.snippet, max_sentences=2)
            if cleaned and len(cleaned) >= 15:
                return cleaned

        top = hits[0]
        return f"{top.title}."

    def answer_factual_question(self, question: str) -> Optional[str]:
        """
        Attempt a silent background search for a factual question. Returns a
        1–2 sentence spoken answer if hits are found, or `None` if offline so
        the caller can fall back to the local GGUF model.
        """
        if not self.enabled:
            return None
        cleaned_q = re.sub(r"^(?:please\s+)?(?:tell\s+me|search\s+for|look\s+up)\s+", "", (question or "").strip(), flags=re.I)
        hits = self.search(cleaned_q, max_results=2)
        if not hits:
            return None
        for h in hits:
            cleaned = self._clean_snippet_for_tts(h.snippet, max_sentences=2)
            if cleaned and len(cleaned) >= 15:
                return cleaned
        return None


WebSearch = WebSearchTool
