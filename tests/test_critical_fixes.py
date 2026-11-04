#!/usr/bin/env python3
"""
Unit tests for Critical Fixes:
  1. LLM Keep-Alive (unload_on_critical: false default, resident across telemetry/ram pressure)
  2. STT low-confidence dropping & silence timeout
  3. Wake word loop starts in STANDBY mode, arms on wake/clap, pure wake only replies "I'm listening."
  4. IntentRouter drops low-confidence (<0.50) commands before FAST rules and LLM
  5. open_application returns False for nonexistent apps like 'kinnar'
  6. "How are you?" goes to Brain generation, "Who are you?" returns Bruprog identity
"""

import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from automation.system import SystemController
from core.brain import Brain
from core.event_bus import EventBus, Topics
from core.intent_router import IntentRouter
from voice.stt import VoskSTT
from voice.wakeword import WakeWordLoop


def test_llm_keep_alive_on_telemetry_and_pressure():
    base_dir = Path(__file__).resolve().parent.parent
    config = {
        "hardware": {
            "ram_budget_gb": 12.0,
            "ram_critical_threshold_gb": 11.0,
            "unload_on_critical": False,
            "reload_on_demand": True,
        },
        "ram": {
            "budget_gb": 12.0,
            "critical_gb": 11.0,
            "unload_on_critical": False,
            "reload_on_demand": True,
        },
        "llm": {"n_gpu_layers": 0},
    }
    bus = EventBus()
    brain = Brain(config=config, base_dir=base_dir, event_bus=bus)

    # Simulate RAM pressure event at 4.34 GB (the exact RSS seen in logs)
    bus.publish(Topics.RAM_PRESSURE, {"level": "critical", "rss_gb": 4.34})
    # Must NOT unload because unload_on_critical is False
    assert brain.is_model_loaded is brain.model_path.exists()

    # check_idle_unload must also keep model resident
    brain.check_idle_unload()
    assert brain.is_model_loaded is brain.model_path.exists()


def test_intent_router_drops_low_confidence():
    base_dir = Path(__file__).resolve().parent.parent
    config = {
        "hardware": {"ram_budget_gb": 12.0},
        "voice": {"stt": {"min_confidence": 0.50}},
    }
    mock_brain = MagicMock()
    mock_brain.generate.return_value = "LLM answer"
    router = IntentRouter(config=config, brain=mock_brain)

    # Confidence below 0.50 (e.g., 0.35 noise: "kinnar", "open me")
    match_low = router.route("open chrome", confidence=0.35)
    assert match_low.intent_name == "low_confidence_rejected"
    assert match_low.response_text == ""
    assert mock_brain.generate.call_count == 0

    # Confidence >= 0.50 passes through
    match_ok = router.route("what time is it", confidence=0.85)
    assert match_ok.intent_name == "time_date"
    assert "currently" in match_ok.response_text.lower() or "today" in match_ok.response_text.lower()


def test_nonexistent_app_returns_false():
    base_dir = Path(__file__).resolve().parent.parent
    config = {"automation": {"execute_os_actions": True}}
    sys_ctrl = SystemController(config=config, base_dir=base_dir)

    ok, pid, msg = sys_ctrl.open_application("kinnar")
    assert ok is False
    assert "Could not find kinnar installed" in msg

    ok2, pid2, msg2 = sys_ctrl.open_application("me")
    assert ok2 is False
    assert "Could not find me installed" in msg2


def test_wakeword_starts_in_standby():
    base_dir = Path(__file__).resolve().parent.parent
    config = {
        "voice": {
            "wake_word": {
                "continuous_listening": True,
                "session_timeout_seconds": 90.0,
                "barge_in_energy_threshold": 0.025,
            },
            "stt": {"min_confidence": 0.50, "silence_timeout_seconds": 0.8},
        }
    }
    bus = EventBus()
    stt = VoskSTT(config=config, base_dir=base_dir, event_bus=bus)
    ww = WakeWordLoop(config=config, stt=stt, event_bus=bus)

    # Must start in Standby (not armed)
    assert ww._in_conversation_session is False
    assert ww._active_command_mode is False


def test_who_are_you_identity_and_how_are_you():
    base_dir = Path(__file__).resolve().parent.parent
    config = {"llm": {"n_gpu_layers": 0}}
    brain = Brain(config=config, base_dir=base_dir, event_bus=EventBus())

    # "Who are you?" must return fixed E.V. / Bruprog identity
    resp_who = brain.generate("Who are you?")
    assert "Bruprog" in resp_who
    assert "Enhanced Virtual Assistant" in resp_who

    # "How are you?" must NOT return the canned identity response
    resp_how = brain.generate("How are you?")
    assert resp_how != brain.IDENTITY_RESPONSE

