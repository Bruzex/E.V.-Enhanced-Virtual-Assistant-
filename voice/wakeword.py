#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Indian English Wake-Word, Confirmation Tone & Continuous Session Loop
=========================================================================================================
Implements a unified, non-competing background microphone loop with 5 key improvements:

  1. Forgiving Indian English Wake-Word Matching (Priority 1):
     Recognizes "Hey Evie", "Hey E.V.", "Evie", "Okay Evie", "Hi Evie", "Hey EV",
     plus common Vosk `vosk-model-small-en-us` phonetic transcriptions of Indian
     English pronunciation ("hey heavy", "hey baby", "hey tv", "hey a v", "hey v",
     "hey ivy", "hey easy", "hey avi", "hey every", "he be", "evie", "e v", "ivy").
  2. Careful Low Energy Gate (`energy_gate_threshold: 0.0025`):
     Lowered from `0.006` to `0.0025` with a 12-frame trailing hangover so soft
     laptop microphone input or quiet wake phrases are never clipped before reaching Vosk.
  3. Short Listening Confirmation Sound + Visual Feedback (Priority 1):
     Whenever a wake word or double-clap triggers, plays a short non-blocking
     confirmation chime (`play_wake_chime()`) and updates the HUD Status Orb to
     `listening` with a clear `"I'm listening."` caption so the user immediately
     knows E.V. heard them.
  4. Usable Multi-Turn Continuous Listening Session (Priority 4):
     Once woken by `"Hey Evie"`, double-clap, or the Mic button (or when
     `continuous_listening: true` is active), E.V. stays in an active listening
     session (`_in_conversation_session = True`) for natural back-and-forth
     conversation without needing to repeat `"Hey Evie"` before every sentence.
     Saying `"stop listening"`, `"go to sleep"`, or `"stand by"` cleanly exits
     continuous mode back to `Standing by`.
  5. Confidence Forwarding & Post-TTS Echo Protection (Priority 2 & 5):
     Uses `VoskSTT.feed_pcm_chunk_detailed()` to compute per-word recognition
     confidence and passes `confidence` in `Topics.VOICE_USER_COMMAND` so
     `IntentRouter` can reject low-confidence misheard noise safely.
"""

from __future__ import annotations

import logging
import math
import re
import struct
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import winsound  # type: ignore
    WINSOUND_AVAILABLE = True
except ImportError:
    winsound = None  # type: ignore
    WINSOUND_AVAILABLE = False

from core.event_bus import Event, EventBus, Topics, get_event_bus
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
    """
    Play a short, pleasant non-blocking confirmation tone on Windows when the
    wake word ("Hey Evie") or double-clap is detected so the user knows E.V. is listening.
    """
    if not WINSOUND_AVAILABLE or winsound is None or not sys.platform.startswith("win"):
        return

    def _chime_worker() -> None:
        try:
            winsound.Beep(880, 75)
            winsound.Beep(1175, 95)
        except Exception:
            pass

    threading.Thread(target=_chime_worker, name="EV-WakeChime", daemon=True).start()


# Comprehensive wake phrases tuned for Indian English pronunciation on Vosk small model
DEFAULT_WAKE_PHRASES: List[str] = [
    "hey evie",
    "hey e v",
    "hey e.v.",
    "hey ev",
    "hi evie",
    "hi e v",
    "hi ev",
    "hello evie",
    "hello ev",
    "okay evie",
    "ok evie",
    "okay e v",
    "okay ev",
    "ok ev",
    "evie",
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
    "jarvis",
]

# Regex catching Indian English phonetic variations of "Hey Evie" / "Evie" / "Okay Evie" at utterance start
WAKE_PREFIX_REGEX = re.compile(
    r"^(?:"
    r"(?:hey|hi|hello|okay|ok|yo)\s+(?:evie|e\s*\.?\s*v\.?|eve|ivy|avi|easy|tv|a\s+v|v|heavy|baby|every|movie|he\s+be|e\s+the|e\s+b|eb)"
    r"|(?:evie|e\s+v|e\.v\.|jarvis)"
    r")\b[\s,.:;-]*(.*)$",
    re.IGNORECASE,
)

# Phrases that explicitly put E.V. back into standby / sleep mode
SLEEP_PHRASES = {
    "stop listening",
    "go to sleep",
    "stand by",
    "standby",
    "pause listening",
    "sleep mode",
    "stop continuous listening",
}

# Short noise/filler tokens from background hum or mic pops that should be ignored
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
    "to",
    "of",
    "in",
    "it",
    "is",
    "that",
}


class WakeWordLoop:
    """
    Unified background microphone stream handling:
      - Forgiving Indian English wake-word detection ("Hey Evie", "Hey E.V.", "Evie", "Okay Evie")
      - Audible & visual wake confirmation
      - Multi-turn continuous listening session (stay awake until "stop listening" or timeout)
      - Real-time voice barge-in (interrupting TTS when user speaks)
    """

    def __init__(
        self,
        config: Dict[str, Any],
        stt: VoskSTT,
        tts: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
        on_raw_audio_chunk: Optional[Callable[[bytes, float], None]] = None,
    ) -> None:
        self.config = config
        self.stt = stt
        self.tts = tts
        self.bus = event_bus or get_event_bus()
        self.on_raw_audio_chunk = on_raw_audio_chunk
        self.logger = logging.getLogger("ev.wakeword")

        ww_cfg = config.get("voice", {}).get("wake_word", {})
        self.enabled: bool = bool(ww_cfg.get("enabled", True))
        self.continuous_listening: bool = bool(ww_cfg.get("continuous_listening", True))
        self.command_timeout_s: float = float(ww_cfg.get("command_timeout_seconds", 15.0))
        self.session_timeout_s: float = float(ww_cfg.get("session_timeout_seconds", 60.0))
        # Lowered energy gate (0.0025) so quiet Indian English consonants/vowels aren't dropped
        self.energy_gate_threshold: float = float(ww_cfg.get("energy_gate_threshold", 0.0025))
        self.barge_in_energy_threshold: float = float(ww_cfg.get("barge_in_energy_threshold", 0.030))
        self.confirmation_sound: bool = bool(ww_cfg.get("confirmation_sound", True))

        raw_phrases = ww_cfg.get("phrases", DEFAULT_WAKE_PHRASES)
        merged_phrases = list(raw_phrases) + [p for p in DEFAULT_WAKE_PHRASES if p not in raw_phrases]
        # Sort longest phrases first so "hey evie" matches before "evie"
        self.wake_phrases: List[str] = sorted(
            {self._normalize(p) for p in merged_phrases if p},
            key=len,
            reverse=True,
        )

        self._stop_event = threading.Event()
        self._active_command_mode: bool = self.continuous_listening
        self._in_conversation_session: bool = self.continuous_listening
        self._active_deadline: float = time.monotonic() + self.session_timeout_s
        self._last_wake_ack_ts: float = 0.0
        self._thread: Optional[threading.Thread] = None

        self.bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed)

    @staticmethod
    def _normalize(text: str) -> str:
        cleaned = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
        return re.sub(r"\s+", " ", cleaned).strip()

    def start(self) -> None:
        """Start the background wake-word and continuous voice capture daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="EV-WakeWord-Loop",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(
            "Voice loop started (continuous_listening=%s | wake_word=%s | energy_gate=%.4f)",
            self.continuous_listening,
            self.enabled,
            self.energy_gate_threshold,
        )

    def stop(self) -> None:
        """Stop the background wake-word thread cleanly."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def trigger_manual_listen(self, play_chime: bool = True, reason: str = "manual") -> None:
        """
        Called when the user says 'Hey Evie', double-claps, or clicks the central Mic button.
        Immediately interrupts TTS if speaking, plays confirmation chime, and opens a
        continuous listening session.
        """
        if self.tts is not None and self.tts.is_speaking:
            self.tts.interrupt()
        self.stt.set_tts_mute(False)
        self._active_command_mode = True
        self._in_conversation_session = True
        self._active_deadline = time.monotonic() + max(self.command_timeout_s, self.session_timeout_s)

        if play_chime and self.confirmation_sound:
            play_wake_chime()

        self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")
        self.bus.publish(Topics.VOICE_LISTENING_STARTED, {"reason": reason}, source="wakeword")

    def enter_standby(self) -> None:
        """Exit active listening session and return Orb to idle ('Standing by')."""
        self._active_command_mode = False
        self._in_conversation_session = False
        self.bus.publish(Topics.VOICE_LISTENING_STOPPED, {}, source="wakeword")
        self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="wakeword")

    def cancel_active_listen(self) -> None:
        """Pause active command capture (or interrupt speaking) and return Orb to idle."""
        if self.tts is not None and self.tts.is_speaking:
            self.tts.interrupt()
        self.enter_standby()

    def inspect_transcript(self, text: str) -> Tuple[bool, str]:
        """
        Check whether `text` contains a wake phrase (including Indian English phonetic matches).
        Returns `(wake_triggered, remainder_command_text)`.
        """
        norm = self._normalize(text)
        if not norm:
            return False, ""

        # 1. Check regex prefix match for Indian English phonetic variants first
        m = WAKE_PREFIX_REGEX.match(norm)
        if m:
            return True, m.group(1).strip()

        # 2. Check configured wake phrases anywhere in utterance
        for phrase in self.wake_phrases:
            pattern = rf"\b{re.escape(phrase)}\b"
            m_phrase = re.search(pattern, norm)
            if m_phrase:
                remainder = norm[m_phrase.end() :].strip()
                return True, remainder

        return False, ""

    @staticmethod
    def _is_meaningful_utterance(text: str) -> bool:
        """Filter out 1-syllable background noise pops ('the', 'uh', 'hm', 'e')."""
        norm = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower()).strip()
        norm = re.sub(r"\s+", " ", norm)
        if not norm or norm in NOISE_FILLER_TOKENS:
            return False
        words = norm.split()
        # Reject single tiny filler words unless they are valid short commands
        valid_short = {"hi", "no", "ok", "yes", "up", "on"}
        if len(words) == 1 and (len(words[0]) <= 2 or words[0] in NOISE_FILLER_TOKENS) and words[0] not in valid_short:
            return False
        # Reject 2-word filler combinations like "the a", "in the", "uh the"
        if all(w in NOISE_FILLER_TOKENS for w in words):
            return False
        return True

    # =========================================================================
    # Internal Audio Processing Loop (Wake Word + Continuous Session + Barge-In)
    # =========================================================================
    def _on_settings_changed(self, event: Event) -> None:
        key = event.payload.get("key")
        val = event.payload.get("value")
        if key == "wakeWord":
            self.enabled = bool(val)
        elif key == "continuousListening":
            self.continuous_listening = bool(val)
            self._active_command_mode = bool(val)
            self._in_conversation_session = bool(val)
            if val:
                self._active_deadline = time.monotonic() + self.session_timeout_s
                self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")
            else:
                self.enter_standby()

    def _acknowledge_wake_word(self, now: float) -> None:
        """Provide immediate visual + audio confirmation that E.V. heard 'Hey Evie'."""
        self._active_command_mode = True
        self._in_conversation_session = True
        self._active_deadline = now + max(self.command_timeout_s, self.session_timeout_s)

        if (now - self._last_wake_ack_ts) > 1.2:
            self._last_wake_ack_ts = now
            if self.confirmation_sound:
                play_wake_chime()
            self.bus.publish(
                Topics.VOICE_WAKEWORD_DETECTED,
                {"phrase": "hey evie", "timestamp": time.time()},
                source="wakeword",
            )
            self.bus.publish(
                Topics.ORB_STATE,
                {"state": "listening", "progress": 0.0},
                source="wakeword",
            )
            self.bus.publish(
                Topics.CAPTION,
                {"speaker": "ev", "text": "I'm listening.", "hold_ms": 2000},
                source="wakeword",
            )

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
            if self.on_raw_audio_chunk is not None:
                energy = compute_pcm16_rms(pcm)
                self.on_raw_audio_chunk(pcm, energy)

        for pcm_chunk in self.stt.iter_mic_chunks(
            stop_check=self._stop_event.is_set,
            on_raw_chunk=_handle_raw,
        ):
            if self._stop_event.is_set():
                break

            rms = compute_pcm16_rms(pcm_chunk)
            now = time.monotonic()
            tts_speaking = (self.tts is not None and self.tts.is_speaking) or self.stt.is_tts_speaking

            # -----------------------------------------------------------------
            # 1. Voice Barge-In Detection while E.V. is speaking
            # -----------------------------------------------------------------
            if tts_speaking:
                if rms >= self.barge_in_energy_threshold:
                    barge_in_loud_frames += 1
                    is_final, barge_text, barge_conf = self.stt.feed_pcm_chunk_detailed(
                        pcm_chunk,
                        recognizer=rec,
                        allow_during_tts=True,
                    )
                    if (
                        barge_text
                        and self._is_meaningful_utterance(barge_text)
                        and not self.stt.is_likely_tts_echo(barge_text)
                    ) or barge_in_loud_frames >= 3:
                        if self.tts is not None:
                            self.tts.interrupt()
                        self.stt.set_tts_mute(False)
                        self._active_command_mode = True
                        self._in_conversation_session = True
                        self._active_deadline = now + self.session_timeout_s
                        barge_in_loud_frames = 0
                        self.bus.publish(
                            Topics.ORB_STATE,
                            {"state": "listening", "progress": 0.0},
                            source="wakeword",
                        )
                        if is_final and barge_text and self._is_meaningful_utterance(barge_text):
                            _, stripped = self.inspect_transcript(barge_text)
                            clean_cmd = stripped if stripped else barge_text
                            self.bus.publish(
                                Topics.CAPTION,
                                {"speaker": "user", "text": clean_cmd, "hold_ms": 3000},
                                source="wakeword",
                            )
                            self.bus.publish(
                                Topics.VOICE_USER_COMMAND,
                                {"text": clean_cmd, "confidence": barge_conf, "source": "voice"},
                                source="wakeword",
                            )
                else:
                    barge_in_loud_frames = 0
                continue

            barge_in_loud_frames = 0

            # -----------------------------------------------------------------
            # 2. Check session timeout when not in permanent continuous mode
            # -----------------------------------------------------------------
            if (
                self._active_command_mode
                and not self.continuous_listening
                and not self._in_conversation_session
                and now > self._active_deadline
            ):
                self.enter_standby()

            # -----------------------------------------------------------------
            # 3. Gentle Energy Gate: skip dead-silent frames, allow 12 trailing
            #    frames (1.5s) after speech so Vosk finalizes utterances cleanly
            # -----------------------------------------------------------------
            if rms < self.energy_gate_threshold:
                silence_frames += 1
                if silence_frames > 12:
                    continue
            else:
                silence_frames = 0

            is_final, transcript, confidence = self.stt.feed_pcm_chunk_detailed(pcm_chunk, recognizer=rec)
            if not transcript:
                continue

            # Check if the utterance contains a wake phrase ("Hey Evie", "Evie", etc.)
            triggered, stripped = self.inspect_transcript(transcript)

            # Check if the user said "stop listening" / "go to sleep" / "stand by"
            check_norm = self._normalize(stripped if triggered and stripped else transcript)
            if is_final and check_norm in SLEEP_PHRASES:
                self.enter_standby()
                self.bus.publish(
                    Topics.CAPTION,
                    {"speaker": "user", "text": check_norm, "hold_ms": 2000},
                    source="wakeword",
                )
                self.bus.publish(
                    Topics.VOICE_USER_COMMAND,
                    {"text": "stop listening", "confidence": 1.0, "source": "voice"},
                    source="wakeword",
                )
                continue

            # If wake word was spoken alone ("Hey Evie" / "Evie"), acknowledge immediately!
            if triggered and not stripped:
                if is_final:
                    self._acknowledge_wake_word(now)
                else:
                    self.bus.publish(
                        Topics.ORB_STATE,
                        {"state": "listening", "progress": 0.0},
                        source="wakeword",
                    )
                continue

            # If wake word + command spoken together ("Hey Evie open WhatsApp"):
            if triggered and stripped:
                self._active_command_mode = True
                self._in_conversation_session = True
                self._active_deadline = now + self.session_timeout_s

            # Mode A: Active Session or Continuous Listening Mode
            if self._active_command_mode or self._in_conversation_session or self.continuous_listening:
                display_text = stripped if (triggered and stripped) else transcript

                if not is_final:
                    if self._is_meaningful_utterance(display_text):
                        self.bus.publish(
                            Topics.ORB_STATE,
                            {"state": "listening", "progress": 0.0},
                            source="wakeword",
                        )
                        self.bus.publish(
                            Topics.CAPTION,
                            {"speaker": "user", "text": display_text, "hold_ms": 0},
                            source="wakeword",
                        )
                else:
                    if not self._is_meaningful_utterance(display_text):
                        # If wake word was triggered with only noise after it, just say "I'm listening."
                        if triggered:
                            self._acknowledge_wake_word(now)
                        continue

                    # Keep conversation session alive for multi-turn follow-ups
                    self._active_deadline = now + self.session_timeout_s
                    self.bus.publish(
                        Topics.CAPTION,
                        {"speaker": "user", "text": display_text, "hold_ms": 3000},
                        source="wakeword",
                    )
                    self.bus.publish(
                        Topics.VOICE_USER_COMMAND,
                        {
                            "text": display_text,
                            "confidence": confidence,
                            "wake_triggered": triggered,
                            "source": "voice",
                        },
                        source="wakeword",
                    )
                continue

            # Mode B: Standby Wake-Word-Only Mode (when user said "stop listening")
            if not self.enabled:
                continue

            if triggered:
                self._acknowledge_wake_word(now)
                if is_final and stripped and self._is_meaningful_utterance(stripped):
                    self.bus.publish(
                        Topics.CAPTION,
                        {"speaker": "user", "text": stripped, "hold_ms": 3000},
                        source="wakeword",
                    )
                    self.bus.publish(
                        Topics.VOICE_USER_COMMAND,
                        {
                            "text": stripped,
                            "confidence": confidence,
                            "wake_triggered": True,
                            "source": "voice",
                        },
                        source="wakeword",
                    )


# Backwards compatibility alias
WakeWordDetector = WakeWordLoop
