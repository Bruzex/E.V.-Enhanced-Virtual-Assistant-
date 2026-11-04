"""
Unit tests for `core/event_bus.py`, `voice/stt.py`, and `voice/wakeword.py`.
Verifies:
  1. EventBus non-reentrancy (zero `maximum recursion depth exceeded` when a
     subscriber re-publishes the same topic).
  2. Complete `Topics` registry (`VOICE_WAKEWORD_DETECTED` / `WAKEWORD_DETECTED`,
     `VOICE_PARTIAL_TRANSCRIPT` / `TRANSCRIPT_PARTIAL`, `VOICE_FINAL_TRANSCRIPT` /
     `TRANSCRIPT_FINAL`, `VOICE_CLAP_DETECTED`, `ORB_STATE`).
  3. 100% Vosk-only STT (zero `faster-whisper` imports or config keys).
  4. Contiguous phone digit normalization & Indian-English corrections.
  5. Voice `"stop listening"` / `"stopped listening"` -> `STANDBY` and `"Hey Evie"` -> `LISTENING`.
"""

import tempfile
import unittest
from pathlib import Path

from core.event_bus import Event, EventBus, Topics
from voice.clap_detector import ClapDetector
from voice.stt import VoskSTT, convert_spoken_numbers, join_spoken_digits
from voice.wakeword import WakeWordLoop, compute_pcm16_rms


class TestVoicePipelineAndEventBus(unittest.TestCase):
    def test_event_bus_non_reentrant_never_recurses(self) -> None:
        bus = EventBus()
        call_counts = {
            Topics.VOICE_WAKEWORD_DETECTED: 0,
            Topics.ORB_STATE: 0,
            Topics.VOICE_PARTIAL_TRANSCRIPT: 0,
            Topics.VOICE_FINAL_TRANSCRIPT: 0,
        }

        # Subscribers that maliciously/accidentally re-publish the exact same topic
        def _recursive_wake(event: Event) -> None:
            call_counts[Topics.VOICE_WAKEWORD_DETECTED] += 1
            bus.publish(Topics.VOICE_WAKEWORD_DETECTED, {"loop": True})

        def _recursive_orb(event: Event) -> None:
            call_counts[Topics.ORB_STATE] += 1
            bus.publish(Topics.ORB_STATE, {"state": "listening"})

        def _recursive_partial(event: Event) -> None:
            call_counts[Topics.VOICE_PARTIAL_TRANSCRIPT] += 1
            bus.publish(Topics.TRANSCRIPT_PARTIAL, {"text": "hey"})

        def _recursive_final(event: Event) -> None:
            call_counts[Topics.VOICE_FINAL_TRANSCRIPT] += 1
            bus.publish(Topics.TRANSCRIPT_FINAL, {"text": "hey evie"})

        bus.subscribe(Topics.VOICE_WAKEWORD_DETECTED, _recursive_wake)
        bus.subscribe(Topics.ORB_STATE, _recursive_orb)
        bus.subscribe(Topics.VOICE_PARTIAL_TRANSCRIPT, _recursive_partial)
        bus.subscribe(Topics.VOICE_FINAL_TRANSCRIPT, _recursive_final)

        # Must complete cleanly with count == 1 each and ZERO RecursionError
        bus.publish(Topics.WAKEWORD_DETECTED, {"phrase": "hey evie"})
        bus.publish(Topics.ORB_STATE, {"state": "listening"})
        bus.publish(Topics.VOICE_PARTIAL_TRANSCRIPT, {"text": "hey"})
        bus.publish(Topics.VOICE_FINAL_TRANSCRIPT, {"text": "hey evie"})

        self.assertEqual(call_counts[Topics.VOICE_WAKEWORD_DETECTED], 1)
        self.assertEqual(call_counts[Topics.ORB_STATE], 1)
        self.assertEqual(call_counts[Topics.VOICE_PARTIAL_TRANSCRIPT], 1)
        self.assertEqual(call_counts[Topics.VOICE_FINAL_TRANSCRIPT], 1)

    def test_no_whisper_anywhere_in_stt_or_config(self) -> None:
        base_dir = Path(__file__).resolve().parent.parent
        stt_src = (base_dir / "voice" / "stt.py").read_text(encoding="utf-8")
        cfg_src = (base_dir / "config.yaml").read_text(encoding="utf-8")
        req_src = (base_dir / "requirements.txt").read_text(encoding="utf-8")

        self.assertNotIn("faster_whisper", stt_src)
        self.assertNotIn("WhisperModel", stt_src)
        self.assertNotIn("whisper_model", cfg_src)
        self.assertNotIn("faster-whisper", req_src)

    def test_contiguous_phone_digit_joining(self) -> None:
        raw = "remember that my mobile number is nine five eight to one nine eight one six five"
        norm = VoskSTT.normalize_indian_english_transcript(raw)
        self.assertIn("9582198165", norm)

        raw2 = "Mom's phone number is double nine eight one triple zero two four five"
        norm2 = convert_spoken_numbers(join_spoken_digits(raw2))
        self.assertIn("9981000245", norm2)

    def test_indian_english_accent_corrections(self) -> None:
        self.assertEqual(
            VoskSTT.normalize_indian_english_transcript("stopped listening"),
            "stop listening",
        )
        fixed_wa = VoskSTT.normalize_indian_english_transcript(
            "send a halo message to mumbai on what's up"
        )
        self.assertIn("send a hello message to mommy", fixed_wa.lower())
        self.assertIn("whatsapp", fixed_wa.lower())

    def test_hey_evie_arms_listening_and_stop_listening_enters_standby(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base_dir = Path(td)
            bus = EventBus()
            orb_states = []
            bus.subscribe(Topics.ORB_STATE, lambda e: orb_states.append(e.payload.get("state")))

            config = {
                "voice": {
                    "wake_word": {
                        "enabled": True,
                        "continuous_listening": True,
                        "session_timeout_seconds": 90.0,
                        "confirmation_sound": False,
                    },
                    "stt": {
                        "engine": "vosk",
                        "sample_rate": 16000,
                        "silence_timeout_seconds": 1.1,
                    },
                }
            }
            stt = VoskSTT(config=config, base_dir=base_dir, event_bus=bus)
            ww = WakeWordLoop(config=config, stt=stt, event_bus=bus)

            self.assertFalse(ww.is_in_active_session)

            # "Hey Evie" arms listening session and publishes ORB_STATE = "listening"
            ww._handle_final_transcript("Hey Evie", confidence=0.90)
            self.assertTrue(ww.is_in_active_session)
            self.assertIn("listening", orb_states)

            # "stopped listening" forces STANDBY ("idle")
            ww._handle_final_transcript("stopped listening", confidence=0.80)
            self.assertFalse(ww.is_in_active_session)
            self.assertEqual(orb_states[-1], "idle")


if __name__ == "__main__":
    unittest.main()
