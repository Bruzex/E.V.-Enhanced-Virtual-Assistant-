#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Offline Speech-to-Text (Vosk)
=================================================================
Lightweight (~50 MB RAM) offline speech recognition engine powered by Vosk
(`vosk-model-small-en-us`) and PyAudio / sounddevice.

Key Features:
  - Echo Suppression Gate: Automatically pauses microphone capture while Piper
    TTS is speaking so E.V. never transcribes its own voice.
  - Streaming Partial & Final Hypotheses: Publishes `VOICE_PARTIAL_TRANSCRIPT`
    for live HUD feedback and `VOICE_FINAL_TRANSCRIPT` when an utterance completes.
  - Graceful Degradation: If no microphone or Vosk model directory is present,
    marks the HUD `mic` indicator accordingly without crashing the application.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.event_bus import EventBus, Topics, get_event_bus

VOSK_IMPORT_ERROR: Optional[str] = None
try:
    from vosk import KaldiRecognizer, Model, SetLogLevel  # type: ignore
    SetLogLevel(-1)
    VOSK_AVAILABLE = True
except Exception as _vosk_exc:
    KaldiRecognizer = None  # type: ignore
    Model = None  # type: ignore
    VOSK_AVAILABLE = False
    VOSK_IMPORT_ERROR = str(_vosk_exc)

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


class VoskSTT:
    """
    Offline Vosk Speech-to-Text engine supporting both single-utterance capture
    and continuous streaming recognition for the Wake-Word / Command loop.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        base_dir: Path,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.bus = event_bus or get_event_bus()
        self.logger = logging.getLogger("ev.stt")

        stt_cfg = config.get("voice", {}).get("stt", {})
        rel_model = str(stt_cfg.get("model_path", "models/vosk-model-small-en-us"))
        self.model_path: Path = (
            (base_dir / rel_model).resolve() if not os.path.isabs(rel_model) else Path(rel_model)
        )
        self.sample_rate: int = int(stt_cfg.get("sample_rate", 16000))
        self.chunk_size: int = int(stt_cfg.get("chunk_size", 4000))
        self.device_index: Optional[int] = stt_cfg.get("input_device_index")

        self._model: Optional[Any] = None
        self._recognizer: Optional[Any] = None
        self._lock = threading.RLock()
        self._muted_for_tts = threading.Event()
        self._audio_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=64)

        # Automatically mute STT while E.V. is speaking to prevent self-triggering
        self.bus.subscribe(Topics.VOICE_TTS_STARTED, lambda _: self.set_tts_mute(True))
        self.bus.subscribe(Topics.VOICE_TTS_FINISHED, lambda _: self.set_tts_mute(False))

    @property
    def is_muted(self) -> bool:
        return self._muted_for_tts.is_set()

    @property
    def is_ready(self) -> bool:
        return self._model is not None and (PYAUDIO_AVAILABLE or SOUNDDEVICE_AVAILABLE)

    def set_tts_mute(self, muted: bool) -> None:
        """Pause or resume microphone ingestion while TTS audio is playing."""
        if muted:
            self._muted_for_tts.set()
            self._drain_queue()
        else:
            self._muted_for_tts.clear()

    def _drain_queue(self) -> None:
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def load_model(self) -> bool:
        """Initialize the Vosk acoustic model (~50 MB footprint)."""
        with self._lock:
            if self._model is not None:
                return True

            if not VOSK_AVAILABLE:
                self.logger.warning(
                    "Vosk library could not be loaded (%s). Voice STT running in simulated/GUI mode.",
                    VOSK_IMPORT_ERROR or "package not installed",
                )
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "standby"}, source="stt")
                return False

            if not self.model_path.exists():
                self.logger.warning(
                    "Vosk model directory not found at %s. Download vosk-model-small-en-us there for live mic STT.",
                    self.model_path,
                )
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "standby"}, source="stt")
                return False

            try:
                self.logger.info("Loading Vosk offline STT model from %s...", self.model_path)
                self._model = Model(str(self.model_path))
                self._recognizer = KaldiRecognizer(self._model, self.sample_rate)
                self._recognizer.SetWords(True)
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "online"}, source="stt")
                return True
            except Exception as exc:
                self.logger.error("Failed to load Vosk model: %s", exc)
                self._model = None
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "offline"}, source="stt")
                return False

    def create_recognizer(self, grammar: Optional[list[str]] = None) -> Optional[Any]:
        """Create a dedicated KaldiRecognizer instance (optionally constrained to a phrase list)."""
        if not self.load_model() or self._model is None:
            return None
        if grammar:
            return KaldiRecognizer(self._model, self.sample_rate, json.dumps(grammar))
        rec = KaldiRecognizer(self._model, self.sample_rate)
        rec.SetWords(True)
        return rec

    def feed_pcm_chunk(self, pcm_bytes: bytes, recognizer: Optional[Any] = None) -> tuple[bool, str]:
        """
        Feed a raw 16-bit mono PCM chunk into Vosk.
        Returns `(is_final, text)`.
        """
        if self._muted_for_tts.is_set() or not pcm_bytes:
            return False, ""

        rec = recognizer or self._recognizer
        if rec is None:
            return False, ""

        try:
            if rec.AcceptWaveform(pcm_bytes):
                res = json.loads(rec.Result() or "{}")
                text = str(res.get("text", "")).strip()
                if text:
                    self.bus.publish(
                        Topics.VOICE_FINAL_TRANSCRIPT,
                        {"text": text, "confidence": 1.0},
                        source="stt",
                    )
                return True, text
            else:
                part = json.loads(rec.PartialResult() or "{}")
                partial_text = str(part.get("partial", "")).strip()
                if partial_text:
                    self.bus.publish(
                        Topics.VOICE_PARTIAL_TRANSCRIPT,
                        {"text": partial_text},
                        source="stt",
                    )
                return False, partial_text
        except Exception as exc:
            self.logger.debug("Vosk recognition error: %s", exc)
            return False, ""

    def listen_once(self, timeout_seconds: float = 6.0) -> str:
        """
        Capture a single spoken command from the microphone up to `timeout_seconds`.
        Returns the final transcribed string (or empty string on silence/timeout).
        """
        if not self.load_model():
            return ""

        rec = self.create_recognizer()
        if rec is None:
            return ""

        self.bus.publish(Topics.VOICE_LISTENING_STARTED, {}, source="stt")
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        last_partial = ""
        final_text = ""

        try:
            for pcm_chunk in self.iter_mic_chunks(stop_check=lambda: time.monotonic() >= deadline):
                is_final, text = self.feed_pcm_chunk(pcm_chunk, recognizer=rec)
                if is_final and text:
                    final_text = text
                    break
                elif text:
                    last_partial = text

            if not final_text:
                flush = json.loads(rec.FinalResult() or "{}")
                final_text = str(flush.get("text", "")).strip() or last_partial
        finally:
            self.bus.publish(Topics.VOICE_LISTENING_STOPPED, {}, source="stt")

        return final_text

    def iter_mic_chunks(
        self,
        stop_check: Callable[[], bool],
        on_raw_chunk: Optional[Callable[[bytes], None]] = None,
    ):
        """
        Generator yielding 16-bit 16kHz mono PCM byte chunks from either PyAudio
        or sounddevice until `stop_check()` returns True.
        """
        if PYAUDIO_AVAILABLE:
            pa = pyaudio.PyAudio()
            stream = None
            try:
                kwargs: Dict[str, Any] = {
                    "format": pyaudio.paInt16,
                    "channels": 1,
                    "rate": self.sample_rate,
                    "input": True,
                    "frames_per_buffer": self.chunk_size,
                }
                if self.device_index is not None:
                    kwargs["input_device_index"] = int(self.device_index)
                stream = pa.open(**kwargs)
                while not stop_check():
                    data = stream.read(self.chunk_size, exception_on_overflow=False)
                    if on_raw_chunk is not None:
                        on_raw_chunk(data)
                    yield data
            except Exception as exc:
                self.logger.error("PyAudio microphone stream error: %s", exc)
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "offline"}, source="stt")
            finally:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                pa.terminate()

        elif SOUNDDEVICE_AVAILABLE:
            q: "queue.Queue[bytes]" = queue.Queue()

            def _sd_callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
                q.put(bytes(indata))

            try:
                with sd.RawInputStream(
                    samplerate=self.sample_rate,
                    blocksize=self.chunk_size,
                    device=self.device_index,
                    dtype="int16",
                    channels=1,
                    callback=_sd_callback,
                ):
                    while not stop_check():
                        try:
                            data = q.get(timeout=0.25)
                            if on_raw_chunk is not None:
                                on_raw_chunk(data)
                            yield data
                        except queue.Empty:
                            continue
            except Exception as exc:
                self.logger.error("SoundDevice microphone stream error: %s", exc)
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "offline"}, source="stt")
        else:
            self.logger.warning("Neither PyAudio nor sounddevice is available for live microphone capture.")
            while not stop_check():
                time.sleep(0.25)
                break