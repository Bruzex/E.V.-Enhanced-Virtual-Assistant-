#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Holographic Status Orb & Theme Primitives
=============================================================================
Extracted from `gui/main_window.py` in Phase 4 for clean modular architecture.

Contains:
  - MagicPatterns Holographic HUD color constants & Qt5/Qt6 painter helpers
  - `ORB_STATE_LABELS` mapping (`idle`, `listening`, `thinking`, `speaking`, `acting`)
  - `StatusOrbWidget`: Custom `QPainter` implementation of `StatusOrb.tsx`,
    `SpeakingBars.tsx`, and `ThinkingSwirl.tsx`:
      * Ambient radial aura glow
      * 90-tick outer ring rotating 360° every 90s
      * Counter-rotating dashed ring (r=168)
      * Segmented arc ring (r=158)
      * Circular progress arc (r=150) for `acting` state
      * State-specific animations:
          - `idle`: Slow cyan pulse (4s period)
          - `listening`: 3 expanding sonar rings
          - `thinking`: 3 counter-rotating particle orbits (`ThinkingSwirl`)
          - `speaking`: 72 radial audio waveform bars (`SpeakingBars`)
          - `acting`: Sharp ice-cyan pulses + progress arc
      * Clipped hexagonal lattice core (r=118) with vertical scanline sweep
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

try:
    from PyQt6.QtCore import QPointF, QRectF, Qt
    from PyQt6.QtGui import (
        QBrush,
        QColor,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPolygonF,
        QRadialGradient,
    )
    from PyQt6.QtWidgets import QSizePolicy, QWidget
    QT_VERSION = 6
except ImportError:
    from PyQt5.QtCore import QPointF, QRectF, Qt  # type: ignore
    from PyQt5.QtGui import (  # type: ignore
        QBrush,
        QColor,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPolygonF,
        QRadialGradient,
    )
    from PyQt5.QtWidgets import QSizePolicy, QWidget  # type: ignore
    QT_VERSION = 5


# =============================================================================
# Theme Constants (Exact MagicPatterns Tailwind Palette)
# =============================================================================
COLOR_BG = QColor("#0B0F19")
COLOR_DEEP = QColor("#060912")
COLOR_PANEL = QColor(13, 20, 34, 195)        # #0D1422 / 75% alpha
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


# =============================================================================
# Cross-Qt (PyQt6 / PyQt5) Painter & Alignment Helpers
# =============================================================================
def format_duration(total_seconds: int) -> str:
    m = max(0, int(total_seconds)) // 60
    s = max(0, int(total_seconds)) % 60
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


def no_brush() -> Any:
    return Qt.BrushStyle.NoBrush if QT_VERSION == 6 else Qt.NoBrush


def custom_dash_line() -> Any:
    return Qt.PenStyle.CustomDashLine if QT_VERSION == 6 else Qt.CustomDashLine


def pointing_hand_cursor() -> Any:
    return Qt.CursorShape.PointingHandCursor if QT_VERSION == 6 else Qt.PointingHandCursor


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
        """Update the active orb state and optional progress arc (0.0 to 1.0)."""
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
        aura_grad.setColorAt(
            0.65,
            QColor(core_color.red(), core_color.green(), core_color.blue(), aura_alpha // 2),
        )
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
        painter.setBrush(no_brush())
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

        if not self.minimal_ui:
            scan_y = -118.0 + ((t % 3.5) / 3.5) * 236.0
            scan_grad = QLinearGradient(QPointF(-118.0, scan_y), QPointF(118.0, scan_y))
            scan_grad.setColorAt(0.0, QColor(0, 212, 255, 0))
            scan_grad.setColorAt(0.5, QColor(0, 212, 255, 95))
            scan_grad.setColorAt(1.0, QColor(0, 212, 255, 0))
            painter.setPen(QPen(QBrush(scan_grad), 2.0))
            painter.drawLine(QPointF(-118.0, scan_y), QPointF(118.0, scan_y))

        painter.restore()

        inner_dash = QPen(QColor(0, 212, 255, 64), 1.0)
        inner_dash.setStyle(custom_dash_line())
        inner_dash.setDashPattern([1.5, 4.5])
        painter.setPen(inner_dash)
        painter.setBrush(no_brush())
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
        bloom_grad.setColorAt(
            0.55,
            QColor(core_color.red(), core_color.green(), core_color.blue(), core_alpha // 2),
        )
        bloom_grad.setColorAt(1.0, c_edge)

        painter.setPen(no_pen())
        painter.setBrush(QBrush(bloom_grad))
        painter.drawEllipse(QPointF(0, 0), bloom_r, bloom_r)

        soft_r = 32.0 * pulse
        soft_grad = QRadialGradient(QPointF(0, 0), soft_r)
        soft_grad.setColorAt(
            0.0,
            QColor(core_color.red(), core_color.green(), core_color.blue(), min(255, core_alpha + 25)),
        )
        soft_grad.setColorAt(1.0, c_edge)
        painter.setBrush(QBrush(soft_grad))
        painter.drawEllipse(QPointF(0, 0), soft_r, soft_r)

        nucleus_grad = QRadialGradient(QPointF(0, 0), 14.0)
        nucleus_grad.setColorAt(0.0, QColor(233, 252, 255, 250))
        nucleus_grad.setColorAt(0.75, QColor(233, 252, 255, 180))
        nucleus_grad.setColorAt(1.0, QColor(0, 212, 255, 0))
        painter.setBrush(QBrush(nucleus_grad))
        painter.drawEllipse(QPointF(0, 0), 14.0, 14.0)

        painter.restore()