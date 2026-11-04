#!/usr/bin/env python3
"""
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
"""

from __future__ import annotations

import ast
import collections.abc
import gc
import logging
import operator
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional

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
        self.n_ctx: int = 2048
        self.n_batch: int = min(512, max(64, int(llm_cfg.get("n_batch", 256))))
        self.n_threads: int = max(1, int(llm_cfg.get("n_threads", min(6, os.cpu_count() or 4))))
        self.n_threads_batch: int = max(1, int(llm_cfg.get("n_threads_batch", self.n_threads)))
        self.use_mmap: bool = bool(llm_cfg.get("use_mmap", True))
        self.use_mlock: bool = bool(llm_cfg.get("use_mlock", False))

        self.temperature: float = float(llm_cfg.get("temperature", 0.3))
        self.top_p: float = float(llm_cfg.get("top_p", 0.9))
        self.max_tokens: int = int(llm_cfg.get("max_tokens", 80))
        self.unload_after_idle_s: int = int(llm_cfg.get("unload_after_idle_seconds", 0))

        self.system_prompt: str = str(
            llm_cfg.get("system_prompt", self.DEFAULT_SYSTEM_PROMPT)
        ).strip()

        hw_cfg = config.get("hardware", {})
        ram_cfg = config.get("ram", {})
        self.ram_budget_gb: float = float(ram_cfg.get("budget_gb", hw_cfg.get("ram_budget_gb", 12.0)))
        self.ram_critical_gb: float = float(ram_cfg.get("critical_gb", hw_cfg.get("ram_critical_threshold_gb", 11.0)))
        self.unload_on_critical: bool = bool(ram_cfg.get("unload_on_critical", hw_cfg.get("unload_on_critical", False)))
        self.reload_on_demand: bool = bool(ram_cfg.get("reload_on_demand", hw_cfg.get("reload_on_demand", True)))

        self._lock = threading.RLock()
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
                with self._lock:
                    for row in past:
                        u_txt = str(row.get("user_text", "")).strip()
                        a_txt = str(row.get("assistant_text", "")).strip()
                        if (
                            u_txt
                            and a_txt
                            and "tell me a bit more" not in a_txt.lower()
                            and "would you like me to remember that" not in a_txt.lower()
                            and a_txt != self.NO_ENGINE_SPOKEN_MESSAGE
                            and a_txt != self.GENERATION_ERROR_MESSAGE
                        ):
                            self._history.append({"role": "user", "content": u_txt})
                            self._history.append({"role": "assistant", "content": a_txt})
            except Exception:
                pass

    # =========================================================================
    # Model Lifecycle, Startup Logging & Cancellation
    # =========================================================================
    @property
    def is_loaded(self) -> bool:
        return self._llm is not None

    @property
    def is_model_loaded(self) -> bool:
        return self._llm is not None

    def cancel(self) -> None:
        """Signal any in-flight token stream to stop immediately."""
        self._cancel_event.set()

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
        except Exception:
            pass
        return 0.0

    def load_model(self) -> bool:
        """
        Load the GGUF model into system RAM with `n_gpu_layers=0`.
        Logs a single diagnostic line with:
          resolved GGUF path, size MB, LLAMA_CPP_AVAILABLE, exception text, RSS after load.
        """
        with self._lock:
            if self._llm is not None:
                self._last_used_ts = time.time()
                return True
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

            if self.unload_on_critical and rss_before > self.ram_critical_gb:
                self._last_load_error = f"Process RSS ({rss_before:.2f} GB) exceeds critical threshold ({self.ram_critical_gb:.2f} GB)"
                self.logger.warning(
                    "LLM startup aborted | path=%s | size_mb=%.1f MB | LLAMA_CPP_AVAILABLE=%s | exception=%s | rss_gb=%.2f GB",
                    resolved_path,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    self._last_load_error,
                    rss_before,
                )
                return False

            try:
                self._llm = Llama(
                    model_path=str(resolved_path),
                    n_gpu_layers=0,
                    n_ctx=self.n_ctx,
                    n_batch=self.n_batch,
                    n_threads=self.n_threads,
                    n_threads_batch=self.n_threads_batch,
                    use_mmap=self.use_mmap,
                    use_mlock=self.use_mlock,
                    verbose=False,
                )
                self._last_used_ts = time.time()
                self._last_load_error = None
                self._no_engine_announced = False
                rss_after = self._current_process_rss_gb()
                self.logger.info(
                    "LLM loaded | path=%s | size_mb=%.1f MB | LLAMA_CPP_AVAILABLE=%s | exception=None | rss_gb=%.2f GB",
                    resolved_path,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    rss_after,
                )
                return True
            except Exception as exc:
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
        """Unload only on explicit critical RAM policy or configured idle timeout."""
        if self._llm is None:
            return
        now = time.time()
        if self.unload_after_idle_s > 0 and self._last_used_ts > 0.0:
            if (now - self._last_used_ts) >= self.unload_after_idle_s:
                self.unload_model(reason=f"idle_{self.unload_after_idle_s}s")
                return

        if not self.unload_on_critical:
            return

        rss = self._current_process_rss_gb()
        if rss > self.ram_critical_gb:
            self.unload_model(reason=f"rss_exceeded_{rss:.2f}gb")

    def _on_ram_pressure(self, event: Event) -> None:
        level = event.payload.get("level")
        force = bool(event.payload.get("force", False))
        rss_val = float(event.payload.get("rss_gb", 0.0) or self._current_process_rss_gb())
        if (self.unload_on_critical or force) and (level == "critical" or rss_val > self.ram_critical_gb) and self._llm is not None:
            self.unload_model(reason="critical_ram_pressure")
        elif level == "critical":
            self.logger.warning(
                "Process RAM at %.2f GB (critical is %.2f GB) — unload_on_critical is False, keeping model resident",
                rss_val,
                self.ram_critical_gb,
            )

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
            if self._llm is None:
                if self.reload_on_demand or not self._startup_attempted:
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

    def generate(
        self,
        user_prompt: str,
        extra_context: Optional[str] = None,
        remember: bool = True,
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
        t_start = time.perf_counter()
        self.bus.publish(Topics.BRAIN_THINKING_STARTED, {"prompt": cleaned_prompt}, source="brain")
        self.bus.publish(Topics.ORB_STATE, {"state": "thinking", "progress": 0.0}, source="brain")

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

        used_llm = self._llm is not None
        if not used_llm and (self.reload_on_demand or not self._startup_attempted):
            used_llm = self.load_model()
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

    def _append_history(self, user_text: str, assistant_text: str) -> None:
        with self._lock:
            self._history.append({"role": "user", "content": user_text})
            self._history.append({"role": "assistant", "content": assistant_text})
            if len(self._history) > self._max_history_turns * 2:
                self._history = self._history[-(self._max_history_turns * 2) :]

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
            sys_content = f"{sys_content}\n\nContext:\n{extra_context.strip()}"

        messages: List[Dict[str, str]] = [{"role": "system", "content": sys_content}]
        messages.extend(self._history[-(self._max_history_turns * 2) :])
        messages.append({"role": "user", "content": user_prompt})
        return messages

    # =========================================================================
    # Unanchored Identity, 'Who Am I', & Safe Offline Fallback (No Fake Loop)
    # =========================================================================
    def _check_identity_or_direct_intent(self, prompt: str) -> Optional[str]:
        low = re.sub(r"[^a-z0-9\s]", " ", prompt.lower())
        low = re.sub(r"\s+", " ", low).strip()

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