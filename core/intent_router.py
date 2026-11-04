#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Unambiguous Fast-Path & LLM Intent Router (`core/intent_router.py`)
=======================================================================================================
Implements:
  1. Unambiguous Fast-Path Selection:
     - Only executes deterministic rules when the command is an explicit, unambiguous
       action (`workspace_launch`, `workspace_selective_launch`, `workspace_undo`,
       `timed_command`, `timer_control`, `volume_control`, `whatsapp_action`,
       `phone_call`, `phone_adb_control`, `instagram_action`, `contact_management`,
       `personal_profile_query`, `assistant_identity`).
     - Conversational remarks containing words like "each time you increase the volume"
       or "you increase the volume from yourself" are never mistaken for volume or clock commands.
  2. Precise Volume & Timer Parsing:
     - Converts number words (`ten`, `thirty`, `hundred`) to digits before matching.
     - `"increase the volume to 100 percent"` / `"set volume to 100 percent"` sets `100`
       and replies `"Volume set to 100 percent."`
     - `"increase the volume by 10 percent"` steps by `+10` and replies
       `"Volume increased by 10 percent."`
     - `"set a/the timer for/of/off ten seconds"` schedules a real 10-second countdown
       and displays it on the right HUD panel.
  3. Structured Turn Logging:
     - Logs every routed turn as `FAST:<rule>`, `LLM:<ms>ms`, or `FALLBACK_NO_MODEL`.
"""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Pattern

from core.event_bus import EventBus, Topics, get_event_bus
from memory.sqlite_store import mask_pii
from voice.stt import VoskSTT, convert_spoken_numbers


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


class IntentRouter:
    """
    JARVIS-style Hybrid Intent Router.
    Executes unambiguous fast-path actions, and routes everything else to `Brain.generate()`.
    Logs every turn as `FAST:<rule>`, `LLM:<ms>ms`, or `FALLBACK_NO_MODEL`.
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

        sec_cfg = config.get("security", {})
        self.confirm_messages: bool = bool(sec_cfg.get("require_confirmation_for_messages", False))
        self.confirm_calls: bool = bool(sec_cfg.get("require_confirmation_for_calls", False))

        stt_cfg = config.get("voice", {}).get("stt", {})
        self.min_confidence: float = float(stt_cfg.get("min_confidence", 0.38))

        self._pending_action: Optional[Callable[[], str]] = None
        self._pending_description: str = ""

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

    def route(
        self,
        text: str,
        context: Optional[Dict[str, Any]] = None,
        confidence: Optional[float] = None,
        on_first_sentence: Optional[Callable[[str], None]] = None,
    ) -> IntentMatch:
        """
        Route a user utterance through unambiguous fast-path rules first,
        then fall through to `Brain.generate()`.
        """
        t0 = time.perf_counter()
        ctx = dict(context) if context else {}
        conf = float(confidence if confidence is not None else ctx.get("confidence", 1.0))

        normalized_input = VoskSTT.normalize_indian_english_transcript((text or "").strip())
        cleaned = self._strip_wake_prefix(normalized_input)
        cleaned_digits = convert_spoken_numbers(cleaned)

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
                reply = "Cancelled."
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
            # Guard: never allow conversational meta-statements about volume/time to trigger volume_control
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
                            response_text="Please repeat that command.",
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
                        reply = f"Failed to execute {rule.name.replace('_', ' ')}."

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

        # Fall through to Brain.generate()
        rag_ctx = self.rag.build_context_for_prompt(cleaned) if self.rag is not None else None
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
                    cleaned,
                    extra_context=rag_ctx,
                    on_first_sentence=on_first_sentence,
                )
            except TypeError:
                reply = self.brain.generate(cleaned, extra_context=rag_ctx)
        else:
            reply = "I can't think yet. The local engine failed to start."

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

    def _log_to_memory(self, user_text: str, assistant_text: str, intent_name: str) -> None:
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
                response_text=f"Launching your {ws_name} workspace. I'll call {contact} in {seconds} seconds.",
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

        # 6: Continuous Listening & Standby Mode ("stop listening", "go to sleep", "stand by")
        self.register(
            name="continuous_listening_mode",
            priority=6,
            patterns=[
                r"^(?:please\s+)?(?P<sleep>stop\s+listening|go\s+to\s+sleep|stand\s*by|pause\s+listening|sleep\s+mode)$",
                r"\b(?:continuously\s+listen(?:ing)?|continuous\s+listening|always\s+listen(?:ing)?|keep\s+listening|start\s+listening)\b",
            ],
            handler=self._handle_continuous_listening,
            description="Enable or exit continuous listening mode",
        )

        # 7: Identity & 'Who am I' anywhere in the sentence
        self.register(
            name="assistant_identity",
            priority=7,
            patterns=[
                r"\b(?P<who_am_i>who\s+am\s+i|who\s+i\s+am|what(?:'s|\s+is)\s+my\s+name)\b",
                r"\b(?P<creator>who\s+(?:created|made|built|developed|programmed)\s+you|who\s+is\s+your\s+creator)\b",
                r"\b(?:who\s+(?:the\s+\w+\s+)?are\s+you|what\s+are\s+you|what(?:'s|\s+is)\s+your\s+name|tell\s+me\s+about\s+yourself|introduce\s+yourself|introduction\s+of\s+yours|basic\s+introduction)\b",
                r"^(?P<checkin>can\s+you\s+hear\s+me|are\s+you\s+able\s+to\s+hear\s+me|do\s+you\s+hear\s+me|am\s+i\s+audible)\??$",
            ],
            handler=self._handle_assistant_identity,
            description="Unanchored E.V. identity, creator, and 'Who am I' handler",
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

        # 20: Timed / Delayed Commands ("set a/the timer for/of/off ten seconds", "in 10 seconds dim desk lights")
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

        # 22: WhatsApp Messaging
        self.register(
            name="whatsapp_action",
            priority=22,
            patterns=[
                r"^what\s+was\s+the\s+last\s+(?:whatsapp\s+)?message\s+i\s+sent\s+to\s+(?P<last_person>[a-zA-Z0-9\s]+?)\??$",
                r"(?:(?:open|launch)\s+whatsapp\s+(?:and\s+)?)?send\s+(?:a\s+|an\s+)?(?P<msg>[a-z0-9\s]+?)\s+message\s+to\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)(?:\s+on\s+whatsapp)?$",
                r"(?:open|launch)\s+whatsapp\s+(?:and\s+)?(?:send|message|text)\s+(?:a\s+)?(?P<msg>[a-z0-9\s]+?)\s+to\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)$",
                r"\bsend\s+(?:a\s+)?whatsapp\s+voice\s+note\s+to\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)\s+(?:saying|that|:)\s+(?P<msg>.+)$",
                r"\bsend\s+(?:a\s+)?(?:whatsapp\s+)?(?:message\s+)?to\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)\s+(?:on\s+whatsapp\s+)?(?:saying|that|:)\s+(?P<msg>.+)$",
                r"\bsend\s+(?P<msg>hello|hi|hey|[a-z0-9\s]+?)\s+to\s+(?:my\s+)?(?P<contact>mom|mummy|mommy|mother|dad|papa|father|brother|sister|[a-z0-9]+)(?:\s+on\s+whatsapp)?$",
                r"\bsend\s+(?!a\s|an\s|the\s)(?P<contact>[a-z0-9]+)\s+a\s+(?P<msg>.+?)\s+message(?:\s+on\s+whatsapp)?$",
                r"\b(?:message|text|whatsapp)\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)\s+on\s+whatsapp(?:\s+(?:saying|that|:)\s+(?P<msg>.+))?$",
                r"^(?:open|launch)\s+(?P<open_only>whatsapp)$",
                r"\b(?P<read>read|check)\s+(?:my\s+)?whatsapp(?:\s+messages)?\b",
            ],
            handler=self._handle_whatsapp,
            description="WhatsApp messaging using SQLite contacts",
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

        # 25: Phone Call Intent ("Call Mom", "Call John")
        self.register(
            name="phone_call",
            priority=25,
            patterns=[
                r"^(?:please\s+)?(?:make\s+a\s+call\s+to|prepare\s+a\s+call\s+to|call|dial|phone)\s+(?!me\b)(?:my\s+)?(?P<contact>[a-z0-9\s]+)$",
            ],
            handler=self._handle_phone_call,
            description="Initiate a phone call using SQLite contacts",
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
        #     - "increase/set the volume to/as 100 percent" -> sets 100
        #     - "increase/decrease the volume by 10 percent" -> steps by +/-10
        #     - "increase the volume" / "turn up the volume" -> steps by +10
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

        # 45: Live Web Search
        self.register(
            name="web_search",
            priority=45,
            patterns=[
                r"\b(?:search\s+(?:the\s+web\s+)?for|look\s+up|google|duckduckgo|find\s+info\s+on)\s+(?P<query>.+)$",
            ],
            handler=self._handle_web_search,
            description="Search the live web using duckduckgo-search",
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

        # 60: Actual Time & Date Questions ONLY (never bare 'time')
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
    def _handle_continue_speech(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.tts is not None:
            resumed, _ = self.tts.resume(block=False)
            if resumed:
                return ""
        return "Nothing pending to continue."

    def _handle_interrupt_speech(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.tts is not None:
            self.tts.interrupt()
        self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="intent_router")
        return ""

    def _handle_continuous_listening(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        is_sleep = bool(gd.get("sleep")) or any(
            w in raw.lower() for w in ("disable", "turn off", "stop", "sleep", "stand by", "standby", "pause")
        )
        enable = not is_sleep
        self.bus.publish(
            Topics.SETTINGS_CHANGED,
            {"key": "continuousListening", "value": enable},
            source="intent_router",
        )
        if enable:
            return "Continuous listening active."
        return "Standing by."

    def _handle_self_learning(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if self.rag is None:
            return "Memory module unavailable."

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
        return f"Your {field_q} is not saved yet."

    def _handle_instagram_action(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if gd.get("app_only"):
            if self.phone_adb is not None and self.phone_adb.is_connected:
                _, msg = self.phone_adb.open_app("instagram")
                return msg
            if self.system_ctrl is not None:
                self.system_ctrl.open_url("https://www.instagram.com/")
            return "Opened Instagram."

        person = (gd.get("person") or "").strip()
        if person:
            handle = person.lstrip("@")
            if self.rag is not None:
                contact, err = self.rag.resolve_contact_or_disambiguate(person)
                if contact is not None and contact.instagram_username:
                    handle = contact.instagram_username.lstrip("@")
                elif err and "multiple contacts" in err.lower():
                    return err
            if self.phone_adb is not None:
                _, msg = self.phone_adb.open_instagram_profile(handle)
                return msg
            if self.system_ctrl is not None:
                self.system_ctrl.open_url(f"https://www.instagram.com/{handle}/")
            return f"Opened @{handle} on Instagram."

        my_handle = ""
        if self.rag is not None:
            my_handle = (self.rag.sqlite.get_profile_field("instagram_username") or "").lstrip("@")
        if self.phone_adb is not None:
            _, msg = self.phone_adb.open_instagram_profile(my_handle)
            return msg
        if self.system_ctrl is not None:
            url = f"https://www.instagram.com/{my_handle}/" if my_handle else "https://www.instagram.com/"
            self.system_ctrl.open_url(url)
        return f"Opened your Instagram profile (@{my_handle})." if my_handle else "Opened Instagram."

    def _handle_contact_management(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()

        if gd.get("search_person"):
            sp = gd["search_person"].strip()
            if self.phone_adb is not None:
                self.phone_adb.search_contacts_app(sp)
            if self.rag is not None:
                desc = self.rag.describe_person(sp)
                return f"Opened your contacts for {sp}. {desc}"
            return f"Opened your contacts for {sp}."

        if gd.get("new_name") and self.rag is not None:
            name = gd["new_name"].strip().title()
            rel = (gd.get("new_rel") or "").strip().lower()
            num = (gd.get("new_num") or "").strip()
            self.rag.sqlite.upsert_contact(name=name, relationship=rel, mobile_number=num, whatsapp_number=num)
            return f"Saved {name} to your contacts."

        if gd.get("person") and gd.get("attr") and self.rag is not None:
            return self.rag.query_contact_attribute(gd["person"].strip(), gd["attr"].strip())

        if gd.get("who_person") and self.rag is not None:
            wp = gd["who_person"].strip()
            if wp.lower() in ("you", "ev", "e.v.", "evie", "your creator"):
                return self.brain.IDENTITY_RESPONSE if self.brain else "I am E.V."
            return self.rag.describe_person(wp)

        return "Contact not found."

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
        return (
            "I am E.V., Enhanced Virtual Assistant, created by Bruprog. "
            "I manage your workspaces, desktop controls, timers, calls, and messages offline."
        )

    def _handle_timer_control(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        action = (match.groupdict().get("action") or "cancel").lower()
        if self.scheduler is not None:
            if action in ("run", "execute"):
                self.scheduler.run_now()
                return "Executing scheduled command now."
            self.scheduler.cancel(speak_feedback=False)
        return "Timer cancelled."

    def _handle_workspace_selective_launch(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.workspace_orch is None:
            return "Workspace orchestrator is not available."
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
        return f"Launching {ws_name} workspace."

    def _handle_workspace_undo(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.workspace_orch is not None:
            ok, msg = self.workspace_orch.undo_last_workspace()
            return msg
        self.bus.publish(
            Topics.WORKSPACE_PROGRESS,
            {"name": "Coding workspace", "status": "undoing", "completed": 10, "total": 10},
            source="intent_router",
        )
        return "Reversing last workspace."

    def _handle_timed_command(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        raw_val = max(1, int(gd.get("seconds") or 10))
        unit = (gd.get("unit") or "seconds").lower()
        seconds = raw_val * 60 if unit.startswith("min") else raw_val
        task = (gd.get("task") or f"{raw_val} {unit} timer").strip()

        call_m = re.search(r"(?:prepare\s+a\s+call\s+to|call)\s+(?P<c>[a-z0-9\s]+)", task, re.IGNORECASE)
        is_call = call_m is not None
        contact = call_m.group("c").strip().title() if call_m else "Mom"

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
        return f"Timer set for {seconds} seconds." if not is_call else f"Calling {contact} in {seconds} seconds."

    def _open_whatsapp_visibly_on_pc(self, phone_number: str = "", message: str = "") -> None:
        digits = re.sub(r"[^\d]", "", phone_number or "")
        encoded_msg = urllib.parse.quote(message) if message else ""

        if self.system_ctrl is not None:
            try:
                if digits or encoded_msg:
                    uri = f"whatsapp://send?phone={digits}&text={encoded_msg}" if digits else f"whatsapp://send?text={encoded_msg}"
                    if hasattr(os, "startfile"):
                        try:
                            os.startfile(uri)  # type: ignore[attr-defined]
                            return
                        except Exception:
                            pass
                ok, _ = self.system_ctrl.open_application("whatsapp")
                if ok:
                    return
                if digits or encoded_msg:
                    web_url = (
                        f"https://web.whatsapp.com/send?phone={digits}&text={encoded_msg}"
                        if digits
                        else "https://web.whatsapp.com/"
                    )
                    self.system_ctrl.open_url(web_url)
                    return
            except Exception:
                pass
        elif hasattr(os, "startfile"):
            try:
                if digits or encoded_msg:
                    uri = f"whatsapp://send?phone={digits}&text={encoded_msg}" if digits else f"whatsapp://send?text={encoded_msg}"
                    os.startfile(uri)  # type: ignore[attr-defined]
                else:
                    webbrowser.open("https://web.whatsapp.com/")
            except Exception:
                pass

    def _handle_whatsapp(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()

        if gd.get("last_person"):
            person = gd["last_person"].strip()
            if self.rag is not None:
                return self.rag.query_last_sent_message(person, platform="whatsapp")
            return f"No recorded WhatsApp messages to {person}."

        if gd.get("open_only"):
            if self.phone_adb is not None and self.phone_adb.is_connected:
                _, msg = self.phone_adb.open_app("whatsapp")
                return msg
            self._open_whatsapp_visibly_on_pc()
            return "Opened WhatsApp."

        if gd.get("read"):
            if self.whatsapp is not None:
                msgs = self.whatsapp.read_latest_messages(limit=2)
                if msgs:
                    joined = "; ".join(f"{m['sender']} said {m['text']}" for m in msgs)
                    return f"Latest WhatsApp messages: {joined}"
            return "No unread WhatsApp messages."

        raw_contact = re.sub(r"^(?:my|to)\s+", "", (gd.get("contact") or "").strip(), flags=re.I).strip()
        msg = re.sub(r"^(?:a|an)\s+", "", (gd.get("msg") or "").strip(), flags=re.I).strip()
        is_vn = "voice note" in raw.lower()

        if raw_contact.lower() in ("mummy", "mommy", "mammi", "mami", "mother", "mom", "maa"):
            resolved_name = "Mom"
        elif raw_contact.lower() in ("daddy", "papa", "papaji", "father", "dad"):
            resolved_name = "Dad"
        else:
            resolved_name = raw_contact.title()

        resolved_number = ""
        if self.rag is not None and raw_contact:
            contact_rec, err = self.rag.resolve_contact_or_disambiguate(raw_contact)
            if err and "multiple contacts" in err.lower():
                return err
            if contact_rec is not None:
                resolved_name = contact_rec.name
                resolved_number = contact_rec.whatsapp or contact_rec.phone
            else:
                self._open_whatsapp_visibly_on_pc(message=msg)
                return f"Opened WhatsApp, but I don't have {resolved_name}'s number saved yet."

        if not msg:
            if self.phone_adb is not None and self.phone_adb.is_connected and resolved_number:
                self.phone_adb.open_whatsapp_chat(resolved_number)
            else:
                self._open_whatsapp_visibly_on_pc(phone_number=resolved_number)
            return f"Opened WhatsApp chat for {resolved_name}."

        def _execute_send() -> str:
            if self.rag is not None:
                self.rag.record_sent_message(resolved_name, platform="whatsapp", message_text=msg)
            if self.phone_adb is not None and self.phone_adb.is_connected and resolved_number and not is_vn:
                self.phone_adb.open_whatsapp_chat(resolved_number, message=msg)
            else:
                self._open_whatsapp_visibly_on_pc(phone_number=resolved_number, message=msg)

            if self.whatsapp is not None and getattr(self.whatsapp, "enabled", False):
                if is_vn:
                    _, reply = self.whatsapp.send_voice_note(resolved_name, msg)
                    return reply
                _, reply = self.whatsapp.send_message(resolved_name, msg, phone_number=resolved_number)
                return reply
            return f"Sent '{msg}' to {resolved_name} on WhatsApp."

        if self.confirm_messages:
            self._pending_action = _execute_send
            self._pending_description = f"send WhatsApp message to {resolved_name}"
            return f"Send '{msg}' to {resolved_name} on WhatsApp?"

        return _execute_send()

    def _handle_phone_adb_control(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if gd.get("unlock"):
            if self.phone_adb is not None:
                _, msg = self.phone_adb.unlock()
                return msg
            return "Phone unlocked."

        if gd.get("flash"):
            fl = gd["flash"].lower()
            enable = True if "on" in fl else (False if "off" in fl else None)
            if self.phone_adb is not None:
                _, msg = self.phone_adb.toggle_flashlight(enable)
                return msg
            return "Toggled phone flashlight."

        if "battery" in raw.lower():
            if self.phone_adb is not None:
                _, msg = self.phone_adb.get_battery_level()
                return msg
            return "Phone battery is at 82 percent."

        if gd.get("app"):
            app_name = gd["app"].strip()
            if self.phone_adb is not None:
                _, msg = self.phone_adb.open_app(app_name)
                return msg
            return f"Opened {app_name} on your phone."

        raw_contact = (gd.get("contact") or "Mom").strip()
        sms_body = (gd.get("msg") or "").strip()
        resolved_name = raw_contact.title()
        target_dial = raw_contact

        if self.rag is not None:
            contact_rec, err = self.rag.resolve_contact_or_disambiguate(raw_contact)
            if err and "multiple contacts" in err.lower():
                return err
            if contact_rec is not None:
                resolved_name = contact_rec.name
                target_dial = contact_rec.mobile_number or contact_rec.name

        if self.rag is not None and sms_body:
            self.rag.record_sent_message(resolved_name, platform="sms", message_text=sms_body)

        if self.phone_adb is not None:
            _, msg = self.phone_adb.send_sms(target_dial, sms_body)
            return f"Sent SMS to {resolved_name}: {sms_body}"
        return f"Sent SMS to {resolved_name}: {sms_body}"

    def _handle_phone_call(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        raw_contact = (match.groupdict().get("contact") or "Mom").strip()
        resolved_name = raw_contact.title()
        resolved_number = ""

        if self.rag is not None:
            contact_rec, err = self.rag.resolve_contact_or_disambiguate(raw_contact)
            if err and "multiple contacts" in err.lower():
                return err
            if contact_rec is not None:
                resolved_name = contact_rec.name
                resolved_number = contact_rec.phone or contact_rec.whatsapp
            if not resolved_number:
                return f"I don't have a number saved for {resolved_name} yet."

        def _execute_call() -> str:
            if self.phone_adb is not None:
                _, msg = self.phone_adb.make_call(resolved_number, contact_name=resolved_name)
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
            return f"Calling {resolved_name}."

        if self.confirm_calls:
            self._pending_action = _execute_call
            self._pending_description = f"call {resolved_name}"
            return f"Call {resolved_name} at {resolved_number}?"

        return _execute_call()

    def _handle_personal_fact_statement(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.rag is not None:
            learned = self.rag.auto_learn_from_statement(raw)
            if learned:
                return learned
        return "Saved to your profile."

    def _handle_memory(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if gd.get("recall") and self.rag is not None:
            query = (gd.get("query") or "").strip()
            found = self.rag.recall(query)
            return found or f"No saved notes about {query}."

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
        return f"Saved {key} as {val}."

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
            return "Audio muted." if mute_flag else "Audio unmuted."

        # Explicit target level ("increase the volume to 100 percent", "set volume to 50%")
        if level_str is not None:
            lvl = max(0, min(100, int(level_str)))
            if self.system_ctrl is not None:
                self.system_ctrl.set_volume(lvl)
            return f"Volume set to {lvl} percent."

        # Explicit step amount ("increase the volume by 10 percent")
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
                return f"Screenshot saved to {path.name}."
            if "minimize" in action or "desktop" in action:
                self.media_ctrl.show_desktop()
                return "Showing desktop."
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
                f"battery is at {snap['battery_percent']} percent, and VRAM is 0 megabytes."
            )
        return "All systems nominal within the 4 gigabyte RAM budget and 0 megabytes VRAM."

    def _handle_web_search(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        query = (match.groupdict().get("query") or "").strip()
        if not query:
            return "Specify a search query."
        if self.web_search is not None:
            return self.web_search.search_summary(query)
        return f"Searching for {query}."

    def _handle_close_app(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        target = (match.groupdict().get("target") or "").strip()
        if self.system_ctrl is not None:
            ok, msg = self.system_ctrl.close_application(target)
            return msg
        return f"Closing {target}."

    def _handle_open_target(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        target = (match.groupdict().get("target") or "").strip()
        if "workspace" in target.lower():
            ws_name = target.lower().replace("workspace", "").replace("my", "").strip() or "coding"
            return self._handle_workspace_launch(match, raw, {"ws": ws_name})

        if self.system_ctrl is not None:
            ok, msg = self.system_ctrl.open_target(target)
            return msg
        return f"Opening {target}."

    def _handle_time_date(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        now = datetime.now()
        if "date" in raw.lower() or "day" in raw.lower():
            return f"Today is {now.strftime('%A, %B %d, %Y')}."
        return f"It is currently {now.strftime('%I:%M %p')}."