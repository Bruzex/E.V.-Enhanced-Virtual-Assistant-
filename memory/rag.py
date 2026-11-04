#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Personal Knowledge Base, RAG & Self-Learning Engine (`rag.py`)
==================================================================================================
Provides:
  1. Personal Profile & Contact Intelligence:
     - `set_my_profile_field(field, value)`, `get_my_profile_field(field)`, `get_full_profile()`
     - `add_or_update_contact(name, **kwargs)`, `get_contact(name)`, `search_contacts(query)`
     - Contiguous digit normalization for phone/mobile/WhatsApp numbers (`9582198165`).
     - Automatic resolution of Indian family names (`"mummy"`, `"mommy"`, `"mammi"`, `"mother"` -> `Mom`).
  2. Active Self-Learning & Correction Memory:
     - Automatically learns when the user corrects E.V. or teaches a rule/habit.
  3. Safe RAG Context Injection (`build_context_for_prompt`):
     - Injects relevant learned facts, rules, and preferences into `Brain` while keeping
       `"sir"` as the default form of address.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from memory.chroma_store import ChromaMemoryStore
from memory.sqlite_store import ContactRecord, SQLiteMemoryStore
from voice.stt import convert_spoken_numbers, join_spoken_digits


def normalize_phone_digits(raw_val: str) -> str:
    """Convert spoken digit words and collapse spaces between digits in phone numbers."""
    if not raw_val:
        return ""
    joined = convert_spoken_numbers(join_spoken_digits(raw_val.strip()))
    collapsed = re.sub(r"(?<=\d)\s+(?=\d)", "", joined)
    return collapsed.strip()


AUTO_LEARN_PATTERNS = [
    (re.compile(r"^(?:remember\s+that\s+|actually\s+)?my\s+(?:full\s+)?name\s+is\s+(?P<val>[a-zA-Z0-9\s]{2,40})$", re.I), "full_name"),
    (re.compile(r"^(?:remember\s+that\s+|from\s+now\s+on\s+)?(?:my\s+nickname\s+is|call\s+me)\s+(?P<val>[a-zA-Z0-9\s]{2,25})$", re.I), "nickname"),
    (re.compile(r"^(?:remember\s+that\s+|actually\s+)?my\s+(?:mobile|phone|cell)(?:\s+number)?\s+is\s+(?P<val>[a-zA-Z0-9\+\s\-\(\)]{5,65})$", re.I), "mobile"),
    (re.compile(r"^(?:remember\s+that\s+|actually\s+)?my\s+email(?:\s+address)?\s+is\s+(?P<val>[^\s]+@[^\s]+)$", re.I), "email"),
    (re.compile(r"^(?:remember\s+that\s+|actually\s+)?my\s+instagram(?:\s+username|\s+handle|\s+id)?\s+is\s+@?(?P<val>[a-zA-Z0-9._]{2,35})$", re.I), "instagram"),
    (re.compile(r"^(?:remember\s+that\s+|actually\s+)?my\s+(?:birthday|date\s+of\s+birth|dob)\s+is\s+(?P<val>[a-zA-Z0-9\s,\-/]{3,35})$", re.I), "birthday"),
    (re.compile(r"^(?:remember\s+that\s+|actually\s+)?my\s+age\s+is\s+(?P<val>\d{1,3})$", re.I), "age"),
    (re.compile(r"^(?:remember\s+that\s+|actually\s+)?i\s+live\s+in\s+(?P<val>[a-zA-Z0-9\s,]{2,50})$", re.I), "address"),
    (re.compile(r"^(?:remember\s+that\s+|actually\s+)?my\s+(?:home\s+)?address\s+is\s+(?P<val>[a-zA-Z0-9\s,\-#]{3,70})$", re.I), "address"),
    (re.compile(r"^(?:remember\s+that\s+|actually\s+)?my\s+(?P<key>[a-zA-Z0-9\s]{2,25}?)\s+is\s+(?P<val>[^.?!]{2,60})$", re.I), None),
]


class RAGMemoryManager:
    """
    Centralized Personal Companion Knowledge Base, RAG & Self-Learning Manager.
    """

    def __init__(self, config: Dict[str, Any], base_dir: Path) -> None:
        self.config = config
        self.base_dir = base_dir
        self.logger = logging.getLogger("ev.rag")

        self.enabled: bool = bool(config.get("memory", {}).get("enabled", True))
        self.sqlite = SQLiteMemoryStore(config=config, base_dir=base_dir)
        self.chroma = ChromaMemoryStore(config=config, base_dir=base_dir)

    # =========================================================================
    # High-Level Personal Profile API
    # =========================================================================
    def set_my_profile_field(self, field: str, value: str) -> str:
        """Store or update a personal profile field in SQLite (`user_profile`)."""
        norm_field = self.sqlite.normalize_profile_key(field)
        clean_v = (value or "").strip()
        if norm_field == "mobile":
            clean_v = normalize_phone_digits(clean_v)

        norm = self.sqlite.set_profile_field(field, clean_v)
        if not norm:
            return "Please provide a valid profile field and value."
        self.sqlite.set_preference(f"your {norm}", clean_v, category="user_profile")
        self.sqlite.add_learned_knowledge(f"profile:{norm}", f"User's {norm.replace('_', ' ')} is {clean_v}", source_type="fact")
        self.chroma.add_memory(f"User profile {norm} is {clean_v}", metadata={"key": norm})
        friendly = norm.replace("_", " ").replace("full name", "name")
        if norm == "instagram" and not clean_v.startswith("@"):
            return f"Got it, sir. I've saved your Instagram as @{clean_v}."
        if norm == "nickname":
            return f"Got it. From now on, I'll call you {clean_v}."
        return f"Got it, sir. I've saved your {friendly} as {clean_v}."

    def get_my_profile_field(self, field: str) -> Optional[str]:
        return self.sqlite.get_profile_field(field)

    def get_full_profile(self) -> Dict[str, str]:
        return self.sqlite.get_full_profile()

    def get_preferred_user_name(self) -> str:
        """Return 'sir' by default, or custom nickname if explicitly set."""
        prof = self.get_full_profile()
        if prof.get("nickname"):
            return prof["nickname"].strip()
        return "sir"

    def query_my_profile(self, field_query: str) -> str:
        norm = self.sqlite.normalize_profile_key(field_query)
        val = self.get_my_profile_field(norm)

        friendly_labels = {
            "full_name": "name",
            "nickname": "nickname",
            "age": "age",
            "birthday": "birthday",
            "mobile": "phone number",
            "email": "email address",
            "address": "address",
            "instagram": "Instagram",
            "preferences": "preferences",
        }
        label = friendly_labels.get(norm, field_query.replace("_", " ").strip())

        if val:
            if norm == "instagram":
                handle = val if val.startswith("@") else f"@{val}"
                return f"Your Instagram is {handle}, sir."
            return f"Your {label} is {val}, sir."

        return f"I don't have your {label} saved yet, sir. You can tell me by saying 'My {label} is ...'."

    # =========================================================================
    # High-Level Contacts API (`contacts`)
    # =========================================================================
    def add_or_update_contact(self, name: str, **kwargs: Any) -> ContactRecord:
        for num_key in ("phone", "mobile_number", "whatsapp", "whatsapp_number"):
            if num_key in kwargs and kwargs[num_key]:
                kwargs[num_key] = normalize_phone_digits(str(kwargs[num_key]))
        rec = self.sqlite.add_or_update_contact(name, **kwargs)
        summary = f"Contact {rec.name} ({rec.relationship or 'contact'})"
        if rec.phone:
            summary += f" phone {rec.phone}"
        if rec.instagram:
            summary += f" instagram @{rec.instagram}"
        self.chroma.add_memory(summary, metadata={"contact": rec.name})
        return rec

    def get_contact(self, name: str) -> Optional[ContactRecord]:
        return self.sqlite.get_contact(name)

    def search_contacts(self, query: str) -> List[ContactRecord]:
        return self.sqlite.search_contacts(query)

    def list_contacts(self, relationship: Optional[str] = None) -> List[ContactRecord]:
        return self.sqlite.list_contacts(relationship=relationship)

    def resolve_contact_or_disambiguate(
        self,
        person_query: str,
    ) -> Tuple[Optional[ContactRecord], Optional[str]]:
        clean_q = re.sub(r"^(?:my|to|for)\s+", "", (person_query or "").strip(), flags=re.I).strip()
        matches = self.search_contacts(clean_q)
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            names = ", ".join(f"{c.name} ({c.relationship or 'contact'})" for c in matches[:4])
            return None, f"I found multiple contacts matching '{clean_q}': {names}. Which one did you mean, sir?"
        return None, f"I don't have {clean_q} saved in your contacts yet, sir."

    def describe_person(self, person_query: str) -> str:
        contact, err = self.resolve_contact_or_disambiguate(person_query)
        if err:
            return err
        assert contact is not None

        head = (
            f"{contact.name} is your {contact.relationship}"
            if contact.relationship
            else f"{contact.name} is in your contacts"
        )
        if contact.nickname and contact.nickname.lower() != contact.name.lower():
            head += f" (nickname {contact.nickname})"

        details: List[str] = []
        if contact.phone:
            details.append(f"phone number {contact.phone}")
        if contact.whatsapp and contact.whatsapp != contact.phone:
            details.append(f"WhatsApp {contact.whatsapp}")
        if contact.instagram:
            details.append(f"Instagram @{contact.instagram.lstrip('@')}")
        if contact.birthday:
            details.append(f"birthday {contact.birthday}")
        if contact.notes:
            details.append(contact.notes)

        if details:
            return f"{head}. Saved details: {', '.join(details)}."
        return f"{head}."

    def query_contact_attribute(self, person_query: str, attribute: str) -> str:
        contact, err = self.resolve_contact_or_disambiguate(person_query)
        if err:
            return f"I don't have {person_query} saved in your contacts yet, sir."
        assert contact is not None

        attr_low = attribute.lower()
        if "whatsapp" in attr_low:
            num = contact.whatsapp or contact.phone
            return (
                f"{contact.name}'s WhatsApp number is {num}."
                if num
                else f"I don't have a WhatsApp number saved for {contact.name} yet, sir."
            )
        if any(w in attr_low for w in ("phone", "mobile", "number", "cell")):
            num = contact.phone or contact.whatsapp
            return (
                f"{contact.name}'s phone number is {num}."
                if num
                else f"I don't have a phone number saved for {contact.name} yet, sir."
            )
        if any(w in attr_low for w in ("instagram", "insta")):
            ig = contact.instagram
            return (
                f"{contact.name}'s Instagram is @{ig.lstrip('@')}."
                if ig
                else f"I don't have an Instagram username saved for {contact.name} yet, sir."
            )
        if any(w in attr_low for w in ("birthday", "birth", "dob")):
            return (
                f"{contact.name}'s birthday is {contact.birthday}."
                if contact.birthday
                else f"I don't have {contact.name}'s birthday saved yet, sir."
            )
        return self.describe_person(person_query)

    # =========================================================================
    # Message History & Natural Self-Learning Engine
    # =========================================================================
    def record_sent_message(self, contact_name: str, platform: str, message_text: str) -> None:
        self.sqlite.log_message(contact_name, platform=platform, message_text=message_text, direction="outgoing")

    def query_last_sent_message(self, person_query: str, platform: Optional[str] = None) -> str:
        contact = self.get_contact(person_query)
        target_name = contact.name if contact else person_query.strip().title()
        row = self.sqlite.get_last_message(target_name, platform=platform, direction="outgoing")
        if row:
            plat = str(row["platform"]).title()
            msg = str(row["message_text"])
            return f"The last message you sent to {row['contact_name']} on {plat} was: '{msg}'."
        return f"I don't have any recorded messages sent to {target_name} yet, sir."

    def learn_rule_or_correction(self, statement: str, source_type: str = "rule") -> str:
        clean = (statement or "").strip().rstrip(".?!")
        if not clean:
            return "Tell me what you'd like me to learn or update, sir."

        m_call_me = re.search(r"\bcall\s+me\s+([a-zA-Z0-9\s]{2,25})$", clean, re.I)
        if m_call_me:
            nick = m_call_me.group(1).strip()
            return self.set_my_profile_field("nickname", nick)

        m_dont = re.match(r"^(?:from\s+now\s+on\s+)?(?:please\s+)?(?:don't|do\s+not|never)\s+(.+)$", clean, re.I)
        if m_dont:
            avoid_rule = m_dont.group(1).strip()
            topic_key = f"avoid:{avoid_rule[:32].lower()}"
            self.sqlite.set_preference(topic_key, f"Do not {avoid_rule}", category="negative_preference")
            self.sqlite.add_learned_knowledge(topic_key, f"User preference: Do NOT {avoid_rule}", source_type="preference")
            self.chroma.add_memory(f"User preference: never {avoid_rule}", metadata={"type": "negative_preference"})
            return f"Understood, sir. I've noted your preference and won't {avoid_rule} going forward."

        core_stmt = re.sub(
            r"^(?:no[\s,]+that(?:'s|\s+is)\s+(?:wrong|incorrect)[\s,.:;-]*|actually[\s,]+|update\s+your\s+knowledge[\s,.:;-]*|from\s+now\s+on[\s,.:;-]*)+",
            "",
            clean,
            flags=re.I,
        ).strip()

        structured = self.auto_learn_from_statement(core_stmt)
        if structured:
            self.sqlite.add_learned_knowledge(core_stmt[:32], core_stmt, source_type=source_type)
            return structured

        m_kv = re.match(r"^(?:remember\s+that\s+)?(?P<k>[^=]{2,40}?)\s+is\s+(?P<v>.+)$", core_stmt, re.I)
        if m_kv:
            k = m_kv.group("k").strip()
            v = m_kv.group("v").strip()
            self.sqlite.add_learned_knowledge(k, f"{k} is {v}", source_type=source_type)
            return self.remember_fact(k, v, category=source_type)

        topic = core_stmt[:36].lower()
        self.sqlite.add_learned_knowledge(topic, core_stmt, source_type=source_type)
        self.sqlite.set_preference(f"rule:{topic}", core_stmt, category=source_type)
        self.chroma.add_memory(f"Learned from user ({source_type}): {core_stmt}", metadata={"type": source_type})
        return f"Got it, sir. I've updated my memory: {core_stmt}."

    def summarize_learned_about_user(self) -> str:
        prof = self.get_full_profile()
        contacts = self.list_contacts()
        learned = self.sqlite.list_learned_knowledge(limit=8)
        prefs = self.sqlite.list_preferences(limit=8)

        parts: List[str] = []
        if prof.get("full_name"):
            name_str = f"your name is {prof['full_name']}"
            if prof.get("nickname"):
                name_str += f" (and you like to be called {prof['nickname']})"
            parts.append(name_str)
        elif prof.get("nickname"):
            parts.append(f"you like to be called {prof['nickname']}")

        if prof.get("mobile"):
            parts.append(f"your mobile number is {prof['mobile']}")
        if prof.get("instagram"):
            ig = prof["instagram"] if prof["instagram"].startswith("@") else f"@{prof['instagram']}"
            parts.append(f"your Instagram is {ig}")
        if prof.get("birthday"):
            parts.append(f"your birthday is {prof['birthday']}")
        if prof.get("address"):
            parts.append(f"you live in {prof['address']}")

        if contacts:
            c_names = ", ".join(
                f"{c.name} ({c.relationship})" if c.relationship else c.name
                for c in contacts[:4]
            )
            parts.append(f"your saved contacts include {c_names}")

        custom_facts: List[str] = []
        for item in learned:
            ft = str(item.get("fact_text", "")).strip()
            if ft and not ft.lower().startswith("user's ") and ft not in custom_facts:
                custom_facts.append(ft)
        for k, v in prefs.items():
            if not k.startswith("your ") and not k.startswith("rule:") and not k.startswith("avoid:"):
                entry = f"{k} is {v}"
                if entry not in custom_facts:
                    custom_facts.append(entry)

        if custom_facts:
            parts.append("I've also learned that " + "; ".join(custom_facts[:4]))

        if not parts:
            return (
                "I haven't stored many personal details about you yet, sir. "
                "You can teach me anytime by saying 'Remember that...', 'From now on...', or 'My name is...'."
            )

        return "Here is what I know about you, sir: " + ", ".join(parts) + "."

    def remember_fact(self, key: str, value: str, category: str = "user_note") -> str:
        clean_k = (key or "note").strip()
        clean_v = (value or "").strip()
        if not clean_v:
            return "Please tell me what you'd like me to remember, sir."

        m_contact = re.match(
            r"^(?P<person>[a-zA-Z0-9\s]+?)'s\s+(?P<attr>whatsapp(?:\s+number)?|phone(?:\s+number)?|mobile(?:\s+number)?|number|instagram(?:\s+username)?|birthday)$",
            clean_k,
            re.I,
        )
        if m_contact:
            attr_str = m_contact.group("attr").strip()
            if any(w in attr_str.lower() for w in ("phone", "mobile", "whatsapp", "number")):
                clean_v = normalize_phone_digits(clean_v)
            _, msg = self.sqlite.update_contact_field(
                m_contact.group("person").strip(),
                attr_str,
                clean_v,
            )
            return msg

        if clean_k.lower().startswith("my "):
            field_part = clean_k[3:].strip()
            norm_prof = self.sqlite.normalize_profile_key(field_part)
            if norm_prof in (
                "full_name",
                "nickname",
                "age",
                "birthday",
                "mobile",
                "email",
                "address",
                "instagram",
                "preferences",
            ):
                return self.set_my_profile_field(norm_prof, clean_v)

        low_k = clean_k.lower()
        self.sqlite.set_preference(low_k, clean_v, category=category)
        self.sqlite.add_learned_knowledge(low_k, f"{clean_k} is {clean_v}", source_type="fact")
        self.chroma.add_memory(f"{low_k} is {clean_v}", metadata={"key": low_k, "category": category})
        return f"Got it, sir. I'll remember that {clean_k} is {clean_v}."

    def auto_learn_from_statement(self, user_text: str) -> Optional[str]:
        if not self.enabled:
            return None
        cleaned = (user_text or "").strip().rstrip(".?!")
        low = cleaned.lower()
        if low.startswith(("what ", "who ", "where ", "when ", "why ", "how ", "is ", "can ", "do you ", "are you ")):
            return None

        if re.match(
            r"^(?:no[\s,]+that(?:'s|\s+is)\s+(?:wrong|incorrect)|from\s+now\s+on\b|update\s+your\s+knowledge\b|actually\s+remember\b)",
            low,
        ):
            return self.learn_rule_or_correction(cleaned, source_type="correction")

        # 1. Compound contact teaching: "Arin is my friend, his number is +91 9876543210"
        m_compound = re.search(
            r"^(?:remember\s+that\s+|actually\s+)?(?P<person>[a-zA-Z0-9]+)\s+is\s+my\s+(?P<rel>friend|best\s+friend|brother|sister|mother|father|mom|mummy|dad|colleague|cousin)"
            r"(?:[\s,;]+(?:and\s+)?(?:his|her|their)\s+(?P<attr>phone\s+number|mobile\s+number|whatsapp\s+number|number|phone|whatsapp|instagram)\s+is\s+(?P<val>.+))?$",
            cleaned,
            re.I,
        )
        if m_compound:
            person = m_compound.group("person").strip().title()
            rel = m_compound.group("rel").strip().lower()
            attr = (m_compound.group("attr") or "").strip().lower()
            val = (m_compound.group("val") or "").strip()

            kwargs: Dict[str, Any] = {"relationship": rel}
            if val:
                if "instagram" in attr:
                    kwargs["instagram"] = val.lstrip("@")
                elif "whatsapp" in attr:
                    norm_num = normalize_phone_digits(val)
                    kwargs["whatsapp"] = norm_num
                    kwargs["phone"] = norm_num
                    val = norm_num
                else:
                    norm_num = normalize_phone_digits(val)
                    kwargs["phone"] = norm_num
                    kwargs["whatsapp"] = norm_num
                    val = norm_num

            rec = self.add_or_update_contact(person, **kwargs)
            if val:
                return f"Got it, sir. I've saved {rec.name} as your {rel} with {attr} {val}."
            return f"Got it, sir. I've saved {rec.name} in your contacts as your {rel}."

        # 2. Contact attribute update
        m_update = re.search(
            r"^(?:update|set|change|remember\s+that|actually)\s+(?P<person>[a-zA-Z0-9\s]+?)'s\s+"
            r"(?P<attr>phone\s+number|mobile\s+number|whatsapp\s+number|number|phone|whatsapp|instagram(?:\s+username)?|birthday)"
            r"\s+(?:to|is|as)\s+(?P<val>.+)$",
            cleaned,
            re.I,
        )
        if m_update:
            attr_s = m_update.group("attr").strip()
            val_s = m_update.group("val").strip()
            if any(w in attr_s.lower() for w in ("phone", "mobile", "whatsapp", "number")):
                val_s = normalize_phone_digits(val_s)
            _, msg = self.sqlite.update_contact_field(
                m_update.group("person").strip(),
                attr_s,
                val_s,
            )
            return msg

        # 3. Direct contact possessive statement
        m_cattr = re.search(
            r"^(?P<person>[a-zA-Z0-9]+)'s\s+(?P<attr>phone\s+number|mobile\s+number|whatsapp\s+number|number|instagram|birthday)\s+is\s+(?P<val>.+)$",
            cleaned,
            re.I,
        )
        if m_cattr:
            attr_s = m_cattr.group("attr").strip()
            val_s = m_cattr.group("val").strip()
            if any(w in attr_s.lower() for w in ("phone", "mobile", "whatsapp", "number")):
                val_s = normalize_phone_digits(val_s)
            _, msg = self.sqlite.update_contact_field(
                m_cattr.group("person").strip(),
                attr_s,
                val_s,
            )
            return msg

        # 4. Self profile teaching patterns
        for pattern, profile_field in AUTO_LEARN_PATTERNS:
            m = pattern.search(cleaned)
            if m:
                gd = m.groupdict()
                val = gd.get("val", "").strip()
                if not val or len(val) < 2:
                    continue
                if profile_field:
                    return self.set_my_profile_field(profile_field, val)

                raw_key = (gd.get("key") or "note").strip().lower()
                norm_prof = self.sqlite.normalize_profile_key(raw_key)
                if norm_prof in ("full_name", "nickname", "age", "birthday", "mobile", "email", "address", "instagram"):
                    return self.set_my_profile_field(norm_prof, val)

                key = f"your {raw_key}"
                self.sqlite.set_preference(key, val, category="auto_learned")
                self.sqlite.add_learned_knowledge(raw_key, f"User's {raw_key} is {val}", source_type="fact")
                self.chroma.add_memory(f"{key} is {val}", metadata={"key": key, "category": "auto_learned"})
                return f"I've noted that your {raw_key} is {val}, sir."
        return None

    def find_direct_fact_answer(self, question: str) -> Optional[str]:
        if not self.enabled:
            return None
        cleaned = re.sub(r"[^a-z0-9\s]", " ", (question or "").lower())
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        m = re.search(r"\b(?:what|who|where|when)\s+is\s+my\s+([a-z0-9\s]+)$", cleaned)
        if m:
            subject = m.group(1).strip()
            prof_val = self.get_my_profile_field(subject)
            if prof_val:
                return self.query_my_profile(subject)
            for candidate_key in (f"your {subject}", f"my {subject}", subject):
                val = self.sqlite.get_preference(candidate_key)
                if val:
                    return f"Your {subject} is {val}, sir."

        matches = self.sqlite.search_facts(cleaned, limit=1)
        if matches and any(w in cleaned for w in ("my ", "remember", "told you", "learned")):
            k, v = matches[0]
            return f"You mentioned earlier that {k} is {v}, sir."
        return None

    def recall(self, query: str) -> Optional[str]:
        direct = self.find_direct_fact_answer(query)
        if direct:
            return direct

        learned_matches = self.sqlite.search_learned_knowledge(query, limit=3)
        if learned_matches:
            joined = "; ".join(str(m["fact_text"]) for m in learned_matches)
            return f"From what you taught me: {joined}."

        sql_matches = self.sqlite.search_facts(query, limit=3)
        if sql_matches:
            formatted = "; ".join(f"{k} is {v}" for k, v in sql_matches)
            return f"Here is what I have in memory: {formatted}."

        vec_matches = self.chroma.query_similar(query, top_k=2)
        if vec_matches:
            return f"From our past conversations: {' '.join(vec_matches)}."
        return None

    def recall_conversational(self, query: str) -> Optional[str]:
        direct = self.recall(query)
        if direct:
            return direct

        turns = self.sqlite.recent_turns(limit=3)
        if turns:
            last = turns[-1]
            return (
                f"Earlier you asked '{last.get('user_text', '')}', "
                f"and I replied '{last.get('assistant_text', '')}'."
            )
        return None

    def build_context_for_prompt(self, user_prompt: str) -> str:
        """
        Build a concise RAG context block injected into the local LLM system prompt
        containing relevant learned facts, user habits, and preferences.
        """
        if not self.enabled:
            return ""

        lines: List[str] = []
        prof = self.get_full_profile()
        preferred_address = prof.get("nickname") or "sir"
        lines.append(
            f"- Address the user as '{preferred_address}' by default. Only say their full name if they explicitly ask who they are."
        )

        neg_prefs = self.sqlite.list_preferences(limit=5, category="negative_preference")
        for _, rule_text in neg_prefs.items():
            lines.append(f"- User constraint: {rule_text}")

        for item in self.sqlite.search_learned_knowledge(user_prompt, limit=3):
            lines.append(f"- Learned knowledge ({item['source_type']}): {item['fact_text']}")

        for k, v in self.sqlite.search_facts(user_prompt, limit=3):
            lines.append(f"- User fact ({k}): {v}")

        for snippet in self.chroma.query_similar(user_prompt, top_k=2):
            entry = f"- Past memory: {snippet}"
            if entry not in lines:
                lines.append(entry)

        return "\n".join(lines)

    def record_interaction(
        self,
        user_text: str,
        assistant_text: str,
        intent_name: str = "unknown",
    ) -> None:
        if not self.enabled:
            return
        u_clean = (user_text or "").strip()
        a_clean = (assistant_text or "").strip()
        if not u_clean or not a_clean:
            return

        self.sqlite.log_turn(u_clean, a_clean, intent_name=intent_name)
        self.auto_learn_from_statement(u_clean)

        if intent_name not in ("empty", "wake_acknowledge", "continue_speech", "interrupt_speech") and len(u_clean) >= 6:
            self.chroma.add_memory(
                f"User said: '{u_clean}'. E.V. responded: '{a_clean}'.",
                metadata={"intent": intent_name},
            )