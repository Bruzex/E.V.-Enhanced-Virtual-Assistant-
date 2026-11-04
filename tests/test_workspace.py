#!/usr/bin/env python3
"""
Unit tests for `automation/workspace.py` and `automation/system.py`:
  - Sequential + parallel workspace launch
  - Aggressive multi-layer `_reverse_single_item` & `_final_cleanup_sweep` Undo
  - Protected OS process whitelist (`explorer.exe`, `svchost.exe`, `python.exe`)
  - Safe File Explorer (`CabinetWClass`) window closing vs app image killing
  - Deep application discovery (`Discord`, `Notion`, `Spotify`, `VS Code`, `Chrome`, arbitrary Start Menu `.lnk` shortcuts)
  - Selective workspace launching (`item_filter`)
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.system import PROTECTED_PROCESSES, SystemController
from automation.workspace import WorkspaceOrchestrator
from core.event_bus import EventBus, Topics


class TestWorkspaceOrchestratorAndAppDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base_dir = Path(self.tmp_dir.name)
        ws_dir = self.base_dir / "data" / "workspaces"
        ws_dir.mkdir(parents=True, exist_ok=True)

        ws_data = {
            "name": "Coding workspace",
            "slug": "coding",
            "items": [
                {"id": "vscode", "name": "VS Code", "detail": "~/Projects/ev", "kind": "app", "target": "code"},
                {"id": "projects", "name": "Projects folder", "detail": "~/Projects", "kind": "folder", "target": "~/Projects", "parallel_group": "g1"},
                {"id": "github", "name": "GitHub", "detail": "github.com", "kind": "url", "target": "https://github.com", "parallel_group": "g1"},
                {"id": "logs", "name": "RAM monitor", "detail": "tail -f logs/rss.log", "kind": "terminal", "target": "tail -f logs/rss.log"},
            ],
        }
        (ws_dir / "coding.json").write_text(json.dumps(ws_data), encoding="utf-8")

        self.config = {
            "automation": {
                "workspaces_dir": "data/workspaces",
                "default_workspace": "coding",
                "sequential_delay_ms": 15,
                "undo_delay_ms": 15,
                "max_parallel_group": 2,
                "execute_os_actions": False,
                "force_close_browsers_on_undo": True,
            }
        }
        self.bus = EventBus()
        self.sys_ctrl = SystemController(self.config, self.base_dir)
        self.orch = WorkspaceOrchestrator(self.config, self.base_dir, self.sys_ctrl, self.bus)

    def tearDown(self) -> None:
        self.orch.cancel()
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_launch_and_lifo_undo(self) -> None:
        progress_events = []
        self.bus.subscribe(Topics.WORKSPACE_PROGRESS, lambda e: progress_events.append(e.payload))

        ok, msg = self.orch.launch_workspace("coding")
        self.assertTrue(ok)
        time.sleep(0.25)

        self.assertTrue(any(e["status"] == "done" and e["completed"] == 4 for e in progress_events))

        ok_undo, _ = self.orch.undo_last_workspace()
        self.assertTrue(ok_undo)
        time.sleep(0.25)

        self.assertTrue(any(e["status"] == "undone" and e["completed"] == 0 for e in progress_events))

    def test_aggressive_undo_calls_all_layers_and_final_sweep(self) -> None:
        """Verify that Undo kills PIDs, closes Explorer folders safely, kills images, and runs final sweep."""
        self.orch.execute_os_actions = True

        killed_images = []
        explorer_closed = []

        with patch.object(self.sys_ctrl, "kill_by_image_names", side_effect=lambda imgs, **kw: killed_images.extend(imgs) or len(imgs)), \
             patch.object(self.sys_ctrl, "close_explorer_folder_windows", side_effect=lambda **kw: explorer_closed.append(kw.get("folder_hint")) or 1), \
             patch.object(self.sys_ctrl, "open_application", return_value=(True, 99991, "Launched")), \
             patch.object(self.sys_ctrl, "open_folder", return_value=(True, None)), \
             patch.object(self.sys_ctrl, "open_url", return_value=(True, None)), \
             patch.object(self.sys_ctrl, "open_terminal_command", return_value=(True, 99992)), \
             patch.object(self.sys_ctrl, "terminate_pid", return_value=True):

            ok, _ = self.orch.launch_workspace("coding")
            self.assertTrue(ok)
            time.sleep(0.25)

            ok_u, _ = self.orch.undo_last_workspace()
            self.assertTrue(ok_u)
            time.sleep(0.25)

        self.assertIn("Code.exe", killed_images)
        self.assertIn("chrome.exe", killed_images)
        self.assertIn("WindowsTerminal.exe", killed_images)
        self.assertGreaterEqual(len(explorer_closed), 2)  # Per-item folder close + final sweep

    def test_protected_processes_never_killed_and_app_discovery(self) -> None:
        """Ensure explorer.exe / svchost.exe / python.exe are protected and Start Menu index resolves apps."""
        self.assertIn("explorer.exe", PROTECTED_PROCESSES)
        self.assertIn("svchost.exe", PROTECTED_PROCESSES)
        self.assertIn("python.exe", PROTECTED_PROCESSES)

        # Attempting to kill protected images must be a no-op returning 0
        self.assertEqual(
            self.sys_ctrl.kill_by_image_names(["explorer.exe", "svchost.exe", "python.exe"]),
            0,
        )

        # Verify Start Menu shortcut index discovery for arbitrary installed apps
        fake_lnk = self.base_dir / "SuperCustomApp.lnk"
        fake_lnk.write_text("shortcut", encoding="utf-8")
        self.sys_ctrl._index_built = True
        self.sys_ctrl._app_index["super custom app"] = str(fake_lnk)

        found = self.sys_ctrl._find_in_app_index("Super Custom")
        self.assertEqual(found, str(fake_lnk))

        # Verify well-known apps resolve image names and open gracefully
        for app_cmd in ("Discord", "Notion", "Spotify", "VS Code", "Chrome"):
            ok, msg = self.sys_ctrl.open_target(app_cmd)
            self.assertTrue(ok, f"Failed to open {app_cmd}: {msg}")

    def test_selective_workspace_launch(self) -> None:
        """Verify selective workspace launching filters items correctly."""
        progress_events = []
        self.bus.subscribe(Topics.WORKSPACE_PROGRESS, lambda e: progress_events.append(e.payload))

        ok, msg = self.orch.launch_workspace("coding", item_filter=["VS Code", "GitHub"])
        self.assertTrue(ok)
        self.assertIn("2 items", msg)
        time.sleep(0.2)

        self.assertTrue(any(e["status"] == "done" and e["completed"] == 2 for e in progress_events))


if __name__ == "__main__":
    unittest.main()
