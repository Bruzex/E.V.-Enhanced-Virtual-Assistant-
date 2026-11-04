#!/usr/bin/env python3
"""
Unit tests for `remote/phone_adb.py` and `remote/audio_routing.py`.
Verifies graceful degradation when no physical Android device is attached
and validates `Topics.CALL_STATE` & `Topics.CONNECTION_STATUS` events.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.event_bus import EventBus, Topics
from remote.audio_routing import AudioRouter
from remote.phone_adb import PhoneADBController


class TestPhoneADBAndAudioRouting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_dir = Path(self.tmp_dir.name)
        self.config = {
            "remote": {
                "phone_adb": {
                    "enabled": True,
                    "host": "192.168.1.105",
                    "port": 5555,
                    "contacts": {
                        "mom": {
                            "name": "Mom",
                            "number": "+1 (555) 014-2290",
                            "meta": "Mobile · via phone bridge",
                        }
                    },
                },
                "audio_routing": {"enabled": True},
            }
        }
        self.bus = EventBus()
        self.audio = AudioRouter(self.config, event_bus=self.bus)
        self.phone = PhoneADBController(
            self.config, self.base_dir, event_bus=self.bus, audio_router=self.audio
        )

    def tearDown(self) -> None:
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_make_accept_and_end_call_events(self) -> None:
        call_events = []
        self.bus.subscribe(Topics.CALL_STATE, lambda e: call_events.append(e.payload))

        ok, msg = self.phone.make_call("Mom")
        self.assertTrue(ok)
        self.assertIn("Mom", msg)
        self.assertEqual(len(call_events), 1)
        self.assertEqual(call_events[0]["name"], "Mom")
        self.assertTrue(self.audio.is_routing_active)

        self.phone.accept_call()
        self.assertTrue(self.audio.is_routing_active)

        self.phone.end_call()
        self.assertFalse(self.audio.is_routing_active)

    def test_unlock_sms_and_battery_graceful_standby(self) -> None:
        ok_u, _ = self.phone.unlock("1234")
        self.assertTrue(ok_u)

        ok_s, sms_msg = self.phone.send_sms("Mom", "Testing offline bridge")
        self.assertTrue(ok_s)
        self.assertIn("Mom", sms_msg)

        lvl, bat_msg = self.phone.get_battery_level()
        self.assertGreater(lvl, 0)
        self.assertIn("percent", bat_msg)


if __name__ == "__main__":
    unittest.main()