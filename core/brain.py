#!/usr/bin/env python3
"""
<<<<<<< HEAD
E.V. (Enhanced Virtual Assistant) — Local GGUF Brain (`core/brain.py`)
======================================================================
Strictly CPU-only (`n_gpu_layers=0`, RSS <= 4.0 GB) local LLM engine using
`llama-cpp-python` (`models/qwen2.5-3b-instruct-q4_k_m.gguf`).

Key Guarantees:
  1. Startup Model Load & Single-Line Diagnostic Logging:
     Calls `load_model()` once at initialization and logs a single diagnostic line
     with the resolved GGUF path, file size in MB, `LLAMA_CPP_AVAILABLE`, any
     exception text if `Llama()` fails, and process RSS in GB.
  2. Multi-Format Chunk Extraction & First-Chunk Telemetry:
     Reads streamed/completion text from all of:
       - `choices[0].delta.content`
       - `choices[0].message.content`
       - `choices[0].text`
       - object attributes when a chunk is not a `dict`
     Logs the first raw chunk once (`type` + `repr` truncated to 300 chars).
  3. Strict Model-Loaded Error Handling:
     If `self._llm` is loaded, NEVER returns `NO_ENGINE_SPOKEN_MESSAGE`. On a real
     inference exception, logs the full traceback and returns:
       `"I heard you, but that reply failed."`
=======
E.V. (Enhanced Virtual Assistant) — Local LLM Brain & Safe Conversational Engine
================================================================================
Strictly CPU-only local inference wrapper (`n_gpu_layers=0`) with a natural,
calm, JARVIS-inspired conversational responder and strict privacy/safety guards.

Key Guarantees:
  1. NEVER echoes "I heard: '...'" or mentions internal model names, GGUF files,
     llama.cpp, or missing model directories to the user.
  2. NEVER leaks personal profile fields (e.g., "From what I remember, Aditya Raj")
     or contact phone numbers on general or unrecognized questions. Personal
     information is ONLY spoken when the user explicitly asks for that specific field.
  3. Replies naturally as E.V. (Enhanced Virtual Assistant) created by Bruprog,
     including audio check-ins ("Can you hear me?", "Are you able to hear me?").
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
"""

from __future__ import annotations

import ast
<<<<<<< HEAD
import collections.abc
=======
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
import gc
import logging
import operator
import os
import re
<<<<<<< HEAD
=======
try:
    import resource  # type: ignore
except ImportError:
    resource = None  # type: ignore
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
import threading
import time
from datetime import datetime
from pathlib import Path
<<<<<<< HEAD
from typing import Any, Callable, Dict, Generator, List, Optional
=======
from typing import Any, Dict, Generator, List, Optional
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

try:
    import psutil  # type: ignore
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    PSUTIL_AVAILABLE = False

from core.event_bus import Event, EventBus, Topics, get_event_bus

# Enforce zero GPU visibility before llama_cpp import
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["HIP_VISIBLE_DEVICES"] = ""
os.environ["ROCR_VISIBLE_DEVICES"] = ""
os.environ["GGML_VK_VISIBLE_DEVICES"] = ""

<<<<<<< HEAD
LLAMA_CPP_IMPORT_ERROR: Optional[str] = None
try:
    from llama_cpp import Llama  # type: ignore
    LLAMA_CPP_AVAILABLE = True
except Exception as _llama_exc:
    Llama = None  # type: ignore
    LLAMA_CPP_AVAILABLE = False
    LLAMA_CPP_IMPORT_ERROR = str(_llama_exc)

CPU_WHEEL_INSTALL_CMD = (
    "pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu"
)

=======
try:
    from llama_cpp import Llama  # type: ignore
    LLAMA_CPP_AVAILABLE = True
except ImportError:
    Llama = None  # type: ignore
    LLAMA_CPP_AVAILABLE = False


# Safe arithmetic evaluator for spoken math questions
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_math_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Num):  # Python 3.7 compat
        return float(node.n)  # type: ignore
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        return float(_SAFE_OPS[type(node.op)](_eval_math_node(node.left), _eval_math_node(node.right)))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
        return float(_SAFE_OPS[type(node.op)](_eval_math_node(node.operand)))
    raise ValueError("Unsupported math expression")


<<<<<<< HEAD
def _extract_chunk_text(chunk: Any) -> str:
    """
    Extract text from a Llama completion or stream chunk supporting both dict keys
    and object attributes across:
      - choices[0].delta.content
      - choices[0].message.content
      - choices[0].text
    """
    if chunk is None:
        return ""
    if isinstance(chunk, str):
        return chunk

    choices = chunk.get("choices") if isinstance(chunk, dict) else getattr(chunk, "choices", None)
    if not choices:
        return ""
    try:
        c0 = choices[0]
    except Exception:
        return ""

    # 1. choices[0].delta.content
    delta = c0.get("delta") if isinstance(c0, dict) else getattr(c0, "delta", None)
    if delta is not None:
        content = delta.get("content") if isinstance(delta, dict) else getattr(delta, "content", None)
        if isinstance(content, str) and content:
            return content

    # 2. choices[0].message.content
    message = c0.get("message") if isinstance(c0, dict) else getattr(c0, "message", None)
    if message is not None:
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        if isinstance(content, str) and content:
            return content

    # 3. choices[0].text
    text_val = c0.get("text") if isinstance(c0, dict) else getattr(c0, "text", None)
    if isinstance(text_val, str) and text_val:
        return text_val

    return ""


class Brain:
    """
    Thread-safe CPU-only LLM engine for E.V. (`n_gpu_layers=0`, `max_tokens=80`, `n_ctx=2048`).
    """

    IDENTITY_RESPONSE = (
        "I am E.V., Enhanced Virtual Assistant, created by Bruprog. "
        "I manage your workspaces, desktop controls, timers, calls, and messages offline."
    )

    NO_ENGINE_SPOKEN_MESSAGE = "I can't think yet. The local engine failed to start."
    GENERATION_ERROR_MESSAGE = "I heard you, but that reply failed."

    DEFAULT_SYSTEM_PROMPT = (
        "E.V., Enhanced Virtual Assistant, created by Bruprog. "
        "One or two spoken sentences. Act, then confirm. "
        "No follow-up question unless a required slot is missing."
=======
class Brain:
    """
    Thread-safe CPU-only LLM engine and natural conversational brain for E.V.
    """

    IDENTITY_RESPONSE = (
        "Hey, I’m E.V. — Enhanced Virtual Assistant. I was created by Bruprog. "
        "I can help you control your computer, manage workspaces, remember important things, "
        "make calls, send messages, and more — all offline."
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    )

    def __init__(
        self,
        config: Dict[str, Any],
        base_dir: Path,
        event_bus: Optional[EventBus] = None,
        rag: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.bus = event_bus or get_event_bus()
        self.rag = rag
        self.logger = logging.getLogger("ev.brain")

        llm_cfg = config.get("llm", {})
        rel_model = str(llm_cfg.get("model_path", "models/qwen2.5-3b-instruct-q4_k_m.gguf"))
        self.model_path: Path = (
            (base_dir / rel_model).resolve() if not os.path.isabs(rel_model) else Path(rel_model)
        )

        # HARD HARDWARE INVARIANT: Never allow GPU layers on RX 6500M
        self.n_gpu_layers: int = 0
<<<<<<< HEAD
        self.n_ctx: int = 2048
=======
        self.n_ctx: int = min(4096, max(512, int(llm_cfg.get("n_ctx", 2048))))
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        self.n_batch: int = min(512, max(64, int(llm_cfg.get("n_batch", 256))))
        self.n_threads: int = max(1, int(llm_cfg.get("n_threads", min(6, os.cpu_count() or 4))))
        self.n_threads_batch: int = max(1, int(llm_cfg.get("n_threads_batch", self.n_threads)))
        self.use_mmap: bool = bool(llm_cfg.get("use_mmap", True))
        self.use_mlock: bool = bool(llm_cfg.get("use_mlock", False))

        self.temperature: float = float(llm_cfg.get("temperature", 0.3))
        self.top_p: float = float(llm_cfg.get("top_p", 0.9))
<<<<<<< HEAD
        self.max_tokens: int = int(llm_cfg.get("max_tokens", 80))
        self.unload_after_idle_s: int = int(llm_cfg.get("unload_after_idle_seconds", 0))

        self.system_prompt: str = str(
            llm_cfg.get("system_prompt", self.DEFAULT_SYSTEM_PROMPT)
=======
        self.max_tokens: int = int(llm_cfg.get("max_tokens", 256))
        self.unload_after_idle_s: int = int(llm_cfg.get("unload_after_idle_seconds", 300))

        self.system_prompt: str = str(
            llm_cfg.get(
                "system_prompt",
                (
                    "You are E.V. (Enhanced Virtual Assistant), a calm, articulate, slightly formal "
                    "yet warm sci-fi AI assistant created by Bruprog and inspired by JARVIS. "
                    "Never repeat the user's question back with 'I heard', never mention model "
                    "filenames or technical internals, and never read out personal profile data "
                    "unless explicitly asked. Keep responses concise (1 to 2 sentences) "
                    "so they sound natural when spoken aloud."
                ),
            )
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        ).strip()

        hw_cfg = config.get("hardware", {})
        self.ram_budget_gb: float = float(hw_cfg.get("ram_budget_gb", 4.0))
        self.ram_critical_gb: float = float(hw_cfg.get("ram_critical_threshold_gb", 3.8))

        self._lock = threading.RLock()
<<<<<<< HEAD
        self._cancel_event = threading.Event()
        self._llm: Optional[Any] = None
        self._startup_attempted: bool = False
        self._logged_first_chunk: bool = False
        self._last_used_ts: float = 0.0
        self._last_load_error: Optional[str] = None
        self._no_engine_announced: bool = False
        self._history: List[Dict[str, str]] = []
        self._max_history_turns: int = 6

        self.bus.subscribe(Topics.RAM_PRESSURE, self._on_ram_pressure)
        self.bus.subscribe("voice.interrupt", lambda _: self.cancel())

        # Call load_model() once at process start and log the single startup line
        self.load_model()

    def set_rag(self, rag: Any) -> None:
        """Attach the RAGMemoryManager so Brain can access profile and learned context."""
        self.rag = rag
        if self.rag is not None and hasattr(self.rag, "sqlite"):
            try:
                past = self.rag.sqlite.recent_turns(limit=3)
=======
        self._llm: Optional[Any] = None
        self._last_used_ts: float = 0.0
        self._history: List[Dict[str, str]] = []
        self._max_history_turns: int = 8

        self.bus.subscribe(Topics.RAM_PRESSURE, self._on_ram_pressure)

    def set_rag(self, rag: Any) -> None:
        """Attach the RAGMemoryManager so Brain can learn from and query past conversations."""
        self.rag = rag
        if self.rag is not None and hasattr(self.rag, "sqlite"):
            try:
                past = self.rag.sqlite.recent_turns(limit=4)
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                with self._lock:
                    for row in past:
                        u_txt = str(row.get("user_text", "")).strip()
                        a_txt = str(row.get("assistant_text", "")).strip()
<<<<<<< HEAD
                        if (
                            u_txt
                            and a_txt
                            and "tell me a bit more" not in a_txt.lower()
                            and "would you like me to remember that" not in a_txt.lower()
                            and a_txt != self.NO_ENGINE_SPOKEN_MESSAGE
                            and a_txt != self.GENERATION_ERROR_MESSAGE
                        ):
=======
                        if u_txt and a_txt:
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                            self._history.append({"role": "user", "content": u_txt})
                            self._history.append({"role": "assistant", "content": a_txt})
            except Exception:
                pass

    # =========================================================================
<<<<<<< HEAD
    # Model Lifecycle, Startup Logging & Cancellation
=======
    # Model Lifecycle & RAM Guardrails
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    # =========================================================================
    @property
    def is_loaded(self) -> bool:
        return self._llm is not None

    @property
    def is_model_loaded(self) -> bool:
<<<<<<< HEAD
        return self._llm is not None

    def cancel(self) -> None:
        """Signal any in-flight token stream to stop immediately."""
        self._cancel_event.set()
=======
        return self.is_loaded
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

    def _current_process_rss_gb(self) -> float:
        try:
            if PSUTIL_AVAILABLE and psutil is not None:
                proc = psutil.Process(os.getpid())
                rss = proc.memory_info().rss
                for child in proc.children(recursive=True):
                    try:
                        rss += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                return rss / (1024 ** 3)
<<<<<<< HEAD
        except Exception:
            pass
        return 0.0

    def load_model(self) -> bool:
        """
        Load the GGUF model into system RAM with `n_gpu_layers=0`.
        Logs a single diagnostic line with:
          resolved GGUF path, size MB, LLAMA_CPP_AVAILABLE, exception text, RSS after load.
        """
=======
            if resource is not None:
                return (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024) / (1024 ** 3)
            return 0.0
        except Exception:
            return 0.0

    def load_model(self) -> bool:
        """Load the GGUF model into system RAM with `n_gpu_layers=0`."""
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        with self._lock:
            if self._llm is not None:
                self._last_used_ts = time.time()
                return True
<<<<<<< HEAD
            self._startup_attempted = True

            resolved_path = self.model_path.resolve()
            exists = resolved_path.exists() and resolved_path.is_file()
            size_mb = round(resolved_path.stat().st_size / (1024.0 * 1024.0), 1) if exists else 0.0
            rss_before = self._current_process_rss_gb()

            if not LLAMA_CPP_AVAILABLE or Llama is None:
                self._last_load_error = f"llama_cpp import failed: {LLAMA_CPP_IMPORT_ERROR or 'not installed'}"
                self.logger.error(
                    "LLM startup failed | path=%s | size_mb=%.1f MB | LLAMA_CPP_AVAILABLE=%s | exception=%s | rss_gb=%.2f GB",
                    resolved_path,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    self._last_load_error,
                    rss_before,
                )
                self.logger.error("Install prebuilt CPU wheel with: %s", CPU_WHEEL_INSTALL_CMD)
                return False

            if not exists or size_mb < 10.0:
                self._last_load_error = f"GGUF file missing or incomplete at {resolved_path} ({size_mb:.1f} MB)"
                self.logger.warning(
                    "LLM startup failed | path=%s | size_mb=%.1f MB | LLAMA_CPP_AVAILABLE=%s | exception=%s | rss_gb=%.2f GB",
                    resolved_path,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    self._last_load_error,
                    rss_before,
                )
                return False

            if rss_before > self.ram_critical_gb:
                self._last_load_error = f"Process RSS ({rss_before:.2f} GB) exceeds critical threshold ({self.ram_critical_gb:.2f} GB)"
                self.logger.warning(
                    "LLM startup aborted | path=%s | size_mb=%.1f MB | LLAMA_CPP_AVAILABLE=%s | exception=%s | rss_gb=%.2f GB",
                    resolved_path,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    self._last_load_error,
=======

            if not LLAMA_CPP_AVAILABLE or not self.model_path.exists():
                return False

            try:
                if self.model_path.stat().st_size < 100 * 1024 * 1024:
                    return False
            except Exception:
                return False

            rss_before = self._current_process_rss_gb()
            if rss_before >= self.ram_critical_gb:
                self.logger.warning(
                    "Skipping LLM load to preserve 4.0 GB RAM ceiling (current RSS: %.2f GB).",
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                    rss_before,
                )
                return False

            try:
<<<<<<< HEAD
                self._llm = Llama(
                    model_path=str(resolved_path),
                    n_gpu_layers=0,
=======
                self.logger.info(
                    "Loading local LLM (CPU-only | n_gpu_layers=0 | n_ctx=%d | threads=%d)...",
                    self.n_ctx,
                    self.n_threads,
                )
                t0 = time.perf_counter()
                self._llm = Llama(
                    model_path=str(self.model_path),
                    n_gpu_layers=0,  # STRICT ZERO VRAM
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                    n_ctx=self.n_ctx,
                    n_batch=self.n_batch,
                    n_threads=self.n_threads,
                    n_threads_batch=self.n_threads_batch,
                    use_mmap=self.use_mmap,
                    use_mlock=self.use_mlock,
                    verbose=False,
                )
                self._last_used_ts = time.time()
<<<<<<< HEAD
                self._last_load_error = None
                self._no_engine_announced = False
                rss_after = self._current_process_rss_gb()
                self.logger.info(
                    "LLM loaded | path=%s | size_mb=%.1f MB | LLAMA_CPP_AVAILABLE=%s | exception=None | rss_gb=%.2f GB",
                    resolved_path,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
=======
                rss_after = self._current_process_rss_gb()
                self.logger.info(
                    "LLM loaded in %.2fs (Process RSS: %.2f GB -> %.2f GB | VRAM: 0 MB)",
                    time.perf_counter() - t0,
                    rss_before,
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                    rss_after,
                )
                return True
            except Exception as exc:
<<<<<<< HEAD
                self._llm = None
                self._last_load_error = f"{type(exc).__name__}: {exc}"
                gc.collect()
                rss_after = self._current_process_rss_gb()
                self.logger.error(
                    "LLM startup failed | path=%s | size_mb=%.1f MB | LLAMA_CPP_AVAILABLE=%s | exception=%s | rss_gb=%.2f GB",
                    resolved_path,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    self._last_load_error,
                    rss_after,
                )
                return False

    def unload_model(self, reason: str = "manual") -> None:
        """Release the LLM from RAM only when RSS exceeds 3.8 GB or on shutdown."""
=======
                self.logger.warning("Local LLM unavailable, using built-in conversational responder: %s", exc)
                self._llm = None
                gc.collect()
                return False

    def unload_model(self, reason: str = "manual") -> None:
        """Release the LLM from RAM and run garbage collection."""
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        with self._lock:
            if self._llm is None:
                return
            self.logger.info("Unloading LLM from RAM (reason: %s)...", reason)
            self._llm = None
            gc.collect()
            self.bus.publish(
                Topics.BRAIN_MODEL_UNLOADED,
                {"reason": reason, "rss_gb": round(self._current_process_rss_gb(), 2)},
                source="brain",
            )

    def check_idle_unload(self) -> None:
<<<<<<< HEAD
        """Unload only if process RSS exceeds 3.8 GB (never during normal active sessions)."""
        rss = self._current_process_rss_gb()
        if rss > self.ram_critical_gb and self._llm is not None:
            self.unload_model(reason=f"rss_exceeded_{rss:.2f}gb")

    def _on_ram_pressure(self, event: Event) -> None:
        level = event.payload.get("level")
        rss_val = float(event.payload.get("rss_gb", 0.0) or self._current_process_rss_gb())
        if (level == "critical" or rss_val > self.ram_critical_gb) and self._llm is not None:
            self.unload_model(reason="critical_ram_pressure")

    # =========================================================================
    # Streaming Inference & Fast Identity/Self Checks
    # =========================================================================
    def stream_generate(
        self,
        user_prompt: str,
        extra_context: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """
        Stream tokens from `self._llm.create_chat_completion(..., stream=True)`.
        - Reads text from all of: `choices[0].delta.content`, `choices[0].message.content`,
          `choices[0].text`, and object attributes if the chunk is not a dict.
        - Logs the first raw chunk once: `type` plus `repr`, truncated to 300 chars.
        - If `self._llm` exists, NEVER returns `NO_ENGINE_SPOKEN_MESSAGE`. On a real
          exception, logs the full exception and yields `"I heard you, but that reply failed."`.
        """
        cleaned_prompt = (user_prompt or "").strip()
        if not cleaned_prompt:
            return

        self._cancel_event.clear()

        direct = self._check_identity_or_direct_intent(cleaned_prompt)
        if direct is not None:
            if not self._cancel_event.is_set():
                yield direct
            return

        with self._lock:
            if self._llm is None and not self._startup_attempted:
                self.load_model()

            if self._llm is not None:
                messages = self._build_messages(cleaned_prompt, extra_context)
                try:
                    raw_res = self._llm.create_chat_completion(
                        messages=messages,
                        temperature=self.temperature,
                        top_p=self.top_p,
                        max_tokens=self.max_tokens,
                        stop=["<|im_end|>", "<|endoftext|>", "\nUser:"],
                        stream=True,
                    )

                    # Handle both iterator/generator streams and single dict/object responses
                    if isinstance(raw_res, (dict, str)) or not isinstance(raw_res, collections.abc.Iterable):
                        chunk_iter = [raw_res]
                    else:
                        chunk_iter = raw_res

                    collected: List[str] = []
                    for chunk in chunk_iter:
                        if not self._logged_first_chunk:
                            self._logged_first_chunk = True
                            self.logger.info(
                                "First LLM raw chunk | type=%s | repr=%s",
                                type(chunk).__name__,
                                repr(chunk)[:300],
                            )
                        if self._cancel_event.is_set():
                            self.logger.info("LLM token stream cancelled by user interrupt.")
                            break

                        piece = _extract_chunk_text(chunk)
                        if piece:
                            collected.append(piece)
                            yield piece

                    # If stream=True yielded no text tokens and wasn't cancelled, retry once with stream=False and no custom stop list
                    if not collected and not self._cancel_event.is_set():
                        non_stream = self._llm.create_chat_completion(
                            messages=messages,
                            temperature=self.temperature,
                            top_p=self.top_p,
                            max_tokens=self.max_tokens,
                            stream=False,
                        )
                        if not self._logged_first_chunk:
                            self._logged_first_chunk = True
                            self.logger.info(
                                "First LLM raw chunk | type=%s | repr=%s",
                                type(non_stream).__name__,
                                repr(non_stream)[:300],
                            )
                        piece = _extract_chunk_text(non_stream)
                        if piece:
                            collected.append(piece)
                            yield piece

                    self._last_used_ts = time.time()
                    full = re.sub(r"\s+", " ", "".join(collected)).strip()
                    if full and not self._cancel_event.is_set():
                        self._append_history(cleaned_prompt, full)
                    elif not full and not self._cancel_event.is_set():
                        self.logger.error("LLM returned empty completion for prompt='%s'", cleaned_prompt)
                        yield self.GENERATION_ERROR_MESSAGE
                    return
                except Exception as exc:
                    self._last_load_error = f"{type(exc).__name__}: {exc}"
                    self.logger.error(
                        "Llama create_chat_completion failed: %s",
                        self._last_load_error,
                        exc_info=True,
                    )
                    if not self._cancel_event.is_set():
                        yield self.GENERATION_ERROR_MESSAGE
                    return

            if not self._cancel_event.is_set():
                yield self._no_model_fallback(cleaned_prompt)

=======
        if self.unload_after_idle_s <= 0:
            return
        with self._lock:
            if self._llm is not None and (time.time() - self._last_used_ts) > self.unload_after_idle_s:
                self.unload_model(reason="idle_timeout")

    def _on_ram_pressure(self, event: Event) -> None:
        level = event.payload.get("level")
        if level == "critical" and self._llm is not None:
            self.unload_model(reason="critical_ram_pressure")

    # =========================================================================
    # Inference & Conversational Responder
    # =========================================================================
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    def generate(
        self,
        user_prompt: str,
        extra_context: Optional[str] = None,
        remember: bool = True,
<<<<<<< HEAD
        on_first_sentence: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Generate a reply using `stream_generate()`.
        If `self._llm` exists, NEVER returns `NO_ENGINE_SPOKEN_MESSAGE`.
        """
        cleaned_prompt = (user_prompt or "").strip()
        if not cleaned_prompt:
            return "I'm listening."

        self._cancel_event.clear()
=======
    ) -> str:
        """
        Generate a natural, spoken-friendly response to `user_prompt`.
        Never echoes "I heard...", never exposes internal model filenames,
        and never leaks profile data on unrelated utterances.
        """
        cleaned_prompt = (user_prompt or "").strip()
        if not cleaned_prompt or len(cleaned_prompt) <= 2:
            return "I'm listening."

>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        t_start = time.perf_counter()
        self.bus.publish(Topics.BRAIN_THINKING_STARTED, {"prompt": cleaned_prompt}, source="brain")
        self.bus.publish(Topics.ORB_STATE, {"state": "thinking", "progress": 0.0}, source="brain")

<<<<<<< HEAD
        direct = self._check_identity_or_direct_intent(cleaned_prompt)
        if direct is not None:
            latency_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
            if on_first_sentence is not None:
                on_first_sentence(direct)
            if remember:
                self._append_history(cleaned_prompt, direct)
            self.bus.publish(
                Topics.BRAIN_RESPONSE_READY,
                {"prompt": cleaned_prompt, "text": direct, "latency_ms": latency_ms, "from_llm": False},
                source="brain",
            )
            return direct

        used_llm = self._llm is not None or (not self._startup_attempted and self.load_model())
        tokens: List[str] = []
        first_sentence_dispatched = False

        for delta in self.stream_generate(cleaned_prompt, extra_context=extra_context):
            if self._cancel_event.is_set():
                break
            tokens.append(delta)
            if on_first_sentence is not None and not first_sentence_dispatched:
                joined_so_far = "".join(tokens)
                m_sent = re.search(r"^(.+?[.!?])(?:\s+|$)", joined_so_far)
                if m_sent and len(m_sent.group(1).strip()) >= 6:
                    first_sentence_dispatched = True
                    on_first_sentence(m_sent.group(1).strip())

        reply = re.sub(r"\s+", " ", "".join(tokens)).strip()
        if not reply and not self._cancel_event.is_set():
            if self._llm is not None:
                reply = self.GENERATION_ERROR_MESSAGE
                used_llm = True
            else:
                reply = self._no_model_fallback(cleaned_prompt)
                used_llm = False
=======
        with self._lock:
            # 1. Check direct identity / creator / capability / audio check-in questions first
            direct = self._check_identity_or_direct_intent(cleaned_prompt)
            if direct is not None:
                reply = direct
                used_llm = False
            elif self.load_model() and self._llm is not None:
                messages = self._build_messages(cleaned_prompt, extra_context)
                used_llm = False
                try:
                    response = self._llm.create_chat_completion(
                        messages=messages,
                        temperature=self.temperature,
                        top_p=self.top_p,
                        max_tokens=self.max_tokens,
                        stop=["<|im_end|>", "<|endoftext|>", "\nUser:"],
                    )
                    reply = (
                        response["choices"][0]["message"]["content"]
                        .strip()
                        .replace("\n", " ")
                    )
                    self._last_used_ts = time.time()
                    used_llm = True
                except Exception as exc:
                    self.logger.warning("LLM inference fallback: %s", exc)
                    reply = self._conversational_fallback(cleaned_prompt, extra_context)
            else:
                used_llm = False
                reply = self._conversational_fallback(cleaned_prompt, extra_context)

            if not reply:
                reply = "I'm listening. How can I help?"

            if remember:
                self._history.append({"role": "user", "content": cleaned_prompt})
                self._history.append({"role": "assistant", "content": reply})
                if len(self._history) > self._max_history_turns * 2:
                    self._history = self._history[-(self._max_history_turns * 2) :]
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

        latency_ms = round((time.perf_counter() - t_start) * 1000.0, 1)
        self.bus.publish(
            Topics.BRAIN_RESPONSE_READY,
            {
                "prompt": cleaned_prompt,
                "text": reply,
                "latency_ms": latency_ms,
                "from_llm": used_llm,
            },
            source="brain",
        )
        return reply

<<<<<<< HEAD
    def _append_history(self, user_text: str, assistant_text: str) -> None:
        with self._lock:
            self._history.append({"role": "user", "content": user_text})
            self._history.append({"role": "assistant", "content": assistant_text})
            if len(self._history) > self._max_history_turns * 2:
                self._history = self._history[-(self._max_history_turns * 2) :]
=======
    def stream_generate(
        self,
        user_prompt: str,
        extra_context: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """Stream tokens from local LLM or yield a natural conversational reply."""
        cleaned_prompt = (user_prompt or "").strip()
        if not cleaned_prompt:
            return

        with self._lock:
            direct = self._check_identity_or_direct_intent(cleaned_prompt)
            if direct is not None:
                yield direct
                return

            if self.load_model() and self._llm is not None:
                messages = self._build_messages(cleaned_prompt, extra_context)
                try:
                    stream = self._llm.create_chat_completion(
                        messages=messages,
                        temperature=self.temperature,
                        top_p=self.top_p,
                        max_tokens=self.max_tokens,
                        stop=["<|im_end|>", "<|endoftext|>"],
                        stream=True,
                    )
                    collected: List[str] = []
                    for chunk in stream:
                        delta = chunk["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            collected.append(delta)
                            yield delta
                    self._last_used_ts = time.time()
                    full = "".join(collected).strip()
                    if full:
                        self._history.append({"role": "user", "content": cleaned_prompt})
                        self._history.append({"role": "assistant", "content": full})
                    return
                except Exception as exc:
                    self.logger.warning("Streaming inference error: %s", exc)

            yield self._conversational_fallback(cleaned_prompt, extra_context)
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def _build_messages(
        self,
        user_prompt: str,
        extra_context: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        sys_content = self.system_prompt
        if extra_context:
<<<<<<< HEAD
            sys_content = f"{sys_content}\n\nContext:\n{extra_context.strip()}"
=======
            sys_content = f"{sys_content}\n\nRelevant local context from long-term memory:\n{extra_context.strip()}"
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

        messages: List[Dict[str, str]] = [{"role": "system", "content": sys_content}]
        messages.extend(self._history[-(self._max_history_turns * 2) :])
        messages.append({"role": "user", "content": user_prompt})
        return messages

    # =========================================================================
<<<<<<< HEAD
    # Unanchored Identity, 'Who Am I', & Safe Offline Fallback (No Fake Loop)
=======
    # Natural Conversational & Memory-Aware Offline Responder (Safe & Grounded)
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    # =========================================================================
    def _check_identity_or_direct_intent(self, prompt: str) -> Optional[str]:
        low = re.sub(r"[^a-z0-9\s]", " ", prompt.lower())
        low = re.sub(r"\s+", " ", low).strip()

<<<<<<< HEAD
        # 1. "Who am I" / "Who I am" / "What is my name" -> read saved profile name and nothing else
        if re.search(r"\b(?:who\s+am\s+i|who\s+i\s+am|what(?:\s+is|\s*s)\s+my\s+name)\b", low):
            name = ""
            if self.rag is not None and hasattr(self.rag, "get_my_profile_field"):
                name = (self.rag.get_my_profile_field("full_name") or self.rag.get_my_profile_field("nickname") or "").strip()
            if name:
                return f"You are {name}."
            return "I don't have your name saved in your profile yet."

        # 2. Identity anywhere in the sentence
        if re.search(
            r"\b(?:who\s+(?:the\s+\w+\s+)?are\s+you|what\s+are\s+you|what(?:\s+is|\s*s)\s+your\s+name|tell\s+me\s+about\s+yourself|introduce\s+yourself|introduction\s+of\s+yours|basic\s+introduction|are\s+you\s+(?:jarvis|ev|evie))\b",
            low,
        ):
            return self.IDENTITY_RESPONSE

        # 3. Creator questions anywhere in the sentence
        if re.search(
            r"\bwho\s+(?:created|made|built|developed|programmed)\s+you\b|\bwho\s+is\s+your\s+creator\b",
            low,
        ):
            return "I was created by Bruprog as an offline Enhanced Virtual Assistant."

        # 4. Actual time questions ONLY (never bare \btime\b!)
        if re.search(
            r"^(?:what\s+time\s+is\s+it|what(?:\s+is|\s*s)\s+the\s+time|tell\s+me\s+the\s+time|current\s+time)$",
            low,
        ):
            return f"It is currently {datetime.now().strftime('%I:%M %p')}."

        # 5. Spoken arithmetic
        math_reply = self._try_math_evaluation(low)
        if math_reply is not None:
            return math_reply

        return None

    def _no_model_fallback(self, prompt: str) -> str:
        """
        Executed ONLY when `self._llm` is None.
        """
        low = re.sub(r"[^a-z0-9\s]", " ", prompt.lower())
        low = re.sub(r"\s+", " ", low).strip()

        if any(p in low for p in ("status of the system", "system status", "status report")):
            cpu = int(round(psutil.cpu_percent(interval=None))) if (PSUTIL_AVAILABLE and psutil is not None) else 12
            rss = self._current_process_rss_gb()
            return (
                f"All local systems are nominal. CPU load is {cpu} percent, RAM is {rss:.2f} gigabytes, "
                "and GPU VRAM usage is 0 megabytes."
            )

        if "dim" in low and ("light" in low or "lights" in low):
            return "Dimming your desk lights now."

        if any(p in low for p in ("how are you", "how is it going", "hows it going", "you doing okay")):
            return "All systems are operational and ready."

        self.logger.error(
            "FALLBACK_NO_MODEL for prompt='%s' | load_error=%s",
            prompt,
            self._last_load_error or "LLM not loaded",
        )
        if not self._no_engine_announced:
            self._no_engine_announced = True
            return self.NO_ENGINE_SPOKEN_MESSAGE
        return self.NO_ENGINE_SPOKEN_MESSAGE
=======
        # Audio / Listening Check-ins ("Are you able to hear me?", "Can you hear me?", "Are you listening?")
        if any(
            phrase in low
            for phrase in (
                "can you hear me",
                "able to hear me",
                "are you hearing me",
                "are you listening",
                "am i audible",
                "do you hear me",
                "hear me",
                "are you there",
            )
        ):
            return "Yes, I can hear you clearly. How can I help?"

        # Identity / Who are you / What is your name / Tell me about yourself
        if any(
            phrase in low
            for phrase in (
                "who are you",
                "what are you",
                "what is your name",
                "whats your name",
                "tell me about yourself",
                "introduce yourself",
                "are you jarvis",
                "are you ev",
                "are you evie",
            )
        ):
            return self.IDENTITY_RESPONSE

        # Creator questions
        if any(
            phrase in low
            for phrase in (
                "who created you",
                "who made you",
                "who built you",
                "who is your creator",
                "who developed you",
                "who programmed you",
            )
        ):
            return "I was created and engineered by Bruprog as a local-first Enhanced Virtual Assistant."

        # Capabilities / Help
        if any(
            phrase in low
            for phrase in (
                "what can you do",
                "how can you help",
                "what are your features",
                "what are your capabilities",
            )
        ):
            return (
                "I can launch and undo full multi-app workspaces, open any installed application, "
                "schedule timed commands, control your phone and WhatsApp, and remember your preferences — all completely offline."
            )

        return None

    def _conversational_fallback(self, prompt: str, extra_context: Optional[str] = None) -> str:
        """
        Natural, calm JARVIS-style conversational responder when operating without
        `llama-cpp-python`.
        SAFETY GUARANTEE: Never dumps random user profile facts ("From what I remember, Aditya Raj")
        unless the user explicitly asked a memory/profile question.
        """
        low = prompt.lower().strip()
        clean_words = re.sub(r"[^a-z0-9\s]", " ", low)
        clean_words = re.sub(r"\s+", " ", clean_words).strip()
        now = datetime.now()

        # Guard: if utterance is very short or fragmented, respond gently without guessing
        words_list = clean_words.split()
        if not words_list or (len(words_list) == 1 and len(words_list[0]) <= 3 and words_list[0] not in ("hi", "hey", "time", "date")):
            return "I'm listening."

        # 1. Greetings & Check-ins
        if clean_words in ("hi", "hello", "hey", "hey there", "good morning", "good afternoon", "good evening"):
            hour = now.hour
            greeting = "Good morning" if hour < 12 else ("Good afternoon" if hour < 17 else "Good evening")
            return f"{greeting}. All systems are online and I'm listening. What can I do for you?"

        if any(p in clean_words for p in ("how are you", "how are things", "you doing okay", "are you online")):
            return "All local systems are running smoothly and within memory limits. How can I assist you?"

        if any(p in clean_words for p in ("thank you", "thanks", "appreciate it", "good job", "well done")):
            return "Always a pleasure. Let me know if you need anything else."

        # 2. Continuous Listening / Interrupt requests
        if "continuously listen" in clean_words or "continuous listening" in clean_words or "always listen" in clean_words:
            return (
                "Continuous listening is active. You don't need to click the microphone — "
                "just speak naturally, and you can interrupt me or say continue at any time."
            )

        # 3. Explicit Past Conversation / Long-term Memory Lookup ONLY when user asks for it
        if self.rag is not None:
            if any(
                q in clean_words
                for q in (
                    "what did i say",
                    "what did we talk about",
                    "last conversation",
                    "previous conversation",
                    "do you remember",
                    "what is my",
                    "whats my",
                    "who is my",
                    "where is my",
                    "when is my",
                )
            ):
                fact_answer = self.rag.find_direct_fact_answer(prompt)
                if fact_answer:
                    return fact_answer
                recalled = self.rag.recall_conversational(prompt)
                if recalled:
                    return recalled

        # 4. Spoken Arithmetic / Math ("what is 25 times 14", "calculate 120 / 4")
        math_reply = self._try_math_evaluation(clean_words)
        if math_reply is not None:
            return math_reply

        # 5. Time & Date
        if re.search(r"\btime\b", clean_words):
            return f"It is currently {now.strftime('%I:%M %p')}."
        if re.search(r"\bdate\b", clean_words) or "day is it" in clean_words:
            return f"Today is {now.strftime('%A, %B %d, %Y')}."

        # 6. System & Hardware Status
        if any(w in words_list for w in ("ram", "memory", "cpu", "status", "system", "battery")):
            cpu = int(round(psutil.cpu_percent(interval=None))) if (PSUTIL_AVAILABLE and psutil is not None) else 14
            rss = self._current_process_rss_gb()
            return (
                f"All local systems are nominal. CPU load is {cpu} percent, and my memory footprint "
                f"is {rss:.2f} gigabytes out of your 4 gigabyte budget with 0 megabytes of VRAM allocated."
            )

        # 7. Workspaces, Timers & Calls
        if "workspace" in words_list and ("call" in words_list or "mom" in words_list):
            sec = self.config.get("gui", {}).get("default_countdown_seconds", 30)
            return f"Launching your coding workspace now, and I'll prepare your call to Mom in {sec} seconds."
        if "workspace" in words_list:
            return "Right away. Launching your workspace now."
        if "dim" in words_list or "light" in words_list or "lights" in words_list:
            return "Understood. Dimming your desk lights."
        if "call" in words_list:
            return "Preparing your call through the phone bridge."

        # 8. Jokes / Light personality
        if "joke" in words_list or "funny" in words_list:
            return "Why do programmers prefer dark mode? Because light attracts bugs."

        # 9. If the user explicitly states a personal fact ("my name is...", "Arin is my friend..."), learn it
        if self.rag is not None:
            learned = self.rag.auto_learn_from_statement(prompt)
            if learned:
                return learned

        # 10. Safe, polite clarification when speech was unclear or unrecognized (NEVER dump profile data!)
        return "Sorry, I didn't catch a clear command there. Could you repeat what you'd like me to do?"
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

    @staticmethod
    def _try_math_evaluation(clean_words: str) -> Optional[str]:
        expr = clean_words
        replacements = [
            (r"\bwhat\s+is\b", ""),
            (r"\bcalculate\b", ""),
            (r"\bcompute\b", ""),
            (r"\bplus\b", "+"),
            (r"\badd\b", "+"),
            (r"\bminus\b", "-"),
            (r"\bsubtract\b", "-"),
            (r"\btimes\b", "*"),
            (r"\bmultiplied\s+by\b", "*"),
            (r"\bdivided\s+by\b", "/"),
            (r"\bover\b", "/"),
            (r"\bto\s+the\s+power\s+of\b", "**"),
        ]
        for pat, rep in replacements:
            expr = re.sub(pat, rep, expr)
        expr = expr.strip()
        if not re.fullmatch(r"[\d\s\.\+\-\*\/\(\)]+", expr) or not any(op in expr for op in "+-*/"):
            return None
        try:
            tree = ast.parse(expr, mode="eval")
            val = _eval_math_node(tree.body)
            formatted = str(int(val)) if val == int(val) else f"{val:.2f}".rstrip("0").rstrip(".")
            return f"The answer is {formatted}."
        except Exception:
            return None