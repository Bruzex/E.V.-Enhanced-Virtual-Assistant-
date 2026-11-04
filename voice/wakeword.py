#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Wake Word, <150ms Barge-In & 90s Session Loop (`voice/wakeword.py`)
=======================================================================================================
Implements:
  1. Instant Voice Barge-In (<150–200ms):
     While TTS is playing (`tts_speaking`), microphone RMS energy above
     `barge_in_energy_threshold` (`0.030`) for ~200ms (2 frames of 100-125ms)
     immediately calls `tts.interrupt()` and `brain.cancel()` without waiting
     for the sentence to finish!
  2. Self-Echo Suppression (During TTS + 500ms After):
     Mutes `ClapDetector` and ignores Vosk STT during TTS plus 500ms after
     `Topics.VOICE_TTS_FINISHED` so E.V. never hears its own speaker output.
  3. Sticky 90-Second LISTENING State:
     After every reply, the Orb returns to `LISTENING` and refreshes the 90s
     session timer. `STANDING BY` (`idle`) is entered ONLY when the user says
     `"stop listening"`, `"go to sleep"`, or `"stand by"`, or after 90 seconds
     of complete silence when `continuous_listening` is disabled.
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

SLEEP_PHRASES = {
    "stop listening",
    "go to sleep",
    "stand by",
    "standby",
    "pause listening",
    "sleep mode",
    "stop continuous listening",
}

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
    echo suppression, and 90-second sticky LISTENING session state.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        stt: VoskSTT,
        tts: Optional[Any] = None,
        brain: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
        on_raw_audio_chunk: Optional[Callable[[bytes, float], None]] = None,
    ) -> None:
        self.config = config
        self.stt = stt
        self.tts = tts
        self.brain = brain
        self.bus = event_bus or get_event_bus()
        self.on_raw_audio_chunk = on_raw_audio_chunk
        self.logger = logging.getLogger("ev.wakeword")

        ww_cfg = config.get("voice", {}).get("wake_word", {})
        self.enabled: bool = bool(ww_cfg.get("enabled", True))
        self.continuous_listening: bool = bool(ww_cfg.get("continuous_listening", True))
        self.command_timeout_s: float = float(ww_cfg.get("command_timeout_seconds", 20.0))
        self.session_timeout_s: float = float(ww_cfg.get("session_timeout_seconds", 90.0))
        self.energy_gate_threshold: float = float(ww_cfg.get("energy_gate_threshold", 0.0018))
        self.barge_in_energy_threshold: float = float(ww_cfg.get("barge_in_energy_threshold", 0.030))
        self.confirmation_sound: bool = bool(ww_cfg.get("confirmation_sound", True))

        raw_phrases = ww_cfg.get("phrases", DEFAULT_WAKE_PHRASES)
        merged_phrases = list(raw_phrases) + [p for p in DEFAULT_WAKE_PHRASES if p not in raw_phrases]
        self.wake_phrases: List[str] = sorted(
            {self._normalize(p) for p in merged_phrases if p},
            key=len,
            reverse=True,
        )

        self._stop_event = threading.Event()
        self._active_command_mode: bool = True
        self._in_conversation_session: bool = True
        self._active_deadline: float = time.monotonic() + self.session_timeout_s
        self._tts_active_flag: bool = False
        self._tts_end_ts: float = 0.0
        self._last_wake_ack_ts: float = 0.0
        self._last_partial_text: str = ""
        self._last_partial_ts: float = 0.0
        self._thread: Optional[threading.Thread] = None

        self.bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed)
        self.bus.subscribe(Topics.VOICE_TTS_STARTED, self._on_tts_started)
        self.bus.subscribe(Topics.VOICE_TTS_FINISHED, self._on_tts_finished)
        self.bus.subscribe(Topics.VOICE_CLAP_DETECTED, self._on_clap_detected)

    @staticmethod
    def _normalize(text: str) -> str:
        cleaned = re.sub(r"[^a-z0-9\s]", " ", (text or "").lower())
        return re.sub(r"\s+", " ", cleaned).strip()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._active_command_mode = True
        self._in_conversation_session = True
        self._active_deadline = time.monotonic() + self.session_timeout_s
        self._thread = threading.Thread(
            target=self._run_loop,
            name="EV-WakeWord-Loop",
            daemon=True,
        )
        self._thread.start()
        self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")
        self.logger.info(
            "Voice loop started (continuous_listening=%s | session_timeout=%.0fs | energy_gate=%.4f)",
            self.continuous_listening,
            self.session_timeout_s,
            self.energy_gate_threshold,
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
        refresh the 90-second session deadline, and restore Orb to LISTENING.
        """
        self._tts_active_flag = False
        self._tts_end_ts = time.monotonic()
        if self._in_conversation_session or self.continuous_listening:
            self._active_command_mode = True
            self._active_deadline = time.monotonic() + self.session_timeout_s
            self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")

    def _on_clap_detected(self, event: Event) -> None:
        """Double-clap opens the same 90s listening session as 'Hey Evie'."""
        now = time.monotonic()
        if self._tts_active_flag or (now - self._tts_end_ts) < 0.50:
            return
        self._acknowledge_wake_word(now, speak_ack=False, reason="double_clap")

    def trigger_manual_listen(self, play_chime: bool = True, reason: str = "manual") -> None:
        self._interrupt_tts_and_brain()
        self.stt.set_tts_mute(False)
        self._active_command_mode = True
        self._in_conversation_session = True
        self._active_deadline = time.monotonic() + self.session_timeout_s

        if play_chime and self.confirmation_sound:
            play_wake_chime()

        self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")
        self.bus.publish(Topics.VOICE_LISTENING_STARTED, {"reason": reason}, source="wakeword")

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
        self.bus.publish("voice.interrupt", {}, source="wakeword")

    def enter_standby(self) -> None:
        """Enter STANDING BY (`idle`) ONLY when requested or after 90s timeout."""
        self._active_command_mode = False
        self._in_conversation_session = False
        self.bus.publish(Topics.VOICE_LISTENING_STOPPED, {}, source="wakeword")
        self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="wakeword")

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
            if rem in STANDALONE_WAKE_UTTERANCES:
                rem = ""
            return True, rem

        for phrase in self.wake_phrases:
            pattern = rf"\b{re.escape(phrase)}\b"
            m_phrase = re.search(pattern, norm)
            if m_phrase:
                remainder = norm[m_phrase.end() :].strip()
                if remainder in STANDALONE_WAKE_UTTERANCES:
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
        self.bus.publish(
            Topics.ORB_STATE,
            {"state": "listening", "progress": 0.0},
            source="wakeword",
        )
        self.bus.publish(
            Topics.CAPTION,
            {"speaker": "user", "text": clean, "hold_ms": 1500},
            source="wakeword",
        )

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

    def _acknowledge_wake_word(self, now: float, speak_ack: bool = True, reason: str = "wakeword") -> None:
        self._active_command_mode = True
        self._in_conversation_session = True
        self._active_deadline = now + self.session_timeout_s

        if (now - self._last_wake_ack_ts) > 1.2:
            self._last_wake_ack_ts = now
            if self.confirmation_sound:
                play_wake_chime()
            self.bus.publish(
                Topics.VOICE_WAKEWORD_DETECTED,
                {"phrase": "hey evie", "reason": reason, "timestamp": time.time()},
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
            if speak_ack and self.tts is not None and not self.tts.is_speaking:
                self.tts.speak("I'm listening.", block=False)

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
            # Mute ClapDetector during TTS and for 500ms after TTS finishes
            now_r = time.monotonic()
            tts_busy = (
                self._tts_active_flag
                or (self.tts is not None and getattr(self.tts, "is_speaking", False))
                or (now_r - self._tts_end_ts) < 0.50
            )
            if not tts_busy and self.on_raw_audio_chunk is not None:
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
            tts_playing = self._tts_active_flag or (
                self.tts is not None and getattr(self.tts, "is_speaking", False)
            )
            in_post_tts_cooldown = (now - self._tts_end_ts) < 0.50

            # -----------------------------------------------------------------
            # 1. Instant Voice Barge-In (~200ms of speech energy during TTS)
            # -----------------------------------------------------------------
            if tts_playing:
                if rms >= self.barge_in_energy_threshold:
                    barge_in_loud_frames += 1
                    # 2 consecutive 100-125ms chunks (~200ms) immediately interrupts TTS & cancels Brain!
                    if barge_in_loud_frames >= 2:
                        self.logger.info("User barge-in detected (rms=%.3f); interrupting TTS and cancelling LLM.", rms)
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
                        self.bus.publish(
                            Topics.ORB_STATE,
                            {"state": "listening", "progress": 0.0},
                            source="wakeword",
                        )
                else:
                    barge_in_loud_frames = 0
                continue

            barge_in_loud_frames = 0

            # -----------------------------------------------------------------
            # 2. Ignore STT for 500ms after TTS finishes so E.V. never hears itself
            # -----------------------------------------------------------------
            if in_post_tts_cooldown or self.stt.is_tts_speaking:
                try:
                    rec.Reset()
                except Exception:
                    pass
                continue

            # -----------------------------------------------------------------
            # 3. 90-Second Session Timeout Check
            # -----------------------------------------------------------------
            if (self._active_command_mode or self._in_conversation_session) and now > self._active_deadline:
                if not self.continuous_listening:
                    self.enter_standby()
                else:
                    self._active_deadline = now + self.session_timeout_s

            # -----------------------------------------------------------------
            # 4. Energy Gate (0.0018) with 16-frame trailing hangover
            # -----------------------------------------------------------------
            if rms < self.energy_gate_threshold:
                silence_frames += 1
                if silence_frames > 16:
                    continue
            else:
                silence_frames = 0

            is_final, transcript, confidence = self.stt.feed_pcm_chunk_detailed(pcm_chunk, recognizer=rec)
            if not transcript:
                continue

            triggered, stripped = self.inspect_transcript(transcript)

            # Check for explicit sleep command ("stop listening", "go to sleep", "stand by")
            check_norm = self._normalize(stripped if (triggered and stripped) else transcript)
            if is_final and any(sp in check_norm for sp in SLEEP_PHRASES):
                self.enter_standby()
                self._last_partial_text = ""
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

            # Pure Wake Word ("Hey Evie" / "Evie" alone)
            if triggered and (not stripped or not self._is_meaningful_utterance(stripped)):
                if is_final:
                    self._last_partial_text = ""
                    self._acknowledge_wake_word(now, speak_ack=True, reason="wakeword")
                else:
                    self.bus.publish(
                        Topics.ORB_STATE,
                        {"state": "listening", "progress": 0.0},
                        source="wakeword",
                    )
                continue

            if triggered and stripped:
                self._active_command_mode = True
                self._in_conversation_session = True
                self._active_deadline = now + self.session_timeout_s

            # Mode A: Active 90s Session or Continuous Listening Mode
            if self._active_command_mode or self._in_conversation_session or self.continuous_listening:
                display_text = stripped if (triggered and stripped) else transcript

                if not is_final:
                    if self._is_meaningful_utterance(display_text):
                        self._emit_partial_caption_throttled(display_text, now)
                else:
                    self._last_partial_text = ""
                    if not self._is_meaningful_utterance(display_text):
                        continue

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

            # Mode B: Standby Mode (after user explicitly said "stop listening")
            if not self.enabled:
                continue

            if triggered:
                self._active_command_mode = True
                self._in_conversation_session = True
                self._active_deadline = now + self.session_timeout_s
                if is_final and stripped and self._is_meaningful_utterance(stripped):
                    self._last_partial_text = ""
                    if self.confirmation_sound:
                        play_wake_chime()
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
                elif is_final:
                    self._acknowledge_wake_word(now, speak_ack=True, reason="wakeword")


# Backwards-compatible alias
WakeWordDetector = WakeWordLoop
