"""
E.V. (Enhanced Virtual Assistant) — Automation Subsystem
========================================================
Desktop, system, workspace orchestration, media controls, and zero-API web search.
"""

from automation.media import MediaController
from automation.system import SystemAutomation, SystemController
from automation.web_search import WebSearch, WebSearchTool
from automation.workspace import WorkspaceOrchestrator

__all__ = [
    "MediaController",
    "SystemAutomation",
    "SystemController",
    "WebSearch",
    "WebSearchTool",
    "WorkspaceOrchestrator",
]
