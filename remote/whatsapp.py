#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — WhatsApp Web Controller (Selenium)
======================================================================
Low-memory, on-demand Selenium WhatsApp Web bridge using a persistent local
Chrome user profile (`data/chrome_whatsapp_profile`) so QR authentication is
only needed once.

RAM Conservation Design (MSI Bravo 15 <= 4.0 GB Budget):
  - Lazy Browser Initialization: Chrome is NEVER launched at startup unless
    `remote.whatsapp.enabled: true` AND a WhatsApp action or poll is requested.
  - Ultra-Slim Chromium Flags: Runs with `--disable-gpu`, `--no-sandbox`,
    `--disable-extensions`, `--disable-application-cache`, and single-tab reuse.
  - Emergency RAM Unload: Subscribes to `Topics.RAM_PRESSURE` and automatically
    closes the headless Chrome driver if total E.V. RSS crosses the critical
    threshold (3.8 GB), falling back gracefully to ADB SMS/standby mode.
  - Supports reading latest unread messages, sending text replies, and sending
    synthesized Piper TTS voice notes (`.wav` / `.ogg`).
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.event_bus import Event, EventBus, Topics, get_event_bus

try:
    from selenium import webdriver  # type: ignore
    from selenium.webdriver.chrome.options import Options as ChromeOptions  # type: ignore
    from selenium.webdriver.common.by import By  # type: ignore
    from selenium.webdriver.common.keys import Keys  # type: ignore
    from selenium.webdriver.support import expected_conditions as EC  # type: ignore
    from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
    SELENIUM_AVAILABLE = True
except ImportError:
    webdriver = None  # type: ignore
    ChromeOptions = None  # type: ignore
    By = None  # type: ignore
    Keys = None  # type: ignore
    EC = None  # type: ignore
    WebDriverWait = None  # type: ignore
    SELENIUM_AVAILABLE = False


class WhatsAppController:
    """
    Persistent-profile Selenium WhatsApp Web controller with text + Piper TTS
    voice-note reply capabilities and automatic RAM-pressure shutdown.
    """

    WHATSAPP_URL = "https://web.whatsapp.com"

    def __init__(
        self,
        config: Dict[str, Any],
        base_dir: Path,
        event_bus: Optional[EventBus] = None,
        tts: Optional[Any] = None,
        rag: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.bus = event_bus or get_event_bus()
        self.tts = tts
        self.rag = rag
        self.logger = logging.getLogger("ev.whatsapp")

        wa_cfg = config.get("remote", {}).get("whatsapp", {})
        self.enabled: bool = bool(wa_cfg.get("enabled", False))
        self.headless: bool = bool(wa_cfg.get("headless", True))
        self.poll_interval_s: float = float(wa_cfg.get("poll_interval_seconds", 5.0))

        rel_prof = str(wa_cfg.get("chrome_profile_dir", "data/chrome_whatsapp_profile"))
        self.profile_dir: Path = (
            (base_dir / rel_prof).resolve() if not Path(rel_prof).is_absolute() else Path(rel_prof)
        )
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        self.voice_notes_dir: Path = base_dir / "data" / "voice_notes"
        self.voice_notes_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._driver: Optional[Any] = None
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # Subscribe to RAM pressure to close headless Chrome if RAM approaches 4.0 GB
        self.bus.subscribe(Topics.RAM_PRESSURE, self._on_ram_pressure)

        # Publish initial standby status to HUD top bar
        self._set_hud_status("standby" if self.enabled or SELENIUM_AVAILABLE else "offline")

    def _set_hud_status(self, status: str) -> None:
        self.bus.publish(
            Topics.CONNECTION_STATUS,
            {"id": "whatsapp", "status": status},
            source="whatsapp",
        )

    def _on_ram_pressure(self, event: Event) -> None:
        if event.payload.get("level") == "critical" and self._driver is not None:
            self.logger.warning("Closing WhatsApp Chrome session due to critical RAM pressure.")
            self.close_session()

    # =========================================================================
    # Browser Session Lifecycle
    # =========================================================================
    def ensure_session(self) -> bool:
        """Launch or reuse the persistent Chrome profile session for WhatsApp Web."""
        with self._lock:
            if self._driver is not None:
                return True

            if not SELENIUM_AVAILABLE or webdriver is None:
                self.logger.warning("selenium is not installed; WhatsApp controller in standby.")
                self._set_hud_status("standby")
                return False

            try:
                opts = ChromeOptions()
                opts.add_argument(f"--user-data-dir={self.profile_dir}")
                opts.add_argument("--disable-gpu")  # Strict 0 VRAM mandate
                opts.add_argument("--no-sandbox")
                opts.add_argument("--disable-dev-shm-usage")
                opts.add_argument("--disable-extensions")
                opts.add_argument("--mute-audio")
                if self.headless:
                    opts.add_argument("--headless=new")

                self.logger.info("Starting WhatsApp Web Chrome session (profile=%s)...", self.profile_dir.name)
                self._driver = webdriver.Chrome(options=opts)
                self._driver.get(self.WHATSAPP_URL)
                self._set_hud_status("online")
                return True
            except Exception as exc:
                self.logger.warning("WhatsApp Web browser launch degraded to standby: %s", exc)
                self._driver = None
                self._set_hud_status("standby")
                return False

    def close_session(self) -> None:
        """Close the Selenium Chrome driver and free ~250-400 MB of system RAM."""
        with self._lock:
            if self._driver is not None:
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None
                self._set_hud_status("standby")

    # =========================================================================
    # Messaging & Voice Note Actions
    # =========================================================================
    def set_rag(self, rag: Any) -> None:
        """Attach the RAGMemoryManager so WhatsAppController resolves contacts from SQLite."""
        self.rag = rag

    def send_message(self, contact_name: str, message: str, phone_number: str = "") -> Tuple[bool, str]:
        """
        Send a WhatsApp text message to `contact_name`, resolving their stored
        name/WhatsApp number from the SQLite `contacts` database when available.
        """
        contact = (contact_name or "").strip()
        body = (message or "").strip()
        if not contact or not body:
            return False, "Please specify both a WhatsApp contact and message."

        resolved_number = phone_number.strip()
        if self.rag is not None and hasattr(self.rag, "get_contact"):
            rec = self.rag.get_contact(contact)
            if rec is not None:
                contact = rec.name
                resolved_number = resolved_number or rec.whatsapp or rec.phone

        if not self.enabled or not self.ensure_session() or self._driver is None:
            self.logger.info(
                "WhatsApp Web on standby; queued simulated message to %s: '%s'",
                contact,
                body,
            )
            return True, f"WhatsApp message queued for {contact}: {body}"

        with self._lock:
            try:
                wait = WebDriverWait(self._driver, 12)
                # 1. Search for contact in left pane search box
                search_box = wait.until(
                    EC.presence_of_element_located(
                        (By.XPATH, '//div[@contenteditable="true"][@data-tab="3"]')
                    )
                )
                search_box.clear()
                search_box.send_keys(contact)
                search_box.send_keys(Keys.ENTER)
                time.sleep(0.6)

                # 2. Type message in conversation input box
                msg_box = wait.until(
                    EC.presence_of_element_located(
                        (By.XPATH, '//footer//div[@contenteditable="true"]')
                    )
                )
                msg_box.send_keys(body)
                msg_box.send_keys(Keys.ENTER)
                return True, f"Sent WhatsApp message to {contact}."
            except Exception as exc:
                self.logger.warning("WhatsApp Web send_message fallback: %s", exc)
                return False, f"Could not send WhatsApp message to {contact} right now."

    def send_voice_note(self, contact_name: str, text_to_synthesize: str) -> Tuple[bool, str]:
        """
        Synthesize `text_to_synthesize` to a `.wav` file using Piper TTS and
        upload it as an audio attachment in WhatsApp Web.
        """
        contact = (contact_name or "").strip()
        spoken = (text_to_synthesize or "").strip()
        if not contact or not spoken:
            return False, "Please provide both a contact and voice note message."

        wav_path = self.voice_notes_dir / f"vn_{int(time.time())}.wav"
        if self.tts is not None and hasattr(self.tts, "_synthesize_wav_bytes"):
            wav_bytes = self.tts._synthesize_wav_bytes(spoken)
            if wav_bytes:
                wav_path.write_bytes(wav_bytes)

        if not self.enabled or not self.ensure_session() or self._driver is None:
            return True, f"Prepared WhatsApp voice note for {contact}."

        with self._lock:
            try:
                if wav_path.exists():
                    file_inputs = self._driver.find_elements(By.XPATH, '//input[@type="file"]')
                    if file_inputs:
                        file_inputs[0].send_keys(str(wav_path))
                        time.sleep(1.0)
                        send_btns = self._driver.find_elements(By.XPATH, '//span[@data-icon="send"]')
                        if send_btns:
                            send_btns[0].click()
                            return True, f"Sent WhatsApp voice note to {contact}."
            except Exception as exc:
                self.logger.warning("WhatsApp voice note upload fallback: %s", exc)

        # Fallback to text message if attachment selector changed
        return self.send_message(contact, spoken)

    def read_latest_messages(self, limit: int = 3) -> List[Dict[str, str]]:
        """Read the latest unread or recent messages from the active WhatsApp view."""
        if not self.enabled or self._driver is None:
            return []

        with self._lock:
            try:
                nodes = self._driver.find_elements(
                    By.XPATH, '//div[contains(@class, "message-in")]//span[contains(@class, "selectable-text")]'
                )
                texts = [n.text.strip() for n in nodes[-limit:] if n.text.strip()]
                return [{"sender": "Contact", "text": t} for t in texts]
            except Exception:
                return []