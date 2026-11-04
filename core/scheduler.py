#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Timed Command Scheduler
===========================================================
Thread-safe, non-blocking countdown scheduler for delayed voice/GUI commands
(e.g., "in 30 seconds call Mom", "in 10 seconds dim desk lights to 30%").

Key Features:
  - Real-time 1 Hz tick worker publishing `Topics.COUNTDOWN_UPDATE` (`status`:
    `"running"` | `"completed"` | `"cancelled"`) and `Topics.ORB_STATE`
    (`state="acting"`, `progress=1 - remaining/total`) so the right-side HUD
    Countdown Panel and central Status Orb stay 100% synchronized.
  - Full `cancel()` and `run_now()` support wired directly to the HUD Countdown
    Panel buttons and voice commands ("cancel timer", "run timer now").
  - Automatic Action Dispatch on Zero:
      * If `kind == "call"`, invokes `phone_adb.call_contact()` (when available)
        and publishes `Topics.CALL_STATE` so the full-screen Iron-Man Call
        Overlay activates immediately.
      * If `command` is provided, routes the deferred command through
        `IntentRouter` or a custom callback and speaks `done_message` via `PiperTTS`.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from core.event_bus import EventBus, Topics, get_event_bus


@dataclass
class ScheduledTask:
    """Represents an active or queued timed command."""

    task_id: str
    label: str
    detail: str
    total_seconds: int
    remaining_seconds: int
    kind: str = "notify"          # 'call' | 'notify' | 'command'
    contact: str = "Mom"
    number: str = "+1 (555) 014-2290"
    command_text: str = ""
    done_message: str = ""
    on_complete: Optional[Callable[[], None]] = None
    created_at: float = field(default_factory=time.time)


class TimedCommandScheduler:
    """
    Thread-safe countdown scheduler driving the right-side HUD Countdown Panel
    and executing deferred actions upon completion.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        event_bus: Optional[EventBus] = None,
        tts: Optional[Any] = None,
        phone_adb: Optional[Any] = None,
        command_executor: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.config = config or {}
        self.bus = event_bus or get_event_bus()
        self.tts = tts
        self.phone_adb = phone_adb
        self.command_executor = command_executor
        self.logger = logging.getLogger("ev.scheduler")

        self._lock = threading.RLock()
        self._active_task: Optional[ScheduledTask] = None
        self._cancel_event = threading.Event()
        self._run_now_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    @property
    def active_task(self) -> Optional[ScheduledTask]:
        with self._lock:
            return self._active_task

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._active_task is not None

    @property
    def is_busy(self) -> bool:
        return self.is_running

    def set_dependencies(
        self,
        tts: Optional[Any] = None,
        phone_adb: Optional[Any] = None,
        command_executor: Optional[Callable[[str], Any]] = None,
    ) -> None:
        """Attach or update runtime dependencies after initialization."""
        if tts is not None:
            self.tts = tts
        if phone_adb is not None:
            self.phone_adb = phone_adb
        if command_executor is not None:
            self.command_executor = command_executor

    # =========================================================================
    # Public Scheduling & Control API
    # =========================================================================
    def schedule(
        self,
        seconds: int,
        label: str,
        detail: str = "Scheduled local automation",
        kind: str = "notify",
        contact: str = "Mom",
        number: str = "+1 (555) 014-2290",
        command_text: str = "",
        done_message: str = "",
        on_complete: Optional[Callable[[], None]] = None,
    ) -> ScheduledTask:
        """
        Start a new timed command countdown. If a previous countdown is active,
        it is cleanly superseded.
        """
        total = max(1, int(seconds))
        task = ScheduledTask(
            task_id=uuid.uuid4().hex[:8],
            label=label.strip() or "Timed command",
            detail=detail.strip() or "Scheduled local automation",
            total_seconds=total,
            remaining_seconds=total,
            kind=kind,
            contact=contact,
            number=number,
            command_text=command_text,
            done_message=done_message or f"Completed: {label}.",
            on_complete=on_complete,
        )

        # Stop any existing countdown thread before starting the new one
        self._stop_existing_worker()

        with self._lock:
            self._active_task = task
            self._cancel_event.clear()
            self._run_now_event.clear()

        self._worker_thread = threading.Thread(
            target=self._countdown_loop,
            args=(task,),
            name=f"EV-Scheduler-{task.task_id}",
            daemon=True,
        )
        self._worker_thread.start()
        self.logger.info(
            "Scheduled timed command '%s' (%ds | kind=%s)",
            task.label,
            task.total_seconds,
            task.kind,
        )
        return task

    def schedule_quick_timer(
        self,
        seconds: Optional[int] = None,
        label: Optional[str] = None,
        detail: Optional[str] = None,
        done_message: Optional[str] = None,
    ) -> ScheduledTask:
        """
        Start the default quick timer from the HUD bottom dock ('Timer' button):
        'Dim desk lights to 30%' in 10 seconds.
        """
        sec = seconds if seconds is not None else 10
        lbl = label or "Dim desk lights to 30%"
        det = detail or "Smart plug · Study"
        done_msg = done_message or (f"{lbl} complete." if label else "Desk lights dimmed to 30%.")
        task = self.schedule(
            seconds=sec,
            label=lbl,
            detail=det,
            kind="notify",
            done_message=done_msg,
        )
        msg = f"Okay. {task.label} in {task.total_seconds} seconds."
        if self.tts is not None:
            self.tts.speak(msg, block=False)
        else:
            self.bus.publish(
                Topics.CAPTION,
                {"speaker": "ev", "text": msg, "hold_ms": 2200},
                source="scheduler",
            )
        return task

    def cancel(self, speak_feedback: bool = True) -> bool:
        """Cancel the active timed command and hide the right-side HUD panel."""
        with self._lock:
            if self._active_task is None:
                return False
            task = self._active_task
            self._active_task = None
            self._cancel_event.set()

        self._publish_update(task, status="cancelled")
        self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="scheduler")

        if speak_feedback:
            msg = "Timed command cancelled."
            if self.tts is not None:
                self.tts.speak(msg, block=False)
            else:
                self.bus.publish(
                    Topics.CAPTION,
                    {"speaker": "ev", "text": msg, "hold_ms": 2000},
                    source="scheduler",
                )
        self.logger.info("Cancelled timed command '%s'", task.label)
        return True

    def run_now(self) -> bool:
        """Immediately fast-forward the active countdown to 0 and execute its action."""
        with self._lock:
            if self._active_task is None:
                return False
            self._run_now_event.set()
        return True

    def shutdown(self) -> None:
        """Cleanly stop any active timer on application exit."""
        self._cancel_event.set()
        self._stop_existing_worker()

    # =========================================================================
    # Internal Worker Loop & Action Execution
    # =========================================================================
    def _stop_existing_worker(self) -> None:
        with self._lock:
            if self._active_task is not None:
                self._cancel_event.set()
            worker = self._worker_thread
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=0.35)

    def _countdown_loop(self, task: ScheduledTask) -> None:
        # Emit initial tick (remaining == total)
        self._publish_update(task, status="running")
        self._publish_orb_progress(task)

        next_tick = time.monotonic() + 1.0

        while task.remaining_seconds > 0:
            if self._cancel_event.is_set():
                return
            if self._run_now_event.is_set():
                task.remaining_seconds = 0
                break

            now = time.monotonic()
            sleep_for = max(0.0, min(0.1, next_tick - now))
            if sleep_for > 0:
                time.sleep(sleep_for)
                continue

            task.remaining_seconds = max(0, task.remaining_seconds - 1)
            next_tick += 1.0

            if self._cancel_event.is_set():
                return
            if self._run_now_event.is_set():
                task.remaining_seconds = 0
                break

            if task.remaining_seconds > 0:
                self._publish_update(task, status="running")
                self._publish_orb_progress(task)

        if self._cancel_event.is_set():
            return

        with self._lock:
            if self._active_task is task:
                self._active_task = None

        task.remaining_seconds = 0
        self._publish_update(task, status="completed")
        self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="scheduler")
        self._execute_task_action(task)

    def _execute_task_action(self, task: ScheduledTask) -> None:
        """Execute the scheduled action when the countdown hits 0."""
        self.logger.info("Executing completed timed command '%s' (kind=%s)", task.label, task.kind)
        try:
            if task.kind == "call":
                # Resolve contact details from config or task
                contacts = self.config.get("remote", {}).get("phone_adb", {}).get("contacts", {})
                c_info = contacts.get(task.contact.lower(), {})
                name = str(c_info.get("name", task.contact or "Mom"))
                number = str(c_info.get("number", task.number or "+1 (555) 014-2290"))
                meta = str(c_info.get("meta", "Mobile · via phone bridge"))

                # Dial via ADB bridge if connected
                if self.phone_adb is not None:
                    self.phone_adb.make_call(number, contact_name=name, emit_overlay=False)

                # Trigger the Iron-Man Call Overlay on the HUD
                self.bus.publish(
                    Topics.CALL_STATE,
                    {
                        "action": "start",
                        "direction": "outgoing",
                        "status": "ringing",
                        "name": name,
                        "number": number,
                        "meta": meta,
                    },
                    source="scheduler",
                )
                ready_msg = f"Your call to {name} is ready."
                if self.tts is not None:
                    self.tts.speak(ready_msg, block=False)
                else:
                    self.bus.publish(
                        Topics.CAPTION,
                        {"speaker": "ev", "text": ready_msg, "hold_ms": 2600},
                        source="scheduler",
                    )
            else:
                if task.command_text and self.command_executor is not None:
                    self.command_executor(task.command_text)

                if task.done_message:
                    if self.tts is not None:
                        self.tts.speak(task.done_message, block=False)
                    else:
                        self.bus.publish(
                            Topics.CAPTION,
                            {"speaker": "ev", "text": task.done_message, "hold_ms": 2400},
                            source="scheduler",
                        )

            if task.on_complete is not None:
                task.on_complete()
        except Exception as exc:
            self.logger.error("Error executing scheduled task '%s': %s", task.label, exc, exc_info=True)

    def _publish_update(self, task: ScheduledTask, status: str) -> None:
        self.bus.publish(
            Topics.COUNTDOWN_UPDATE,
            {
                "task_id": task.task_id,
                "status": status,  # 'running' | 'completed' | 'cancelled'
                "total": task.total_seconds,
                "remaining": task.remaining_seconds,
                "label": task.label,
                "detail": task.detail,
                "kind": task.kind,
                "contact": task.contact,
                "done_message": task.done_message,
            },
            source="scheduler",
        )

    def _publish_orb_progress(self, task: ScheduledTask) -> None:
        prog = 1.0 - (task.remaining_seconds / float(max(1, task.total_seconds)))
        self.bus.publish(
            Topics.ORB_STATE,
            {"state": "acting", "progress": max(0.0, min(1.0, prog))},
            source="scheduler",
        )

# Backward compatibility alias
TimedScheduler = TimedCommandScheduler
