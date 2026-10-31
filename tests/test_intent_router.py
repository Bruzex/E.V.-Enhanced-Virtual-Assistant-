#!/usr/bin/env python3
"""
Unit tests for `core/intent_router.py`, `core/scheduler.py`, `voice/clap_detector.py`,
and `memory/rag.py`. Runs 100% offline without requiring a physical microphone,
Android phone, or Selenium Chrome browser.
"""

from __future__ import annotations

import struct
import time
import unittest
from pathlib import Path
import tempfile
import yaml

from core.event_bus import EventBus, Topics
from core.intent_router import IntentRouter
from core.scheduler import TimedCommandScheduler
from memory.rag import RAGMemoryManager
from remote.phone_adb import PhoneADBController
from remote.whatsapp import WhatsAppController
from voice.clap_detector import ClapDetector


class MockBrain:
    def __init__(self) -> None:
        self.calls: int = 0
        self.last_prompt: str = ""
        self.last_context: str | None = None

    def generate(self, prompt: str, extra_context: str | None = None) -> str:
        self.calls += 1
        self.last_prompt = prompt
        self.last_context = extra_context
        return f"LLM answered: {prompt}"


class TestIntentRouterAndPhase3(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_dir = Path(self.tmp_dir.name)
        self.config = {
            "hardware": {"ram_budget_gb": 4.0},
            "gui": {"default_countdown_seconds": 30},
            "voice": {
                "clap_detector": {
                    "enabled": True,
                    "required_claps": 2,
                    "energy_threshold": 0.32,
                    "double_clap_min_gap_ms": 100,
                    "double_clap_max_gap_ms": 500,
                    "cooldown_seconds": 0.5,
                }
            },
            "memory": {
                "enabled": True,
                "sqlite_path": "data/memory.db",
                "chroma_dir": "data/chroma",
            },
            "remote": {
                "phone_adb": {"enabled": True, "host": "127.0.0.1", "port": 5555},
                "whatsapp": {"enabled": False},
            },
        }
        self.bus = EventBus()
        self.brain = MockBrain()
        self.rag = RAGMemoryManager(self.config, self.base_dir)
        self.phone = PhoneADBController(self.config, self.base_dir, event_bus=self.bus)
        self.whatsapp = WhatsAppController(self.config, self.base_dir, event_bus=self.bus)
        self.scheduler = TimedCommandScheduler(self.config, event_bus=self.bus, phone_adb=self.phone)
        self.router = IntentRouter(
            config=self.config,
            brain=self.brain,
            scheduler=self.scheduler,
            phone_adb=self.phone,
            whatsapp=self.whatsapp,
            rag=self.rag,
            event_bus=self.bus,
        )

    def tearDown(self) -> None:
        self.scheduler.shutdown()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_fast_path_never_calls_llm(self) -> None:
        commands = [
            "Hey E.V., set volume to 50%",
            "mute audio",
            "system status report",
            "what time is it",
            "unlock my phone",
            "send an sms to Mom saying On my way",
            "send a whatsapp message to Mom saying Hello",
        ]
        for cmd in commands:
            match = self.router.route(cmd)
            self.assertTrue(match.handled_by_fast_path, f"Failed fast-path for: {cmd}")
        self.assertEqual(self.brain.calls, 0)

    def test_compound_workspace_and_timed_call(self) -> None:
        countdowns = []
        self.bus.subscribe(Topics.COUNTDOWN_UPDATE, lambda e: countdowns.append(e.payload))
        match = self.router.route(
            "Hey E.V., open my coding workspace and prepare a call to Mom in 15 seconds"
        )
        self.assertEqual(match.intent_name, "compound_workspace_and_timed_call")
        self.assertTrue(match.handled_by_fast_path)
        self.assertEqual(match.slots["seconds"], 15)
        self.assertEqual(self.brain.calls, 0)
        time.sleep(0.1)
        self.assertTrue(any(c["total"] == 15 and c["kind"] == "call" for c in countdowns))

    def test_scheduler_schedule_cancel_and_run_now(self) -> None:
        events = []
        self.bus.subscribe(Topics.COUNTDOWN_UPDATE, lambda e: events.append(e.payload))

        self.scheduler.schedule(seconds=10, label="Dim lights")
        time.sleep(0.05)
        self.assertTrue(self.scheduler.is_running)
        self.assertTrue(self.scheduler.cancel(speak_feedback=False))
        self.assertEqual(events[-1]["status"], "cancelled")

        self.scheduler.schedule(seconds=20, label="Instant task", kind="notify")
        time.sleep(0.05)
        self.assertTrue(self.scheduler.run_now())
        time.sleep(0.2)
        self.assertEqual(events[-1]["status"], "completed")

    def test_clap_detector_single_and_double_clap(self) -> None:
        claps = []
        self.bus.subscribe(Topics.VOICE_CLAP_DETECTED, lambda e: claps.append(e.payload))
        detector = ClapDetector(self.config, event_bus=self.bus)

        loud = struct.pack("<400h", *([25000, -25000] * 200))
        quiet = struct.pack("<400h", *([40, -40] * 200))

        self.assertFalse(detector.process_pcm_frame(loud))
        detector.process_pcm_frame(quiet)
        time.sleep(0.18)
        self.assertTrue(detector.process_pcm_frame(loud))
        self.assertEqual(len(claps), 1)
        self.assertEqual(claps[0]["count"], 2)

    def test_rag_memory_remember_recall_and_llm_context(self) -> None:
        m1 = self.router.route("remember that my favorite editor is Neovim")
        self.assertTrue(m1.handled_by_fast_path)

        m2 = self.router.route("what do you remember about favorite editor?")
        self.assertTrue(m2.handled_by_fast_path)
        self.assertIn("Neovim", m2.response_text)

        # Open-ended query should inject RAG memory into Brain.generate()
        m3 = self.router.route("Why do developers enjoy using my favorite editor?")
        self.assertFalse(m3.handled_by_fast_path)
        self.assertEqual(self.brain.calls, 1)
        self.assertIsNotNone(self.brain.last_context)
        self.assertIn("Neovim", self.brain.last_context or "")


    def test_dynamic_contacts_save_and_call(self) -> None:
        call_events = []
        self.bus.subscribe(Topics.CALL_STATE, lambda e: call_events.append(e.payload))

        # 1. Save new contact
        res_save = self.router.route("remember that John's phone number is +1 (555) 019-9988")
        self.assertEqual(res_save.intent_name, "personal_fact_statement")
        self.assertIn("John", res_save.response_text)
        self.assertIn("+1 (555) 019-9988", res_save.response_text)

        # 2. Call dynamic contact
        res_call = self.router.route("call John")
        self.assertEqual(res_call.intent_name, "phone_call")
        self.assertTrue(len(call_events) >= 1)
        self.assertEqual(call_events[-1]["name"], "John")
        self.assertEqual(call_events[-1]["number"], "+1 (555) 019-9988")

        # 3. Call unknown contact prompts to save
        res_unknown = self.router.route("call Alice")
        self.assertEqual(res_unknown.intent_name, "phone_call")
        self.assertIn("don't have a number saved for Alice", res_unknown.response_text)

    def test_selective_workspace_routing(self) -> None:
        match = self.router.route("open only VS Code and browser from my coding workspace")
        self.assertEqual(match.intent_name, "workspace_selective_launch")
        self.assertTrue(match.handled_by_fast_path)


if __name__ == "__main__":
    unittest.main()
