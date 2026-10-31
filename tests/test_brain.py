"""
Unit tests for core/brain.py
"""

from pathlib import Path
from core.brain import Brain
from core.event_bus import EventBus, Topics


def test_brain_initialization_cpu_only():
    base_dir = Path(__file__).resolve().parent.parent
    config = {
        "llm": {
            "provider": "llama_cpp",
            "model_path": "models/qwen2.5-3b-instruct-q4_k_m.gguf",
            "n_gpu_layers": 10,  # Deliberately non-zero to test enforcement
            "n_ctx": 2048,
            "n_threads": 4,
        }
    }
    bus = EventBus()
    brain = Brain(config=config, base_dir=base_dir, event_bus=bus)

    # Invariant: n_gpu_layers MUST be forced to 0
    assert brain.n_gpu_layers == 0
    assert brain.is_model_loaded is False


def test_brain_offline_fallback_generator():
    base_dir = Path(__file__).resolve().parent.parent
    config = {
        "llm": {
            "model_path": "models/nonexistent.gguf",
            "n_gpu_layers": 0,
        }
    }
    bus = EventBus()
    brain = Brain(config=config, base_dir=base_dir, event_bus=bus)

    # Test deterministic fallback responses
    resp1 = brain.generate("What is the status of the system?")
    assert "local systems are nominal" in resp1.lower()
    assert "0 megabytes" in resp1.lower()

    resp2 = brain.generate("Can you dim the desk lights?")
    assert "dim" in resp2.lower()

    resp3 = brain.generate("Who are you?")
    assert "enhanced virtual assistant" in resp3.lower()


def test_brain_ram_pressure_handling():
    base_dir = Path(__file__).resolve().parent.parent
    config = {"llm": {"n_gpu_layers": 0}}
    bus = EventBus()
    brain = Brain(config=config, base_dir=base_dir, event_bus=bus)

    # Emulate critical RAM pressure
    bus.publish(Topics.RAM_PRESSURE, {"level": "critical", "rss_gb": 3.9})
    assert brain.is_model_loaded is False

