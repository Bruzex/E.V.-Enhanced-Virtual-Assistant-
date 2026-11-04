"""
<<<<<<< HEAD
Unit tests for core/brain.py — including mocked Llama GGUF streaming, cancel()
event interruption, unanchored identity/who-am-i matching, and verification
that the three fake companion lines and bare `time` clock shortcut are gone.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import core.brain as brain_mod
from core.brain import Brain
from core.event_bus import EventBus, Topics

BANNED_CANNED_LINES = (
    "I'm with you, Aditya. Tell me a bit more or let me know what action you'd like me to take.",
    "Got it, Aditya. Would you like me to remember that, open an app, or look something up?",
    "I'm listening, Aditya. Just say the word if you want me to run a command or save that to memory.",
)

=======
Unit tests for core/brain.py
"""

from pathlib import Path
from core.brain import Brain
from core.event_bus import EventBus, Topics

>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

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

<<<<<<< HEAD
=======
    # Invariant: n_gpu_layers MUST be forced to 0
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
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

<<<<<<< HEAD
=======
    # Test deterministic fallback responses
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
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

<<<<<<< HEAD
    bus.publish(Topics.RAM_PRESSURE, {"level": "critical", "rss_gb": 3.9})
    assert brain.is_model_loaded is False


def test_mock_llama_streaming_and_first_sentence_dispatch(tmp_path: Path):
    gguf_file = tmp_path / "qwen2.5-3b-instruct-q4_k_m.gguf"
    gguf_file.write_bytes(b"\x00" * (12 * 1024 * 1024))  # 12 MB dummy GGUF

    def fake_stream(*args, **kwargs):
        assert kwargs.get("stream") is True
        assert kwargs.get("max_tokens") == 80
        for tok in ["All ", "systems ", "are ", "online. ", "Ready ", "to ", "assist."]:
            yield {"choices": [{"delta": {"content": tok}}]}

    mock_llama_instance = MagicMock()
    mock_llama_instance.create_chat_completion.side_effect = fake_stream

    with patch.object(brain_mod, "LLAMA_CPP_AVAILABLE", True), patch.object(
        brain_mod, "Llama", return_value=mock_llama_instance
    ) as mock_llama_cls:
        config = {
            "llm": {
                "model_path": str(gguf_file),
                "max_tokens": 80,
                "n_ctx": 2048,
                "n_gpu_layers": 0,
            }
        }
        brain = Brain(config=config, base_dir=tmp_path, event_bus=EventBus())
        assert brain.is_model_loaded is True
        mock_llama_cls.assert_called_once()
        assert mock_llama_cls.call_args.kwargs["n_gpu_layers"] == 0

        first_sentences = []
        reply = brain.generate("How are you?", on_first_sentence=lambda s: first_sentences.append(s))
        assert reply == "All systems are online. Ready to assist."
        assert first_sentences == ["All systems are online."]
        for canned in BANNED_CANNED_LINES:
            assert reply != canned


def test_cancel_stops_fake_stream(tmp_path: Path):
    gguf_file = tmp_path / "qwen2.5-3b-instruct-q4_k_m.gguf"
    gguf_file.write_bytes(b"\x00" * (12 * 1024 * 1024))

    brain_ref = []

    def fake_long_stream(*args, **kwargs):
        for idx, tok in enumerate(["First ", "token ", "third ", "fourth ", "fifth "]):
            if idx == 2 and brain_ref:
                brain_ref[0].cancel()
            yield {"choices": [{"delta": {"content": tok}}]}

    mock_llama_instance = MagicMock()
    mock_llama_instance.create_chat_completion.side_effect = fake_long_stream

    with patch.object(brain_mod, "LLAMA_CPP_AVAILABLE", True), patch.object(
        brain_mod, "Llama", return_value=mock_llama_instance
    ):
        config = {"llm": {"model_path": str(gguf_file), "max_tokens": 80}}
        brain = Brain(config=config, base_dir=tmp_path, event_bus=EventBus())
        brain_ref.append(brain)

        streamed = list(brain.stream_generate("Explain quantum mechanics"))
        # Stream must stop immediately at index 2 when cancel() is set
        assert streamed == ["First ", "token "]


def test_identity_and_no_clock_on_volume_time_phrase(tmp_path: Path):
    config = {"llm": {"model_path": str(tmp_path / "missing.gguf")}}
    brain = Brain(config=config, base_dir=tmp_path, event_bus=EventBus())

    for q in ["how are you", "Evie who the fuck are you", "give me a basic introduction of yours"]:
        ans = brain.generate(q)
        for canned in BANNED_CANNED_LINES:
            assert ans != canned

    assert "Bruprog" in brain.generate("Evie who the fuck are you")
    assert "Bruprog" in brain.generate("give me a basic introduction of yours")

    # "each time you increase the volume" must NEVER return a clock time
    vol_remark = brain.generate("each time you increase the volume ten percent")
    assert "currently" not in vol_remark.lower()
    assert "pm" not in vol_remark.lower() and "am" not in vol_remark.lower()


import tempfile
import unittest


class TestBrainUnittest(unittest.TestCase):
    def test_all_brain_functions(self) -> None:
        test_brain_initialization_cpu_only()
        test_brain_offline_fallback_generator()
        test_brain_ram_pressure_handling()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            test_mock_llama_streaming_and_first_sentence_dispatch(p)
            test_cancel_stops_fake_stream(p)
            test_identity_and_no_clock_on_volume_time_phrase(p)
=======
    # Emulate critical RAM pressure
    bus.publish(Topics.RAM_PRESSURE, {"level": "critical", "rss_gb": 3.9})
    assert brain.is_model_loaded is False

>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
