"""
E.V. (Enhanced Virtual Assistant) — Offline Voice Subsystem
===========================================================
Speech-to-text (Vosk), text-to-speech (Piper), wake-word detection,
and energy-based double-clap acoustic triggers.
"""

from voice.clap_detector import ClapDetector
from voice.stt import VoskSTT
from voice.tts import PiperTTS
from voice.wakeword import WakeWordDetector, WakeWordLoop, compute_pcm16_rms

__all__ = [
    "ClapDetector",
    "PiperTTS",
    "VoskSTT",
    "WakeWordDetector",
    "WakeWordLoop",
    "compute_pcm16_rms",
]
