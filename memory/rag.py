#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — High-Level Personal Knowledge Base & RAG API
================================================================================
Provides the high-level Personal Companion API requested in specification:
  - `set_my_profile_field(field, value)`
  - `get_my_profile_field(field)`
  - `get_full_profile()`
  - `add_or_update_contact(name, **kwargs)`
  - `get_contact(name)`
  - `search_contacts(query)`
  - `list_contacts(relationship=None)`
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from memory.chroma_store import ChromaMemoryStore
from memory.sqlite_store import ContactRecord, SQLiteMemoryStore


AUTO_LEARN_PATTERNS = [
    (re.compile(r"^(?:remember\s+that\s+)?my\s+(?:full\s+)?name\s+is\s+(?P<val>[a-zA-Z0-9\s]{2,40})$", re.I), "full_name"),
    (re.compile(r"^(?:remember\s+that\s+)?my\s+nickname\s+is\s+(?P<val>[a-zA-Z0-9\s]{2,25})$", re.I), "nickname"),
    (re.compile(r"^(?:remember\s+that\s+)?my\s+(?:mobile|phone|cell)(?:\s+number)?\s+is\s+(?P<val>[\+\d\s\-\(\)]{5,25})$", re.I), "mobile"),
    (re.compile(r"^(?:remember\s+that\s+)?my\s+email(?:\s+address)?\s+is\s+(?P<val>[^\s]+@[^\s]+)$", re.I), "email"),
    (re.compile(r"^(?:remember\s+that\s+)?my\s+instagram(?:\s+username|\s+handle|\s+id)?\s+is\s+@?(?P<val>[a-zA-Z0-9._]{2,35})$", re.I), "instagram"),
    (re.compile(r"^(?:remember\s+that\s+)?my\s+(?:birthday|date\s+of\s+birth|dob)\s+is\s+(?P<val>[a-zA-Z0-9\s,\-/]{3,35})$", re.I), "birthday"),
    (re.compile(r"^(?:remember\s+that\s+)?my\s+age\s+is\s+(?P<val>\d{1,3})$", re.I), "age"),
    (re.compile(r"^(?:remember\s+that\s+)?i\s+live\s+in\s+(?P<val>[a-zA-Z0-9\s,]{2,50})$", re.I), "address"),
    (re.compile(r"^(?:remember\s+that\s+)?my\s+(?:home\s+)?address\s+is\s+(?P<val>[a-zA-Z0-9\s,\-#]{3,70})$", re.I), "address"),
    (re.compile(r"^(?:remember\s+that\s+)?my\s+(?P<key>[a-zA-Z0-9\s]{2,25}?)\s+is\s+(?P<val>[^.?!]{2,60})$", re.I), None),
]


class RAGMemoryManager:
    """
    Centralized Personal Companion Knowledge Base & RAG Manager.
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
        norm = self.sqlite.set_profile_field(field, value)
        if not norm:
            return "Please provide a valid profile field and value."
        clean_v = value.strip()
        self.sqlite.set_preference(f"your {norm}", clean_v, category="user_profile")
        self.chroma.add_memory(f"User profile {norm} is {clean_v}", metadata={"key": norm})
        friendly = norm.replace("_", " ").replace("full name", "name")
        if norm == "instagram" and not clean_v.startswith("@"):
            return f"Got it. I've saved your Instagram as @{clean_v}."
        return f"Got it. I've saved your {friendly} as {clean_v}."

    def get_my_profile_field(self, field: str) -> Optional[str]:
        """Retrieve a raw personal profile field value from SQLite (`user_profile`)."""
        return self.sqlite.get_profile_field(field)

    def get_full_profile(self) -> Dict[str, str]:
        """Return all stored personal profile fields as a dictionary."""
        return self.sqlite.get_full_profile()

    def query_my_profile(self, field_query: str) -> str:
        """Format a natural spoken answer for 'What's my Instagram?', 'What's my phone number?', etc."""
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
                return f"Your Instagram is {handle}."
            return f"Your {label} is {val}."

        return f"I don't have your {label} saved yet. You can tell me by saying 'My {label} is ...'."

    # =========================================================================
    # High-Level Contacts API (`contacts`)
    # =========================================================================
    def add_or_update_contact(self, name: str, **kwargs: Any) -> ContactRecord:
        """Add or update a friend, family member, or colleague in the `contacts` table."""
        rec = self.sqlite.add_or_update_contact(name, **kwargs)
        summary = f"Contact {rec.name} ({rec.relationship or 'contact'})"
        if rec.phone:
            summary += f" phone {rec.phone}"
        if rec.instagram:
            summary += f" instagram @{rec.instagram}"
        self.chroma.add_memory(summary, metadata={"contact": rec.name})
        return rec

    def get_contact(self, name: str) -> Optional[ContactRecord]:
        """Retrieve a single contact by name, nickname, or relationship ('Mom', 'Arin')."""
        return self.sqlite.get_contact(name)

    def search_contacts(self, query: str) -> List[ContactRecord]:
        """Search contacts by name, nickname, or relationship."""
        return self.sqlite.search_contacts(query)

    def list_contacts(self, relationship: Optional[str] = None) -> List[ContactRecord]:
        """List all saved contacts, optionally filtered by relationship."""
        return self.sqlite.list_contacts(relationship=relationship)

    def resolve_contact_or_disambiguate(
        self,
        person_query: str,
    ) -> Tuple[Optional[ContactRecord], Optional[str]]:
        matches = self.search_contacts(person_query)
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            names = ", ".join(f"{c.name} ({c.relationship or 'contact'})" for c in matches[:4])
            return None, f"I found multiple contacts matching '{person_query}': {names}. Which one did you mean?"
        return None, f"I don't have {person_query} saved in your contacts yet."

    def describe_person(self, person_query: str) -> str:
        """Format a natural spoken response for 'Who is Arin?'."""
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
        """Answer 'What's Arin's phone number?', 'What's Mom's number?', 'What is Arin's Instagram?'."""
        contact, err = self.resolve_contact_or_disambiguate(person_query)
        if err:
            return f"I don't have {person_query} saved in your contacts yet."
        assert contact is not None

        attr_low = attribute.lower()
        if "whatsapp" in attr_low:
            num = contact.whatsapp or contact.phone
            return (
                f"{contact.name}'s WhatsApp number is {num}."
                if num
                else f"I don't have a WhatsApp number saved for {contact.name} yet."
            )
        if any(w in attr_low for w in ("phone", "mobile", "number", "cell")):
            num = contact.phone or contact.whatsapp
            return (
                f"{contact.name}'s phone number is {num}."
                if num
                else f"I don't have a phone number saved for {contact.name} yet."
            )
        if any(w in attr_low for w in ("instagram", "insta")):
            ig = contact.instagram
            return (
                f"{contact.name}'s Instagram is @{ig.lstrip('@')}."
                if ig
                else f"I don't have an Instagram username saved for {contact.name} yet."
            )
        if any(w in attr_low for w in ("birthday", "birth", "dob")):
            return (
                f"{contact.name}'s birthday is {contact.birthday}."
                if contact.birthday
                else f"I don't have {contact.name}'s birthday saved yet."
            )
        return self.describe_person(person_query)

    # =========================================================================
    # Message History & Natural Fact Teaching
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
        return f"I don't have any recorded messages sent to {target_name} yet."

    def remember_fact(self, key: str, value: str, category: str = "user_note") -> str:
        clean_k = (key or "note").strip()
        clean_v = (value or "").strip()
        if not clean_v:
            return "Please tell me what you'd like me to remember."

        # Check if teaching a contact's attribute: "Remember that Mom's WhatsApp number is +91..."
        m_contact = re.match(
            r"^(?P<person>[a-zA-Z0-9\s]+?)'s\s+(?P<attr>whatsapp(?:\s+number)?|phone(?:\s+number)?|mobile(?:\s+number)?|number|instagram(?:\s+username)?|birthday)$",
            clean_k,
            re.I,
        )
        if m_contact:
            _, msg = self.sqlite.update_contact_field(
                m_contact.group("person").strip(),
                m_contact.group("attr").strip(),
                clean_v,
            )
            return msg

        # Check if teaching own profile field: "Remember that my name is Aditya Raj"
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
        self.chroma.add_memory(f"{low_k} is {clean_v}", metadata={"key": low_k, "category": category})
        return f"Got it. I'll remember that {clean_k} is {clean_v}."

    def auto_learn_from_statement(self, user_text: str) -> Optional[str]:
        """
        Parse natural teaching statements for both self profile and contacts:
          - "Arin is my friend, his number is +91 9876543210"
          - "Remember that Mom's WhatsApp number is +91 9999988888"
          - "Update Arin's Instagram to @arin_dev"
          - "My Instagram is @bruprog"
          - "Remember that my name is Aditya Raj"
        """
        if not self.enabled:
            return None
        cleaned = (user_text or "").strip().rstrip(".?!")
        low = cleaned.lower()
        if low.startswith(("what ", "who ", "where ", "when ", "why ", "how ", "is ", "can ", "do ")):
            return None

        # 1. Compound contact teaching: "Arin is my friend, his number is +91 9876543210"
        m_compound = re.search(
            r"^(?:remember\s+that\s+)?(?P<person>[a-zA-Z0-9]+)\s+is\s+my\s+(?P<rel>friend|best\s+friend|brother|sister|mother|father|mom|dad|colleague|cousin)"
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
                    kwargs["whatsapp"] = val
                    kwargs["phone"] = val
                else:
                    kwargs["phone"] = val
                    kwargs["whatsapp"] = val

            rec = self.add_or_update_contact(person, **kwargs)
            if val:
                return f"Got it. I've saved {rec.name} as your {rel} with {attr} {val}."
            return f"Got it. I've saved {rec.name} in your contacts as your {rel}."

        # 2. Contact attribute update: "Update Arin's Instagram to @arin_dev" / "Remember that Mom's WhatsApp number is ..."
        m_update = re.search(
            r"^(?:update|set|change|remember\s+that)\s+(?P<person>[a-zA-Z0-9\s]+?)'s\s+"
            r"(?P<attr>phone\s+number|mobile\s+number|whatsapp\s+number|number|phone|whatsapp|instagram(?:\s+username)?|birthday)"
            r"\s+(?:to|is|as)\s+(?P<val>.+)$",
            cleaned,
            re.I,
        )
        if m_update:
            _, msg = self.sqlite.update_contact_field(
                m_update.group("person").strip(),
                m_update.group("attr").strip(),
                m_update.group("val").strip(),
            )
            return msg

        # 3. Direct contact possessive statement: "Arin's phone number is ..."
        m_cattr = re.search(
            r"^(?P<person>[a-zA-Z0-9]+)'s\s+(?P<attr>phone\s+number|mobile\s+number|whatsapp\s+number|number|instagram|birthday)\s+is\s+(?P<val>.+)$",
            cleaned,
            re.I,
        )
        if m_cattr:
            _, msg = self.sqlite.update_contact_field(
                m_cattr.group("person").strip(),
                m_cattr.group("attr").strip(),
                m_cattr.group("val").strip(),
            )
            return msg

        # 4. Self profile teaching patterns ("Remember that my name is Aditya Raj", "My Instagram is @bruprog")
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
                self.chroma.add_memory(f"{key} is {val}", metadata={"key": key, "category": "auto_learned"})
                return f"I've noted that your {raw_key} is {val}."
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
                    return f"Your {subject} is {val}."

        matches = self.sqlite.search_facts(cleaned, limit=1)
        if matches and any(w in cleaned for w in ("my ", "remember", "told you")):
            k, v = matches[0]
            return f"You mentioned earlier that {k} is {v}."
        return None

    def recall(self, query: str) -> Optional[str]:
        direct = self.find_direct_fact_answer(query)
        if direct:
            return direct

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
        if not self.enabled:
            return ""

        lines: List[str] = []
        prof = self.get_full_profile()
        if prof.get("full_name"):
            lines.append(f"- User Name: {prof['full_name']} (Nickname: {prof.get('nickname', '')})")

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

        if intent_name not in ("empty", "continue_speech", "interrupt_speech") and len(u_clean) >= 6:
            self.chroma.add_memory(
                f"User said: '{u_clean}'. E.V. responded: '{a_clean}'.",
                metadata={"intent": intent_name},
            )


# Alias for backwards compatibility
MemoryRAG = RAGMemoryManager
