#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Offline Text-to-Speech (Piper TTS + Windows SAPI5)
======================================================================================
Fast, CPU-optimized neural Text-to-Speech wrapper using Piper TTS with automatic
local Windows binary discovery and Windows SAPI5 offline voice fallback.

Supports 3 Synthesis Routes (in priority order):
  1. Native Python `piper.PiperVoice` ONNX runtime (if `piper-tts` Python wheel is installed).
  2. Standalone Windows/Linux `piper` CLI binary:
     Automatically checks `models/piper/piper.exe`, `models/piper/piper/piper.exe`,
     and system `PATH` — so `piper_windows_amd64.zip` works out-of-the-box on any
     Python version (including Python 3.12–3.14) with zero C++ compilation!
  3. Windows SAPI5 Offline Speech Synthesizer fallback (`System.Speech.Synthesis`):
     Guarantees E.V. ALWAYS speaks out loud through your speakers on Windows even
     before `piper.exe` or `piper-tts` is installed.

Supports 3 Audio Playback Routes:
  1. `sounddevice` (with `numpy`)
  2. `PyAudio`
  3. Built-in Windows `winsound.PlaySound` (zero external dependencies)
"""

from __future__ import annotations

import io
import logging
import os
import platform
import queue
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any, Dict, Optional

from core.event_bus import EventBus, Topics, get_event_bus

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
    Non-blocking background TTS worker queue that synthesizes speech with Piper
    (or Windows offline SAPI5 fallback) and drives the HUD Status Orb `speaking`
    state + caption display.
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

        # Asynchronous speech queue so `speak()` never blocks the caller or GUI
        self._queue: "queue.Queue[Optional[tuple[str, Optional[int], bool]]]" = queue.Queue()
        self._stop_event = threading.Event()
        self._abort_current = threading.Event()
        self._worker_thread = threading.Thread(
            target=self._tts_worker_loop,
            name="EV-PiperTTS-Worker",
            daemon=True,
        )
        self._worker_thread.start()

    def _discover_piper_binary(self) -> Optional[str]:
        """Locate standalone `piper.exe` / `piper` binary in `models/piper/` or system PATH."""
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
        """Load the Piper ONNX voice model via Python API or standalone `piper.exe` binary."""
        with self._lock:
            if self._voice is not None:
                return True

            # Refresh binary discovery in case user just extracted piper_windows_amd64.zip
            self._piper_bin = self._discover_piper_binary()

            if not self.model_path.exists():
                self.logger.info(
                    "Piper ONNX model not found at %s; will use Windows offline SAPI5 voice fallback.",
                    self.model_path,
                )
                return False

            if PIPER_PYTHON_AVAILABLE and PiperVoice is not None:
                try:
                    self.logger.info("Loading Piper neural TTS voice from %s...", self.model_path.name)
                    cfg_str = str(self.config_path) if self.config_path.exists() else None
                    self._voice = PiperVoice.load(
                        str(self.model_path),
                        config_path=cfg_str,
                        use_cuda=False,  # STRICT CPU-ONLY
                    )
                    return True
                except Exception as exc:
                    self.logger.error("Failed to load PiperVoice via Python API: %s", exc)

            if self._piper_bin:
                self.logger.info("Using standalone Piper binary at %s with %s", self._piper_bin, self.model_path.name)
                return True

            if self.is_windows:
                self.logger.info(
                    "Piper Python wheel / piper.exe not yet present; Windows offline SAPI5 voice active."
                )
                return True

            return False

    def speak(
        self,
        text: str,
        output_device_index: Optional[int] = None,
        block: bool = False,
    ) -> None:
        """
        Queue `text` to be spoken by E.V.
        If `block=True`, waits until playback completes; otherwise returns immediately.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return

        if block:
            self._synthesize_and_play(cleaned, output_device_index)
        else:
            self._queue.put((cleaned, output_device_index, False))

    def stop(self) -> None:
        """Interrupt active speech playback and clear pending utterances."""
        self._abort_current.set()
        if WINSOUND_AVAILABLE and winsound is not None:
            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

    def shutdown(self) -> None:
        """Stop the background TTS worker thread cleanly."""
        self.stop()
        self._stop_event.set()
        self._queue.put(None)
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.5)

    # =========================================================================
    # Internal Synthesis & Playback Pipeline
    # =========================================================================
    def _tts_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                self._queue.task_done()
                break

            text, dev_idx, _ = item
            try:
                self._synthesize_and_play(text, dev_idx)
            except Exception as exc:
                self.logger.error("TTS worker error: %s", exc, exc_info=True)
            finally:
                self._queue.task_done()

    def _synthesize_and_play(self, text: str, output_device_index: Optional[int] = None) -> None:
        self._abort_current.clear()
        dev = output_device_index if output_device_index is not None else self.output_device_index

        estimated_ms = max(2000, min(12000, int(len(text) * 65)))

        self.bus.publish(Topics.VOICE_TTS_STARTED, {"text": text}, source="tts")
        self.bus.publish(Topics.ORB_STATE, {"state": "speaking", "progress": 0.0}, source="tts")
        self.bus.publish(
            Topics.CAPTION,
            {"speaker": "ev", "text": text, "hold_ms": estimated_ms + 400},
            source="tts",
        )

        t0 = time.monotonic()
        played_audio = False

        try:
            wav_bytes = self._synthesize_wav_bytes(text)
            if wav_bytes and not self._abort_current.is_set():
                played_audio = self._play_wav_bytes(wav_bytes, dev)

            # Fallback 2: Built-in Windows Offline SAPI5 Speech Synthesizer (guarantees audible voice on Windows)
            if not played_audio and self.is_windows and not self._abort_current.is_set():
                played_audio = self._speak_windows_sapi5(text)

            if not played_audio and not self._abort_current.is_set():
                target_s = estimated_ms / 1000.0
                while (time.monotonic() - t0) < target_s and not self._abort_current.is_set():
                    time.sleep(0.05)
        finally:
            self.bus.publish(Topics.VOICE_TTS_FINISHED, {"text": text}, source="tts")
            self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="tts")

    def _synthesize_wav_bytes(self, text: str) -> Optional[bytes]:
        if not self.load_model():
            return None

        with self._lock:
            # Route 1: Native Python piper.PiperVoice
            if self._voice is not None:
                try:
                    buf = io.BytesIO()
                    with wave.open(buf, "wb") as wav_file:
                        self._voice.synthesize(
                            text,
                            wav_file,
                            speaker_id=self.speaker_id,
                            length_scale=self.length_scale,
                            noise_scale=self.noise_scale,
                        )
                    return buf.getvalue()
                except Exception as exc:
                    self.logger.error("PiperVoice synthesis error: %s", exc)

            # Route 2: Standalone piper.exe CLI binary
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
                    proc = subprocess.run(
                        cmd,
                        input=text.encode("utf-8"),
                        capture_output=True,
                        cwd=str(Path(self._piper_bin).parent),
                        timeout=12,
                        check=False,
                    )
                    if proc.returncode == 0 and proc.stdout:
                        buf = io.BytesIO()
                        with wave.open(buf, "wb") as wf:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(22050)
                            wf.writeframes(proc.stdout)
                        return buf.getvalue()
                except Exception as exc:
                    self.logger.error("Piper CLI synthesis error: %s", exc)

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

        # Route 1: SoundDevice + NumPy (supports explicit output_device_index for VB-Audio)
        if SOUNDDEVICE_AVAILABLE and sd is not None and np is not None:
            try:
                audio_np = np.frombuffer(frames, dtype=np.int16)
                sd.play(audio_np, samplerate=rate, device=device_index, blocking=True)
                return True
            except Exception as exc:
                self.logger.warning("SoundDevice playback failed, trying winsound: %s", exc)

        # Route 2: Built-in Windows winsound (zero external dependencies)
        if WINSOUND_AVAILABLE and winsound is not None and device_index is None:
            try:
                winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
                return True
            except Exception as exc:
                self.logger.warning("winsound playback failed: %s", exc)

        # Route 3: PyAudio fallback
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
                step = 4096
                for offset in range(0, len(frames), step):
                    if self._abort_current.is_set():
                        break
                    stream.write(frames[offset : offset + step])
                return True
            except Exception as exc:
                self.logger.warning("PyAudio playback failed: %s", exc)
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
        """Offline Windows SAPI5 speech synthesis fallback when Piper binary/wheel isn't installed yet."""
        safe_text = text.replace("'", " ").replace('"', " ")
        ps_cmd = (
            "Add-Type -AssemblyName System.Speech; "
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$synth.Rate = 1; "
            f"$synth.Speak('{safe_text}')"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", ps_cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            return True
        except Exception as exc:
            self.logger.debug("Windows SAPI5 fallback error: %s", exc)
            return False