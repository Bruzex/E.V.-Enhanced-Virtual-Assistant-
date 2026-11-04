#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Wake-Word, Double-Clap & Continuous Voice Loop (`voice/wakeword.py`)
========================================================================================================
Implements:
  1. Reliable Voice Standby ("stop listening" / "stopped listening" / "go to standby"):
     - Spoken "stop listening", "stopped listening", "stand by", "go to standby", or
       "go to sleep" immediately forces `STANDBY` mode, cancels any active TTS or LLM
       stream, and closes active listening session—without needing a manual mic click.
  2. Forgiving Double-Clap Detector:
     - Detects two sharp transient claps (`0.08s - 0.70s` apart) using both crest-factor
       and transient-onset ratios, immediately arming/refreshing the 90-second continuous
       listening session.
  3. Natural Silence Endpointing (`1.1s` default):
     - Prevents mid-sentence truncation when the user pauses briefly during compound
       commands (e.g., "send a hello message ... to Mommy on WhatsApp").
  4. Pure Wake Word Guard:
     - Saying only "Hey Evie" / "Evie" / "E.V." arms the 90s session and replies
       "I'm listening." without executing random commands.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

from core.event_bus import Event, EventBus, Topics, get_event_bus
from memory.sqlite_store import mask_pii
from voice.stt import VoskSTT

try:
    import sounddevice as sd  # type: ignore

    SOUNDDEVICE_AVAILABLE = True
except Exception:
    sd = None  # type: ignore
    SOUNDDEVICE_AVAILABLE = False

try:
    import winsound  # type: ignore

    WINSOUND_AVAILABLE = True
except ImportError:
    winsound = None  # type: ignore
    WINSOUND_AVAILABLE = False


class ClapDetector:
    """
    Time-domain double-clap transient detector for 16 kHz mono PCM frames.
    Combines crest-factor (`peak_abs / rms`) and sudden onset (`rms / prev_rms`)
    so laptop microphones reliably trigger on a natural double-clap while
    ignoring steady speech.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        min_peak_rms: float = 0.14,
        max_clap_duration_s: float = 0.25,
        min_gap_s: float = 0.08,
        max_gap_s: float = 0.70,
        cooldown_s: float = 1.5,
    ) -> None:
        self.sample_rate = sample_rate
        self.min_peak_rms = min_peak_rms
        self.max_clap_duration_s = max_clap_duration_s
        self.min_gap_s = min_gap_s
        self.max_gap_s = max_gap_s
        self.cooldown_s = cooldown_s

        self._in_burst: bool = False
        self._burst_start_t: float = 0.0
        self._first_clap_t: float = 0.0
        self._last_trigger_t: float = 0.0
        self._prev_rms: float = 0.01

    def process_frame(self, samples_f32: np.ndarray, now: Optional[float] = None) -> bool:
        """Return True when a valid double-clap transient pair completes."""
        t = now if now is not None else time.monotonic()
        if t - self._last_trigger_t < self.cooldown_s:
            return False

        if samples_f32.size == 0:
            return False

        rms = float(np.sqrt(np.mean(np.square(samples_f32))))
        peak_abs = float(np.max(np.abs(samples_f32)))
        crest = peak_abs / (rms + 1e-6)
        onset_ratio = rms / max(self._prev_rms, 0.008)
        self._prev_rms = 0.75 * self._prev_rms + 0.25 * rms

        is_sharp_impulse = rms >= self.min_peak_rms and (crest >= 1.85 or onset_ratio >= 2.8)

        if is_sharp_impulse:
            if not self._in_burst:
                self._in_burst = True
                self._burst_start_t = t
        else:
            if self._in_burst:
                burst_dur = t - self._burst_start_t
                self._in_burst = False
                if burst_dur <= self.max_clap_duration_s:
                    if self._first_clap_t > 0.0:
                        gap = self._burst_start_t - self._first_clap_t
                        if self.min_gap_s <= gap <= self.max_gap_s:
                            self._first_clap_t = 0.0
                            self._last_trigger_t = t
                            return True
                    self._first_clap_t = self._burst_start_t

        if self._first_clap_t > 0.0 and (t - self._first_clap_t) > self.max_gap_s:
            self._first_clap_t = 0.0

        return False


class WakeWordLoop:
    """
    Continuous microphone listener with:
      - Wake-word + double-clap detection
      - 90s active listening session window (refreshed on each command)
      - Instant voice "stop listening" / "stopped listening" / "go to standby"
      - Instant barge-in interruption while E.V. is speaking or generating
    """

    WAKE_REGEX = re.compile(
        r"\b(?:hey\s+evie|hey\s+e\.?\s*v\.?|hi\s+evie|hi\s+e\.?\s*v\.?|okay\s+evie|ok\s+evie|hello\s+evie|yo\s+evie|evie|e\.?v\.?|jarvis|hey\s+ivy|hey\s+avi|hey\s+heavy|hey\s+baby|he\s+be|hey\s+tv|hey\s+eb)\b",
        re.IGNORECASE,
    )

    BARE_WAKE_TAIL_NOISE = {
        "",
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
        "he",
        "be",
        "e",
        "v",
        "ev",
        "evie",
        "ivy",
        "avi",
        "tv",
        "eb",
        "to",
        "of",
        "in",
        "it",
        "is",
        "are",
        "the are",
    }

    INTERRUPT_PHRASES = re.compile(
        r"\b(?:stop\s+talking|stop\s+speaking|be\s+quiet|shut\s+up|silence|pause\s+speaking|hold\s+on|wait\s+a\s+second)\b",
        re.IGNORECASE,
    )
    CONTINUE_PHRASES = re.compile(
        r"^(?:please\s+)?(?:continue|go\s+on|carry\s+on|keep\s+talking|keep\s+going|resume(?:\s+speaking|\s+talking)?)$",
        re.IGNORECASE,
    )
    SLEEP_PHRASES = re.compile(
        r"\b(?:stop(?:ped)?\s+listening|go\s+to\s+(?:stand\s*by|sleep)|stand\s*by|pause\s+listening|sleep\s+mode)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        config: Dict[str, Any],
        *args: Any,
        stt: Optional[VoskSTT] = None,
        base_dir: Optional[Path] = None,
        tts: Optional[Any] = None,
        brain: Optional[Any] = None,
        on_raw_audio_chunk: Optional[Callable[..., None]] = None,
        on_utterance: Optional[Callable[[str, float], None]] = None,
        event_bus: Optional[EventBus] = None,
        **kwargs: Any,
    ) -> None:
        self.config = config

        # Handle flexible positional arguments
        for a in args:
            if isinstance(a, Path):
                base_dir = a
            elif isinstance(a, str):
                base_dir = Path(a)
            elif isinstance(a, VoskSTT) or hasattr(a, "accept_waveform"):
                stt = a
            elif isinstance(a, EventBus):
                event_bus = a
            elif callable(a) and on_utterance is None:
                on_utterance = a

        self.base_dir = base_dir or Path(__file__).resolve().parent.parent
        self.bus = event_bus or get_event_bus()
        self.stt = stt or VoskSTT(config=config, base_dir=self.base_dir, event_bus=self.bus)
        self.tts = tts
        self.brain = brain
        self.on_raw_audio_chunk = on_raw_audio_chunk
        self.on_utterance = on_utterance
        self.logger = logging.getLogger("ev.wakeword")

        voice_cfg = config.get("voice", {})
        wake_cfg = voice_cfg.get("wake_word", {})
        stt_cfg = voice_cfg.get("stt", {})

        self.sample_rate: int = int(voice_cfg.get("sample_rate", 16000))
        self.block_size: int = int(voice_cfg.get("block_size", 4000))
        self.wake_words = [
            w.lower()
            for w in voice_cfg.get(
                "wake_words",
                ["hey evie", "hey e.v.", "hey ev", "evie", "e.v.", "okay evie", "hi evie"],
            )
        ]
        self.double_clap_enabled: bool = bool(voice_cfg.get("double_clap_enabled", True))
        self.energy_threshold: float = float(voice_cfg.get("energy_threshold", 0.008))
        self.barge_in_rms_threshold: float = float(
            wake_cfg.get(
                "barge_in_energy_threshold",
                voice_cfg.get("barge_in_rms_threshold", 0.050),
            )
        )
        self.silence_timeout_s: float = float(
            stt_cfg.get(
                "silence_timeout_seconds",
                voice_cfg.get("silence_timeout_seconds", 1.1),
            )
        )
        self.continuous_listening: bool = bool(
            wake_cfg.get(
                "continuous_listening",
                voice_cfg.get("continuous_listening", True),
            )
        )
        self.session_timeout_s: float = float(
            wake_cfg.get(
                "session_timeout_seconds",
                voice_cfg.get("session_timeout_seconds", 90.0),
            )
        )
        self.min_confidence: float = float(stt_cfg.get("min_confidence", 0.50))
        self.wake_chime_enabled: bool = bool(voice_cfg.get("wake_chime_enabled", True))

        self.clap_detector = ClapDetector(
            sample_rate=self.sample_rate,
            min_peak_rms=float(voice_cfg.get("clap_min_rms", 0.14)),
            max_clap_duration_s=0.25,
            min_gap_s=0.08,
            max_gap_s=0.70,
        )

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._armed: bool = False
        self._session_until: float = 0.0
        self._last_voice_t: float = 0.0
        self._has_speech_in_turn: bool = False
        self._barge_in_frames: int = 0
        self._manual_mic_override: bool = False

        self.bus.subscribe(Topics.SETTINGS_CHANGED, self._on_settings_changed)

    def _on_settings_changed(self, event: Event) -> None:
        key = event.payload.get("key")
        val = event.payload.get("value")
        if key == "wakeWord":
            self.double_clap_enabled = bool(val)
        elif key == "continuousListening":
            self.continuous_listening = bool(val)
            if not self.continuous_listening:
                self.enter_standby()
            else:
                self.arm_session()

    @property
    def is_in_active_session(self) -> bool:
        """True if manual mic is active or within the 90s wake-word session window."""
        if self._manual_mic_override or self._armed:
            return True
        if self.continuous_listening and time.monotonic() < self._session_until:
            return True
        return False

    @property
    def _in_conversation_session(self) -> bool:
        return self._armed

    @property
    def _active_command_mode(self) -> bool:
        return self._manual_mic_override

    def arm_session(self) -> None:
        """Open or extend the 90-second continuous listening session window."""
        now = time.monotonic()
        self._armed = True
        self._session_until = now + self.session_timeout_s
        self._last_voice_t = now
        self._has_speech_in_turn = False

    def enter_standby(self) -> None:
        """Immediately exit active session and return to wake-word/double-clap standby."""
        self._armed = False
        self._manual_mic_override = False
        self._session_until = 0.0
        self._has_speech_in_turn = False
        self.stt.reset()
        self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="wakeword")

    def cancel_active_listen(self) -> None:
        """Compatibility method for GUI cancel."""
        self.enter_standby()

    def _play_wake_chime(self) -> None:
        if not self.wake_chime_enabled:
            return
        if WINSOUND_AVAILABLE and winsound is not None:
            try:
                threading.Thread(
                    target=lambda: winsound.Beep(880, 75),
                    daemon=True,
                ).start()
            except Exception:
                pass

    def start(self) -> None:
        """Start background microphone capture thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="EV-WakeWordLoop",
            daemon=True,
        )
        self._thread.start()
        self.logger.info(
            "Voice loop started (continuous_listening=%s | session_timeout=%.0fs | silence_timeout=%.2fs | stt=%s)",
            self.continuous_listening,
            self.session_timeout_s,
            self.silence_timeout_s,
            getattr(self.stt, "active_engine", "vosk"),
        )

    def stop(self) -> None:
        """Stop microphone capture thread cleanly."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def trigger_manual_listen(self) -> None:
        """Called when user clicks HUD mic button or presses Space."""
        self._interrupt_tts_and_brain()
        self._manual_mic_override = True
        self.arm_session()
        self._play_wake_chime()
        self.bus.publish(Topics.WAKEWORD_DETECTED, {"trigger": "manual"}, source="wakeword")
        self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")

    def _interrupt_tts_and_brain(self) -> bool:
        """Stop any ongoing TTS playback or LLM generation immediately."""
        interrupted = False
        if self.brain is not None and hasattr(self.brain, "cancel"):
            self.brain.cancel()
        if self.tts is not None and getattr(self.tts, "is_speaking", False):
            self.tts.interrupt()
            interrupted = True
        return interrupted

    @classmethod
    def split_wake_word_and_command(cls, text: str) -> Tuple[bool, str]:
        """
        Check if `text` contains a wake word (`Hey Evie`, `E.V.`, `Evie`, etc.).
        Returns `(has_wake_word, remaining_command_after_wake_word)`.
        """
        cleaned = (text or "").strip()
        if not cleaned:
            return False, ""

        m = cls.WAKE_REGEX.search(cleaned)
        if not m:
            return False, cleaned

        remainder = cleaned[m.end() :].strip(" ,.:;!?-").lower()
        rem_low = re.sub(r"[^a-z0-9\s]", "", remainder).strip()
        if rem_low in cls.BARE_WAKE_TAIL_NOISE:
            return True, ""
        return True, remainder

    @classmethod
    def inspect_transcript(cls, text: str) -> Tuple[bool, str]:
        """Compatibility alias for split_wake_word_and_command."""
        return cls.split_wake_word_and_command(text)

    @classmethod
    def is_sleep_command(cls, text: str) -> bool:
        """Return True if `text` contains a voice standby/stop-listening phrase."""
        if not text:
            return False
        norm = VoskSTT.normalize_indian_english_transcript(text)
        return bool(cls.SLEEP_PHRASES.search(norm) or cls.SLEEP_PHRASES.search(text))

    def _run_loop(self) -> None:
        if not SOUNDDEVICE_AVAILABLE or sd is None:
            self.logger.warning("sounddevice unavailable; voice loop running in text/manual standby mode.")
            return

        try:
            with sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                dtype="int16",
                channels=1,
            ) as stream:
                while not self._stop_event.is_set():
                    data, _overflowed = stream.read(self.block_size)
                    pcm_bytes = bytes(data)
                    self.process_audio_chunk(pcm_bytes)
        except Exception as exc:
            self.logger.warning("Microphone stream unavailable (%s); voice loop standing by.", exc)

    def process_audio_chunk(self, pcm_bytes: bytes) -> None:
        """Process a single 16-bit PCM audio frame for barge-in, double-clap, wake-word, and STT."""
        if not pcm_bytes:
            return

        samples_i16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        samples_f32 = samples_i16.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(np.square(samples_f32)))) if samples_f32.size else 0.0
        now = time.monotonic()

        # Raw audio chunk callback (for clap detector or external visualizer)
        if self.on_raw_audio_chunk is not None:
            try:
                self.on_raw_audio_chunk(pcm_bytes, rms)
            except TypeError:
                try:
                    self.on_raw_audio_chunk(pcm_bytes)
                except Exception:
                    pass
            except Exception:
                pass

        tts_speaking = bool(self.tts is not None and getattr(self.tts, "is_speaking", False))

        # 1. Double-Clap Detection (works even in standby to arm continuous listening)
        if not tts_speaking and self.double_clap_enabled and self.clap_detector.process_frame(samples_f32, now=now):
            self.logger.info("Double-clap detected -> arming continuous listening session.")
            self._interrupt_tts_and_brain()
            self.arm_session()
            self._play_wake_chime()
            self.bus.publish(Topics.WAKEWORD_DETECTED, {"trigger": "double_clap"}, source="wakeword")
            self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")
            if self.tts is not None:
                self.tts.speak("I'm listening.", block=False)
            return

        # 2. Barge-In While E.V. Is Speaking
        if tts_speaking:
            if rms >= self.barge_in_rms_threshold:
                self._barge_in_frames += 1
                if self._barge_in_frames >= 2:
                    self.logger.info("Voice barge-in detected (rms=%.3f) -> interrupting TTS.", rms)
                    self._interrupt_tts_and_brain()
                    self.arm_session()
                    self._barge_in_frames = 0
                    self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")
            else:
                self._barge_in_frames = 0
            return

        self._barge_in_frames = 0

        # 3. Check if active session expired back to standby
        if self._armed and not self._manual_mic_override and now >= self._session_until:
            self._armed = False
            self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="wakeword")

        if rms >= self.energy_threshold:
            self._last_voice_t = now
            self._has_speech_in_turn = True

        # 4. Feed STT Engine
        if not self.stt.is_ready:
            return

        is_final, text, conf = self.stt.accept_waveform(pcm_bytes)
        cleaned = text.strip()

        if not is_final:
            if cleaned:
                if self.is_sleep_command(cleaned):
                    self.logger.info("Voice standby command detected in partial ('%s') -> STANDBY.", cleaned)
                    self._interrupt_tts_and_brain()
                    self.enter_standby()
                    if self.tts is not None:
                        self.tts.speak("Standing by, sir.", block=False)
                    return

                has_wake, rem = self.split_wake_word_and_command(cleaned)
                if has_wake and not self.is_in_active_session:
                    self.arm_session()
                    self._play_wake_chime()
                    self.bus.publish(
                        Topics.WAKEWORD_DETECTED,
                        {"trigger": "voice_partial", "text": cleaned},
                        source="wakeword",
                    )
                    self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")
                elif self.is_in_active_session:
                    self.bus.publish(
                        Topics.TRANSCRIPT_PARTIAL,
                        {"text": rem if has_wake else cleaned},
                        source="wakeword",
                    )

            if self._has_speech_in_turn and (now - self._last_voice_t) >= self.silence_timeout_s:
                flushed = self.stt.flush_final()
                self._has_speech_in_turn = False
                if flushed.text:
                    self._handle_final_transcript(flushed.text, flushed.confidence)
            return

        # 5. Finalized Utterance from Vosk/Whisper
        self._has_speech_in_turn = False
        if cleaned:
            self._handle_final_transcript(cleaned, conf)

    def _handle_final_transcript(self, text: str, confidence: float = 1.0) -> None:
        """Process a finalized STT utterance."""
        cleaned = (text or "").strip()
        if not cleaned:
            return

        # Voice standby command ALWAYS forces STANDBY immediately
        if self.is_sleep_command(cleaned):
            self.logger.info("Voice standby command ('%s') -> forcing STANDBY.", mask_pii(cleaned))
            self._interrupt_tts_and_brain()
            self.enter_standby()
            if self.tts is not None:
                self.tts.speak("Standing by, sir.", block=False)
            return

        has_wake, command_after_wake = self.split_wake_word_and_command(cleaned)

        # Case A: Utterance started with wake word ("Hey Evie ...")
        if has_wake:
            self.arm_session()
            self._manual_mic_override = False
            self.bus.publish(
                Topics.WAKEWORD_DETECTED,
                {"trigger": "voice", "text": cleaned},
                source="wakeword",
            )
            # Bare wake word ONLY ("Hey Evie" with no command after it)
            if not command_after_wake:
                self._play_wake_chime()
                self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="wakeword")
                if self.tts is not None:
                    self.tts.speak("I'm listening.", block=False)
                return
            cleaned = command_after_wake

        # Case B: Not in active session and no wake word -> ignore background chatter
        elif not self.is_in_active_session:
            return

        # Case C: Quick voice controls
        if self.INTERRUPT_PHRASES.search(cleaned):
            self._interrupt_tts_and_brain()
            self.arm_session()
            return

        if self.CONTINUE_PHRASES.match(cleaned):
            self.arm_session()
            if self.tts is not None:
                resumed, _ = self.tts.resume(block=False)
                if resumed:
                    return

        # Case D: Confidence gate for normal commands
        if confidence < self.min_confidence:
            self.logger.debug(
                "Ignoring low-confidence transcript (conf=%.2f < %.2f): '%s'",
                confidence,
                self.min_confidence,
                mask_pii(cleaned),
            )
            return

        # Refresh the 90-second continuous listening session and dispatch utterance
        self.arm_session()
        self._manual_mic_override = False
        self.bus.publish(
            Topics.TRANSCRIPT_FINAL,
            {"text": cleaned, "confidence": confidence},
            source="wakeword",
        )
        if self.on_utterance is not None:
            self.on_utterance(cleaned, confidence)


def compute_pcm16_rms(pcm_bytes: bytes) -> float:
    if not pcm_bytes:
        return 0.0
    samples_i16 = np.frombuffer(pcm_bytes, dtype=np.int16)
    if samples_i16.size == 0:
        return 0.0
    samples_f32 = samples_i16.astype(np.float32) / 32767.0
    return float(np.sqrt(np.mean(np.square(samples_f32))))


WakeWordDetector = WakeWordLoop
