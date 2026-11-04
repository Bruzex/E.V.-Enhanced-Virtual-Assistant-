"""
Unit tests for core/scheduler.py (TimedCommandScheduler).
"""

from core.event_bus import EventBus
from core.scheduler import TimedCommandScheduler


def test_scheduler_quick_timer():
    bus = EventBus()
    scheduler = TimedCommandScheduler(config={}, event_bus=bus)

    task = scheduler.schedule_quick_timer(seconds=5, label="Test timer")
    assert task is not None
    assert task.remaining_seconds == 5
    assert scheduler.is_busy is True

    # Cancel timer
    scheduler.cancel(speak_feedback=False)
    assert scheduler.is_busy is False


def test_scheduler_run_now():
    bus = EventBus()
    completed_actions = []

    scheduler = TimedCommandScheduler(config={}, event_bus=bus)
    scheduler.schedule(
        seconds=100,
        label="Test run now",
        on_complete=lambda: completed_actions.append(True),
    )
    assert scheduler.is_busy is True

    scheduler.run_now()
    if scheduler._worker_thread is not None:
        scheduler._worker_thread.join(timeout=1.0)

    # Task should finish immediately
    assert scheduler.is_busy is False
    assert len(completed_actions) == 1
