#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Media, Window & Screenshot Automation
=========================================================================
Lightweight desktop media playback controls, window management shortcuts,
and screenshot capture powered by `PyAutoGUI` and `Pillow`.
"""

from __future__ import annotations

import ctypes
import logging
import platform
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import pyautogui  # type: ignore
    pyautogui.FAILSAFE = False
    PYAUTOGUI_AVAILABLE = True
except ImportError:
    pyautogui = None  # type: ignore
    PYAUTOGUI_AVAILABLE = False

try:
    from PIL import ImageGrab  # type: ignore
    PIL_AVAILABLE = True
except ImportError:
    ImageGrab = None  # type: ignore
    PIL_AVAILABLE = False


class MediaController:
    """Controls system media keys, window minimization, and screenshots."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, base_dir: Optional[Path] = None) -> None:
        self.config = config or {}
        self.base_dir = base_dir or Path.cwd()
        self.logger = logging.getLogger("ev.media")
        self.is_windows = platform.system().lower().startswith("win")
        self.screenshots_dir = self.base_dir / "data" / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

    def play_pause(self) -> bool:
        return self._send_media_key(0xB3, "playpause")  # VK_MEDIA_PLAY_PAUSE

    def next_track(self) -> bool:
        return self._send_media_key(0xB0, "nexttrack")  # VK_MEDIA_NEXT_TRACK

    def previous_track(self) -> bool:
        return self._send_media_key(0xB1, "prevtrack")  # VK_MEDIA_PREV_TRACK

    def stop_media(self) -> bool:
        return self._send_media_key(0xB2, "stop")       # VK_MEDIA_STOP

    def show_desktop(self) -> bool:
        """Minimize all windows (Win+D on Windows)."""
        if PYAUTOGUI_AVAILABLE and pyautogui is not None:
            try:
                pyautogui.hotkey("win", "d")
                return True
            except Exception:
                pass
        return False

    def take_screenshot(self) -> Path:
        """Capture the screen to `data/screenshots/screenshot_YYYYMMDD_HHMMSS.png`."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = self.screenshots_dir / f"screenshot_{ts}.png"

        if PIL_AVAILABLE and ImageGrab is not None:
            try:
                img = ImageGrab.grab()
                img.save(out_path)
                return out_path
            except Exception as exc:
                self.logger.debug("PIL ImageGrab failed: %s", exc)

        if PYAUTOGUI_AVAILABLE and pyautogui is not None:
            try:
                pyautogui.screenshot(str(out_path))
                return out_path
            except Exception as exc:
                self.logger.debug("PyAutoGUI screenshot failed: %s", exc)

        return out_path

    def _send_media_key(self, vk_code: int, pyautogui_name: str) -> bool:
        if self.is_windows:
            try:
                ctypes.windll.user32.keybd_event(vk_code, 0, 0, 0)  # type: ignore
                ctypes.windll.user32.keybd_event(vk_code, 0, 2, 0)  # type: ignore
                return True
            except Exception:
                pass
        if PYAUTOGUI_AVAILABLE and pyautogui is not None:
            try:
                pyautogui.press(pyautogui_name)
                return True
            except Exception:
                pass
        return True