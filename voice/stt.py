#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Dual-Backend Speech-to-Text Engine (`voice/stt.py`)
=======================================================================================
Supports:
  1. Primary Engine (`faster-whisper`, optional):
     - CPU-only (`device="cpu"`, `compute_type="int8"`), configurable model size
       (`"base"` or `"small"` via `voice.stt.whisper_model`).
     - Automatic fallback to offline `vosk` (`vosk-model-en-in-0.5` or
       `vosk-model-small-en-in-0.4`) when `faster-whisper` is not installed or
       `voice.stt.engine` is set to `"vosk"`.
  2. Indian-English Accent & Contiguous Phone Digit Normalization:
     - Converts spoken number sequences (including `"double"`, `"triple"`, and
       homophones like `"to"` -> `2`, `"for"` -> `4`, `"oh"` -> `0` inside digit runs)
       into contiguous digit strings (`"nine five eight to one nine eight one six five"`
       -> `"9582198165"`).
     - Fixes common Vosk `en-in` mishearings (`"stopped listening"` -> `"stop listening"`,
       `"halo message"` -> `"hello message"`, `"message to mumbai"` -> `"message to mommy"`).
  3. Echo Suppression & Detailed Turn Logging:
     - Mutes input during active TTS playback to prevent assistant feedback loops.
     - Logs: `STT final | engine=<whisper|vosk> | conf=<0.00> | text='...'`
"""

from __future__ import annotations

import json
import logging
import math
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import numpy as np

from core.event_bus import Event, EventBus, Topics, get_event_bus
from memory.sqlite_store import mask_pii

try:
    from faster_whisper import WhisperModel  # type: ignore

    FASTER_WHISPER_AVAILABLE = True
except ImportError:
    WhisperModel = None  # type: ignore
    FASTER_WHISPER_AVAILABLE = False

try:
    from vosk import KaldiRecognizer, Model, SetLogLevel  # type: ignore

    SetLogLevel(-1)
    VOSK_AVAILABLE = True
except ImportError:
    KaldiRecognizer = None  # type: ignore
    Model = None  # type: ignore
    VOSK_AVAILABLE = False

try:
    import sounddevice as sd  # type: ignore

    SOUNDDEVICE_AVAILABLE = True
except Exception:
    sd = None  # type: ignore
    SOUNDDEVICE_AVAILABLE = False

try:
    import pyaudio  # type: ignore

    PYAUDIO_AVAILABLE = True
except Exception:
    pyaudio = None  # type: ignore
    PYAUDIO_AVAILABLE = False


# =============================================================================
# Spoken Number Word -> Digit Conversion & Contiguous Phone Digit Joining
# =============================================================================
_NUMBER_WORDS: Dict[str, int] = {
    "zero": 0,
    "oh": 0,
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

_SINGLE_DIGIT_WORDS: Dict[str, str] = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}

_DIGIT_HOMOPHONES: Dict[str, str] = {
    "to": "2",
    "too": "2",
    "tu": "2",
    "for": "4",
    "fore": "4",
    "won": "1",
    "ate": "8",
}


def join_spoken_digits(text: str) -> str:
    """
    Convert sequences of spoken single digits (including 'double X', 'triple X',
    and digit-run homophones like 'nine five eight to one...') into contiguous
    digit strings (e.g., '9582198165').
    """
    if not text:
        return ""

    tokens = text.split()
    if not tokens:
        return text

    # Expand "double <digit>" / "triple <digit>" first
    expanded: List[str] = []
    i = 0
    while i < len(tokens):
        low = re.sub(r"[^a-z0-9]", "", tokens[i].lower())
        if low in ("double", "triple") and i + 1 < len(tokens):
            nxt = re.sub(r"[^a-z0-9]", "", tokens[i + 1].lower())
            digit_char = _SINGLE_DIGIT_WORDS.get(nxt) or (nxt if nxt.isdigit() and len(nxt) == 1 else None)
            if digit_char is not None:
                repeat = 2 if low == "double" else 3
                expanded.append(digit_char * repeat)
                i += 2
                continue
        expanded.append(tokens[i])
        i += 1

    # Identify runs of single-digit words / homophones
    def _as_digit_token(tok: str, in_digit_context: bool = False) -> Optional[str]:
        if any(c in tok for c in "()+-"):
            return None
        clean = re.sub(r"[^a-z0-9]", "", tok.lower())
        if not clean:
            return None
        if clean.isdigit():
            return clean
        if clean in _SINGLE_DIGIT_WORDS:
            return _SINGLE_DIGIT_WORDS[clean]
        if in_digit_context and clean in _DIGIT_HOMOPHONES:
            return _DIGIT_HOMOPHONES[clean]
        return None

    n = len(expanded)
    out: List[str] = []
    idx = 0
    while idx < n:
        d0 = _as_digit_token(expanded[idx], in_digit_context=False)
        if d0 is not None:
            run_digits: List[str] = [d0]
            j = idx + 1
            while j < n:
                dj = _as_digit_token(expanded[j], in_digit_context=True)
                if dj is not None:
                    run_digits.append(dj)
                    j += 1
                else:
                    break
            if len(run_digits) >= 3 or sum(len(x) for x in run_digits) >= 3:
                out.append("".join(run_digits))
                idx = j
                continue
        out.append(expanded[idx])
        idx += 1

    return " ".join(out)


def convert_spoken_numbers(text: str) -> str:
    """
    Convert English number words ('ten', 'thirty five', 'one hundred') and
    spoken digit sequences ('nine five eight two...') into numeric strings.
    """
    if not text:
        return ""

    pre_joined = join_spoken_digits(text)
    tokens = pre_joined.split()
    out: List[str] = []
    i = 0
    n = len(tokens)

    while i < n:
        raw_tok = tokens[i]
        clean_tok = re.sub(r"[^a-z0-9]", "", raw_tok.lower())

        if clean_tok == "a" and i + 1 < n:
            nxt_clean = re.sub(r"[^a-z0-9]", "", tokens[i + 1].lower())
            if nxt_clean == "hundred":
                out.append("100")
                i += 2
                continue

        if clean_tok in _NUMBER_WORDS and clean_tok != "oh":
            val = _NUMBER_WORDS[clean_tok]
            j = i + 1
            while j < n:
                nxt_clean = re.sub(r"[^a-z0-9]", "", tokens[j].lower())
                if nxt_clean not in _NUMBER_WORDS or nxt_clean == "oh":
                    break
                nxt_val = _NUMBER_WORDS[nxt_clean]
                if nxt_val == 100:
                    val = max(1, val) * 100
                elif val >= 20 and nxt_val < 10:
                    val += nxt_val
                elif val >= 100 and nxt_val < 100:
                    val += nxt_val
                else:
                    break
                j += 1
            suffix = re.sub(r"^[a-zA-Z0-9]+", "", raw_tok)
            out.append(f"{val}{suffix}")
            i = j
        else:
            out.append(raw_tok)
            i += 1

    result = " ".join(out)
    result = re.sub(
        r"\b(\d)\s+(\d)\s+(\d)(?:\s+(\d))?(?:\s+(\d))?(?:\s+(\d))?(?:\s+(\d))?(?:\s+(\d))?(?:\s+(\d))?(?:\s+(\d))?(?:\s+(\d))?(?:\s+(\d))?\b",
        lambda m: "".join(g for g in m.groups() if g is not None),
        result,
    )
    return result


@dataclass
class STTResult:
    """Structured result from a finalized speech-to-text recognition turn."""

    text: str
    confidence: float = 1.0
    words: List[Dict[str, Any]] = field(default_factory=list)
    is_final: bool = True
    engine: str = "vosk"


class VoskSTT:
    """
    Streaming & Utterance Speech-to-Text engine supporting:
      - Primary: `faster-whisper` (`WhisperModel`, CPU `int8`, `"base"` or `"small"`)
      - Fallback: `vosk` (`vosk-model-en-in-0.5` / `vosk-model-small-en-in-0.4`)
    Preserves all streaming and backward-compatible methods (`feed_pcm_chunk`,
    `listen_once`, `is_muted`, `create_recognizer`).
    """

    WAKE_ALIASES = [
        (re.compile(r"\b(?:hey|hi|hello|okay|ok|yo)\s+(?:evie|e\s*v|ev|eve|ivy|avi|easy|tv|a\s*v|heavy|baby|he\s+be|e\s+the|eb)\b", re.I), "hey evie"),
        (re.compile(r"^(?:he|the)\s+(?:be|v|evie|tv|eb)\b", re.I), "hey evie"),
        (re.compile(r"^(?:evie|e\.?v\.?|jarvis)\b", re.I), "hey evie"),
    ]

    ACCENT_CORRECTIONS: List[Tuple[re.Pattern[str], str]] = [
        (re.compile(r"\bstopped\s+listening\b", re.I), "stop listening"),
        (re.compile(r"\bstop\s+listen(?:ing)?\s+to\s+me\b", re.I), "stop listening"),
        (re.compile(r"\bgo\s+to\s+stand\s*by\b", re.I), "stand by"),
        (re.compile(r"\b(?:what's\s+up|whats\s+up|what\s+sup|watts\s+app|whats\s+app|what\s+sap|what\s+app)\b", re.I), "whatsapp"),
        (re.compile(r"\bsend\s+(?:a\s+|an\s+|our\s+)?(?:halo|hallow|hollow|hell)\s+message\b", re.I), "send a hello message"),
        (re.compile(r"\bsend\s+our\s+hello\s+message\b", re.I), "send a hello message"),
        (re.compile(r"\b(?:message|whatsapp|text|hello|hi)\s+to\s+(?:my\s+)?(?:mumbai|mumbi|mammi|mami|mommy|mummy)\b", re.I), "message to mommy"),
        (re.compile(r"\bsend\s+(?P<m>[a-z0-9\s]+?)\s+to\s+(?:mumbai|mumbi)\b", re.I), r"send \g<m> to mommy"),
        (re.compile(r"\bcall\s+(?:my\s+)?(?:mumbai|mumbi|mammi|mami|mommy|mummy)\b", re.I), "call mommy"),
        (re.compile(r"\b(?:insta\s+gram|in\s+sta\s+gram|instant\s+gram)\b", re.I), "instagram"),
        (re.compile(r"\b(?:wall\s+ume|val\s+ume|vol\s+ume|volume\s+level)\b", re.I), "volume"),
        (re.compile(r"\b(?:one\s+hundred|a\s+hundred|hundred)\s+(?:per\s+cent|person|percent)\b", re.I), "100 percent"),
        (re.compile(r"\b(?:set\s+(?:a\s+|the\s+)?timer\s+(?:for|of|off|to))\b", re.I), "set a timer for"),
        (re.compile(r"\b(?:time\s+her|tie\s+mer)\b", re.I), "timer"),
        (re.compile(r"\b(?:who\s+are\s+you|hu\s+r\s+u|who\s+is\s+you)\b", re.I), "who are you"),
        (re.compile(r"\b(?:who\s+am\s+i|who\s+i\s+am)\b", re.I), "who am i"),
        (re.compile(r"\b(?:can\s+you\s+hear\s+me|can\s+u\s+hear\s+me|are\s+you\s+able\s+to\s+hear\s+me)\b", re.I), "can you hear me"),
    ]

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

        voice_cfg = config.get("voice", {})
        stt_cfg = voice_cfg.get("stt", {})

        self.sample_rate: int = int(voice_cfg.get("sample_rate", 16000))
        self.chunk_size: int = int(voice_cfg.get("block_size", 4000))
        self.device_index: Optional[int] = voice_cfg.get("device_index", None)
        self.requested_engine: str = str(stt_cfg.get("engine", "vosk")).strip().lower()
        self.whisper_model_size: str = str(stt_cfg.get("whisper_model", "base")).strip()
        self.whisper_compute_type: str = str(stt_cfg.get("whisper_compute_type", "int8")).strip()

        rel_model = str(voice_cfg.get("vosk_model_path", "models/vosk-model-en-in-0.5"))
        self.model_path: Path = self._resolve_vosk_model_path(base_dir, rel_model)

        self.min_confidence: float = float(stt_cfg.get("min_confidence", 0.50))
        self.partial_reset_s: float = float(stt_cfg.get("partial_reset_seconds", 4.0))
        self.tts_cooldown_s: float = float(stt_cfg.get("tts_cooldown_seconds", 0.5))

        self._lock = threading.RLock()
        self._whisper_model: Optional[Any] = None
        self._model: Optional[Any] = None
        self._recognizer: Optional[Any] = None
        self.active_engine: str = "vosk"

        # Buffer for PCM frames when using faster-whisper utterance transcription
        self._pcm_buffer = bytearray()
        self._audio_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=100)

        # Echo suppression
        self._muted_for_tts = threading.Event()
        self._current_tts_text: str = ""
        self._tts_finished_ts: float = 0.0

        self.bus.subscribe(Topics.VOICE_TTS_STARTED, self._on_tts_started)
        self.bus.subscribe(Topics.VOICE_TTS_FINISHED, self._on_tts_finished)

        self._init_engines()

    @staticmethod
    def _resolve_vosk_model_path(base_dir: Path, configured_rel: str) -> Path:
        primary = (base_dir / configured_rel).resolve() if not Path(configured_rel).is_absolute() else Path(configured_rel)
        if primary.exists():
            return primary
        for candidate in [
            "models/vosk-model-en-in-0.5",
            "models/vosk-model-small-en-in-0.4",
            "models/vosk-model-small-en-us-0.15",
        ]:
            p = (base_dir / candidate).resolve()
            if p.exists():
                return p
        return primary

    def _init_engines(self) -> None:
        """Initialize faster-whisper (if requested and available) and Vosk fallback."""
        with self._lock:
            # 1. Always initialize Vosk if available
            if VOSK_AVAILABLE and self.model_path.exists() and any(self.model_path.iterdir()):
                try:
                    self._model = Model(str(self.model_path))
                    self._recognizer = KaldiRecognizer(self._model, float(self.sample_rate))
                    self._recognizer.SetWords(True)
                    self.active_engine = "vosk"
                    self.logger.info("Vosk STT ready (%s @ %d Hz).", self.model_path.name, self.sample_rate)
                except Exception as exc:
                    self.logger.error("Failed to load Vosk model: %s", exc)

            # 2. Initialize faster-whisper on CPU (int8) if requested and installed
            if self.requested_engine == "whisper" and FASTER_WHISPER_AVAILABLE:
                try:
                    local_whisper_dir = self.base_dir / "models" / f"faster-whisper-{self.whisper_model_size}"
                    model_id_or_path = str(local_whisper_dir) if local_whisper_dir.exists() else self.whisper_model_size
                    self._whisper_model = WhisperModel(
                        model_id_or_path,
                        device="cpu",
                        compute_type=self.whisper_compute_type,
                        cpu_threads=4,
                    )
                    self.active_engine = "whisper"
                    self.logger.info(
                        "Faster-Whisper STT engine initialized (model=%s, device=cpu, compute_type=%s).",
                        self.whisper_model_size,
                        self.whisper_compute_type,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Faster-Whisper unavailable (%s); falling back to Vosk STT.",
                        exc,
                    )
                    self._whisper_model = None
                    if self._recognizer is not None:
                        self.active_engine = "vosk"
            elif self.requested_engine == "whisper" and not FASTER_WHISPER_AVAILABLE:
                self.logger.info(
                    "faster-whisper package not installed; using Vosk STT (%s).",
                    self.model_path.name,
                )

    def load_model(self) -> bool:
        if self._model is None and self._whisper_model is None:
            self._init_engines()
        return self.is_ready

    @property
    def is_ready(self) -> bool:
        with self._lock:
            return self._model is not None or self._whisper_model is not None

    @property
    def is_muted(self) -> bool:
        """True while muted for active TTS playback."""
        return self._muted_for_tts.is_set()

    @property
    def is_tts_speaking(self) -> bool:
        """True while TTS is speaking OR within post-TTS echo cooldown window."""
        if self._muted_for_tts.is_set():
            return True
        if self._tts_finished_ts > 0.0 and (time.monotonic() - self._tts_finished_ts) < self.tts_cooldown_s:
            return True
        return False

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
        cleaned = (transcript or "").strip().lower()
        if not cleaned or not self._current_tts_text:
            return False
        if cleaned in {"yes", "no", "stop", "continue", "cancel", "hey evie", "evie"}:
            return False
        words = [w for w in re.sub(r"[^a-z0-9\s]", " ", cleaned).split() if len(w) >= 3]
        if len(words) < 2:
            return False
        overlap = sum(1 for w in words if w in self._current_tts_text)
        return (overlap / float(len(words))) >= 0.60

    def _drain_queue(self) -> None:
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break

    def create_recognizer(self, grammar: Optional[List[str]] = None) -> Optional[Any]:
        with self._lock:
            if not VOSK_AVAILABLE or self._model is None:
                return None
            try:
                if grammar:
                    return KaldiRecognizer(self._model, float(self.sample_rate), json.dumps(grammar))
                rec = KaldiRecognizer(self._model, float(self.sample_rate))
                rec.SetWords(True)
                return rec
            except Exception as exc:
                self.logger.warning("Failed to create KaldiRecognizer: %s", exc)
                return None

    @classmethod
    def normalize_indian_english_transcript(cls, raw_text: str) -> str:
        """
        Normalize raw STT output for Indian-English pronunciation, wake-word
        variants, and contiguous phone digit runs.
        """
        if not raw_text:
            return ""

        text = raw_text.strip()
        for pattern, replacement in cls.WAKE_ALIASES:
            text = pattern.sub(replacement, text)

        for pattern, replacement in cls.ACCENT_CORRECTIONS:
            text = pattern.sub(replacement, text)

        text = convert_spoken_numbers(text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def transcribe_With_whisper(self, pcm_bytes: bytes) -> Optional[STTResult]:
        """Transcribe raw 16kHz 16-bit mono PCM bytes using faster-whisper on CPU."""
        if self._whisper_model is None or not pcm_bytes or len(pcm_bytes) < 1600:
            return None

        try:
            audio_i16 = np.frombuffer(pcm_bytes, dtype=np.int16)
            audio_f32 = audio_i16.astype(np.float32) / 32768.0

            segments, _ = self._whisper_model.transcribe(
                audio_f32,
                language="en",
                beam_size=1,
                best_of=1,
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
                condition_on_previous_text=False,
            )
            seg_list = list(segments)
            if not seg_list:
                return None

            raw_text = " ".join(s.text.strip() for s in seg_list if s.text.strip()).strip()
            if not raw_text:
                return None

            avg_logprobs = [float(getattr(s, "avg_logprob", -0.25)) for s in seg_list]
            mean_lp = sum(avg_logprobs) / len(avg_logprobs) if avg_logprobs else -0.25
            conf = max(0.0, min(1.0, math.exp(max(-3.0, min(0.0, mean_lp)))))

            normalized = self.normalize_indian_english_transcript(raw_text)
            return STTResult(
                text=normalized,
                confidence=round(conf, 3),
                words=[],
                is_final=True,
                engine="whisper",
            )
        except Exception as exc:
            self.logger.debug("Faster-Whisper transcription error: %s", exc)
            return None

    def accept_waveform(self, pcm_bytes: bytes) -> Tuple[bool, str, float]:
        """
        Feed 16-bit mono PCM bytes into the active STT recognizer.
        Returns `(is_final, text, confidence)`.
        """
        if not self.is_ready or not pcm_bytes:
            return False, "", 0.0

        with self._lock:
            if self._whisper_model is not None and self.active_engine == "whisper":
                self._pcm_buffer.extend(pcm_bytes)
                if len(self._pcm_buffer) > 800_000:
                    del self._pcm_buffer[:-640_000]

            if self._recognizer is not None:
                try:
                    if self._recognizer.AcceptWaveform(pcm_bytes):
                        raw = json.loads(self._recognizer.Result() or "{}")
                        res = self._parse_vosk_json(raw)
                        if self._whisper_model is not None and self.active_engine == "whisper" and len(self._pcm_buffer) >= 4800:
                            buf_copy = bytes(self._pcm_buffer)
                            self._pcm_buffer.clear()
                            w_res = self.transcribe_With_whisper(buf_copy)
                            if w_res is not None and w_res.text:
                                self._log_final(w_res)
                                return True, w_res.text, w_res.confidence
                        if res.text:
                            self._log_final(res)
                        return True, res.text, res.confidence
                    else:
                        partial = json.loads(self._recognizer.PartialResult() or "{}")
                        p_text = self.normalize_indian_english_transcript(str(partial.get("partial", "")).strip())
                        return False, p_text, 1.0
                except Exception as exc:
                    self.logger.debug("Vosk accept_waveform error: %s", exc)
                    return False, "", 0.0

            return False, "", 0.0

    def flush_final(self) -> STTResult:
        """Force-finalize current utterance buffer and return `STTResult`."""
        if not self.is_ready:
            return STTResult(text="", confidence=0.0, is_final=True, engine=self.active_engine)

        with self._lock:
            buf_copy = bytes(self._pcm_buffer)
            self._pcm_buffer.clear()

            if self._whisper_model is not None and self.active_engine == "whisper" and len(buf_copy) >= 3200:
                if self._recognizer is not None:
                    try:
                        self._recognizer.Reset()
                    except Exception:
                        pass
                w_res = self.transcribe_With_whisper(buf_copy)
                if w_res is not None and w_res.text:
                    self._log_final(w_res)
                    return w_res

            if self._recognizer is not None:
                try:
                    raw = json.loads(self._recognizer.FinalResult() or "{}")
                    res = self._parse_vosk_json(raw)
                    if res.text:
                        self._log_final(res)
                    return res
                except Exception:
                    pass

            return STTResult(text="", confidence=0.0, is_final=True, engine=self.active_engine)

    def reset(self) -> None:
        """Clear partial recognizer state and audio buffer."""
        with self._lock:
            self._pcm_buffer.clear()
            if self._recognizer is not None:
                try:
                    self._recognizer.Reset()
                except Exception:
                    pass

    def _log_final(self, res: STTResult) -> None:
        self.logger.info(
            "STT final | engine=%s | conf=%.2f | text='%s'",
            res.engine,
            res.confidence,
            mask_pii(res.text),
        )

    def _parse_vosk_json(self, data: Dict[str, Any]) -> STTResult:
        raw_text = str(data.get("text", "")).strip()
        if not raw_text:
            return STTResult(text="", confidence=0.0, words=[], is_final=True, engine="vosk")

        words = data.get("result", [])
        if isinstance(words, list) and words:
            confs = [float(w.get("conf", 1.0)) for w in words if isinstance(w, dict)]
            avg_conf = sum(confs) / len(confs) if confs else 1.0
        else:
            avg_conf = 1.0

        normalized = self.normalize_indian_english_transcript(raw_text)
        return STTResult(
            text=normalized,
            confidence=round(avg_conf, 3),
            words=words if isinstance(words, list) else [],
            is_final=True,
            engine="vosk",
        )

    # =========================================================================
    # Compatibility Layer for Existing Callers & Unit Tests
    # =========================================================================
    def feed_pcm_chunk_detailed(
        self,
        pcm_bytes: bytes,
        recognizer: Optional[Any] = None,
        allow_during_tts: bool = False,
    ) -> Tuple[bool, str, float]:
        """
        Feeds a PCM chunk. Returns `(is_final, transcript, confidence)`.
        Mutes immediately when TTS is active unless `allow_during_tts` is set.
        """
        if self.is_muted and not allow_during_tts:
            return False, "", 0.0

        if recognizer is not None:
            try:
                if recognizer.AcceptWaveform(pcm_bytes):
                    raw = json.loads(recognizer.Result() or "{}")
                    res = self._parse_vosk_json(raw)
                    return True, res.text, res.confidence
                else:
                    partial = json.loads(recognizer.PartialResult() or "{}")
                    p_text = self.normalize_indian_english_transcript(str(partial.get("partial", "")).strip())
                    return False, p_text, 1.0
            except Exception as exc:
                self.logger.debug("Recognizer feed error: %s", exc)
                return False, "", 0.0

        return self.accept_waveform(pcm_bytes)

    def feed_pcm_chunk(
        self,
        pcm_bytes: bytes,
        recognizer: Optional[Any] = None,
        allow_during_tts: bool = False,
    ) -> Tuple[bool, str]:
        """Backward-compatible tuple: `(is_final, text)`."""
        is_final, text, _ = self.feed_pcm_chunk_detailed(
            pcm_bytes, recognizer=recognizer, allow_during_tts=allow_during_tts
        )
        return is_final, text

    def listen_once(self, timeout_seconds: float = 6.0) -> str:
        """Synchronously record from microphone for up to `timeout_seconds` and return text."""
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
                final_text = self.normalize_indian_english_transcript(raw_flush) or last_partial
        finally:
            self.bus.publish(Topics.VOICE_LISTENING_STOPPED, {}, source="stt")

        return final_text

    def iter_mic_chunks(
        self,
        stop_check: Callable[[], bool],
        on_raw_chunk: Optional[Callable[[bytes], None]] = None,
    ) -> Iterator[bytes]:
        """Yield raw 16kHz mono PCM frames from physical microphone."""
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

        if PYAUDIO_AVAILABLE and pyaudio is not None:
            pa = pyaudio.PyAudio()
            stream = None
            try:
                stream = pa.open(
                    format=pyaudio.paInt16,
                    channels=1,
                    rate=self.sample_rate,
                    input=True,
                    frames_per_buffer=self.chunk_size,
                    input_device_index=self.device_index,
                )
                while not stop_check():
                    try:
                        data = stream.read(self.chunk_size, exception_on_overflow=False)
                        if on_raw_chunk is not None:
                            on_raw_chunk(data)
                        yield data
                    except Exception:
                        continue
            except Exception as exc:
                self.logger.error("PyAudio stream error: %s", exc)
            finally:
                if stream is not None:
                    try:
                        stream.stop_stream()
                        stream.close()
                    except Exception:
                        pass
                pa.terminate()
