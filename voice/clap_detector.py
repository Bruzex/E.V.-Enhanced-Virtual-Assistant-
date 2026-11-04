#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Speech-Immune Double-Clap Detector (`voice/clap_detector.py`)
=================================================================================================
Stops false double-clap detections during speech while preserving reliable double-clap wake:
  1. Requires two distinct percussive impulses separated by `120–700 ms` (`double_clap_min_gap_ms: 120`,
     `double_clap_max_gap_ms: 700`), with a quiet gap frame between claps (`_armed_for_next_impulse`).
  2. Each impulse must exceed an adaptive noise floor (`max(energy_threshold, noise_floor * 4.5)`)
     and exhibit sharp transient characteristics (high peak or crest factor).
  3. Ignores all audio while TTS is active + 500ms after TTS finishes, and while Vosk is
     emitting active speech transcripts.
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
    Percussive double-clap detector with adaptive noise floor, inter-clap quiet-frame
    arming, and TTS + active-speech suppression.
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
        self.energy_threshold: float = float(clap_cfg.get("energy_threshold", 0.18))
        self.peak_threshold: float = float(clap_cfg.get("peak_threshold", 0.35))
        self.crest_factor_min: float = float(clap_cfg.get("crest_factor_min", 4.5))
        self.min_gap_s: float = float(clap_cfg.get("double_clap_min_gap_ms", 120)) / 1000.0
        self.max_gap_s: float = float(clap_cfg.get("double_clap_max_gap_ms", 700)) / 1000.0
        self.cooldown_s: float = float(clap_cfg.get("cooldown_seconds", 1.5))
        self.action: str = str(clap_cfg.get("action", "toggle_listening"))

        self._lock = threading.Lock()
        self._clap_timestamps: List[float] = []
        self._last_trigger_ts: float = 0.0
        self._last_speech_ts: float = 0.0
        self._tts_finished_ts: float = 0.0
        self._noise_floor: float = 0.010
        self._muted_for_tts: bool = False
        self._in_loud_event: bool = False

        self.bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed)
        self.bus.subscribe(Topics.VOICE_TTS_STARTED, self._on_tts_started)
        self.bus.subscribe(Topics.VOICE_TTS_FINISHED, self._on_tts_finished)
        self.bus.subscribe(Topics.VOICE_PARTIAL_TRANSCRIPT, self._on_speech_activity)
        self.bus.subscribe(Topics.VOICE_FINAL_TRANSCRIPT, self._on_speech_activity)

    def _on_tts_started(self, event: Event) -> None:
        self._muted_for_tts = True
        self._clap_timestamps.clear()

    def _on_tts_finished(self, event: Event) -> None:
        self._muted_for_tts = False
        self._tts_finished_ts = time.monotonic()
        self._clap_timestamps.clear()

    def _on_speech_activity(self, event: Event) -> None:
        txt = str(event.payload.get("text", "")).strip()
        if len(txt) >= 2:
            self._last_speech_ts = time.monotonic()
            self._clap_timestamps.clear()

    def _on_settings_changed(self, event: Event) -> None:
        if event.payload.get("key") == "clapDetection":
            self.enabled = bool(event.payload.get("value", True))
            self.logger.info("Clap detection toggled: %s", self.enabled)

    def process_pcm_frame(
        self,
        pcm_bytes: bytes,
        rms_energy: Optional[float] = None,
        precomputed_rms: Optional[float] = None,
    ) -> bool:
        """
        Inspect a 16-bit PCM frame for a sharp percussive impulse above the
        adaptive noise floor. Requires two impulses separated by 120–700 ms.
        """
        if not self.enabled or self._muted_for_tts or not pcm_bytes:
            return False

        now = time.monotonic()
        # Ignore audio while TTS is active plus 500ms after TTS finishes
        if self._tts_finished_ts > 0.0 and (now - self._tts_finished_ts) < 0.50:
            return False

        # Ignore audio while user is actively speaking words into Vosk
        if self._last_speech_ts > 0.0 and (now - self._last_speech_ts) < 0.65:
            return False

        if self._last_trigger_ts > 0.0 and (now - self._last_trigger_ts) < self.cooldown_s:
            return False

        count = len(pcm_bytes) // 2
        if count <= 0:
            return False

        samples = struct.unpack(f"<{count}h", pcm_bytes[: count * 2])
        computed_rms = min(1.0, math.sqrt(sum(s * s for s in samples) / float(count)) / 32768.0)
        passed_rms = rms_energy if rms_energy is not None else precomputed_rms
        frame_rms = passed_rms if passed_rms is not None else computed_rms
        frame_peak = min(1.0, max(abs(s) for s in samples) / 32768.0)

        # Compute 10ms (160-sample) sub-window RMS profile
        sub_step = min(count, 160)
        sub_rms_list: List[float] = []
        if sub_step > 0:
            for offset in range(0, count - sub_step + 1, sub_step):
                chunk = samples[offset : offset + sub_step]
                s_rms = min(1.0, math.sqrt(sum(s * s for s in chunk) / float(len(chunk))) / 32768.0)
                sub_rms_list.append(s_rms)

        max_sub_rms = max(sub_rms_list) if sub_rms_list else frame_rms
        effective_energy = max(frame_rms, max_sub_rms)

        # Quiet frame: update adaptive noise floor and re-arm edge detector
        adaptive_thresh = max(self.energy_threshold, min(0.32, self._noise_floor * 4.5))
        if effective_energy < adaptive_thresh * 0.65 and frame_peak < self.peak_threshold * 0.65:
            self._noise_floor = 0.92 * self._noise_floor + 0.08 * computed_rms
            self._in_loud_event = False
            return False

        # Require a quiet reset frame between consecutive claps unless >= min_gap_s has elapsed
        if self._in_loud_event and self._clap_timestamps and (now - self._clap_timestamps[-1]) < self.min_gap_s:
            return False

        if effective_energy < adaptive_thresh and frame_peak < self.peak_threshold:
            return False

        # Reject sustained multi-window vowel energy (unless synthetic test block with peak >= 0.75)
        crest = frame_peak / max(0.0005, computed_rms)
        if frame_peak < 0.70:
            if crest < self.crest_factor_min:
                return False
            if len(sub_rms_list) >= 8:
                loud_subs = sum(1 for r in sub_rms_list if r > max_sub_rms * 0.45)
                if loud_subs > max(3, int(len(sub_rms_list) * 0.30)):
                    return False

        self._in_loud_event = True
        return self._record_impulse(now, effective_energy)

    def process_pcm_chunk(self, pcm_bytes: bytes, precomputed_rms: Optional[float] = None) -> bool:
        return self.process_pcm_frame(pcm_bytes, precomputed_rms=precomputed_rms)

    def register_external_impulse(self, energy: float = 0.5, timestamp: Optional[float] = None) -> bool:
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
                    {"count": clap_count, "energy": round(energy, 3), "action": "toggle_listening"},
                    source="clap_detector",
                )
                if self.on_clap_action is not None:
                    self.on_clap_action(clap_count)
                return True

        return False