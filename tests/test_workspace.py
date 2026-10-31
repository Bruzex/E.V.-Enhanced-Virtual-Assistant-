#!/usr/bin/env python3
"""
Unit tests for `automation/workspace.py` (`WorkspaceOrchestrator` sequential +
parallel launch, real-time `Topics.WORKSPACE_PROGRESS` events, and LIFO Undo).
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from automation.system import SystemController
from automation.workspace import WorkspaceOrchestrator
from core.event_bus import EventBus, Topics


class TestWorkspaceOrchestrator(unittest.TestCase):
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
                "sequential_delay_ms": 20,
                "undo_delay_ms": 15,
                "max_parallel_group": 2,
                "execute_os_actions": False,
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
        time.sleep(0.3)

        self.assertTrue(any(e["status"] == "done" and e["completed"] == 4 for e in progress_events))

        ok_undo, _ = self.orch.undo_last_workspace()
        self.assertTrue(ok_undo)
        time.sleep(0.25)

        self.assertTrue(any(e["status"] == "undone" and e["completed"] == 0 for e in progress_events))


if __name__ == "__main__":
    unittest.main()