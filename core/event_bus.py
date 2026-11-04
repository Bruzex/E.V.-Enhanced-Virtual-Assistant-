#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Non-Reentrant Thread-Safe Event Bus (`core/event_bus.py`)
=============================================================================================
Lightweight publish/subscribe broker that decouples background worker threads
(Vosk STT, Wake-Word loop, Clap Detector, llama.cpp Brain, Workspace Orchestrator,
Timed Scheduler, ADB/WhatsApp bridges) from the PyQt HUD main thread.

Key Safety Guarantees:
  - Non-Reentrant Publish Guard: Thread-local active-topic set and depth cap
    prevent any subscriber from recursively re-publishing the same topic while
    still inside the publish call stack (eliminates `maximum recursion depth exceeded`).
  - Fault Isolation: Every subscriber invocation is wrapped in `try/except`
    (catching both `Exception` and `RecursionError`), logged, and skipped so one
    faulty handler never crashes the voice loop or blocks subsequent subscribers.
  - Complete Topic Registry: Includes both canonical `VOICE_*` names and short
    aliases (`WAKEWORD_DETECTED`, `TRANSCRIPT_PARTIAL`, `TRANSCRIPT_FINAL`).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


class Topics:
    """Canonical event topic names and aliases used across E.V. modules."""

    # GUI / Presence State
    ORB_STATE = "orb.state"                     # payload: {"state": str, "progress": float}
    CAPTION = "hud.caption"                     # payload: {"speaker": "user"|"ev", "text": str, "hold_ms": int}
    CONNECTION_STATUS = "hud.connection"        # payload: {"id": str, "status": "online"|"standby"|"offline"}
    SYSTEM_METRICS = "system.metrics"           # payload: {"cpu": int, "ram_gb": float, "battery": int}
    RAM_PRESSURE = "system.ram_pressure"        # payload: {"level": "warning"|"critical", "rss_gb": float}

    # Voice & Audio Pipeline (canonical + short aliases)
    VOICE_WAKEWORD_DETECTED = "voice.wakeword"
    WAKEWORD_DETECTED = "voice.wakeword"
    VOICE_LISTENING_STARTED = "voice.listening.started"
    VOICE_LISTENING_STOPPED = "voice.listening.stopped"
    VOICE_PARTIAL_TRANSCRIPT = "voice.stt.partial"
    TRANSCRIPT_PARTIAL = "voice.stt.partial"
    VOICE_FINAL_TRANSCRIPT = "voice.stt.final"
    TRANSCRIPT_FINAL = "voice.stt.final"
    VOICE_USER_COMMAND = "voice.command"
    VOICE_INTERRUPT = "voice.interrupt"
    VOICE_TTS_STARTED = "voice.tts.started"
    VOICE_TTS_FINISHED = "voice.tts.finished"
    VOICE_CLAP_DETECTED = "voice.clap"

    # Brain / LLM & Intent Routing
    BRAIN_THINKING_STARTED = "brain.thinking.started"
    BRAIN_RESPONSE_READY = "brain.response.ready"
    BRAIN_MODEL_UNLOADED = "brain.model.unloaded"

    # Workspace & Timed Commands
    WORKSPACE_PROGRESS = "workspace.progress"
    COUNTDOWN_UPDATE = "scheduler.countdown"
    CALL_STATE = "remote.call.state"

    # User Settings Toggles from GUI
    SETTINGS_CHANGED = "settings.changed"


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
    Thread-safe, non-reentrant pub/sub event bus.
    Prevents recursive re-entry on the same topic and isolates subscriber exceptions.
    """

    MAX_PUBLISH_DEPTH = 6

    def __init__(self) -> None:
        self._logger = logging.getLogger("ev.event_bus")
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[SubscriberCallback]] = {}
        self._wildcard_subscribers: List[SubscriberCallback] = []
        self._history_limit = 50
        self._recent_events: List[Event] = []
        self._tls = threading.local()

    def _get_tls_state(self) -> tuple[Set[str], int]:
        active: Optional[Set[str]] = getattr(self._tls, "active_topics", None)
        if active is None:
            active = set()
            self._tls.active_topics = active
        depth: int = int(getattr(self._tls, "depth", 0))
        return active, depth

    def subscribe(self, topic: str, callback: SubscriberCallback) -> Callable[[], None]:
        """Register a callback for a specific topic (or '*' for all topics)."""
        if not callable(callback) or callback is self.publish:
            return lambda: None

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
        Non-reentrant per topic: if a subscriber attempts to publish the same
        `topic` while already inside `publish(topic)` on the same thread (or if
        nesting exceeds `MAX_PUBLISH_DEPTH`), the re-entrant call returns immediately.
        """
        safe_payload = dict(payload) if isinstance(payload, dict) else {}
        event = Event(
            topic=str(topic),
            payload=safe_payload,
            timestamp=time.time(),
            source=str(source),
        )

        active_topics, depth = self._get_tls_state()
        if topic in active_topics or depth >= self.MAX_PUBLISH_DEPTH:
            self._logger.debug(
                "Skipped re-entrant publish for topic '%s' (source=%s, depth=%d).",
                topic,
                source,
                depth,
            )
            return event

        active_topics.add(topic)
        self._tls.depth = depth + 1
        try:
            with self._lock:
                self._recent_events.append(event)
                if len(self._recent_events) > self._history_limit:
                    self._recent_events = self._recent_events[-self._history_limit :]

                callbacks = list(self._subscribers.get(topic, ()))
                wildcards = list(self._wildcard_subscribers)

            for cb in callbacks + wildcards:
                try:
                    cb(event)
                except (Exception, RecursionError) as exc:
                    self._logger.error(
                        "Subscriber %r raised %s on topic '%s': %s",
                        getattr(cb, "__qualname__", cb),
                        type(exc).__name__,
                        topic,
                        exc,
                    )
        finally:
            active_topics.discard(topic)
            self._tls.depth = depth

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
