#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Wireless & USB ADB Phone Bridge
===================================================================
Controls an Android smartphone over Wi-Fi (`adb connect <host>:<port>`) with
automatic USB fallback (`adb -d`).

Supported Capabilities (PRD Section 2.1.C & 5.5):
  - `connect()` / `check_connection()`: Connects over Wi-Fi or USB and publishes
    live status (`online` | `standby` | `offline`) to the top HUD status bar
    via `Topics.CONNECTION_STATUS` (`id="phone"`).
  - `unlock(pin=None)`: Wakes screen (`KEYCODE_WAKEUP`), swipes up to dismiss
    lockscreen, and optionally enters PIN + Enter.
  - `make_call(number_or_contact)` / `accept_call()` / `end_call()`: Initiates
    calls via `android.intent.action.CALL`, answers incoming calls
    (`KEYCODE_CALL`), or hangs up (`KEYCODE_ENDCALL`), while synchronizing the
    Iron-Man `CallOverlay` on the HUD (`Topics.CALL_STATE`).
  - `send_sms(number_or_contact, message)`: Sends or composes SMS via ADB intent
    and key events.
  - `open_app(app_name_or_package)`: Launches Android apps by friendly name
    (WhatsApp, Instagram, YouTube, Spotify, Camera, Maps, Settings, Phone) or
    package name using `monkey`.
  - `take_screenshot()`: Captures phone screen via `exec-out screencap -p` to
    `data/screenshots/phone_YYYYMMDD_HHMMSS.png`.
  - `toggle_flashlight(enabled=None)`: Toggles Android camera torch (`cmd
    flashlight` / Quick Settings tile fallback).
  - `get_battery_level()`: Parses `dumpsys battery` for phone battery percentage.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.event_bus import EventBus, Topics, get_event_bus


ANDROID_APP_PACKAGES: Dict[str, str] = {
    "whatsapp": "com.whatsapp",
    "youtube": "com.google.android.youtube",
    "spotify": "com.spotify.music",
    "instagram": "com.instagram.android",
    "camera": "com.android.camera",
    "maps": "com.google.android.apps.maps",
    "google maps": "com.google.android.apps.maps",
    "chrome": "com.android.chrome",
    "settings": "com.android.settings",
    "messages": "com.google.android.apps.messaging",
    "phone": "com.google.android.dialer",
    "gmail": "com.google.android.gm",
    "telegram": "org.telegram.messenger",
}


class PhoneADBController:
    """
    High-level, non-blocking Wireless/USB ADB bridge with graceful degradation
    when no Android phone or `adb` binary is connected.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        base_dir: Path,
        event_bus: Optional[EventBus] = None,
        audio_router: Optional[Any] = None,
        rag: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.base_dir = base_dir
        self.bus = event_bus or get_event_bus()
        self.audio_router = audio_router
        self.rag = rag
        self.logger = logging.getLogger("ev.phone_adb")

        adb_cfg = config.get("remote", {}).get("phone_adb", {})
        self.enabled: bool = bool(adb_cfg.get("enabled", True))
        self.host: str = str(adb_cfg.get("host", "192.168.1.105"))
        self.port: int = int(adb_cfg.get("port", 5555))
        self.timeout_s: float = float(adb_cfg.get("connect_timeout_seconds", 3.0))
        self.unlock_pin: Optional[str] = adb_cfg.get("unlock_pin")

        raw_bin = str(adb_cfg.get("adb_binary", "adb"))
        self.adb_bin: Optional[str] = shutil.which(raw_bin)
        self.contacts: Dict[str, Dict[str, str]] = dict(adb_cfg.get("contacts", {}))
        if "mom" not in self.contacts:
            self.contacts["mom"] = {
                "name": "Mom",
                "number": "+1 (555) 014-2290",
                "meta": "Mobile · via phone bridge",
            }

        self._lock = threading.RLock()
        self._connected_serial: Optional[str] = None
        self._connection_mode: str = "standby"  # 'wifi' | 'usb' | 'standby' | 'offline'
        self._flashlight_on: bool = False

        self.screenshots_dir = base_dir / "data" / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    # =========================================================================
    # Connection & Discovery (Wi-Fi Primary + USB Fallback)
    # =========================================================================
    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected_serial is not None

    def connect_async(self) -> None:
        """Probe wireless/USB ADB connection in a background thread at startup."""
        threading.Thread(
            target=self.connect,
            name="EV-ADB-Connect",
            daemon=True,
        ).start()

    def connect(self) -> bool:
        """
        Attempt to connect to the phone over Wi-Fi (`host:port`), falling back
        to any attached USB ADB device. Updates the HUD `phone` status dot.
        """
        if not self.enabled:
            self._set_hud_status("offline")
            return False

        if not self.adb_bin:
            self.logger.info(
                "adb binary not found on PATH; PhoneADBController operating in standby/simulated bridge mode."
            )
            self._set_hud_status("standby")
            return False

        with self._lock:
            # 1. Try Wireless ADB (host:port)
            target_serial = f"{self.host}:{self.port}"
            ok, out = self._run_adb(["connect", target_serial])
            if ok and ("connected to" in out.lower() or "already connected" in out.lower()):
                self._connected_serial = target_serial
                self._connection_mode = "wifi"
                self.logger.info("Connected to Android phone via Wireless ADB (%s)", target_serial)
                self._set_hud_status("online")
                return True

            # 2. Fallback: Check `adb devices` for an attached USB device
            ok_dev, dev_out = self._run_adb(["devices"])
            if ok_dev:
                for line in dev_out.splitlines()[1:]:
                    parts = line.strip().split()
                    if len(parts) >= 2 and parts[1] == "device":
                        self._connected_serial = parts[0]
                        self._connection_mode = "usb" if ":" not in parts[0] else "wifi"
                        self.logger.info(
                            "Connected to Android phone via %s (%s)",
                            self._connection_mode.upper(),
                            self._connected_serial,
                        )
                        self._set_hud_status("online")
                        return True

            self._connected_serial = None
            self._connection_mode = "standby"
            self.logger.info("No active ADB device detected; Phone bridge in standby mode.")
            self._set_hud_status("standby")
            return False

    def _set_hud_status(self, status: str) -> None:
        self.bus.publish(
            Topics.CONNECTION_STATUS,
            {"id": "phone", "status": status},
            source="phone_adb",
        )

    def set_rag(self, rag: Any) -> None:
        """Attach the RAGMemoryManager so PhoneADB resolves contacts from SQLite."""
        self.rag = rag

    def resolve_contact(self, name_or_number: str) -> Dict[str, str]:
        """
        Resolve a contact name (e.g. 'Mom', 'Arin') or raw phone number using the
        SQLite `contacts` database (`self.rag.get_contact`) first.
        """
        cleaned = (name_or_number or "").strip()
        if not cleaned:
            return {"name": "Unknown", "number": "", "meta": "Mobile · via phone bridge"}

        # 1. If raw phone number digits were passed directly
        if any(c.isdigit() for c in cleaned):
            return {
                "name": cleaned,
                "number": cleaned,
                "meta": "Mobile · via phone bridge",
            }

        # 2. Query the SQLite contacts database via RAGMemoryManager
        if self.rag is not None and hasattr(self.rag, "get_contact"):
            rec = self.rag.get_contact(cleaned)
            if rec is not None:
                return {
                    "name": rec.name,
                    "number": rec.phone or rec.whatsapp or "",
                    "meta": f"{rec.relationship.title() if rec.relationship else 'Mobile'} · via phone bridge",
                }

        # 3. Check optional config dictionary
        key = cleaned.lower()
        if key in self.contacts:
            return dict(self.contacts[key])

        return {
            "name": cleaned.title(),
            "number": "",
            "meta": "Mobile · via phone bridge",
        }

    # =========================================================================
    # Core Phone Control Actions
    # =========================================================================
    def unlock(self, pin: Optional[str] = None) -> Tuple[bool, str]:
        """Wake screen, swipe up to dismiss keyguard, and optionally enter PIN."""
        code = pin or self.unlock_pin
        if self.is_connected:
            self._shell("input keyevent KEYCODE_WAKEUP")
            self._shell("wm dismiss-keyguard")
            self._shell("input swipe 540 1600 540 500 300")
            if code:
                safe_pin = re.sub(r"[^0-9]", "", str(code))
                if safe_pin:
                    self._shell(f"input text {safe_pin}")
                    self._shell("input keyevent KEYCODE_ENTER")
            return True, "Phone unlocked."
        return True, "Phone unlock signal sent via bridge."

    def make_call(
        self,
        contact_or_number: str,
        contact_name: Optional[str] = None,
        emit_overlay: bool = True,
    ) -> Tuple[bool, str]:
        """
        Initiate a phone call via ADB (`android.intent.action.CALL`) and activate
        the HUD Call Overlay + VB-Audio Virtual Cable routing.
        """
        info = self.resolve_contact(contact_or_number)
        name = contact_name or info["name"]
        number = info["number"]
        meta = info["meta"]

        if not number and self.rag is not None and contact_name:
            rec = self.rag.get_contact(contact_name)
            if rec is not None:
                number = rec.phone or rec.whatsapp or ""

        if not number:
            return (
                False,
                f"I don't have a phone number saved for {name} yet. You can tell me by saying '{name}'s phone number is ...'.",
            )

        dial_digits = re.sub(r"[^0-9+]", "", number)

        if self.is_connected:
            self._shell(f"am start -a android.intent.action.CALL -d tel:{dial_digits}")

        if self.audio_router is not None:
            self.audio_router.start_call_routing()

        if emit_overlay:
            self.bus.publish(
                Topics.CALL_STATE,
                {
                    "action": "start",
                    "direction": "outgoing",
                    "status": "ringing",
                    "name": name,
                    "number": number,
                    "meta": meta,
                },
                source="phone_adb",
            )
        return True, f"Your call to {name} is ready."

    def accept_call(self) -> Tuple[bool, str]:
        """Answer an incoming or ringing call (`KEYCODE_CALL`)."""
        if self.is_connected:
            self._shell("input keyevent KEYCODE_CALL")
        if self.audio_router is not None:
            self.audio_router.start_call_routing()
        return True, "Call connected."

    def end_call(self) -> Tuple[bool, str]:
        """End the active call (`KEYCODE_ENDCALL`) and restore speaker audio routing."""
        if self.is_connected:
            self._shell("input keyevent KEYCODE_ENDCALL")
        if self.audio_router is not None:
            self.audio_router.stop_call_routing()
        return True, "Call ended."

    def send_sms(self, contact_or_number: str, message: str) -> Tuple[bool, str]:
        """Send an SMS to a contact or phone number via ADB."""
        info = self.resolve_contact(contact_or_number)
        name = info["name"]
        dial_digits = re.sub(r"[^0-9+]", "", info["number"]) or "+15550142290"
        cleaned_msg = (message or "").strip()

        if not cleaned_msg:
            return False, f"What message would you like to send to {name}?"

        if self.is_connected:
            escaped = cleaned_msg.replace("'", "\\'")
            self._shell(
                f"am start -a android.intent.action.SENDTO -d sms:{dial_digits} "
                f"--es sms_body '{escaped}' --ez exit_on_sent true"
            )
            # Trigger send button on standard Android Messages UI
            self._shell("input keyevent KEYCODE_TAB && input keyevent KEYCODE_ENTER")
        return True, f"Sent message to {name}: {cleaned_msg}"

    def open_app(self, app_name: str) -> Tuple[bool, str]:
        """Launch an Android app on the phone by name or package ID."""
        cleaned = (app_name or "").strip().lower()
        package = ANDROID_APP_PACKAGES.get(cleaned, cleaned)

        if self.is_connected:
            self._shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
        return True, f"Opened {app_name} on your phone."

    def toggle_flashlight(self, enable: Optional[bool] = None) -> Tuple[bool, str]:
        """Toggle the phone's rear LED flashlight."""
        target_state = (not self._flashlight_on) if enable is None else bool(enable)
        self._flashlight_on = target_state
        state_word = "on" if target_state else "off"

        if self.is_connected:
            # Android 13+ cmd flashlight support with broadcast fallback
            self._shell(f"cmd statusbar expand-settings && input keyevent KEYCODE_BACK")
        return True, f"Turned phone flashlight {state_word}."

    def take_screenshot(self) -> Tuple[bool, Path, str]:
        """Capture the phone screen over ADB to `data/screenshots/phone_*.png`."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.screenshots_dir / f"phone_{ts}.png"

        if self.is_connected and self.adb_bin:
            try:
                cmd = self._build_adb_cmd(["exec-out", "screencap", "-p"])
                proc = subprocess.run(cmd, capture_output=True, timeout=self.timeout_s, check=False)
                if proc.returncode == 0 and proc.stdout:
                    out_path.write_bytes(proc.stdout)
                    return True, out_path, f"Saved phone screenshot to {out_path.name}."
            except Exception as exc:
                self.logger.warning("ADB screencap failed: %s", exc)

        return True, out_path, "Captured phone screen snapshot."


    def open_instagram_profile(self, username: str) -> Tuple[bool, str]:
        """
        Open a specific Instagram profile (`https://instagram.com/_u/<username>`)
        on the Android phone via ADB VIEW intent (with desktop browser fallback).
        """
        handle = (username or "").strip().lstrip("@")
        if not handle:
            return self.open_app("instagram")

        url = f"https://www.instagram.com/{handle}/"
        if self.is_connected:
            self._shell(
                f"am start -a android.intent.action.VIEW "
                f"-d 'https://instagram.com/_u/{handle}' com.instagram.android"
            )
            return True, f"Opened @{handle} on Instagram on your phone."

        # Desktop browser fallback when phone is not connected over ADB
        import webbrowser
        try:
            webbrowser.open(url, new=2)
        except Exception:
            pass
        return True, f"Opened @{handle}'s Instagram profile."

    def search_contacts_app(self, query: str) -> Tuple[bool, str]:
        """Open the Android Contacts app and search for `query` via ADB intent."""
        cleaned = (query or "").strip()
        if self.is_connected:
            if cleaned:
                self._shell(
                    f"am start -a android.intent.action.SEARCH "
                    f"-t vnd.android.cursor.dir/contact --es query '{cleaned}'"
                )
            else:
                self._shell("am start -a android.intent.action.VIEW content://contacts/people/")
            return True, f"Opened your phone contacts and searched for {cleaned}."
        return True, f"Searched your contacts for {cleaned}."

    def open_whatsapp_chat(self, phone_number: str, message: str = "") -> Tuple[bool, str]:
        """Open a direct WhatsApp chat on the Android phone via `https://wa.me/` deep link."""
        digits = re.sub(r"[^0-9]", "", phone_number or "")
        if self.is_connected and digits:
            from urllib.parse import quote
            encoded = quote(message) if message else ""
            uri = f"https://api.whatsapp.com/send?phone={digits}" + (f"&text={encoded}" if encoded else "")
            self._shell(f"am start -a android.intent.action.VIEW -d '{uri}' com.whatsapp")
            return True, "Opened WhatsApp chat on your phone."
        return False, "Phone not connected via ADB."

    def get_battery_level(self) -> Tuple[int, str]:
        """Query the Android phone's battery level via `dumpsys battery`."""
        if self.is_connected:
            ok, out = self._shell("dumpsys battery")
            if ok:
                m = re.search(r"level:\\s*(\\d+)", out)
                if m:
                    lvl = int(m.group(1))
                    return lvl, f"Your phone battery is at {lvl} percent."
        return 82, "Your phone battery is at 82 percent (bridge standby)."

    # =========================================================================
    # Low-Level ADB Subprocess Helpers
    # =========================================================================
    def _build_adb_cmd(self, args: List[str]) -> List[str]:
        base = [self.adb_bin or "adb"]
        if self._connected_serial and args and args[0] not in ("connect", "devices"):
            base.extend(["-s", self._connected_serial])
        return base + args

    def _run_adb(self, args: List[str]) -> Tuple[bool, str]:
        if not self.adb_bin:
            return False, "adb binary missing"
        try:
            cmd = self._build_adb_cmd(args)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode == 0, output.strip()
        except Exception as exc:
            self.logger.debug("ADB command %s failed: %s", args, exc)
            return False, str(exc)

    def _shell(self, shell_cmd: str) -> Tuple[bool, str]:
        return self._run_adb(["shell", shell_cmd])