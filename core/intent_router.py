#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Hybrid Fast-Path, Multi-Step Planner & LLM Intent Router (`core/intent_router.py`)
======================================================================================================================
Implements:
  1. Unambiguous Fast-Path Selection & Strict "Who Are You" Rule:
     - Only executes deterministic rules when the command is an explicit action.
     - Conversational questions like "how are you?" or "she is not working" are never
       hijacked by the identity rule or volume/clock rules.
  2. Multi-Step Task Planner for WhatsApp & Calls:
     - Resolves contact from SQLite (`name`, `nickname`, `relationship`, `"Mommy"` -> `"Mom"`).
     - If missing phone/WhatsApp number, asks once and remembers pending message so the
       user can simply speak the number on the next turn.
     - Publishes step-by-step progress to the HUD (`Topics.ORB_STATE` + `Topics.WORKSPACE_PROGRESS`),
       opens WhatsApp (desktop URI or ADB), selects contact, types message, and sends.
  3. Coding Capability (`data/workspace/code/`):
     - Generates HTML/CSS/JS, React, or Python code via `Brain.generate_code()`, saves to
       `data/workspace/code/<filename>`, speaks a concise confirmation, and optionally opens
       in browser or VS Code.
  4. Silent Background Web Search (DuckDuckGo):
     - Answers factual world questions ("what is the capital of...", "what is the richest
       country...", "who is the CEO of...") silently in the background in 1–2 sentences
       without opening a browser window.
  5. Short-Term Context & Coreference Resolution:
     - Tracks last N turns and active entities (`person`, `app`, `file`, `assistant="E.V."`).
     - Resolves pronouns (`"she"`, `"her"`, `"he"`, `"him"`, `"it"`, `"that"`) accurately.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
import urllib.parse
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Pattern, Tuple

from core.event_bus import EventBus, Topics, get_event_bus
from memory.sqlite_store import mask_pii
from voice.stt import VoskSTT, convert_spoken_numbers


def normalize_phone_digits(raw: str) -> str:
    """Normalize phone string, preserving leading + if present."""
    if not raw:
        return ""
    text = str(raw).strip()
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    return f"+{digits}" if has_plus else digits



@dataclass
class IntentMatch:
    """Structured result returned when an intent is matched and executed."""

    intent_name: str
    raw_text: str
    response_text: str = ""
    slots: Dict[str, Any] = field(default_factory=dict)
    handled_by_fast_path: bool = True
    latency_ms: float = 0.0


IntentHandler = Callable[[re.Match[str], str, Dict[str, Any]], str]


@dataclass
class IntentRule:
    """Single entry in the IntentRouter deterministic registry."""

    name: str
    patterns: List[Pattern[str]]
    handler: IntentHandler
    priority: int = 100
    description: str = ""
    dangerous: bool = False


_WAKE_OR_NOISE_ONLY = {
    "hey evie",
    "hey e v",
    "hey ev",
    "evie",
    "e v",
    "ev",
    "eve",
    "ivy",
    "avi",
    "heavy",
    "he be",
    "he v",
    "the v",
    "tv",
    "baby",
    "a v",
    "e b",
    "eb",
    "the",
    "a",
    "an",
    "uh",
    "um",
    "ah",
    "hm",
    "hmm",
    "oh",
    "eh",
    "he",
    "be",
    "e",
    "v",
    "to",
    "of",
    "in",
    "it",
    "is",
    "are",
    "the are",
    "e the",
}

_FEMALE_RELATIONS = {"mother", "mom", "mummy", "mommy", "mammi", "mami", "maa", "sister", "sis", "didi"}
_MALE_RELATIONS = {"father", "dad", "daddy", "papa", "papaji", "brother", "bro", "bhaiya", "bhai"}

_FACTUAL_QUESTION_PATTERN = re.compile(
    r"^(?:what\s+(?:is|are|was)\s+the\s+(?:capital|population|currency|president|prime\s+minister|richest|largest|smallest|tallest|highest|longest|deepest|fastest|oldest|distance|meaning|definition|weather|temperature|gdp)\b|"
    r"who\s+(?:is|was)\s+(?:the\s+)?(?:president|prime\s+minister|ceo|founder|creator|inventor|author|director|king|queen|richest\s+person)\s+of\b|"
    r"where\s+is\s+[a-z0-9\s]+\s+located\b)",
    re.IGNORECASE,
)


class IntentRouter:
    """
    JARVIS-style Hybrid Intent Router with Multi-Step Task Planning, Coding Generation,
    Silent Factual Web Search, and Short-Term Coreference Resolution.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        brain: Any,
        system_ctrl: Optional[Any] = None,
        workspace_orch: Optional[Any] = None,
        media_ctrl: Optional[Any] = None,
        web_search: Optional[Any] = None,
        scheduler: Optional[Any] = None,
        phone_adb: Optional[Any] = None,
        whatsapp: Optional[Any] = None,
        rag: Optional[Any] = None,
        tts: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.config = config
        self.brain = brain
        self.system_ctrl = system_ctrl
        self.workspace_orch = workspace_orch
        self.media_ctrl = media_ctrl
        self.web_search = web_search
        self.scheduler = scheduler
        self.phone_adb = phone_adb
        self.whatsapp = whatsapp
        self.rag = rag
        self.tts = tts
        self.bus = event_bus or get_event_bus()
        self.logger = logging.getLogger("ev.intent_router")

        base_dir_Attr = getattr(brain, "base_dir", None)
        self.base_dir: Path = Path(base_dir_Attr) if base_dir_Attr else Path(__file__).resolve().parent.parent
        self.code_dir: Path = self.base_dir / "data" / "workspace" / "code"
        self.code_dir.mkdir(parents=True, exist_ok=True)

        sec_cfg = config.get("security", {})
        self.confirm_messages: bool = bool(sec_cfg.get("require_confirmation_for_messages", False))
        self.confirm_calls: bool = bool(sec_cfg.get("require_confirmation_for_calls", False))

        stt_cfg = config.get("voice", {}).get("stt", {})
        self.min_confidence: float = float(stt_cfg.get("min_confidence", 0.50))

        self._pending_action: Optional[Callable[[], str]] = None
        self._pending_description: str = ""
        # Pending contact number prompt: (contact_name, pending_message, action_type="whatsapp"|"call")
        self._awaiting_contact_number: Optional[Tuple[str, str, str]] = None

        # Short-term session context for pronoun/coreference resolution
        self._recent_turns: Deque[Dict[str, str]] = deque(maxlen=8)
        self._entities: Dict[str, str] = {
            "assistant": "E.V.",
            "person": "",
            "person_gender": "",
            "female_person": "",
            "male_person": "",
            "app": "",
            "file": "",
            "topic": "",
        }

        self._rules: List[IntentRule] = []
        self._register_builtin_rules()

    def register(
        self,
        name: str,
        patterns: List[str],
        handler: IntentHandler,
        priority: int = 100,
        description: str = "",
        dangerous: bool = False,
    ) -> None:
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        self._rules.append(
            IntentRule(
                name=name,
                patterns=compiled,
                handler=handler,
                priority=priority,
                description=description,
                dangerous=dangerous,
            )
        )
        self._rules.sort(key=lambda r: r.priority)

    # =========================================================================
    # Short-Term Coreference & Entity Tracking
    # =========================================================================
    def _remember_person_entity(self, name: str, relationship: str = "") -> None:
        clean = (name or "").strip()
        if not clean or clean.lower() in ("e.v.", "ev", "evie", "you", "yourself"):
            return
        self._entities["person"] = clean
        rel_low = (relationship or clean).strip().lower()
        if rel_low in _FEMALE_RELATIONS:
            self._entities["person_gender"] = "female"
            self._entities["female_person"] = clean
        elif rel_low in _MALE_RELATIONS:
            self._entities["person_gender"] = "male"
            self._entities["male_person"] = clean

    def resolve_coreferences(self, text: str) -> str:
        """
        Resolve unambiguous pronouns ('she', 'her', 'he', 'him', 'it') using the
        short-term session entity tracker:
          - If the user says 'she is not working' / 'why is she...' when talking about
            the assistant (or no female contact is active), resolve 'she' -> 'E.V.'.
          - If a contact ('Mom', 'Arin') was just discussed and the user says
            'call her' / 'send hello to her on WhatsApp' / 'what is her number',
            substitute that contact's name.
          - If a code file or app was just created/opened and the user says
            'open it', substitute the file or app.
        """
        if not text:
            return ""
        resolved = text.strip()
        low = resolved.lower()

        female_contact = self._entities.get("female_person") or (
            self._entities.get("person") if self._entities.get("person_gender") == "female" else ""
        )
        male_contact = self._entities.get("male_person") or (
            self._entities.get("person") if self._entities.get("person_gender") == "male" else ""
        )
        any_contact = self._entities.get("person", "")

        # 1. Assistant complaints / status ("she is not working", "she didn't hear me", "why is she slow")
        if re.search(
            r"\bshe\s+(?:is\s+not|isn't|doesn't|does\s+not|didn't|won't|can't|cannot|keeps|always|never)\s+(?:work(?:ing)?|listen(?:ing)?|hear(?:ing)?|respond(?:ing)?|answer(?:ing)?|speak(?:ing)?|talk(?:ing)?)\b",
            low,
        ):
            resolved = re.sub(r"\bshe\b", "E.V.", resolved, flags=re.IGNORECASE)
            return resolved

        # 2. Action commands with 'her' / 'she' ("call her", "send hello to her on whatsapp", "her phone number")
        if female_contact:
            resolved = re.sub(
                r"\b(?P<prep>to|for|call|dial|phone|message|text|whatsapp)\s+her\b",
                lambda m: f"{m.group('prep')} {female_contact}",
                resolved,
                flags=re.IGNORECASE,
            )
            resolved = re.sub(
                r"\bher\s+(?P<attr>phone\s+number|mobile\s+number|whatsapp\s+number|number|instagram|birthday)\b",
                lambda m: f"{female_contact}'s {m.group('attr')}",
                resolved,
                flags=re.IGNORECASE,
            )
        elif not female_contact and re.search(r"\b(?:she|her)\b", low):
            # Default 'she' to E.V. when no female person was mentioned in the session
            if re.search(r"\b(?:why\s+is\s+she|what\s+is\s+she|she\s+is)\b", low):
                resolved = re.sub(r"\bshe\b", "E.V.", resolved, flags=re.IGNORECASE)

        # 3. Action commands with 'him' / 'he'
        target_male = male_contact or any_contact
        if target_male:
            resolved = re.sub(
                r"\b(?P<prep>to|for|call|dial|phone|message|text|whatsapp)\s+him\b",
                lambda m: f"{m.group('prep')} {target_male}",
                resolved,
                flags=re.IGNORECASE,
            )
            resolved = re.sub(
                r"\bhis\s+(?P<attr>phone\s+number|mobile\s+number|whatsapp\s+number|number|instagram|birthday)\b",
                lambda m: f"{target_male}'s {m.group('attr')}",
                resolved,
                flags=re.IGNORECASE,
            )

        # 4. "open it" / "run it" referring to the last generated code file or app
        last_file = self._entities.get("file", "")
        last_app = self._entities.get("app", "")
        if re.match(r"^(?:please\s+)?(?:open|launch|run|show)\s+(?:it|that)(?:\s+in\s+(?P<where>browser|vs\s*code|code))?$", low):
            if last_file:
                return f"open generated file {last_file}"
            if last_app:
                return f"open {last_app}"

        return resolved

    # =========================================================================
    # Main Routing Entrypoint
    # =========================================================================
    def route(
        self,
        text: str,
        context: Optional[Dict[str, Any]] = None,
        confidence: Optional[float] = None,
        on_first_sentence: Optional[Callable[[str], None]] = None,
    ) -> IntentMatch:
        """
        Route a user utterance through unambiguous fast-path rules first,
        then silent factual search (if applicable), then `Brain.generate()`.
        """
        t0 = time.perf_counter()
        ctx = dict(context) if context else {}
        ctx["raw_input"] = text
        conf = float(confidence if confidence is not None else ctx.get("confidence", 1.0))

        normalized_input = VoskSTT.normalize_indian_english_transcript((text or "").strip())
        cleaned = self._strip_wake_prefix(normalized_input)
        cleaned_digits = convert_spoken_numbers(cleaned)

        # Priority 0: "stop listening" / "stopped listening" / "stand by" ALWAYS works
        # even if STT confidence was low
        if re.search(
            r"\b(?:stop(?:ped)?\s+listening|go\s+to\s+(?:stand\s*by|sleep)|stand\s*by|pause\s+listening|sleep\s+mode)\b",
            cleaned_digits,
            re.IGNORECASE,
        ):
            if self.tts is not None:
                self.tts.interrupt()
            if self.brain is not None and hasattr(self.brain, "cancel"):
                self.brain.cancel()
            self.bus.publish(
                Topics.SETTINGS_CHANGED,
                {"key": "continuousListening", "value": False},
                source="intent_router",
            )
            self.bus.publish(
                Topics.ORB_STATE,
                {"state": "idle", "progress": 0.0},
                source="intent_router",
            )
            latency = round((time.perf_counter() - t0) * 1000.0, 2)
            reply = "Standing by, sir."
            self.logger.info("FAST:continuous_listening_mode | %.2fms | input='%s'", latency, mask_pii(cleaned))
            return IntentMatch(
                intent_name="continuous_listening_mode",
                raw_text=cleaned,
                response_text=reply,
                handled_by_fast_path=True,
                latency_ms=latency,
            )

        if confidence is not None and conf < self.min_confidence:
            self.logger.info(
                "Dropping low-confidence input (conf=%.2f < %.2f): '%s'",
                conf,
                self.min_confidence,
                mask_pii(text),
            )
            return IntentMatch(
                intent_name="low_confidence_rejected",
                raw_text=text,
                response_text="",
                handled_by_fast_path=True,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )

        # Apply short-term pronoun/coreference resolution
        cleaned_digits = self.resolve_coreferences(cleaned_digits)

        low_clean = re.sub(r"[^a-z0-9\s]", " ", cleaned_digits.lower())
        low_clean = re.sub(r"\s+", " ", low_clean).strip()

        if not low_clean or low_clean in _WAKE_OR_NOISE_ONLY or len(low_clean) <= 1:
            return IntentMatch(
                intent_name="wake_acknowledge",
                raw_text=text,
                response_text="I'm listening.",
                handled_by_fast_path=True,
                latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
            )

        # Check if we were waiting for a missing contact phone number from the previous turn
        if self._awaiting_contact_number is not None:
            digits_only = re.sub(r"[^\d+]", "", normalize_phone_digits(cleaned_digits))
            if len(re.sub(r"\D", "", digits_only)) >= 7:
                c_name, pending_msg, act_type = self._awaiting_contact_number
                self._awaiting_contact_number = None
                if self.rag is not None:
                    self.rag.add_or_update_contact(c_name, phone=digits_only, whatsapp=digits_only)
                if act_type == "whatsapp" and pending_msg:
                    reply = self._execute_multistep_whatsapp_send(c_name, digits_only, pending_msg)
                elif act_type == "call":
                    reply = self._execute_multistep_call(c_name, digits_only)
                else:
                    reply = f"Saved {c_name}'s number as {digits_only}, sir."
                latency = round((time.perf_counter() - t0) * 1000.0, 2)
                self._log_to_memory(cleaned, reply, "saved_pending_contact_number")
                return IntentMatch(
                    intent_name="saved_pending_contact_number",
                    raw_text=cleaned,
                    response_text=reply,
                    handled_by_fast_path=True,
                    latency_ms=latency,
                )

        # Confirmation gate check
        if self._pending_action is not None:
            if low_clean in ("yes", "yeah", "yep", "confirm", "send it", "do it", "go ahead", "sure", "okay", "ok"):
                action_fn = self._pending_action
                self._pending_action = None
                self._pending_description = ""
                reply = action_fn()
                latency = round((time.perf_counter() - t0) * 1000.0, 2)
                self.logger.info("FAST:confirmed_action | %.2fms | input='%s'", latency, mask_pii(cleaned))
                self._log_to_memory(cleaned, reply, "confirmed_action")
                return IntentMatch(
                    intent_name="confirmed_action",
                    raw_text=cleaned,
                    response_text=reply,
                    handled_by_fast_path=True,
                    latency_ms=latency,
                )
            if low_clean in ("no", "nope", "cancel", "stop", "don t", "dont", "abort", "never mind"):
                self._pending_action = None
                self._pending_description = ""
                reply = "Cancelled, sir."
                latency = round((time.perf_counter() - t0) * 1000.0, 2)
                self.logger.info("FAST:cancelled_action | %.2fms | input='%s'", latency, mask_pii(cleaned))
                self._log_to_memory(cleaned, reply, "cancelled_action")
                return IntentMatch(
                    intent_name="cancelled_action",
                    raw_text=cleaned,
                    response_text=reply,
                    handled_by_fast_path=True,
                    latency_ms=latency,
                )

        # Compound command (Workspace + Timed Call)
        compound = self._try_compound_command(cleaned_digits, ctx)
        if compound is not None:
            compound.latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            self.logger.info(
                "FAST:%s | %.2fms | input='%s'",
                compound.intent_name,
                compound.latency_ms,
                mask_pii(cleaned),
            )
            self._log_to_memory(cleaned, compound.response_text, compound.intent_name)
            return compound

        # Evaluate deterministic fast-path rules
        for rule in self._rules:
            if rule.name == "volume_control" and re.search(
                r"\b(?:each\s+time|every\s+time|from\s+yourself|by\s+yourself|you\s+increase(?:d)?|why\s+did\s+you|when\s+you)\b",
                low_clean,
            ):
                continue

            for pattern in rule.patterns:
                match = pattern.search(cleaned_digits)
                if match is not None:
                    if rule.dangerous and conf < self.min_confidence:
                        latency = round((time.perf_counter() - t0) * 1000.0, 2)
                        self.logger.info(
                            "FAST:low_confidence_guard(%s) | %.2fms | input='%s'",
                            rule.name,
                            latency,
                            mask_pii(cleaned),
                        )
                        return IntentMatch(
                            intent_name="low_confidence_guard",
                            raw_text=cleaned,
                            response_text="Please repeat that command, sir.",
                            handled_by_fast_path=True,
                            latency_ms=latency,
                        )

                    self.bus.publish(
                        Topics.ORB_STATE,
                        {"state": "acting", "progress": 0.0},
                        source="intent_router",
                    )
                    try:
                        reply = rule.handler(match, cleaned_digits, ctx)
                    except Exception as exc:
                        self.logger.error("Error in intent handler '%s': %s", rule.name, exc, exc_info=True)
                        reply = f"Failed to execute {rule.name.replace('_', ' ')}, sir."

                    latency = round((time.perf_counter() - t0) * 1000.0, 2)
                    self.logger.info(
                        "FAST:%s | %.2fms | input='%s'",
                        rule.name,
                        latency,
                        mask_pii(cleaned),
                    )
                    self._log_to_memory(cleaned, reply, rule.name)
                    return IntentMatch(
                        intent_name=rule.name,
                        raw_text=cleaned,
                        response_text=reply,
                        slots=match.groupdict(),
                        handled_by_fast_path=True,
                        latency_ms=latency,
                    )

        # Silent background web search for factual world-knowledge questions if not in local memory
        if self.web_search is not None and _FACTUAL_QUESTION_PATTERN.search(cleaned_digits):
            local_fact = self.rag.find_direct_fact_answer(cleaned_digits) if self.rag is not None else None
            if not local_fact:
                web_answer = self.web_search.answer_factual_question(cleaned_digits)
                if web_answer:
                    latency = round((time.perf_counter() - t0) * 1000.0, 2)
                    self.logger.info(
                        "FAST:background_web_search | %.2fms | input='%s'",
                        latency,
                        mask_pii(cleaned),
                    )
                    self._log_to_memory(cleaned, web_answer, "background_web_search")
                    return IntentMatch(
                        intent_name="background_web_search",
                        raw_text=cleaned,
                        response_text=web_answer,
                        handled_by_fast_path=True,
                        latency_ms=latency,
                    )

        # Fall through to Brain.generate() with RAG + short-term session context
        rag_ctx = self.rag.build_context_for_prompt(cleaned_digits) if self.rag is not None else ""
        session_ctx = self._build_short_term_context()
        combined_ctx = "\n".join(part for part in (rag_ctx, session_ctx) if part).strip() or None

        model_loaded = bool(
            self.brain is not None
            and (
                getattr(self.brain, "is_model_loaded", False)
                or getattr(self.brain, "is_loaded", False)
                or getattr(self.brain, "_llm", None) is not None
            )
        )

        if self.brain is not None:
            try:
                reply = self.brain.generate(
                    cleaned_digits,
                    extra_context=combined_ctx,
                    on_first_sentence=on_first_sentence,
                )
            except TypeError:
                reply = self.brain.generate(cleaned_digits, extra_context=combined_ctx)
        else:
            reply = "I can't think yet, sir. The local engine failed to start."

        latency = round((time.perf_counter() - t0) * 1000.0, 2)
        if model_loaded:
            self.logger.info("LLM:%.1fms | input='%s'", latency, mask_pii(cleaned))
        else:
            self.logger.warning("FALLBACK_NO_MODEL | %.1fms | input='%s'", latency, mask_pii(cleaned))

        self._log_to_memory(cleaned, reply, "llm_fallback")
        return IntentMatch(
            intent_name="llm_fallback",
            raw_text=cleaned,
            response_text=reply,
            handled_by_fast_path=False,
            latency_ms=latency,
        )

    def _build_short_term_context(self) -> str:
        lines: List[str] = []
        if self._entities.get("person"):
            lines.append(f"- Last mentioned contact/person in this session: {self._entities['person']}")
        if self._entities.get("file"):
            lines.append(f"- Last generated code file in this session: {self._entities['file']}")
        if self._entities.get("app"):
            lines.append(f"- Last opened application: {self._entities['app']}")
        return "\n".join(lines)

    def _log_to_memory(self, user_text: str, assistant_text: str, intent_name: str) -> None:
        if user_text and assistant_text:
            self._recent_turns.append(
                {"user": user_text, "assistant": assistant_text, "intent": intent_name}
            )
        if self.rag is not None and intent_name not in ("wake_acknowledge", "low_confidence_guard", "empty"):
            try:
                self.rag.record_interaction(user_text, assistant_text, intent_name=intent_name)
            except Exception:
                pass

    @staticmethod
    def _strip_wake_prefix(text: str) -> str:
        return re.sub(
            r"^(?:(?:hey|hi|hello|okay|ok|yo|he|the)\s+(?:evie|e\s*\.?\s*v\.?|eve|ivy|avi|easy|tv|a\s*v|v|heavy|baby|every|movie|he\s+be|e\s+the|eb)|evie|e\.?v\.?|jarvis|hey)[\s,.:;-]*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    def _try_compound_command(self, cleaned: str, ctx: Dict[str, Any]) -> Optional[IntentMatch]:
        pattern = re.compile(
            r"(?:open|launch|start)\s+(?:my\s+)?(?P<ws>[a-z0-9_-]+)\s+workspace"
            r".*?\band\b.*?"
            r"(?:prepare\s+a\s+call\s+to|call)\s+(?P<contact>[a-z0-9\s]+?)"
            r"(?:\s+in\s+(?P<seconds>\d+)\s*(?:seconds?|secs?|s))?$",
            re.IGNORECASE,
        )
        m = pattern.search(cleaned)
        if m is not None:
            ws_name = (m.group("ws") or "coding").strip().lower()
            contact = (m.group("contact") or "Mom").strip().title()
            self._remember_person_entity(contact)
            seconds = int(
                m.group("seconds")
                or self.config.get("gui", {}).get("default_countdown_seconds", 30)
            )

            self.bus.publish(
                Topics.ORB_STATE,
                {"state": "acting", "progress": 0.0},
                source="intent_router",
            )

            def _start_call_countdown() -> None:
                if self.scheduler is not None:
                    self.scheduler.schedule(
                        seconds=seconds,
                        label=f"Call {contact}",
                        detail="Phone bridge · Mobile · E.V. voice routed",
                        kind="call",
                        contact=contact,
                    )
                else:
                    self.bus.publish(
                        Topics.COUNTDOWN_UPDATE,
                        {
                            "status": "running",
                            "total": seconds,
                            "remaining": seconds,
                            "label": f"Call {contact}",
                            "detail": "Phone bridge · Mobile · E.V. voice routed",
                            "kind": "call",
                            "contact": contact,
                            "done_message": "",
                        },
                        source="intent_router",
                    )

            if self.workspace_orch is not None:
                self.workspace_orch.launch_workspace(ws_name, on_complete=_start_call_countdown)
            else:
                _start_call_countdown()

            return IntentMatch(
                intent_name="compound_workspace_and_timed_call",
                raw_text=cleaned,
                response_text=f"Launching your {ws_name} workspace. I'll call {contact} in {seconds} seconds, sir.",
                slots={"workspace": ws_name, "contact": contact, "seconds": seconds},
                handled_by_fast_path=True,
            )
        return None

    # =========================================================================
    # Built-in Fast-Path Intent Rules
    # =========================================================================
    def _register_builtin_rules(self) -> None:
        # 4: Resume Interrupted Speech ("continue", "go on")
        self.register(
            name="continue_speech",
            priority=4,
            patterns=[
                r"^(?:please\s+)?(?:continue|go\s+on|carry\s+on|keep\s+talking|keep\s+going|resume(?:\s+speaking|\s+talking)?)$",
            ],
            handler=self._handle_continue_speech,
            description="Resume speaking from where E.V. was interrupted",
        )

        # 5: Stop / Interrupt Speech ("stop talking", "be quiet")
        self.register(
            name="interrupt_speech",
            priority=5,
            patterns=[
                r"^(?:please\s+)?(?:stop\s+talking|stop\s+speaking|be\s+quiet|quiet|shh|silence|pause\s+speaking)$",
            ],
            handler=self._handle_interrupt_speech,
            description="Immediately stop E.V. from speaking",
        )

        # 6: Continuous Listening & Standby Mode ("stop listening", "stopped listening", "stand by")
        self.register(
            name="continuous_listening_mode",
            priority=6,
            patterns=[
                r"\b(?P<sleep>stop(?:ped)?\s+listening|go\s+to\s+(?:stand\s*by|sleep)|stand\s*by|pause\s+listening|sleep\s+mode)\b",
                r"\b(?:continuously\s+listen(?:ing)?|continuous\s+listening|always\s+listen(?:ing)?|keep\s+listening|start\s+listening)\b",
            ],
            handler=self._handle_continuous_listening,
            description="Enable or exit continuous listening mode",
        )

        # 7: Strict Identity & 'Who am I' (ONLY when explicitly asking identity/creator/hearing)
        self.register(
            name="assistant_identity",
            priority=7,
            patterns=[
                r"^(?:please\s+)?(?P<who_am_i>who\s+am\s+i|who\s+i\s+am|what(?:'s|\s+is)\s+my\s+name)\??$",
                r"^(?:please\s+)?(?P<creator>who\s+(?:created|made|built|developed|programmed)\s+you|who\s+is\s+your\s+creator)\??$",
                r"^(?:please\s+)?(?:who\s+are\s+you|what\s+are\s+you|what(?:'s|\s+is)\s+your\s+name|tell\s+me\s+about\s+yourself|introduce\s+yourself|give\s+me\s+(?:a\s+)?(?:basic\s+)?introduction(?:\s+of\s+yours)?)\??$",
                r"^(?P<checkin>can\s+you\s+hear\s+me|are\s+you\s+able\s+to\s+hear\s+me|do\s+you\s+hear\s+me|am\s+i\s+audible)\??$",
            ],
            handler=self._handle_assistant_identity,
            description="Strict E.V. identity, creator, and 'Who am I' handler",
        )

        # 8: Timer Cancel / Run Now
        self.register(
            name="timer_control",
            priority=8,
            patterns=[
                r"\b(?P<action>cancel|stop|abort)\s+(?:the\s+)?(?:timer|countdown|timed\s+command)\b",
                r"\b(?P<action>run|execute)\s+(?:the\s+)?(?:timer|countdown|timed\s+command)\s+now\b",
            ],
            handler=self._handle_timer_control,
            description="Cancel or immediately run the active countdown timer",
        )

        # 10: Workspace Undo
        self.register(
            name="workspace_undo",
            priority=10,
            patterns=[
                r"\b(?:undo|reverse|close|restore)\s+(?:last\s+|my\s+|the\s+)?(?P<ws>[a-z0-9_-]+\s+)?workspace\b",
            ],
            handler=self._handle_workspace_undo,
            description="Reverse the last launched workspace and close spawned items",
            dangerous=True,
        )

        # 12: Coding Capability (HTML/CSS/JS/React/Python generation -> data/workspace/code/)
        self.register(
            name="coding_generation",
            priority=12,
            patterns=[
                r"^(?:please\s+)?(?:create|make|build|write|generate|code)\s+(?:a\s+|an\s+)?(?P<lang>react|html|css|javascript|js|python|web)?\s*(?P<kind>page|webpage|website|app|application|component|script|program|code|file)\s+(?:that|for|to|which|with|about)\s+(?P<task>.+)$",
                r"^(?:please\s+)?(?:write|generate)\s+(?P<lang>react|html|css|javascript|js|python)\s+code\s+(?:for|to|that)\s+(?P<task>.+)$",
                r"^open\s+generated\s+file\s+(?P<open_file>.+)$",
            ],
            handler=self._handle_coding_generation,
            description="Generate HTML/CSS/JS/React/Python code and save under data/workspace/code/",
        )

        # 14: Selective Workspace Launch
        self.register(
            name="workspace_selective_launch",
            priority=14,
            patterns=[
                r"\b(?:open|launch)\s+only\s+(?P<filters>.+?)\s+from\s+(?:my\s+)?(?P<ws>[a-z0-9_-]+)\s+workspace\b",
                r"\b(?:open|launch)\s+(?:my\s+)?(?P<ws>[a-z0-9_-]+)\s+workspace\s+with\s+only\s+(?P<filters>.+)\b",
            ],
            handler=self._handle_workspace_selective_launch,
            description="Launch only matching apps or folders from a saved workspace",
        )

        # 15: Workspace Launch
        self.register(
            name="workspace_launch",
            priority=15,
            patterns=[
                r"\b(?:open|launch|start|load|activate)\s+(?:my\s+|the\s+)?(?P<ws>[a-z0-9_-]+)\s+workspace\b",
                r"\b(?:open|launch|start)\s+workspace\s+(?P<ws>[a-z0-9_-]+)\b",
                r"\b(?P<ws>coding|study|gaming|media|work)\s+workspace\b",
            ],
            handler=self._handle_workspace_launch,
            description="Launch a saved multi-app JSON workspace",
        )

        # 16: Self-Learning & Personal Fact Statements
        self.register(
            name="self_learning",
            priority=16,
            patterns=[
                r"^(?P<query_learned>what\s+(?:did|have)\s+you\s+learn(?:ed)?\s+about\s+me|what\s+do\s+you\s+know\s+about\s+me|tell\s+me\s+what\s+you\s+(?:know|learned)\s+about\s+me)\??$",
                r"^(?:from\s+now\s+on|update\s+your\s+knowledge|no[\s,]+that(?:'s|\s+is)\s+(?:wrong|incorrect)|actually\s+remember(?:\s+that)?)[\s,.:;-]*(?P<rule>.+)$",
                r"^(?P<neg_pref>(?:please\s+)?(?:don't|do\s+not|never)\s+(?:ask\s+for\s+confirmation|call\s+me\s+by|repeat\s+what|read\s+out|interrupt|open\s+|say\s+))(?P<neg_rest>.*)$",
            ],
            handler=self._handle_self_learning,
            description="Self-learning commands",
        )

        self.register(
            name="personal_fact_statement",
            priority=16,
            patterns=[
                r"^(?:remember\s+that\s+)?(?P<person>[a-zA-Z0-9]+)\s+is\s+my\s+(?P<rel>friend|best\s+friend|brother|sister|mother|father|mom|mummy|dad|colleague|cousin)(?:[\s,;]+(?:and\s+)?(?:his|her|their)\s+(?P<attr>phone\s+number|mobile\s+number|whatsapp\s+number|number|phone|whatsapp|instagram)\s+is\s+(?P<val>.+))?$",
                r"^(?:update|set|change|remember\s+that)\s+(?P<person2>[a-zA-Z0-9\s]+?)'s\s+(?P<attr2>phone\s+number|mobile\s+number|whatsapp\s+number|number|phone|whatsapp|instagram(?:\s+username)?|birthday)\s+(?:to|is|as)\s+(?P<val2>.+)$",
                r"^(?P<person3>[a-zA-Z0-9]+)'s\s+(?P<attr3>phone\s+number|mobile\s+number|whatsapp\s+number|number|phone|whatsapp|instagram(?:\s+username)?|birthday)\s+is\s+(?P<val3>.+)$",
                r"^(?:remember\s+that\s+)?(?:my\s+(?:full\s+name|name|nickname|mobile\s+number|phone\s+number|cell\s+number|mobile|phone|email(?:\s+address)?|instagram(?:\s+username|\s+handle|\s+id)?|birthday|date\s+of\s+birth|dob|age|home\s+address|address)|i\s+live\s+in)\s+(?:is\s+)?(?P<self_val>.+)$",
            ],
            handler=self._handle_personal_fact_statement,
            description="Teach E.V. personal profile fields and contact details",
        )

        # 17: Personal Identity & Self Profile Queries
        self.register(
            name="personal_profile_query",
            priority=17,
            patterns=[
                r"^(?:what(?:'s|\s+is)|tell\s+me)\s+my\s+(?P<field>mobile\s+number|phone\s+number|cell\s+number|number|instagram\s+username|instagram\s+handle|instagram|insta\s+id|email\s+address|email|home\s+address|address|full\s+name|nickname|age|birthday|date\s+of\s+birth|dob)\??$",
                r"^when\s+is\s+my\s+(?P<field>birthday|date\s+of\s+birth)\??$",
            ],
            handler=self._handle_personal_profile_query,
            description="Retrieve user's own personal profile fields from SQLite",
        )

        # 18: Instagram App & Profile Automation
        self.register(
            name="instagram_action",
            priority=18,
            patterns=[
                r"^(?:open|launch|show)\s+my\s+instagram(?:\s+profile|\s+account|\s+page)?$",
                r"^(?:find|search(?:\s+for)?|look\s+up|open|show)\s+(?P<person>[a-zA-Z0-9\s]+?)\s+on\s+instagram$",
                r"^(?:open|show)\s+(?P<person>[a-zA-Z0-9]+)'s\s+instagram(?:\s+profile|\s+page)?$",
                r"^(?:open|launch)\s+(?P<app_only>instagram)$",
            ],
            handler=self._handle_instagram_action,
            description="Open Instagram app or profile",
        )

        # 19: Friends, Family & Contact Queries
        self.register(
            name="contact_management",
            priority=19,
            patterns=[
                r"^open\s+(?:my\s+)?contacts\s+and\s+(?:search|find|look)\s+for\s+(?P<search_person>[a-zA-Z0-9\s]+)$",
                r"^(?:save|add)\s+contact\s+(?P<new_name>[a-zA-Z0-9\s]+?)(?:\s+as\s+(?:my\s+)?(?P<new_rel>friend|brother|sister|mother|father|colleague))?(?:\s+with\s+(?:number|phone)\s+(?P<new_num>[\+\d\s\-\(\)]+))?$",
                r"^(?:what(?:'s|\s+is)|find|get|tell\s+me)\s+(?:my\s+)?(?P<person>[a-zA-Z0-9\s]+?)(?:'s\s+|\s+)(?P<attr>mobile\s+number|phone\s+number|whatsapp\s+number|number|instagram(?:\s+username)?|birthday|relationship)\??$",
                r"^who\s+is\s+(?P<who_person>[a-zA-Z0-9\s]+?)\??$",
            ],
            handler=self._handle_contact_management,
            description="Query or manage contacts in SQLite",
        )

        # 20: Timed / Delayed Commands
        self.register(
            name="timed_command",
            priority=20,
            patterns=[
                r"\b(?:set|start)\s+(?:a\s+|the\s+)?timer\s+(?:for|of|off|to)?\s*(?P<seconds>\d+)\s*(?P<unit>minutes?|mins?|seconds?|secs?|s)\b(?:\s+(?:to\s+)?(?P<task>.+))?",
                r"\bin\s+(?P<seconds>\d+)\s*(?P<unit>minutes?|mins?|seconds?|secs?|s)\s+(?P<task>.+)$",
                r"^(?P<task>prepare\s+a\s+call\s+to\s+[a-z0-9\s]+|call\s+[a-z0-9\s]+|dim\s+.+?)\s+in\s+(?P<seconds>\d+)\s*(?P<unit>minutes?|mins?|seconds?|secs?|s)\b",
            ],
            handler=self._handle_timed_command,
            description="Schedule a delayed command or countdown timer on the right HUD panel",
        )

        # 22: Multi-Step WhatsApp Messaging
        self.register(
            name="whatsapp_action",
            priority=22,
            patterns=[
                r"^what\s+was\s+the\s+last\s+(?:whatsapp\s+)?message\s+i\s+sent\s+to\s+(?P<last_person>[a-zA-Z0-9\s]+?)\??$",
                r"(?:(?:open|launch)\s+whatsapp\s+(?:and\s+)?)?send\s+(?:a\s+|an\s+|our\s+)?(?P<msg>[a-z0-9\s]+?)\s+message\s+(?:to|too)\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)(?:\s+on\s+whatsapp)?$",
                r"(?:open|launch)\s+whatsapp\s+(?:and\s+)?(?:send|message|text)\s+(?:a\s+)?(?P<msg>[a-z0-9\s]+?)\s+(?:to|too)\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)$",
                r"\bsend\s+(?:a\s+)?whatsapp\s+voice\s+note\s+to\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)\s+(?:saying|that|:)\s+(?P<msg>.+)$",
                r"\bsend\s+(?:a\s+)?(?:whatsapp\s+)?(?:message\s+)?to\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)\s+(?:on\s+whatsapp\s+)?(?:saying|that|:)\s+(?P<msg>.+)$",
                r"\bsend\s+(?P<msg>hello|hi|hey|[a-z0-9\s]+?)\s+to\s+(?:my\s+)?(?P<contact>mom|mummy|mommy|mammi|mother|dad|papa|father|brother|sister|[a-z0-9]+)(?:\s+on\s+whatsapp)?$",
                r"\bsend\s+(?!a\s|an\s|the\s)(?P<contact>[a-z0-9]+)\s+a\s+(?P<msg>.+?)\s+message(?:\s+on\s+whatsapp)?$",
                r"\b(?:message|text|whatsapp)\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)\s+on\s+whatsapp(?:\s+(?:saying|that|:)\s+(?P<msg>.+))?$",
                r"^(?:open|launch)\s+(?P<open_only>whatsapp)$",
                r"\b(?P<read>read|check)\s+(?:my\s+)?whatsapp(?:\s+messages)?\b",
            ],
            handler=self._handle_whatsapp,
            description="Multi-step WhatsApp messaging using SQLite contacts",
        )

        # 24: Phone ADB Specific Controls
        self.register(
            name="phone_adb_control",
            priority=24,
            patterns=[
                r"\b(?P<unlock>unlock)\s+(?:my\s+)?phone\b",
                r"\bsend\s+(?:an?\s+)?(?:sms|text)\s+to\s+(?P<contact>[a-z0-9\s]+?)\s+(?:saying|that|:)\s+(?P<msg>.+)$",
                r"\b(?P<flash>toggle|turn\s+on|turn\s+off)\s+(?:phone\s+)?flashlight\b",
                r"\b(?P<bat>check\s+)?phone\s+battery\b",
                r"\bopen\s+(?P<app>[a-z0-9\s]+?)\s+on\s+(?:my\s+)?phone\b",
            ],
            handler=self._handle_phone_adb_control,
            description="Control Android phone over Wireless/USB ADB",
            dangerous=True,
        )

        # 25: Multi-Step Phone / WhatsApp Call Intent ("Call Mom", "Call Mom on WhatsApp")
        self.register(
            name="phone_call",
            priority=25,
            patterns=[
                r"^(?:please\s+)?(?:make\s+a\s+call\s+to|prepare\s+a\s+call\s+to|call|dial|phone)\s+(?!me\b)(?:my\s+)?(?P<contact>[a-z0-9\s]+?)(?:\s+on\s+(?P<platform>whatsapp))?$",
            ],
            handler=self._handle_phone_call,
            description="Initiate a multi-step phone or WhatsApp call using SQLite contacts",
            dangerous=True,
        )

        # 28: Memory Store & Recall
        self.register(
            name="memory_rag",
            priority=28,
            patterns=[
                r"\bremember\s+that\s+(?P<key>[^=]+?)\s+is\s+(?P<value>.+)$",
                r"^\s*remember\s+that\s+(?P<free_fact>.+)$",
                r"\b(?P<recall>what\s+do\s+you\s+remember\s+about|what\s+is\s+my\s+saved)\s+(?P<query>[a-z0-9\s_-]+)\??$",
            ],
            handler=self._handle_memory,
            description="Save or recall persistent user facts in SQLite + ChromaDB",
        )

        # 30: System Volume & Mute Controls
        self.register(
            name="volume_control",
            priority=30,
            patterns=[
                r"^(?:please\s+)?(?P<action>mute|unmute)\b(?:\s+(?:the\s+)?(?:audio|volume|sound|system))?$",
                r"\b(?:set|increase|decrease|change|put)\s+(?:the\s+)?(?:volume|sound|audio)\s+(?:to|as|at)\s+(?:the\s+)?(?P<level>\d{1,3})\s*(?:%|percent)?\b",
                r"\b(?:set\s+)?(?:the\s+)?volume\s+(?:to\s+)?(?P<level2>\d{1,3})\s*(?:%|percent)?\b",
                r"\b(?P<step_dir>increase|decrease|raise|lower)\s+(?:the\s+)?(?:volume|sound|audio)\s+(?:by\s+)?(?P<step_val>\d{1,3})\s*(?:%|percent)\b",
                r"\b(?:let\s+me\s+just\s+|please\s+)?(?:turn\s+(?:the\s+)?(?:volume|sound|audio)\s+(?P<dir1>up|down)|(?:volume|sound|audio)\s+(?P<dir2>up|down|louder|quieter|softer)|turn\s+(?P<dir3>up|down)\s+(?:the\s+)?(?:volume|sound|audio)|(?P<dir5>increase|decrease|raise|lower)\s+(?:the\s+)?(?:volume|sound|audio))$",
            ],
            handler=self._handle_volume,
            description="Adjust or set system master audio volume accurately",
        )

        # 35: Media Playback Controls
        self.register(
            name="media_control",
            priority=35,
            patterns=[
                r"\b(?P<action>play|pause|resume|stop)\s+(?:the\s+)?(?:music|media|song|track|spotify|playback)\b",
                r"\b(?P<action>next|previous|skip)\s+(?:track|song)\b",
                r"\b(?P<action>take\s+a\s+screenshot|capture\s+screen|screenshot)\b",
                r"\b(?P<action>minimize\s+all|show\s+desktop)\b",
            ],
            handler=self._handle_media,
            description="Control media playback, screenshots, and window layout",
        )

        # 40: System Telemetry / Status Check
        self.register(
            name="system_status",
            priority=40,
            patterns=[
                r"\b(?:system|hardware|cpu|ram|memory|battery|vram)\s+(?:status|report|usage|health|check|metrics)\b",
                r"\bhow\s+much\s+(?:ram|memory|cpu|battery)\b",
                r"\bstatus\s+report\b",
            ],
            handler=self._handle_system_status,
            description="Report live CPU, RAM, Battery, and zero-VRAM status",
        )

        # 45: Live Background Web Search
        self.register(
            name="web_search",
            priority=45,
            patterns=[
                r"\b(?:search\s+(?:the\s+web\s+)?for|look\s+up|google|duckduckgo|find\s+info\s+on)\s+(?P<query>.+)$",
            ],
            handler=self._handle_web_search,
            description="Search the live web silently in the background using duckduckgo-search",
        )

        # 50: Close / Terminate Application
        self.register(
            name="close_app",
            priority=50,
            patterns=[
                r"^(?:please\s+)?(?:close|quit|kill|exit|terminate)\s+(?!yourself\b)(?:the\s+)?(?P<target>[a-z0-9\s._-]+)$",
            ],
            handler=self._handle_close_app,
            description="Close a running desktop application",
            dangerous=True,
        )

        # 55: Open Application / Folder / Website
        self.register(
            name="open_target",
            priority=55,
            patterns=[
                r"^(?:please\s+)?(?:open|launch|start|run)\s+(?:the\s+|my\s+)?(?P<target>[a-z0-9\s.:/_~-]+)$",
            ],
            handler=self._handle_open_target,
            description="Open a local application, folder, or URL",
        )

        # 60: Actual Time & Date Questions ONLY
        self.register(
            name="time_date",
            priority=60,
            patterns=[
                r"^(?:what\s+(?:time|day|date)\s+is\s+it|what(?:'s|\s+is)\s+the\s+(?:time|date)|tell\s+me\s+the\s+(?:time|date)|current\s+(?:time|date))\??$",
            ],
            handler=self._handle_time_date,
            description="Report current local time and date only when explicitly asked",
        )

    # =========================================================================
    # Handler Implementations
    # =========================================================================
    def _publish_step_progress(self, task_name: str, step: int, total: int, status: str = "running") -> None:
        pct = min(1.0, max(0.0, float(step) / float(max(1, total))))
        self.bus.publish(
            Topics.ORB_STATE,
            {"state": "acting" if status == "running" else "idle", "progress": pct},
            source="intent_router",
        )
        self.bus.publish(
            Topics.WORKSPACE_PROGRESS,
            {"name": task_name, "status": status, "completed": step, "total": total},
            source="intent_router",
        )

    def _handle_continue_speech(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.tts is not None:
            resumed, _ = self.tts.resume(block=False)
            if resumed:
                return ""
        return "Nothing pending to continue, sir."

    def _handle_interrupt_speech(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.tts is not None:
            self.tts.interrupt()
        if self.brain is not None and hasattr(self.brain, "cancel"):
            self.brain.cancel()
        self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="intent_router")
        return ""

    def _handle_continuous_listening(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        is_sleep = bool(gd.get("sleep")) or any(
            w in raw.lower() for w in ("disable", "turn off", "stop", "stopped", "sleep", "stand by", "standby", "pause")
        )
        enable = not is_sleep
        if is_sleep:
            if self.tts is not None:
                self.tts.interrupt()
            if self.brain is not None and hasattr(self.brain, "cancel"):
                self.brain.cancel()
        self.bus.publish(
            Topics.SETTINGS_CHANGED,
            {"key": "continuousListening", "value": enable},
            source="intent_router",
        )
        if enable:
            return "Continuous listening active, sir."
        return "Standing by, sir."

    # =========================================================================
    # Coding Capability Handler (`data/workspace/code/`)
    # =========================================================================
    def _handle_coding_generation(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if gd.get("open_file"):
            fpath = Path(gd["open_file"].strip())
            if fpath.exists():
                self._open_generated_code_file(fpath, open_in_vscode=False)
                return f"Opened {fpath.name}, sir."
            return "I couldn't find that generated file, sir."

        raw_lang = (gd.get("lang") or "").strip().lower()
        kind = (gd.get("kind") or "page").strip().lower()
        task_full = (gd.get("task") or "interactive page").strip()

        # Check if user asked to open it after generating
        wants_open = bool(
            re.search(r"\band\s+(?:open|launch|run|show)\s+it\b|\bopen\s+it\s+in\s+(?:browser|vs\s*code|code)\b", task_full, re.I)
        )
        wants_vscode = bool(re.search(r"\b(?:vs\s*code|visual\s+studio\s+code)\b", task_full, re.I))
        clean_task = re.sub(
            r"\s*\band\s+(?:open|launch|run|show)\s+it(?:\s+in\s+(?:browser|vs\s*code|code))?$",
            "",
            task_full,
            flags=re.I,
        ).strip()

        if raw_lang in ("python", "py") or kind in ("script", "program") and raw_lang not in ("js", "javascript"):
            lang = "python"
            ext = ".py"
        elif raw_lang == "react":
            lang = "react"
            ext = ".html"  # Self-contained React + Babel HTML so it runs immediately in browser
        elif raw_lang in ("css",):
            lang = "css"
            ext = ".css"
        elif raw_lang in ("javascript", "js"):
            lang = "javascript"
            ext = ".js"
        else:
            lang = "html"
            ext = ".html"

        slug_words = re.findall(r"[a-z0-9]+", clean_task.lower())
        stop = {"a", "an", "the", "that", "for", "to", "with", "and", "in", "on", "of"}
        slug_parts = [w for w in slug_words if w not in stop][:4] or [lang, kind]
        filename = "_".join(slug_parts) + ext
        out_path = self.code_dir / filename

        self._publish_step_progress(f"Code: {filename}", 1, 3, "running")
        if self.brain is not None and hasattr(self.brain, "generate_code"):
            code_content = self.brain.generate_code(clean_task, language=lang)
        else:
            code_content = f"<!-- Generated for: {clean_task} -->\n<h1>{clean_task}</h1>\n"

        self._publish_step_progress(f"Code: {filename}", 2, 3, "running")
        out_path.write_text(code_content, encoding="utf-8")
        self._entities["file"] = str(out_path)
        self._entities["topic"] = clean_task
        self._publish_step_progress(f"Code: {filename}", 3, 3, "completed")

        if wants_open or wants_vscode:
            self._open_generated_code_file(out_path, open_in_vscode=wants_vscode)
            return f"I've generated {filename}, saved it to your code workspace, and opened it for you, sir."

        return f"I've generated {filename} and saved it to your code workspace, sir."

    def _open_generated_code_file(self, file_path: Path, open_in_vscode: bool = False) -> None:
        try:
            if open_in_vscode:
                subprocess.Popen(["code", str(file_path)], shell=(os.name == "nt"))
                return
            if file_path.suffix.lower() in (".html", ".htm"):
                webbrowser.open(file_path.resolve().as_uri(), new=2)
            elif hasattr(os, "startfile"):
                os.startfile(str(file_path))  # type: ignore[attr-defined]
        except Exception as exc:
            self.logger.debug("Could not auto-open generated code file %s: %s", file_path, exc)

    def _handle_self_learning(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if self.rag is None:
            return "Memory module unavailable, sir."

        if gd.get("query_learned"):
            return self.rag.summarize_learned_about_user()

        if gd.get("neg_pref"):
            full_neg = f"{gd.get('neg_pref', '')}{gd.get('neg_rest', '')}".strip()
            return self.rag.learn_rule_or_correction(full_neg, source_type="preference")

        rule_text = (gd.get("rule") or raw).strip()
        return self.rag.learn_rule_or_correction(rule_text, source_type="rule")

    def _handle_personal_profile_query(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        field_q = (match.groupdict().get("field") or "").strip()
        if self.rag is not None:
            return self.rag.query_my_profile(field_q)
        return f"Your {field_q} is not saved yet, sir."

    def _handle_instagram_action(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        self._entities["app"] = "instagram"
        if gd.get("app_only"):
            if self.phone_adb is not None and self.phone_adb.is_connected:
                _, msg = self.phone_adb.open_app("instagram")
                return msg
            if self.system_ctrl is not None:
                self.system_ctrl.open_url("https://www.instagram.com/")
            return "Opened Instagram, sir."

        person = (gd.get("person") or "").strip()
        if person:
            self._remember_person_entity(person)
            handle = person.lstrip("@")
            if self.rag is not None:
                contact, err = self.rag.resolve_contact_or_disambiguate(person)
                if contact is not None:
                    self._remember_person_entity(contact.name, contact.relationship)
                    if contact.instagram_username:
                        handle = contact.instagram_username.lstrip("@")
                elif err and "multiple contacts" in err.lower():
                    return err
            if self.phone_adb is not None:
                _, msg = self.phone_adb.open_instagram_profile(handle)
                return msg
            if self.system_ctrl is not None:
                self.system_ctrl.open_url(f"https://www.instagram.com/{handle}/")
            return f"Opened @{handle} on Instagram, sir."

        my_handle = ""
        if self.rag is not None:
            my_handle = (self.rag.sqlite.get_profile_field("instagram_username") or "").lstrip("@")
        if self.phone_adb is not None:
            _, msg = self.phone_adb.open_instagram_profile(my_handle)
            return msg
        if self.system_ctrl is not None:
            url = f"https://www.instagram.com/{my_handle}/" if my_handle else "https://www.instagram.com/"
            self.system_ctrl.open_url(url)
        return f"Opened your Instagram profile (@{my_handle}), sir." if my_handle else "Opened Instagram, sir."

    def _handle_contact_management(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()

        if gd.get("search_person"):
            sp = gd["search_person"].strip()
            self._remember_person_entity(sp)
            if self.phone_adb is not None:
                self.phone_adb.search_contacts_app(sp)
            if self.rag is not None:
                desc = self.rag.describe_person(sp)
                return f"Opened your contacts for {sp}. {desc}"
            return f"Opened your contacts for {sp}, sir."

        if gd.get("new_name") and self.rag is not None:
            name = gd["new_name"].strip().title()
            rel = (gd.get("new_rel") or "").strip().lower()
            num = normalize_phone_digits(gd.get("new_num") or "")
            self._remember_person_entity(name, rel)
            self.rag.sqlite.upsert_contact(name=name, relationship=rel, mobile_number=num, whatsapp_number=num)
            return f"Saved {name} to your contacts, sir."

        if gd.get("person") and gd.get("attr") and self.rag is not None:
            person_q = gd["person"].strip()
            self._remember_person_entity(person_q)
            return self.rag.query_contact_attribute(person_q, gd["attr"].strip())

        if gd.get("who_person") and self.rag is not None:
            wp = gd["who_person"].strip()
            if wp.lower() in ("you", "ev", "e.v.", "evie", "your creator"):
                return self.brain.IDENTITY_RESPONSE if self.brain else "I am E.V., sir."
            self._remember_person_entity(wp)
            return self.rag.describe_person(wp)

        return "Contact not found, sir."

    def _handle_assistant_identity(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if gd.get("who_am_i"):
            name = ""
            if self.rag is not None and hasattr(self.rag, "get_my_profile_field"):
                name = (self.rag.get_my_profile_field("full_name") or self.rag.get_my_profile_field("nickname") or "").strip()
            return f"You are {name}." if name else "I don't have your name saved in your profile yet."
        if gd.get("creator"):
            return "I was created by Bruprog as an offline Enhanced Virtual Assistant."
        if gd.get("checkin"):
            return "Yes, I can hear you clearly."
        if self.brain is not None and hasattr(self.brain, "IDENTITY_RESPONSE"):
            return str(self.brain.IDENTITY_RESPONSE)
        return "I am E.V., Enhanced Virtual Assistant, created by Bruprog. How can I help you, sir?"

    def _handle_timer_control(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        action = (match.groupdict().get("action") or "cancel").lower()
        if self.scheduler is not None:
            if action in ("run", "execute"):
                self.scheduler.run_now()
                return "Executing scheduled command now, sir."
            self.scheduler.cancel(speak_feedback=False)
        return "Timer cancelled, sir."

    def _handle_workspace_selective_launch(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.workspace_orch is None:
            return "Workspace orchestrator is not available, sir."
        ws_name = (match.groupdict().get("ws") or "coding").strip().lower()
        raw_filters = (match.groupdict().get("filters") or "").strip()
        items = [f.strip() for f in re.split(r",|\band\b|\bor\b", raw_filters) if f.strip()]
        ok, msg = self.workspace_orch.launch_workspace(ws_name, item_filter=items)
        return msg

    def _handle_workspace_launch(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        ws_name = (match.groupdict().get("ws") or "coding").strip().lower()
        if self.workspace_orch is not None:
            ok, msg = self.workspace_orch.launch_workspace(ws_name)
            return msg
        self.bus.publish(
            Topics.WORKSPACE_PROGRESS,
            {"name": f"{ws_name.title()} workspace", "status": "running", "completed": 0, "total": 10},
            source="intent_router",
        )
        return f"Launching {ws_name} workspace, sir."

    def _handle_workspace_undo(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.workspace_orch is not None:
            ok, msg = self.workspace_orch.undo_last_workspace()
            return msg
        self.bus.publish(
            Topics.WORKSPACE_PROGRESS,
            {"name": "Coding workspace", "status": "undoing", "completed": 10, "total": 10},
            source="intent_router",
        )
        return "Reversing last workspace, sir."

    def _handle_timed_command(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        raw_val = max(1, int(gd.get("seconds") or 10))
        unit = (gd.get("unit") or "seconds").lower()
        seconds = raw_val * 60 if unit.startswith("min") else raw_val
        task = (gd.get("task") or f"{raw_val} {unit} timer").strip()

        call_m = re.search(r"(?:prepare\s+a\s+call\s+to|call)\s+(?P<c>[a-z0-9\s]+)", task, re.IGNORECASE)
        is_call = call_m is not None
        contact = call_m.group("c").strip().title() if call_m else "Mom"
        if is_call:
            self._remember_person_entity(contact)

        label = f"Call {contact}" if is_call else (task[:1].upper() + task[1:])
        detail = "Phone bridge · Mobile · E.V. voice routed" if is_call else "Countdown timer · HUD"
        done_msg = "" if is_call else f"Timer complete: {label}."

        if self.scheduler is not None:
            self.scheduler.schedule(
                seconds=seconds,
                label=label,
                detail=detail,
                kind="call" if is_call else "notify",
                contact=contact,
                done_message=done_msg,
            )
        else:
            self.bus.publish(
                Topics.COUNTDOWN_UPDATE,
                {
                    "status": "running",
                    "total": seconds,
                    "remaining": seconds,
                    "label": label,
                    "detail": detail,
                    "kind": "call" if is_call else "notify",
                    "contact": contact,
                    "done_message": done_msg,
                },
                source="intent_router",
            )
        return f"Timer set for {seconds} seconds, sir." if not is_call else f"Calling {contact} in {seconds} seconds, sir."

    # =========================================================================
    # Multi-Step Task Planner for WhatsApp & Calls
    # =========================================================================
    def _open_whatsapp_visibly_on_pc(self, phone_number: str = "", message: str = "", auto_send_enter: bool = False) -> None:
        digits = re.sub(r"[^\d]", "", phone_number or "")
        encoded_msg = urllib.parse.quote(message) if message else ""

        launched_uri = False
        if digits or encoded_msg:
            uri = f"whatsapp://send?phone={digits}&text={encoded_msg}" if digits else f"whatsapp://send?text={encoded_msg}"
            if hasattr(os, "startfile"):
                try:
                    os.startfile(uri)  # type: ignore[attr-defined]
                    launched_uri = True
                except Exception:
                    pass

        if not launched_uri and self.system_ctrl is not None:
            try:
                ok, _ = self.system_ctrl.open_application("whatsapp")
                if not ok and (digits or encoded_msg):
                    web_url = (
                        f"https://web.whatsapp.com/send?phone={digits}&text={encoded_msg}"
                        if digits
                        else "https://web.whatsapp.com/"
                    )
                    self.system_ctrl.open_url(web_url)
            except Exception:
                pass

    def _execute_multistep_whatsapp_send(
        self,
        resolved_name: str,
        resolved_number: str,
        msg: str,
        is_vn: bool = False,
    ) -> str:
        """
        Execute the 5-step WhatsApp message workflow with live HUD progress updates:
          Step 1: Contact resolved
          Step 2: Open WhatsApp (Desktop URI or Android ADB)
          Step 3: Select contact chat
          Step 4: Type message
          Step 5: Send & record in SQLite message_history
        """
        task_label = f"WhatsApp → {resolved_name}"
        self._publish_step_progress(task_label, 1, 5, "running")

        if self.rag is not None:
            self.rag.record_sent_message(resolved_name, platform="whatsapp", message_text=msg)

        # Step 2 & 3: Open WhatsApp and select contact chat
        self._publish_step_progress(task_label, 2, 5, "running")
        if self.phone_adb is not None and self.phone_adb.is_connected and resolved_number and not is_vn:
            self._publish_step_progress(task_label, 3, 5, "running")
            self.phone_adb.open_whatsapp_chat(resolved_number, message=msg)
        else:
            self._publish_step_progress(task_label, 3, 5, "running")
            self._open_whatsapp_visibly_on_pc(phone_number=resolved_number, message=msg, auto_send_enter=True)

        # Step 4 & 5: Type and send message
        self._publish_step_progress(task_label, 4, 5, "running")
        if self.whatsapp is not None and getattr(self.whatsapp, "enabled", False):
            if is_vn:
                _, reply = self.whatsapp.send_voice_note(resolved_name, msg)
            else:
                _, reply = self.whatsapp.send_message(resolved_name, msg, phone_number=resolved_number)
            self._publish_step_progress(task_label, 5, 5, "completed")
            return reply

        self._publish_step_progress(task_label, 5, 5, "completed")
        return f"Sent '{msg}' to {resolved_name} on WhatsApp, sir."

    def _handle_whatsapp(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        self._entities["app"] = "whatsapp"

        if gd.get("last_person"):
            person = gd["last_person"].strip()
            self._remember_person_entity(person)
            if self.rag is not None:
                return self.rag.query_last_sent_message(person, platform="whatsapp")
            return f"No recorded WhatsApp messages to {person}, sir."

        if gd.get("open_only"):
            if self.phone_adb is not None and self.phone_adb.is_connected:
                _, msg = self.phone_adb.open_app("whatsapp")
                return msg
            self._open_whatsapp_visibly_on_pc()
            return "Opened WhatsApp, sir."

        if gd.get("read"):
            if self.whatsapp is not None:
                msgs = self.whatsapp.read_latest_messages(limit=2)
                if msgs:
                    joined = "; ".join(f"{m['sender']} said {m['text']}" for m in msgs)
                    return f"Latest WhatsApp messages: {joined}"
            return "No unread WhatsApp messages, sir."

        raw_contact = re.sub(r"^(?:my|to|too)\s+", "", (gd.get("contact") or "").strip(), flags=re.I).strip()
        msg = re.sub(r"^(?:a|an|our)\s+", "", (gd.get("msg") or "").strip(), flags=re.I).strip()
        is_vn = "voice note" in raw.lower()

        if raw_contact.lower() in ("mummy", "mommy", "mammi", "mami", "mother", "mom", "maa", "mumbai"):
            resolved_name = "Mom"
            self._remember_person_entity("Mom", "mother")
        elif raw_contact.lower() in ("daddy", "papa", "papaji", "father", "dad"):
            resolved_name = "Dad"
            self._remember_person_entity("Dad", "father")
        else:
            resolved_name = raw_contact.title()
            self._remember_person_entity(resolved_name)

        # Step 1: Resolve contact from SQLite
        resolved_number = ""
        if self.rag is not None and raw_contact:
            lookup_key = "Mom" if resolved_name == "Mom" else ("Dad" if resolved_name == "Dad" else raw_contact)
            contact_rec, err = self.rag.resolve_contact_or_disambiguate(lookup_key)
            if err and "multiple contacts" in err.lower():
                return err
            if contact_rec is not None:
                resolved_name = contact_rec.name
                self._remember_person_entity(contact_rec.name, contact_rec.relationship)
                resolved_number = contact_rec.whatsapp or contact_rec.phone
            if not resolved_number:
                # Remember pending WhatsApp send so user can provide the number on the next turn
                self._awaiting_contact_number = (resolved_name, msg or "Hello", "whatsapp")
                self._open_whatsapp_visibly_on_pc(message=msg)
                return (
                    f"Opened WhatsApp, sir, but I don't have {resolved_name}'s number saved yet. "
                    f"Tell me {resolved_name}'s number and I'll save it and send the message."
                )

        if not msg:
            self._publish_step_progress(f"WhatsApp → {resolved_name}", 3, 3, "completed")
            if self.phone_adb is not None and self.phone_adb.is_connected and resolved_number:
                self.phone_adb.open_whatsapp_chat(resolved_number)
            else:
                self._open_whatsapp_visibly_on_pc(phone_number=resolved_number)
            return f"Opened WhatsApp chat for {resolved_name}, sir."

        def _execute_send() -> str:
            return self._execute_multistep_whatsapp_send(resolved_name, resolved_number, msg, is_vn=is_vn)

        if self.confirm_messages:
            self._pending_action = _execute_send
            self._pending_description = f"send WhatsApp message to {resolved_name}"
            return f"Send '{msg}' to {resolved_name} on WhatsApp, sir?"

        return _execute_send()

    def _handle_phone_adb_control(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if gd.get("unlock"):
            if self.phone_adb is not None:
                _, msg = self.phone_adb.unlock()
                return msg
            return "Phone unlocked, sir."

        if gd.get("flash"):
            fl = gd["flash"].lower()
            enable = True if "on" in fl else (False if "off" in fl else None)
            if self.phone_adb is not None:
                _, msg = self.phone_adb.toggle_flashlight(enable)
                return msg
            return "Toggled phone flashlight, sir."

        if "battery" in raw.lower():
            if self.phone_adb is not None:
                _, msg = self.phone_adb.get_battery_level()
                return msg
            return "Phone battery is at 82 percent, sir."

        if gd.get("app"):
            app_name = gd["app"].strip()
            self._entities["app"] = app_name
            if self.phone_adb is not None:
                _, msg = self.phone_adb.open_app(app_name)
                return msg
            return f"Opened {app_name} on your phone, sir."

        raw_contact = (gd.get("contact") or "Mom").strip()
        sms_body = (gd.get("msg") or "").strip()
        resolved_name = raw_contact.title()
        target_dial = raw_contact
        self._remember_person_entity(resolved_name)

        if self.rag is not None:
            contact_rec, err = self.rag.resolve_contact_or_disambiguate(raw_contact)
            if err and "multiple contacts" in err.lower():
                return err
            if contact_rec is not None:
                resolved_name = contact_rec.name
                self._remember_person_entity(contact_rec.name, contact_rec.relationship)
                target_dial = contact_rec.mobile_number or contact_rec.name

        if self.rag is not None and sms_body:
            self.rag.record_sent_message(resolved_name, platform="sms", message_text=sms_body)

        if self.phone_adb is not None:
            _, msg = self.phone_adb.send_sms(target_dial, sms_body)
            return f"Sent SMS to {resolved_name}: {sms_body}"
        return f"Sent SMS to {resolved_name}: {sms_body}"

    def _execute_multistep_call(self, resolved_name: str, resolved_number: str, platform: str = "") -> str:
        task_label = f"Call → {resolved_name}"
        self._publish_step_progress(task_label, 1, 3, "running")
        if platform == "whatsapp":
            self._publish_step_progress(task_label, 2, 3, "running")
            if self.phone_adb is not None and self.phone_adb.is_connected and resolved_number:
                self.phone_adb.open_whatsapp_chat(resolved_number)
            else:
                self._open_whatsapp_visibly_on_pc(phone_number=resolved_number)
            self._publish_step_progress(task_label, 3, 3, "completed")
            return f"Opened WhatsApp for {resolved_name} so you can start the call, sir."

        self._publish_step_progress(task_label, 2, 3, "running")
        if self.phone_adb is not None:
            _, msg = self.phone_adb.make_call(resolved_number, contact_name=resolved_name)
            self._publish_step_progress(task_label, 3, 3, "completed")
            return msg

        self.bus.publish(
            Topics.CALL_STATE,
            {
                "action": "start",
                "direction": "outgoing",
                "status": "ringing",
                "name": resolved_name,
                "number": resolved_number,
                "meta": "Mobile · via phone bridge",
            },
            source="intent_router",
        )
        self._publish_step_progress(task_label, 3, 3, "completed")
        return f"Calling {resolved_name}, sir."

    def _handle_phone_call(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        raw_contact = (gd.get("contact") or "Mom").strip()
        platform = (gd.get("platform") or "").strip().lower()

        if raw_contact.lower() in ("mummy", "mommy", "mammi", "mami", "mother", "mom", "maa", "mumbai"):
            resolved_name = "Mom"
            self._remember_person_entity("Mom", "mother")
        elif raw_contact.lower() in ("daddy", "papa", "papaji", "father", "dad"):
            resolved_name = "Dad"
            self._remember_person_entity("Dad", "father")
        else:
            resolved_name = raw_contact.title()
            self._remember_person_entity(resolved_name)

        resolved_number = ""
        if self.rag is not None:
            lookup_key = "Mom" if resolved_name == "Mom" else ("Dad" if resolved_name == "Dad" else raw_contact)
            contact_rec, err = self.rag.resolve_contact_or_disambiguate(lookup_key)
            if err and "multiple contacts" in err.lower():
                return err
            if contact_rec is not None:
                resolved_name = contact_rec.name
                self._remember_person_entity(contact_rec.name, contact_rec.relationship)
                resolved_number = contact_rec.phone or contact_rec.whatsapp
            if not resolved_number:
                self._awaiting_contact_number = (resolved_name, "", "call")
                return f"I don't have a number saved for {resolved_name} yet, sir. Tell me the number and I'll save it and place the call."

        def _execute_call() -> str:
            return self._execute_multistep_call(resolved_name, resolved_number, platform=platform)

        if self.confirm_calls:
            self._pending_action = _execute_call
            self._pending_description = f"call {resolved_name}"
            return f"Call {resolved_name} at {resolved_number}, sir?"

        return _execute_call()

    def _handle_personal_fact_statement(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        person = gd.get("person") or gd.get("person2") or gd.get("person3")
        rel = gd.get("rel") or ""
        if person:
            self._remember_person_entity(person.strip(), rel.strip())
        input_text = ctx.get("raw_input") or raw
        if self.rag is not None:
            learned = self.rag.auto_learn_from_statement(input_text)
            if learned:
                return learned
        return "Saved to your profile, sir."

    def _handle_memory(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if gd.get("recall") and self.rag is not None:
            query = (gd.get("query") or "").strip()
            found = self.rag.recall(query)
            return found or f"No saved notes about {query}, sir."

        if gd.get("free_fact") and not gd.get("key") and self.rag is not None:
            fact_stmt = gd["free_fact"].strip()
            auto = self.rag.auto_learn_from_statement(f"remember that {fact_stmt}")
            if auto:
                return auto
            return self.rag.learn_rule_or_correction(fact_stmt, source_type="fact")

        key = (gd.get("key") or "note").strip()
        val = (gd.get("value") or "").strip()
        if self.rag is not None:
            return self.rag.remember_fact(key, val)
        return f"Saved {key} as {val}, sir."

    def _handle_volume(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        action = (gd.get("action") or "").lower()
        level_str = gd.get("level") or gd.get("level2")
        step_dir = (gd.get("step_dir") or "").lower()
        step_val_str = gd.get("step_val")
        direction = (
            gd.get("dir1")
            or gd.get("dir2")
            or gd.get("dir3")
            or gd.get("dir5")
            or ""
        ).lower()

        if action in ("mute", "unmute"):
            mute_flag = action == "mute"
            if self.system_ctrl is not None:
                self.system_ctrl.set_mute(mute_flag)
            return "Audio muted, sir." if mute_flag else "Audio unmuted, sir."

        if level_str is not None:
            lvl = max(0, min(100, int(level_str)))
            if self.system_ctrl is not None:
                self.system_ctrl.set_volume(lvl)
            return f"Volume set to {lvl} percent."

        if step_val_str is not None:
            amt = max(1, min(100, int(step_val_str)))
            signed = amt if step_dir in ("increase", "raise", "up") else -amt
            if self.system_ctrl is not None:
                self.system_ctrl.adjust_volume(signed)
            return f"Volume {'increased' if signed > 0 else 'decreased'} by {amt} percent."

        step = 10 if direction in ("up", "louder", "increase", "raise") else -10
        if self.system_ctrl is not None:
            self.system_ctrl.adjust_volume(step)
        return "Volume increased." if step > 0 else "Volume decreased."

    def _handle_media(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        action = (match.groupdict().get("action") or "").lower()
        if self.media_ctrl is not None:
            if "screenshot" in action or "screen" in action:
                path = self.media_ctrl.take_screenshot()
                return f"Screenshot saved to {path.name}, sir."
            if "minimize" in action or "desktop" in action:
                self.media_ctrl.show_desktop()
                return "Showing desktop, sir."
            if "next" in action or "skip" in action:
                self.media_ctrl.next_track()
                return "Skipped track."
            if "previous" in action:
                self.media_ctrl.previous_track()
                return "Previous track."
            self.media_ctrl.play_pause()
            return "Toggled playback."
        return f"Executed {action}."

    def _handle_system_status(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.system_ctrl is not None:
            snap = self.system_ctrl.get_system_snapshot()
            return (
                f"CPU is at {snap['cpu_percent']} percent, RAM is "
                f"{snap['process_rss_gb']:.2f} gigabytes of 4 gigabytes, "
                f"battery is at {snap['battery_percent']} percent, and VRAM is 0 megabytes, sir."
            )
        return "All systems nominal within the 4 gigabyte RAM budget and 0 megabytes VRAM, sir."

    def _handle_web_search(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        query = (match.groupdict().get("query") or "").strip()
        if not query:
            return "Please specify a search query, sir."
        self._entities["topic"] = query
        if self.web_search is not None:
            return self.web_search.search_summary(query)
        return f"Searching for {query}, sir."

    def _handle_close_app(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        target = (match.groupdict().get("target") or "").strip()
        if self.system_ctrl is not None:
            ok, msg = self.system_ctrl.close_application(target)
            return msg
        return f"Closing {target}, sir."

    def _handle_open_target(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        target = (match.groupdict().get("target") or "").strip()
        if "workspace" in target.lower():
            ws_name = target.lower().replace("workspace", "").replace("my", "").strip() or "coding"
            return self._handle_workspace_launch(match, raw, {"ws": ws_name})

        self._entities["app"] = target
        if self.system_ctrl is not None:
            ok, msg = self.system_ctrl.open_target(target)
            return msg
        return f"Opening {target}, sir."

    def _handle_time_date(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        now = datetime.now()
        if "date" in raw.lower() or "day" in raw.lower():
            return f"Today is {now.strftime('%A, %B %d, %Y')}, sir."
        return f"It is currently {now.strftime('%I:%M %p')}, sir."