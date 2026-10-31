#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — VB-Audio Virtual Cable Call Routing
=======================================================================
Enables E.V.'s Piper TTS voice to be injected directly into live phone calls
(via Android Scrcpy / Phone Link / ADB audio bridge) or WhatsApp desktop calls
using VB-Audio Virtual Cable (`CABLE Input (VB-Audio Virtual Cable)`).

How It Works:
  1. Device Discovery (`discover_virtual_cable()`):
     Enumerates system output & input devices via `PyAudio` or `sounddevice`
     and matches the configured hint (`CABLE Input (VB-Audio Virtual Cable)`).
  2. Call Routing (`start_call_routing()` / `stop_call_routing()`):
     When a phone call connects on the HUD (`CallOverlay` status == `connected`),
     `AudioRouter` switches `PiperTTS.output_device_index` to the Virtual Cable
     input device (or plays simultaneously to speakers + virtual cable) and
     restores the normal desktop speaker device as soon as the call ends.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional, Tuple

from core.event_bus import EventBus, get_event_bus

try:
    import pyaudio  # type: ignore
    _pa_test = pyaudio.PyAudio()
    _pa_test.terminate()
    PYAUDIO_AVAILABLE = True
except Exception:
    pyaudio = None  # type: ignore
    PYAUDIO_AVAILABLE = False

try:
    import sounddevice as sd  # type: ignore
    SOUNDDEVICE_AVAILABLE = True
except ImportError:
    sd = None  # type: ignore
    SOUNDDEVICE_AVAILABLE = False


class AudioRouter:
    """
    Manages VB-Audio Virtual Cable discovery and live call audio injection
    for `PiperTTS`.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        tts: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.config = config or {}
        self.tts = tts
        self.bus = event_bus or get_event_bus()
        self.logger = logging.getLogger("ev.audio_routing")

        ar_cfg = self.config.get("remote", {}).get("audio_routing", {})
        self.enabled: bool = bool(ar_cfg.get("enabled", True))
        self.cable_input_hint: str = str(
            ar_cfg.get("virtual_cable_input_Hint")
            or ar_cfg.get("virtual_cable_input_hint")
            or "CABLE Input"
        )
        self.cable_output_hint: str = str(
            ar_cfg.get("virtual_cable_output_hint", "CABLE Output")
        )

        self._lock = threading.RLock()
        self._default_tts_device: Optional[int] = (
            tts.output_device_index if tts is not None else None
        )
        self._virtual_cable_out_idx: Optional[int] = None
        self._virtual_cable_in_idx: Optional[int] = None
        self._routing_active: bool = False

        self.discover_virtual_cable()

    @property
    def is_routing_active(self) -> bool:
        with self._lock:
            return self._routing_active

    @property
    def virtual_cable_device_index(self) -> Optional[int]:
        with self._lock:
            return self._virtual_cable_out_idx

    def discover_virtual_cable(self) -> Tuple[Optional[int], Optional[int]]:
        """
        Scan audio devices for VB-Audio Virtual Cable playback (`CABLE Input`)
        and recording (`CABLE Output`) endpoints.
        Returns `(playback_device_index, recording_device_index)`.
        """
        if not self.enabled:
            return None, None

        out_idx: Optional[int] = None
        in_idx: Optional[int] = None
        hint_out = self.cable_input_hint.lower()
        hint_in = self.cable_output_hint.lower()

        if PYAUDIO_AVAILABLE and pyaudio is not None:
            pa = pyaudio.PyAudio()
            try:
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    name = str(info.get("name", "")).lower()
                    max_out = int(info.get("maxOutputChannels", 0))
                    max_in = int(info.get("maxInputChannels", 0))
                    if max_out > 0 and (hint_out in name or "vb-audio" in name or "cable input" in name):
                        out_idx = i
                    if max_in > 0 and (hint_in in name or "vb-audio" in name or "cable output" in name):
                        in_idx = i
            except Exception as exc:
                self.logger.debug("PyAudio device scan error: %s", exc)
            finally:
                pa.terminate()

        elif SOUNDDEVICE_AVAILABLE and sd is not None:
            try:
                devices = sd.query_devices()
                for i, info in enumerate(devices):
                    name = str(info.get("name", "")).lower()
                    max_out = int(info.get("max_output_channels", 0))
                    max_in = int(info.get("max_input_channels", 0))
                    if max_out > 0 and (hint_out in name or "vb-audio" in name or "cable input" in name):
                        out_idx = i
                    if max_in > 0 and (hint_in in name or "vb-audio" in name or "cable output" in name):
                        in_idx = i
            except Exception as exc:
                self.logger.debug("SoundDevice scan error: %s", exc)

        with self._lock:
            self._virtual_cable_out_idx = out_idx
            self._virtual_cable_in_idx = in_idx

        if out_idx is not None:
            self.logger.info(
                "Discovered VB-Audio Virtual Cable playback endpoint at device index %d",
                out_idx,
            )
        else:
            self.logger.info(
                "VB-Audio Virtual Cable not detected; call audio will play through default system speakers."
            )
        return out_idx, in_idx

    def start_call_routing(self) -> bool:
        """
        Route E.V.'s Piper TTS output into VB-Audio Virtual Cable (`CABLE Input`)
        so the remote caller hears E.V. directly.
        """
        with self._lock:
            self._routing_active = True
            if self.tts is not None and self._virtual_cable_out_idx is not None:
                self._default_tts_device = self.tts.output_device_index
                self.tts.output_device_index = self._virtual_cable_out_idx
                self.logger.info(
                    "Call audio routing ACTIVE -> VB-Audio device index %d",
                    self._virtual_cable_out_idx,
                )
                return True
            self.logger.info("Call audio routing ACTIVE (using default speaker output).")
            return False

    def stop_call_routing(self) -> None:
        """Restore Piper TTS output to the normal desktop speaker device."""
        with self._lock:
            self._routing_active = False
            if self.tts is not None:
                self.tts.output_device_index = self._default_tts_device
            self.logger.info("Call audio routing STOPPED; restored standard speaker output.")

    def list_audio_devices(self) -> List[Dict[str, Any]]:
        """Return a diagnostic list of available audio devices."""
        results: List[Dict[str, Any]] = []
        if PYAUDIO_AVAILABLE and pyaudio is not None:
            pa = pyaudio.PyAudio()
            try:
                for i in range(pa.get_device_count()):
                    info = pa.get_device_info_by_index(i)
                    results.append(
                        {
                            "index": i,
                            "name": str(info.get("name", "")),
                            "max_in": int(info.get("maxInputChannels", 0)),
                            "max_out": int(info.get("maxOutputChannels", 0)),
                        }
                    )
            finally:
                pa.terminate()
        return results

# Backward compatibility alias
AudioRoutingController = AudioRouter
