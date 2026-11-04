"""
Unit tests for voice pipeline (VoskSTT, PiperTTS, WakeWordLoop).
"""

import struct
from pathlib import Path
from core.event_bus import EventBus, Topics
from voice.stt import VoskSTT
from voice.tts import PiperTTS
from voice.wakeword import WakeWordLoop, compute_pcm16_rms


def test_compute_pcm16_rms():
    # Complete silence
    silence = b"\x00\x00" * 100
    assert compute_pcm16_rms(silence) == 0.0

    # Max amplitude sine-wave/square-wave
    max_amp = struct.pack("<h", 32767) * 100
    rms = compute_pcm16_rms(max_amp)
    assert 0.99 <= rms <= 1.0


def test_wakeword_phrase_inspection():
    config = {
        "voice": {
            "wake_word": {
                "enabled": True,
                "phrases": ["hey ev", "hey e v", "hey e.v.", "okay ev"],
                "command_timeout_seconds": 6.0,
            }
        }
    }
    bus = EventBus()
    base_dir = Path(__file__).resolve().parent.parent
    stt = VoskSTT(config=config, base_dir=base_dir, event_bus=bus)
    ww = WakeWordLoop(config=config, stt=stt, event_bus=bus)

    # Clean wake phrase only
    matched, remainder = ww.inspect_transcript("Hey E.V.")
    assert matched is True
    assert remainder == ""

    # Inline single-breath utterance
    matched, remainder = ww.inspect_transcript("Hey E.V., open my coding workspace and call Mom.")
    assert matched is True
    assert "open my coding workspace" in remainder
    assert "call mom" in remainder

    # Unrelated phrase
    matched, remainder = ww.inspect_transcript("Good morning world")
    assert matched is False


def test_stt_echo_suppression():
    bus = EventBus()
    base_dir = Path(__file__).resolve().parent.parent
    config = {"voice": {"stt": {"sample_rate": 16000}}}
    stt = VoskSTT(config=config, base_dir=base_dir, event_bus=bus)

    assert stt.is_muted is False

    # Simulate TTS started
    bus.publish(Topics.VOICE_TTS_STARTED, {"text": "Speaking now"})
    assert stt.is_muted is True

    # While muted, feed_pcm_chunk returns immediately without transcribing
    is_final, text = stt.feed_pcm_chunk(b"\x00\x00" * 100)
    assert is_final is False
    assert text == ""

    # Simulate TTS finished
    bus.publish(Topics.VOICE_TTS_FINISHED, {"text": "Speaking now"})
    assert stt.is_muted is False
