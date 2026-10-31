"""
Unit tests for voice/clap_detector.py.
"""

import struct
import time
from core.event_bus import EventBus, Topics
from voice.clap_detector import ClapDetector


def test_clap_detector_silence_ignores():
    bus = EventBus()
    detected = []
    bus.subscribe(Topics.VOICE_CLAP_DETECTED, lambda ev: detected.append(ev))

    detector = ClapDetector(config={}, event_bus=bus)
    silence = b"\x00\x00" * 400

    detector.process_pcm_frame(silence, rms_energy=0.0)
    assert len(detected) == 0


def test_clap_detector_double_clap():
    bus = EventBus()
    detected = []
    bus.subscribe(Topics.VOICE_CLAP_DETECTED, lambda ev: detected.append(ev))

    config = {
        "voice": {
            "clap_detector": {
                "enabled": True,
                "required_claps": 2,
                "energy_threshold": 0.25,
                "double_clap_min_gap_ms": 10,
                "double_clap_max_gap_ms": 300,
            }
        }
    }
    detector = ClapDetector(config=config, event_bus=bus)

    # Sharp impulse: 1 high sample followed by quiet samples (high peak-to-RMS)
    impulse = struct.pack("<h", 30000) + b"\x00\x00" * 399
    silence = b"\x00\x00" * 400

    # First clap
    detector.process_pcm_frame(impulse, rms_energy=0.5)
    assert len(detected) == 0

    # Quiet frame between claps
    detector.process_pcm_frame(silence, rms_energy=0.0)

    # Wait 50ms (within min/max gap)
    time.sleep(0.05)

    # Second clap
    detector.process_pcm_frame(impulse, rms_energy=0.5)
    assert len(detected) == 1
