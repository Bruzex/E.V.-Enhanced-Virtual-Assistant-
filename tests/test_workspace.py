"""
Unit Tests for E.V. Workspace Orchestrator
==========================================
Validates JSON loading, execution structure, and LIFO undo tracking.
"""

from pathlib import Path
from automation.workspace import WorkspaceOrchestrator


def test_load_coding_workspace() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    orchestrator = WorkspaceOrchestrator(base_dir=base_dir)

    ws = orchestrator.load_workspace("coding")
    assert ws is not None
    assert ws["name"] == "Coding workspace"
    assert len(ws["items"]) >= 10

    kinds = {item["kind"] for item in ws["items"]}
    assert "app" in kinds
    assert "folder" in kinds
    assert "url" in kinds
    assert "terminal" in kinds


def test_workspace_orchestration_and_undo_tracking() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    config = {
        "automation": {
            "default_workspace": "coding",
            "execute_os_actions": False,
            "sequential_delay_ms": 10,
            "undo_delay_ms": 10,
        }
    }
    orchestrator = WorkspaceOrchestrator(config=config, base_dir=base_dir)

    ok, msg = orchestrator.launch_workspace("coding")
    assert ok is True
    assert "launching" in msg.lower() or "ready" in msg.lower() or "launched" in msg.lower()

    # Verify LIFO Undo stack can reverse the workspace
    undo_ok, undo_msg = orchestrator.undo_last_workspace()
    assert undo_ok is True
    assert "closed" in undo_msg.lower() or "restored" in undo_msg.lower() or "reversing" in undo_msg.lower()
