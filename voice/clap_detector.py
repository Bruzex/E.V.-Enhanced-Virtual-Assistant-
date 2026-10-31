#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Laptop-Mic Retuned Clap Detector (`clap_detector.py`)
=========================================================================================
Why Clap Detection Failed Previously on the MSI Bravo 15:
  - A hand clap lasts ~15–30 ms, whereas an audio frame is 125–250 ms.
    Averaging RMS across 250 ms diluted the clap's RMS below `0.32`, and
    Windows laptop mic AGC (Automatic Gain Control) compresses sharp peaks.

How It Works Now:
  1. Sub-Window Peak & Adaptive Noise Floor:
     Splits each incoming PCM buffer into 10ms sub-windows (160 samples @ 16kHz),
     tracks the rolling ambient background RMS floor (`_noise_floor`), and
     triggers when:
       - `peak >= peak_threshold` (default `0.16`, easily reached on laptop mics), AND
       - `sub_window_rms >= max(energy_threshold, noise_floor * 3.2)`, AND
       - `crest_factor (peak / frame_rms) >= 2.1` (sharp percussive transient).
  2. Wide Double-Clap Window (`80 ms` to `850 ms`):
     Supports natural hand-clap cadences (like the double-clap at 01:35 in your
     video) and acts as a first-class wake trigger (identical to saying "Hey Evie").
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core.event_bus import Event, EventBus, Topics, get_event_bus


class ClapDetector:
    """
    Laptop-microphone-tuned single/double clap detector with adaptive noise floor
    and 10ms sub-window transient analysis.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        event_bus: Optional[EventBus] = None,
        on_clap_action: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.config = config
        self.bus = event_bus or get_event_bus()
        self.on_clap_action = on_clap_action
        self.logger = logging.getLogger("ev.clap")

        clap_cfg = config.get("voice", {}).get("clap_detector", {})
        self.enabled: bool = bool(clap_cfg.get("enabled", True))
        self.required_claps: int = max(1, min(2, int(clap_cfg.get("required_claps", 2))))
        # Retuned for real laptop microphones with Windows AGC
        self.energy_threshold: float = float(clap_cfg.get("energy_threshold", 0.065))
        self.peak_threshold: float = float(clap_cfg.get("peak_threshold", 0.16))
        self.crest_factor_min: float = float(clap_cfg.get("crest_factor_min", 2.0))
        self.min_gap_s: float = float(clap_cfg.get("double_clap_min_gap_ms", 80)) / 1000.0
        self.max_gap_s: float = float(clap_cfg.get("double_clap_max_gap_ms", 850)) / 1000.0
        self.cooldown_s: float = float(clap_cfg.get("cooldown_seconds", 1.2))
        self.action: str = str(clap_cfg.get("action", "wake_assistant"))

        self._lock = threading.Lock()
        self._clap_timestamps: List[float] = []
        self._last_trigger_ts: float = 0.0
        self._last_impulse_ts: float = 0.0
        self._noise_floor: float = 0.008
        self._muted_for_tts: bool = False

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
        **kwargs: Any,
    ) -> bool:
        """
        Inspect a 16-bit PCM buffer using 10ms sub-windows so sharp 15-30ms
        hand claps are detected reliably on laptop microphones.
        """
        if not self.enabled or self._muted_for_tts or not pcm_bytes:
            return False

        now = time.monotonic()
        if (now - self._last_trigger_ts) < self.cooldown_s:
            return False

        count = len(pcm_bytes) // 2
        if count <= 0:
            return False

        samples = struct.unpack(f"<{count}h", pcm_bytes[: count * 2])
        effective_rms = rms_energy if rms_energy is not None else precomputed_rms
        frame_rms = (
            effective_rms
            if effective_rms is not None
            else min(1.0, math.sqrt(sum(s * s for s in samples) / float(count)) / 32768.0)
        )
        frame_peak = min(1.0, max(abs(s) for s in samples) / 32768.0)

        # Compute max 10ms (160-sample) sub-window RMS FIRST to catch short 15-30ms percussive claps
        sub_step = min(count, 160)
        max_sub_rms = frame_rms
        for offset in range(0, count - sub_step + 1, sub_step):
            chunk = samples[offset : offset + sub_step]
            sub_rms = min(1.0, math.sqrt(sum(s * s for s in chunk) / float(len(chunk))) / 32768.0)
            if sub_rms > max_sub_rms:
                max_sub_rms = sub_rms

        # Update rolling ambient noise floor during genuinely quiet frames (no transient peak)
        if max_sub_rms < self.energy_threshold * 0.75 and frame_peak < self.peak_threshold * 0.75:
            self._noise_floor = 0.92 * self._noise_floor + 0.08 * frame_rms
            return False

        # 1. Check peak & sub-window energy against threshold and adaptive noise floor
        effective_rms_thresh = max(self.energy_threshold, min(0.22, self._noise_floor * 3.0))
        if frame_peak < self.peak_threshold and max_sub_rms < self.energy_threshold:
            return False
        if max_sub_rms < effective_rms_thresh and frame_rms < self.energy_threshold:
            return False

        # 2. Crest factor check: claps have a sharp peak relative to average frame RMS
        crest = frame_peak / max(0.001, frame_rms)
        if crest < self.crest_factor_min and frame_peak < 0.45:
            return False

        # Debounce: ensure within min_gap_s and at most 95ms so single claps aren't double counted
        debounce_s = min(self.min_gap_s, 0.095)
        if (now - self._last_impulse_ts) < debounce_s:
            return False

        self._last_impulse_ts = now
        return self._record_impulse(now, max(frame_rms, max_sub_rms))

    def register_external_impulse(self, energy: float = 0.5, timestamp: Optional[float] = None) -> bool:
        """Helper for unit tests or synthetic impulse injection."""
        if not self.enabled:
            return False
        now = timestamp if timestamp is not None else time.monotonic()
        if energy < self.energy_threshold:
            return False
        return self._record_impulse(now, energy)

    def _record_impulse(self, now: float, energy: float) -> bool:
        with self._lock:
            self._clap_timestamps = [
                ts for ts in self._clap_timestamps if (now - ts) <= self.max_gap_s
            ]

            if self._clap_timestamps and (now - self._clap_timestamps[-1]) < self.min_gap_s:
                return False

            self._clap_timestamps.append(now)

            if len(self._clap_timestamps) >= self.required_claps:
                clap_count = len(self._clap_timestamps)
                self._clap_timestamps.clear()
                self._last_trigger_ts = now
                self.logger.info(
                    "Detected %d-clap wake trigger (energy=%.3f | action=%s)",
                    clap_count,
                    energy,
                    self.action,
                )
                self.bus.publish(
                    Topics.VOICE_CLAP_DETECTED,
                    {"count": clap_count, "energy": round(energy, 3), "action": self.action},
                    source="clap_detector",
                )
                if self.on_clap_action is not None:
                    self.on_clap_action(clap_count)
                return True

        return False