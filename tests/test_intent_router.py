#!/usr/bin/env python3
"""
Unit tests for `core/intent_router.py`, `core/scheduler.py`, `memory/rag.py`,
coding generation (`data/workspace/code/`), multi-step WhatsApp/Call task planner,
background web search, and short-term pronoun/coreference resolution.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from automation.web_search import WebSearchTool
from core.event_bus import EventBus, Topics
from core.intent_router import IntentRouter
from core.scheduler import TimedCommandScheduler
from memory.rag import RAGMemoryManager
from remote.phone_adb import PhoneADBController
from remote.whatsapp import WhatsAppController


class MockBrain:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.calls: int = 0
        self.last_prompt: str = ""
        self.last_context: str | None = None
        self.is_model_loaded: bool = True
        self.IDENTITY_RESPONSE = "I am E.V., Enhanced Virtual Assistant, created by Bruprog. How can I help you, sir?"

    def generate(self, prompt: str, extra_context: str | None = None, **kwargs) -> str:
        self.calls += 1
        self.last_prompt = prompt
        self.last_context = extra_context
        return f"LLM answered: {prompt}"

    def generate_code(self, task_description: str, language: str = "html") -> str:
        if language == "python":
            return f"# Python script for {task_description}\nprint('Hello sir')\n"
        return f"<!DOCTYPE html><html><body><h1>{task_description}</h1></body></html>"

    def cancel(self) -> None:
        pass


class TestIntentRouterAndUpgrades(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_dir = Path(self.tmp_dir.name)
        self.config = {
            "hardware": {"ram_budget_gb": 4.0},
            "gui": {"default_countdown_seconds": 30},
            "voice": {"stt": {"min_confidence": 0.50}},
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
        self.brain = MockBrain(self.base_dir)
        self.rag = RAGMemoryManager(self.config, self.base_dir)
        self.phone = PhoneADBController(self.config, self.base_dir, event_bus=self.bus, rag=self.rag)
        self.whatsapp = WhatsAppController(self.config, self.base_dir, event_bus=self.bus, rag=self.rag)
        self.scheduler = TimedCommandScheduler(self.config, event_bus=self.bus, phone_adb=self.phone)
        self.web_search = MagicMock(spec=WebSearchTool)
        self.web_search.search_summary.return_value = "Tokyo is the capital of Japan."
        self.web_search.answer_factual_question.return_value = "Luxembourg has the highest GDP per capita in the world."

        self.router = IntentRouter(
            config=self.config,
            brain=self.brain,
            scheduler=self.scheduler,
            phone_adb=self.phone,
            whatsapp=self.whatsapp,
            rag=self.rag,
            web_search=self.web_search,
            event_bus=self.bus,
        )

    def tearDown(self) -> None:
        self.scheduler.shutdown()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_fast_path_never_calls_llm(self) -> None:
        self.rag.add_or_update_contact("Mom", relationship="mother", phone="9876543210")
        commands = [
            "Hey E.V., set volume to 50%",
            "mute audio",
            "system status report",
            "what time is it",
            "unlock my phone",
            "send an sms to Mom saying On my way",
            "send a hello message to Mommy on WhatsApp",
        ]
        for cmd in commands:
            match = self.router.route(cmd)
            self.assertTrue(match.handled_by_fast_path, f"Failed fast-path for: {cmd}")
        self.assertEqual(self.brain.calls, 0)

    def test_spoken_phone_number_saved_contiguously(self) -> None:
        m = self.router.route(
            "remember that my mobile number is nine five eight to one nine eight one six five"
        )
        self.assertTrue(m.handled_by_fast_path)
        self.assertEqual(self.rag.get_my_profile_field("mobile"), "9582198165")

    def test_stop_listening_works_by_voice_even_with_stopped(self) -> None:
        settings_events = []
        self.bus.subscribe(Topics.SETTINGS_CHANGED, lambda e: settings_events.append(e.payload))

        for phrase in ["stop listening", "stopped listening", "Hey Evie go to standby"]:
            m = self.router.route(phrase, confidence=0.40)  # Even below normal min_confidence
            self.assertEqual(m.intent_name, "continuous_listening_mode")
            self.assertIn("Standing by, sir", m.response_text)

        self.assertTrue(any(e.get("key") == "continuousListening" and e.get("value") is False for e in settings_events))

    def test_multistep_whatsapp_mommy_and_missing_number_flow(self) -> None:
        progress_events = []
        self.bus.subscribe(Topics.WORKSPACE_PROGRESS, lambda e: progress_events.append(e.payload))

        # 1. When Mom's number is not saved yet -> asks once and remembers pending send
        m_missing = self.router.route("send a hello message to Mommy on WhatsApp")
        self.assertEqual(m_missing.intent_name, "whatsapp_action")
        self.assertIn("don't have Mom's number saved yet", m_missing.response_text)

        # 2. User provides the number on the next turn -> saves and completes multi-step send
        m_provide = self.router.route("nine eight seven six five four three two one zero")
        self.assertEqual(m_provide.intent_name, "saved_pending_contact_number")
        self.assertIn("Sent 'hello' to Mom on WhatsApp", m_provide.response_text)
        self.assertEqual(self.rag.get_contact("Mom").phone, "9876543210")
        self.assertTrue(any(p.get("status") == "completed" for p in progress_events))

    def test_coding_capability_saves_to_workspace_code(self) -> None:
        m_react = self.router.route("create a React page that displays a live project timer")
        self.assertEqual(m_react.intent_name, "coding_generation")
        self.assertIn("saved it to your code workspace, sir", m_react.response_text)

        saved_files = list((self.base_dir / "data" / "workspace" / "code").glob("*.html"))
        self.assertEqual(len(saved_files), 1)

        m_py = self.router.route("write a Python script that sorts a list of numbers")
        self.assertEqual(m_py.intent_name, "coding_generation")
        py_files = list((self.base_dir / "data" / "workspace" / "code").glob("*.py"))
        self.assertEqual(len(py_files), 1)

    def test_background_web_search_for_factual_question(self) -> None:
        m = self.router.route("what is the richest country in the world")
        self.assertEqual(m.intent_name, "background_web_search")
        self.assertIn("Luxembourg", m.response_text)
        self.web_search.answer_factual_question.assert_called_once()

    def test_short_term_coreference_resolution(self) -> None:
        # 1. "she is not working" resolves "she" -> "E.V." and routes to LLM
        m_ev = self.router.route("she is not working properly")
        self.assertEqual(m_ev.intent_name, "llm_fallback")
        self.assertIn("E.V. is not working properly", self.brain.last_prompt)

        # 2. Talk about Mom, then say "send hello to her on WhatsApp"
        self.rag.add_or_update_contact("Mom", relationship="mother", phone="9876543210")
        self.router.route("who is Mom?")
        m_her = self.router.route("send hello to her on WhatsApp")
        self.assertEqual(m_her.intent_name, "whatsapp_action")
        self.assertIn("Mom", m_her.response_text)

    def test_how_are_you_jarvis_reply_and_identity(self) -> None:
        m_how = self.router.route("how are you?")
        self.assertEqual(m_how.response_text, "All systems running smoothly, sir. How can I help?")
        self.assertNotIn("don't experience feelings", m_how.response_text.lower())

        m_who = self.router.route("who are you?")
        self.assertEqual(m_who.intent_name, "assistant_identity")
        self.assertIn("Bruprog", m_who.response_text)


if __name__ == "__main__":
    unittest.main()
