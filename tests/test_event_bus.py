"""
Unit tests for core/event_bus.py
"""

from core.event_bus import Event, EventBus, Topics, get_event_bus


def test_event_bus_publish_subscribe():
    bus = EventBus()
    received = []

    def on_orb_state(event: Event):
        received.append(event)

    unsubscribe = bus.subscribe(Topics.ORB_STATE, on_orb_state)

    ev = bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.5}, source="test")
    assert len(received) == 1
    assert received[0].payload["state"] == "listening"
    assert received[0].payload["progress"] == 0.5
    assert received[0].source == "test"

    unsubscribe()
    bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="test")
    assert len(received) == 1  # Unsubscribed, so count shouldn't change


def test_event_bus_wildcard_listener():
    bus = EventBus()
    events = []

    bus.subscribe("*", lambda ev: events.append(ev.topic))

    bus.publish(Topics.CAPTION, {"text": "hello"})
    bus.publish(Topics.VOICE_WAKEWORD_DETECTED, {"phrase": "hey ev"})

    assert events == [Topics.CAPTION, Topics.VOICE_WAKEWORD_DETECTED]


def test_event_bus_exception_isolation():
    bus = EventBus()
    success_called = []

    def faulty_cb(event: Event):
        raise RuntimeError("Simulated failure in subscriber")

    def ok_cb(event: Event):
        success_called.append(True)

    bus.subscribe(Topics.RAM_PRESSURE, faulty_cb)
    bus.subscribe(Topics.RAM_PRESSURE, ok_cb)

    # Publishing should not crash despite the exception in faulty_cb
    bus.publish(Topics.RAM_PRESSURE, {"level": "critical", "rss_gb": 3.9})
    assert len(success_called) == 1


def test_global_singleton_bus():
    bus1 = get_event_bus()
    bus2 = get_event_bus()
    assert bus1 is bus2

