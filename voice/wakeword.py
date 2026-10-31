#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Wake-Word & Continuous Voice Loop
=====================================================================
Implements a low-CPU, two-stage wake-word + command listening pipeline:

  1. Energy Gate (RMS Pre-Filter):
     Computes fast 16-bit PCM root-mean-square energy on each audio frame.
     Silent/ambient background frames are skipped before touching Vosk,
     keeping idle CPU usage near 1–2%.
  2. Phrase Matcher + Inline Command Extraction:
     Matches customizable wake phrases ("hey ev", "hey e v", "okay ev") via
     Vosk partial/final transcripts.
     - If the user speaks a full sentence in one breath (e.g.,
       "Hey E.V., open my coding workspace and prepare a call to Mom in 30 seconds"),
       the command portion after the wake phrase is extracted and dispatched
       immediately without forcing a second pause.
     - If the user only says "Hey E.V.", E.V. transitions the Status Orb to
       `Listening` and captures the follow-up command until silence or timeout.
  3. Continuous Listening Mode:
     When `continuousListening` is toggled in the HUD Settings popover, E.V.
     stays in active command listening mode between responses without requiring
     the wake word each time.
"""

from __future__ import annotations

import logging
import math
import re
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core.event_bus import Event, EventBus, Topics, get_event_bus
from voice.stt import VoskSTT


def compute_pcm16_rms(pcm_bytes: bytes) -> float:
    """Compute normalized RMS energy (0.0 to 1.0) of a 16-bit mono PCM buffer."""
    count = len(pcm_bytes) // 2
    if count <= 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm_bytes[: count * 2])
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / float(count))
    return min(1.0, rms / 32768.0)


class WakeWordLoop:
    """
    Background voice loop managing Wake-Word detection ("Hey E.V."),
    push-to-talk Mic triggers from the HUD, and continuous listening mode.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        stt: VoskSTT,
        event_bus: Optional[EventBus] = None,
        on_raw_audio_chunk: Optional[Callable[[bytes, float], None]] = None,
    ) -> None:
        self.config = config
        self.stt = stt
        self.bus = event_bus or get_event_bus()
        self.on_raw_audio_chunk = on_raw_audio_chunk
        self.logger = logging.getLogger("ev.wakeword")

        ww_cfg = config.get("voice", {}).get("wake_word", {})
        self.enabled: bool = bool(ww_cfg.get("enabled", True))
        self.continuous_listening: bool = bool(ww_cfg.get("continuous_listening", False))
        self.command_timeout_s: float = float(ww_cfg.get("command_timeout_seconds", 6.0))
        self.energy_gate_threshold: float = float(ww_cfg.get("energy_gate_threshold", 0.004))

        raw_phrases = ww_cfg.get(
            "phrases",
            ["hey ev", "hey e v", "hey e.v.", "okay ev", "hi ev", "jarvis"],
        )
        self.wake_phrases: List[str] = [self._normalize(p) for p in raw_phrases if p]

        self._stop_event = threading.Event()
        self._manual_trigger_event = threading.Event()
        self._active_command_mode = False
        self._active_deadline: float = 0.0
        self._thread: Optional[threading.Thread] = None

        # Listen for GUI Settings popover changes (wakeWord / continuousListening toggles)
        self.bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed)

    @staticmethod
    def _normalize(text: str) -> str:
        cleaned = re.sub(r"[^a-z0-9\\s]", " ", (text or "").lower())
        return re.sub(r"\\s+", " ", cleaned).strip()

    def start(self) -> None:
        """Start the background wake-word and voice capture daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="EV-WakeWord-Loop",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(
            "Wake-word loop started (enabled=%s | continuous=%s | phrases=%s)",
            self.enabled,
            self.continuous_listening,
            self.wake_phrases,
        )

    def stop(self) -> None:
        """Stop the background wake-word thread cleanly."""
        self._stop_event.set()
        self._manual_trigger_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def trigger_manual_listen(self) -> None:
        """
        Called when the user clicks the central Mic button on the HUD or
        triggers a double-clap. Enters active command listening immediately.
        """
        self._active_command_mode = True
        self._active_deadline = time.monotonic() + self.command_timeout_s
        self._manual_trigger_event.set()
        self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")
        self.bus.publish(Topics.VOICE_LISTENING_STARTED, {}, source="wakeword")

    def cancel_active_listen(self) -> None:
        """Cancel active command capture and return Orb to idle."""
        self._active_command_mode = False
        self.bus.publish(Topics.VOICE_LISTENING_STOPPED, {}, source="wakeword")
        self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="wakeword")

    def inspect_transcript(self, text: str) -> tuple[bool, str]:
        """
        Check whether `text` contains a wake phrase.
        Returns `(wake_triggered, remainder_command_text)`.
        """
        norm = self._normalize(text)
        if not norm:
            return False, ""

        for phrase in self.wake_phrases:
            idx = norm.find(phrase)
            if idx != -1:
                remainder = norm[idx + len(phrase) :].strip()
                return True, remainder
        return False, ""

    # =========================================================================
    # Internal Audio Processing Loop
    # =========================================================================
    def _on_settings_changed(self, event: Event) -> None:
        key = event.payload.get("key")
        val = event.payload.get("value")
        if key == "wakeWord":
            self.enabled = bool(val)
        elif key == "continuousListening":
            self.continuous_listening = bool(val)

    def _run_loop(self) -> None:
        if not self.stt.load_model():
            self.logger.info(
                "Vosk microphone model not active; WakeWordLoop standing by for simulated/GUI voice triggers."
            )
            while not self._stop_event.is_set():
                time.sleep(0.5)
            return

        rec = self.stt.create_recognizer()
        if rec is None:
            return

        silence_frames = 0

        def _handle_raw(pcm: bytes) -> None:
            if self.on_raw_audio_chunk is not None:
                energy = compute_pcm16_rms(pcm)
                self.on_raw_audio_chunk(pcm, energy)

        for pcm_chunk in self.stt.iter_mic_chunks(
            stop_check=self._stop_event.is_set,
            on_raw_chunk=_handle_raw,
        ):
            if self._stop_event.is_set():
                break

            rms = compute_pcm16_rms(pcm_chunk)
            now = time.monotonic()

            # Check command timeout if waiting for follow-up speech after "Hey E.V."
            if self._active_command_mode and now > self._active_deadline and not self.continuous_listening:
                self._active_command_mode = False
                self.bus.publish(Topics.VOICE_LISTENING_STOPPED, {}, source="wakeword")
                self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="wakeword")

            # Energy gate: skip near-silent frames when idle unless flush is needed
            if rms < self.energy_gate_threshold and not self._active_command_mode:
                silence_frames += 1
                if silence_frames > 4:
                    continue
            else:
                silence_frames = 0

            is_final, transcript = self.stt.feed_pcm_chunk(pcm_chunk, recognizer=rec)
            if not transcript:
                continue

            # Mode A: Already in active command listening mode (or continuous mode)
            if self._active_command_mode or self.continuous_listening:
                self._active_deadline = now + self.command_timeout_s
                _, stripped = self.inspect_transcript(transcript)
                display_text = stripped if stripped else transcript

                if not is_final:
                    self.bus.publish(
                        Topics.CAPTION,
                        {"speaker": "user", "text": display_text, "hold_ms": 0},
                        source="wakeword",
                    )
                else:
                    self._active_command_mode = self.continuous_listening
                    self.bus.publish(
                        Topics.CAPTION,
                        {"speaker": "user", "text": display_text, "hold_ms": 3000},
                        source="wakeword",
                    )
                    self.bus.publish(
                        Topics.VOICE_USER_COMMAND,
                        {"text": display_text, "source": "voice"},
                        source="wakeword",
                    )
                continue

            # Mode B: Waiting for Wake Word ("Hey E.V.")
            if not self.enabled:
                continue

            triggered, remainder = self.inspect_transcript(transcript)
            if triggered:
                self.bus.publish(
                    Topics.VOICE_WAKEWORD_DETECTED,
                    {"phrase": "hey ev", "timestamp": time.time()},
                    source="wakeword",
                )
                if is_final and remainder:
                    # User spoke wake word + command in a single utterance
                    self.bus.publish(
                        Topics.CAPTION,
                        {"speaker": "user", "text": transcript, "hold_ms": 3000},
                        source="wakeword",
                    )
                    self.bus.publish(
                        Topics.VOICE_USER_COMMAND,
                        {"text": remainder, "source": "voice"},
                        source="wakeword",
                    )
                else:
                    # Wake word detected; transition Orb to Listening and await command
                    self._active_command_mode = True
                    self._active_deadline = now + self.command_timeout_s
                    self.bus.publish(
                        Topics.ORB_STATE,
                        {"state": "listening", "progress": 0.0},
                        source="wakeword",
                    )
                    if remainder:
                        self.bus.publish(
                            Topics.CAPTION,
                            {"speaker": "user", "text": remainder, "hold_ms": 0},
                            source="wakeword",
                        )


# Alias for backward compatibility
WakeWordDetector = WakeWordLoop
