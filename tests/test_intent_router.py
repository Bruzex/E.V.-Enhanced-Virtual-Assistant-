"""
Unit Tests for E.V. Intent Router (Phase 2 Fast-Path Registry & Fallback)
========================================================================
Validates fast-path parsing for chained workflows, timed events, workspaces,
media, volume, search, system telemetry, and chat fallback.
"""

from core.intent_router import IntentRouter


def test_chained_workspace_and_call() -> None:
    router = IntentRouter()
    cmd = "Hey E.V., open my coding workspace and prepare a call to Mom in 30 seconds."
    parsed = router.route(cmd)

    assert parsed.intent_type == "chained_workflow"
    assert parsed.action == "workspace_and_timed_call"
    assert parsed.params["workspace"] == "coding"
    assert parsed.params["contact"] == "Mom"
    assert parsed.params["delay_seconds"] == 30
    assert parsed.bypass_llm is True
    assert parsed.handled_by_fast_path is True


def test_timed_command_dim_lights() -> None:
    router = IntentRouter()
    cmd = "dim desk lights to 30% in 10 seconds"
    parsed = router.route(cmd)

    assert parsed.intent_type == "timed_command"
    assert parsed.params["delay_seconds"] == 10
    assert "dim" in parsed.params["command"].lower()
    assert parsed.bypass_llm is True


def test_workspace_undo() -> None:
    router = IntentRouter()
    parsed = router.route("undo workspace")

    assert parsed.intent_type == "workspace"
    assert parsed.action == "undo"
    assert parsed.bypass_llm is True


def test_volume_control_intent() -> None:
    router = IntentRouter()
    p1 = router.route("mute audio")
    assert p1.intent_name == "volume_control"
    assert p1.handled_by_fast_path is True

    p2 = router.route("set volume to 75%")
    assert p2.intent_name == "volume_control"
    assert p2.slots.get("level") == "75"


def test_media_control_intent() -> None:
    router = IntentRouter()
    p1 = router.route("pause music")
    assert p1.intent_name == "media_control"
    assert p1.handled_by_fast_path is True

    p2 = router.route("next track")
    assert p2.intent_name == "media_control"
    assert p2.handled_by_fast_path is True


def test_system_status_intent() -> None:
    router = IntentRouter()
    parsed = router.route("system status")
    assert parsed.intent_name == "system_status"
    assert parsed.handled_by_fast_path is True


def test_web_search_intent() -> None:
    router = IntentRouter()
    parsed = router.route("search python documentation")
    assert parsed.intent_name == "web_search"
    assert parsed.handled_by_fast_path is True


def test_time_date_intent() -> None:
    router = IntentRouter()
    parsed = router.route("what time is it")
    assert parsed.intent_name == "time_date"
    assert parsed.handled_by_fast_path is True


def test_chat_fallback() -> None:
    router = IntentRouter()
    parsed = router.route("Tell me a story about quantum computing.")

    assert parsed.intent_type == "chat"
    assert parsed.bypass_llm is False
    assert parsed.handled_by_fast_path is False
