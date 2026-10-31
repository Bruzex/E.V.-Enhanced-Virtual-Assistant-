"""
E.V. (Enhanced Virtual Assistant) — Holographic HUD Overlays
============================================================
Modular exports of HUD components:
  - GlassPanel: Frosted container with cyan L-shaped corner brackets
  - CountdownPanelWidget: Timed command dial with countdown animation
  - WorkspacePanelWidget: Multi-application launch queue with progress
  - MetricsOverlay: CPU, process RAM, Sparklines, and Battery telemetry
  - CallModalCard: Iron-Man-style full-screen call management card
  - SettingsPopoverWidget: Fast toggle drawer for assistant parameters
"""

from gui.main_window import (
    CallModalCard,
    CallState,
    CountdownDialWidget,
    CountdownPanelWidget,
    CountdownState,
    GlassPanel,
    MetricsOverlay,
    SettingsPopoverWidget,
    SparklineWidget,
    WorkspacePanelWidget,
    WorkspaceRowWidget,
    WorkspaceState,
)

__all__ = [
    "GlassPanel",
    "SparklineWidget",
    "MetricsOverlay",
    "WorkspaceRowWidget",
    "WorkspacePanelWidget",
    "CountdownDialWidget",
    "CountdownPanelWidget",
    "SettingsPopoverWidget",
    "CallModalCard",
    "WorkspaceState",
    "CountdownState",
    "CallState",
]

