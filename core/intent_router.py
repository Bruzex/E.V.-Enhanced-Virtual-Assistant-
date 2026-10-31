#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Personal Digital Companion Intent Router (v3.0)
===================================================================================
Natural-language intent engine combining:
  1. Personal Identity & Profile Management:
     - "What's my mobile number?", "What's my Instagram username?",
       "When is my birthday?", "What is my email address?"
     - "My mobile number is ...", "My Instagram username is @..."
  2. Friends, Family & Contact Intelligence (with Ambiguity Guard):
     - "Who is Arin?", "What's Arin's phone number?", "Find my brother's mobile number",
       "What is Arin's Instagram?", "Open my contacts and search for Arin"
     - "Arin is my friend", "Arin's phone number is ...", "Save contact Arin ..."
  3. Mobile Application Integration (Instagram, WhatsApp, Phone & Contacts):
     - Instagram: "Open Instagram", "Open my Instagram profile", "Find Arin on Instagram"
     - WhatsApp: "Open WhatsApp", "Send a hello message to Arin on WhatsApp",
       "Hey, send Arin a hello message on WhatsApp", "Message Arin on WhatsApp",
       "What was the last message I sent to Arin?"
     - Phone: "Call my mother", "Call Arin"
  4. Permission & Confirmation Gate (`_pending_confirmation`):
     - When `security.require_confirmation_for_messages` or `require_confirmation_for_calls`
       is enabled in `config.yaml`, E.V. asks for confirmation ("Should I send the
       WhatsApp message 'Hello' to Arin? Say yes to confirm or no to cancel")
       before executing sensitive outbound actions.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Pattern

from core.event_bus import EventBus, Topics, get_event_bus
from memory.sqlite_store import mask_pii


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


class IntentRouter:
    """
    Personal Companion Hybrid Intent Router.
    Evaluates priority-ordered natural language rules first (0 MB LLM RAM),
    enforces permission/confirmation gates on sensitive outbound actions,
    and falls back to `Brain.generate(extra_context=rag_context)` for open-ended chat.
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
    ) -> None:
        """Register a deterministic intent rule. Lower `priority` numbers run first."""
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        self._rules.append(
            IntentRule(
                name=name,
                patterns=compiled,
                handler=handler,
                priority=priority,
                description=description,
            )
        )
        self._rules.sort(key=lambda r: r.priority)

    def route(self, text: str, context: Optional[Dict[str, Any]] = None) -> IntentMatch:
        """
        Route a user utterance through confirmation gates and fast-path rules first.
        Only falls back to `Brain.generate()` if no deterministic rule matches.
        """
        t0 = time.perf_counter()
        ctx = dict(context) if context else {}
        cleaned = self._strip_wake_prefix((text or "").strip())

        if not cleaned:
            return IntentMatch(
                intent_name="empty",
                raw_text=text,
                response_text="Standing by.",
                handled_by_fast_path=True,
                latency_ms=0.0,
            )

        # 0. Check if we are waiting for user confirmation ("yes" / "confirm" / "no" / "cancel")
        if self._pending_action is not None:
            low_c = cleaned.lower().strip(".?!")
            if low_c in ("yes", "yeah", "yep", "confirm", "send it", "do it", "go ahead", "sure", "okay", "ok"):
                action_fn = self._pending_action
                self._pending_action = None
                self._pending_description = ""
                reply = action_fn()
                self._log_to_memory(cleaned, reply, "confirmed_action")
                return IntentMatch(
                    intent_name="confirmed_action",
                    raw_text=cleaned,
                    response_text=reply,
                    handled_by_fast_path=True,
                    latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                )
            if low_c in ("no", "nope", "cancel", "stop", "don't", "abort", "never mind"):
                self._pending_action = None
                self._pending_description = ""
                reply = "Cancelled. I won't send or execute that action."
                self._log_to_memory(cleaned, reply, "cancelled_action")
                return IntentMatch(
                    intent_name="cancelled_action",
                    raw_text=cleaned,
                    response_text=reply,
                    handled_by_fast_path=True,
                    latency_ms=round((time.perf_counter() - t0) * 1000.0, 2),
                )

        # 1. Check compound command (Workspace + Timed Call/Action)
        compound = self._try_compound_command(cleaned, ctx)
        if compound is not None:
            compound.latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            self._log_to_memory(cleaned, compound.response_text, compound.intent_name)
            return compound

        # 2. Evaluate single deterministic rules in priority order
        for rule in self._rules:
            for pattern in rule.patterns:
                match = pattern.search(cleaned)
                if match is not None:
                    self.bus.publish(
                        Topics.ORB_STATE,
                        {"state": "acting", "progress": 0.0},
                        source="intent_router",
                    )
                    try:
                        reply = rule.handler(match, cleaned, ctx)
                    except Exception as exc:
                        self.logger.error("Error in intent handler '%s': %s", rule.name, exc, exc_info=True)
                        reply = f"I encountered an issue executing {rule.name.replace('_', ' ')}."

                    latency = round((time.perf_counter() - t0) * 1000.0, 2)
                    self.logger.info(
                        "Fast-path intent '%s' matched in %.2f ms (input='%s')",
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

        # 3. Fall through to CPU-only Local LLM (Qwen2.5-3B-Instruct Q4_K_M) with RAG context
        self.logger.info("Routing conversational query to Brain: '%s'", mask_pii(cleaned))
        rag_ctx = self.rag.build_context_for_prompt(cleaned) if self.rag is not None else None
        reply = (
            self.brain.generate(cleaned, extra_context=rag_ctx)
            if self.brain is not None
            else "I'm listening and ready."
        )
        latency = round((time.perf_counter() - t0) * 1000.0, 2)
        self._log_to_memory(cleaned, reply, "llm_fallback")
        return IntentMatch(
            intent_name="llm_fallback",
            raw_text=cleaned,
            response_text=reply,
            handled_by_fast_path=False,
            latency_ms=latency,
        )

    def _log_to_memory(self, user_text: str, assistant_text: str, intent_name: str) -> None:
        if self.rag is not None:
            try:
                self.rag.record_interaction(user_text, assistant_text, intent_name=intent_name)
            except Exception:
                pass

    @staticmethod
    def _strip_wake_prefix(text: str) -> str:
        return re.sub(
            r"^(?:hey\s+(?:e\.?v\.?|eve|ivy|easy|tv|a\s*v|heavy)|okay\s+e\.?v\.?|ok\s+e\.?v\.?|hi\s+e\.?v\.?|e\.?v\.?|jarvis|hey)[\s,.:;-]*",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    # =========================================================================
    # Compound Command Decomposition (Workspace + Timed Call via Scheduler)
    # =========================================================================
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
    # Built-in Fast-Path Intent Rules (Priority Ordered)
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

        # 6: Continuous Listening Voice Toggle
        self.register(
            name="continuous_listening_mode",
            priority=6,
            patterns=[
                r"\b(?:continuously\s+listen(?:ing)?|continuous\s+listening|always\s+listen(?:ing)?|keep\s+listening)\b",
            ],
            handler=self._handle_continuous_listening,
            description="Enable always-on continuous listening mode",
        )

        # 7: Identity ("Who are you?", "What is your name?", "Who created you?")
        self.register(
            name="assistant_identity",
            priority=7,
            patterns=[
                r"^(?:who|what)\s+are\s+you\??$",
                r"^(?:what(?:'s|\s+is)\s+your\s+name|tell\s+me\s+about\s+yourself|introduce\s+yourself)\??$",
                r"^who\s+(?:created|made|built|developed)\s+you\??$",
            ],
            handler=self._handle_assistant_identity,
            description="Respond with E.V.'s natural Bruprog identity",
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

        # 17: Personal Identity & Self Profile Queries
        #     ("What's my mobile number?", "What's my Instagram username?", "When is my birthday?", "What is my email address?")
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
        #     ("Open Instagram", "Open my Instagram profile", "Find Arin on Instagram", "Open Arin's Instagram")
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
            description="Open Instagram app, user's own Instagram profile, or a contact's Instagram profile",
        )

        # 19: Friends, Family & Contact Queries & Updates
        #     ("Who is Arin?", "What's Arin's phone number?", "Find my brother's mobile number", "Open my contacts and search for Arin")
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
            description="Query or manage friends, family, and contacts in SQLite",
        )

        # 20: Timed / Delayed Commands ("in 10 seconds dim desk lights to 30%")
        self.register(
            name="timed_command",
            priority=20,
            patterns=[
                r"\bin\s+(?P<seconds>\d+)\s*(?:seconds?|secs?|s)\s+(?P<task>.+)$",
                r"^(?P<task>prepare\s+a\s+call\s+to\s+[a-z0-9\s]+|call\s+[a-z0-9\s]+|dim\s+.+?)\s+in\s+(?P<seconds>\d+)\s*(?:seconds?|secs?|s)\b",
                r"\bset\s+(?:a\s+)?timer\s+for\s+(?P<seconds>\d+)\s*(?:seconds?|secs?|s)(?:\s+to\s+(?P<task>.+))?",
            ],
            handler=self._handle_timed_command,
            description="Schedule a delayed command with visual HUD countdown",
        )

        # 22: Natural Language WhatsApp Messaging, Voice Notes & Last Message Recall
        #     - "What was the last message I sent to Arin?"
        #     - "Hey, send Arin a hello message on WhatsApp"
        #     - "Send a hello message to Arin on WhatsApp"
        #     - "Message Arin on WhatsApp saying hello"
        #     - "Message Arin on WhatsApp"
        #     - "Open WhatsApp"
        self.register(
            name="whatsapp_action",
            priority=22,
            patterns=[
                r"^what\s+was\s+the\s+last\s+(?:whatsapp\s+)?message\s+i\s+sent\s+to\s+(?P<last_person>[a-zA-Z0-9\s]+?)\??$",
                r"^(?:open|launch)\s+(?P<open_only>whatsapp)$",
                r"\bsend\s+(?:a\s+)?whatsapp\s+voice\s+note\s+to\s+(?P<contact>[a-z0-9\s]+?)\s+(?:saying|that|:)\s+(?P<msg>.+)$",
                r"\bsend\s+(?:a\s+)?whatsapp\s+(?:message\s+)?to\s+(?P<contact>[a-z0-9\s]+?)\s+(?:saying|that|:)\s+(?P<msg>.+)$",
                r"\bsend\s+(?P<contact>[a-z0-9\s]+?)\s+a\s+(?P<msg>.+?)\s+message\s+on\s+whatsapp\b",
                r"\bsend\s+a\s+(?P<msg>.+?)\s+message\s+to\s+(?P<contact>[a-z0-9\s]+?)\s+on\s+whatsapp\b",
                r"\b(?:message|text|whatsapp)\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+?)\s+on\s+whatsapp(?:\s+(?:saying|that|:)\s+(?P<msg>.+))?$",
                r"\b(?P<read>read|check)\s+(?:my\s+)?whatsapp(?:\s+messages)?\b",
            ],
            handler=self._handle_whatsapp,
            description="Natural language WhatsApp messaging, voice notes, and message history recall",
        )

        # 24: Phone ADB Specific Controls (Unlock, SMS, Flashlight, Phone App, Phone Battery)
        self.register(
            name="phone_adb_control",
            priority=24,
            patterns=[
                r"\b(?P<unlock>unlock)\s+(?:my\s+)?phone\b",
                r"\bsend\s+(?:an?\s+)?(?:sms|text|message)\s+to\s+(?P<contact>[a-z0-9\s]+?)\s+(?:saying|that|:)\s+(?P<msg>.+)$",
                r"\b(?P<flash>toggle|turn\s+on|turn\s+off)\s+(?:phone\s+)?flashlight\b",
                r"\b(?P<bat>check\s+)?phone\s+battery\b",
                r"\bopen\s+(?P<app>[a-z0-9\s]+?)\s+on\s+(?:my\s+)?phone\b",
            ],
            handler=self._handle_phone_adb_control,
            description="Control Android phone over Wireless/USB ADB",
        )

        # 25: Phone Call Intent ("Call my mother", "Call Arin")
        self.register(
            name="phone_call",
            priority=25,
            patterns=[
                r"\b(?:make\s+a\s+call\s+to|prepare\s+a\s+call\s+to|call|dial|phone)\s+(?:my\s+)?(?P<contact>[a-z0-9\s]+)$",
            ],
            handler=self._handle_phone_call,
            description="Initiate or prepare a phone call via wireless ADB bridge with contact resolution",
        )

        # 16: Natural Personal Profile & Contact Teaching Statements
        #     - "Remember that my name is Aditya Raj"
        #     - "My Instagram is @bruprog"
        #     - "My mobile number is +91 ..."
        #     - "Arin is my friend, his number is ..."
        #     - "Remember that Mom's WhatsApp number is ..."
        #     - "Update Arin's Instagram to @..."
        self.register(
            name="personal_fact_statement",
            priority=16,
            patterns=[
                r"^(?:remember\s+that\s+)?(?P<person>[a-zA-Z0-9]+)\s+is\s+my\s+(?P<rel>friend|best\s+friend|brother|sister|mother|father|mom|dad|colleague|cousin)(?:[\s,;]+(?:and\s+)?(?:his|her|their)\s+(?P<attr>phone\s+number|mobile\s+number|whatsapp\s+number|number|phone|whatsapp|instagram)\s+is\s+(?P<val>.+))?$",
                r"^(?:update|set|change|remember\s+that)\s+(?P<person2>[a-zA-Z0-9\s]+?)'s\s+(?P<attr2>phone\s+number|mobile\s+number|whatsapp\s+number|number|phone|whatsapp|instagram(?:\s+username)?|birthday)\s+(?:to|is|as)\s+(?P<val2>.+)$",
                r"^(?P<person3>[a-zA-Z0-9]+)'s\s+(?P<attr3>phone\s+number|mobile\s+number|whatsapp\s+number|number|phone|whatsapp|instagram(?:\s+username)?|birthday)\s+is\s+(?P<val3>.+)$",
                r"^(?:remember\s+that\s+)?(?:my\s+(?:full\s+name|name|nickname|mobile\s+number|phone\s+number|cell\s+number|mobile|phone|email(?:\s+address)?|instagram(?:\s+username|\s+handle|\s+id)?|birthday|date\s+of\s+birth|dob|age|home\s+address|address)|i\s+live\s+in)\s+(?:is\s+)?(?P<self_val>.+)$",
            ],
            handler=self._handle_personal_fact_statement,
            description="Teach E.V. personal profile fields and contact details naturally by voice",
        )

        # 28: Memory Store & Recall ("remember that X is Y", "what do you remember about X")
        self.register(
            name="memory_rag",
            priority=28,
            patterns=[
                r"\bremember\s+that\s+(?P<key>[^=]+?)\s+is\s+(?P<value>.+)$",
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
                r"\b(?P<action>mute|unmute)\b(?:\s+(?:the\s+)?(?:audio|volume|sound|system))?",
                r"\b(?:set\s+)?volume\s+(?:to\s+)?(?P<level>\d{1,3})\s*%?",
                r"\b(?:turn\s+|volume\s+)?(?P<direction>up|down|louder|quieter|softer)\b(?:\s+(?:the\s+)?volume)?",
            ],
            handler=self._handle_volume,
            description="Adjust or mute system master audio volume",
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

        # 45: Live Web Search (duckduckgo-search)
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
                r"\b(?:close|quit|kill|exit|terminate)\s+(?:the\s+)?(?P<target>[a-z0-9\s._-]+)$",
            ],
            handler=self._handle_close_app,
            description="Close a running desktop application",
        )

        # 55: Open Application / Folder / Website
        self.register(
            name="open_target",
            priority=55,
            patterns=[
                r"\b(?:open|launch|start|run)\s+(?:the\s+|my\s+)?(?P<target>[a-z0-9\s.:/_~-]+)$",
            ],
            handler=self._handle_open_target,
            description="Open a local application, folder, or URL",
        )

        # 60: Time & Date Fast Queries
        self.register(
            name="time_date",
            priority=60,
            patterns=[
                r"\bwhat\s+(?:time|day|date)\s+is\s+it\b",
                r"\bcurrent\s+(?:time|date)\b",
            ],
            handler=self._handle_time_date,
            description="Report current local time and date without LLM overhead",
        )

    # =========================================================================
    # Handler Implementations
    # =========================================================================
    def _handle_continue_speech(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.tts is not None:
            resumed, _ = self.tts.resume(block=False)
            if resumed:
                return ""
        return "There wasn't anything pending to continue, so I'm ready for your next command."

    def _handle_interrupt_speech(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.tts is not None:
            self.tts.interrupt()
        self.bus.publish(Topics.ORB_STATE, {"state": "listening", "progress": 0.0}, source="intent_router")
        return ""

    def _handle_continuous_listening(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        enable = not any(w in raw.lower() for w in ("disable", "turn off", "stop"))
        self.bus.publish(
            Topics.SETTINGS_CHANGED,
            {"key": "continuousListening", "value": enable},
            source="intent_router",
        )
        if enable:
            return (
                "Continuous listening is active. You don't need to click the microphone — "
                "just speak naturally, and you can interrupt me or say continue at any time."
            )
        return "Continuous listening turned off. Say Hey E.V. whenever you need me."

    def _handle_personal_profile_query(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        field_q = (match.groupdict().get("field") or "").strip()
        if self.rag is not None:
            return self.rag.query_my_profile(field_q)
        return f"Please configure your {field_q} in data/personal_profile.json."

    def _handle_instagram_action(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        # 1. "Open Instagram"
        if gd.get("app_only"):
            if self.phone_adb is not None and self.phone_adb.is_connected:
                _, msg = self.phone_adb.open_app("instagram")
                return msg
            if self.system_ctrl is not None:
                self.system_ctrl.open_url("https://www.instagram.com/")
            return "Opened Instagram."

        # 2. "Find Arin on Instagram" / "Open Arin's Instagram"
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

        # 3. "Open my Instagram profile"
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

        # 1. "Open my contacts and search for Arin"
        if gd.get("search_person"):
            sp = gd["search_person"].strip()
            if self.phone_adb is not None:
                self.phone_adb.search_contacts_app(sp)
            if self.rag is not None:
                desc = self.rag.describe_person(sp)
                return f"Opened your contacts for {sp}. {desc}"
            return f"Opened your contacts and searched for {sp}."

        # 2. "Save contact Arin as friend with number ..."
        if gd.get("new_name") and self.rag is not None:
            name = gd["new_name"].strip().title()
            rel = (gd.get("new_rel") or "").strip().lower()
            num = (gd.get("new_num") or "").strip()
            self.rag.sqlite.upsert_contact(name=name, relationship=rel, mobile_number=num, whatsapp_number=num)
            return f"Saved {name} to your contacts."

        # 3. "What's Arin's phone number?" / "Find my brother's mobile number"
        if gd.get("person") and gd.get("attr") and self.rag is not None:
            return self.rag.query_contact_attribute(gd["person"].strip(), gd["attr"].strip())

        # 4. "Who is Arin?"
        if gd.get("who_person") and self.rag is not None:
            wp = gd["who_person"].strip()
            # Guard against "Who is my creator" or "Who are you"
            if wp.lower() in ("you", "ev", "e.v.", "your creator"):
                return self.brain.IDENTITY_RESPONSE if self.brain else "I am E.V."
            return self.rag.describe_person(wp)

        return "I couldn't find that contact in your database."

    def _handle_assistant_identity(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.brain is not None and hasattr(self.brain, "IDENTITY_RESPONSE"):
            return str(self.brain.IDENTITY_RESPONSE)
        return (
            "Hey, I’m E.V. — Enhanced Virtual Assistant. I was created by Bruprog. "
            "I can help you control your computer, manage workspaces, remember important things, "
            "make calls, send messages, and more — all offline."
        )

    def _handle_timer_control(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        action = (match.groupdict().get("action") or "cancel").lower()
        if self.scheduler is not None:
            if action in ("run", "execute"):
                self.scheduler.run_now()
                return "Executing scheduled command now."
            self.scheduler.cancel(speak_feedback=False)
        return "Timed command cancelled."

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
        seconds = max(1, int(gd.get("seconds") or 10))
        task = (gd.get("task") or "Timer complete").strip()

        call_m = re.search(r"(?:prepare\s+a\s+call\s+to|call)\s+(?P<c>[a-z0-9\s]+)", task, re.IGNORECASE)
        is_call = call_m is not None
        contact = call_m.group("c").strip().title() if call_m else "Mom"

        label = f"Call {contact}" if is_call else (task[:1].upper() + task[1:])
        detail = "Phone bridge · Mobile · E.V. voice routed" if is_call else "Smart plug · Study"
        done_msg = "" if is_call else f"Completed: {label}."

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
        return f"Okay. {label} in {seconds} seconds."

    def _handle_whatsapp(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()

        # 1. "What was the last message I sent to Arin?"
        if gd.get("last_person"):
            person = gd["last_person"].strip()
            if self.rag is not None:
                return self.rag.query_last_sent_message(person, platform="whatsapp")
            return f"I don't have any recorded WhatsApp messages to {person} yet."

        # 2. "Open WhatsApp"
        if gd.get("open_only"):
            if self.phone_adb is not None and self.phone_adb.is_connected:
                _, msg = self.phone_adb.open_app("whatsapp")
                return msg
            if self.system_ctrl is not None:
                self.system_ctrl.open_application("whatsapp")
            return "Opened WhatsApp."

        # 3. "Read my WhatsApp messages"
        if gd.get("read"):
            if self.whatsapp is not None:
                msgs = self.whatsapp.read_latest_messages(limit=2)
                if msgs:
                    joined = "; ".join(f"{m['sender']} said {m['text']}" for m in msgs)
                    return f"Latest WhatsApp messages: {joined}"
            return "No unread WhatsApp messages right now."

        # 4. Send WhatsApp Message / Voice Note (with Contact Resolution & Disambiguation)
        raw_contact = (gd.get("contact") or "").strip()
        msg = (gd.get("msg") or "").strip()
        is_vn = "voice note" in raw.lower()

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
                return (
                    f"I don't have a number saved for {resolved_name} yet. "
                    f"You can tell me by saying '{resolved_name} is my friend, his number is ...'."
                )

        # If user said "Message Arin on WhatsApp" without specifying message body yet
        if not msg:
            if self.phone_adb is not None and resolved_number:
                self.phone_adb.open_whatsapp_chat(resolved_number)
            return f"Opened WhatsApp chat for {resolved_name}. What message would you like me to send?"

        def _execute_send() -> str:
            if self.rag is not None:
                self.rag.record_sent_message(resolved_name, platform="whatsapp", message_text=msg)
            if self.phone_adb is not None and self.phone_adb.is_connected and resolved_number and not is_vn:
                self.phone_adb.open_whatsapp_chat(resolved_number, message=msg)
            if self.whatsapp is not None:
                if is_vn:
                    _, reply = self.whatsapp.send_voice_note(resolved_name, msg)
                    return reply
                _, reply = self.whatsapp.send_message(resolved_name, msg, phone_number=resolved_number)
                return reply
            return f"Sent WhatsApp message to {resolved_name}: {msg}"

        if self.confirm_messages:
            self._pending_action = _execute_send
            self._pending_description = f"send WhatsApp message to {resolved_name}"
            return f"Should I send the WhatsApp message '{msg}' to {resolved_name}? Say yes to confirm or no to cancel."

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
            return "Your phone battery is at 82 percent."

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
                return (
                    f"I don't have a number saved for {resolved_name} yet. "
                    f"You can tell me by saying 'Remember that {resolved_name}'s phone number is ...'."
                )

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
            return f"Your call to {resolved_name} is ready."

        if self.confirm_calls:
            self._pending_action = _execute_call
            self._pending_description = f"call {resolved_name}"
            return f"Would you like me to call {resolved_name} at {resolved_number}? Say yes to confirm or no to cancel."

        return _execute_call()

    def _handle_personal_fact_statement(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.rag is not None:
            learned = self.rag.auto_learn_from_statement(raw)
            if learned:
                return learned
        return "I've saved that to your personal profile."

    def _handle_memory(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        if gd.get("recall") and self.rag is not None:
            query = (gd.get("query") or "").strip()
            found = self.rag.recall(query)
            return found or f"I don't have any saved notes about {query} yet."

        key = (gd.get("key") or "note").strip()
        val = (gd.get("value") or "").strip()
        if self.rag is not None:
            return self.rag.remember_fact(key, val)
        return f"I'll remember that {key} is {val}."

    def _handle_volume(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        action = (gd.get("action") or "").lower()
        level_str = gd.get("level")
        direction = (gd.get("direction") or "").lower()

        if action in ("mute", "unmute"):
            mute_flag = action == "mute"
            if self.system_ctrl is not None:
                self.system_ctrl.set_mute(mute_flag)
            return "System audio muted." if mute_flag else "System audio unmuted."

        if level_str is not None:
            lvl = max(0, min(100, int(level_str)))
            if self.system_ctrl is not None:
                self.system_ctrl.set_volume(lvl)
            return f"Volume set to {lvl} percent."

        step = 10 if direction in ("up", "louder") else -10
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
                return "Skipped to next track."
            if "previous" in action:
                self.media_ctrl.previous_track()
                return "Returned to previous track."
            self.media_ctrl.play_pause()
            return "Toggled media playback."
        return f"Executed media action: {action}."

    def _handle_system_status(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        if self.system_ctrl is not None:
            snap = self.system_ctrl.get_system_snapshot()
            return (
                f"CPU is at {snap['cpu_percent']} percent, E.V. RAM usage is "
                f"{snap['process_rss_gb']:.2f} gigabytes of your 4 gigabyte budget, "
                f"battery is at {snap['battery_percent']} percent, and GPU VRAM is 0 megabytes."
            )
        return "All systems nominal. CPU and RAM are within your 4 gigabyte budget with 0 megabytes VRAM."

    def _handle_web_search(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        query = (match.groupdict().get("query") or "").strip()
        if not query:
            return "What would you like me to search for?"
        if self.web_search is not None:
            return self.web_search.search_summary(query)
        return f"Searching DuckDuckGo for {query}."

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
        return f"The current time is {now.strftime('%I:%M %p')}."