#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Thin Holographic HUD Main Window
====================================================================
Phase 4 modular orchestrator composing:
  - `gui/status_orb.py`: `StatusOrbWidget` & theme/painter primitives
  - `gui/overlays.py`: `WorkspacePanelWidget`, `CountdownPanelWidget`,
    `MetricsOverlay`, `SettingsPopoverWidget`, `CallModalCard`, `GlassPanel`

Maintains 100% visual, behavioral, and signal/handler compatibility with
`SystemBridge` (`set_external_mic_handler`, `set_external_workspace_handlers`,
`set_external_scheduler_handlers`, `set_external_call_handlers`, and
`setting_changed`).
"""

from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from PyQt6.QtCore import QPointF, Qt, QTimer, pyqtSignal
    from PyQt6.QtGui import (
        QBrush,
        QColor,
        QKeyEvent,
        QMouseEvent,
        QPainter,
        QPen,
        QPixmap,
        QRadialGradient,
    )
    from PyQt6.QtWidgets import (
        QFrame,
        QHBoxLayout,
        QLabel,
        QMainWindow,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )
    QT_VERSION = 6
except ImportError:
    try:
        from PySide6.QtCore import QPointF, Qt, QTimer, Signal as pyqtSignal  # type: ignore
        from PySide6.QtGui import (  # type: ignore
            QBrush,
            QColor,
            QKeyEvent,
            QMouseEvent,
            QPainter,
            QPen,
            QRadialGradient,
        )
        from PySide6.QtWidgets import (  # type: ignore
            QFrame,
            QHBoxLayout,
            QLabel,
            QMainWindow,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )
        QT_VERSION = 6
    except ImportError:
        from PyQt5.QtCore import QPointF, Qt, QTimer, pyqtSignal  # type: ignore
        from PyQt5.QtGui import (  # type: ignore
            QBrush,
            QColor,
            QKeyEvent,
            QMouseEvent,
            QPainter,
            QPen,
            QRadialGradient,
        )
        from PyQt5.QtWidgets import (  # type: ignore
            QFrame,
            QHBoxLayout,
            QLabel,
            QMainWindow,
            QPushButton,
            QVBoxLayout,
            QWidget,
        )
        QT_VERSION = 5

from gui.overlays import (
    DEFAULT_WORKSPACE_ITEMS,
    CallModalCard,
    CallState,
    CountdownPanelWidget,
    CountdownState,
    MetricsOverlay,
    Particle,
    SettingsPopoverWidget,
    SparkBurst,
    WorkspacePanelWidget,
    WorkspaceState,
)
from gui.status_orb import (
    COLOR_BG,
    COLOR_CYAN,
    COLOR_PURPLE,
    ORB_STATE_LABELS,
    StatusOrbWidget,
    align_center,
    antialias_hint,
    format_duration,
    no_pen,
    pointing_hand_cursor,
)


class EVMainWindow(QMainWindow):
    """
    Thin PyQt HUD Main Window orchestrating `StatusOrbWidget`, `Overlays`,
    background grid/particles, top status bar, bottom dock, and `SystemBridge` slots.
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

        self._base_orb_state: str = "idle"
        self._preview_orb_state: Optional[str] = None
        self._orb_progress_override: Optional[float] = None
        self.voice_active: bool = False

        # External backend handlers wired by SystemBridge in main.py
        self._external_mic_handler: Optional[Callable[[bool], None]] = None
        self._external_workspace_launch_handler: Optional[Callable[[], None]] = None
        self._external_workspace_undo_handler: Optional[Callable[[], None]] = None
        self._external_workspace_cancel_handler: Optional[Callable[[], None]] = None
        self._external_timer_start_handler: Optional[Callable[[], None]] = None
        self._external_timer_cancel_handler: Optional[Callable[[], None]] = None
        self._external_timer_run_now_handler: Optional[Callable[[], None]] = None
        self._external_call_accept_handler: Optional[Callable[[], None]] = None
        self._external_call_end_handler: Optional[Callable[[], None]] = None

        self.caption_speaker: Optional[str] = None
        self.caption_text: str = ""
        self.caption_born_ms: float = 0.0

        self.workspace_items = self._load_workspace_items()
        self.workspace_state: Optional[WorkspaceState] = None
        self.countdown_state: Optional[CountdownState] = None
        self.call_state: Optional[CallState] = None

        self.connections: Dict[str, str] = {
            "local": "online",
            "mic": "online",
            "phone": "online",
            "whatsapp": "standby",
        }

        self.cpu_pct: int = 14
        self.ram_gb: float = 2.4
        self.battery_pct: int = 86
        self.cpu_history: List[float] = [12.0 + random.random() * 6.0 for _ in range(28)]
        self.ram_history: List[float] = [2.3 + random.random() * 0.12 for _ in range(28)]

        self._t0 = time.monotonic()
        self._bg_cache: Optional[QPixmap] = None
        self.particles: List[Particle] = []
        self.sparks: List[SparkBurst] = []
        self._init_particles(int(gui_cfg.get("particle_density", 70)))
        self._oneshot_timers: List[QTimer] = []

        self._build_ui()

        fps = max(15, min(60, int(gui_cfg.get("fps", 30))))
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._on_frame_tick)
        self._anim_timer.start(int(1000 / fps))

        self._sec_timer = QTimer(self)
        self._sec_timer.timeout.connect(self._on_second_tick)
        self._sec_timer.start(1000)

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

        # 1. Top Status Bar
        self.top_bar = QFrame(self.canvas)
        self.top_bar.setFixedHeight(56)
        self.top_bar.setStyleSheet(
            "QFrame { background-color: rgba(11, 15, 25, 165); border-bottom: 1px solid rgba(0, 212, 255, 28); }"
        )
        top_layout = QHBoxLayout(self.top_bar)
        top_layout.setContentsMargins(24, 0, 24, 0)

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

        right_row = QHBoxLayout()
        right_row.setSpacing(10)
        self.btn_top_metrics = QPushButton("CPU 14%   RAM 2.4/4.0 GB")
        self.btn_top_metrics.setFixedHeight(32)
        self.btn_top_metrics.setCursor(pointing_hand_cursor())
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
        self.orb_widget.minimal_ui = self.settings.get("minimalUi", False)
        orb_row.addWidget(self.orb_widget)
        orb_row.addStretch()
        center_area.addLayout(orb_row, 4)

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

        # 3. Floating Overlay Panels from gui/overlays.py
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

        self.btn_mic = QPushButton("🎙")
        self.btn_mic.setFixedSize(54, 54)
        self.btn_mic.setCursor(pointing_hand_cursor())
        self.btn_mic.clicked.connect(self.run_voice_command)
        self._style_mic_btn(False)
        dock_layout.addWidget(self.btn_mic)

        dock_layout.addWidget(self._dock_sep())

        self.btn_metrics = self._make_dock_btn("∿  Metrics", self.toggle_metrics_panel)
        self.btn_settings = self._make_dock_btn("⚙", self.toggle_settings_popover, width=42)
        dock_layout.addWidget(self.btn_metrics)
        dock_layout.addWidget(self.btn_settings)

        # 5. Full-window Call Overlay Backdrop
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
        btn.setCursor(pointing_hand_cursor())
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

        ws_h = 470
        self.workspace_panel.setFixedHeight(ws_h)
        self.workspace_panel.move(24, max(72, (h - ws_h) // 2))

        cd_w = self.countdown_panel.width()
        cd_h = self.countdown_panel.height()
        self.countdown_panel.move(max(24, w - cd_w - 24), max(72, (h - cd_h) // 2))

        mp_w = self.metrics_panel.width()
        self.metrics_panel.move(max(16, w - mp_w - 24), 68)

        dock_w = self.control_dock.width()
        dock_h = self.control_dock.height()
        dock_x = (w - dock_w) // 2
        dock_y = h - dock_h - 20
        self.control_dock.move(dock_x, dock_y)

        sp_w = self.settings_popover.width()
        sp_h = self.settings_popover.height()
        self.settings_popover.move(
            min(w - sp_w - 16, dock_x + dock_w - sp_w),
            max(68, dock_y - sp_h - 14),
        )

        self.call_backdrop.setGeometry(0, 0, w, h)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._bg_cache = None
        self._reposition_overlays()

    # =========================================================================
    # External Handler Registration & EventBus Slots
    # =========================================================================
    def set_external_mic_handler(self, handler: Callable[[bool], None]) -> None:
        self._external_mic_handler = handler

    def set_external_workspace_handlers(
        self,
        launch_fn: Callable[[], None],
        undo_fn: Callable[[], None],
        cancel_fn: Callable[[], None],
    ) -> None:
        self._external_workspace_launch_handler = launch_fn
        self._external_workspace_undo_handler = undo_fn
        self._external_workspace_cancel_handler = cancel_fn

    def set_external_scheduler_handlers(
        self,
        start_quick_fn: Callable[[], None],
        cancel_fn: Callable[[], None],
        run_now_fn: Callable[[], None],
    ) -> None:
        self._external_timer_start_handler = start_quick_fn
        self._external_timer_cancel_handler = cancel_fn
        self._external_timer_run_now_handler = run_now_fn

    def set_external_call_handlers(
        self,
        accept_fn: Callable[[], None],
        end_fn: Callable[[], None],
    ) -> None:
        self._external_call_accept_handler = accept_fn
        self._external_call_end_handler = end_fn

    def update_system_metrics(self, cpu_pct: int, ram_gb: float, battery_pct: int) -> None:
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

    def set_orb_state(self, state: str, progress: float = 0.0) -> None:
        self._base_orb_state = state
        self._orb_progress_override = progress if progress > 0.0 else None
        self.voice_active = state in ("listening", "thinking", "speaking")
        self._style_mic_btn(self.voice_active)
        self._refresh_orb_and_readout()

    def show_caption(self, speaker: str, text: str, hold_ms: int = 0) -> None:
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
        if conn_id in self.connections:
            self.connections[conn_id] = status
            self._refresh_connection_badges()

    def on_workspace_progress_event(self, payload: Dict[str, Any]) -> None:
        name = str(payload.get("name", "Coding workspace"))
        status = str(payload.get("status", "running"))
        completed = int(payload.get("completed", 0))
        items = payload.get("items") or self.workspace_items

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
        status = str(payload.get("status", "running"))
        if status in ("completed", "cancelled"):
            self.countdown_state = None
            self.countdown_panel.hide()
            self._style_dock_btn(self.btn_timer, False)
            self._refresh_orb_and_readout()
            return

        total = max(1, int(payload.get("total", self.countdown_default_sec)))
        remaining = max(0, int(payload.get("remaining", total)))
        label = str(payload.get("label", "Timed command"))
        detail = str(payload.get("detail", "Scheduled local automation"))
        kind = str(payload.get("kind", "notify"))
        done_msg = str(payload.get("done_message", f"Completed: {label}."))

        self.countdown_state = CountdownState(
            total=total,
            remaining=remaining,
            label=label,
            detail=detail,
            action="call" if kind == "call" else "notify",
            done_message=done_msg,
        )
        self.countdown_panel.update_countdown(self.countdown_state)
        self.countdown_panel.show()
        self.countdown_panel.raise_()
        self._style_dock_btn(self.btn_timer, True)
        self._refresh_orb_and_readout()

    def on_call_event(self, payload: Dict[str, Any]) -> None:
        direction = str(payload.get("direction", "outgoing"))
        name = str(payload.get("name", "Mom"))
        number = str(payload.get("number", "+1 (555) 014-2290"))
        meta = str(payload.get("meta", "Mobile · via phone bridge"))
        self.call_state = CallState(
            direction=direction,
            status="ringing",
            seconds=0,
            name=name,
            number=number,
            meta=meta,
        )
        self.call_card.sync_call(self.call_state)
        self.call_backdrop.show()
        self.call_backdrop.raise_()
        self._style_dock_btn(self.btn_call, True)
        self._refresh_orb_and_readout()

    # =========================================================================
    # Interactive Control Actions
    # =========================================================================
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

    def run_voice_command(self) -> None:
        if self._external_mic_handler is not None:
            self._external_mic_handler(self.voice_active)

    def launch_workspace(self) -> None:
        if self._external_workspace_launch_handler is not None:
            self._external_workspace_launch_handler()

    def undo_workspace(self) -> None:
        if self._external_workspace_undo_handler is not None:
            self._external_workspace_undo_handler()

    def dismiss_workspace(self) -> None:
        if self._external_workspace_cancel_handler is not None:
            self._external_workspace_cancel_handler()
        self.workspace_state = None
        self.workspace_panel.hide()
        self._style_dock_btn(self.btn_ws, False)
        self._refresh_orb_and_readout()

    def start_quick_timer(self) -> None:
        if self._external_timer_start_handler is not None:
            self._external_timer_start_handler()

    def cancel_countdown(self) -> None:
        if self._external_timer_cancel_handler is not None:
            self._external_timer_cancel_handler()

    def run_countdown_now(self) -> None:
        if self._external_timer_run_now_handler is not None:
            self._external_timer_run_now_handler()

    def start_incoming_call(self) -> None:
        self.on_call_event(
            {
                "direction": "incoming",
                "name": "Mom",
                "number": "+1 (555) 014-2290",
                "meta": "Mobile · via phone bridge",
            }
        )

    def accept_call(self) -> None:
        if not self.call_state:
            return
        self.call_state.status = "connected"
        self.call_state.seconds = 0
        self.call_card.sync_call(self.call_state)
        if self._external_call_accept_handler is not None:
            self._external_call_accept_handler()

    def end_call(self) -> None:
        if not self.call_state:
            return
        if self._external_call_end_handler is not None:
            self._external_call_end_handler()
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
            self._bg_cache = None
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

        now_ms = time.monotonic() * 1000.0
        self.sparks = [s for s in self.sparks if (now_ms - s.born_ms) < 400.0]

        if self.caption_speaker and self.caption_text:
            self._refresh_orb_and_readout()

        self.orb_widget.update()
        if self.call_backdrop.isVisible():
            self.call_card.update()
        self.update()

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

    def _ensure_background_cache(self, w: int, h: int, minimal: bool) -> QPixmap:
        """Render the static holographic grid, CRT scanlines, and vignette into a cached QPixmap once per resize."""
        if (
            self._bg_cache is not None
            and self._bg_cache.width() == w
            and self._bg_cache.height() == h
        ):
            return self._bg_cache

        pix = QPixmap(max(1, w), max(1, h))
        pix.fill(COLOR_BG)
        bp = QPainter(pix)

        if self.show_grid:
            bp.setPen(QPen(QColor(0, 212, 255, 13), 1.0))
            for x in range(0, w, 48):
                bp.drawLine(x, 0, x, h)
            for y in range(0, h, 48):
                bp.drawLine(0, y, w, y)

            bp.setPen(QPen(QColor(0, 212, 255, 24), 1.0))
            for x in range(0, w, 240):
                bp.drawLine(x, 0, x, h)
            for y in range(0, h, 240):
                bp.drawLine(0, y, w, y)

        if self.scanlines and not minimal:
            bp.setPen(QPen(QColor(0, 0, 0, 50), 1.0))
            for y in range(0, h, 4):
                bp.drawLine(0, y, w, y)

        vignette = QRadialGradient(QPointF(w / 2.0, h / 2.0), max(w, h) * 0.72)
        vignette.setColorAt(0.0, QColor(0, 0, 0, 0))
        vignette.setColorAt(0.7, QColor(0, 0, 0, 60))
        vignette.setColorAt(1.0, QColor(0, 0, 0, 195))
        bp.setPen(no_pen())
        bp.setBrush(QBrush(vignette))
        bp.drawRect(0, 0, w, h)
        bp.end()

        self._bg_cache = pix
        return pix

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(antialias_hint())

        w = int(self.width())
        h = int(self.height())
        minimal = self.settings.get("minimalUi", False)
        t = time.monotonic() - self._t0

        # 1. Blit pre-rendered static background (grid + scanlines + radial vignette) in O(1)
        bg_pix = self._ensure_background_cache(w, h, minimal)
        painter.drawPixmap(0, 0, bg_pix)

        # 2. Draw drifting particles
        if self.show_particles and not minimal:
            painter.setPen(no_pen())
            for p in self.particles:
                alpha = p.a * (0.6 + 0.4 * math.sin(t + p.tw))
                col = QColor(COLOR_PURPLE if p.purple else COLOR_CYAN)
                col.setAlphaF(max(0.02, min(1.0, alpha)))
                painter.setBrush(QBrush(col))
                painter.drawEllipse(QPointF(p.x, p.y), p.r, p.r)

        # 3. Single moving sweep line
        if self.scanlines and not minimal:
            sweep_y = int(((t % 9.0) / 9.0) * h)
            painter.setPen(QPen(QColor(0, 212, 255, 45), 1.2))
            painter.drawLine(0, sweep_y, w, sweep_y)

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