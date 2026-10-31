"""
Unit Tests for E.V. Phone ADB Controller
========================================
Validates phone control wrappers, contact resolution, and graceful fallback mode.
"""

from remote.phone_adb import PhoneADBController


def test_adb_disabled_fallback() -> None:
    config = {
        "remote": {
            "phone_adb": {
                "enabled": False,
                "device_ip": "127.0.0.1",
                "port": 5555,
            }
        }
    }
    controller = PhoneADBController(config)
    assert controller.connect() is False
    ok, msg = controller.make_call("+15550142290")
    assert ok is True
    assert "ready" in msg.lower() or "call" in msg.lower()

    ok, msg = controller.unlock()
    assert ok is True


def test_adb_contact_resolution():
    controller = PhoneADBController(config={})
    info = controller.resolve_contact("mom")
    assert info["name"] == "Mom"
    assert "number" in info
