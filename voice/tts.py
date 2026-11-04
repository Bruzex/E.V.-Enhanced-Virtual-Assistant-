#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Interruptible Offline Text-to-Speech (`voice/tts.py`)
=========================================================================================
Fast, CPU-optimized neural Text-to-Speech wrapper (Piper TTS + Windows SAPI5) with:
  1. Dual-API Piper WAV Synthesis (`_synthesize_wav_bytes`):
     - Always initializes WAV header (`1` channel, `sampwidth=2`, `framerate` from
       `voice.config.sample_rate` or `22050`) BEFORE calling `synthesize` so
       `wave.close()` never raises `# channels not specified`.
     - Supports both old `synthesize(text, wav_file)` and new generator
       `synthesize(text)` yielding audio chunks (`chunk.audio_int16_bytes` or
       `chunk.audio_int16_array.tobytes()`).
  2. Instant <150ms Barge-In Interruption (`interrupt()`):
     Stops Piper audio playback immediately and calls `brain.cancel()`.
"""

from __future__ import annotations

import collections.abc
import inspect
import io
import logging
import os
import platform
import queue
import re
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.event_bus import Event, EventBus, Topics, get_event_bus

try:
    from piper.voice import PiperVoice  # type: ignore
    PIPER_PYTHON_AVAILABLE = True
except Exception:
    PiperVoice = None  # type: ignore
    PIPER_PYTHON_AVAILABLE = False

try:
    import winsound  # type: ignore
    WINSOUND_AVAILABLE = True
except ImportError:
    winsound = None  # type: ignore
    WINSOUND_AVAILABLE = False

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
    import numpy as np  # type: ignore
    SOUNDDEVICE_AVAILABLE = True
except Exception:
    sd = None  # type: ignore
    np = None  # type: ignore
    SOUNDDEVICE_AVAILABLE = False


class PiperTTS:
    """
    Non-blocking, sentence-chunked TTS engine supporting both old and new PiperVoice
    APIs, instant voice barge-in (`interrupt()` -> stops audio + calls `brain.cancel()`),
    and continuation (`resume()`).
    """

    def __init__(
        self,
        config: Dict[str, Any],
        base_dir: Path,
        event_bus: Optional[EventBus] = None,
        brain: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.bus = event_bus or get_event_bus()
        self.brain = brain
        self.logger = logging.getLogger("ev.tts")
        self.is_windows = platform.system().lower().startswith("win")

        tts_cfg = config.get("voice", {}).get("tts", {})
        rel_model = str(tts_cfg.get("model_path", "models/piper/en_US-lessac-medium.onnx"))
        rel_cfg = str(tts_cfg.get("config_path", f"{rel_model}.json"))

        self.model_path: Path = (
            (base_dir / rel_model).resolve() if not os.path.isabs(rel_model) else Path(rel_model)
        )
        self.config_path: Path = (
            (base_dir / rel_cfg).resolve() if not os.path.isabs(rel_cfg) else Path(rel_cfg)
        )
        self.speaker_id: Optional[int] = tts_cfg.get("speaker_id", 0)
        self.length_scale: float = float(tts_cfg.get("length_scale", 0.95))
        self.noise_scale: float = float(tts_cfg.get("noise_scale", 0.667))
        self.output_device_index: Optional[int] = tts_cfg.get("output_device_index")

        self._voice: Optional[Any] = None
        self._piper_bin: Optional[str] = self._discover_piper_binary()
        self._lock = threading.RLock()
        self._is_speaking = threading.Event()
        self._active_proc: Optional[subprocess.Popen[Any]] = None

        self._interrupted_sentences: List[str] = []
        self._last_full_response: str = ""

        self._queue: "queue.Queue[Optional[tuple[List[str], Optional[int]]]]" = queue.Queue()
        self._stop_event = threading.Event()
        self._abort_current = threading.Event()

        self.bus.subscribe("voice.interrupt", self._on_bus_interrupt)

        self._worker_thread = threading.Thread(
            target=self._tts_worker_loop,
            name="EV-PiperTTS-Worker",
            daemon=True,
        )
        self._worker_thread.start()

    def set_brain(self, brain: Any) -> None:
        self.brain = brain

    def _on_bus_interrupt(self, event: Event) -> None:
        self.interrupt()

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking.is_set()

    @property
    def has_interrupted_speech(self) -> bool:
        with self._lock:
            return len(self._interrupted_sentences) > 0

    def _discover_piper_binary(self) -> Optional[str]:
        candidates = [
            self.base_dir / "models" / "piper" / "piper.exe",
            self.base_dir / "models" / "piper" / "piper" / "piper.exe",
            self.base_dir / "models" / "piper" / "piper",
            self.base_dir / "piper" / "piper.exe",
        ]
        for cand in candidates:
            if cand.exists() and cand.is_file():
                return str(cand.resolve())
        return shutil.which("piper") or shutil.which("piper.exe")

    def load_model(self) -> bool:
        with self._lock:
            if self._voice is not None:
                return True

            self._piper_bin = self._discover_piper_binary()

            if not self.model_path.exists():
                return False

            if PIPER_PYTHON_AVAILABLE and PiperVoice is not None:
                try:
                    cfg_str = str(self.config_path) if self.config_path.exists() else None
                    self._voice = PiperVoice.load(
                        str(self.model_path),
                        config_path=cfg_str,
                        use_cuda=False,
                    )
                    return True
                except Exception as exc:
                    self.logger.error("Failed to load PiperVoice via Python API: %s", exc)

            if self._piper_bin:
                self.logger.info("Using standalone Piper binary at %s with %s", self._piper_bin, self.model_path.name)
                return True

            if self.is_windows:
                return True

            return False

    @staticmethod
    def _split_into_sentences(text: str) -> List[str]:
        cleaned = re.sub(r"\s+", " ", (text or "").strip())
        if not cleaned:
            return []
        parts = re.split(r"(?<=[.!?])\s+", cleaned)
        return [p.strip() for p in parts if p.strip()]

    def speak(
        self,
        text: str,
        output_device_index: Optional[int] = None,
        block: bool = False,
        append: bool = False,
    ) -> None:
        sentences = self._split_into_sentences(text)
        if not sentences:
            return

        if not append and self._is_speaking.is_set():
            self.interrupt()

        with self._lock:
            if append and self._last_full_response:
                self._last_full_response = f"{self._last_full_response} {(text or '').strip()}".strip()
            else:
                self._last_full_response = (text or "").strip()
                self._interrupted_sentences = []

        if block:
            self._synthesize_and_play_sentences(sentences, output_device_index)
        else:
            self._queue.put((sentences, output_device_index))

    def interrupt(self) -> bool:
        was_speaking = self._is_speaking.is_set()
        self._abort_current.set()

        if self.brain is not None and hasattr(self.brain, "cancel"):
            try:
                self.brain.cancel()
            except Exception:
                pass

        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

        if SOUNDDEVICE_AVAILABLE and sd is not None:
            try:
                sd.stop()
            except Exception:
                pass
        if WINSOUND_AVAILABLE and winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        if self._active_proc is not None and self._active_proc.poll() is None:
            try:
                self._active_proc.terminate()
            except Exception:
                pass
        return was_speaking

    def resume(self, block: bool = False) -> Tuple[bool, str]:
        with self._lock:
            if self._interrupted_sentences:
                remaining = list(self._interrupted_sentences)
                self._interrupted_sentences.clear()
            elif self._last_full_response:
                remaining = self._split_into_sentences(self._last_full_response)
            else:
                return False, "There wasn't anything pending to continue."

        joined = " ".join(remaining)
        if block:
            self._synthesize_and_play_sentences(remaining, self.output_device_index)
        else:
            self._queue.put((remaining, self.output_device_index))
        return True, joined

    def stop(self) -> None:
        self.interrupt()

    def shutdown(self) -> None:
        self.interrupt()
        self._stop_event.set()
        self._queue.put(None)
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.5)

    # =========================================================================
    # Internal Sentence-Chunked Playback Loop
    # =========================================================================
    def _tts_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.3)
            except queue.Empty:
                continue

            if item is None:
                self._queue.task_done()
                break

            sentences, dev_idx = item
            try:
                self._synthesize_and_play_sentences(sentences, dev_idx)
            except Exception as exc:
                self.logger.error("TTS worker error: %s", exc, exc_info=True)
            finally:
                self._queue.task_done()

    def _synthesize_and_play_sentences(
        self,
        sentences: List[str],
        output_device_index: Optional[int] = None,
    ) -> None:
        self._abort_current.clear()
        self._is_speaking.set()
        dev = output_device_index if output_device_index is not None else self.output_device_index
        full_text = " ".join(sentences)
        estimated_ms = max(2000, min(14000, int(len(full_text) * 65)))

        self.bus.publish(Topics.VOICE_TTS_STARTED, {"text": full_text}, source="tts")
        self.bus.publish(Topics.ORB_STATE, {"state": "speaking", "progress": 0.0}, source="tts")
        self.bus.publish(
            Topics.CAPTION,
            {"speaker": "ev", "text": full_text, "hold_ms": estimated_ms + 500},
            source="tts",
        )

        try:
            for idx, sentence in enumerate(sentences):
                if self._abort_current.is_set():
                    with self._lock:
                        self._interrupted_sentences = sentences[idx:]
                    break

                t0 = time.monotonic()
                played = False
                wav_bytes = self._synthesize_wav_bytes(sentence)
                if wav_bytes and not self._abort_current.is_set():
                    played = self._play_wav_bytes(wav_bytes, dev)

                if not played and self.is_windows and not self._abort_current.is_set():
                    played = self._speak_windows_sapi5(sentence)

                if not played and not self._abort_current.is_set():
                    sent_ms = max(600, int(len(sentence) * 55))
                    target_s = sent_ms / 1000.0
                    while (time.monotonic() - t0) < target_s and not self._abort_current.is_set():
                        time.sleep(0.025)

                if self._abort_current.is_set():
                    with self._lock:
                        self._interrupted_sentences = sentences[idx:]
                    break
        finally:
            self._is_speaking.clear()
            self.bus.publish(Topics.VOICE_TTS_FINISHED, {"text": full_text}, source="tts")
            self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="tts")

    @staticmethod
    def _write_piper_chunks_to_wav(chunks: Any, wav_file: wave.Wave_write) -> None:
        """Write yielded Piper AudioChunk items into an open `wave.Wave_write`."""
        if chunks is None or not isinstance(chunks, collections.abc.Iterable):
            return
        for chunk in chunks:
            pcm = getattr(chunk, "audio_int16_bytes", None)
            if pcm is None and hasattr(chunk, "audio_int16_array"):
                arr = getattr(chunk, "audio_int16_array")
                if hasattr(arr, "tobytes"):
                    pcm = arr.tobytes()
            elif pcm is None and isinstance(chunk, (bytes, bytearray)):
                pcm = bytes(chunk)
            if pcm:
                wav_file.writeframes(pcm)

    def _synthesize_wav_bytes(self, text: str) -> Optional[bytes]:
        if not self.load_model():
            return None

        with self._lock:
            if self._voice is not None:
                buf = io.BytesIO()
                wav_file = wave.open(buf, "wb")
                try:
                    voice_cfg = getattr(self._voice, "config", None)
                    sample_rate = int(getattr(voice_cfg, "sample_rate", 22050) or 22050)
                    # Always set WAV header BEFORE synthesize so wave.close() never fails
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(sample_rate)

                    synth_fn = self._voice.synthesize
                    params: List[str] = []
                    try:
                        params = list(inspect.signature(synth_fn).parameters.keys())
                    except Exception:
                        params = []

                    takes_wav_file = any(p in ("wav_file", "wf") for p in params) or len(params) >= 2
                    if takes_wav_file and "syn_config" not in params:
                        try:
                            res = synth_fn(
                                text,
                                wav_file,
                                speaker_id=self.speaker_id,
                                length_scale=self.length_scale,
                                noise_scale=self.noise_scale,
                            )
                        except TypeError:
                            try:
                                res = synth_fn(text, wav_file)
                            except TypeError:
                                res = synth_fn(text)
                        self._write_piper_chunks_to_wav(res, wav_file)
                    else:
                        res = synth_fn(text)
                        self._write_piper_chunks_to_wav(res, wav_file)

                    nframes = wav_file.getnframes()
                except Exception as exc:
                    self.logger.error("PiperVoice synthesis error: %s", exc)
                    nframes = 0
                finally:
                    try:
                        wav_file.close()
                    except Exception:
                        pass

                if nframes > 0:
                    return buf.getvalue()

            if self._piper_bin and self.model_path.exists():
                try:
                    cmd = [
                        self._piper_bin,
                        "--model",
                        str(self.model_path),
                        "--output_raw",
                    ]
                    if self.config_path.exists():
                        cmd.extend(["--config", str(self.config_path)])
                    self._active_proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                        cwd=str(Path(self._piper_bin).parent),
                    )
                    stdout_bytes, _ = self._active_proc.communicate(input=text.encode("utf-8"), timeout=10)
                    if self._abort_current.is_set():
                        return None
                    if self._active_proc.returncode == 0 and stdout_bytes:
                        buf = io.BytesIO()
                        wf = wave.open(buf, "wb")
                        try:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(22050)
                            wf.writeframes(stdout_bytes)
                        finally:
                            wf.close()
                        return buf.getvalue()
                except Exception as exc:
                    self.logger.error("Piper CLI synthesis error: %s", exc)
                finally:
                    self._active_proc = None

        return None

    def _play_wav_bytes(self, wav_bytes: bytes, device_index: Optional[int]) -> bool:
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                rate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
        except Exception as exc:
            self.logger.error("Invalid WAV buffer from Piper: %s", exc)
            return False

        if SOUNDDEVICE_AVAILABLE and sd is not None and np is not None:
            try:
                audio_np = np.frombuffer(frames, dtype=np.int16)
                sd.play(audio_np, samplerate=rate, device=device_index, blocking=False)
                duration_s = len(audio_np) / float(max(1, rate * channels))
                t0 = time.monotonic()
                while (time.monotonic() - t0) < duration_s:
                    if self._abort_current.is_set():
                        sd.stop()
                        return True
                    time.sleep(0.025)
                sd.wait()
                return True
            except Exception as exc:
                self.logger.warning("SoundDevice playback failed, trying winsound: %s", exc)

        if WINSOUND_AVAILABLE and winsound is not None and device_index is None:
            try:
                duration_s = len(frames) / float(max(1, rate * channels * sampwidth))
                winsound.PlaySound(wav_bytes, winsound.SND_MEMORY | winsound.SND_ASYNC)
                t0 = time.monotonic()
                while (time.monotonic() - t0) < duration_s:
                    if self._abort_current.is_set():
                        winsound.PlaySound(None, winsound.SND_PURGE)
                        return True
                    time.sleep(0.025)
                return True
            except Exception as exc:
                self.logger.warning("winsound playback failed: %s", exc)

        if PYAUDIO_AVAILABLE and pyaudio is not None:
            pa = pyaudio.PyAudio()
            stream = None
            try:
                kwargs: Dict[str, Any] = {
                    "format": pa.get_format_from_width(sampwidth),
                    "channels": channels,
                    "rate": rate,
                    "output": True,
                }
                if device_index is not None:
                    kwargs["output_device_index"] = int(device_index)
                stream = pa.open(**kwargs)
                step = 1024
                for offset in range(0, len(frames), step):
                    if self._abort_current.is_set():
                        break
                    stream.write(frames[offset : offset + step])
                return True
            except Exception:
                return False
            finally:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                pa.terminate()

        return False

    def _speak_windows_sapi5(self, text: str) -> bool:
        safe_text = text.replace("'", " ").replace('"', " ")
        ps_cmd = (
            "Add-Type -AssemblyName System.Speech; "
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$synth.Rate = 1; "
            f"$synth.Speak('{safe_text}')"
        )
        try:
            self._active_proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            while self._active_proc.poll() is None:
                if self._abort_current.is_set():
                    self._active_proc.terminate()
                    return True
                time.sleep(0.03)
            return True
        except Exception:
            return False
        finally:
            self._active_proc = None