"""
Unit tests for `core/brain.py` — including mocked Llama GGUF streaming, `cancel()`
interruption, `"sir"` persona system prompt, model-name sanitization, and `generate_code()`.
"""

import tempfile
import unittest
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


def test_brain_initialization_cpu_only():
    base_dir = Path(__file__).resolve().parent.parent
    config = {
        "llm": {
            "provider": "llama_cpp",
            "model_path": "models/qwen2.5-3b-instruct-q4_k_m.gguf",
            "n_gpu_layers": 10,  # Deliberately non-zero to test strict CPU-only enforcement
            "n_ctx": 1536,
            "n_threads": 6,
        }
    }
    bus = EventBus()
    brain = Brain(config=config, base_dir=base_dir, event_bus=bus)

    assert brain.n_gpu_layers == 0
    assert "sir" in brain.system_prompt.lower()
    assert "bruprog" in brain.system_prompt.lower()


def test_brain_missing_model_message_and_sanitization():
    base_dir = Path(__file__).resolve().parent.parent
    config = {
        "llm": {
            "model_path": "models/nonexistent.gguf",
            "n_gpu_layers": 0,
        }
    }
    bus = EventBus()
    brain = Brain(config=config, base_dir=base_dir, event_bus=bus)

    resp = brain.generate("Hello there")
    assert "sir" in resp.lower()
    assert "qwen" not in resp.lower()
    assert "gguf" not in resp.lower()

    sanitized = Brain.sanitize_response("I am running Qwen2.5-3B-Instruct GGUF with faster-whisper and vosk.")
    assert "qwen" not in sanitized.lower()
    assert "gguf" not in sanitized.lower()
    assert "whisper" not in sanitized.lower()
    assert "vosk" not in sanitized.lower()


def test_brain_ram_pressure_handling(tmp_path: Path):
    gguf_file = tmp_path / "qwen2.5-3b-instruct-q4_k_m.gguf"
    gguf_file.write_bytes(b"\x00" * (2 * 1024 * 1024))

    mock_llama_instance = MagicMock()
    with patch.object(brain_mod, "LLAMA_CPP_AVAILABLE", True), patch.object(
        brain_mod, "Llama", return_value=mock_llama_instance
    ):
        bus = EventBus()
        # 1. When unload_on_critical is False (default), critical RAM pressure keeps model resident
        config_keep = {
            "llm": {"model_path": str(gguf_file), "n_gpu_layers": 0},
            "ram_guard": {"unload_on_critical": False},
        }
        brain_keep = Brain(config=config_keep, base_dir=tmp_path, event_bus=bus)
        assert brain_keep.is_model_loaded is True
        bus.publish(Topics.RAM_PRESSURE, {"level": "critical", "rss_gb": 3.9})
        assert brain_keep.is_model_loaded is True

        # 2. When unload_on_critical is True, critical RAM pressure unloads model
        config_unload = {
            "llm": {"model_path": str(gguf_file), "n_gpu_layers": 0},
            "ram_guard": {"unload_on_critical": True},
        }
        brain_unload = Brain(config=config_unload, base_dir=tmp_path, event_bus=bus)
        bus.publish(Topics.RAM_PRESSURE, {"level": "critical", "rss_gb": 3.9})
        assert brain_unload.is_model_loaded is False


def test_mock_llama_streaming_and_first_sentence_dispatch(tmp_path: Path):
    gguf_file = tmp_path / "qwen2.5-3b-instruct-q4_k_m.gguf"
    gguf_file.write_bytes(b"\x00" * (12 * 1024 * 1024))

    def fake_stream(*args, **kwargs):
        assert kwargs.get("stream") is True
        assert kwargs.get("max_tokens") == 80
        for tok in ["All ", "systems ", "are ", "online, ", "sir. ", "Ready ", "to ", "assist."]:
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
                "n_ctx": 1536,
                "n_gpu_layers": 0,
            }
        }
        brain = Brain(config=config, base_dir=tmp_path, event_bus=EventBus())
        assert brain.is_model_loaded is True
        mock_llama_cls.assert_called_once()
        assert mock_llama_cls.call_args.kwargs["n_gpu_layers"] == 0

        first_sentences = []
        assert brain.generate("How are you?") == "All systems running smoothly, sir. How can I help?"
        reply = brain.generate("Tell me a quick thought about engineering.", on_first_sentence=lambda s: first_sentences.append(s))
        assert reply == "All systems are online, sir. Ready to assist."
        assert first_sentences == ["All systems are online, sir."]
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
        assert streamed == ["First ", "token "]


def test_generate_code_html_react_python(tmp_path: Path):
    config = {"llm": {"model_path": str(tmp_path / "missing.gguf")}}
    brain = Brain(config=config, base_dir=tmp_path, event_bus=EventBus())

    html_code = brain.generate_code("portfolio landing page", language="html")
    assert "<!DOCTYPE html>" in html_code

    react_code = brain.generate_code("interactive counter app", language="react")
    assert "React.useState" in react_code

    py_code = brain.generate_code("calculate fibonacci numbers", language="python")
    assert "def main()" in py_code


class TestBrainUnittest(unittest.TestCase):
    def test_all_brain_functions(self) -> None:
        test_brain_initialization_cpu_only()
        test_brain_missing_model_message_and_sanitization()
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            test_brain_ram_pressure_handling(p)
            test_mock_llama_streaming_and_first_sentence_dispatch(p)
            test_cancel_stops_fake_stream(p)
            test_generate_code_html_react_python(p)
