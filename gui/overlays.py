#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Holographic HUD Overlay Panels
==================================================================
Extracted from `gui/main_window.py` in Phase 4 for clean modular architecture.

Contains all MagicPatterns HUD overlay panels and state dataclasses:
  - `GlassPanel`: Translucent HUD card with 4 cyan L-shaped corner brackets
  - `SparklineWidget` & `MetricsOverlay` (`MetricsPanel.tsx`): Top-right CPU/RAM
    sparklines, Battery progress bar, and `GPU: 0 MB (CPU-only inference)` cell
  - `WorkspaceRowWidget` & `WorkspacePanelWidget` (`WorkspacePanel.tsx`):
    Left-side live workspace progress list, percentage bar, and `Undo workspace`
  - `CountdownDialWidget` & `CountdownPanelWidget` (`CountdownOverlay.tsx`):
    Right-side circular countdown dial, tabular seconds, `Cancel`, and `Run now`
  - `SettingsPopoverWidget` (`SettingsPopover.tsx`): 4 feature toggles + live
    Status Orb state preview buttons
  - `CallModalCard` (`CallOverlay.tsx`): Full-screen Iron-Man incoming/outgoing
    call modal with rotating rings, ringing ripples, 18-bar equalizer, and
    `Accept` / `Ignore` / `End call` controls
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
    from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
    from PyQt6.QtWidgets import (
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QPushButton,
        QScrollArea,
        QVBoxLayout,
        QWidget,
    )
    QT_VERSION = 6
except ImportError:
    try:
        from PySide6.QtCore import QPointF, QRectF, Qt, Signal as pyqtSignal  # type: ignore
        from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF  # type: ignore
        from PySide6.QtWidgets import (  # type: ignore
            QFrame,
            QGridLayout,
            QHBoxLayout,
            QLabel,
            QPushButton,
            QScrollArea,
            QVBoxLayout,
            QWidget,
        )
        QT_VERSION = 6
    except ImportError:
        from PyQt5.QtCore import QPointF, QRectF, Qt, pyqtSignal  # type: ignore
        from PyQt5.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF  # type: ignore
        from PyQt5.QtWidgets import (  # type: ignore
            QFrame,
            QGridLayout,
            QHBoxLayout,
            QLabel,
            QPushButton,
            QScrollArea,
            QVBoxLayout,
            QWidget,
        )
        QT_VERSION = 5

from gui.status_orb import (
    COLOR_CYAN,
    COLOR_DANGER,
    COLOR_MUTED,
    COLOR_PANEL,
    COLOR_PANEL_STRONG,
    COLOR_PURPLE,
    align_center,
    antialias_hint,
    custom_dash_line,
    format_duration,
    no_brush,
    no_pen,
    pen_round_cap,
    pointing_hand_cursor,
)


DEFAULT_WORKSPACE_ITEMS: List[Dict[str, str]] = [
    {"id": "vscode", "name": "VS Code", "detail": "~/Projects/ev", "kind": "app"},
    {"id": "terminal", "name": "Terminal", "detail": "zsh · ev/ (venv active)", "kind": "terminal"},
    {"id": "projects", "name": "Projects folder", "detail": "~/Projects", "kind": "folder"},
    {"id": "github", "name": "GitHub", "detail": "github.com/ev-assistant", "kind": "url"},
    {"id": "localhost", "name": "Dev server", "detail": "localhost:5173", "kind": "url"},
    {"id": "docker", "name": "Docker Desktop", "detail": "3 containers", "kind": "app"},
    {"id": "notion", "name": "Notion", "detail": "Sprint board · Phase 3", "kind": "app"},
    {"id": "models", "name": "Models folder", "detail": "~/ev/models", "kind": "folder"},
    {"id": "spotify", "name": "Spotify", "detail": "Focus Flow playlist", "kind": "app"},
    {"id": "logs", "name": "RAM monitor", "detail": "tail -f logs/rss.log", "kind": "terminal"},
]


# =============================================================================
# HUD Overlay State Dataclasses
# =============================================================================
@dataclass
class Particle:
    x: float
    y: float
    vx: float
    vy: float
    r: float
    a: float
    tw: float
    purple: bool


@dataclass
class SparkBurst:
    x: float
    y: float
    born_ms: float


@dataclass
class WorkspaceState:
    name: str = "Coding workspace"
    status: str = "running"  # 'running' | 'done' | 'undoing' | 'undone'
    completed: int = 0
    items: List[Dict[str, str]] = field(default_factory=lambda: list(DEFAULT_WORKSPACE_ITEMS))


@dataclass
class CountdownState:
    total: int
    remaining: int
    label: str
    detail: str
    action: str = "notify"  # 'call' | 'notify'
    done_message: str = ""


@dataclass
class CallState:
    direction: str = "incoming"  # 'incoming' | 'outgoing'
    status: str = "ringing"      # 'ringing' | 'connected'
    seconds: int = 0
    name: str = "Mom"
    number: str = "+1 (555) 014-2290"
    meta: str = "Mobile · via phone bridge"


# =============================================================================
# Reusable GlassPanel with Holographic Corner Brackets
# =============================================================================
class GlassPanel(QFrame):
    """Dark translucent HUD container with 4 cyan L-shaped corner brackets."""

    def __init__(self, strong: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.strong = strong
        self.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground
            if QT_VERSION == 6
            else Qt.WA_TranslucentBackground
        )

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(antialias_hint())

        rect = QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        radius = 16.0

        painter.setPen(no_pen())
        painter.setBrush(QBrush(COLOR_PANEL_STRONG if self.strong else COLOR_PANEL))
        painter.drawRoundedRect(rect, radius, radius)

        border_color = QColor(COLOR_CYAN)
        border_color.setAlpha(150 if self.strong else 55)
        painter.setPen(QPen(border_color, 1.1))
        painter.setBrush(no_brush())
        painter.drawRoundedRect(rect, radius, radius)

        bracket_color = QColor(COLOR_CYAN)
        bracket_color.setAlpha(185)
        painter.setPen(QPen(bracket_color, 1.3))
        m = 8.0
        arm = 10.0
        w = float(self.width())
        h = float(self.height())

        # Top-left
        painter.drawLine(QPointF(m, m), QPointF(m + arm, m))
        painter.drawLine(QPointF(m, m), QPointF(m, m + arm))
        # Top-right
        painter.drawLine(QPointF(w - m - arm, m), QPointF(w - m, m))
        painter.drawLine(QPointF(w - m, m), QPointF(w - m, m + arm))
        # Bottom-left
        painter.drawLine(QPointF(m, h - m), QPointF(m + arm, h - m))
        painter.drawLine(QPointF(m, h - m - arm), QPointF(m, h - m))
        # Bottom-right
        painter.drawLine(QPointF(w - m - arm, h - m), QPointF(w - m, h - m))
        painter.drawLine(QPointF(w - m, h - m - arm), QPointF(w - m, h - m))


# =============================================================================
# Sparkline Widget & Top-Right System Metrics Floating Overlay
# =============================================================================
class SparklineWidget(QWidget):
    """Draws the SVG-style polyline + 10% opacity area fill from Sparkline.tsx."""

    def __init__(self, max_val: float = 100.0, color: QColor = COLOR_CYAN, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.values: List[float] = [14.0] * 28
        self.max_val = max_val
        self.color = QColor(color)
        self.setFixedSize(82, 28)

    def set_data(self, values: List[float], color: Optional[QColor] = None) -> None:
        self.values = list(values[-28:]) if values else [0.0] * 28
        if color is not None:
            self.color = QColor(color)
        self.update()

    def paintEvent(self, event: Any) -> None:
        if len(self.values) < 2:
            return
        painter = QPainter(self)
        painter.setRenderHint(antialias_hint())

        w = float(self.width())
        h = float(self.height())
        step = w / float(len(self.values) - 1)

        pts: List[QPointF] = []
        for i, v in enumerate(self.values):
            clamped = max(0.0, min(self.max_val, float(v)))
            x = i * step
            y = h - (clamped / self.max_val) * (h - 4.0) - 2.0
            pts.append(QPointF(x, y))

        area = QPolygonF([QPointF(0.0, h)] + pts + [QPointF(w, h)])
        fill_col = QColor(self.color)
        fill_col.setAlpha(28)
        painter.setPen(no_pen())
        painter.setBrush(QBrush(fill_col))
        painter.drawPolygon(area)

        pen = QPen(self.color, 1.35)
        painter.setPen(pen)
        painter.setBrush(no_brush())
        painter.drawPolyline(QPolygonF(pts))


class MetricsOverlay(GlassPanel):
    """Floating system telemetry strip with CPU/RAM sparklines, Battery bar, and 0 MB GPU badge."""

    closed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(strong=False, parent=parent)
        self.setFixedSize(530, 88)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 34, 12)
        layout.setSpacing(16)

        # Cell 1: CPU
        cpu_col = QVBoxLayout()
        cpu_hdr = QHBoxLayout()
        lbl_cpu = QLabel("CPU")
        lbl_cpu.setStyleSheet("color: #8AA4BF; font-size: 11px;")
        self.val_cpu = QLabel("14%")
        self.val_cpu.setStyleSheet("color: #00D4FF; font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 600;")
        cpu_hdr.addWidget(lbl_cpu)
        cpu_hdr.addStretch()
        cpu_hdr.addWidget(self.val_cpu)
        self.spark_cpu = SparklineWidget(max_val=100.0, color=COLOR_CYAN)
        cpu_col.addLayout(cpu_hdr)
        cpu_col.addWidget(self.spark_cpu)
        layout.addLayout(cpu_col)

        layout.addWidget(self._divider())

        # Cell 2: RAM
        ram_col = QVBoxLayout()
        ram_hdr = QHBoxLayout()
        lbl_ram = QLabel("RAM")
        lbl_ram.setStyleSheet("color: #8AA4BF; font-size: 11px;")
        self.val_ram = QLabel("2.4 GB")
        self.val_ram.setStyleSheet("color: #00D4FF; font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 600;")
        ram_hdr.addWidget(lbl_ram)
        ram_hdr.addStretch()
        ram_hdr.addWidget(self.val_ram)
        self.spark_ram = SparklineWidget(max_val=4.0, color=COLOR_PURPLE)
        sub_ram = QLabel("of 4.0 GB budget")
        sub_ram.setStyleSheet("color: #4A6079; font-size: 10px;")
        ram_col.addLayout(ram_hdr)
        ram_col.addWidget(self.spark_ram)
        ram_col.addWidget(sub_ram)
        layout.addLayout(ram_col)

        layout.addWidget(self._divider())

        # Cell 3: Battery
        bat_col = QVBoxLayout()
        bat_hdr = QHBoxLayout()
        lbl_bat = QLabel("Battery")
        lbl_bat.setStyleSheet("color: #8AA4BF; font-size: 11px;")
        self.val_bat = QLabel("86%")
        self.val_bat.setStyleSheet("color: #00D4FF; font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 600;")
        bat_hdr.addWidget(lbl_bat)
        bat_hdr.addStretch()
        bat_hdr.addWidget(self.val_bat)

        self.bat_bar_bg = QFrame()
        self.bat_bar_bg.setFixedSize(80, 6)
        self.bat_bar_bg.setStyleSheet("background-color: #1B2A3F; border-radius: 3px;")
        self.bat_bar_fill = QFrame(self.bat_bar_bg)
        self.bat_bar_fill.setGeometry(0, 0, 68, 6)
        self.bat_bar_fill.setStyleSheet("background-color: #3DF5A7; border-radius: 3px;")

        sub_bat = QLabel("Plugged in")
        sub_bat.setStyleSheet("color: #4A6079; font-size: 10px;")
        bat_col.addLayout(bat_hdr)
        bat_col.addWidget(self.bat_bar_bg)
        bat_col.addWidget(sub_bat)
        layout.addLayout(bat_col)

        layout.addWidget(self._divider())

        # Cell 4: GPU (Strict 0 MB VRAM)
        gpu_col = QVBoxLayout()
        gpu_hdr = QHBoxLayout()
        lbl_gpu = QLabel("GPU")
        lbl_gpu.setStyleSheet("color: #8AA4BF; font-size: 11px;")
        val_gpu = QLabel("0 MB")
        val_gpu.setStyleSheet("color: #00D4FF; font-family: 'JetBrains Mono', monospace; font-size: 13px; font-weight: 600;")
        gpu_hdr.addWidget(lbl_gpu)
        gpu_hdr.addStretch()
        gpu_hdr.addWidget(val_gpu)
        sub_gpu = QLabel("CPU-only inference")
        sub_gpu.setStyleSheet("color: #4A6079; font-size: 10px;")
        gpu_col.addLayout(gpu_hdr)
        gpu_col.addStretch()
        gpu_col.addWidget(sub_gpu)
        layout.addLayout(gpu_col)

        btn_close = QPushButton("✕", self)
        btn_close.setGeometry(498, 10, 22, 22)
        btn_close.setStyleSheet(
            "QPushButton { background: transparent; color: #4A6079; border: none; font-size: 12px; }"
            "QPushButton:hover { color: #00D4FF; }"
        )
        btn_close.clicked.connect(self.closed.emit)

    @staticmethod
    def _divider() -> QFrame:
        line = QFrame()
        line.setFixedWidth(1)
        line.setStyleSheet("background-color: rgba(0, 212, 255, 28);")
        return line

    def update_data(self, cpu: int, ram: float, battery: int, cpu_hist: List[float], ram_hist: List[float]) -> None:
        self.val_cpu.setText(f"{cpu}%")
        self.val_ram.setText(f"{ram:.1f} GB")
        self.val_bat.setText(f"{battery}%")
        self.spark_cpu.set_data(cpu_hist, COLOR_CYAN)
        ram_col = COLOR_DANGER if (ram / 4.0) > 0.85 else COLOR_PURPLE
        self.spark_ram.set_data(ram_hist, ram_col)
        fill_w = int(round(80.0 * max(0, min(100, battery)) / 100.0))
        self.bat_bar_fill.setGeometry(0, 0, fill_w, 6)


# =============================================================================
# Left Workspace Progress Panel (WorkspacePanel.tsx)
# =============================================================================
class WorkspaceRowWidget(QFrame):
    """Single row inside WorkspacePanel showing icon badge, name, detail, and status indicator."""

    KIND_SYMBOLS = {
        "app": "⊞",
        "folder": "📁",
        "url": "🌐",
        "terminal": ">_",
    }

    def __init__(self, item: Dict[str, str], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.item = item
        self.state = "pending"
        self.setFixedHeight(44)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(10)

        self.lbl_icon = QLabel(self.KIND_SYMBOLS.get(item.get("kind", "app"), "⊞"))
        self.lbl_icon.setFixedWidth(20)
        self.lbl_icon.setAlignment(align_center())

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        self.lbl_name = QLabel(item.get("name", ""))
        self.lbl_detail = QLabel(item.get("detail", ""))
        self.lbl_detail.setStyleSheet("color: #4A6079; font-family: 'JetBrains Mono', monospace; font-size: 11px;")
        text_col.addWidget(self.lbl_name)
        text_col.addWidget(self.lbl_detail)

        self.lbl_status = QLabel("•")
        self.lbl_status.setFixedSize(20, 20)
        self.lbl_status.setAlignment(align_center())

        row.addWidget(self.lbl_icon)
        row.addLayout(text_col, 1)
        row.addWidget(self.lbl_status)
        self.set_row_state("pending")

    def set_row_state(self, state: str) -> None:
        self.state = state
        if state == "done":
            self.setStyleSheet("background: transparent; border-radius: 8px;")
            self.lbl_icon.setStyleSheet("color: #00D4FF; font-size: 13px; font-weight: 600;")
            self.lbl_name.setStyleSheet("color: #E6F4FF; font-size: 13px; font-weight: 500;")
            self.lbl_status.setText("✓")
            self.lbl_status.setStyleSheet(
                "color: #00D4FF; background-color: rgba(0, 212, 255, 38); "
                "border: 1px solid #00D4FF; border-radius: 10px; font-size: 11px; font-weight: 700;"
            )
        elif state == "active":
            self.setStyleSheet("background-color: rgba(0, 212, 255, 18); border-radius: 8px;")
            self.lbl_icon.setStyleSheet("color: #00D4FF; font-size: 13px; font-weight: 600;")
            self.lbl_name.setStyleSheet("color: #E6F4FF; font-size: 13px; font-weight: 500;")
            self.lbl_status.setText("◌")
            self.lbl_status.setStyleSheet("color: #00D4FF; font-size: 14px; font-weight: 700;")
        else:
            self.setStyleSheet("background: transparent; border-radius: 8px;")
            self.lbl_icon.setStyleSheet("color: #4A6079; font-size: 13px;")
            self.lbl_name.setStyleSheet("color: #8AA4BF; font-size: 13px;")
            self.lbl_status.setText("•")
            self.lbl_status.setStyleSheet("color: #1B2A3F; font-size: 14px;")


class WorkspacePanelWidget(GlassPanel):
    """Left-side Workspace Orchestrator HUD Panel with progress bar and Undo button."""

    undo_requested = pyqtSignal()
    dismiss_requested = pyqtSignal()

    def __init__(self, items: List[Dict[str, str]], parent: Optional[QWidget] = None) -> None:
        super().__init__(strong=False, parent=parent)
        self.setFixedWidth(340)
        self.items = items
        self.rows: List[WorkspaceRowWidget] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        hdr = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self.lbl_title = QLabel("Coding workspace")
        self.lbl_title.setStyleSheet("color: #E6F4FF; font-size: 15px; font-weight: 600;")
        self.lbl_status = QLabel("Launching 0/10")
        self.lbl_status.setStyleSheet("color: #00D4FF; font-family: 'JetBrains Mono', monospace; font-size: 12px;")
        title_col.addWidget(self.lbl_title)
        title_col.addWidget(self.lbl_status)

        btn_close = QPushButton("✕")
        btn_close.setFixedSize(24, 24)
        btn_close.setStyleSheet(
            "QPushButton { background: transparent; color: #4A6079; border: none; font-size: 13px; }"
            "QPushButton:hover { color: #00D4FF; }"
        )
        btn_close.clicked.connect(self.dismiss_requested.emit)
        hdr.addLayout(title_col)
        hdr.addStretch()
        hdr.addWidget(btn_close)
        root.addLayout(hdr)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFixedHeight(285)
        self.scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            "QScrollBar:vertical { width: 4px; background: transparent; }"
            "QScrollBar::handle:vertical { background: rgba(0, 212, 255, 64); border-radius: 2px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }"
        )
        list_container = QWidget()
        list_container.setStyleSheet("background: transparent;")
        list_layout = QVBoxLayout(list_container)
        list_layout.setContentsMargins(0, 0, 4, 0)
        list_layout.setSpacing(2)

        for item in self.items:
            row_w = WorkspaceRowWidget(item)
            self.rows.append(row_w)
            list_layout.addWidget(row_w)
        list_layout.addStretch()
        self.scroll.setWidget(list_container)
        root.addWidget(self.scroll)

        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet("background-color: rgba(0, 212, 255, 25);")
        root.addWidget(div)

        prog_hdr = QHBoxLayout()
        lbl_overall = QLabel("Overall progress")
        lbl_overall.setStyleSheet("color: #8AA4BF; font-size: 12px;")
        self.lbl_pct = QLabel("0%")
        self.lbl_pct.setStyleSheet("color: #00D4FF; font-family: 'JetBrains Mono', monospace; font-size: 12px;")
        prog_hdr.addWidget(lbl_overall)
        prog_hdr.addStretch()
        prog_hdr.addWidget(self.lbl_pct)
        root.addLayout(prog_hdr)

        self.prog_track = QFrame()
        self.prog_track.setFixedHeight(6)
        self.prog_track.setStyleSheet("background-color: #1B2A3F; border-radius: 3px;")
        self.prog_fill = QFrame(self.prog_track)
        self.prog_fill.setGeometry(0, 0, 0, 6)
        self.prog_fill.setStyleSheet("background-color: #00D4FF; border-radius: 3px;")
        root.addWidget(self.prog_track)

        self.btn_undo = QPushButton("↶  Undo workspace")
        self.btn_undo.setFixedHeight(36)
        self.btn_undo.setCursor(pointing_hand_cursor())
        self.btn_undo.setStyleSheet(
            "QPushButton {"
            "  background-color: transparent;"
            "  color: #00D4FF;"
            "  border: 1px solid rgba(0, 212, 255, 80);"
            "  border-radius: 8px;"
            "  font-size: 13px;"
            "  font-weight: 500;"
            "}"
            "QPushButton:hover { background-color: rgba(0, 212, 255, 25); }"
            "QPushButton:disabled { color: #4A6079; border-color: #1B2A3F; }"
        )
        self.btn_undo.clicked.connect(self.undo_requested.emit)
        root.addWidget(self.btn_undo)

    def update_workspace(self, ws: WorkspaceState) -> None:
        total = max(1, len(self.rows))
        pct = int(round((ws.completed / total) * 100.0))
        self.lbl_title.setText(ws.name)

        if ws.status == "running":
            self.lbl_status.setText(f"Launching {ws.completed}/{total}")
        elif ws.status == "done":
            self.lbl_status.setText("All items launched")
        elif ws.status == "undoing":
            self.lbl_status.setText(f"Closing · {ws.completed} left")
        else:
            self.lbl_status.setText("Restored")

        self.lbl_pct.setText(f"{pct}%")
        track_w = max(280, self.prog_track.width())
        self.prog_fill.setGeometry(0, 0, int(round(track_w * (pct / 100.0))), 6)

        for idx, row_w in enumerate(self.rows):
            if idx < ws.completed:
                row_w.set_row_state("done")
            elif idx == ws.completed and ws.status == "running":
                row_w.set_row_state("active")
                self.scroll.ensureWidgetVisible(row_w)
            else:
                row_w.set_row_state("pending")

        can_undo = ws.status not in ("undoing", "undone") and ws.completed > 0
        self.btn_undo.setEnabled(can_undo)


# =============================================================================
# Right Timed Command / Countdown Panel (CountdownOverlay.tsx)
# =============================================================================
class CountdownDialWidget(QWidget):
    """Circular countdown dial with outer dashed ring, glowing arc, and large tabular number."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.total: int = 10
        self.remaining: int = 10
        self.setFixedSize(176, 176)

    def set_values(self, total: int, remaining: int) -> None:
        self.total = max(1, total)
        self.remaining = max(0, remaining)
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(antialias_hint())

        w = float(self.width())
        h = float(self.height())
        cx, cy = w / 2.0, h / 2.0
        r = 68.0
        urgent = self.remaining <= 5
        accent = COLOR_PURPLE if urgent else COLOR_CYAN

        outer_pen = QPen(QColor(0, 212, 255, 64), 1.0)
        outer_pen.setStyle(custom_dash_line())
        outer_pen.setDashPattern([1.5, 5.0])
        painter.setPen(outer_pen)
        painter.setBrush(no_brush())
        painter.drawEllipse(QPointF(cx, cy), r + 9.0, r + 9.0)

        painter.setPen(QPen(QColor(0, 212, 255, 32), 5.5))
        painter.drawEllipse(QPointF(cx, cy), r, r)

        frac = max(0.0, min(1.0, self.remaining / float(self.total)))
        arc_pen = QPen(accent, 5.5)
        arc_pen.setCapStyle(pen_round_cap())
        painter.setPen(arc_pen)
        painter.drawArc(
            QRectF(cx - r, cy - r, r * 2.0, r * 2.0),
            90 * 16,
            int(-frac * 360.0 * 16),
        )

        painter.setPen(accent)
        num_font = QFont("JetBrains Mono", 36)
        num_font.setBold(True)
        painter.setFont(num_font)
        painter.drawText(QRectF(0, 34, w, 68), align_center(), str(self.remaining))

        painter.setPen(COLOR_MUTED)
        sub_font = QFont("Inter", 10)
        painter.setFont(sub_font)
        painter.drawText(QRectF(0, 102, w, 24), align_center(), "seconds")


class CountdownPanelWidget(GlassPanel):
    """Right-side Timed Command / Countdown HUD Panel."""

    cancel_requested = pyqtSignal()
    run_now_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(strong=False, parent=parent)
        self.setFixedSize(300, 340)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 22)
        root.setSpacing(8)

        dial_row = QHBoxLayout()
        dial_row.addStretch()
        self.dial = CountdownDialWidget()
        dial_row.addWidget(self.dial)
        dial_row.addStretch()
        root.addLayout(dial_row)

        self.lbl_title = QLabel("Dim desk lights to 30%")
        self.lbl_title.setAlignment(align_center())
        self.lbl_title.setWordWrap(True)
        self.lbl_title.setStyleSheet("color: #E6F4FF; font-size: 15px; font-weight: 600; margin-top: 6px;")
        root.addWidget(self.lbl_title)

        self.lbl_detail = QLabel("Smart plug · Study")
        self.lbl_detail.setAlignment(align_center())
        self.lbl_detail.setStyleSheet("color: #8AA4BF; font-size: 12px;")
        root.addWidget(self.lbl_detail)

        root.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_cancel = QPushButton("✕  Cancel")
        self.btn_cancel.setFixedHeight(36)
        self.btn_cancel.setCursor(pointing_hand_cursor())
        self.btn_cancel.setStyleSheet(
            "QPushButton {"
            "  background: transparent;"
            "  color: #8AA4BF;"
            "  border: 1px solid #1B2A3F;"
            "  border-radius: 8px;"
            "  font-size: 13px;"
            "}"
            "QPushButton:hover { color: #FF4D6D; border-color: rgba(255, 77, 109, 150); }"
        )
        self.btn_cancel.clicked.connect(self.cancel_requested.emit)

        self.btn_run = QPushButton("▷  Run now")
        self.btn_run.setFixedHeight(36)
        self.btn_run.setCursor(pointing_hand_cursor())
        self.btn_run.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(0, 212, 255, 26);"
            "  color: #00D4FF;"
            "  border: 1px solid rgba(0, 212, 255, 102);"
            "  border-radius: 8px;"
            "  font-size: 13px;"
            "  font-weight: 500;"
            "}"
            "QPushButton:hover { background-color: rgba(0, 212, 255, 52); }"
        )
        self.btn_run.clicked.connect(self.run_now_requested.emit)

        btn_row.addWidget(self.btn_cancel)
        btn_row.addWidget(self.btn_run)
        root.addLayout(btn_row)

    def update_countdown(self, state: CountdownState) -> None:
        self.dial.set_values(state.total, state.remaining)
        self.lbl_title.setText(state.label)
        self.lbl_detail.setText(state.detail)


# =============================================================================
# Settings Popover (SettingsPopover.tsx)
# =============================================================================
class SettingsPopoverWidget(GlassPanel):
    """Popover above the Settings icon with 4 switches and live Orb state preview buttons."""

    setting_toggled = pyqtSignal(str)
    preview_selected = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(strong=False, parent=parent)
        self.setFixedSize(305, 345)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(8)

        hdr = QLabel("Settings")
        hdr.setStyleSheet("color: #E6F4FF; font-size: 14px; font-weight: 600;")
        root.addWidget(hdr)

        self.switch_buttons: Dict[str, QPushButton] = {}
        rows = [
            ("wakeWord", "Wake word", "“Hey E.V.”"),
            ("clapDetection", "Clap detection", "Double-clap toggles status"),
            ("continuousListening", "Continuous listening", "Stay awake between commands"),
            ("minimalUi", "Minimal UI", "Fewer effects on low-end CPUs"),
        ]

        for key, label, hint in rows:
            row_layout = QHBoxLayout()
            txt_col = QVBoxLayout()
            txt_col.setSpacing(1)
            lbl_main = QLabel(label)
            lbl_main.setStyleSheet("color: #E6F4FF; font-size: 13px;")
            lbl_sub = QLabel(hint)
            lbl_sub.setStyleSheet("color: #8AA4BF; font-size: 11px;")
            txt_col.addWidget(lbl_main)
            txt_col.addWidget(lbl_sub)

            btn_sw = QPushButton()
            btn_sw.setFixedSize(38, 20)
            btn_sw.setCursor(pointing_hand_cursor())
            btn_sw.clicked.connect(lambda _=False, k=key: self.setting_toggled.emit(k))
            self.switch_buttons[key] = btn_sw

            row_layout.addLayout(txt_col, 1)
            row_layout.addWidget(btn_sw)
            root.addLayout(row_layout)

        lbl_prev = QLabel("Orb preview")
        lbl_prev.setStyleSheet("color: #8AA4BF; font-size: 11px; margin-top: 4px;")
        root.addWidget(lbl_prev)

        grid = QGridLayout()
        grid.setSpacing(6)
        self.preview_buttons: Dict[Optional[str], QPushButton] = {}
        options: List[tuple[Optional[str], str]] = [
            (None, "Live"),
            ("idle", "Idle"),
            ("listening", "Listening"),
            ("thinking", "Thinking"),
            ("speaking", "Speaking"),
            ("acting", "Acting"),
        ]
        for idx, (state_id, text) in enumerate(options):
            b = QPushButton(text)
            b.setFixedHeight(28)
            b.setCursor(pointing_hand_cursor())
            b.clicked.connect(lambda _=False, sid=state_id: self.preview_selected.emit(sid))
            self.preview_buttons[state_id] = b
            grid.addWidget(b, idx // 3, idx % 3)

        root.addLayout(grid)

    def sync_state(self, settings: Dict[str, bool], preview: Optional[str]) -> None:
        for key, btn in self.switch_buttons.items():
            on = bool(settings.get(key, False))
            if on:
                btn.setText("●")
                btn.setStyleSheet(
                    "QPushButton {"
                    "  background-color: rgba(0, 212, 255, 76);"
                    "  border: 1px solid #00D4FF;"
                    "  border-radius: 10px;"
                    "  color: #00D4FF;"
                    "  text-align: right;"
                    "  padding-right: 3px;"
                    "  font-size: 12px;"
                    "}"
                )
            else:
                btn.setText("●")
                btn.setStyleSheet(
                    "QPushButton {"
                    "  background-color: #060912;"
                    "  border: 1px solid #1B2A3F;"
                    "  border-radius: 10px;"
                    "  color: #4A6079;"
                    "  text-align: left;"
                    "  padding-left: 3px;"
                    "  font-size: 12px;"
                    "}"
                )

        for sid, btn in self.preview_buttons.items():
            active = (sid == preview)
            if active:
                btn.setStyleSheet(
                    "QPushButton {"
                    "  background-color: rgba(0, 212, 255, 38);"
                    "  border: 1px solid #00D4FF;"
                    "  border-radius: 7px;"
                    "  color: #00D4FF;"
                    "  font-size: 12px;"
                    "}"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton {"
                    "  background-color: transparent;"
                    "  border: 1px solid #1B2A3F;"
                    "  border-radius: 7px;"
                    "  color: #8AA4BF;"
                    "  font-size: 12px;"
                    "}"
                    "QPushButton:hover { border-color: rgba(0, 212, 255, 102); color: #E6F4FF; }"
                )


# =============================================================================
# Full-Screen Iron-Man Style Call Overlay (CallOverlay.tsx)
# =============================================================================
class CallModalCard(GlassPanel):
    """Center holographic card inside CallOverlay with rotating rings, avatar, and voice bars."""

    accept_clicked = pyqtSignal()
    end_clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(strong=True, parent=parent)
        self.setFixedSize(440, 460)
        self.call_state = CallState()
        self._t0 = time.monotonic()

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 30)
        root.setSpacing(6)

        top_row = QHBoxLayout()
        self.lbl_heading = QLabel("INCOMING CALL")
        self.lbl_heading.setStyleSheet(
            "color: #00D4FF; font-family: 'JetBrains Mono', monospace; font-size: 12px; letter-spacing: 2px;"
        )
        self.lbl_timer = QLabel("00:00")
        self.lbl_timer.setStyleSheet("color: #E6F4FF; font-family: 'JetBrains Mono', monospace; font-size: 12px;")
        top_row.addWidget(self.lbl_heading)
        top_row.addStretch()
        top_row.addWidget(self.lbl_timer)
        root.addLayout(top_row)

        root.addSpacing(145)

        self.lbl_name = QLabel("Mom")
        self.lbl_name.setAlignment(align_center())
        self.lbl_name.setStyleSheet("color: #E6F4FF; font-size: 28px; font-weight: 600;")
        root.addWidget(self.lbl_name)

        self.lbl_number = QLabel("+1 (555) 014-2290")
        self.lbl_number.setAlignment(align_center())
        self.lbl_number.setStyleSheet("color: #8AA4BF; font-family: 'JetBrains Mono', monospace; font-size: 13px;")
        root.addWidget(self.lbl_number)

        self.lbl_meta = QLabel("Mobile · via phone bridge")
        self.lbl_meta.setAlignment(align_center())
        self.lbl_meta.setStyleSheet("color: #4A6079; font-size: 12px;")
        root.addWidget(self.lbl_meta)

        self.lbl_routed = QLabel("E.V. voice routed into call")
        self.lbl_routed.setAlignment(align_center())
        self.lbl_routed.setStyleSheet("color: #8AA4BF; font-size: 12px; margin-top: 26px;")
        self.lbl_routed.hide()
        root.addWidget(self.lbl_routed)

        root.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(48)
        btn_row.addStretch()

        end_col = QVBoxLayout()
        end_col.setAlignment(align_center())
        self.btn_end = QPushButton("✕")
        self.btn_end.setFixedSize(64, 64)
        self.btn_end.setCursor(pointing_hand_cursor())
        self.btn_end.setStyleSheet(
            "QPushButton {"
            "  background-color: rgba(255, 77, 109, 26);"
            "  border: 2px solid #FF4D6D;"
            "  border-radius: 32px;"
            "  color: #FF4D6D;"
            "  font-size: 20px;"
            "  font-weight: 700;"
            "}"
            "QPushButton:hover { background-color: rgba(255, 77, 109, 56); }"
        )
        self.btn_end.clicked.connect(self.end_clicked.emit)
        self.lbl_end = QLabel("Ignore")
        self.lbl_end.setAlignment(align_center())
        self.lbl_end.setStyleSheet("color: #8AA4BF; font-size: 12px;")
        end_col.addWidget(self.btn_end)
        end_col.addWidget(self.lbl_end)
        btn_row.addLayout(end_col)

        self.accept_col_widget = QWidget()
        acc_col = QVBoxLayout(self.accept_col_widget)
        acc_col.setContentsMargins(0, 0, 0, 0)
        acc_col.setAlignment(align_center())
        self.btn_accept = QPushButton("✆")
        self.btn_accept.setFixedSize(64, 64)
        self.btn_accept.setCursor(pointing_hand_cursor())
        self.btn_accept.setStyleSheet(
            "QPushButton {"
            "  background-color: #00D4FF;"
            "  border: 2px solid #00D4FF;"
            "  border-radius: 32px;"
            "  color: #0B0F19;"
            "  font-size: 22px;"
            "  font-weight: 700;"
            "}"
            "QPushButton:hover { background-color: #33DDFF; }"
        )
        self.btn_accept.clicked.connect(self.accept_clicked.emit)
        self.lbl_accept = QLabel("Accept")
        self.lbl_accept.setAlignment(align_center())
        self.lbl_accept.setStyleSheet("color: #8AA4BF; font-size: 12px;")
        acc_col.addWidget(self.btn_accept)
        acc_col.addWidget(self.lbl_accept)
        btn_row.addWidget(self.accept_col_widget)

        btn_row.addStretch()
        root.addLayout(btn_row)

    def sync_call(self, call: CallState) -> None:
        self.call_state = call
        ringing = (call.status == "ringing")
        if ringing:
            heading = "INCOMING CALL" if call.direction == "incoming" else "SCHEDULED CALL READY"
        else:
            heading = "CONNECTED"
        self.lbl_heading.setText(heading)
        self.lbl_timer.setText(format_duration(call.seconds))
        self.lbl_name.setText(call.name)
        self.lbl_number.setText(call.number)
        self.lbl_meta.setText(call.meta)

        if ringing:
            self.lbl_end.setText("Ignore" if call.direction == "incoming" else "Cancel")
            self.lbl_accept.setText("Accept" if call.direction == "incoming" else "Connect")
            self.accept_col_widget.show()
            self.lbl_routed.hide()
        else:
            self.lbl_end.setText("End call")
            self.accept_col_widget.hide()
            self.lbl_routed.show()
        self.update()

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(antialias_hint())

        t = time.monotonic() - self._t0
        w = float(self.width())
        cx = w / 2.0
        cy = 132.0

        painter.save()
        painter.translate(cx, cy)
        painter.rotate((t * 12.0) % 360.0)
        p1 = QPen(QColor(0, 212, 255, 42), 1.0)
        p1.setStyle(custom_dash_line())
        p1.setDashPattern([3.0, 10.0])
        painter.setPen(p1)
        painter.setBrush(no_brush())
        painter.drawEllipse(QPointF(0, 0), 145.0, 145.0)

        p2 = QPen(QColor(123, 97, 255, 50), 1.4)
        p2.setStyle(custom_dash_line())
        p2.setDashPattern([40.0, 20.0, 10.0, 30.0])
        painter.setPen(p2)
        painter.drawEllipse(QPointF(0, 0), 115.0, 115.0)
        painter.restore()

        if self.call_state.status == "ringing":
            for i in range(2):
                phase = ((t / 1.6) + i * 0.5) % 1.0
                r_rip = 54.0 * (1.0 + 0.6 * phase)
                c_rip = QColor(COLOR_CYAN)
                c_rip.setAlpha(int(175 * (1.0 - phase)))
                painter.setPen(QPen(c_rip, 1.4))
                painter.drawEllipse(QPointF(cx, cy), r_rip, r_rip)

        painter.setPen(QPen(COLOR_CYAN, 2.0))
        painter.setBrush(QBrush(QColor(0, 212, 255, 28)))
        painter.drawEllipse(QPointF(cx, cy), 54.0, 54.0)

        initial = (self.call_state.name[:1] or "M").upper()
        painter.setPen(COLOR_CYAN)
        f = QFont("Inter", 28)
        painter.setFont(f)
        painter.drawText(QRectF(cx - 54.0, cy - 54.0, 108.0, 108.0), align_center(), initial)

        if self.call_state.status == "connected":
            bars_y = 292.0
            total_w = 18 * 6.0
            start_x = cx - total_w / 2.0
            painter.setPen(no_pen())
            painter.setBrush(QBrush(COLOR_CYAN))
            for i in range(18):
                bh = 4.0 + abs(math.sin(t * 6.5 + i * 0.45)) * (8.0 + (i * 7) % 14)
                bx = start_x + i * 6.0
                painter.drawRoundedRect(QRectF(bx, bars_y - bh / 2.0, 3.5, bh), 1.5, 1.5)