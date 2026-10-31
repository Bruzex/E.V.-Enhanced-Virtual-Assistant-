#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Holographic HUD Main Window
===============================================================
Native PyQt6 / PyQt5 implementation of the MagicPatterns Dark Holographic HUD:
  - BackgroundField: 48px/240px cyan grid, 70 drifting cyan/purple particles,
    CRT scanlines, animated sweep line, and radial vignette
  - TopStatusBar: Animated E.V. wordmark, live HH:mm:ss clock, CPU/RAM pill,
    and 4 connection status indicators (local, mic, phone, whatsapp)
  - Central StatusOrb: 5-state animated holographic orb (idle, listening,
    thinking, speaking, acting) with 90-tick outer ring, counter-rotating
    dashed ring, segmented arc ring, progress arc, hex grid core, and
    OrbReadout with word-by-word captions
  - Left WorkspacePanel: GlassPanel with corner brackets, 10 workspace items,
    live progress bar, and "Undo workspace" button
  - Right CountdownOverlay: Circular SVG-style progress ring, large tabular
    seconds readout, and Cancel / Run now controls
  - Top-Right MetricsPanel: CPU & RAM sparklines, Battery bar, and 0 MB GPU cell
  - Bottom ControlStrip: Floating pill dock (Workspace, Timer, Call, Mic with
    pulsing ring, Metrics, Settings popover with 4 switches + Orb state preview)
  - Full-Screen CallOverlay: Iron-Man-style incoming/outgoing call modal with
    rotating rings, scanline, audio equalizer bars, and Accept/Ignore/End buttons
  - ClickSparks: 8-ray cyan spark bursts on button clicks
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from PyQt6.QtCore import (
        QEasingCurve,
        QPointF,
        QRectF,
        Qt,
        QTimer,
        pyqtSignal,
    )
    from PyQt6.QtGui import (
        QBrush,
        QColor,
        QFont,
        QKeyEvent,
        QLinearGradient,
        QMouseEvent,
        QPainter,
        QPainterPath,
        QPen,
        QPolygonF,
        QRadialGradient,
    )
    from PyQt6.QtWidgets import (
        QApplication,
        QFrame,
        QGraphicsDropShadowEffect,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QVBoxLayout,
        QWidget,
    )
    QT_VERSION = 6
except ImportError:
    from PyQt5.QtCore import (  # type: ignore
        QEasingCurve,
        QPointF,
        QRectF,
        Qt,
        QTimer,
        pyqtSignal,
    )
    from PyQt5.QtGui import (  # type: ignore
        QBrush,
        QColor,
        QFont,
        QKeyEvent,
        QLinearGradient,
        QMouseEvent,
        QPainter,
        QPainterPath,
        QPen,
        QPolygonF,
        QRadialGradient,
    )
    from PyQt5.QtWidgets import (  # type: ignore
        QApplication,
        QFrame,
        QGraphicsDropShadowEffect,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QVBoxLayout,
        QWidget,
    )
    QT_VERSION = 5


# =============================================================================
# Theme Constants (Exact MagicPatterns Tailwind Palette)
# =============================================================================
COLOR_BG = QColor("#0B0F19")
COLOR_DEEP = QColor("#060912")
COLOR_PANEL = QColor(13, 20, 34, 195)      # #0D1422 / 75% alpha
COLOR_PANEL_STRONG = QColor(13, 20, 34, 230)
COLOR_LINE = QColor("#1B2A3F")
COLOR_CYAN = QColor("#00D4FF")
COLOR_PURPLE = QColor("#7B61FF")
COLOR_LIGHT_PURPLE = QColor("#B9A9FF")
COLOR_ACTING_CYAN = QColor("#7FE9FF")
COLOR_TEXT = QColor("#E6F4FF")
COLOR_MUTED = QColor("#8AA4BF")
COLOR_FAINT = QColor("#4A6079")
COLOR_DANGER = QColor("#FF4D6D")
COLOR_SUCCESS = QColor("#3DF5A7")

ORB_STATE_LABELS: Dict[str, str] = {
    "idle": "Standing by",
    "listening": "Listening",
    "thinking": "Processing locally",
    "speaking": "Speaking",
    "acting": "Executing",
}

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


def format_duration(total_seconds: int) -> str:
    m = max(0, total_seconds) // 60
    s = max(0, total_seconds) % 60
    return f"{m:02d}:{s:02d}"


def align_center() -> Any:
    return Qt.AlignmentFlag.AlignCenter if QT_VERSION == 6 else Qt.AlignCenter


def align_left_vcenter() -> Any:
    if QT_VERSION == 6:
        return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    return Qt.AlignLeft | Qt.AlignVCenter


def antialias_hint() -> Any:
    return QPainter.RenderHint.Antialiasing if QT_VERSION == 6 else QPainter.Antialiasing


def pen_round_cap() -> Any:
    return Qt.PenCapStyle.RoundCap if QT_VERSION == 6 else Qt.RoundCap


def no_pen() -> Any:
    return Qt.PenStyle.NoPen if QT_VERSION == 6 else Qt.NoPen


def solid_line() -> Any:
    return Qt.PenStyle.SolidLine if QT_VERSION == 6 else Qt.SolidLine


def custom_dash_line() -> Any:
    return Qt.PenStyle.CustomDashLine if QT_VERSION == 6 else Qt.CustomDashLine


# =============================================================================
# Data Models
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

        # Panel fill
        painter.setPen(no_pen())
        painter.setBrush(QBrush(COLOR_PANEL_STRONG if self.strong else COLOR_PANEL))
        painter.drawRoundedRect(rect, radius, radius)

        # Border stroke
        border_color = QColor(COLOR_CYAN)
        border_color.setAlpha(150 if self.strong else 55)
        painter.setPen(QPen(border_color, 1.1))
        painter.setBrush(Qt.BrushStyle.NoBrush if QT_VERSION == 6 else Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

        # Four corner HUD brackets (left/right/top/bottom 8px inset, 10px arm length)
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
# Central Animated Status Orb Widget (Exact SVG Port from StatusOrb.tsx)
# =============================================================================
class StatusOrbWidget(QWidget):
    """
    Renders the central holographic Status Orb across all 5 states:
      - idle: Slow cyan pulse (4s period), low aura opacity
      - listening: Brighter core + 3 expanding sonar rings
      - thinking: Purple core + 3 counter-rotating particle orbits (ThinkingSwirl)
      - speaking: Cyan core + 72 radial audio waveform bars (SpeakingBars)
      - acting: Bright ice-cyan core + sharp pulses + circular progress arc
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.state: str = "idle"
        self.progress: float = 0.0
        self.minimal_ui: bool = False
        self._start_time: float = time.monotonic()
        self.setMinimumSize(380, 380)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding if QT_VERSION == 6 else QSizePolicy.Expanding,
            QSizePolicy.Policy.Expanding if QT_VERSION == 6 else QSizePolicy.Expanding,
        )

    def set_state(self, state: str, progress: Optional[float] = None) -> None:
        if state in ORB_STATE_LABELS:
            self.state = state
        if progress is not None:
            self.progress = max(0.0, min(1.0, float(progress)))
        self.update()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(antialias_hint())

        w = float(self.width())
        h = float(self.height())
        side = min(w, h, 460.0)
        scale = side / 400.0

        painter.save()
        painter.translate(w / 2.0, h / 2.0)
        painter.scale(scale, scale)

        t = time.monotonic() - self._start_time

        # Core color selection
        if self.state == "thinking":
            core_color = QColor(COLOR_PURPLE)
            ring_color = QColor(COLOR_PURPLE)
        elif self.state == "acting":
            core_color = QColor(COLOR_ACTING_CYAN)
            ring_color = QColor(COLOR_CYAN)
        else:
            core_color = QColor(COLOR_CYAN)
            ring_color = QColor(COLOR_CYAN)

        aura_alpha_map = {
            "idle": 38,
            "listening": 88,
            "thinking": 78,
            "speaking": 78,
            "acting": 102,
        }
        aura_alpha = aura_alpha_map.get(self.state, 38)

        # 1. Ambient outer aura glow
        aura_grad = QRadialGradient(QPointF(0, 0), 165.0)
        c_aura = QColor(core_color)
        c_aura.setAlpha(aura_alpha)
        c_zero = QColor(core_color)
        c_zero.setAlpha(0)
        aura_grad.setColorAt(0.0, c_aura)
        aura_grad.setColorAt(0.65, QColor(core_color.red(), core_color.green(), core_color.blue(), aura_alpha // 2))
        aura_grad.setColorAt(1.0, c_zero)
        painter.setPen(no_pen())
        painter.setBrush(QBrush(aura_grad))
        painter.drawEllipse(QPointF(0, 0), 165.0, 165.0)

        # 2. Outer 90-tick ring (rotates 360 deg every 90s)
        painter.save()
        if not self.minimal_ui:
            painter.rotate((t * 4.0) % 360.0)
        for i in range(90):
            angle = (i / 90.0) * math.tau
            is_long = (i % 5 == 0)
            r1 = 184.0
            r2 = r1 + (9.0 if is_long else 4.0)
            tick_col = QColor(COLOR_CYAN)
            tick_col.setAlpha(128 if is_long else 56)
            painter.setPen(QPen(tick_col, 1.2 if is_long else 0.8))
            painter.drawLine(
                QPointF(math.cos(angle) * r1, math.sin(angle) * r1),
                QPointF(math.cos(angle) * r2, math.sin(angle) * r2),
            )
        painter.restore()

        # 3. Counter-rotating dashed ring (r = 168)
        painter.save()
        if not self.minimal_ui:
            painter.rotate((-t * 9.0) % 360.0)
        dash_pen = QPen(QColor(ring_color.red(), ring_color.green(), ring_color.blue(), 115), 1.1)
        dash_pen.setStyle(custom_dash_line())
        dash_pen.setDashPattern([2.0, 9.0])
        painter.setPen(dash_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush if QT_VERSION == 6 else Qt.NoBrush)
        painter.drawEllipse(QPointF(0, 0), 168.0, 168.0)
        painter.restore()

        # 4. Segmented arc ring (r = 158)
        painter.save()
        seg_speed = 60.0 if self.state == "thinking" else 13.8
        if not self.minimal_ui:
            painter.rotate((t * seg_speed) % 360.0)
        seg_pen = QPen(QColor(0, 212, 255, 78), 2.0)
        seg_pen.setStyle(custom_dash_line())
        seg_pen.setDashPattern([35.0, 15.0, 9.0, 70.0, 20.0, 30.0])
        painter.setPen(seg_pen)
        painter.drawEllipse(QPointF(0, 0), 158.0, 158.0)
        painter.restore()

        # 5. Progress arc when state == 'acting' (r = 150)
        if self.state == "acting" or self.progress > 0.005:
            track_col = QColor(COLOR_CYAN)
            track_col.setAlpha(38)
            painter.setPen(QPen(track_col, 1.2))
            painter.drawEllipse(QPointF(0, 0), 150.0, 150.0)

            arc_pen = QPen(QColor(COLOR_CYAN), 3.2)
            arc_pen.setCapStyle(pen_round_cap())
            painter.setPen(arc_pen)
            span_angle = int(-self.progress * 360.0 * 16)
            painter.drawArc(QRectF(-150.0, -150.0, 300.0, 300.0), 90 * 16, span_angle)

        # 6. State-specific dynamic overlays
        if self.state == "listening":
            for i in range(3):
                phase = ((t / 1.8) + (i / 3.0)) % 1.0
                r_wave = 120.0 * (1.0 + 0.55 * phase)
                alpha = int(155 * (1.0 - phase))
                wave_col = QColor(COLOR_CYAN)
                wave_col.setAlpha(max(0, alpha))
                painter.setPen(QPen(wave_col, 1.5))
                painter.drawEllipse(QPointF(0, 0), r_wave, r_wave)

        elif self.state == "acting":
            for i in range(2):
                phase = ((t / 0.95) + (i * 0.45)) % 1.0
                r_pulse = 122.0 * (1.0 + 0.26 * phase)
                alpha = int(220 * (1.0 - phase))
                pulse_col = QColor("#BFF4FF")
                pulse_col.setAlpha(max(0, alpha))
                painter.setPen(QPen(pulse_col, 2.4))
                painter.drawEllipse(QPointF(0, 0), r_pulse, r_pulse)

        elif self.state == "speaking":
            # 72 radial waveform bars around r = 127
            envelope = 0.45 + 0.55 * abs(math.sin(t * 1.7) * math.sin(t * 0.6 + 1.0))
            bar_pen = QPen(QColor(0, 212, 255, 218), 2.0)
            bar_pen.setCapStyle(pen_round_cap())
            painter.setPen(bar_pen)
            for i in range(72):
                a = (i / 72.0) * math.tau
                v = abs(math.sin(t * 7.0 + i * 0.55) * math.sin(t * 3.1 + i * 0.23))
                bar_len = 6.0 if self.minimal_ui else (3.0 + v * 24.0 * envelope)
                r1 = 127.0
                r2 = r1 + bar_len
                painter.drawLine(
                    QPointF(math.cos(a) * r1, math.sin(a) * r1),
                    QPointF(math.cos(a) * r2, math.sin(a) * r2),
                )

        elif self.state == "thinking":
            # 3 particle swirl orbits around r = 134, 146, 124
            orbits = [
                (134.0, 16, COLOR_CYAN, 2.4, 1.0, 3.2),
                (146.0, 12, COLOR_PURPLE, 3.6, -1.0, 2.6),
                (124.0, 9, COLOR_LIGHT_PURPLE, 1.7, 1.0, 2.0),
            ]
            painter.setPen(no_pen())
            for oi, (orb_r, count, col, dur, direction, dot_size) in enumerate(orbits):
                base_rot = (t / dur) * math.tau * direction
                for i in range(count):
                    a = base_rot + math.radians(-i * 7.0 * direction + oi * 120.0)
                    dot_r = max(0.6, dot_size - i * (dot_size / count))
                    c_dot = QColor(col)
                    c_dot.setAlphaF(max(0.05, 1.0 - (i / count)))
                    painter.setBrush(QBrush(c_dot))
                    painter.drawEllipse(
                        QPointF(math.cos(a) * orb_r, math.sin(a) * orb_r),
                        dot_r,
                        dot_r,
                    )

        # 7. Core shell (r = 118) with dark #07101D fill & hex lattice
        painter.setPen(QPen(QColor(ring_color.red(), ring_color.green(), ring_color.blue(), 145), 1.3))
        painter.setBrush(QBrush(QColor(7, 16, 29, 220)))
        painter.drawEllipse(QPointF(0, 0), 118.0, 118.0)

        # Clip interior hex lattice and scanline to r = 118
        painter.save()
        clip_path = QPainterPath()
        clip_path.addEllipse(QPointF(0, 0), 117.0, 117.0)
        painter.setClipPath(clip_path)

        hex_pen = QPen(QColor(0, 212, 255, 34), 0.7)
        painter.setPen(hex_pen)
        dx = 18.0
        dy = 15.5
        for row in range(-8, 9):
            for col_idx in range(-8, 9):
                cx = col_idx * dx + (dx * 0.5 if row % 2 else 0.0)
                cy = row * dy
                if cx * cx + cy * cy <= 125.0 * 125.0:
                    poly = QPolygonF()
                    for k in range(6):
                        ang = math.radians(60 * k + 30)
                        poly.append(QPointF(cx + 8.5 * math.cos(ang), cy + 8.5 * math.sin(ang)))
                    painter.drawPolygon(poly)

        # Interior vertical scanline (sweeps from -118 to +118 over 3.5s)
        if not self.minimal_ui:
            scan_y = -118.0 + ((t % 3.5) / 3.5) * 236.0
            scan_grad = QLinearGradient(QPointF(-118.0, scan_y), QPointF(118.0, scan_y))
            scan_grad.setColorAt(0.0, QColor(0, 212, 255, 0))
            scan_grad.setColorAt(0.5, QColor(0, 212, 255, 95))
            scan_grad.setColorAt(1.0, QColor(0, 212, 255, 0))
            painter.setPen(QPen(QBrush(scan_grad), 2.0))
            painter.drawLine(QPointF(-118.0, scan_y), QPointF(118.0, scan_y))

        painter.restore()

        # Inner dashed ring (r = 74)
        inner_dash = QPen(QColor(0, 212, 255, 64), 1.0)
        inner_dash.setStyle(custom_dash_line())
        inner_dash.setDashPattern([1.5, 4.5])
        painter.setPen(inner_dash)
        painter.setBrush(Qt.BrushStyle.NoBrush if QT_VERSION == 6 else Qt.NoBrush)
        painter.drawEllipse(QPointF(0, 0), 74.0, 74.0)

        # 8. Animated Energy Core (bloom r=58, soft r=30, white-cyan nucleus r=11)
        if self.state == "idle":
            pulse = 1.0 + 0.06 * math.sin(t * math.tau / 4.0)
            core_alpha = int(115 + 35 * math.sin(t * math.tau / 4.0))
        elif self.state == "listening":
            pulse = 1.12 + 0.08 * (0.5 + 0.5 * math.sin(t * math.tau / 1.4))
            core_alpha = 240
        elif self.state == "thinking":
            pulse = 0.90 + 0.07 * (0.5 + 0.5 * math.sin(t * math.tau / 1.2))
            core_alpha = 205
        elif self.state == "speaking":
            pulse = 1.0 + 0.14 * abs(math.sin(t * 4.5))
            core_alpha = 230
        else:  # acting
            pulse = 1.0 + 0.18 * abs(math.sin(t * math.tau / 0.6))
            core_alpha = 245

        bloom_r = 68.0 * pulse
        bloom_grad = QRadialGradient(QPointF(0, 0), bloom_r)
        c_mid = QColor(core_color)
        c_mid.setAlpha(core_alpha)
        c_edge = QColor(core_color)
        c_edge.setAlpha(0)
        bloom_grad.setColorAt(0.0, c_mid)
        bloom_grad.setColorAt(0.55, QColor(core_color.red(), core_color.green(), core_color.blue(), core_alpha // 2))
        bloom_grad.setColorAt(1.0, c_edge)

        painter.setPen(no_pen())
        painter.setBrush(QBrush(bloom_grad))
        painter.drawEllipse(QPointF(0, 0), bloom_r, bloom_r)

        soft_r = 32.0 * pulse
        soft_grad = QRadialGradient(QPointF(0, 0), soft_r)
        soft_grad.setColorAt(0.0, QColor(core_color.red(), core_color.green(), core_color.blue(), min(255, core_alpha + 25)))
        soft_grad.setColorAt(1.0, c_edge)
        painter.setBrush(QBrush(soft_grad))
        painter.drawEllipse(QPointF(0, 0), soft_r, soft_r)

        # Bright center point (r = 11, #E9FCFF)
        nucleus_grad = QRadialGradient(QPointF(0, 0), 14.0)
        nucleus_grad.setColorAt(0.0, QColor(233, 252, 255, 250))
        nucleus_grad.setColorAt(0.75, QColor(233, 252, 255, 180))
        nucleus_grad.setColorAt(1.0, QColor(0, 212, 255, 0))
        painter.setBrush(QBrush(nucleus_grad))
        painter.drawEllipse(QPointF(0, 0), 14.0, 14.0)

        painter.restore()


# =============================================================================
# Sparkline Widget for Top-Right Metrics Panel
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
        painter.setBrush(Qt.BrushStyle.NoBrush if QT_VERSION == 6 else Qt.NoBrush)
        painter.drawPolyline(QPolygonF(pts))


# =============================================================================
# Top-Right System Metrics Floating Overlay (MetricsPanel.tsx)
# =============================================================================
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

        # Close button
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
        self.state = "pending"  # 'pending' | 'active' | 'done'
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

        # Header
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

        # Scrollable item list
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

        # Divider
        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet("background-color: rgba(0, 212, 255, 25);")
        root.addWidget(div)

        # Progress bar section
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

        # Undo workspace button
        self.btn_undo = QPushButton("↶  Undo workspace")
        self.btn_undo.setFixedHeight(36)
        self.btn_undo.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
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

        # Outer dotted ring (r + 9)
        outer_pen = QPen(QColor(0, 212, 255, 64), 1.0)
        outer_pen.setStyle(custom_dash_line())
        outer_pen.setDashPattern([1.5, 5.0])
        painter.setPen(outer_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush if QT_VERSION == 6 else Qt.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), r + 9.0, r + 9.0)

        # Background track ring
        painter.setPen(QPen(QColor(0, 212, 255, 32), 5.5))
        painter.drawEllipse(QPointF(cx, cy), r, r)

        # Progress arc
        frac = max(0.0, min(1.0, self.remaining / float(self.total)))
        arc_pen = QPen(accent, 5.5)
        arc_pen.setCapStyle(pen_round_cap())
        painter.setPen(arc_pen)
        painter.drawArc(
            QRectF(cx - r, cy - r, r * 2.0, r * 2.0),
            90 * 16,
            int(-frac * 360.0 * 16),
        )

        # Large seconds readout
        painter.setPen(accent)
        num_font = QFont("JetBrains Mono", 36)
        num_font.setBold(True)
        painter.setFont(num_font)
        painter.drawText(QRectF(0, 34, w, 68), align_center(), str(self.remaining))

        # "seconds" caption
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
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
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
        self.btn_run.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
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
    preview_selected = pyqtSignal(object)  # None or str

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
            btn_sw.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
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
            b.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
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

        # Spacer for painted avatar circle + rings
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

        # Action buttons row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(48)
        btn_row.addStretch()

        end_col = QVBoxLayout()
        end_col.setAlignment(align_center())
        self.btn_end = QPushButton("✕")
        self.btn_end.setFixedSize(64, 64)
        self.btn_end.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
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
        self.btn_accept.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
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

        # Background rotating decorative rings
        painter.save()
        painter.translate(cx, cy)
        painter.rotate((t * 12.0) % 360.0)
        p1 = QPen(QColor(0, 212, 255, 42), 1.0)
        p1.setStyle(custom_dash_line())
        p1.setDashPattern([3.0, 10.0])
        painter.setPen(p1)
        painter.setBrush(Qt.BrushStyle.NoBrush if QT_VERSION == 6 else Qt.NoBrush)
        painter.drawEllipse(QPointF(0, 0), 145.0, 145.0)

        p2 = QPen(QColor(123, 97, 255, 50), 1.4)
        p2.setStyle(custom_dash_line())
        p2.setDashPattern([40.0, 20.0, 10.0, 30.0])
        painter.setPen(p2)
        painter.drawEllipse(QPointF(0, 0), 115.0, 115.0)
        painter.restore()

        # Ringing expanding ripples
        if self.call_state.status == "ringing":
            for i in range(2):
                phase = ((t / 1.6) + i * 0.5) % 1.0
                r_rip = 54.0 * (1.0 + 0.6 * phase)
                c_rip = QColor(COLOR_CYAN)
                c_rip.setAlpha(int(175 * (1.0 - phase)))
                painter.setPen(QPen(c_rip, 1.4))
                painter.drawEllipse(QPointF(cx, cy), r_rip, r_rip)

        # Avatar circle
        painter.setPen(QPen(COLOR_CYAN, 2.0))
        painter.setBrush(QBrush(QColor(0, 212, 255, 28)))
        painter.drawEllipse(QPointF(cx, cy), 54.0, 54.0)

        initial = (self.call_state.name[:1] or "M").upper()
        painter.setPen(COLOR_CYAN)
        f = QFont("Inter", 28)
        painter.setFont(f)
        painter.drawText(QRectF(cx - 54.0, cy - 54.0, 108.0, 108.0), align_center(), initial)

        # Connected 18-bar voice equalizer
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


# =============================================================================
# Main Holographic HUD Window (Hud.tsx + useAssistant.ts state machine)
# =============================================================================
class EVMainWindow(QMainWindow):
    """
    Main PyQt HUD Window integrating the complete MagicPatterns design and
    exposing clean slots/methods for Phase 1–4 backend modules.
    """

    setting_changed = pyqtSignal(str, bool)

    def __init__(self, config: Dict[str, Any], base_dir: Path) -> None:
        super().__init__()
        self.config = config
        self.base_dir = base_dir

        gui_cfg = config.get("gui", {})
        self.setWindowTitle(gui_cfg.get("title", "E.V. — Enhanced Virtual Assistant"))
        self.resize(int(gui_cfg.get("width", 1440)), int(gui_cfg.get("height", 900)))
        self.setMinimumSize(
            int(gui_cfg.get("min_width", 1024)),
            int(gui_cfg.get("min_height", 640)),
        )

        # Visual settings matching Hud.tsx
        bg_effect = str(gui_cfg.get("background_effect", "grid-and-particles"))
        self.show_grid: bool = bg_effect != "particles"
        self.show_particles: bool = bg_effect != "grid"
        self.scanlines: bool = bool(gui_cfg.get("scanlines", True))
        self.countdown_default_sec: int = max(3, int(gui_cfg.get("default_countdown_seconds", 30)))

        self.settings: Dict[str, bool] = {
            "wakeWord": bool(config.get("voice", {}).get("wake_word", {}).get("enabled", True)),
            "clapDetection": bool(config.get("voice", {}).get("clap_detector", {}).get("enabled", True)),
            "continuousListening": bool(config.get("voice", {}).get("wake_word", {}).get("continuous_listening", False)),
            "minimalUi": bool(gui_cfg.get("minimal_ui", False)),
        }

        # Assistant state machine matching useAssistant.ts
        self._base_orb_state: str = "idle"
        self._preview_orb_state: Optional[str] = None
        self._orb_progress_override: Optional[float] = None
        self.voice_active: bool = False
        self._external_mic_handler: Optional[Callable[[bool], None]] = None
        self._external_workspace_launch_handler: Optional[Callable[[], None]] = None
        self._external_workspace_undo_handler: Optional[Callable[[], None]] = None
        self._external_workspace_cancel_handler: Optional[Callable[[], None]] = None
        self._external_scheduler_start_quick_handler: Optional[Callable[[], None]] = None
        self._external_scheduler_cancel_handler: Optional[Callable[[], None]] = None
        self._external_scheduler_run_now_handler: Optional[Callable[[], None]] = None
        self._external_call_accept_handler: Optional[Callable[[], None]] = None
        self._external_call_end_handler: Optional[Callable[[], None]] = None
        self._chain_workspace_to_call: bool = False

        self.caption_speaker: Optional[str] = None
        self.caption_text: str = ""
        self.caption_born_ms: float = 0.0

        self.workspace_items = self._load_workspace_items()
        self.workspace_state: Optional[WorkspaceState] = None
        self.countdown_state: Optional[CountdownState] = None
        self.call_state: Optional[CallState] = None

        # Connection statuses ('online' | 'standby' | 'offline')
        self.connections: Dict[str, str] = {
            "local": "online",
            "mic": "online",
            "phone": "online",
            "whatsapp": "standby",
        }

        # System metrics & sparkline history (28 samples)
        self.cpu_pct: int = 14
        self.ram_gb: float = 2.4
        self.battery_pct: int = 86
        self.cpu_history: List[float] = [12.0 + random.random() * 6.0 for _ in range(28)]
        self.ram_history: List[float] = [2.3 + random.random() * 0.12 for _ in range(28)]

        # Background particles & click sparks
        self._t0 = time.monotonic()
        self.particles: List[Particle] = []
        self.sparks: List[SparkBurst] = []
        self._init_particles(int(gui_cfg.get("particle_density", 70)))

        # Scheduled timer callbacks
        self._oneshot_timers: List[QTimer] = []

        # Build UI hierarchy
        self._build_ui()

        # Master 30 FPS render & particle loop (lightweight CPU footprint)
        fps = max(15, min(60, int(gui_cfg.get("fps", 30))))
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._on_frame_tick)
        self._anim_timer.start(int(1000 / fps))

        # 1-second clock & countdown/call tick
        self._sec_timer = QTimer(self)
        self._sec_timer.timeout.connect(self._on_second_tick)
        self._sec_timer.start(1000)

        # Workspace sequential step timer (520ms launch / 150ms undo)
        self._ws_timer = QTimer(self)
        self._ws_timer.timeout.connect(self._on_workspace_step)

    def _load_workspace_items(self) -> List[Dict[str, str]]:
        ws_file = self.base_dir / "data" / "workspaces" / "coding.json"
        if ws_file.exists():
            try:
                data = json.loads(ws_file.read_text(encoding="utf-8"))
                items = data.get("items", [])
                if isinstance(items, list) and items:
                    return items
            except Exception:
                pass
        return list(DEFAULT_WORKSPACE_ITEMS)

    def _init_particles(self, density: int) -> None:
        w = max(1280, self.width())
        h = max(800, self.height())
        self.particles = [
            Particle(
                x=random.random() * w,
                y=random.random() * h,
                vx=(random.random() - 0.5) * 0.32,
                vy=-(random.random() * 0.36 + 0.08),
                r=random.random() * 1.4 + 0.4,
                a=random.random() * 0.5 + 0.14,
                tw=random.random() * math.tau,
                purple=(random.random() < 0.18),
            )
            for _ in range(density)
        ]

    # =========================================================================
    # UI Construction
    # =========================================================================
    def _build_ui(self) -> None:
        self.canvas = QWidget(self)
        self.setCentralWidget(self.canvas)

        root_layout = QVBoxLayout(self.canvas)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # 1. Top Status Bar (56px height)
        self.top_bar = QFrame(self.canvas)
        self.top_bar.setFixedHeight(56)
        self.top_bar.setStyleSheet(
            "QFrame { background-color: rgba(11, 15, 25, 165); border-bottom: 1px solid rgba(0, 212, 255, 28); }"
        )
        top_layout = QHBoxLayout(self.top_bar)
        top_layout.setContentsMargins(24, 0, 24, 0)

        # Left Wordmark
        wm_row = QHBoxLayout()
        wm_row.setSpacing(12)
        lbl_ev = QLabel("E.V.")
        lbl_ev.setStyleSheet(
            "color: #00D4FF; font-size: 18px; font-weight: 700; letter-spacing: 3px; border: none; background: transparent;"
        )
        lbl_full = QLabel("ENHANCED VIRTUAL ASSISTANT")
        lbl_full.setStyleSheet(
            "color: #8AA4BF; font-size: 11px; font-weight: 500; letter-spacing: 2.5px; border: none; background: transparent;"
        )
        wm_row.addWidget(lbl_ev)
        wm_row.addWidget(lbl_full)
        top_layout.addLayout(wm_row)

        top_layout.addStretch()

        # Center Digital Clock
        clock_row = QHBoxLayout()
        clock_row.setSpacing(10)
        self.lbl_clock_time = QLabel(datetime.now().strftime("%H:%M:%S"))
        self.lbl_clock_time.setStyleSheet(
            "color: #E6F4FF; font-family: 'JetBrains Mono', monospace; font-size: 14px; font-weight: 500; border: none; background: transparent;"
        )
        self.lbl_clock_date = QLabel(datetime.now().strftime("%a %d %b"))
        self.lbl_clock_date.setStyleSheet(
            "color: #8AA4BF; font-family: 'JetBrains Mono', monospace; font-size: 12px; border: none; background: transparent;"
        )
        clock_row.addWidget(self.lbl_clock_time)
        clock_row.addWidget(self.lbl_clock_date)
        top_layout.addLayout(clock_row)

        top_layout.addStretch()

        # Right System Pill + 4 Connection Dots
        right_row = QHBoxLayout()
        right_row.setSpacing(10)

        self.btn_top_metrics = QPushButton("CPU 14%   RAM 2.4/4.0 GB")
        self.btn_top_metrics.setFixedHeight(32)
        self.btn_top_metrics.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
        self.btn_top_metrics.clicked.connect(self.toggle_metrics_panel)
        self._style_top_metrics_btn(False)
        right_row.addWidget(self.btn_top_metrics)

        sep = QFrame()
        sep.setFixedSize(1, 20)
        sep.setStyleSheet("background-color: rgba(0, 212, 255, 38); border: none;")
        right_row.addWidget(sep)

        self.conn_badges: Dict[str, QLabel] = {}
        conn_meta = [
            ("local", "🛡", "Offline mode · all processing local"),
            ("mic", "🎙", "Microphone ready"),
            ("phone", "📱", "Phone bridge connected (ADB over Wi-Fi)"),
            ("whatsapp", "💬", "WhatsApp Web on standby"),
        ]
        for cid, symbol, tooltip in conn_meta:
            badge = QLabel(f"{symbol}•")
            badge.setToolTip(tooltip)
            badge.setFixedSize(32, 28)
            badge.setAlignment(align_center())
            self.conn_badges[cid] = badge
            right_row.addWidget(badge)
        self._refresh_connection_badges()

        top_layout.addLayout(right_row)
        root_layout.addWidget(self.top_bar)

        # 2. Center Presence Area (Status Orb + OrbReadout)
        center_area = QVBoxLayout()
        center_area.setContentsMargins(0, 16, 0, 96)
        center_area.setSpacing(4)
        center_area.addStretch(1)

        orb_row = QHBoxLayout()
        orb_row.addStretch()
        self.orb_widget = StatusOrbWidget(self.canvas)
        orb_row.addWidget(self.orb_widget)
        orb_row.addStretch()
        center_area.addLayout(orb_row, 4)

        # OrbReadout (state label + live caption / wake-word hint)
        readout_col = QVBoxLayout()
        readout_col.setSpacing(10)
        self.lbl_orb_state = QLabel("●  STANDING BY")
        self.lbl_orb_state.setAlignment(align_center())
        self.lbl_orb_state.setStyleSheet(
            "color: #00D4FF; font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 600; letter-spacing: 3.5px;"
        )
        readout_col.addWidget(self.lbl_orb_state)

        self.lbl_caption = QLabel("Say “Hey E.V.” or tap the mic")
        self.lbl_caption.setAlignment(align_center())
        self.lbl_caption.setWordWrap(True)
        self.lbl_caption.setFixedWidth(680)
        self.lbl_caption.setMinimumHeight(54)
        self.lbl_caption.setStyleSheet("color: #8AA4BF; font-size: 14px;")

        cap_row = QHBoxLayout()
        cap_row.addStretch()
        cap_row.addWidget(self.lbl_caption)
        cap_row.addStretch()
        readout_col.addLayout(cap_row)

        center_area.addLayout(readout_col, 1)
        center_area.addStretch(1)
        root_layout.addLayout(center_area, 1)

        # 3. Floating Overlay Panels (parented to self.canvas for exact HUD placement)
        self.workspace_panel = WorkspacePanelWidget(self.workspace_items, parent=self.canvas)
        self.workspace_panel.undo_requested.connect(self.undo_workspace)
        self.workspace_panel.dismiss_requested.connect(self.dismiss_workspace)
        self.workspace_panel.hide()

        self.countdown_panel = CountdownPanelWidget(parent=self.canvas)
        self.countdown_panel.cancel_requested.connect(self.cancel_countdown)
        self.countdown_panel.run_now_requested.connect(self.run_countdown_now)
        self.countdown_panel.hide()

        self.metrics_panel = MetricsOverlay(parent=self.canvas)
        self.metrics_panel.closed.connect(self.toggle_metrics_panel)
        self.metrics_panel.hide()

        self.settings_popover = SettingsPopoverWidget(parent=self.canvas)
        self.settings_popover.setting_toggled.connect(self._on_toggle_setting)
        self.settings_popover.preview_selected.connect(self._on_select_preview)
        self.settings_popover.hide()

        # 4. Bottom ControlStrip Pill Dock
        self.control_dock = QFrame(self.canvas)
        self.control_dock.setFixedSize(560, 68)
        self.control_dock.setStyleSheet(
            "QFrame {"
            "  background-color: rgba(13, 20, 34, 195);"
            "  border: 1px solid rgba(0, 212, 255, 55);"
            "  border-radius: 34px;"
            "}"
        )
        dock_layout = QHBoxLayout(self.control_dock)
        dock_layout.setContentsMargins(14, 6, 14, 6)
        dock_layout.setSpacing(6)

        self.btn_ws = self._make_dock_btn("⊞  Workspace", self.launch_workspace)
        self.btn_timer = self._make_dock_btn("⏱  Timer", self.start_quick_timer)
        self.btn_call = self._make_dock_btn("✆  Call", self.start_incoming_call)
        dock_layout.addWidget(self.btn_ws)
        dock_layout.addWidget(self.btn_timer)
        dock_layout.addWidget(self.btn_call)

        dock_layout.addWidget(self._dock_sep())

        # Center circular Mic button (54x54)
        self.btn_mic = QPushButton("🎙")
        self.btn_mic.setFixedSize(54, 54)
        self.btn_mic.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
        self.btn_mic.clicked.connect(self.run_voice_command)
        self._style_mic_btn(False)
        dock_layout.addWidget(self.btn_mic)

        dock_layout.addWidget(self._dock_sep())

        self.btn_metrics = self._make_dock_btn("∿  Metrics", self.toggle_metrics_panel)
        self.btn_settings = self._make_dock_btn("⚙", self.toggle_settings_popover, width=42)
        dock_layout.addWidget(self.btn_metrics)
        dock_layout.addWidget(self.btn_settings)

        # 5. Full-window Call Overlay backdrop + modal card
        self.call_backdrop = QFrame(self.canvas)
        self.call_backdrop.setStyleSheet("background-color: rgba(6, 9, 18, 195);")
        backdrop_layout = QVBoxLayout(self.call_backdrop)
        backdrop_layout.setAlignment(align_center())
        self.call_card = CallModalCard(self.call_backdrop)
        self.call_card.accept_clicked.connect(self.accept_call)
        self.call_card.end_clicked.connect(self.end_call)
        backdrop_layout.addWidget(self.call_card)
        self.call_backdrop.hide()

        self._reposition_overlays()
        self._refresh_orb_and_readout()

    def _make_dock_btn(self, text: str, slot: Callable[[], None], width: int = 104) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedSize(width, 42)
        btn.setCursor(Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor)
        btn.clicked.connect(slot)
        self._style_dock_btn(btn, False)
        return btn

    @staticmethod
    def _style_dock_btn(btn: QPushButton, active: bool) -> None:
        if active:
            btn.setStyleSheet(
                "QPushButton {"
                "  background-color: rgba(0, 212, 255, 32);"
                "  color: #00D4FF;"
                "  border: 1px solid rgba(0, 212, 255, 90);"
                "  border-radius: 21px;"
                "  font-size: 13px;"
                "  font-weight: 500;"
                "}"
            )
        else:
            btn.setStyleSheet(
                "QPushButton {"
                "  background-color: transparent;"
                "  color: #8AA4BF;"
                "  border: none;"
                "  border-radius: 21px;"
                "  font-size: 13px;"
                "  font-weight: 500;"
                "}"
                "QPushButton:hover {"
                "  background-color: rgba(0, 212, 255, 24);"
                "  color: #00D4FF;"
                "}"
            )

    def _style_mic_btn(self, active: bool) -> None:
        if active:
            self.btn_mic.setText("■")
            self.btn_mic.setStyleSheet(
                "QPushButton {"
                "  background-color: #00D4FF;"
                "  color: #0B0F19;"
                "  border: 2px solid #00D4FF;"
                "  border-radius: 27px;"
                "  font-size: 16px;"
                "  font-weight: 700;"
                "}"
            )
        else:
            self.btn_mic.setText("🎙")
            self.btn_mic.setStyleSheet(
                "QPushButton {"
                "  background-color: rgba(0, 212, 255, 28);"
                "  color: #00D4FF;"
                "  border: 1.5px solid rgba(0, 212, 255, 140);"
                "  border-radius: 27px;"
                "  font-size: 18px;"
                "}"
                "QPushButton:hover { background-color: rgba(0, 212, 255, 55); }"
            )

    def _style_top_metrics_btn(self, active: bool) -> None:
        bg = "rgba(0, 212, 255, 28)" if active else "transparent"
        border = "rgba(0, 212, 255, 90)" if active else "rgba(0, 212, 255, 30)"
        self.btn_top_metrics.setStyleSheet(
            f"QPushButton {{"
            f"  background-color: {bg};"
            f"  color: #00D4FF;"
            f"  border: 1px solid {border};"
            f"  border-radius: 8px;"
            f"  padding: 0 12px;"
            f"  font-family: 'JetBrains Mono', monospace;"
            f"  font-size: 12px;"
            f"}}"
            f"QPushButton:hover {{ background-color: rgba(0, 212, 255, 28); }}"
        )

    @staticmethod
    def _dock_sep() -> QFrame:
        sep = QFrame()
        sep.setFixedSize(1, 30)
        sep.setStyleSheet("background-color: rgba(0, 212, 255, 38); border: none;")
        return sep

    def _refresh_connection_badges(self) -> None:
        symbols = {"local": "🛡", "mic": "🎙", "phone": "📱", "whatsapp": "💬"}
        colors = {"online": "#3DF5A7", "standby": "#7B61FF", "offline": "#FF4D6D"}
        for cid, badge in self.conn_badges.items():
            status = self.connections.get(cid, "online")
            col = colors.get(status, "#3DF5A7")
            badge.setText(f"{symbols[cid]}•")
            badge.setStyleSheet(
                f"color: {col}; font-size: 13px; border: none; background: transparent;"
            )

    def _reposition_overlays(self) -> None:
        w = self.canvas.width()
        h = self.canvas.height()

        # Left Workspace Panel (vertically centered, 24px from left edge)
        ws_h = 470
        self.workspace_panel.setFixedHeight(ws_h)
        self.workspace_panel.move(24, max(72, (h - ws_h) // 2))

        # Right Countdown Panel (vertically centered, 24px from right edge)
        cd_w = self.countdown_panel.width()
        cd_h = self.countdown_panel.height()
        self.countdown_panel.move(max(24, w - cd_w - 24), max(72, (h - cd_h) // 2))

        # Top-Right Metrics Panel (top: 68px, right: 24px)
        mp_w = self.metrics_panel.width()
        self.metrics_panel.move(max(16, w - mp_w - 24), 68)

        # Bottom ControlStrip Dock (bottom: 20px, centered horizontally)
        dock_w = self.control_dock.width()
        dock_h = self.control_dock.height()
        dock_x = (w - dock_w) // 2
        dock_y = h - dock_h - 20
        self.control_dock.move(dock_x, dock_y)

        # Settings Popover (aligned to right edge of ControlStrip, above dock)
        sp_w = self.settings_popover.width()
        sp_h = self.settings_popover.height()
        self.settings_popover.move(
            min(w - sp_w - 16, dock_x + dock_w - sp_w),
            max(68, dock_y - sp_h - 14),
        )

        # Full-screen Call Overlay Backdrop
        self.call_backdrop.setGeometry(0, 0, w, h)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._reposition_overlays()

    # =========================================================================
    # Public Slots & API for Phase 1–4 Backend Integration
    # =========================================================================
    def update_system_metrics(self, cpu_pct: int, ram_gb: float, battery_pct: int) -> None:
        """Slot connected to SystemBridge / psutil telemetry."""
        self.cpu_pct = max(0, min(100, int(cpu_pct)))
        self.ram_gb = max(0.1, min(16.0, float(ram_gb)))
        self.battery_pct = max(0, min(100, int(battery_pct)))

        self.cpu_history = self.cpu_history[1:] + [float(self.cpu_pct)]
        self.ram_history = self.ram_history[1:] + [float(self.ram_gb)]

        self.btn_top_metrics.setText(f"CPU {self.cpu_pct:2d}%   RAM {self.ram_gb:.1f}/4.0 GB")
        if self.metrics_panel.isVisible():
            self.metrics_panel.update_data(
                self.cpu_pct, self.ram_gb, self.battery_pct, self.cpu_history, self.ram_history
            )

    def set_external_mic_handler(self, handler: Callable[[bool], None]) -> None:
        """Register the SystemBridge mic button handler for live Vosk + Brain + Piper integration."""
        self._external_mic_handler = handler

    def set_external_workspace_handlers(
        self,
        launch_fn: Callable[[], None],
        undo_fn: Callable[[], None],
        cancel_fn: Callable[[], None],
    ) -> None:
        """Register real WorkspaceOrchestrator callbacks so the GUI left panel reflects live OS orchestration."""
        self._external_workspace_launch_handler = launch_fn
        self._external_workspace_undo_handler = undo_fn
        self._external_workspace_cancel_handler = cancel_fn

    def set_external_scheduler_handlers(
        self,
        start_quick_fn: Callable[[], None],
        cancel_fn: Callable[[], None],
        run_now_fn: Callable[[], None],
    ) -> None:
        """Register real TimedCommandScheduler callbacks for right panel & dock timer."""
        self._external_scheduler_start_quick_handler = start_quick_fn
        self._external_scheduler_cancel_handler = cancel_fn
        self._external_scheduler_run_now_handler = run_now_fn

    def set_external_call_handlers(
        self,
        accept_fn: Callable[[], None],
        end_fn: Callable[[], None],
    ) -> None:
        """Register real PhoneADB / CallOverlay callbacks for call accept & end."""
        self._external_call_accept_handler = accept_fn
        self._external_call_end_handler = end_fn

    def on_workspace_progress_event(self, payload: Dict[str, Any]) -> None:
        """Slot driven by `Topics.WORKSPACE_PROGRESS` from the real `WorkspaceOrchestrator`."""
        name = str(payload.get("name", "Coding workspace"))
        status = str(payload.get("status", "running"))
        completed = int(payload.get("completed", 0))
        items = payload.get("items") or self.workspace_items

        # Stop any legacy internal simulation timer
        self._ws_timer.stop()

        self.workspace_state = WorkspaceState(
            name=name,
            status=status,
            completed=completed,
            items=items,
        )
        self.workspace_panel.update_workspace(self.workspace_state)

        if status in ("running", "done", "undoing"):
            self.workspace_panel.show()
            self.workspace_panel.raise_()
            self._style_dock_btn(self.btn_ws, status in ("running", "undoing"))
        elif status == "undone":
            self._schedule_later(self.dismiss_workspace, 1400)

        self._refresh_orb_and_readout()

    def on_countdown_event(self, payload: Dict[str, Any]) -> None:
        """Slot driven by `Topics.COUNTDOWN_UPDATE` from `IntentRouter` or `Scheduler`."""
        status = str(payload.get("status", "running")).lower()
        if status in ("completed", "cancelled"):
            self.countdown_state = None
            self.countdown_panel.hide()
            self._style_dock_btn(self.btn_timer, False)
            self._refresh_orb_and_readout()
            return

        total = int(payload.get("total", self.countdown_default_sec))
        remaining = int(payload.get("remaining", total))
        label = str(payload.get("label", "Timed command"))
        detail = str(payload.get("detail", "Scheduled local automation"))
        kind = str(payload.get("kind", "notify"))
        done_msg = str(payload.get("done_message", f"Completed: {label}."))

        self._start_countdown(
            CountdownState(
                total=total,
                remaining=remaining,
                label=label,
                detail=detail,
                action="call" if kind == "call" else "notify",
                done_message=done_msg,
            )
        )

    def on_call_event(self, payload: Dict[str, Any]) -> None:
        """Slot driven by `Topics.CALL_STATE` from `IntentRouter` or `PhoneADB`."""
        status = str(payload.get("status", "ringing")).lower()
        if status in ("ended", "cancelled", "idle"):
            self.call_state = None
            self.call_backdrop.hide()
            self._style_dock_btn(self.btn_call, False)
            self._base_orb_state = "idle"
            self._refresh_orb_and_readout()
            return

        direction = str(payload.get("direction", "outgoing"))
        name = str(payload.get("name", "Mom"))
        number = str(payload.get("number", "+1 (555) 014-2290"))
        meta = str(payload.get("meta", "Mobile · via phone bridge"))

        self.call_state = CallState(
            direction=direction,
            status=status if status in ("connected", "ringing") else "ringing",
            seconds=int(payload.get("seconds", 0)),
            name=name,
            number=number,
            meta=meta,
        )
        if direction == "outgoing" and status == "ringing":
            self.show_caption("ev", f"Your call to {name} is ready.", 2600)
        self.call_card.sync_call(self.call_state)
        self.call_backdrop.show()
        self.call_backdrop.raise_()
        self._style_dock_btn(self.btn_call, True)
        self._refresh_orb_and_readout()

    def set_orb_state(self, state: str, progress: float = 0.0) -> None:
        """Slot to set the orb state ('idle' | 'listening' | 'thinking' | 'speaking' | 'acting')."""
        self._base_orb_state = state
        self._orb_progress_override = progress if progress > 0.0 else None
        self.voice_active = state in ("listening", "thinking", "speaking")
        self._style_mic_btn(self.voice_active)
        self._refresh_orb_and_readout()

    def show_caption(self, speaker: str, text: str, hold_ms: int = 0) -> None:
        """Display spoken/recognized text below the Status Orb with word-by-word reveal."""
        self.caption_speaker = speaker
        self.caption_text = text
        self.caption_born_ms = time.monotonic() * 1000.0
        self._refresh_orb_and_readout()

        if hold_ms > 0:
            captured_born = self.caption_born_ms
            self._schedule_later(
                lambda: self._clear_caption_if_match(captured_born),
                hold_ms,
            )

    def set_connection_status(self, conn_id: str, status: str) -> None:
        """Update one of the 4 top-right status dots ('local' | 'mic' | 'phone' | 'whatsapp')."""
        if conn_id in self.connections:
            self.connections[conn_id] = status
            self._refresh_connection_badges()

    # =========================================================================
    # Interactive Assistant Actions (Matches useAssistant.ts)
    # =========================================================================
    def _clear_timers(self) -> None:
        for t in self._oneshot_timers:
            t.stop()
        self._oneshot_timers.clear()

    def _schedule_later(self, fn: Callable[[], None], delay_ms: int) -> None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(fn)
        timer.start(max(1, int(delay_ms)))
        self._oneshot_timers.append(timer)

    def _clear_caption_if_match(self, born_ms: float) -> None:
        if abs(self.caption_born_ms - born_ms) < 1.0:
            self.caption_speaker = None
            self.caption_text = ""
            self._refresh_orb_and_readout()

    def _speak_simulated(self, text: str, duration_ms: int) -> None:
        self._base_orb_state = "speaking"
        self.show_caption("ev", text, duration_ms)
        self._refresh_orb_and_readout()
        self._schedule_later(
            lambda: self._reset_speaking_to_idle(),
            duration_ms,
        )

    def _reset_speaking_to_idle(self) -> None:
        if self._base_orb_state == "speaking":
            self._base_orb_state = "idle"
            self._refresh_orb_and_readout()

    def run_voice_command(self) -> None:
        """Trigger or cancel the voice command flow (delegates to SystemBridge when wired)."""
        if self._external_mic_handler is not None:
            self._external_mic_handler(self.voice_active)
            return

        if self.voice_active:
            self._clear_timers()
            self.voice_active = False
            self._style_mic_btn(False)
            self._base_orb_state = "idle"
            self.show_caption("ev", "Standing by.", 1600)
            self._refresh_orb_and_readout()
            return

        self._clear_timers()
        self.voice_active = True
        self._style_mic_btn(True)
        self._base_orb_state = "listening"
        sec = self.countdown_default_sec
        self.show_caption(
            "user",
            f"Hey E.V., open my coding workspace and prepare a call to Mom in {sec} seconds.",
        )
        self._refresh_orb_and_readout()

        self._schedule_later(lambda: self.set_orb_state("thinking"), 3600)
        self._schedule_later(
            lambda: (
                self.set_orb_state("speaking"),
                self.show_caption("ev", f"Launching your coding workspace. I'll call Mom in {sec} seconds."),
            ),
            5400,
        )
        self._schedule_later(self._finish_voice_and_chain_workspace, 8400)

    def _finish_voice_and_chain_workspace(self) -> None:
        self._base_orb_state = "idle"
        self.caption_speaker = None
        self.caption_text = ""
        self.voice_active = False
        self._style_mic_btn(False)
        self._start_workspace(chain=True)

    def launch_workspace(self) -> None:
        if self._external_workspace_launch_handler is not None:
            self._external_workspace_launch_handler()
            return
        if self.workspace_state and self.workspace_state.status in ("running", "undoing"):
            return
        self.show_caption("ev", "Launching coding workspace.", 1800)
        self._start_workspace(chain=False)

    def _start_workspace(self, chain: bool) -> None:
        self._chain_workspace_to_call = chain
        self.workspace_state = WorkspaceState(
            name="Coding workspace",
            status="running",
            completed=0,
            items=self.workspace_items,
        )
        self.workspace_panel.update_workspace(self.workspace_state)
        self.workspace_panel.show()
        self.workspace_panel.raise_()
        self._style_dock_btn(self.btn_ws, True)
        self._ws_timer.start(520)
        self._refresh_orb_and_readout()

    def _on_workspace_step(self) -> None:
        if not self.workspace_state:
            self._ws_timer.stop()
            return

        total = len(self.workspace_state.items)
        if self.workspace_state.status == "running":
            if self.workspace_state.completed >= total:
                self.workspace_state.status = "done"
                self._ws_timer.stop()
                self.workspace_panel.update_workspace(self.workspace_state)
                self._speak_simulated(f"Workspace ready. {total} items launched.", 2400)
                if self._chain_workspace_to_call:
                    self._chain_workspace_to_call = False
                    self._schedule_later(self._chain_to_call_countdown, 2400)
                self._refresh_orb_and_readout()
                return
            self.workspace_state.completed += 1
            self.workspace_panel.update_workspace(self.workspace_state)
            self._refresh_orb_and_readout()

        elif self.workspace_state.status == "undoing":
            if self.workspace_state.completed <= 0:
                self.workspace_state.status = "undone"
                self._ws_timer.stop()
                self.workspace_panel.update_workspace(self.workspace_state)
                self._speak_simulated("Workspace closed. Previous state restored.", 2400)
                self._schedule_later(self.dismiss_workspace, 1400)
                self._refresh_orb_and_readout()
                return
            self.workspace_state.completed -= 1
            self.workspace_panel.update_workspace(self.workspace_state)
            self._refresh_orb_and_readout()

    def _chain_to_call_countdown(self) -> None:
        self.dismiss_workspace()
        self._start_countdown(
            CountdownState(
                total=self.countdown_default_sec,
                remaining=self.countdown_default_sec,
                label="Call Mom",
                detail="Phone bridge · Mobile · E.V. voice routed",
                action="call",
                done_message="",
            )
        )

    def undo_workspace(self) -> None:
        if self._external_workspace_undo_handler is not None:
            self._external_workspace_undo_handler()
            return
        if not self.workspace_state:
            return
        self._chain_workspace_to_call = False
        self.workspace_state.status = "undoing"
        self.workspace_panel.update_workspace(self.workspace_state)
        self.show_caption("ev", "Reversing last workspace.", 1800)
        self._ws_timer.start(150)
        self._refresh_orb_and_readout()

    def dismiss_workspace(self) -> None:
        if self._external_workspace_cancel_handler is not None:
            self._external_workspace_cancel_handler()
        self._chain_workspace_to_call = False
        self._ws_timer.stop()
        self.workspace_state = None
        self.workspace_panel.hide()
        self._style_dock_btn(self.btn_ws, False)
        self._refresh_orb_and_readout()

    def start_quick_timer(self) -> None:
        if self._external_scheduler_start_quick_handler is not None:
            self._external_scheduler_start_quick_handler()
            return
        self._start_countdown(
            CountdownState(
                total=10,
                remaining=10,
                label="Dim desk lights to 30%",
                detail="Smart plug · Study",
                action="notify",
                done_message="Desk lights dimmed to 30%.",
            )
        )
        self.show_caption("ev", "Okay. Dim desk lights to 30% in 10 seconds.", 2200)

    def _start_countdown(self, state: CountdownState) -> None:
        self.countdown_state = state
        self.countdown_panel.update_countdown(state)
        self.countdown_panel.show()
        self.countdown_panel.raise_()
        self._style_dock_btn(self.btn_timer, True)
        self._refresh_orb_and_readout()

    def cancel_countdown(self) -> None:
        if self._external_scheduler_cancel_handler is not None:
            self._external_scheduler_cancel_handler()
            return
        self.countdown_state = None
        self.countdown_panel.hide()
        self._style_dock_btn(self.btn_timer, False)
        self.show_caption("ev", "Timed command cancelled.", 2000)
        self._refresh_orb_and_readout()

    def run_countdown_now(self) -> None:
        if self._external_scheduler_run_now_handler is not None:
            self._external_scheduler_run_now_handler()
            return
        if self.countdown_state:
            self.countdown_state.remaining = 0
            self._complete_countdown()

    def _complete_countdown(self) -> None:
        if not self.countdown_state:
            return
        done = self.countdown_state
        self.countdown_state = None
        self.countdown_panel.hide()
        self._style_dock_btn(self.btn_timer, False)
        if done.action == "call":
            self._start_call("outgoing")
        else:
            self._speak_simulated(done.done_message, 2400)
        self._refresh_orb_and_readout()

    def start_incoming_call(self) -> None:
        self._start_call("incoming")

    def _start_call(self, direction: str) -> None:
        self.call_state = CallState(
            direction=direction,
            status="ringing",
            seconds=0,
            name="Mom",
            number="+1 (555) 014-2290",
            meta="Mobile · via phone bridge",
        )
        if direction == "outgoing":
            self.show_caption("ev", "Your call to Mom is ready.", 2600)
        self.call_card.sync_call(self.call_state)
        self.call_backdrop.show()
        self.call_backdrop.raise_()
        self._style_dock_btn(self.btn_call, True)
        self._refresh_orb_and_readout()

    def accept_call(self) -> None:
        if self._external_call_accept_handler is not None:
            self._external_call_accept_handler()
        if not self.call_state:
            return
        self.call_state.status = "connected"
        self.call_state.seconds = 0
        self.call_card.sync_call(self.call_state)
        self._speak_simulated("Hi Mom, I'm connecting you now. One moment please.", 3600)

    def end_call(self) -> None:
        if self._external_call_end_handler is not None:
            self._external_call_end_handler()
        if not self.call_state:
            return
        if self.call_state.status == "connected":
            msg = f"Call ended · {format_duration(self.call_state.seconds)}"
        elif self.call_state.direction == "incoming":
            msg = "Call ignored."
        else:
            msg = "Call cancelled."
        self.call_state = None
        self.call_backdrop.hide()
        self._style_dock_btn(self.btn_call, False)
        self._base_orb_state = "idle"
        self.show_caption("ev", msg, 2200)
        self._refresh_orb_and_readout()

    def toggle_metrics_panel(self) -> None:
        vis = not self.metrics_panel.isVisible()
        if vis:
            self.metrics_panel.update_data(
                self.cpu_pct, self.ram_gb, self.battery_pct, self.cpu_history, self.ram_history
            )
            self.metrics_panel.show()
            self.metrics_panel.raise_()
        else:
            self.metrics_panel.hide()
        self._style_dock_btn(self.btn_metrics, vis)
        self._style_top_metrics_btn(vis)

    def toggle_settings_popover(self) -> None:
        vis = not self.settings_popover.isVisible()
        if vis:
            self.settings_popover.sync_state(self.settings, self._preview_orb_state)
            self.settings_popover.show()
            self.settings_popover.raise_()
        else:
            self.settings_popover.hide()
        self._style_dock_btn(self.btn_settings, vis)

    def _on_toggle_setting(self, key: str) -> None:
        if key in self.settings:
            self.settings[key] = not self.settings[key]
            self.orb_widget.minimal_ui = self.settings["minimalUi"]
            self.settings_popover.sync_state(self.settings, self._preview_orb_state)
            self._refresh_orb_and_readout()
            self.setting_changed.emit(key, self.settings[key])

    def _on_select_preview(self, preview_state: Optional[str]) -> None:
        self._preview_orb_state = preview_state
        self.settings_popover.sync_state(self.settings, self._preview_orb_state)
        self._refresh_orb_and_readout()

    # =========================================================================
    # State Derivation & Timer Loops
    # =========================================================================
    def _derived_orb_state_and_progress(self) -> tuple[str, float]:
        if self._preview_orb_state is not None:
            return self._preview_orb_state, (0.62 if self._preview_orb_state == "acting" else 0.0)

        if self._base_orb_state != "idle":
            state = self._base_orb_state
        elif self.workspace_state and self.workspace_state.status in ("running", "undoing"):
            state = "acting"
        elif self.countdown_state is not None:
            state = "acting"
        elif self.call_state and self.call_state.status == "ringing":
            state = "listening"
        else:
            state = "idle"

        if self._orb_progress_override is not None:
            prog = self._orb_progress_override
        elif self.workspace_state:
            total = max(1, len(self.workspace_state.items))
            prog = self.workspace_state.completed / float(total)
        elif self.countdown_state:
            total = max(1, self.countdown_state.total)
            prog = 1.0 - (self.countdown_state.remaining / float(total))
        else:
            prog = 0.0

        return state, max(0.0, min(1.0, prog))

    def _refresh_orb_and_readout(self) -> None:
        state, prog = self._derived_orb_state_and_progress()
        self.orb_widget.set_state(state, prog)

        label = ORB_STATE_LABELS.get(state, "Standing by").upper()
        dot_color = "#7B61FF" if state == "thinking" else "#00D4FF"
        self.lbl_orb_state.setText(f"●  {label}")
        self.lbl_orb_state.setStyleSheet(
            f"color: {dot_color}; font-family: 'JetBrains Mono', monospace; font-size: 11px; font-weight: 600; letter-spacing: 3.5px;"
        )

        if self.caption_speaker and self.caption_text:
            words = self.caption_text.split(" ")
            elapsed_s = max(0.0, (time.monotonic() * 1000.0 - self.caption_born_ms) / 1000.0)
            visible_count = max(1, min(len(words), int(elapsed_s / 0.055) + 1))
            partial = " ".join(words[:visible_count])
            if self.caption_speaker == "user":
                self.lbl_caption.setText(f"“{partial}”")
                self.lbl_caption.setStyleSheet("color: #E6F4FF; font-size: 18px; font-weight: 400;")
            else:
                self.lbl_caption.setText(partial)
                self.lbl_caption.setStyleSheet("color: #00D4FF; font-size: 18px; font-weight: 500;")
        elif state == "idle":
            hint = "Say “Hey E.V.” or tap the mic" if self.settings.get("wakeWord", True) else "Tap the mic to talk to E.V."
            self.lbl_caption.setText(hint)
            self.lbl_caption.setStyleSheet("color: #8AA4BF; font-size: 14px;")
        else:
            self.lbl_caption.setText("")

    def _on_second_tick(self) -> None:
        now = datetime.now()
        self.lbl_clock_time.setText(now.strftime("%H:%M:%S"))
        self.lbl_clock_date.setText(now.strftime("%a %d %b"))

        if self.countdown_state is not None:
            if self.countdown_state.remaining <= 0:
                self._complete_countdown()
            else:
                self.countdown_state.remaining -= 1
                if self.countdown_state.remaining <= 0:
                    self._complete_countdown()
                else:
                    self.countdown_panel.update_countdown(self.countdown_state)
                    self._refresh_orb_and_readout()

        if self.call_state is not None:
            self.call_state.seconds += 1
            self.call_card.sync_call(self.call_state)

    def _on_frame_tick(self) -> None:
        minimal = self.settings.get("minimalUi", False)
        w = float(self.width())
        h = float(self.height())

        if self.show_particles and not minimal:
            for p in self.particles:
                p.x += p.vx
                p.y += p.vy
                if p.y < -4.0:
                    p.y = h + 4.0
                if p.x < -4.0:
                    p.x = w + 4.0
                elif p.x > w + 4.0:
                    p.x = -4.0

        # Prune expired click sparks (>400ms)
        now_ms = time.monotonic() * 1000.0
        self.sparks = [s for s in self.sparks if (now_ms - s.born_ms) < 400.0]

        if self.caption_speaker and self.caption_text:
            self._refresh_orb_and_readout()

        self.orb_widget.update()
        if self.call_backdrop.isVisible():
            self.call_card.update()
        self.update()

    # =========================================================================
    # Input Events & Background HUD Painting (BackgroundField + ClickSparks)
    # =========================================================================
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self.settings.get("minimalUi", False):
            pos = event.position() if QT_VERSION == 6 else event.pos()
            self.sparks.append(SparkBurst(x=float(pos.x()), y=float(pos.y()), born_ms=time.monotonic() * 1000.0))
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        esc = Qt.Key.Key_Escape if QT_VERSION == 6 else Qt.Key_Escape
        if key == esc:
            if self.call_backdrop.isVisible():
                self.end_call()
                return
            if self.settings_popover.isVisible():
                self.toggle_settings_popover()
                return
        super().keyPressEvent(event)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(antialias_hint())

        w = int(self.width())
        h = int(self.height())
        minimal = self.settings.get("minimalUi", False)
        t = time.monotonic() - self._t0

        # 1. Base deep space fill (#0B0F19)
        painter.fillRect(0, 0, w, h, COLOR_BG)

        # 2. Minor (48px) and Major (240px) Cyan Holographic Grid
        if self.show_grid:
            minor_pen = QPen(QColor(0, 212, 255, 13), 1.0)
            painter.setPen(minor_pen)
            for x in range(0, w, 48):
                painter.drawLine(x, 0, x, h)
            for y in range(0, h, 48):
                painter.drawLine(0, y, w, y)

            major_pen = QPen(QColor(0, 212, 255, 24), 1.0)
            painter.setPen(major_pen)
            for x in range(0, w, 240):
                painter.drawLine(x, 0, x, h)
            for y in range(0, h, 240):
                painter.drawLine(0, y, w, y)

        # 3. Drifting Particle Field (cyan #00D4FF + 18% purple #7B61FF)
        if self.show_particles and not minimal:
            painter.setPen(no_pen())
            for p in self.particles:
                alpha = p.a * (0.6 + 0.4 * math.sin(t + p.tw))
                col = QColor(COLOR_PURPLE if p.purple else COLOR_CYAN)
                col.setAlphaF(max(0.02, min(1.0, alpha)))
                painter.setBrush(QBrush(col))
                painter.drawEllipse(QPointF(p.x, p.y), p.r, p.r)

        # 4. Scanlines + 9-second vertical sweep line
        if self.scanlines and not minimal:
            scan_pen = QPen(QColor(0, 0, 0, 55), 1.0)
            painter.setPen(scan_pen)
            for y in range(0, h, 4):
                painter.drawLine(0, y, w, y)

            sweep_y = int(((t % 9.0) / 9.0) * h)
            painter.setPen(QPen(QColor(0, 212, 255, 45), 1.2))
            painter.drawLine(0, sweep_y, w, sweep_y)

        # 5. Subtle radial edge vignette
        vignette = QRadialGradient(QPointF(w / 2.0, h / 2.0), max(w, h) * 0.72)
        vignette.setColorAt(0.0, QColor(0, 0, 0, 0))
        vignette.setColorAt(0.7, QColor(0, 0, 0, 60))
        vignette.setColorAt(1.0, QColor(0, 0, 0, 195))
        painter.setPen(no_pen())
        painter.setBrush(QBrush(vignette))
        painter.drawRect(0, 0, w, h)

        # 6. ClickSparks (8-ray cyan bursts)
        if self.sparks and not minimal:
            now_ms = time.monotonic() * 1000.0
            painter.setPen(no_pen())
            for sp in self.sparks:
                age = (now_ms - sp.born_ms) / 280.0
                if 0.0 <= age <= 1.0:
                    dist = age * 22.0
                    rad = max(0.5, 2.2 * (1.0 - age * 0.6))
                    c_sp = QColor(COLOR_CYAN)
                    c_sp.setAlphaF(max(0.0, 1.0 - age))
                    painter.setBrush(QBrush(c_sp))
                    for k in range(8):
                        ang = (k / 8.0) * math.tau
                        painter.drawEllipse(
                            QPointF(sp.x + math.cos(ang) * dist, sp.y + math.sin(ang) * dist),
                            rad,
                            rad,
                        )