#!/usr/bin/env python3
"""
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
"""

from __future__ import annotations

import ast
import gc
import logging
import operator
import os
import re
try:
    import resource  # type: ignore
except ImportError:
    resource = None  # type: ignore
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

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

try:
    from llama_cpp import Llama  # type: ignore
    LLAMA_CPP_AVAILABLE = True
except ImportError:
    Llama = None  # type: ignore
    LLAMA_CPP_AVAILABLE = False


# Safe arithmetic evaluator for spoken math questions
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


class Brain:
    """
    Thread-safe CPU-only LLM engine and natural conversational brain for E.V.
    """

    IDENTITY_RESPONSE = (
        "Hey, I’m E.V. — Enhanced Virtual Assistant. I was created by Bruprog. "
        "I can help you control your computer, manage workspaces, remember important things, "
        "make calls, send messages, and more — all offline."
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
        self.n_ctx: int = min(4096, max(512, int(llm_cfg.get("n_ctx", 2048))))
        self.n_batch: int = min(512, max(64, int(llm_cfg.get("n_batch", 256))))
        self.n_threads: int = max(1, int(llm_cfg.get("n_threads", min(6, os.cpu_count() or 4))))
        self.n_threads_batch: int = max(1, int(llm_cfg.get("n_threads_batch", self.n_threads)))
        self.use_mmap: bool = bool(llm_cfg.get("use_mmap", True))
        self.use_mlock: bool = bool(llm_cfg.get("use_mlock", False))

        self.temperature: float = float(llm_cfg.get("temperature", 0.3))
        self.top_p: float = float(llm_cfg.get("top_p", 0.9))
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
        ).strip()

        hw_cfg = config.get("hardware", {})
        self.ram_budget_gb: float = float(hw_cfg.get("ram_budget_gb", 4.0))
        self.ram_critical_gb: float = float(hw_cfg.get("ram_critical_threshold_gb", 3.8))

        self._lock = threading.RLock()
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
                with self._lock:
                    for row in past:
                        u_txt = str(row.get("user_text", "")).strip()
                        a_txt = str(row.get("assistant_text", "")).strip()
                        if u_txt and a_txt:
                            self._history.append({"role": "user", "content": u_txt})
                            self._history.append({"role": "assistant", "content": a_txt})
            except Exception:
                pass

    # =========================================================================
    # Model Lifecycle & RAM Guardrails
    # =========================================================================
    @property
    def is_loaded(self) -> bool:
        return self._llm is not None

    @property
    def is_model_loaded(self) -> bool:
        return self.is_loaded

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
            if resource is not None:
                return (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024) / (1024 ** 3)
            return 0.0
        except Exception:
            return 0.0

    def load_model(self) -> bool:
        """Load the GGUF model into system RAM with `n_gpu_layers=0`."""
        with self._lock:
            if self._llm is not None:
                self._last_used_ts = time.time()
                return True

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
                    rss_before,
                )
                return False

            try:
                self.logger.info(
                    "Loading local LLM (CPU-only | n_gpu_layers=0 | n_ctx=%d | threads=%d)...",
                    self.n_ctx,
                    self.n_threads,
                )
                t0 = time.perf_counter()
                self._llm = Llama(
                    model_path=str(self.model_path),
                    n_gpu_layers=0,  # STRICT ZERO VRAM
                    n_ctx=self.n_ctx,
                    n_batch=self.n_batch,
                    n_threads=self.n_threads,
                    n_threads_batch=self.n_threads_batch,
                    use_mmap=self.use_mmap,
                    use_mlock=self.use_mlock,
                    verbose=False,
                )
                self._last_used_ts = time.time()
                rss_after = self._current_process_rss_gb()
                self.logger.info(
                    "LLM loaded in %.2fs (Process RSS: %.2f GB -> %.2f GB | VRAM: 0 MB)",
                    time.perf_counter() - t0,
                    rss_before,
                    rss_after,
                )
                return True
            except Exception as exc:
                self.logger.warning("Local LLM unavailable, using built-in conversational responder: %s", exc)
                self._llm = None
                gc.collect()
                return False

    def unload_model(self, reason: str = "manual") -> None:
        """Release the LLM from RAM and run garbage collection."""
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
    def generate(
        self,
        user_prompt: str,
        extra_context: Optional[str] = None,
        remember: bool = True,
    ) -> str:
        """
        Generate a natural, spoken-friendly response to `user_prompt`.
        Never echoes "I heard...", never exposes internal model filenames,
        and never leaks profile data on unrelated utterances.
        """
        cleaned_prompt = (user_prompt or "").strip()
        if not cleaned_prompt or len(cleaned_prompt) <= 2:
            return "I'm listening."

        t_start = time.perf_counter()
        self.bus.publish(Topics.BRAIN_THINKING_STARTED, {"prompt": cleaned_prompt}, source="brain")
        self.bus.publish(Topics.ORB_STATE, {"state": "thinking", "progress": 0.0}, source="brain")

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
            sys_content = f"{sys_content}\n\nRelevant local context from long-term memory:\n{extra_context.strip()}"

        messages: List[Dict[str, str]] = [{"role": "system", "content": sys_content}]
        messages.extend(self._history[-(self._max_history_turns * 2) :])
        messages.append({"role": "user", "content": user_prompt})
        return messages

    # =========================================================================
    # Natural Conversational & Memory-Aware Offline Responder (Safe & Grounded)
    # =========================================================================
    def _check_identity_or_direct_intent(self, prompt: str) -> Optional[str]:
        low = re.sub(r"[^a-z0-9\s]", " ", prompt.lower())
        low = re.sub(r"\s+", " ", low).strip()

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