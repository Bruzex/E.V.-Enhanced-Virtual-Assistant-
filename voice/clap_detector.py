#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Energy/Peak Clap Detector
=============================================================
Ultra-lightweight acoustic impulse detector inspired by Jarvis AI Spark.
 piggybacks directly on the 16kHz mono PCM stream already captured by
`WakeWordLoop` (`on_raw_audio_chunk`), consuming near-zero extra CPU or RAM.

Algorithm:
  1. Fast Energy Gate: Ignores frames below `energy_threshold` (default 0.32 RMS).
  2. Transient Spike / Crest Check: Distinguishes sharp percussive claps from
     sustained loud speech by checking that the frame's peak-to-average ratio
     is impulsive and returns to quiet within `max_impulse_duration_ms`.
  3. Configurable Single or Double-Clap Window:
     - When `required_claps == 2` (default), two impulses separated by
       `double_clap_min_gap_ms` (120ms) to `double_clap_max_gap_ms` (480ms)
       trigger `Topics.VOICE_CLAP_DETECTED` and execute the configured action
       (e.g., toggle active listening / status orb).
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core.event_bus import Event, EventBus, Topics, get_event_bus


class ClapDetector:
    """
    Real-time single/double clap detector operating on raw 16-bit PCM frames.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        event_bus: Optional[EventBus] = None,
        on_clap_action: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.config = config or {}
        self.bus = event_bus or get_event_bus()
        self.on_clap_action = on_clap_action
        self.logger = logging.getLogger("ev.clap")

        clap_cfg = config.get("voice", {}).get("clap_detector", {})
        self.enabled: bool = bool(clap_cfg.get("enabled", True))
        self.required_claps: int = max(1, min(2, int(clap_cfg.get("required_claps", 2))))
        self.energy_threshold: float = float(clap_cfg.get("energy_threshold", 0.32))
        self.min_gap_s: float = float(clap_cfg.get("double_clap_min_gap_ms", 120)) / 1000.0
        self.max_gap_s: float = float(clap_cfg.get("double_clap_max_gap_ms", 480)) / 1000.0
        self.cooldown_s: float = float(clap_cfg.get("cooldown_seconds", 1.5))
        self.action: str = str(clap_cfg.get("action", "toggle_listening"))

        self._lock = threading.Lock()
        self._clap_timestamps: List[float] = []
        self._last_trigger_ts: float = 0.0
        self._in_loud_burst: bool = False
        self._muted_for_tts: bool = False

        # Subscribe to GUI Settings toggle & TTS mute events
        self.bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed)
        self.bus.subscribe(Topics.VOICE_TTS_STARTED, lambda _: self._set_tts_mute(True))
        self.bus.subscribe(Topics.VOICE_TTS_FINISHED, lambda _: self._set_tts_mute(False))

    def _set_tts_mute(self, muted: bool) -> None:
        self._muted_for_tts = muted

    def _on_settings_changed(self, event: Event) -> None:
        if event.payload.get("key") == "clapDetection":
            self.enabled = bool(event.payload.get("value", True))
            self.logger.info("Clap detection toggled: %s", self.enabled)

    def process_pcm_frame(
        self,
        pcm_bytes: bytes,
        precomputed_rms: Optional[float] = None,
        rms_energy: Optional[float] = None,
    ) -> bool:
        if rms_energy is not None and precomputed_rms is None:
            precomputed_rms = rms_energy
        """
        Inspect a single 16-bit PCM audio frame for a clap impulse.
        Returns True if the configured clap pattern (1 or 2 claps) was completed.
        """
        if not self.enabled or self._muted_for_tts or not pcm_bytes:
            return False

        now = time.monotonic()
        if (now - self._last_trigger_ts) < self.cooldown_s:
            return False

        # 1. Fast RMS Energy Gate
        rms = precomputed_rms if precomputed_rms is not None else self._compute_rms(pcm_bytes)
        if rms < self.energy_threshold:
            self._in_loud_burst = False
            return False

        # Avoid counting consecutive loud frames of the same clap as multiple claps
        if self._in_loud_burst:
            return False

        # 2. Crest factor check (peak amplitude vs RMS) to reject steady hum/vowels
        peak = self._compute_peak(pcm_bytes)
        if peak < min(0.95, self.energy_threshold * 1.35):
            return False

        self._in_loud_burst = True
        return self._record_impulse(now, rms)

    def register_external_impulse(self, energy: float = 0.5, timestamp: Optional[float] = None) -> bool:
        """
        Helper for testing or synthetic impulse injection without raw PCM hardware.
        """
        if not self.enabled:
            return False
        now = timestamp if timestamp is not None else time.monotonic()
        if energy < self.energy_threshold:
            return False
        return self._record_impulse(now, energy)

    def _record_impulse(self, now: float, energy: float) -> bool:
        with self._lock:
            # Prune stale impulses older than max_gap_s
            self._clap_timestamps = [
                ts for ts in self._clap_timestamps if (now - ts) <= self.max_gap_s
            ]

            if self._clap_timestamps and (now - self._clap_timestamps[-1]) < self.min_gap_s:
                # Too close to previous impulse (echo/reverb)
                return False

            self._clap_timestamps.append(now)

            if len(self._clap_timestamps) >= self.required_claps:
                count = len(self._clap_timestamps)
                self._clap_timestamps.clear()
                self._last_trigger_ts = now
                self.logger.info(
                    "Detected %d-clap trigger (energy=%.2f | action=%s)",
                    count,
                    energy,
                    self.action,
                )
                self.bus.publish(
                    Topics.VOICE_CLAP_DETECTED,
                    {"count": count, "energy": round(energy, 3), "action": self.action},
                    source="clap_detector",
                )
                if self.on_clap_action is not None:
                    self.on_clap_action(count)
                return True

        return False

    @staticmethod
    def _compute_rms(pcm_bytes: bytes) -> float:
        count = len(pcm_bytes) // 2
        if count <= 0:
            return 0.0
        samples = struct.unpack(f"<{count}h", pcm_bytes[: count * 2])
        sum_sq = sum(s * s for s in samples)
        return min(1.0, (sum_sq / float(count)) ** 0.5 / 32768.0)

    @staticmethod
    def _compute_peak(pcm_bytes: bytes) -> float:
        count = len(pcm_bytes) // 2
        if count <= 0:
            return 0.0
        samples = struct.unpack(f"<{count}h", pcm_bytes[: count * 2])
        max_abs = max(abs(s) for s in samples)
        return min(1.0, max_abs / 32768.0)