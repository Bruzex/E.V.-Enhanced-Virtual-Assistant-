#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Offline Speech-to-Text (Vosk)
=================================================================
Local, privacy-preserving speech recognizer utilizing the lightweight
vosk-model-small-en-us (~50 MB footprint, CPU only).

Features:
  - Streaming audio input via PyAudio with sounddevice fallback.
  - Streaming partial and final transcription hypotheses.
  - Echo-suppression gate: listens to Topics.VOICE_TTS_STARTED and
    Topics.VOICE_TTS_FINISHED to mute mic ingestion while E.V. is speaking.
  - Exposes create_recognizer(), iter_mic_chunks(), feed_pcm_chunk(), and
    listen_once() methods expected by WakeWordLoop and SystemBridge.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Generator, Optional, Tuple

from core.event_bus import Event, EventBus, Topics, get_event_bus

logger = logging.getLogger("ev.voice.stt")


class VoskSTT:
    """Offline STT stream processor powered by Vosk."""

    def __init__(
        self,
        config: Dict[str, Any],
        base_dir: Path,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.bus = event_bus or get_event_bus()
        self.voice_cfg = config.get("voice", {}).get("stt", {})
        self.model_rel = self.voice_cfg.get("model_path", "models/vosk-model-small-en-us")
        self.model_path = (self.base_dir / self.model_rel).resolve()
        self.sample_rate = int(self.voice_cfg.get("sample_rate", 16000))

        self._model: Any = None
        self._recognizer: Any = None
        self._lock = threading.Lock()
        self._muted_for_tts = False

        if self.bus is not None:
            self.bus.subscribe(Topics.VOICE_TTS_STARTED, self._on_tts_started)
            self.bus.subscribe(Topics.VOICE_TTS_FINISHED, self._on_tts_finished)

    def _on_tts_started(self, event: Event) -> None:
        """Echo suppression: mute STT while assistant is speaking."""
        self._muted_for_tts = True
        logger.debug("Echo suppression engaged: STT muted during TTS output.")

    def _on_tts_finished(self, event: Event) -> None:
        """Echo suppression: unmute STT once assistant finishes speaking."""
        self._muted_for_tts = False
        logger.debug("Echo suppression released: STT unmuted.")

    @property
    def is_muted(self) -> bool:
        return self._muted_for_tts

    @property
    def is_ready(self) -> bool:
        """Return True if Vosk model is loaded or available to load."""
        if self._model is not None:
            return True
        return self.is_model_available()

    def is_model_available(self) -> bool:
        """Verify model directory exists."""
        return self.model_path.is_dir()

    def load_model(self) -> bool:
        """Load Vosk model into memory."""
        if self._model is not None:
            return True

        if not self.is_model_available():
            logger.info("Vosk model not found at %s. Standing by for download.", self.model_path)
            return False

        with self._lock:
            if self._model is not None:
                return True
            try:
                from vosk import KaldiRecognizer, Model, SetLogLevel

                SetLogLevel(-1)
                logger.info("Loading Vosk STT model from %s...", self.model_path.name)
                self._model = Model(str(self.model_path))
                self._recognizer = KaldiRecognizer(self._model, self.sample_rate)
                logger.info("Vosk STT ready.")
                return True
            except ImportError:
                logger.warning("vosk package is not installed.")
                return False
            except Exception as exc:
                logger.error("Failed to load Vosk model: %s", exc)
                return False

    def create_recognizer(self) -> Optional[Any]:
        """Create a new KaldiRecognizer instance for a listening session."""
        if not self.load_model():
            return None
        try:
            from vosk import KaldiRecognizer

            return KaldiRecognizer(self._model, self.sample_rate)
        except Exception as exc:
            logger.error("Failed to create KaldiRecognizer: %s", exc)
            return None

    def feed_pcm_chunk(
        self,
        pcm_chunk: bytes,
        recognizer: Optional[Any] = None,
    ) -> Tuple[bool, str]:
        """
        Feed a raw 16-bit mono PCM buffer to the Vosk recognizer.
        Returns (is_final, transcript).
        """
        if self._muted_for_tts:
            return False, ""

        rec = recognizer or self._recognizer
        if rec is None:
            return False, ""

        try:
            if rec.AcceptWaveform(pcm_chunk):
                res = json.loads(rec.Result())
                text = str(res.get("text", "")).strip()
                if text and self.bus is not None:
                    self.bus.publish(
                        Topics.VOICE_FINAL_TRANSCRIPT,
                        {"text": text},
                        source="stt",
                    )
                return True, text
            else:
                partial = json.loads(rec.PartialResult())
                p_text = str(partial.get("partial", "")).strip()
                if p_text and self.bus is not None:
                    self.bus.publish(
                        Topics.VOICE_PARTIAL_TRANSCRIPT,
                        {"text": p_text},
                        source="stt",
                    )
                return False, p_text
        except Exception as exc:
            logger.debug("Error feeding PCM chunk to Vosk: %s", exc)
            return False, ""

    def iter_mic_chunks(
        self,
        stop_check: Optional[Callable[[], bool]] = None,
        on_raw_chunk: Optional[Callable[[bytes], None]] = None,
        chunk_samples: int = 4000,
    ) -> Generator[bytes, None, None]:
        """
        Yield 16-bit mono PCM audio chunks from the microphone.
        Tries PyAudio first, with sounddevice as fallback.
        """
        # Strategy A: PyAudio
        pyaudio_available = False
        try:
            import pyaudio

            pyaudio_available = True
        except ImportError:
            pass

        if pyaudio_available:
            pa: Any = None
            stream: Any = None
            try:
                pa = pyaudio.PyAudio()
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=self.sample_rate,
                    input=True,
                    frames_per_buffer=chunk_samples,
                )
                logger.info("Microphone input opened via PyAudio.")
                while stop_check is None or not stop_check():
                    data = stream.read(chunk_samples, exception_on_overflow=False)
                    if on_raw_chunk is not None:
                        on_raw_chunk(data)
                    if not self._muted_for_tts:
                        yield data
                return
            except Exception as exc:
                logger.debug("PyAudio stream failed or unavailable: %s. Falling back to sounddevice.", exc)
            finally:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                if pa is not None:
                    try:
                        pa.terminate()
                    except Exception:
                        pass

        # Strategy B: sounddevice
        sounddevice_available = False
        try:
            import sounddevice as sd

            sounddevice_available = True
        except ImportError:
            pass

        if sounddevice_available:
            audio_q: queue.Queue[bytes] = queue.Queue()

            def _callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
                audio_q.put(bytes(indata))

            try:
                with sd.RawInputStream(
                    samplerate=self.sample_rate,
                    blocksize=chunk_samples,
                    dtype="int16",
                    channels=1,
                    callback=_callback,
                ):
                    logger.info("Microphone input opened via sounddevice.")
                    while stop_check is None or not stop_check():
                        try:
                            chunk = audio_q.get(timeout=0.15)
                        except queue.Empty:
                            continue
                        if on_raw_chunk is not None:
                            on_raw_chunk(chunk)
                        if not self._muted_for_tts:
                            yield chunk
                    return
            except Exception as exc:
                logger.debug("sounddevice stream failed: %s", exc)

        # Strategy C: Simulated/No-Mic fallback
        logger.warning("No functional audio input device available. Microphone loop standing by.")
        while stop_check is None or not stop_check():
            time.sleep(0.2)

    def listen_once(self, timeout_seconds: float = 6.0) -> str:
        """
        Record a single utterance from the microphone until a final transcript
        is recognized or timeout_seconds elapses.
        """
        if not self.load_model():
            logger.warning("Vosk STT model not loaded; cannot perform live speech recognition.")
            return ""

        rec = self.create_recognizer()
        if rec is None:
            return ""

        deadline = time.monotonic() + timeout_seconds
        last_partial = ""

        for chunk in self.iter_mic_chunks(stop_check=lambda: time.monotonic() > deadline):
            is_final, text = self.feed_pcm_chunk(chunk, recognizer=rec)
            if is_final and text:
                return text
            if text:
                last_partial = text

        return last_partial


# Alias for backward compatibility
SpeechToText = VoskSTT
