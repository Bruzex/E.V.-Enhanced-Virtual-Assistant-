#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — 100% Vosk Offline STT Engine (`voice/stt.py`)
=================================================================================
Strictly offline Vosk Speech-to-Text (`models/vosk-model-en-in-0.5` @ 16000 Hz)
with zero Whisper / HuggingFace / network dependencies.

Includes:
  1. 500ms Post-TTS Echo Guard (`tts_cooldown_ms: 500`) + `is_muted` property:
     Ignores microphone frames while TTS is speaking plus 500ms after TTS finishes
     so E.V. never transcribes its own voice.
  2. Contiguous Phone Digit Joining & Indian-English Normalization:
     - Converts spoken digit runs (`"nine five eight to one nine eight one six five"`
       or `"double nine"`) into contiguous digit strings (`"9582198165"`).
     - Fixes Indian-English Vosk mishearings (`"stopped listening"` -> `"stop listening"`,
       `"send a halo message to mumbai"` -> `"send a hello message to mommy"`,
       `"sector diameter"` -> `"set a timer"`).
  3. Startup & Turn Logging:
     - Startup: `Vosk STT ready (vosk-model-en-in-0.5 @ 16000 Hz)`
     - Turn: `STT final | engine=vosk | conf=0.88 | text='...'`
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.event_bus import Event, EventBus, Topics, get_event_bus
from memory.sqlite_store import mask_pii

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
except Exception:
    sd = None  # type: ignore
    SOUNDDEVICE_AVAILABLE = False


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

    def _as_digit_token(tok: str, in_digit_context: bool = False) -> Optional[str]:
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


INDIAN_ENGLISH_CORRECTIONS: List[Tuple[re.Pattern[str], Any]] = [
    # "stopped listening" -> "stop listening"
    (re.compile(r"\bstopped\s+listening\b", re.I), "stop listening"),
    (re.compile(r"\bstop\s+listen(?:ing)?\s+to\s+me\b", re.I), "stop listening"),
    (re.compile(r"\bgo\s+to\s+stand\s*by\b", re.I), "stand by"),
    # "set a timer of/for" misheard by Vosk as "sector diameter of" / "sector timer of"
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
    (re.compile(r"^(?:ivo|evie\s+who|hey\s+evie\s+who)\s+i\s+am\b", re.I), "who am i"),
    # "booed off of got a you" -> "who are you"
    (re.compile(r"\bbooed\s+off\s+of\s+got\s+a\s+you\b", re.I), "who are you"),
    # WhatsApp & Mommy / Mumbai mishearings
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
            r"\b(?:what's\s+up|whats\s+up|what\s+sup|watts\s+app|watch\s+app|words\s+app|whats\s+app|what\s+sap)\b",
            re.I,
        ),
        "whatsapp",
    ),
    (
        re.compile(
            r"\b(?:zelda|sender|sand\s+a|centre|sent\s+a|sander)\s+(hello|hi|hey|whatsapp|message|text)\b",
            re.I,
        ),
        lambda m: f"send a {m.group(1)}",
    ),
    (re.compile(r"\bsend\s+(?:a\s+|an\s+|our\s+)?(?:halo|hallow|hollow|hell)\s+message\b", re.I), "send a hello message"),
    (re.compile(r"\bsend\s+our\s+hello\s+message\b", re.I), "send a hello message"),
    (re.compile(r"\b(?:message|whatsapp|text|hello|hi)\s+to\s+(?:my\s+)?(?:mumbai|mumbi|mammi|mami|mommy|mummy)\b", re.I), "message to mommy"),
    (re.compile(r"\bsend\s+(?P<m>[a-z0-9\s]+?)\s+to\s+(?:mumbai|mumbi)\b", re.I), r"send \g<m> to mommy"),
    (re.compile(r"\bcall\s+(?:my\s+)?(?:mumbai|mumbi|mammi|mami|mommy|mummy)\b", re.I), "call mommy"),
    (re.compile(r"\b(?:ooh|who\s+is)\s+are\s+you\b", re.I), "who are you"),
    (
        re.compile(r"\b(are\s+you\s+able\s+to|can\s+you|do\s+you)\s+(?:hair|air|here|hear|er)\s+me\b", re.I),
        lambda m: f"{m.group(1)} hear me",
    ),
    (re.compile(r"\b(?:papaji|pitaji|daddy|papa|abba)\b", re.I), "dad"),
    (re.compile(r"\b(?:insta\s+gram|in\s+sta\s+gram|instant\s+gram)\b", re.I), "instagram"),
]


def normalize_indian_english_transcript(text: str) -> str:
    """Apply Indian-English phonetic corrections and contiguous spoken number conversion."""
    if not text:
        return ""
    out = text.strip()
    for pattern, replacement in INDIAN_ENGLISH_CORRECTIONS:
        out = pattern.sub(replacement, out)
    out = re.sub(
        r"^(?:i\s+said|said|uh|um|ah)\s+(?=(?:open|launch|send|call|who|what|how|set|increase|decrease|remember|from\s+now\s+on|create|write|make|stop)\b)",
        "",
        out,
        flags=re.I,
    )
    out = convert_spoken_numbers(out)
    return re.sub(r"\s+", " ", out).strip()


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
    100% Vosk Offline Speech-to-Text engine (`vosk-model-en-in-0.5` @ 16000 Hz)
    with 500ms post-TTS echo guard, contiguous phone digit joining, and per-word
    confidence scoring.
    """

    normalize_indian_english_transcript = staticmethod(normalize_indian_english_transcript)
    convert_spoken_numbers = staticmethod(convert_spoken_numbers)
    join_spoken_digits = staticmethod(join_spoken_digits)

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
        rel_model = str(
            stt_cfg.get("model_path")
            or voice_cfg.get("vosk_model_path")
            or "models/vosk-model-en-in-0.5"
        )
        self.model_path: Path = self._resolve_vosk_model_path(base_dir, rel_model)
        self.sample_rate: int = int(stt_cfg.get("sample_rate", voice_cfg.get("sample_rate", 16000)))
        self.chunk_size: int = int(stt_cfg.get("chunk_size", 2000))
        self.device_index: Optional[int] = stt_cfg.get("input_device_index")
        self.min_confidence: float = float(stt_cfg.get("min_confidence", 0.50))
        self.silence_timeout_s: float = float(
            stt_cfg.get("silence_timeout_seconds", voice_cfg.get("silence_timeout_seconds", 1.1))
        )
        self.tts_cooldown_s: float = float(stt_cfg.get("tts_cooldown_ms", 500)) / 1000.0
        self.active_engine: str = "vosk"

        self._model: Optional[Any] = None
        self._recognizer: Optional[Any] = None
        self._lock = threading.RLock()
        self._muted_for_tts = threading.Event()
        self._tts_finished_ts: float = 0.0
        self._current_tts_text: str = ""
        self._audio_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=64)

        self.bus.subscribe(Topics.VOICE_TTS_STARTED, self._on_tts_started)
        self.bus.subscribe(Topics.VOICE_TTS_FINISHED, self._on_tts_finished)

        # Load Vosk model immediately if present on disk
        if VOSK_AVAILABLE and self.model_path.exists():
            self.load_model()

    @staticmethod
    def _resolve_vosk_model_path(base_dir: Path, configured_rel: str) -> Path:
        primary = (
            (base_dir / configured_rel).resolve()
            if not os.path.isabs(configured_rel)
            else Path(configured_rel)
        )
        if primary.exists():
            return primary
        for candidate in (
            "models/vosk-model-en-in-0.5",
            "models/vosk-model-small-en-in",
            "models/vosk-model-small-en-in-0.4",
            "models/vosk-model-small-en-us",
        ):
            p = (base_dir / candidate).resolve()
            if p.exists():
                return p
        return primary

    def _safe_publish(self, topic: str, payload: Dict[str, Any]) -> None:
        try:
            self.bus.publish(topic, payload, source="stt")
        except Exception as exc:
            self.logger.error("Failed to publish topic '%s' from STT: %s", topic, exc)

    @property
    def is_ready(self) -> bool:
        return self._model is not None and (SOUNDDEVICE_AVAILABLE or PYAUDIO_AVAILABLE)

    @property
    def is_muted(self) -> bool:
        """True while muted for active TTS playback."""
        return self._muted_for_tts.is_set()

    @property
    def is_tts_speaking(self) -> bool:
        """True while TTS is speaking OR within the 500ms post-TTS echo cooldown window."""
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
        if cleaned in {"yes", "no", "stop", "continue", "cancel", "hey evie", "evie", "stop listening", "stand by"}:
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

    def load_model(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True

            if not VOSK_AVAILABLE:
                self.logger.warning(
                    "Vosk library could not be loaded (%s). Voice STT running in simulated/GUI mode.",
                    VOSK_IMPORT_ERROR or "package not installed",
                )
                self._safe_publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "standby"})
                return False

            if not self.model_path.exists():
                self.logger.warning("Vosk model directory not found at %s.", self.model_path)
                self._safe_publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "standby"})
                return False

            try:
                self._model = Model(str(self.model_path))
                self._recognizer = self.create_recognizer()
                self.logger.info("Vosk STT ready (%s @ %d Hz)", self.model_path.name, self.sample_rate)
                self._safe_publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "online"})
                return True
            except Exception as exc:
                self.logger.error("Failed to load Vosk model: %s", exc)
                self._model = None
                self._safe_publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "offline"})
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

    def reset(self) -> None:
        with self._lock:
            if self._recognizer is not None:
                try:
                    self._recognizer.Reset()
                except Exception:
                    pass

    def feed_pcm_chunk_detailed(
        self,
        pcm_bytes: bytes,
        recognizer: Optional[Any] = None,
        allow_during_tts: bool = False,
    ) -> Tuple[bool, str, float]:
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

                if (self.is_tts_speaking or (time.monotonic() - self._tts_finished_ts) < 1.0) and self.is_likely_tts_echo(raw_text):
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
                self.logger.info(
                    "STT final | engine=vosk | conf=%.2f | text='%s'",
                    mean_conf,
                    mask_pii(norm_text),
                )

                # Always allow "stop listening" / "stand by" even on medium confidence (>= 0.30)
                is_standby_phrase = bool(
                    re.search(
                        r"\b(?:stop(?:ped)?\s+listening|go\s+to\s+(?:stand\s*by|sleep)|stand\s*by|pause\s+listening|sleep\s+mode)\b",
                        norm_text,
                        re.I,
                    )
                )
                if mean_conf < self.min_confidence and not is_standby_phrase:
                    self.logger.debug(
                        "Dropping final transcript below min_confidence (conf=%.2f < %.2f): '%s'",
                        mean_conf,
                        self.min_confidence,
                        mask_pii(norm_text),
                    )
                    return False, "", mean_conf

                self._safe_publish(
                    Topics.VOICE_FINAL_TRANSCRIPT,
                    {"text": norm_text, "raw_text": raw_text, "confidence": round(mean_conf, 3)},
                )
                return True, norm_text, mean_conf
            else:
                part = json.loads(rec.PartialResult() or "{}")
                partial_raw = str(part.get("partial", "")).strip()
                if not partial_raw:
                    return False, "", 0.0
                if (self.is_tts_speaking or (time.monotonic() - self._tts_finished_ts) < 1.0) and self.is_likely_tts_echo(partial_raw):
                    return False, "", 0.0

                norm_partial = normalize_indian_english_transcript(partial_raw)
                self._safe_publish(
                    Topics.VOICE_PARTIAL_TRANSCRIPT,
                    {"text": norm_partial},
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
        is_final, text, _ = self.feed_pcm_chunk_detailed(
            pcm_bytes, recognizer=recognizer, allow_during_tts=allow_during_tts
        )
        return is_final, text

    def accept_waveform(self, pcm_bytes: bytes) -> Tuple[bool, str, float]:
        """Alias for `feed_pcm_chunk_detailed`."""
        return self.feed_pcm_chunk_detailed(pcm_bytes)

    def listen_once(self, timeout_seconds: float = 6.0) -> str:
        if not self.load_model():
            return ""

        rec = self.create_recognizer()
        if rec is None:
            return ""

        self._safe_publish(Topics.VOICE_LISTENING_STARTED, {})
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
            self._safe_publish(Topics.VOICE_LISTENING_STOPPED, {})

        return final_text

    def iter_mic_chunks(
        self,
        stop_check: Callable[[], bool],
        on_raw_chunk: Optional[Callable[[bytes], None]] = None,
    ):
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
                self._safe_publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "offline"})

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
                self._safe_publish(Topics.CONNECTION_STATUS, {"id": "mic", "status": "offline"})
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