#!/usr/bin/env python3
"""
<<<<<<< HEAD
E.V. (Enhanced Virtual Assistant) — Offline Vosk STT (`voice/stt.py`)
=====================================================================
Includes:
  1. 500ms Post-TTS Echo Guard (`tts_cooldown_ms: 500`) + `is_muted` property:
     Ignores microphone frames while TTS is speaking plus 500ms after TTS finishes
     so E.V. never transcribes its own voice.
  2. Number-Word & Indian English Phonetic Normalization (`normalize_indian_english_transcript`):
     Converts spoken number words (`"ten"` -> `"10"`, `"thirty"` -> `"30"`, `"hundred"` -> `"100"`)
     and Vosk Indian English mishearings (`"sector diameter"` -> `"set a timer"`,
     `"ivo i am"` -> `"who am i"`, `"booed off of got a you"` -> `"who are you"`,
     `"odd person"` -> `"100 percent"`).
=======
E.V. (Enhanced Virtual Assistant) — Indian-Accent-Tuned Offline Vosk STT (`stt.py`)
===================================================================================
Upgrades for Indian English Pronunciation, Accuracy & Echo Rejection:
  1. Per-Word Confidence Extraction (`feed_pcm_chunk_detailed`):
     Parses Vosk's `result` word array (`[{"conf": 0.92, "word": "..."}]`),
     computes mean word confidence (`0.0` to `1.0`), and passes it downstream
     so `IntentRouter` can reject low-confidence misrecognitions instead of
     firing wrong actions.
  2. Indian English Phonetic Normalization (`normalize_indian_english_transcript`):
     Corrects systematic `vosk-model-small-en-us` misrecognitions on Indian
     English speech:
       - `"what's up"` / `"what sup"` / `"watts app"` / `"watch app"` -> `"whatsapp"`
       - `"mummy"` / `"mammi"` / `"mami"` / `"mommy"` -> `"mom"`
       - `"who are"` misheard as `"the are"` / `"do are"` / `"ooh are"` -> `"who are"`
       - `"insta gram"` -> `"instagram"`
  3. Strong Post-TTS Echo Guard (`tts_cooldown_ms: 450`):
     Blocks speaker reverb/tail frames for 450ms after TTS finishes and checks
     word overlap against E.V.'s last spoken utterance so E.V. never triggers
     on her own voice.
  4. Smaller Audio Frame Blocksize (`chunk_size: 2000` = 125ms @ 16kHz):
     Doubles time resolution for `ClapDetector` and cuts wake-word latency in half.
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.event_bus import Event, EventBus, Topics, get_event_bus

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


<<<<<<< HEAD
NUMBER_WORDS_MAP: Dict[str, int] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}


def convert_spoken_numbers(text: str) -> str:
    """
    Convert spoken English number words ('ten', 'twenty five', 'one hundred')
    into digits ('10', '25', '100') for reliable timer and volume parsing.
    """
    if not text:
        return ""
    out = re.sub(r"\ba\s+hundred\b|\bone\s+hundred\b", "100", text, flags=re.I)

    # Compound tens + units ("twenty five" -> "25")
    tens_map = {
        "twenty": 20,
        "thirty": 30,
        "forty": 40,
        "fifty": 50,
        "sixty": 60,
        "seventy": 70,
        "eighty": 80,
        "ninety": 90,
    }
    units_map = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
    }
    for t_word, t_val in tens_map.items():
        for u_word, u_val in units_map.items():
            out = re.sub(
                rf"\b{t_word}[\s-]+{u_word}\b",
                str(t_val + u_val),
                out,
                flags=re.I,
            )

    for word, val in NUMBER_WORDS_MAP.items():
        out = re.sub(rf"\b{word}\b", str(val), out, flags=re.I)

    return out


INDIAN_ENGLISH_CORRECTIONS: List[Tuple[re.Pattern[str], Any]] = [
    # "set a timer of/for" misheard by Vosk as "sector diameter of" / "sector timer of" / "set the time of"
    (
        re.compile(
            r"\b(?:sector\s+diameter|sector\s+timer|set\s+the\s+diameter|said\s+the\s+timer|set\s+the\s+time)\s+(?:of|for|off)\b",
            re.I,
        ),
        "set a timer for",
    ),
    # "increase the volume as the odd person" -> "increase the volume to 100 percent"
    (
        re.compile(r"\bincrease\s+the\s+volume\s+as\s+the\s+(?:odd|hundred|100)\s+(?:person|percent)\b", re.I),
        "increase the volume to 100 percent",
    ),
    # "ivo i am" -> "who am i"
    (
        re.compile(r"^(?:ivo|evie\s+who|hey\s+evie\s+who)\s+i\s+am\b", re.I),
        "who am i",
    ),
    # "booed off of got a you" -> "who the fuck are you"
    (
        re.compile(r"\bbooed\s+off\s+of\s+got\s+a\s+you\b", re.I),
        "who the fuck are you",
    ),
    # "open whatsapp and" misheard as "open what's happened" / "open board whatsapp"
    (
        re.compile(
            r"\b(?:open|launch)\s+(?:board\s+|the\s+)?(?:what's\s+happened|whats\s+happened|what\s+happened|what's\s+up\s+and|whats\s+up\s+and|watch\s+up\s+and)\b",
            re.I,
        ),
        "open whatsapp and",
    ),
    (
        re.compile(
            r"\b(open|launch|on|to|in|my|check|read)\s+(?:board\s+)?(?:what's\s+up|whats\s+up|what\s+sup|watts\s+app|watch\s+app|what\s+app|words\s+app|world\s+app)\b",
            re.I,
        ),
        lambda m: f"{m.group(1)} whatsapp",
    ),
    (
        re.compile(
            r"\b(?:what's\s+up|whats\s+up|what\s+sup|watts\s+app|watch\s+app|words\s+app)\s+(message|messages|voice\s+note|number|chat)\b",
            re.I,
        ),
        lambda m: f"whatsapp {m.group(1)}",
    ),
    (
        re.compile(
            r"\b(?:zelda|sender|sand\s+a|centre|sent\s+a|sander)\s+(hello|hi|hey|whatsapp|message|text)\b",
            re.I,
        ),
        lambda m: f"send a {m.group(1)}",
    ),
    (
        re.compile(r"\b(?:ooh|who\s+is)\s+are\s+you\b", re.I),
        "who are you",
    ),
    (
        re.compile(r"\b(are\s+you\s+able\s+to|can\s+you|do\s+you)\s+(?:hair|air|here|hear|er)\s+me\b", re.I),
        lambda m: f"{m.group(1)} hear me",
    ),
    (re.compile(r"\b(?:mummy|mammi|mami|mommy|mataji|ammi|amma)\b", re.I), "mom"),
    (re.compile(r"\b(?:papaji|pitaji|daddy|papa|abba)\b", re.I), "dad"),
=======
# Systematic Indian-English phonetic corrections for vosk-model-small-en-us
INDIAN_ENGLISH_CORRECTIONS: List[Tuple[re.Pattern[str], str]] = [
    # "open whatsapp" / "on whatsapp" often heard as "open what's up" / "on what's up"
    (re.compile(r"\b(?:open|launch|on|to|in|my|check|read)\s+(?:what's\s+up|whats\s+up|what\s+sup|watts\s+app|watch\s+app|what\s+app)\b", re.I),
     lambda m: m.group(0).split()[0] + " whatsapp"),  # type: ignore
    (re.compile(r"\b(?:what's\s+up|whats\s+up|what\s+sup|watts\s+app|watch\s+app)\s+(?:message|voice\s+note|number|chat)\b", re.I),
     lambda m: "whatsapp " + " ".join(m.group(0).split()[2:])),  # type: ignore
    # "Who are you" when Indian Englishunaspirated 'wh' is heard as "the are you" / "do are you" / "you are you" / "ooh are you"
    (re.compile(r"\b(?:the|do|to|you|ooh|oh|how)\s+are\s+you\b(?:\s+tell\s+me)?", re.I), "who are you"),
    # "are you able to hear me" when "hear" is heard as "hair" / "air" / "here"
    (re.compile(r"\b(?:are\s+you\s+able\s+to|can\s+you)\s+(?:hair|air|here|hear|er)\s+me\b", re.I), "can you hear me"),
    # Indian family terms -> canonical contact names
    (re.compile(r"\b(?:mummy|mammi|mami|mommy|mataji)\b", re.I), "mom"),
    (re.compile(r"\b(?:papaji|pitaji|daddy)\b", re.I), "dad"),
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    (re.compile(r"\binsta\s+gram\b", re.I), "instagram"),
]


def normalize_indian_english_transcript(text: str) -> str:
<<<<<<< HEAD
    """Apply Indian English phonetic corrections and spoken number-word conversion."""
=======
    """Apply lightweight phonetic normalizations for Indian English speech."""
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    if not text:
        return ""
    out = text.strip()
    for pattern, replacement in INDIAN_ENGLISH_CORRECTIONS:
<<<<<<< HEAD
        out = pattern.sub(replacement, out)
    out = re.sub(
        r"^(?:i\s+said|said|uh|um|ah)\s+(?=(?:open|launch|send|call|who|what|how|set|increase|decrease|remember|from\s+now\s+on)\b)",
        "",
        out,
        flags=re.I,
    )
=======
        out = pattern.sub(replacement, out)  # type: ignore
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    return re.sub(r"\s+", " ", out).strip()


class VoskSTT:
<<<<<<< HEAD
    """
    Offline Vosk Speech-to-Text engine with 500ms post-TTS echo guard,
    `is_muted` property, and per-word confidence scoring.
    """

    normalize_indian_english_transcript = staticmethod(normalize_indian_english_transcript)
    convert_spoken_numbers = staticmethod(convert_spoken_numbers)
=======
    normalize_indian_english_transcript = staticmethod(normalize_indian_english_transcript)
    """
    Offline Vosk Speech-to-Text engine with per-word confidence scoring,
    Indian English phonetic normalization, and post-TTS echo rejection.
    """
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

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
<<<<<<< HEAD
        self.chunk_size: int = int(stt_cfg.get("chunk_size", 2000))
        self.device_index: Optional[int] = stt_cfg.get("input_device_index")
        self.min_confidence: float = float(stt_cfg.get("min_confidence", 0.38))
        # Ignore STT during TTS plus 500ms after TTS finishes
        self.tts_cooldown_s: float = float(stt_cfg.get("tts_cooldown_ms", 500)) / 1000.0
=======
        # Smaller 2000-frame (125ms) chunk size for responsive wake-word & clap detection
        self.chunk_size: int = int(stt_cfg.get("chunk_size", 2000))
        self.device_index: Optional[int] = stt_cfg.get("input_device_index")
        self.min_confidence: float = float(stt_cfg.get("min_confidence", 0.52))
        self.tts_cooldown_s: float = float(stt_cfg.get("tts_cooldown_ms", 450)) / 1000.0
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

        self._model: Optional[Any] = None
        self._recognizer: Optional[Any] = None
        self._lock = threading.RLock()
        self._muted_for_tts = threading.Event()
        self._tts_finished_ts: float = 0.0
        self._current_tts_text: str = ""
        self._audio_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=64)

        self.bus.subscribe(Topics.VOICE_TTS_STARTED, self._on_tts_started)
        self.bus.subscribe(Topics.VOICE_TTS_FINISHED, self._on_tts_finished)

    @property
    def is_ready(self) -> bool:
        return self._model is not None and (SOUNDDEVICE_AVAILABLE or PYAUDIO_AVAILABLE)

    @property
    def is_muted(self) -> bool:
<<<<<<< HEAD
        """True while muted for active TTS playback."""
=======
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        return self._muted_for_tts.is_set()

    @property
    def is_tts_speaking(self) -> bool:
<<<<<<< HEAD
        """True while TTS is speaking OR within the 500ms post-TTS echo cooldown window."""
        if self._muted_for_tts.is_set():
            return True
        if self._tts_finished_ts > 0.0 and (time.monotonic() - self._tts_finished_ts) < self.tts_cooldown_s:
            return True
        return False
=======
        if self._muted_for_tts.is_set():
            return True
        # Also treat the first 450ms after TTS finishes as echo cooldown
        return (time.monotonic() - self._tts_finished_ts) < self.tts_cooldown_s
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

    def _on_tts_started(self, event: Event) -> None:
        self._current_tts_text = str(event.payload.get("text", "")).lower()
        self.set_tts_mute(True)

    def _on_tts_finished(self, event: Event) -> None:
        self._tts_finished_ts = time.monotonic()
        self.set_tts_mute(False)

    def set_tts_mute(self, muted: bool) -> None:
        if muted:
            self._muted_for_tts.set()
            self._drain_queue()
        else:
            self._muted_for_tts.clear()

    def is_likely_tts_echo(self, transcript: str) -> bool:
<<<<<<< HEAD
        cleaned = (transcript or "").strip().lower()
        if not cleaned or not self._current_tts_text:
            return False
        if cleaned in {"yes", "no", "stop", "continue", "cancel", "hey evie", "evie"}:
            return False
        words = [w for w in re.sub(r"[^a-z0-9\s]", " ", cleaned).split() if len(w) >= 3]
        if len(words) < 2:
=======
        """Check if a recognized transcript is speaker bleed from E.V.'s own TTS voice."""
        cleaned = (transcript or "").strip().lower()
        if not cleaned or not self._current_tts_text:
            return False
        words = [w for w in re.sub(r"[^a-z0-9\s]", " ", cleaned).split() if len(w) >= 3]
        if not words:
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
            return False
        overlap = sum(1 for w in words if w in self._current_tts_text)
        return (overlap / float(len(words))) >= 0.60

    def _drain_queue(self) -> None:
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def load_model(self) -> bool:
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

<<<<<<< HEAD
=======
            # Also check if user downloaded vosk-model-small-en-in (Indian English model)
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
            en_in_dir = self.base_dir / "models" / "vosk-model-small-en-in"
            if en_in_dir.exists() and str(self.model_path).endswith("vosk-model-small-en-us"):
                self.model_path = en_in_dir

            if not self.model_path.exists():
                self.logger.warning("Vosk model directory not found at %s.", self.model_path)
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "standby"}, source="stt")
                return False

            try:
                self.logger.info("Loading Vosk offline STT model from %s...", self.model_path)
                self._model = Model(str(self.model_path))
                self._recognizer = self.create_recognizer()
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "online"}, source="stt")
                return True
            except Exception as exc:
                self.logger.error("Failed to load Vosk model: %s", exc)
                self._model = None
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "offline"}, source="stt")
                return False

    def create_recognizer(self, grammar: Optional[list[str]] = None) -> Optional[Any]:
        if self._model is None and not self.load_model():
            return None
        if grammar:
            rec = KaldiRecognizer(self._model, self.sample_rate, json.dumps(grammar))
        else:
            rec = KaldiRecognizer(self._model, self.sample_rate)
        rec.SetWords(True)
        if hasattr(rec, "SetPartialWords"):
            try:
                rec.SetPartialWords(True)
            except Exception:
                pass
        return rec

    def feed_pcm_chunk_detailed(
        self,
        pcm_bytes: bytes,
        recognizer: Optional[Any] = None,
        allow_during_tts: bool = False,
    ) -> Tuple[bool, str, float]:
<<<<<<< HEAD
=======
        """
        Feed a raw 16-bit mono PCM chunk into Vosk.
        Returns `(is_final, normalized_text, mean_confidence)`.
        """
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        if not pcm_bytes:
            return False, "", 0.0
        if self.is_tts_speaking and not allow_during_tts:
            return False, "", 0.0

        rec = recognizer or self._recognizer
        if rec is None:
            return False, "", 0.0

        try:
            if rec.AcceptWaveform(pcm_bytes):
                res = json.loads(rec.Result() or "{}")
                raw_text = str(res.get("text", "")).strip()
                if not raw_text:
                    return False, "", 0.0

<<<<<<< HEAD
                if (self.is_tts_speaking or (time.monotonic() - self._tts_finished_ts) < 1.0) and self.is_likely_tts_echo(raw_text):
=======
                if (self.is_tts_speaking or (time.monotonic() - self._tts_finished_ts) < 1.2) and self.is_likely_tts_echo(raw_text):
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                    try:
                        rec.Reset()
                    except Exception:
                        pass
                    return False, "", 0.0

                words_info = res.get("result", [])
                if isinstance(words_info, list) and words_info:
                    confs = [float(w.get("conf", 1.0)) for w in words_info if isinstance(w, dict)]
                    mean_conf = sum(confs) / float(len(confs)) if confs else 1.0
                else:
                    mean_conf = 0.85

                norm_text = normalize_indian_english_transcript(raw_text)
                self.bus.publish(
                    Topics.VOICE_FINAL_TRANSCRIPT,
                    {"text": norm_text, "raw_text": raw_text, "confidence": round(mean_conf, 3)},
                    source="stt",
                )
                return True, norm_text, mean_conf
            else:
                part = json.loads(rec.PartialResult() or "{}")
                partial_raw = str(part.get("partial", "")).strip()
                if not partial_raw:
                    return False, "", 0.0
<<<<<<< HEAD
                if (self.is_tts_speaking or (time.monotonic() - self._tts_finished_ts) < 1.0) and self.is_likely_tts_echo(partial_raw):
=======
                if (self.is_tts_speaking or (time.monotonic() - self._tts_finished_ts) < 1.2) and self.is_likely_tts_echo(partial_raw):
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                    return False, "", 0.0

                norm_partial = normalize_indian_english_transcript(partial_raw)
                self.bus.publish(
                    Topics.VOICE_PARTIAL_TRANSCRIPT,
                    {"text": norm_partial},
                    source="stt",
                )
                return False, norm_partial, 0.8
        except Exception as exc:
            self.logger.debug("Vosk recognition error: %s", exc)
            return False, "", 0.0

    def feed_pcm_chunk(
        self,
        pcm_bytes: bytes,
        recognizer: Optional[Any] = None,
        allow_during_tts: bool = False,
    ) -> Tuple[bool, str]:
<<<<<<< HEAD
=======
        """Backwards-compatible wrapper returning `(is_final, text)`."""
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        is_final, text, _ = self.feed_pcm_chunk_detailed(
            pcm_bytes, recognizer=recognizer, allow_during_tts=allow_during_tts
        )
        return is_final, text

    def listen_once(self, timeout_seconds: float = 6.0) -> str:
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
                is_final, text, _ = self.feed_pcm_chunk_detailed(pcm_chunk, recognizer=rec)
                if is_final and text:
                    final_text = text
                    break
                elif text:
                    last_partial = text

            if not final_text:
                flush = json.loads(rec.FinalResult() or "{}")
                raw_flush = str(flush.get("text", "")).strip()
                final_text = normalize_indian_english_transcript(raw_flush) or last_partial
        finally:
            self.bus.publish(Topics.VOICE_LISTENING_STOPPED, {}, source="stt")

        return final_text

    def iter_mic_chunks(
        self,
        stop_check: Callable[[], bool],
        on_raw_chunk: Optional[Callable[[bytes], None]] = None,
    ):
<<<<<<< HEAD
=======
        """
        Generator yielding 16-bit 16kHz mono PCM byte chunks from `sounddevice`
        (preferred on Windows) or `PyAudio`.
        """
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        if SOUNDDEVICE_AVAILABLE and sd is not None:
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
                            data = q.get(timeout=0.20)
                            if on_raw_chunk is not None:
                                on_raw_chunk(data)
                            yield data
                        except queue.Empty:
                            continue
                return
            except Exception as exc:
                self.logger.error("SoundDevice microphone stream error: %s", exc)
                self.bus.publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "offline"}, source="stt")

        if PYAUDIO_AVAILABLE and pyaudio is not None:
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
        else:
            while not stop_check():
                time.sleep(0.25)
                break