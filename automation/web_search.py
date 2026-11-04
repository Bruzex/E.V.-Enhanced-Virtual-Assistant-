#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Live Web Search (duckduckgo-search)
=======================================================================
Zero-API-key web search helper using `duckduckgo-search` (`DDGS`).
Returns concise, TTS-ready summaries and structured result dicts while
gracefully handling offline/no-internet environments.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

try:
    from duckduckgo_search import DDGS  # type: ignore
    DDGS_AVAILABLE = True
except ImportError:
    DDGS = None  # type: ignore
    DDGS_AVAILABLE = False


class WebSearchTool:
    """Lightweight wrapper around DuckDuckGo text search."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config = config or {}
        self.logger = logging.getLogger("ev.web_search")
        self.max_results: int = int(
            self.config.get("automation", {}).get("web_search_max_results", 4)
        )

    def search(self, query: str, max_results: int | None = None) -> List[Dict[str, str]]:
        """Return a list of `{title, href, body}` search results."""
        cleaned = (query or "").strip()
        if not cleaned or not DDGS_AVAILABLE or DDGS is None:
            return []

        limit = max_results or self.max_results
        try:
            with DDGS() as ddgs:
                raw = list(ddgs.text(cleaned, max_results=limit))
            return [
                {
                    "title": str(r.get("title", "")),
                    "href": str(r.get("href", "")),
                    "body": str(r.get("body", "")),
                }
                for r in raw
            ]
        except Exception as exc:
            self.logger.warning("DuckDuckGo search unavailable (offline or blocked): %s", exc)
            return []

    def search_summary(self, query: str) -> str:
        """Return a 1–2 sentence spoken summary of the top search result."""
        results = self.search(query, max_results=2)
        if not results:
            return (
                f"I'm currently running in strict offline mode and couldn't reach "
                f"DuckDuckGo for '{query}'."
            )
        top = results[0]
        body = top.get("body", "").strip()
        if len(body) > 200:
            body = body[:197].rsplit(" ", 1)[0] + "..."
        return f"According to {top.get('title', 'web results')}: {body}"

# Alias for backward compatibility
WebSearch = WebSearchTool
