#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Thread-Safe Event Bus
=========================================================
Lightweight publish/subscribe broker that decouples background worker threads
(Vosk STT, Wake-Word loop, Clap Detector, llama.cpp Brain, Workspace Orchestrator,
Timed Scheduler, ADB/WhatsApp bridges) from the PyQt HUD main thread.

Key Design Principles:
  - Zero external dependencies (pure Python stdlib: threading, logging, time).
  - Thread-safe subscription management via RLock.
  - Fault isolation: an exception inside one subscriber callback is logged and
    never crashes the publisher or blocks subsequent subscribers.
  - Supports wildcard ('*') listeners for global debugging, telemetry, or Qt
    signal bridging.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# =============================================================================
# Standard Event Topic Constants
# =============================================================================
class Topics:
    """Canonical event topic names used across E.V. modules."""

    # GUI / Presence State
    ORB_STATE = "orb.state"                     # payload: {"state": str, "progress": float}
    CAPTION = "hud.caption"                     # payload: {"speaker": "user"|"ev", "text": str, "hold_ms": int}
    CONNECTION_STATUS = "hud.connection"        # payload: {"id": str, "status": "online"|"standby"|"offline"}
    SYSTEM_METRICS = "system.metrics"           # payload: {"cpu": int, "ram_gb": float, "battery": int}
    RAM_PRESSURE = "system.ram_pressure"        # payload: {"level": "warning"|"critical", "rss_gb": float}

    # Voice & Audio Pipeline
    VOICE_WAKEWORD_DETECTED = "voice.wakeword"  # payload: {"phrase": str, "timestamp": float}
    VOICE_LISTENING_STARTED = "voice.listening.started"
    VOICE_LISTENING_STOPPED = "voice.listening.stopped"
    VOICE_PARTIAL_TRANSCRIPT = "voice.stt.partial"   # payload: {"text": str}
    VOICE_FINAL_TRANSCRIPT = "voice.stt.final"       # payload: {"text": str, "confidence": float}
    VOICE_USER_COMMAND = "voice.command"             # payload: {"text": str, "source": "voice"|"gui"|"text"}
    VOICE_TTS_STARTED = "voice.tts.started"          # payload: {"text": str}
    VOICE_TTS_FINISHED = "voice.tts.finished"        # payload: {"text": str}
    VOICE_CLAP_DETECTED = "voice.clap"               # payload: {"count": int, "energy": float}

    # Brain / LLM & Intent Routing
    BRAIN_THINKING_STARTED = "brain.thinking.started"  # payload: {"prompt": str}
    BRAIN_RESPONSE_READY = "brain.response.ready"      # payload: {"text": str, "latency_ms": float, "from_llm": bool}
    BRAIN_MODEL_UNLOADED = "brain.model.unloaded"      # payload: {"reason": str}

    # Workspace & Timed Commands (Phase 2 & 3 hooks)
    WORKSPACE_PROGRESS = "workspace.progress"   # payload: {"name": str, "status": str, "completed": int, "total": int}
    COUNTDOWN_UPDATE = "scheduler.countdown"    # payload: {"label": str, "detail": str, "total": int, "remaining": int}
    CALL_STATE = "remote.call.state"            # payload: {"direction": str, "status": str, "name": str, "number": str}

    # User Settings Toggles from GUI
    SETTINGS_CHANGED = "settings.changed"       # payload: {"key": str, "value": Any, "settings": dict}


@dataclass(frozen=True)
class Event:
    """Immutable event envelope dispatched to subscribers."""

    topic: str
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    source: str = "system"


SubscriberCallback = Callable[[Event], None]


class EventBus:
    """
    Thread-safe synchronous/asynchronous pub/sub event bus.
    Callbacks are invoked inline on the publishing thread by default; GUI
    components should subscribe through a QObject signal bridge (such as
    SystemBridge in main.py) so Qt automatically marshals calls onto the
    main GUI event loop thread.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("ev.event_bus")
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[SubscriberCallback]] = {}
        self._wildcard_subscribers: List[SubscriberCallback] = []
        self._history_limit = 50
        self._recent_events: List[Event] = []

    def subscribe(self, topic: str, callback: SubscriberCallback) -> Callable[[], None]:
        """
        Register a callback for a specific topic (or '*' for all topics).
        Returns an unsubscribe callable for convenient cleanup.
        """
        with self._lock:
            if topic == "*":
                if callback not in self._wildcard_subscribers:
                    self._wildcard_subscribers.append(callback)
            else:
                bucket = self._subscribers.setdefault(topic, [])
                if callback not in bucket:
                    bucket.append(callback)

        def _unsubscribe() -> None:
            self.unsubscribe(topic, callback)

        return _unsubscribe

    def unsubscribe(self, topic: str, callback: SubscriberCallback) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            if topic == "*":
                if callback in self._wildcard_subscribers:
                    self._wildcard_subscribers.remove(callback)
            elif topic in self._subscribers:
                bucket = self._subscribers[topic]
                if callback in bucket:
                    bucket.remove(callback)
                if not bucket:
                    del self._subscribers[topic]

    def publish(
        self,
        topic: str,
        payload: Optional[Dict[str, Any]] = None,
        source: str = "system",
    ) -> Event:
        """
        Publish an event to all topic subscribers and wildcard listeners.
        Exceptions in any subscriber are caught and logged so one faulty module
        never breaks the pipeline.
        """
        event = Event(
            topic=topic,
            payload=dict(payload) if payload else {},
            timestamp=time.time(),
            source=source,
        )

        with self._lock:
            self._recent_events.append(event)
            if len(self._recent_events) > self._history_limit:
                self._recent_events = self._recent_events[-self._history_limit :]

            callbacks = list(self._subscribers.get(topic, ()))
            wildcards = list(self._wildcard_subscribers)

        for cb in callbacks + wildcards:
            try:
                cb(event)
            except Exception as exc:
                self._logger.error(
                    "Unhandled exception in subscriber %r for topic '%s': %s",
                    getattr(cb, "__qualname__", cb),
                    topic,
                    exc,
                    exc_info=True,
                )

        return event

    def recent_events(self, limit: int = 20) -> List[Event]:
        """Return a snapshot of the most recent events for diagnostics."""
        with self._lock:
            return list(self._recent_events[-max(1, limit) :])

    def clear(self) -> None:
        """Remove all subscribers and event history (useful for unit tests)."""
        with self._lock:
            self._subscribers.clear()
            self._wildcard_subscribers.clear()
            self._recent_events.clear()


# Singleton accessor for convenience when passing the bus instance isn't desired
_GLOBAL_BUS: Optional[EventBus] = None
_GLOBAL_LOCK = threading.Lock()


def get_event_bus() -> EventBus:
    """Return the process-wide singleton EventBus instance."""
    global _GLOBAL_BUS
    if _GLOBAL_BUS is None:
        with _GLOBAL_LOCK:
            if _GLOBAL_BUS is None:
                _GLOBAL_BUS = EventBus()
    return _GLOBAL_BUS

