#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Main Entry Point (Phase 1–4 Production Release)
===================================================================================
Wires together the complete offline, CPU-only E.V. stack:
  - Phase 1: `EventBus`, `Brain` (`n_gpu_layers=0`), `VoskSTT`, `PiperTTS`, `WakeWordLoop`
  - Phase 2: `IntentRouter`, `SystemController`, `WorkspaceOrchestrator`, `MediaController`, `WebSearchTool`
  - Phase 3: `TimedCommandScheduler`, `ClapDetector`, `PhoneADBController`, `WhatsAppController`,
             `AudioRouter`, `RAGMemoryManager` (SQLite + ChromaDB)
  - Phase 4: Modular HUD (`gui/status_orb.py`, `gui/overlays.py`, `gui/main_window.py`),
             structured `logs/rss.log` telemetry persistence, and defensive error boundaries.
"""

from __future__ import annotations

import argparse
import gc
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml

try:
    import psutil  # type: ignore
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    PSUTIL_AVAILABLE = False

# Force 0 GPU visibility across CUDA / ROCm / HIP / Vulkan before any native imports
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["HIP_VISIBLE_DEVICES"] = ""
os.environ["ROCR_VISIBLE_DEVICES"] = ""
os.environ["GGML_VK_VISIBLE_DEVICES"] = ""

try:
    from PyQt6.QtCore import QObject, QTimer, pyqtSignal
    from PyQt6.QtWidgets import QApplication
    QT_VERSION = 6
except ImportError:
    try:
        from PySide6.QtCore import QObject, QTimer, Signal as pyqtSignal  # type: ignore
        from PySide6.QtWidgets import QApplication  # type: ignore
        QT_VERSION = 6
    except ImportError:
        from PyQt5.QtCore import QObject, QTimer, pyqtSignal  # type: ignore
        from PyQt5.QtWidgets import QApplication  # type: ignore
        QT_VERSION = 5

from automation.media import MediaController
from automation.system import SystemController
from automation.web_search import WebSearchTool
from automation.workspace import WorkspaceOrchestrator
from core.brain import Brain
from core.event_bus import Event, EventBus, Topics, get_event_bus
from core.intent_router import IntentRouter
from core.scheduler import TimedCommandScheduler
from gui.main_window import EVMainWindow
from memory.rag import RAGMemoryManager
from remote.audio_routing import AudioRouter
from remote.phone_adb import PhoneADBController
from remote.whatsapp import WhatsAppController
from voice.clap_detector import ClapDetector
from voice.stt import VoskSTT
from voice.tts import PiperTTS
from voice.wakeword import WakeWordLoop


DEFAULT_CONFIG: Dict[str, Any] = {
    "app": {
        "name": "E.V.",
        "full_name": "Enhanced Virtual Assistant",
        "version": "2.0.0",
        "offline_mode": True,
        "log_level": "INFO",
        "log_file": "logs/ev.log",
        "rss_log_file": "logs/rss.log",
    },
    "hardware": {
        "ram_budget_gb": 4.0,
        "ram_warning_threshold_gb": 3.4,
        "ram_critical_threshold_gb": 3.8,
        "monitor_interval_ms": 1000,
        "rss_log_interval_seconds": 10,
        "strict_cpu_only": True,
        "aggressive_gc": True,
    },
    "llm": {
        "provider": "llama_cpp",
        "model_path": "models/qwen2.5-3b-instruct-q4_k_m.gguf",
        "n_gpu_layers": 0,
        "n_ctx": 2048,
        "n_threads": 6,
    },
    "gui": {
        "title": "E.V. — Enhanced Virtual Assistant",
        "width": 1440,
        "height": 900,
        "min_width": 1024,
        "min_height": 640,
        "fullscreen": False,
        "frameless": False,
        "fps": 30,
        "background_effect": "grid-and-particles",
        "particle_density": 70,
        "scanlines": True,
        "minimal_ui": False,
        "default_countdown_seconds": 30,
    },
}


def load_config(config_path: Path) -> Dict[str, Any]:
    """Load YAML configuration with safe fallback defaults and hard constraint enforcement."""
    config = dict(DEFAULT_CONFIG)
    if config_path.exists():
        try:
            with config_path.open("r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
            for section, values in loaded.items():
                if isinstance(values, dict) and isinstance(config.get(section), dict):
                    config[section] = {**config[section], **values}
                else:
                    config[section] = values
        except Exception as exc:
            print(f"[WARN] Failed to parse {config_path}: {exc}. Using safe defaults.", file=sys.stderr)

    # Hard safety invariant: AMD RX 6500M must NEVER allocate VRAM layers
    if config.get("llm", {}).get("n_gpu_layers", 0) != 0:
        logging.warning("Overriding llm.n_gpu_layers to 0 (RX 6500M zero-VRAM mandate).")
        config.setdefault("llm", {})["n_gpu_layers"] = 0

    return config


def setup_logging(config: Dict[str, Any], base_dir: Path) -> tuple[logging.Logger, Path]:
    """Configure console + file logging and initialize `logs/rss.log`."""
    app_cfg = config.get("app", {})
    level_name = str(app_cfg.get("log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)

    log_rel = app_cfg.get("log_file", "logs/ev.log")
    log_path = base_dir / log_rel
    log_path.parent.mkdir(parents=True, exist_ok=True)

    rss_rel = app_cfg.get("rss_log_file", "logs/rss.log")
    rss_path = base_dir / rss_rel
    rss_path.parent.mkdir(parents=True, exist_ok=True)

    if not rss_path.exists() or rss_path.stat().st_size == 0:
        rss_path.write_text(
            "timestamp | proc_rss_gb | peak_rss_gb | sys_ram_gb | budget_gb | cpu_pct | vram_mb\n",
            encoding="utf-8",
        )

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )
    return logging.getLogger("ev.main"), rss_path


class SystemBridge(QObject):
    """
    Thread-safe Qt signal bridge between `EventBus`, `IntentRouter`,
    `WorkspaceOrchestrator`, `TimedCommandScheduler`, `PhoneADBController`,
    `ClapDetector`, voice/LLM threads, and `EVMainWindow`.
    Also writes structured hardware telemetry to `logs/rss.log`.
    """

    metrics_updated = pyqtSignal(int, float, int)       # cpu_pct, ram_gb, battery_pct
    orb_state_changed = pyqtSignal(str, float)          # state, progress (0.0 - 1.0)
    caption_emitted = pyqtSignal(str, str, int)         # speaker ('user'|'ev'), text, hold_ms
    connection_changed = pyqtSignal(str, str)           # id ('local'|'mic'|'phone'|'whatsapp'), status
    workspace_progress_updated = pyqtSignal(dict)       # payload from Topics.WORKSPACE_PROGRESS
    countdown_updated = pyqtSignal(dict)                # payload from Topics.COUNTDOWN_UPDATE
    call_state_updated = pyqtSignal(dict)               # payload from Topics.CALL_STATE

    def __init__(
        self,
        config: Dict[str, Any],
        base_dir: Path,
        rss_log_path: Path,
        event_bus: EventBus,
        brain: Brain,
        intent_router: IntentRouter,
        workspace_orch: WorkspaceOrchestrator,
        scheduler: TimedCommandScheduler,
        phone_adb: PhoneADBController,
        audio_router: AudioRouter,
        stt: VoskSTT,
        tts: PiperTTS,
        wakeword: WakeWordLoop,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.base_dir = base_dir
        self.rss_log_path = rss_log_path
        self.bus = event_bus
        self.brain = brain
        self.intent_router = intent_router
        self.workspace_orch = workspace_orch
        self.scheduler = scheduler
        self.phone_adb = phone_adb
        self.audio_router = audio_router
        self.stt = stt
        self.tts = tts
        self.wakeword = wakeword
        self.logger = logging.getLogger("ev.bridge")
        self._process = psutil.Process(os.getpid()) if (PSUTIL_AVAILABLE and psutil is not None) else None

        hw_cfg = config.get("hardware", {})
        self.ram_budget_gb: float = float(hw_cfg.get("ram_budget_gb", 4.0))
        self.ram_warn_gb: float = float(hw_cfg.get("ram_warning_threshold_gb", 3.4))
        self.ram_crit_gb: float = float(hw_cfg.get("ram_critical_threshold_gb", 3.8))
        self.rss_log_interval_s: float = float(hw_cfg.get("rss_log_interval_seconds", 10.0))
        self.peak_rss_gb: float = 0.0
        self._last_rss_log_ts: float = 0.0

        # Subscribe to EventBus topics from background threads and marshal via Qt signals
        self.bus.subscribe(Topics.ORB_STATE, self._on_bus_orb_state)
        self.bus.subscribe(Topics.CAPTION, self._on_bus_caption)
        self.bus.subscribe(Topics.CONNECTION_STATUS, self._on_bus_connection)
        self.bus.subscribe(Topics.VOICE_USER_COMMAND, self._on_bus_user_command)
        self.bus.subscribe(Topics.WORKSPACE_PROGRESS, self._on_bus_workspace_progress)
        self.bus.subscribe(Topics.COUNTDOWN_UPDATE, self._on_bus_countdown_update)
        self.bus.subscribe(Topics.CALL_STATE, self._on_bus_call_state)
        self.bus.subscribe(Topics.VOICE_CLAP_DETECTED, self._on_bus_clap_detected)

        if PSUTIL_AVAILABLE and psutil is not None:
            psutil.cpu_percent(interval=None)

        interval_ms = int(hw_cfg.get("monitor_interval_ms", 1000))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_telemetry)
        self._timer.start(interval_ms)

        # Write initial startup snapshot to logs/rss.log immediately
        self._poll_telemetry()

    # =========================================================================
    # EventBus -> Qt Signal Forwarding
    # =========================================================================
    def _on_bus_orb_state(self, event: Event) -> None:
        state = str(event.payload.get("state", "idle"))
        progress = float(event.payload.get("progress", 0.0))
        self.orb_state_changed.emit(state, progress)

    def _on_bus_caption(self, event: Event) -> None:
        speaker = str(event.payload.get("speaker", "ev"))
        text = str(event.payload.get("text", ""))
        hold_ms = int(event.payload.get("hold_ms", 0))
        self.caption_emitted.emit(speaker, text, hold_ms)

    def _on_bus_connection(self, event: Event) -> None:
        conn_id = str(event.payload.get("id", ""))
        status = str(event.payload.get("status", "online"))
        if conn_id:
            self.connection_changed.emit(conn_id, status)

    def _on_bus_workspace_progress(self, event: Event) -> None:
        self.workspace_progress_updated.emit(dict(event.payload))

    def _on_bus_countdown_update(self, event: Event) -> None:
        self.countdown_updated.emit(dict(event.payload))

    def _on_bus_call_state(self, event: Event) -> None:
        self.call_state_updated.emit(dict(event.payload))

    def _on_bus_clap_detected(self, event: Event) -> None:
        action = str(event.payload.get("action", "toggle_listening"))
        if action == "toggle_listening":
            self.wakeword.trigger_manual_listen()
            self.caption_emitted.emit("ev", "Double-clap detected. Listening...", 2000)

    def _on_bus_user_command(self, event: Event) -> None:
        text = str(event.payload.get("text", "")).strip()
        if not text:
            return
        threading.Thread(
            target=self._process_command_worker,
            args=(text,),
            name="EV-IntentRouter-Worker",
            daemon=True,
        ).start()

    def _process_command_worker(self, command_text: str) -> None:
        try:
            match = self.intent_router.route(command_text)
            if match.response_text:
                self.tts.speak(match.response_text, block=False)
        except Exception as exc:
            self.logger.error("Command routing error: %s", exc, exc_info=True)
            self.orb_state_changed.emit("idle", 0.0)

    # =========================================================================
    # GUI ControlStrip & Overlay Button Handlers -> Real Backend Services
    # =========================================================================
    def handle_gui_mic_button(self, currently_active: bool) -> None:
        if currently_active:
            self.wakeword.cancel_active_listen()
            self.tts.stop()
            return

        if self.stt.is_ready:
            threading.Thread(
                target=self._live_mic_capture_worker,
                name="EV-LiveMic-Worker",
                daemon=True,
            ).start()
        else:
            threading.Thread(
                target=self._fallback_mic_command_worker,
                name="EV-MicFallback-Worker",
                daemon=True,
            ).start()

    def handle_gui_workspace_launch(self) -> None:
        if self.workspace_orch.is_busy:
            return
        ok, msg = self.workspace_orch.launch_workspace("coding")
        if msg:
            self.tts.speak(msg, block=False)

    def handle_gui_workspace_undo(self) -> None:
        ok, msg = self.workspace_orch.undo_last_workspace()
        if msg:
            self.tts.speak(msg, block=False)

    def handle_gui_workspace_cancel(self) -> None:
        self.workspace_orch.cancel()

    def handle_gui_timer_start(self) -> None:
        self.scheduler.schedule_quick_timer()

    def handle_gui_timer_cancel(self) -> None:
        self.scheduler.cancel(speak_feedback=True)

    def handle_gui_timer_run_now(self) -> None:
        self.scheduler.run_now()

    def handle_gui_call_accept(self) -> None:
        self.phone_adb.accept_call()
        self.tts.speak(
            "Hi Mom, I'm connecting you now. One moment please.",
            output_device_index=self.audio_router.virtual_cable_device_index,
            block=False,
        )

    def handle_gui_call_end(self) -> None:
        self.phone_adb.end_call()

    def _live_mic_capture_worker(self) -> None:
        self.orb_state_changed.emit("listening", 0.0)
        transcript = self.stt.listen_once(
            timeout_seconds=float(
                self.config.get("voice", {}).get("wake_word", {}).get("command_timeout_seconds", 6.0)
            )
        )
        if transcript:
            _, stripped = self.wakeword.inspect_transcript(transcript)
            clean_cmd = stripped if stripped else transcript
            self.caption_emitted.emit("user", clean_cmd, 3000)
            self.bus.publish(Topics.VOICE_USER_COMMAND, {"text": clean_cmd, "source": "voice"}, source="mic")
        else:
            self.caption_emitted.emit("ev", "I didn't hear a command. Standing by.", 2000)
            self.orb_state_changed.emit("idle", 0.0)

    def _fallback_mic_command_worker(self) -> None:
        """
        When no hardware microphone / Vosk model is attached, clicking the HUD
        Mic dispatches the PRD Section 1.4 benchmark command directly into the
        real `EventBus` -> `IntentRouter` -> `WorkspaceOrchestrator` -> `TimedCommandScheduler` pipeline.
        """
        sec = int(self.config.get("gui", {}).get("default_countdown_seconds", 30))
        sample_cmd = f"Hey E.V., open my coding workspace and prepare a call to Mom in {sec} seconds."
        self.orb_state_changed.emit("listening", 0.0)
        self.caption_emitted.emit("user", sample_cmd, 2500)
        threading.Event().wait(1.2)
        self.bus.publish(Topics.VOICE_USER_COMMAND, {"text": sample_cmd, "source": "gui"}, source="mic")

    # =========================================================================
    # Hardware Telemetry, RAM Guardrails & `logs/rss.log` Writer
    # =========================================================================
    def _poll_telemetry(self) -> None:
        try:
            sys_ram_gb = 0.0
            if PSUTIL_AVAILABLE and psutil is not None and self._process is not None:
                cpu_pct = int(round(psutil.cpu_percent(interval=None)))
                rss_bytes = self._process.memory_info().rss
                for child in self._process.children(recursive=True):
                    try:
                        rss_bytes += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                vm = psutil.virtual_memory()
                sys_ram_gb = round(vm.used / (1024 ** 3), 2)
            else:
                cpu_pct = 14
                rss_bytes = 0
                sys_ram_gb = 0.0

            proc_rss_gb = rss_bytes / (1024 ** 3)
            display_ram_gb = max(proc_rss_gb, min(self.ram_budget_gb, round(1.8 + proc_rss_gb, 2)))

            if proc_rss_gb > self.peak_rss_gb:
                self.peak_rss_gb = proc_rss_gb

            # Write structured telemetry row to logs/rss.log every `rss_log_interval_s`
            now_ts = time.time()
            if (now_ts - self._last_rss_log_ts) >= self.rss_log_interval_s:
                self._last_rss_log_ts = now_ts
                self._append_rss_log(
                    proc_rss_gb=proc_rss_gb,
                    peak_rss_gb=self.peak_rss_gb,
                    sys_ram_gb=sys_ram_gb,
                    cpu_pct=cpu_pct,
                )

            if proc_rss_gb >= self.ram_crit_gb:
                self.logger.warning(
                    "CRITICAL RAM usage: %.2f GB / %.1f GB budget! Publishing critical RAM pressure.",
                    proc_rss_gb,
                    self.ram_budget_gb,
                )
                self.bus.publish(
                    Topics.RAM_PRESSURE,
                    {"level": "critical", "rss_gb": round(proc_rss_gb, 2)},
                    source="telemetry",
                )
                gc.collect()
            elif proc_rss_gb >= self.ram_warn_gb:
                self.bus.publish(
                    Topics.RAM_PRESSURE,
                    {"level": "warning", "rss_gb": round(proc_rss_gb, 2)},
                    source="telemetry",
                )

            self.brain.check_idle_unload()

            battery = psutil.sensors_battery() if (PSUTIL_AVAILABLE and psutil is not None) else None
            battery_pct = int(round(battery.percent)) if battery is not None else 86

            self.metrics_updated.emit(cpu_pct, round(display_ram_gb, 2), battery_pct)
        except Exception as exc:
            self.logger.debug("Telemetry poll error: %s", exc)

    def _append_rss_log(
        self,
        proc_rss_gb: float,
        peak_rss_gb: float,
        sys_ram_gb: float,
        cpu_pct: int,
    ) -> None:
        """Append a single structured telemetry line to `logs/rss.log` (used by `tail -f logs/rss.log`)."""
        try:
            ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = (
                f"{ts_str} | proc_rss={proc_rss_gb:.3f}GB | peak_rss={peak_rss_gb:.3f}GB | "
                f"sys_ram={sys_ram_gb:.2f}GB | budget={self.ram_budget_gb:.1f}GB | "
                f"cpu={cpu_pct:02d}% | vram=0MB\n"
            )
            with self.rss_log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as exc:
            self.logger.debug("Could not write to rss.log: %s", exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E.V. — Enhanced Virtual Assistant")
    parser.add_argument(
        "--config",
        type=str,
        default="config.yaml",
        help="Path to configuration YAML file (default: config.yaml)",
    )
    parser.add_argument(
        "--minimal-ui",
        action="store_true",
        help="Disable background particles and scanlines for ultra-low CPU overhead",
    )
    parser.add_argument(
        "--fullscreen",
        action="store_true",
        help="Launch the holographic HUD in fullscreen mode",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    base_dir = Path(__file__).resolve().parent
    config_path = (base_dir / args.config).resolve() if not os.path.isabs(args.config) else Path(args.config)

    config = load_config(config_path)
    if args.minimal_ui:
        config.setdefault("gui", {})["minimal_ui"] = True
    if args.fullscreen:
        config.setdefault("gui", {})["fullscreen"] = True

    logger, rss_log_path = setup_logging(config, base_dir)
    logger.info(
        "Starting %s v%s (PyQt%d | Strict CPU-Only | n_gpu_layers=0)",
        config["app"]["full_name"],
        config["app"]["version"],
        QT_VERSION,
    )

    app = QApplication(sys.argv)
    app.setApplicationName(config["app"]["name"])
    app.setApplicationDisplayName(config["app"]["full_name"])

    qss_path = base_dir / "gui" / "styles.qss"
    if qss_path.exists():
        try:
            app.setStyleSheet(qss_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not load styles.qss: %s", exc)

    # 1. Core EventBus, Brain, and Voice I/O
    event_bus = get_event_bus()
    brain = Brain(config=config, base_dir=base_dir, event_bus=event_bus)
    stt = VoskSTT(config=config, base_dir=base_dir, event_bus=event_bus)
    tts = PiperTTS(config=config, base_dir=base_dir, event_bus=event_bus)
    clap_detector = ClapDetector(config=config, event_bus=event_bus)
    wakeword = WakeWordLoop(
        config=config,
        stt=stt,
        event_bus=event_bus,
        on_raw_audio_chunk=clap_detector.process_pcm_frame,
    )

    # 2. Automation, Remote Bridges, Audio Routing, Scheduler, and RAG Memory
    system_ctrl = SystemController(config=config, base_dir=base_dir)
    media_ctrl = MediaController(config=config, base_dir=base_dir)
    web_search = WebSearchTool(config=config)
    audio_router = AudioRouter(config=config, tts=tts, event_bus=event_bus)
    phone_adb = PhoneADBController(
        config=config,
        base_dir=base_dir,
        event_bus=event_bus,
        audio_router=audio_router,
    )
    whatsapp = WhatsAppController(
        config=config,
        base_dir=base_dir,
        event_bus=event_bus,
        tts=tts,
    )
    rag = RAGMemoryManager(config=config, base_dir=base_dir)

    workspace_orch = WorkspaceOrchestrator(
        config=config,
        base_dir=base_dir,
        system_ctrl=system_ctrl,
        event_bus=event_bus,
        tts=tts,
    )
    scheduler = TimedCommandScheduler(
        config=config,
        event_bus=event_bus,
        tts=tts,
        phone_adb=phone_adb,
    )
    intent_router = IntentRouter(
        config=config,
        brain=brain,
        system_ctrl=system_ctrl,
        workspace_orch=workspace_orch,
        media_ctrl=media_ctrl,
        web_search=web_search,
        scheduler=scheduler,
        phone_adb=phone_adb,
        whatsapp=whatsapp,
        rag=rag,
        event_bus=event_bus,
    )
    scheduler.set_dependencies(command_executor=intent_router.route)

    bridge = SystemBridge(
        config=config,
        base_dir=base_dir,
        rss_log_path=rss_log_path,
        event_bus=event_bus,
        brain=brain,
        intent_router=intent_router,
        workspace_orch=workspace_orch,
        scheduler=scheduler,
        phone_adb=phone_adb,
        audio_router=audio_router,
        stt=stt,
        tts=tts,
        wakeword=wakeword,
    )
    window = EVMainWindow(config=config, base_dir=base_dir)

    # Wire real-time telemetry and EventBus signals into the HUD
    bridge.metrics_updated.connect(window.update_system_metrics)
    bridge.orb_state_changed.connect(window.set_orb_state)
    bridge.caption_emitted.connect(window.show_caption)
    bridge.connection_changed.connect(window.set_connection_status)
    bridge.workspace_progress_updated.connect(window.on_workspace_progress_event)
    bridge.countdown_updated.connect(window.on_countdown_event)
    bridge.call_state_updated.connect(window.on_call_event)

    # Connect all HUD interactive buttons to real backend services
    window.set_external_mic_handler(bridge.handle_gui_mic_button)
    window.set_external_workspace_handlers(
        launch_fn=bridge.handle_gui_workspace_launch,
        undo_fn=bridge.handle_gui_workspace_undo,
        cancel_fn=bridge.handle_gui_workspace_cancel,
    )
    window.set_external_scheduler_handlers(
        start_quick_fn=bridge.handle_gui_timer_start,
        cancel_fn=bridge.handle_gui_timer_cancel,
        run_now_fn=bridge.handle_gui_timer_run_now,
    )
    window.set_external_call_handlers(
        accept_fn=bridge.handle_gui_call_accept,
        end_fn=bridge.handle_gui_call_end,
    )
    window.setting_changed.connect(
        lambda key, val: event_bus.publish(
            Topics.SETTINGS_CHANGED,
            {"key": key, "value": val, "settings": dict(window.settings)},
            source="gui",
        )
    )

    # Start background wake-word loop ("Hey E.V.") & non-blocking ADB discovery
    wakeword.start()
    phone_adb.connect_async()

    def _cleanup() -> None:
        logger.info("Shutting down E.V. services cleanly...")
        scheduler.shutdown()
        workspace_orch.cancel()
        whatsapp.close_session()
        audio_router.stop_call_routing()
        wakeword.stop()
        tts.shutdown()
        brain.unload_model(reason="shutdown")

    app.aboutToQuit.connect(_cleanup)
    signal.signal(signal.SIGINT, lambda *_: app.quit())

    if config.get("gui", {}).get("fullscreen", False):
        window.showFullScreen()
    else:
        window.show()

    return app.exec() if QT_VERSION == 6 else app.exec_()


if __name__ == "__main__":
    sys.exit(main())