#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Fault-Tolerant Wake-Word & 90s Session Loop (`voice/wakeword.py`)
=====================================================================================================
Implements:
  1. Exception-Proof Audio Loop & Non-Reentrant `_safe_publish()`:
     - Every `bus.publish(...)` call is wrapped in `try/except (Exception, RecursionError)`.
     - Every audio chunk iteration in `_run_loop()` is wrapped in `try/except` so the
       microphone thread NEVER dies on any transient error.
  2. Instant Voice Barge-In (<150ms) & 500ms Post-TTS Echo Guard:
     - Interrupts TTS and cancels LLM generation immediately when the user speaks over E.V.
  3. Reliable Voice Standby ("stop listening" / "stopped listening" / "stand by"):
     - Works even on medium confidence or in partial transcripts to immediately enter
       `STANDBY` (`idle`) and speak `"Standing by, sir."`
  4. 90-Second Continuous Listening Session + Double-Clap Arming:
     - Saying `"Hey Evie"` or double-clapping arms the 90s session, sets HUD to
       `LISTENING`, and replies `"I'm listening."`
"""

from __future__ import annotations

import json
import logging
import math
import re
import struct
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import winsound  # type: ignore

    WINSOUND_AVAILABLE = True
except ImportError:
    winsound = None  # type: ignore
    WINSOUND_AVAILABLE = False

from core.event_bus import Event, EventBus, Topics, get_event_bus
from memory.sqlite_store import mask_pii
from voice.stt import VoskSTT


def compute_pcm16_rms(pcm_bytes: bytes) -> float:
    """Compute normalized RMS energy (0.0 to 1.0) of a 16-bit mono PCM buffer."""
    count = len(pcm_bytes) // 2
    if count <= 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm_bytes[: count * 2])
    sum_sq = sum(s * s for s in samples)
    rms = math.sqrt(sum_sq / float(count))
    return min(1.0, rms / 32768.0)


def play_wake_chime() -> None:
    """Play a short, non-blocking confirmation tone on Windows."""
    if not WINSOUND_AVAILABLE or winsound is None or not sys.platform.startswith("win"):
        return

    def _chime_worker() -> None:
        try:
            winsound.Beep(880, 65)
            winsound.Beep(1175, 85)
        except Exception:
            pass

    threading.Thread(target=_chime_worker, name="EV-WakeChime", daemon=True).start()


DEFAULT_WAKE_PHRASES: List[str] = [
    "hey evie",
    "hey e v",
    "hey e.v.",
    "hey ev",
    "hi evie",
    "hi e v",
    "hi ev",
    "hello evie",
    "hello e v",
    "hello ev",
    "okay evie",
    "ok evie",
    "okay e v",
    "okay ev",
    "ok ev",
    "evie",
    "e v",
    "e.v.",
    "hey eve",
    "hey ivy",
    "hey avi",
    "hey easy",
    "hey tv",
    "hey a v",
    "hey v",
    "hey heavy",
    "hey baby",
    "hey every",
    "hey movie",
    "he be",
    "he v",
    "the v",
    "jarvis",
]

WAKE_PREFIX_REGEX = re.compile(
    r"^(?:"
    r"(?:hey|hi|hello|okay|ok|yo|he|the|a)\s+(?:evie|e\s*\.?\s*v\.?|eve|ivy|avi|easy|tv|a\s+v|v|heavy|baby|every|movie|he\s+be|e\s+the|e\s+b|eb|we|be)"
    r"|(?:evie|e\s+v|e\.v\.|ev|eve|ivy|avi|heavy|he\s+be|he\s+v|the\s+v|a\s+v|e\s+b|jarvis)"
    r")\b[\s,.:;-]*(.*)$",
    re.IGNORECASE,
)

STANDALONE_WAKE_UTTERANCES = {
    "hey evie",
    "hey e v",
    "hey ev",
    "hi evie",
    "hello evie",
    "okay evie",
    "ok evie",
    "evie",
    "e v",
    "ev",
    "eve",
    "ivy",
    "avi",
    "heavy",
    "he be",
    "he v",
    "the v",
    "a v",
    "e b",
    "eb",
    "hey heavy",
    "hey baby",
    "hey tv",
    "hey a v",
    "hey v",
    "hey ivy",
    "hey avi",
    "hey easy",
    "hey every",
    "hey movie",
    "hey he",
    "hey",
    "jarvis",
}

SLEEP_PHRASES_REGEX = re.compile(
    r"\b(?:stop(?:ped)?\s+listening|go\s+to\s+(?:stand\s*by|sleep)|stand\s*by|pause\s+listening|sleep\s+mode|stop\s+continuous\s+listening)\b",
    re.IGNORECASE,
)

NOISE_FILLER_TOKENS = {
    "the",
    "a",
    "an",
    "uh",
    "um",
    "ah",
    "hm",
    "hmm",
    "oh",
    "eh",
    "er",
    "huh",
    "ha",
    "he",
    "be",
    "we",
    "to",
    "of",
    "in",
    "it",
    "is",
    "are",
    "that",
    "this",
    "and",
    "or",
    "but",
    "so",
    "for",
    "on",
    "at",
    "by",
    "en",
    "said",
    "lord",
    "lord the",
    "the are",
    "he the",
    "it it launches",
}


class WakeWordLoop:
    """
    Unified background microphone loop with <200ms energy barge-in, 500ms post-TTS
    echo suppression, 90-second sticky LISTENING session state, and fault-isolated
    EventBus publishing.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        stt: VoskSTT,
        tts: Optional[Any] = None,
        brain: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
        on_raw_audio_chunk: Optional[Callable[[bytes, float], None]] = None,
        base_dir: Optional[Path] = None,
        on_utterance: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.stt = stt
        self.tts = tts
        self.brain = brain
        self.bus = event_bus or get_event_bus()
        self.on_raw_audio_chunk = on_raw_audio_chunk
        self.on_utterance = on_utterance
        self.logger = logging.getLogger("ev.wakeword")

        voice_cfg = config.get("voice", {})
        ww_cfg = voice_cfg.get("wake_word", {})
        stt_cfg = voice_cfg.get("stt", {})

        self.enabled: bool = bool(ww_cfg.get("enabled", True))
        self.continuous_listening: bool = bool(
            ww_cfg.get("continuous_listening", voice_cfg.get("continuous_listening", True))
        )
        self.command_timeout_s: float = float(ww_cfg.get("command_timeout_seconds", 20.0))
        self.session_timeout_s: float = float(
            ww_cfg.get("session_timeout_seconds", voice_cfg.get("session_timeout_seconds", 90.0))
        )
        self.energy_gate_threshold: float = float(
            ww_cfg.get("energy_gate_threshold", voice_cfg.get("energy_threshold", 0.0018))
        )
        self.barge_in_energy_threshold: float = float(
            ww_cfg.get("barge_in_energy_threshold", voice_cfg.get("barge_in_rms_threshold", 0.025))
        )
        self.confirmation_sound: bool = bool(
            ww_cfg.get("confirmation_sound", voice_cfg.get("wake_chime_enabled", True))
        )
        self.min_confidence: float = float(stt_cfg.get("min_confidence", 0.50))
        self.silence_timeout_s: float = float(
            stt_cfg.get("silence_timeout_seconds", voice_cfg.get("silence_timeout_seconds", 1.1))
        )

        raw_phrases = ww_cfg.get("phrases", voice_cfg.get("wake_words", DEFAULT_WAKE_PHRASES))
        merged_phrases = list(raw_phrases) + [p for p in DEFAULT_WAKE_PHRASES if p not in raw_phrases]
        self.wake_phrases: List[str] = sorted(
            {self._normalize(p) for p in merged_phrases if p},
            key=len,
            reverse=True,
        )

        self._stop_event = threading.Event()
        self._active_command_mode: bool = False
        self._in_conversation_session: bool = False
        self._active_deadline: float = 0.0
        self._tts_active_flag: bool = False
        self._tts_end_ts: float = 0.0
        self._last_wake_ack_ts: float = 0.0
        self._last_partial_text: str = ""
        self._last_partial_ts: float = 0.0
        self._thread: Optional[threading.Thread] = None

        self.bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed)
        self.bus.subscribe(Topics.VOICE_TTS_STARTED, self._on_tts_started)
        self.bus.subscribe(Topics.VOICE_TTS_FINISHED, self._on_tts_finished)

    def _safe_publish(self, topic: str, payload: Dict[str, Any]) -> None:
        """Publish to EventBus safely without ever raising or crashing the voice loop."""
        try:
            self.bus.publish(topic, payload, source="wakeword")
        except (Exception, RecursionError) as exc:
            self.logger.error("Failed to publish topic '%s' from wakeword: %s", topic, exc)

    @staticmethod
    def _normalize(text: str) -> str:
        cleaned = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
        return re.sub(r"\s+", " ", cleaned).strip()

    @property
    def is_in_active_session(self) -> bool:
        if self._in_conversation_session and time.monotonic() < self._active_deadline:
            return True
        return False

    def arm_session(self) -> None:
        now = time.monotonic()
        self._active_command_mode = True
        self._in_conversation_session = True
        self._active_deadline = now + self.session_timeout_s

    @classmethod
    def is_sleep_command(cls, text: str) -> bool:
        if not text:
            return False
        norm = VoskSTT.normalize_indian_english_transcript(text)
        return bool(SLEEP_PHRASES_REGEX.search(norm) or SLEEP_PHRASES_REGEX.search(text))

    @classmethod
    def split_wake_word_and_command(cls, text: str) -> Tuple[bool, str]:
        cleaned = cls._normalize(text)
        if not cleaned:
            return False, ""
        if cleaned in STANDALONE_WAKE_UTTERANCES:
            return True, ""
        m = WAKE_PREFIX_REGEX.match(cleaned)
        if m:
            rem = m.group(1).strip()
            if rem in STANDALONE_WAKE_UTTERANCES or rem in NOISE_FILLER_TOKENS:
                rem = ""
            return True, rem
        return False, cleaned

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._active_command_mode = False
        self._in_conversation_session = False
        self._active_deadline = 0.0
        self._thread = threading.Thread(
            target=self._run_loop,
            name="EV-WakeWord-Loop",
            daemon=True,
        )
        self._thread.start()
        self._safe_publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0})
        self.logger.info(
            "Voice loop started (continuous_listening=%s | session_timeout=%.0fs | silence_timeout=%.2fs | stt=vosk)",
            self.continuous_listening,
            self.session_timeout_s,
            self.silence_timeout_s,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def _on_tts_started(self, event: Event) -> None:
        self._tts_active_flag = True

    def _on_tts_finished(self, event: Event) -> None:
        """
        After every reply, record the TTS end timestamp (for 500ms echo guard),
        refresh the 90-second session deadline, and restore Orb to LISTENING if armed.
        """
        self._tts_active_flag = False
        self._tts_end_ts = time.monotonic()
        if self._in_conversation_session:
            self._active_command_mode = True
            self._active_deadline = time.monotonic() + self.session_timeout_s
            self._safe_publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0})

    def trigger_manual_listen(self, play_chime: bool = True, reason: str = "manual") -> None:
        now = time.monotonic()
        # Debounce rapid duplicate triggers (e.g. from double-clap + button)
        if (now - self._last_wake_ack_ts) < 0.60 and self._in_conversation_session:
            return
        self._last_wake_ack_ts = now
        self._interrupt_tts_and_brain()
        self.stt.set_tts_mute(False)
        self._active_command_mode = True
        self._in_conversation_session = True
        self._active_deadline = now + self.session_timeout_s

        if play_chime and self.confirmation_sound:
            play_wake_chime()

        self._safe_publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0})
        self._safe_publish(Topics.VOICE_LISTENING_STARTED, {"reason": reason})

    def _interrupt_tts_and_brain(self) -> None:
        """Stop TTS audio immediately (<150ms) and cancel any streaming LLM generation."""
        self._tts_active_flag = False
        if self.brain is not None and hasattr(self.brain, "cancel"):
            try:
                self.brain.cancel()
            except Exception:
                pass
        if self.tts is not None and hasattr(self.tts, "interrupt"):
            try:
                self.tts.interrupt()
            except Exception:
                pass
        self._safe_publish(Topics.VOICE_INTERRUPT, {})

    def enter_standby(self) -> None:
        """Enter STANDING BY (`idle`) immediately."""
        self._active_command_mode = False
        self._in_conversation_session = False
        self._active_deadline = 0.0
        self._last_partial_text = ""
        self.stt.reset()
        self._safe_publish(Topics.VOICE_LISTENING_STOPPED, {})
        self._safe_publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0})

    def cancel_active_listen(self) -> None:
        self._interrupt_tts_and_brain()
        self.enter_standby()

    def inspect_transcript(self, text: str) -> Tuple[bool, str]:
        norm = self._normalize(text)
        if not norm:
            return False, ""

        if norm in STANDALONE_WAKE_UTTERANCES:
            return True, ""

        m = WAKE_PREFIX_REGEX.match(norm)
        if m:
            rem = m.group(1).strip()
            if rem in STANDALONE_WAKE_UTTERANCES or rem in NOISE_FILLER_TOKENS:
                rem = ""
            return True, rem

        for phrase in self.wake_phrases:
            pattern = rf"\b{re.escape(phrase)}\b"
            m_phrase = re.search(pattern, norm)
            if m_phrase:
                remainder = norm[m_phrase.end() :].strip()
                if remainder in STANDALONE_WAKE_UTTERANCES or remainder in NOISE_FILLER_TOKENS:
                    remainder = ""
                return True, remainder

        return False, ""

    @staticmethod
    def _is_meaningful_utterance(text: str) -> bool:
        norm = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower()).strip()
        norm = re.sub(r"\s+", " ", norm)
        if not norm or norm in NOISE_FILLER_TOKENS:
            return False
        words = norm.split()
        valid_short = {"hi", "no", "ok", "yes"}
        if len(words) == 1 and (len(words[0]) <= 2 or words[0] in NOISE_FILLER_TOKENS) and words[0] not in valid_short:
            return False
        if all(w in NOISE_FILLER_TOKENS for w in words):
            return False
        return True

    def _emit_partial_caption_throttled(self, text: str, now: float) -> None:
        clean = (text or "").strip()
        if not clean or clean == self._last_partial_text:
            return
        if (now - self._last_partial_ts) < 0.24:
            return
        self._last_partial_text = clean
        self._last_partial_ts = now
        self._safe_publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0})
        self._safe_publish(Topics.CAPTION, {"speaker": "user", "text": clean, "hold_ms": 1500})

    def _on_settings_changed(self, event: Event) -> None:
        key = event.payload.get("key")
        val = event.payload.get("value")
        if key == "wakeWord":
            self.enabled = bool(val)
        elif key == "continuousListening":
            self.continuous_listening = bool(val)
            if not val:
                self.enter_standby()

    def _acknowledge_wake_word(self, now: float, speak_ack: bool = True, reason: str = "wakeword") -> None:
        self._active_command_mode = True
        self._in_conversation_session = True
        self._active_deadline = now + self.session_timeout_s

        if (now - self._last_wake_ack_ts) > 1.0:
            self._last_wake_ack_ts = now
            if self.confirmation_sound:
                play_wake_chime()
            self._safe_publish(
                Topics.VOICE_WAKEWORD_DETECTED,
                {"phrase": "hey evie", "reason": reason, "timestamp": time.time()},
            )
            self._safe_publish(
                Topics.ORB_STATE,
                {"state": "listening", "progress": 0.0},
            )
            self._safe_publish(
                Topics.CAPTION,
                {"speaker": "ev", "text": "I'm listening.", "hold_ms": 2000},
            )
            if speak_ack and self.tts is not None and not getattr(self.tts, "is_speaking", False):
                try:
                    self.tts.speak("I'm listening.", block=False)
                except Exception as exc:
                    self.logger.error("TTS speak error on wake ack: %s", exc)

    def _handle_final_transcript(self, text: str, confidence: float = 1.0) -> None:
        """Helper used by unit tests and direct callers to process a final transcript."""
        cleaned = (text or "").strip()
        if not cleaned:
            return
        if self.is_sleep_command(cleaned):
            self._interrupt_tts_and_brain()
            self.enter_standby()
            self._safe_publish(Topics.CAPTION, {"speaker": "ev", "text": "Standing by, sir.", "hold_ms": 2000})
            if self.tts is not None:
                try:
                    self.tts.speak("Standing by, sir.", block=False)
                except Exception:
                    pass
            return

        triggered, stripped = self.inspect_transcript(cleaned)
        now = time.monotonic()
        if triggered:
            self.arm_session()
            if not stripped:
                self._acknowledge_wake_word(now, speak_ack=True, reason="wakeword")
                return
            cleaned = stripped
        elif not self.is_in_active_session:
            return

        if confidence < self.min_confidence:
            return

        self.arm_session()
        self._safe_publish(
            Topics.VOICE_USER_COMMAND,
            {"text": cleaned, "confidence": confidence, "wake_triggered": triggered, "source": "voice"},
        )
        if self.on_utterance is not None:
            self.on_utterance(cleaned, confidence)

    def _run_loop(self) -> None:
        if not self.stt.load_model():
            self.logger.info("Vosk microphone model not active; WakeWordLoop standing by.")
            while not self._stop_event.is_set():
                time.sleep(0.5)
            return

        rec = self.stt.create_recognizer()
        if rec is None:
            return

        silence_frames = 0
        barge_in_loud_frames = 0

        def _handle_raw(pcm: bytes) -> None:
            try:
                now_r = time.monotonic()
                tts_busy = (
                    self._tts_active_flag
                    or (self.tts is not None and getattr(self.tts, "is_speaking", False))
                    or (now_r - self._tts_end_ts) < 0.50
                )
                if not tts_busy and self.on_raw_audio_chunk is not None:
                    energy = compute_pcm16_rms(pcm)
                    self.on_raw_audio_chunk(pcm, energy)
            except Exception as exc:
                self.logger.debug("Raw audio chunk callback error: %s", exc)

        while not self._stop_event.is_set():
            try:
                for pcm_chunk in self.stt.iter_mic_chunks(
                    stop_check=self._stop_event.is_set,
                    on_raw_chunk=_handle_raw,
                ):
                    if self._stop_event.is_set():
                        break

                    try:
                        rms = compute_pcm16_rms(pcm_chunk)
                        now = time.monotonic()
                        tts_playing = self._tts_active_flag or (
                            self.tts is not None and getattr(self.tts, "is_speaking", False)
                        )
                        in_post_tts_cooldown = (now - self._tts_end_ts) < 0.50

                        # 1. Instant Voice Barge-In (<150ms of speech energy during TTS)
                        if tts_playing:
                            if rms >= self.barge_in_energy_threshold:
                                barge_in_loud_frames += 1
                                if barge_in_loud_frames >= 1:
                                    self.logger.info(
                                        "User barge-in detected (rms=%.3f); interrupting TTS and cancelling LLM.",
                                        rms,
                                    )
                                    self._interrupt_tts_and_brain()
                                    self.stt.set_tts_mute(False)
                                    try:
                                        rec.Reset()
                                    except Exception:
                                        pass
                                    self._active_command_mode = True
                                    self._in_conversation_session = True
                                    self._active_deadline = now + self.session_timeout_s
                                    barge_in_loud_frames = 0
                                    self._safe_publish(
                                        Topics.ORB_STATE,
                                        {"state": "listening", "progress": 0.0},
                                    )
                            else:
                                barge_in_loud_frames = 0
                            continue

                        barge_in_loud_frames = 0

                        # 2. Ignore STT for 500ms after TTS finishes so E.V. never hears itself
                        if in_post_tts_cooldown or self.stt.is_tts_speaking:
                            try:
                                rec.Reset()
                            except Exception:
                                pass
                            continue

                        # 3. 90-Second Session Timeout Check
                        if self._in_conversation_session and now > self._active_deadline:
                            self.logger.info(
                                "Session timeout (%.0fs of silence) elapsed. Returning to standby.",
                                self.session_timeout_s,
                            )
                            self.enter_standby()

                        # 4. Energy Gate (0.0018) with trailing hangover
                        if rms < self.energy_gate_threshold:
                            silence_frames += 1
                            if silence_frames > 18 and not self._last_partial_text:
                                continue
                        else:
                            silence_frames = 0

                        # 5. Silence endpointing (1.1s default so compound sentences aren't split)
                        if (
                            self._in_conversation_session
                            and self._last_partial_text
                            and (silence_frames * 0.125) >= self.silence_timeout_s
                        ):
                            silence_frames = 0
                            flush_res = rec.Result()
                            try:
                                flush = json.loads(flush_res or "{}")
                            except Exception:
                                flush = {}
                            raw_text = str(flush.get("text", "")).strip() or self._last_partial_text
                            self._last_partial_text = ""
                            if raw_text:
                                words_info = flush.get("result", [])
                                confs = [float(w.get("conf", 1.0)) for w in words_info if isinstance(w, dict)]
                                conf = sum(confs) / float(len(confs)) if confs else 0.85
                                norm_text = VoskSTT.normalize_indian_english_transcript(raw_text)
                                self.logger.info(
                                    "STT final | engine=vosk | conf=%.2f | text='%s'",
                                    conf,
                                    mask_pii(norm_text),
                                )
                                if (conf >= self.min_confidence or self.is_sleep_command(norm_text)) and self._is_meaningful_utterance(norm_text):
                                    is_final = True
                                    transcript = norm_text
                                    confidence = conf
                                else:
                                    continue
                            else:
                                continue
                        else:
                            is_final, transcript, confidence = self.stt.feed_pcm_chunk_detailed(
                                pcm_chunk, recognizer=rec
                            )

                        if not transcript:
                            continue

                        # Priority 0: "stop listening" / "stopped listening" / "stand by" works immediately
                        if self.is_sleep_command(transcript):
                            self.logger.info("Voice standby command ('%s') -> entering STANDBY.", mask_pii(transcript))
                            self._last_partial_text = ""
                            try:
                                rec.Reset()
                            except Exception:
                                pass
                            self._interrupt_tts_and_brain()
                            self.enter_standby()
                            self._safe_publish(
                                Topics.CAPTION,
                                {"speaker": "ev", "text": "Standing by, sir.", "hold_ms": 2000},
                            )
                            if self.tts is not None:
                                try:
                                    self.tts.speak("Standing by, sir.", block=False)
                                except Exception:
                                    pass
                            continue

                        triggered, stripped = self.inspect_transcript(transcript)

                        # Mode A: Armed Continuous 90-Second Session
                        if self._in_conversation_session:
                            if triggered and (not stripped or not self._is_meaningful_utterance(stripped)):
                                if is_final:
                                    self._last_partial_text = ""
                                    self._acknowledge_wake_word(now, speak_ack=True, reason="wakeword")
                                else:
                                    self._safe_publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0})
                                continue

                            display_text = stripped if (triggered and stripped) else transcript

                            if not is_final:
                                if self._is_meaningful_utterance(display_text):
                                    self._emit_partial_caption_throttled(display_text, now)
                            else:
                                self._last_partial_text = ""
                                if not self._is_meaningful_utterance(display_text):
                                    continue

                                if confidence < self.min_confidence:
                                    self.logger.info(
                                        "Dropping low-confidence transcript in session (conf=%.2f < %.2f): '%s'",
                                        confidence,
                                        self.min_confidence,
                                        mask_pii(display_text),
                                    )
                                    continue

                                self._active_deadline = now + self.session_timeout_s
                                self._safe_publish(
                                    Topics.CAPTION,
                                    {"speaker": "user", "text": display_text, "hold_ms": 3000},
                                )
                                self._safe_publish(
                                    Topics.VOICE_USER_COMMAND,
                                    {
                                        "text": display_text,
                                        "confidence": confidence,
                                        "wake_triggered": triggered,
                                        "source": "voice",
                                    },
                                )
                                if self.on_utterance is not None:
                                    self.on_utterance(display_text, confidence)
                            continue

                        # Mode B: Standby Mode (Requires Wake Word or Double-Clap)
                        if not self.enabled or not triggered:
                            continue

                        # Wake Word Detected in Standby Mode!
                        self._active_command_mode = True
                        self._in_conversation_session = True
                        self._active_deadline = now + self.session_timeout_s

                        if not stripped or not self._is_meaningful_utterance(stripped):
                            if is_final:
                                self._last_partial_text = ""
                                self._acknowledge_wake_word(now, speak_ack=True, reason="wakeword")
                            else:
                                self._safe_publish(
                                    Topics.ORB_STATE,
                                    {"state": "listening", "progress": 0.0},
                                )
                            continue

                        # Wake Word + In-breath Command ("Hey Evie, open Spotify")
                        if not is_final:
                            if self._is_meaningful_utterance(stripped):
                                self._emit_partial_caption_throttled(stripped, now)
                        else:
                            self._last_partial_text = ""
                            if not self._is_meaningful_utterance(stripped):
                                continue

                            if confidence < self.min_confidence:
                                self.logger.info(
                                    "Dropping low-confidence wake command (conf=%.2f < %.2f): '%s'",
                                    confidence,
                                    self.min_confidence,
                                    mask_pii(stripped),
                                )
                                continue

                            if self.confirmation_sound:
                                play_wake_chime()
                            self._safe_publish(
                                Topics.CAPTION,
                                {"speaker": "user", "text": stripped, "hold_ms": 3000},
                            )
                            self._safe_publish(
                                Topics.VOICE_USER_COMMAND,
                                {
                                    "text": stripped,
                                    "confidence": confidence,
                                    "wake_triggered": True,
                                    "source": "voice",
                                },
                            )
                            if self.on_utterance is not None:
                                self.on_utterance(stripped, confidence)
                    except (Exception, RecursionError) as chunk_exc:
                        self.logger.error("Error processing microphone chunk (continuing loop): %s", chunk_exc)
                        continue
            except (Exception, RecursionError) as stream_exc:
                self.logger.error("Microphone stream loop error (retrying in 0.5s): %s", stream_exc)
                time.sleep(0.5)


WakeWordDetector = WakeWordLoop
