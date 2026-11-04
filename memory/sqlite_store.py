#!/usr/bin/env python3
"""
<<<<<<< HEAD
E.V. (Enhanced Virtual Assistant) — Centralized SQLite Knowledge Base & Self-Learning Store
===========================================================================================
Persistent WAL-mode SQLite3 database (`data/memory.db`) storing all personal
identity, friends/family contacts, self-learned rules/corrections, behavioral
preferences ("don't do X"), and conversation history strictly inside SQLite
(zero personal data in `config.yaml` or source code).
=======
E.V. (Enhanced Virtual Assistant) — Centralized SQLite Knowledge Base
=====================================================================
Persistent WAL-mode SQLite3 database (`data/memory.db`) storing all personal
identity, friends/family contacts, preferences, and conversation history
strictly inside the local SQLite database (zero personal data in `config.yaml`
or source code).
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

Tables:
  1. `user_profile`:
     - `field_key` (TEXT PRIMARY KEY): `full_name`, `nickname`, `birthday`,
       `mobile`, `email`, `instagram`, `address`, `preferences`, etc.
<<<<<<< HEAD
  2. `contacts`:
     - `id`, `name`, `nickname`, `relationship`, `phone`, `whatsapp`, `instagram`,
       `birthday`, `notes`, `created_at`, `updated_at`
     - Automatically resolves Indian family terms (`"mummy"`, `"mommy"`, `"mammi"`,
       `"maa"`, `"mother"` <-> `"Mom"`).
  3. `learned_knowledge` (NEW — Self-Learning & Corrections):
     - `id`, `topic`, `fact_text`, `source_type` (`correction` | `habit` | `rule` | `fact`),
       `created_at`, `updated_at`
  4. `preferences` & `conversation_history` & `message_history`:
     - Key-value user preferences (including negative constraints like `dont_do:...`)
       and turn-by-turn conversation logs.
=======
     - `field_value` (TEXT), `updated_at` (REAL)
  2. `contacts`:
     - `id` (INTEGER PRIMARY KEY AUTOINCREMENT)
     - `name`, `nickname`, `relationship`, `phone`, `whatsapp`, `instagram`,
       `birthday`, `notes`, `created_at`, `updated_at`
  3. `message_history`:
     - Outbound/inbound WhatsApp and SMS log for "What was the last message I sent to Arin?"
  4. `preferences` & `conversation_history`:
     - Key-value user preferences and turn-by-turn conversation logs.
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def mask_pii(text: str) -> str:
    """Mask phone numbers and email addresses before writing to application logs."""
    if not text:
        return ""
    masked = re.sub(
        r"(\+?\d[\d\s\-\(\)]{6,}\d)",
        lambda m: m.group(0)[:4] + "****" + m.group(0)[-2:],
        text,
    )
    masked = re.sub(
        r"([a-zA-Z0-9_.+-]+)@([a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)",
        r"***@\2",
        masked,
    )
    return masked


@dataclass
class ContactRecord:
    """Structured representation of a person in the user's `contacts` table."""

    id: int = 0
    name: str = ""
    nickname: str = ""
    relationship: str = ""
    phone: str = ""
    whatsapp: str = ""
    instagram: str = ""
    birthday: str = ""
    notes: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

<<<<<<< HEAD
=======
    # Backwards-compatible property aliases
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    @property
    def mobile_number(self) -> str:
        return self.phone

    @property
    def whatsapp_number(self) -> str:
        return self.whatsapp or self.phone

    @property
    def instagram_username(self) -> str:
        return self.instagram


class SQLiteMemoryStore:
    """
    Thread-safe SQLite3 personal knowledge base managing `user_profile`,
<<<<<<< HEAD
    `contacts`, `learned_knowledge`, `message_history`, `preferences`,
    and `conversation_history`.
=======
    `contacts`, `message_history`, `preferences`, and `conversation_history`.
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    """

    PROFILE_FIELD_ALIASES: Dict[str, str] = {
        "name": "full_name",
        "full name": "full_name",
        "my name": "full_name",
        "nickname": "nickname",
<<<<<<< HEAD
        "call me": "nickname",
=======
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        "age": "age",
        "birthday": "birthday",
        "date of birth": "birthday",
        "date_of_birth": "birthday",
        "dob": "birthday",
        "birth date": "birthday",
        "mobile": "mobile",
        "mobile number": "mobile",
        "mobile_number": "mobile",
        "phone": "mobile",
        "phone number": "mobile",
        "cell": "mobile",
        "cell number": "mobile",
        "number": "mobile",
        "email": "email",
        "email address": "email",
        "mail": "email",
        "address": "address",
        "home address": "address",
        "home_address": "address",
        "location": "address",
        "city": "address",
        "instagram": "instagram",
        "instagram username": "instagram",
        "instagram_username": "instagram",
        "instagram handle": "instagram",
        "insta": "instagram",
        "insta id": "instagram",
        "preferences": "preferences",
    }

    RELATIONSHIP_SYNONYMS: Dict[str, List[str]] = {
<<<<<<< HEAD
        "mother": ["mom", "mother", "mum", "mummy", "mommy", "mammi", "mami", "maa", "ma", "ammi", "mataji"],
        "father": ["dad", "father", "daddy", "papa", "papaji", "pitaji", "pop", "abba"],
        "brother": ["brother", "bro", "bhaiya", "bhai"],
        "sister": ["sister", "sis", "didi", "di"],
        "friend": ["friend", "best friend", "buddy", "dost"],
=======
        "mother": ["mom", "mother", "mum", "mummy", "ma"],
        "father": ["dad", "father", "papa", "pop"],
        "brother": ["brother", "bro"],
        "sister": ["sister", "sis"],
        "friend": ["friend", "best friend", "buddy"],
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        "colleague": ["colleague", "coworker", "teammate"],
    }

    def __init__(self, config: Dict[str, Any], base_dir: Path) -> None:
        self.config = config
        self.base_dir = base_dir
        self.logger = logging.getLogger("ev.sqlite")

        mem_cfg = config.get("memory", {})
        rel_db = str(mem_cfg.get("sqlite_path", "data/memory.db"))
        self.db_path: Path = (
            (base_dir / rel_db).resolve() if not Path(rel_db).is_absolute() else Path(rel_db)
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")

                # 1. user_profile table
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_profile (
                        field_key TEXT PRIMARY KEY,
                        field_value TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )

<<<<<<< HEAD
                # 2. contacts table
=======
                # 2. contacts table (with created_at and updated_at)
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS contacts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        nickname TEXT DEFAULT '',
                        relationship TEXT DEFAULT '',
                        phone TEXT DEFAULT '',
                        whatsapp TEXT DEFAULT '',
                        instagram TEXT DEFAULT '',
                        birthday TEXT DEFAULT '',
                        notes TEXT DEFAULT '',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
<<<<<<< HEAD
=======
                # Ensure columns exist if upgrading an older table
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                existing_cols = {
                    str(r["name"]).lower()
                    for r in conn.execute("PRAGMA table_info(contacts)").fetchall()
                }
                for col_name, col_def in [
                    ("phone", "TEXT DEFAULT ''"),
                    ("whatsapp", "TEXT DEFAULT ''"),
                    ("instagram", "TEXT DEFAULT ''"),
                    ("created_at", "REAL DEFAULT 0.0"),
                    ("updated_at", "REAL DEFAULT 0.0"),
                ]:
                    if col_name not in existing_cols:
                        conn.execute(f"ALTER TABLE contacts ADD COLUMN {col_name} {col_def}")

<<<<<<< HEAD
                # 3. learned_knowledge table (Self-Learning, Corrections & Habits)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS learned_knowledge (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        topic TEXT NOT NULL,
                        fact_text TEXT NOT NULL,
                        source_type TEXT DEFAULT 'fact',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )

                # 4. message_history table
=======
                # 3. message_history table
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS message_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        contact_name TEXT NOT NULL,
                        platform TEXT NOT NULL,
                        direction TEXT DEFAULT 'outgoing',
                        message_text TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                    """
                )

<<<<<<< HEAD
                # 5. preferences table
=======
                # 4. preferences table
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS preferences (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        category TEXT DEFAULT 'general',
                        updated_at REAL NOT NULL
                    )
                    """
                )

<<<<<<< HEAD
                # 6. conversation_history table
=======
                # 5. conversation_history table
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_text TEXT NOT NULL,
                        assistant_text TEXT NOT NULL,
                        intent_name TEXT DEFAULT 'unknown',
                        created_at REAL NOT NULL
                    )
                    """
                )
                conn.commit()

    # =========================================================================
    # 1. Personal Profile Methods (`user_profile`)
    # =========================================================================
    def normalize_profile_key(self, raw_key: str) -> str:
        cleaned = re.sub(r"[^a-z0-9\s_]", "", (raw_key or "").lower()).strip()
        cleaned = re.sub(r"^(?:my|personal)\s+", "", cleaned).strip()
        return self.PROFILE_FIELD_ALIASES.get(cleaned, cleaned.replace(" ", "_"))

    def set_profile_field(self, field: str, value: str) -> str:
        norm_key = self.normalize_profile_key(field)
        clean_val = (value or "").strip()
        if not norm_key or not clean_val:
            return ""

        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_profile (field_key, field_value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(field_key) DO UPDATE SET
                        field_value = excluded.field_value,
                        updated_at = excluded.updated_at
                    """,
                    (norm_key, clean_val, time.time()),
                )
                conn.commit()
        return norm_key

    def get_profile_field(self, field: str) -> Optional[str]:
        norm_key = self.normalize_profile_key(field)
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT field_value FROM user_profile WHERE field_key = ?",
                    (norm_key,),
                ).fetchone()
                if row and str(row["field_value"]).strip():
                    return str(row["field_value"]).strip()
        return self.get_preference(field) or self.get_preference(f"your {field}")

    def get_full_profile(self) -> Dict[str, str]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT field_key, field_value FROM user_profile").fetchall()
                return {
                    str(r["field_key"]): str(r["field_value"])
                    for r in rows
                    if str(r["field_value"]).strip()
                }

    # =========================================================================
    # 2. Friends, Family & Contacts Methods (`contacts`)
    # =========================================================================
    def add_or_update_contact(self, name: str, **kwargs: Any) -> ContactRecord:
<<<<<<< HEAD
=======
        """
        Insert a new contact or update an existing contact's fields in SQLite.
        Accepts `nickname`, `relationship`, `phone` (or `mobile_number`),
        `whatsapp` (or `whatsapp_number`), `instagram` (or `instagram_username`),
        `birthday`, and `notes`.
        """
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("Contact name cannot be empty")

        nickname = str(kwargs.get("nickname") or "").strip()
        relationship = str(kwargs.get("relationship") or "").strip().lower()
<<<<<<< HEAD
        # Automatically infer mother/father relationship when name is Mom/Mummy/Dad/Papa
        if not relationship:
            if clean_name.lower() in self.RELATIONSHIP_SYNONYMS["mother"]:
                relationship = "mother"
                if clean_name.lower() in ("mummy", "mommy", "mammi", "mami"):
                    clean_name = "Mom"
            elif clean_name.lower() in self.RELATIONSHIP_SYNONYMS["father"]:
                relationship = "father"
                if clean_name.lower() in ("daddy", "papa", "papaji"):
                    clean_name = "Dad"

=======
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        phone = str(kwargs.get("phone") or kwargs.get("mobile_number") or "").strip()
        whatsapp = str(kwargs.get("whatsapp") or kwargs.get("whatsapp_number") or phone).strip()
        instagram = str(
            kwargs.get("instagram") or kwargs.get("instagram_username") or ""
        ).strip().lstrip("@")
        birthday = str(kwargs.get("birthday") or "").strip()
        notes = str(kwargs.get("notes") or "").strip()

        now = time.time()
        with self._lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT * FROM contacts WHERE LOWER(name) = LOWER(?)",
                    (clean_name,),
                ).fetchone()

                if existing:
                    cid = int(existing["id"])
                    ex_keys = existing.keys()
                    old_phone = str(existing["phone"] if "phone" in ex_keys else "")
                    old_wa = str(existing["whatsapp"] if "whatsapp" in ex_keys else "")
                    old_ig = str(existing["instagram"] if "instagram" in ex_keys else "")

                    new_nick = nickname or str(existing["nickname"] or "")
                    new_rel = relationship or str(existing["relationship"] or "")
                    new_phone = phone or old_phone
                    new_wa = whatsapp or old_wa or new_phone
                    new_ig = instagram or old_ig
                    new_bday = birthday or str(existing["birthday"] or "")
                    new_notes = notes or str(existing["notes"] or "")

                    conn.execute(
                        """
                        UPDATE contacts SET
                            nickname = ?, relationship = ?, phone = ?,
                            whatsapp = ?, instagram = ?, birthday = ?,
                            notes = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_nick, new_rel, new_phone, new_wa, new_ig, new_bday, new_notes, now, cid),
                    )
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO contacts (
                            name, nickname, relationship, phone, whatsapp,
                            instagram, birthday, notes, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (clean_name, nickname, relationship, phone, whatsapp, instagram, birthday, notes, now, now),
                    )
                    cid = int(cur.lastrowid or 0)
                conn.commit()

        found = self.get_contact(clean_name)
        return found if found is not None else ContactRecord(id=cid, name=clean_name)

<<<<<<< HEAD
=======
    # Alias for backwards compatibility
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    def upsert_contact(self, name: str, **kwargs: Any) -> ContactRecord:
        return self.add_or_update_contact(name, **kwargs)

    def get_contact(self, name_or_relationship: str) -> Optional[ContactRecord]:
<<<<<<< HEAD
=======
        """Return the top matching contact by name, nickname, or relationship."""
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        matches = self.search_contacts(name_or_relationship)
        return matches[0] if matches else None

    def search_contacts(self, query: str) -> List[ContactRecord]:
<<<<<<< HEAD
=======
        """
        Search contacts by exact name, nickname, relationship ('Mom', 'my mother',
        'friend', 'brother'), or substring.
        """
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        cleaned = re.sub(r"^(?:my|the)\s+", "", (query or "").strip(), flags=re.I).strip()
        if not cleaned:
            return []

        q_low = cleaned.lower()
        rel_targets = {q_low}
        for canonical, syns in self.RELATIONSHIP_SYNONYMS.items():
            if q_low == canonical or q_low in syns:
                rel_targets.add(canonical)
                rel_targets.update(syns)

        all_contacts = self.list_contacts()

        # 1. Exact name or nickname match
        exact = [
            c
            for c in all_contacts
            if c.name.lower() == q_low or (c.nickname and c.nickname.lower() == q_low)
        ]
        if exact:
            return exact

<<<<<<< HEAD
        # 2. Exact relationship or synonym match (e.g. "mummy" / "mommy" / "mother" <-> "Mom")
=======
        # 2. Exact relationship or synonym match (e.g. "Mom" <-> "mother")
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        rel_matches = [
            c
            for c in all_contacts
            if c.relationship.lower() in rel_targets
            or c.name.lower() in rel_targets
            or (c.nickname and c.nickname.lower() in rel_targets)
        ]
        if rel_matches:
            return rel_matches

        # 3. Substring / prefix match
        return [
            c
            for c in all_contacts
            if q_low in c.name.lower()
            or (c.nickname and q_low in c.nickname.lower())
            or (c.instagram and q_low == c.instagram.lower())
        ]

<<<<<<< HEAD
=======
    # Alias for backwards compatibility
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    def find_contacts(self, query: str) -> List[ContactRecord]:
        return self.search_contacts(query)

    def list_contacts(self, relationship: Optional[str] = None) -> List[ContactRecord]:
<<<<<<< HEAD
=======
        """Return all contacts, optionally filtered by `relationship`."""
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute("SELECT * FROM contacts ORDER BY name ASC").fetchall()
                records: List[ContactRecord] = []
                for r in rows:
                    r_keys = r.keys()
                    phone_val = str(r["phone"] if "phone" in r_keys else "")
                    wa_val = str(r["whatsapp"] if "whatsapp" in r_keys else "") or phone_val
                    ig_val = str(r["instagram"] if "instagram" in r_keys else "")
                    rec = ContactRecord(
                        id=int(r["id"]),
                        name=str(r["name"]),
                        nickname=str(r["nickname"] or ""),
                        relationship=str(r["relationship"] or ""),
                        phone=phone_val,
                        whatsapp=wa_val,
                        instagram=ig_val,
                        birthday=str(r["birthday"] or ""),
                        notes=str(r["notes"] or ""),
                        created_at=float(r["created_at"] if "created_at" in r_keys and r["created_at"] else 0.0),
                        updated_at=float(r["updated_at"] if "updated_at" in r_keys and r["updated_at"] else 0.0),
                    )
                    if relationship is None or rec.relationship.lower() == relationship.lower().strip():
                        records.append(rec)
                return records

    def list_all_contacts(self) -> List[ContactRecord]:
        return self.list_contacts()

    def update_contact_field(self, person_query: str, field_name: str, value: str) -> Tuple[bool, str]:
        existing = self.get_contact(person_query)
<<<<<<< HEAD
        if existing:
            target_name = existing.name
        else:
            p_low = person_query.strip().lower()
            if p_low in self.RELATIONSHIP_SYNONYMS["mother"]:
                target_name = "Mom"
            elif p_low in self.RELATIONSHIP_SYNONYMS["father"]:
                target_name = "Dad"
            else:
                target_name = person_query.strip().title()
=======
        target_name = existing.name if existing else person_query.strip().title()
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

        f_low = field_name.lower().strip()
        kwargs: Dict[str, Any] = {}
        if "whatsapp" in f_low:
            kwargs["whatsapp"] = value.strip()
            if not existing or not existing.phone:
                kwargs["phone"] = value.strip()
        elif any(w in f_low for w in ("phone", "mobile", "number", "cell")):
            kwargs["phone"] = value.strip()
            if not existing or not existing.whatsapp:
                kwargs["whatsapp"] = value.strip()
        elif any(w in f_low for w in ("instagram", "insta")):
            kwargs["instagram"] = value.strip().lstrip("@")
        elif any(w in f_low for w in ("birthday", "dob", "birth")):
            kwargs["birthday"] = value.strip()
        elif any(w in f_low for w in ("relation", "relationship")):
            kwargs["relationship"] = value.strip().lower()
        elif any(w in f_low for w in ("nick", "nickname")):
            kwargs["nickname"] = value.strip()
        else:
            kwargs["notes"] = value.strip()

        rec = self.add_or_update_contact(target_name, **kwargs)
        return True, f"Updated {rec.name}'s {field_name} to {value.strip()}."

    # =========================================================================
<<<<<<< HEAD
    # 3. Self-Learning & Corrections (`learned_knowledge`)
    # =========================================================================
    def add_learned_knowledge(
        self,
        topic: str,
        fact_text: str,
        source_type: str = "fact",
    ) -> int:
        """
        Store or update a self-learned fact, correction, or user habit in SQLite.
        `source_type` can be `'correction'`, `'rule'`, `'preference'`, or `'fact'`.
        """
        clean_topic = (topic or "general").strip().lower()
        clean_fact = (fact_text or "").strip()
        if not clean_fact:
            return 0
        now = time.time()
        with self._lock:
            with self._connect() as conn:
                existing = conn.execute(
                    "SELECT id FROM learned_knowledge WHERE LOWER(topic) = ? AND source_type = ?",
                    (clean_topic, source_type),
                ).fetchone()
                if existing and clean_topic != "general":
                    kid = int(existing["id"])
                    conn.execute(
                        "UPDATE learned_knowledge SET fact_text = ?, updated_at = ? WHERE id = ?",
                        (clean_fact, now, kid),
                    )
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO learned_knowledge (topic, fact_text, source_type, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (clean_topic, clean_fact, source_type, now, now),
                    )
                    kid = int(cur.lastrowid or 0)
                conn.commit()
                return kid

    def list_learned_knowledge(self, limit: int = 15) -> List[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, topic, fact_text, source_type, updated_at
                    FROM learned_knowledge
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (max(1, limit),),
                ).fetchall()
                return [dict(r) for r in rows]

    def search_learned_knowledge(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        stopwords = {
            "the", "and", "for", "are", "you", "your", "my", "me", "is", "it", "to",
            "in", "on", "of", "can", "able", "hear", "hey", "hi", "hello", "what",
            "who", "how", "when", "where", "why", "do", "does", "did", "ev", "evie",
            "please", "tell", "about", "with", "that", "this", "from", "have",
        }
        words = [w for w in re.sub(r"[^a-z0-9\s]", " ", (query or "").lower()).split() if len(w) >= 3 and w not in stopwords]
        if not words:
            return []
        matches: List[Dict[str, Any]] = []
        seen_ids = set()
        with self._lock:
            with self._connect() as conn:
                for w in words[:4]:
                    pat = f"%{w}%"
                    rows = conn.execute(
                        """
                        SELECT id, topic, fact_text, source_type, updated_at
                        FROM learned_knowledge
                        WHERE LOWER(topic) LIKE ? OR LOWER(fact_text) LIKE ?
                        ORDER BY updated_at DESC LIMIT ?
                        """,
                        (pat, pat, limit),
                    ).fetchall()
                    for r in rows:
                        rid = int(r["id"])
                        if rid not in seen_ids:
                            seen_ids.add(rid)
                            matches.append(dict(r))
        return matches[:limit]

    # =========================================================================
    # 4. Message History (`message_history`)
=======
    # 3. Message History (`message_history`)
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    # =========================================================================
    def log_message(
        self,
        contact_name: str,
        platform: str,
        message_text: str,
        direction: str = "outgoing",
    ) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO message_history (contact_name, platform, direction, message_text, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (contact_name.strip(), platform.lower().strip(), direction, message_text.strip(), time.time()),
                )
                conn.commit()

    def get_last_message(
        self,
        contact_name: str,
        platform: Optional[str] = None,
        direction: str = "outgoing",
    ) -> Optional[Dict[str, Any]]:
        clean_c = (contact_name or "").strip().lower()
        with self._lock:
            with self._connect() as conn:
                if platform:
                    row = conn.execute(
                        """
                        SELECT contact_name, platform, direction, message_text, created_at
                        FROM message_history
                        WHERE LOWER(contact_name) LIKE ? AND LOWER(platform) = ? AND direction = ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (f"%{clean_c}%", platform.lower(), direction),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT contact_name, platform, direction, message_text, created_at
                        FROM message_history
                        WHERE LOWER(contact_name) LIKE ? AND direction = ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (f"%{clean_c}%", direction),
                    ).fetchone()
                return dict(row) if row else None

    # =========================================================================
<<<<<<< HEAD
    # 5. General Preferences & Conversation History
=======
    # 4. General Preferences & Conversation History
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
    # =========================================================================
    def set_preference(self, key: str, value: str, category: str = "general") -> None:
        clean_k = (key or "").strip().lower()
        clean_v = (value or "").strip()
        if not clean_k:
            return
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO preferences (key, value, category, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        category = excluded.category,
                        updated_at = excluded.updated_at
                    """,
                    (clean_k, clean_v, category, time.time()),
                )
                conn.commit()

    def get_preference(self, key: str, default: Optional[str] = None) -> Optional[str]:
        clean_k = (key or "").strip().lower()
        with self._lock:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM preferences WHERE key = ?",
                    (clean_k,),
                ).fetchone()
                return str(row["value"]) if row else default

<<<<<<< HEAD
    def list_preferences(self, limit: int = 20, category: Optional[str] = None) -> Dict[str, str]:
        with self._lock:
            with self._connect() as conn:
                if category:
                    rows = conn.execute(
                        "SELECT key, value FROM preferences WHERE category = ? ORDER BY updated_at DESC LIMIT ?",
                        (category, max(1, limit)),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT key, value FROM preferences ORDER BY updated_at DESC LIMIT ?",
                        (max(1, limit),),
                    ).fetchall()
                return {str(r["key"]): str(r["value"]) for r in rows}

    def search_facts(self, query: str, limit: int = 5) -> List[Tuple[str, str]]:
        stopwords = {
            "the", "and", "for", "are", "you", "your", "my", "me", "is", "it", "to",
            "in", "on", "of", "can", "able", "hear", "hey", "hi", "hello", "what",
            "who", "how", "when", "where", "why", "do", "does", "did", "ev", "evie",
            "please", "tell", "about", "with", "that", "this", "from", "have",
        }
        raw_words = re.sub(r"[^a-z0-9\s]", " ", (query or "").lower()).split()
        tokens = [t for t in raw_words if len(t) >= 3 and t not in stopwords]
        if not tokens:
            return []
=======
    def list_preferences(self, limit: int = 20) -> Dict[str, str]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT key, value FROM preferences ORDER BY updated_at DESC LIMIT ?",
                    (max(1, limit),),
                ).fetchall()
                return {str(r["key"]): str(r["value"]) for r in rows}

    def search_facts(self, query: str, limit: int = 5) -> List[Tuple[str, str]]:
        tokens = [t.strip().lower() for t in (query or "").split() if len(t.strip()) >= 2]
        if not tokens:
            return list(self.list_preferences(limit=limit).items())
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b

        results: List[Tuple[str, str]] = []
        with self._lock:
            with self._connect() as conn:
                for tok in tokens[:4]:
                    like_pat = f"%{tok}%"
                    rows = conn.execute(
                        "SELECT key, value FROM preferences WHERE key LIKE ? OR value LIKE ? LIMIT ?",
                        (like_pat, like_pat, limit),
                    ).fetchall()
                    for r in rows:
                        pair = (str(r["key"]), str(r["value"]))
                        if pair not in results:
                            results.append(pair)
        return results[:limit]

    def log_turn(self, user_text: str, assistant_text: str, intent_name: str = "unknown") -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO conversation_history (user_text, assistant_text, intent_name, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_text.strip(), assistant_text.strip(), intent_name, time.time()),
                )
                conn.commit()

    def recent_turns(self, limit: int = 5) -> List[Dict[str, Any]]:
        with self._lock:
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT user_text, assistant_text, intent_name, created_at
                    FROM conversation_history
                    ORDER BY id DESC LIMIT ?
                    """,
                    (max(1, limit),),
                ).fetchall()
<<<<<<< HEAD
                return [dict(r) for r in reversed(rows)]
=======
                return [dict(r) for r in reversed(rows)]


# Alias for backwards compatibility
SQLiteStore = SQLiteMemoryStore
>>>>>>> 3230d22d1bc56527499a9e289635632005c0d31b
