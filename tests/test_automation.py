"""
Unit tests for automation subsystem (SystemController, MediaController, WebSearchTool).
"""

from pathlib import Path
from automation.system import SystemController
from automation.media import MediaController
from automation.web_search import WebSearchTool


def test_system_controller_snapshot():
    base_dir = Path(__file__).resolve().parent.parent
    sys_ctrl = SystemController(config={}, base_dir=base_dir)

    snapshot = sys_ctrl.get_system_snapshot()
    assert "cpu_percent" in snapshot
    assert "process_rss_gb" in snapshot
    assert "battery_percent" in snapshot
    assert snapshot["vram_mb"] == 0  # Strict zero-VRAM mandate
    assert snapshot["ram_budget_gb"] == 4.0


def test_system_controller_volume_simulation():
    base_dir = Path(__file__).resolve().parent.parent
    sys_ctrl = SystemController(config={}, base_dir=base_dir)

    new_vol = sys_ctrl.adjust_volume(10)
    assert isinstance(new_vol, int)

    ok = sys_ctrl.set_mute(True)
    assert isinstance(ok, bool)


def test_media_controller_initialization():
    base_dir = Path(__file__).resolve().parent.parent
    media_ctrl = MediaController(config={}, base_dir=base_dir)

    # Verify screenshots directory created
    assert media_ctrl.screenshots_dir.exists()


def test_web_search_offline_graceful_degradation():
    web_search = WebSearchTool(config={})
    summary = web_search.search_summary("who invented python")
    assert isinstance(summary, str)
    assert len(summary) > 0
