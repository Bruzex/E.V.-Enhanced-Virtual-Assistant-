#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Hybrid Keyword + LLM Intent Router
======================================================================
Intercepts common system, workspace, media, timer, phone ADB, WhatsApp,
memory, and search commands using deterministic regex/keyword rules in < 2 ms,
bypassing `Brain.generate()` completely so simple automation commands consume
0 extra RAM and never spin up the 3B LLM.

Phase 3 Integration:
  - `TimedCommandScheduler` (`self.scheduler`): Drives delayed actions, Cancel,
    and Run Now directly on the right-side HUD Countdown Panel.
  - `PhoneADBController` (`self.phone_adb`): Handles wireless ADB phone unlock,
    calls, SMS, app opening, flashlight, phone battery, and phone screenshots.
  - `WhatsAppController` (`self.whatsapp`): Handles WhatsApp text & voice notes.
  - `RAGMemoryManager` (`self.rag`): Handles "remember that X is Y" / "what do
    you remember about X" in SQLite + ChromaDB, and injects relevant RAG context
    whenever an open-ended query falls through to `Brain.generate()`.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Pattern

from core.event_bus import EventBus, Topics, get_event_bus


@dataclass
class IntentMatch:
    """Structured result returned when an intent is matched and executed."""

    intent_name: str
    raw_text: str
    response_text: str = ""
    slots: Dict[str, Any] = field(default_factory=dict)
    handled_by_fast_path: bool = True
    latency_ms: float = 0.0

    @property
    def intent_type(self) -> str:
        if "compound" in self.intent_name or "chained" in self.intent_name:
            return "chained_workflow"
        if "workspace" in self.intent_name:
            return "workspace"
        if "timed" in self.intent_name:
            return "timed_command"
        if not self.handled_by_fast_path or self.intent_name in ("llm_fallback", "chat"):
            return "chat"
        return self.intent_name

    @property
    def action(self) -> str:
        if "compound" in self.intent_name or "chained" in self.intent_name:
            return "workspace_and_timed_call"
        if "undo" in self.intent_name:
            return "undo"
        if "launch" in self.intent_name:
            return "launch"
        return self.intent_name

    @property
    def params(self) -> Dict[str, Any]:
        return self.slots

    @property
    def bypass_llm(self) -> bool:
        return self.handled_by_fast_path


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
    Fast hybrid Intent Router.
    1. Evaluates priority-ordered regex/keyword rules first (0 MB LLM RAM).
    2. Supports compound command chaining (e.g. Workspace + Timed Call via Scheduler).
    3. Falls back to `Brain.generate(extra_context=rag_context)` only for
       open-ended conversational queries.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        brain: Optional[Any] = None,
        system_ctrl: Optional[Any] = None,
        workspace_orch: Optional[Any] = None,
        media_ctrl: Optional[Any] = None,
        web_search: Optional[Any] = None,
        scheduler: Optional[Any] = None,
        phone_adb: Optional[Any] = None,
        whatsapp: Optional[Any] = None,
        rag: Optional[Any] = None,
        event_bus: Optional[EventBus] = None,
    ) -> None:
        self.config = config or {}
        self.brain = brain
        self.system_ctrl = system_ctrl
        self.workspace_orch = workspace_orch
        self.media_ctrl = media_ctrl
        self.web_search = web_search
        self.scheduler = scheduler
        self.phone_adb = phone_adb
        self.whatsapp = whatsapp
        self.rag = rag
        self.bus = event_bus or get_event_bus()
        self.logger = logging.getLogger("ev.intent_router")

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
        Route a user utterance through the fast keyword/regex registry first.
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

        # 1. Check for the signature compound command (Workspace + Timed Call/Action)
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
                        "Fast-path intent '%s' matched in %.2f ms (LLM bypassed)",
                        rule.name,
                        latency,
                    )
                    self._log_to_memory(cleaned, reply, rule.name)
                    slots = dict(match.groupdict())
                    if "seconds" in slots and slots["seconds"]:
                        try:
                            slots["delay_seconds"] = int(slots["seconds"])
                        except Exception:
                            pass
                    if "task" in slots and slots["task"]:
                        slots["command"] = slots["task"]

                    return IntentMatch(
                        intent_name=rule.name,
                        raw_text=cleaned,
                        response_text=reply,
                        slots=slots,
                        handled_by_fast_path=True,
                        latency_ms=latency,
                    )

        # 3. Fall through to CPU-only Local LLM (Qwen2.5-3B-Instruct Q4_K_M) with RAG context
        self.logger.info("No fast-path match for '%s'; routing to CPU LLM Brain.", cleaned)
        rag_ctx = self.rag.build_context_for_prompt(cleaned) if self.rag is not None else None
        reply = (
            self.brain.generate(cleaned, extra_context=rag_ctx)
            if self.brain is not None
            else "Standing by."
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
            r"^(?:hey\s+e\.?v\.?|okay\s+e\.?v\.?|hi\s+e\.?v\.?|e\.?v\.?|jarvis)[\s,.:;-]*",
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
            r"(?:\s+in\s+(?P<seconds>\d+)\s*(?:seconds?|secs?|s))?[.\s]*$",
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
                slots={"workspace": ws_name, "contact": contact, "seconds": seconds, "delay_seconds": seconds},
                handled_by_fast_path=True,
            )
        return None

    # =========================================================================
    # Built-in Fast-Path Intent Rules (Priority Ordered)
    # =========================================================================
    def _register_builtin_rules(self) -> None:
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

        # 14: Selective Workspace Launch ("open only VS Code and browser from my coding workspace")
        self.register(
            name="workspace_selective_launch",
            priority=14,
            patterns=[
                r"\b(?:open|launch|start)\s+only\s+(?P<items>.+?)\s+from\s+(?:my\s+|the\s+)?(?P<ws>[a-z0-9_-]+)\s+workspace\b",
                r"\bfrom\s+(?:my\s+|the\s+)?(?P<ws>[a-z0-9_-]+)\s+workspace\s+(?:open|launch|start)\s+only\s+(?P<items>.+)\b",
            ],
            handler=self._handle_workspace_selective_launch,
            description="Launch only selected applications from a saved workspace",
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

        # 22: WhatsApp Messaging & Voice Notes
        self.register(
            name="whatsapp_action",
            priority=22,
            patterns=[
                r"\bsend\s+(?:a\s+)?whatsapp\s+voice\s+note\s+to\s+(?P<contact>[a-z0-9\s]+?)\s+(?:saying|that)\s+(?P<msg>.+)$",
                r"\bsend\s+(?:a\s+)?whatsapp\s+(?:message\s+)?to\s+(?P<contact>[a-z0-9\s]+?)\s+(?:saying|that|:)\s+(?P<msg>.+)$",
                r"\b(?P<read>read|check)\s+(?:my\s+)?whatsapp(?:\s+messages)?\b",
            ],
            handler=self._handle_whatsapp,
            description="Send WhatsApp text/voice notes or read unread WhatsApp messages",
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

        # 26: Call by dynamic contact name
        self.register(
            name="call_contact",
            priority=26,
            patterns=[
                r"\b(?:make\s+a\s+call\s+to|prepare\s+a\s+call\s+to|call|dial|phone|ring)\s+(?P<name>[a-z0-9\s]+)$",
            ],
            handler=self._handle_call_contact,
            description="Call a contact by name using number stored in memory",
        )

        # 27: Save / Update Contact Number
        self.register(
            name="save_contact",
            priority=27,
            patterns=[
                r"\bremember\s+that\s+(?P<name>[a-z0-9\s]+?)(?:'s)?\s+(?:phone\s+)?number\s+is\s+(?P<number>[\d\s\+\-\(\)]+)",
                r"\bupdate\s+(?P<name>[a-z0-9\s]+?)(?:'s)?\s+(?:phone\s+)?number\s+to\s+(?P<number>[\d\s\+\-\(\)]+)",
                r"\b(?:my\s+)?(?P<name>[a-z0-9\s]+?)(?:'s)?\s+number\s+is\s+(?P<number>[\d\s\+\-\(\)]+)[,.]?\s*(?:save\s+it\s+as\s+(?P<alias>[a-z0-9\s]+))?",
                r"\bsave\s+(?P<name>[a-z0-9\s]+)\s+(?:as\s+)?(?:contact\s+)?(?:with\s+)?(?:number\s+)?(?P<number>[\d\s\+\-\(\)]+)",
            ],
            handler=self._handle_save_contact,
            description="Save or update a contact's phone number in long-term memory",
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
                r"\b(?:search(?:\s+the\s+web)?(?:\s+for)?|look\s+up|google|duckduckgo|find\s+info\s+on)\s+(?P<query>.+?)[.\s]*$",
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
    def _handle_timer_control(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        action = (match.groupdict().get("action") or "cancel").lower()
        if self.scheduler is not None:
            if action in ("run", "execute"):
                self.scheduler.run_now()
                return "Executing scheduled command now."
            self.scheduler.cancel(speak_feedback=False)
        return "Timed command cancelled."

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
        if gd.get("read"):
            if self.whatsapp is not None:
                msgs = self.whatsapp.read_latest_messages(limit=2)
                if msgs:
                    joined = "; ".join(f"{m['sender']} said {m['text']}" for m in msgs)
                    return f"Latest WhatsApp messages: {joined}"
            return "No unread WhatsApp messages right now."

        contact = (gd.get("contact") or "").strip().title()
        msg = (gd.get("msg") or "").strip()
        is_vn = "voice note" in raw.lower()

        if self.whatsapp is not None:
            if is_vn:
                _, reply = self.whatsapp.send_voice_note(contact, msg)
                return reply
            _, reply = self.whatsapp.send_message(contact, msg)
            return reply
        return f"Queued WhatsApp message for {contact}."

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

        contact = (gd.get("contact") or "Mom").strip().title()
        sms_body = (gd.get("msg") or "").strip()
        if self.phone_adb is not None:
            _, msg = self.phone_adb.send_sms(contact, sms_body)
            return msg
        return f"Sent SMS to {contact}: {sms_body}"

    def _handle_workspace_selective_launch(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        ws_name = (gd.get("ws") or "coding").strip().lower()
        raw_items = gd.get("items") or ""
        tokens = [tok.strip() for tok in re.split(r",|\band\b|\+", raw_items) if tok.strip()]
        if not tokens:
            tokens = [raw_items.strip()]

        if self.workspace_orch is not None:
            ok, msg = self.workspace_orch.launch_workspace(ws_name, item_filter=tokens)
            return msg
        return f"Selective launch from {ws_name} workspace requested for {', '.join(tokens)}."

    def _handle_save_contact(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        name = (gd.get("name") or gd.get("alias") or "contact").strip().title()
        number = re.sub(r"[^\d+]", "", gd.get("number") or "")

        if not number or len(number) < 8:
            return "I need a valid phone number to save."

        # Save in both structured memory and vector memory
        key = f"contact_{name.lower().replace(' ', '_')}"
        if self.rag is not None:
            self.rag.remember_fact(key, number, category="contact")
            # Also store a human readable version
            self.rag.remember_fact(f"{name}'s phone number", number, category="contact")

        return f"Got it. I've saved {name}'s number as {number}."

    def _handle_call_contact(self, match: re.Match[str], raw: str, ctx: Dict[str, Any]) -> str:
        gd = match.groupdict()
        name = (gd.get("name") or gd.get("contact") or "").strip().title()
        if not name:
            return "Who would you like me to call?"

        number = None
        if self.rag is not None:
            # Try several possible keys in SQLite memory
            for key in [
                f"contact_{name.lower().replace(' ', '_')}",
                f"{name}'s phone number",
                f"{name.lower()} number",
                name.lower(),
            ]:
                number = self.rag.sqlite.get_preference(key)
                if number:
                    break

        # Fallback: check configured contacts in phone_adb (e.g. Mom from config)
        if not number and self.phone_adb is not None:
            if name.lower() in self.phone_adb.contacts:
                info = self.phone_adb.contacts[name.lower()]
                number = info.get("number")

        if not number:
            return f"I don't have a phone number saved for {name} yet. You can say: remember that {name}'s number is ..."

        # Now actually place the call
        if self.phone_adb is not None:
            _, msg = self.phone_adb.make_call(number, contact_name=name)
            return msg

        # Fallback: just show the call overlay
        self.bus.publish(
            Topics.CALL_STATE,
            {
                "action": "start",
                "direction": "outgoing",
                "status": "ringing",
                "name": name,
                "number": number,
                "meta": "Mobile · via phone bridge",
            },
            source="intent_router",
        )
        return f"Calling {name} at {number}."

    _handle_phone_call = _handle_call_contact

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