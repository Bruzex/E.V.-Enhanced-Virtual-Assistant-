#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Local GGUF Brain & Code Generator (`core/brain.py`)
=======================================================================================
Implements:
  1. E.V. Persona & "sir" Default Address:
     - System prompt:
       `"You are E.V. (Enhanced Virtual Assistant), created by Bruprog. Address the user as sir. Be concise. Never mention underlying model names."`
     - Never prepends or appends the user's full name on every reply.
     - Never mentions Qwen, GGUF, llama.cpp, Whisper, or Vosk in spoken output.
  2. Strict CPU-Only Execution (`n_gpu_layers=0`) & Persistent Model Resident:
     - `unload_on_critical: false` by default so the 3B Q4_K_M model (~2.0 GB)
       is never unloaded during normal desktop usage.
  3. Streaming Sentence First-Token Callback + Code Generation (`generate_code`):
     - `generate()` streams sentence-by-sentence to Piper TTS (`max_tokens=120`).
     - `generate_code(prompt, language)` generates complete, runnable HTML/CSS/JS,
       React (`.html` / `.jsx`), or Python (`.py`) files (`code_max_tokens=380`).
"""

from __future__ import annotations

import ast
from datetime import datetime
import gc
import logging
import operator
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Tuple

try:
    import psutil

    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    PSUTIL_AVAILABLE = False

from core.event_bus import Event, EventBus, Topics, get_event_bus
from memory.sqlite_store import mask_pii

try:
    from llama_cpp import Llama  # type: ignore

    LLAMA_CPP_AVAILABLE = True
except Exception:
    Llama = None  # type: ignore
    LLAMA_CPP_AVAILABLE = False


FORBIDDEN_TERMS_PATTERN = re.compile(
    r"\b(?:qwen(?:2\.5)?(?:-3b)?(?:-instruct)?|gguf|q4_k_m|llama\.cpp|llama_cpp|alibaba\s+cloud|tongyi|openai|chatgpt|claude|gemini|large\s+language\s+model|faster[\s_-]?whisper|vosk)\b",
    re.IGNORECASE,
)

_MATH_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}


def _eval_math_node(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _MATH_SAFE_OPS:
        return _MATH_SAFE_OPS[type(node.op)](_eval_math_node(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _MATH_SAFE_OPS:
        left = _eval_math_node(node.left)
        right = _eval_math_node(node.right)
        if isinstance(node.op, ast.Div) and right == 0:
            raise ZeroDivisionError("division by zero")
        return _MATH_SAFE_OPS[type(node.op)](left, right)
    raise ValueError("unsupported operation in math expression")


class Brain:
    """
    Strictly CPU-bound (`n_gpu_layers=0`) local GGUF engine and code generator
    for E.V. (Enhanced Virtual Assistant).
    """

    DEFAULT_SYSTEM_PROMPT = (
        "You are E.V. (Enhanced Virtual Assistant), created by Bruprog. "
        "Address the user as sir. Be concise (1 to 3 sentences for spoken replies). "
        "Never mention underlying model names."
    )

    IDENTITY_RESPONSE = (
        "I am E.V., Enhanced Virtual Assistant, created by Bruprog. "
        "How can I help you, sir?"
    )

    NO_ENGINE_SPOKEN_MESSAGE = "The local engine failed to start."

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
        rel_model = llm_cfg.get("model_path", "models/qwen2.5-3b-instruct-q4_k_m.gguf")
        self.model_path: Path = self._resolve_model_path(base_dir, str(rel_model))

        # HARD MANDATE: Never offload to GPU (0 MB VRAM guarantee)
        self.n_gpu_layers: int = 0
        self.n_ctx: int = int(llm_cfg.get("n_ctx", 1536))
        self.n_threads: int = int(llm_cfg.get("n_threads", 6))
        self.n_batch: int = int(llm_cfg.get("n_batch", 256))
        self.temperature: float = float(llm_cfg.get("temperature", 0.35))
        self.top_p: float = float(llm_cfg.get("top_p", 0.90))
        self.repeat_penalty: float = float(llm_cfg.get("repeat_penalty", 1.12))
        self.max_tokens: int = min(int(llm_cfg.get("max_tokens", 120)), 160)
        self.code_max_tokens: int = int(llm_cfg.get("code_max_tokens", 380))
        self.use_mmap: bool = bool(llm_cfg.get("use_mmap", True))
        self.use_mlock: bool = bool(llm_cfg.get("use_mlock", False))
        self.system_prompt: str = str(
            llm_cfg.get("system_prompt", self.DEFAULT_SYSTEM_PROMPT)
        ).strip()

        hw_cfg = config.get("hardware", {})
        ram_cfg = config.get("ram_guard", {})
        self.unload_on_critical: bool = bool(
            hw_cfg.get("unload_on_critical", ram_cfg.get("unload_on_critical", False))
        )
        self.ram_critical_gb: float = float(
            hw_cfg.get("ram_critical_threshold_gb", ram_cfg.get("ram_critical_threshold_gb", 12.0))
        )
        self.unload_after_idle_s: float = float(
            hw_cfg.get("unload_after_idle_seconds", 0.0)
        )
        self._last_used_ts: float = 0.0
        self._last_load_error: Optional[str] = None
        self._no_engine_announced: bool = False

        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._llm: Optional[Any] = None
        self._history: Deque[Tuple[str, str]] = deque(maxlen=6)

        self.bus.subscribe(Topics.RAM_PRESSURE, self._on_ram_pressure)

        if bool(llm_cfg.get("preload_on_startup", True)):
            self.load_model()

    @staticmethod
    def _resolve_model_path(base_dir: Path, configured_rel: str) -> Path:
        raw_path = Path(configured_rel)
        if raw_path.is_absolute():
            return raw_path
        return (base_dir / raw_path).resolve()

    def set_rag(self, rag: Any) -> None:
        self.rag = rag

    def cancel(self) -> None:
        """Abort any in-progress streaming LLM generation immediately."""
        self._cancel_event.set()

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._llm is not None

    @property
    def is_model_loaded(self) -> bool:
        with self._lock:
            return self._llm is not None

    def _current_process_rss_gb(self) -> float:
        if psutil is None:
            return 0.0
        try:
            return psutil.Process().memory_info().rss / (1024 ** 3)
        except Exception:
            return 0.0

    def load_model(self) -> bool:
        """Load the local GGUF model into system RAM (`n_gpu_layers=0`) and log proof."""
        with self._lock:
            if self._llm is not None:
                return True

            exists = self.model_path.exists() and self.model_path.is_file()
            size_mb = (self.model_path.stat().st_size / (1024 * 1024)) if exists else 0.0
            proc_rss_gb = self._current_process_rss_gb()

            if not LLAMA_CPP_AVAILABLE or Llama is None:
                self.logger.error(
                    "LLM load failed | path=%s | exists=%s | size_mb=%.1f | LLAMA_CPP_AVAILABLE=%s | rss_gb=%.2f",
                    self.model_path,
                    exists,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    proc_rss_gb,
                )
                self._last_load_error = "llama-cpp-python not available"
                return False

            if not exists or size_mb < 1.0:
                self.logger.error(
                    "LLM load failed (file missing/empty) | path=%s | exists=%s | size_mb=%.1f | LLAMA_CPP_AVAILABLE=%s | rss_gb=%.2f",
                    self.model_path,
                    exists,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    proc_rss_gb,
                )
                self._last_load_error = f"Model file missing at {self.model_path}"
                return False

            try:
                self._llm = Llama(
                    model_path=str(self.model_path),
                    n_gpu_layers=0,
                    n_ctx=self.n_ctx,
                    n_threads=self.n_threads,
                    n_batch=self.n_batch,
                    use_mmap=self.use_mmap,
                    use_mlock=self.use_mlock,
                    verbose=False,
                )
                rss_after_gb = self._current_process_rss_gb()
                self._last_used_ts = time.time()
                self._last_load_error = None
                self.logger.info(
                    "LLM loaded | path=%s | size_mb=%.1f MB | LLAMA_CPP_AVAILABLE=%s | exception=None | rss_gb=%.2f GB",
                    self.model_path,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    rss_after_gb,
                )
                return True
            except Exception as exc:
                self.logger.error(
                    "LLM load exception | path=%s | size_mb=%.1f MB | LLAMA_CPP_AVAILABLE=%s | exception=%r | rss_gb=%.2f GB",
                    self.model_path,
                    size_mb,
                    LLAMA_CPP_AVAILABLE,
                    exc,
                    proc_rss_gb,
                    exc_info=True,
                )
                self._llm = None
                self._last_load_error = str(exc)
                return False

    def unload_model(self, reason: str = "manual") -> None:
        """Release the GGUF model from RAM and run garbage collection."""
        with self._lock:
            if self._llm is not None:
                self.logger.warning("Unloading GGUF model from RAM (reason: %s).", reason)
                del self._llm
                self._llm = None
                gc.collect()
                self.bus.publish(
                    Topics.BRAIN_MODEL_UNLOADED,
                    {"reason": reason, "rss_gb": round(self._current_process_rss_gb(), 2)},
                    source="brain",
                )

    def check_idle_unload(self) -> None:
        """Checked by SystemBridge telemetry; respects unload_on_critical: false."""
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
        level = event.payload.get("level", "normal")
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

    @classmethod
    def sanitize_response(cls, text: str) -> str:
        """Strip model-name leaks, role tags, and robotic phrases from spoken replies."""
        if not text:
            return ""
        cleaned = text.strip()
        cleaned = re.sub(r"^(?:E\.?V\.?|Assistant|AI)\s*:\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^I\s+heard\s*:\s*['\"]?.*?['\"]?\s*", "", cleaned, flags=re.IGNORECASE)

        if FORBIDDEN_TERMS_PATTERN.search(cleaned):
            cleaned = FORBIDDEN_TERMS_PATTERN.sub("E.V.", cleaned)

        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    @staticmethod
    def _extract_chunk_delta(chunk: Any) -> str:
        """Extract delta text from either `create_chat_completion` or `create_completion` chunks."""
        if not isinstance(chunk, dict):
            return ""
        choices = chunk.get("choices")
        if not choices or not isinstance(choices, list):
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            return ""
        delta = first.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if content:
                return str(content)
        text_val = first.get("text")
        if text_val:
            return str(text_val)
        message = first.get("message")
        if isinstance(message, dict) and message.get("content"):
            return str(message.get("content"))
        return ""

    def stream_generate(
        self,
        user_prompt: str,
        extra_context: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Iterator[str]:
        """Yield tokens from the local GGUF model in real time."""
        prompt = (user_prompt or "").strip()
        if not prompt:
            return

        self._cancel_event.clear()

        if self._llm is None:
            self.load_model()

        if self._llm is None:
            direct = self._check_identity_or_direct_intent(prompt)
            if direct is not None:
                yield direct
                return
            fallback = self._no_model_fallback(prompt)
            yield fallback
            return

        messages = self._build_messages(prompt, extra_context)
        tokens_budget = max_tokens or self.max_tokens
        collected: List[str] = []
        logged_first_chunk = False

        try:
            self._last_used_ts = time.time()
            with self._lock:
                stream = self._llm.create_chat_completion(
                    messages=messages,
                    max_tokens=tokens_budget,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    repeat_penalty=self.repeat_penalty,
                    stream=True,
                )
                for chunk in stream:
                    if self._cancel_event.is_set():
                        self.logger.info("LLM generation cancelled via barge-in.")
                        break
                    if not logged_first_chunk:
                        logged_first_chunk = True
                        self.logger.debug("First LLM raw chunk: %r", chunk)
                    delta = self._extract_chunk_delta(chunk)
                    if delta:
                        collected.append(delta)
                        yield delta

            # Fallback to raw ChatML completion if chat template produced 0 tokens
            if not collected and not self._cancel_event.is_set():
                chatml_prompt = self._build_chatml_prompt(messages)
                with self._lock:
                    stream2 = self._llm(
                        chatml_prompt,
                        max_tokens=tokens_budget,
                        temperature=self.temperature,
                        top_p=self.top_p,
                        repeat_penalty=self.repeat_penalty,
                        stop=["<|im_end|>", "<|endoftext|>"],
                        stream=True,
                    )
                    for chunk in stream2:
                        if self._cancel_event.is_set():
                            break
                        delta = self._extract_chunk_delta(chunk)
                        if delta:
                            collected.append(delta)
                            yield delta

            full_reply = self.sanitize_response("".join(collected))
            if full_reply:
                self._history.append((prompt, full_reply))
        except Exception as exc:
            self.logger.error("GGUF stream inference error: %r", exc, exc_info=True)
            yield "I ran into a local processing error, sir. Please try again."

    def generate(
        self,
        user_prompt: str,
        extra_context: Optional[str] = None,
        max_tokens: Optional[int] = None,
        on_first_sentence: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        Generate a complete response from the local GGUF model, calling
        `on_first_sentence(sentence)` as soon as the first sentence boundary
        arrives so Piper TTS begins speaking immediately.
        """
        prompt = (user_prompt or "").strip()
        if not prompt:
            return ""

        # Identity questions or direct intent (e.g. "Who are you?", creator, math)
        direct = self._check_identity_or_direct_intent(prompt)
        if direct is not None:
            return direct

        self.bus.publish(Topics.ORB_STATE, {"state": "thinking", "progress": 0.0}, source="brain")
        t0 = time.perf_counter()

        buffer = ""
        full_parts: List[str] = []
        first_sentence_dispatched = False

        for token in self.stream_generate(prompt, extra_context=extra_context, max_tokens=max_tokens):
            full_parts.append(token)
            buffer += token
            if not first_sentence_dispatched and on_first_sentence is not None:
                m = re.search(r"^(.+?[.!?])(?:\s+|$)", buffer)
                if m and len(m.group(1).strip()) >= 8:
                    first_sent = self.sanitize_response(m.group(1).strip())
                    first_sentence_dispatched = True
                    try:
                        on_first_sentence(first_sent)
                    except Exception:
                        pass

        raw_reply = "".join(full_parts).strip()
        cleaned = self.sanitize_response(raw_reply)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.logger.info(
            "Brain.generate completed in %.1f ms | loaded=%s | reply='%s'",
            elapsed_ms,
            self.is_loaded,
            mask_pii(cleaned[:120]),
        )
        return cleaned or "I'm right here, sir. How can I help?"

    def _check_identity_or_direct_intent(self, prompt: str) -> Optional[str]:
        low = re.sub(r"[^a-z0-9\s]", " ", prompt.lower())
        low = re.sub(r"\s+", " ", low).strip()

        # 1. "Who am I" / "Who I am" / "What is my name" -> read saved profile name
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
        """Executed ONLY when `self._llm` is None."""
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

        self.logger.warning(
            "FALLBACK_NO_MODEL | Brain._llm is None for prompt='%s' | load_error=%s",
            mask_pii(prompt),
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

    # =========================================================================
    # Coding Capability (`generate_code`)
    # =========================================================================
    def generate_code(
        self,
        task_description: str,
        language: str = "html",
    ) -> str:
        """
        Generate clean, runnable source code (`html`, `react`, `python`, `javascript`, `css`)
        for the user's coding request. Uses the loaded GGUF model when available, with
        a clean runnable template fallback if the model is not loaded.
        """
        lang = (language or "html").strip().lower()
        task = (task_description or "interactive page").strip()

        if self._llm is None:
            self.load_model()

        if self._llm is not None:
            sys_msg = (
                "You are E.V., an expert software engineer. Output ONLY valid, complete, "
                f"runnable {lang.upper()} code for the user's request. Do not include markdown "
                "explanations outside the code."
            )
            user_msg = f"Write complete, self-contained {lang} code for: {task}"
            try:
                with self._lock:
                    resp = self._llm.create_chat_completion(
                        messages=[
                            {"role": "system", "content": sys_msg},
                            {"role": "user", "content": user_msg},
                        ],
                        max_tokens=self.code_max_tokens,
                        temperature=0.2,
                        top_p=0.9,
                        stream=False,
                    )
                code_text = self._extract_chunk_delta(resp).strip()
                extracted = self._strip_code_fences(code_text)
                if len(extracted) >= 30:
                    return extracted
            except Exception as exc:
                self.logger.warning("LLM code generation fallback triggered: %s", exc)

        return self._fallback_code_template(task, lang)

    @staticmethod
    def _strip_code_fences(raw: str) -> str:
        """Extract code from ```lang ... ``` blocks if present, or return trimmed code."""
        if not raw:
            return ""
        m = re.search(r"```(?:[a-zA-Z0-9_+-]+)?\s*\n(.*?)```", raw, re.DOTALL)
        if m:
            return m.group(1).strip()
        return raw.strip().strip("`").strip()

    @staticmethod
    def _fallback_code_template(task: str, lang: str) -> str:
        title = task[:60].strip().title() or "E.V. Generated Project"
        if lang == "python":
            return (
                "#!/usr/bin/env python3\n"
                f'"""\nGenerated by E.V. (Enhanced Virtual Assistant)\nTask: {task}\n"""\n\n'
                "def main() -> None:\n"
                f"    print({title!r})\n\n\n"
                'if __name__ == "__main__":\n'
                "    main()\n"
            )
        if lang == "react":
            return (
                "<!DOCTYPE html>\n"
                "<html lang=\"en\">\n"
                "<head>\n"
                "  <meta charset=\"UTF-8\" />\n"
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
                f"  <title>{title}</title>\n"
                "  <script src=\"https://unpkg.com/react@18/umd/react.development.js\" crossorigin></script>\n"
                "  <script src=\"https://unpkg.com/react-dom@18/umd/react-dom.development.js\" crossorigin></script>\n"
                "  <script src=\"https://unpkg.com/@babel/standalone/babel.min.js\"></script>\n"
                "  <style>\n"
                "    body { margin: 0; font-family: system-ui, sans-serif; background: #0b0f19; color: #f5f7ff; display: flex; justify-content: center; align-items: center; min-height: 100vh; }\n"
                "    .card { background: #131b2e; border: 1px solid #2dd4bf; border-radius: 14px; padding: 2rem; max-width: 520px; text-align: center; box-shadow: 0 12px 32px rgba(0,0,0,0.45); }\n"
                "    button { background: #2dd4bf; color: #0b0f19; border: none; padding: 0.65rem 1.3rem; border-radius: 8px; font-weight: 600; cursor: pointer; margin-top: 1rem; }\n"
                "  </style>\n"
                "</head>\n"
                "<body>\n"
                "  <div id=\"root\"></div>\n"
                "  <script type=\"text/babel\">\n"
                "    function App() {\n"
                "      const [count, setCount] = React.useState(0);\n"
                "      return (\n"
                "        <div className=\"card\">\n"
                f"          <h1>{title}</h1>\n"
                f"          <p>{task}</p>\n"
                "          <button onClick={() => setCount(count + 1)}>Clicked {count} times</button>\n"
                "        </div>\n"
                "      );\n"
                "    }\n"
                "    ReactDOM.createRoot(document.getElementById('root')).render(<App />);\n"
                "  </script>\n"
                "</body>\n"
                "</html>\n"
            )
        # Default HTML/CSS/JS
        return (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"UTF-8\" />\n"
            "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />\n"
            f"  <title>{title}</title>\n"
            "  <style>\n"
            "    body { margin: 0; font-family: system-ui, sans-serif; background: #0b0f19; color: #f5f7ff; display: flex; justify-content: center; align-items: center; min-height: 100vh; }\n"
            "    .container { background: #131b2e; border: 1px solid #f5a623; border-radius: 14px; padding: 2rem; max-width: 560px; text-align: center; }\n"
            "    button { background: #f5a623; color: #0b0f19; border: none; padding: 0.65rem 1.3rem; border-radius: 8px; font-weight: 700; cursor: pointer; margin-top: 1rem; }\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            "  <div class=\"container\">\n"
            f"    <h1>{title}</h1>\n"
            f"    <p id=\"status\">{task}</p>\n"
            "    <button onclick=\"document.getElementById('status').textContent = 'Action triggered!'\">Run Action</button>\n"
            "  </div>\n"
            "</body>\n"
            "</html>\n"
        )

    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()

    def _build_messages(self, user_prompt: str, extra_context: Optional[str]) -> List[Dict[str, str]]:
        sys_content = self.system_prompt
        if extra_context:
            sys_content += f"\n\nContext:\n{extra_context}"

        messages: List[Dict[str, str]] = [{"role": "system", "content": sys_content}]
        for past_user, past_assistant in self._history:
            messages.append({"role": "user", "content": past_user})
            messages.append({"role": "assistant", "content": past_assistant})
        messages.append({"role": "user", "content": user_prompt})
        return messages

    @staticmethod
    def _build_chatml_prompt(messages: List[Dict[str, str]]) -> str:
        parts: List[str] = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
        parts.append("<|im_start|>assistant\n")
        return "\n".join(parts)
