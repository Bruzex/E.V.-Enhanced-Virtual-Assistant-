#!/usr/bin/env python3
"""
E.V. (Enhanced Virtual Assistant) — Workspace Orchestrator (v2.1 Strong Undo)
=============================================================================
Loads declarative workspace definitions from `data/workspaces/*.json` and
executes multi-item launches (apps, terminals, folders, URLs) on a background
thread with:

  1. Pre-Launch PID Baseline Snapshot (`baseline_pids`):
     Records all PIDs running on the system right before the workspace starts
     AND before each individual item launches, capturing detached child processes
     (such as `Code.exe`, `Spotify.exe`, `Notion.exe`, `chrome.exe`, `msedge.exe`,
     `brave.exe`, `WindowsTerminal.exe`) that spawn outside the launcher PID.
  2. Multi-Layer Per-Item Undo (`_reverse_single_item`):
     - Layer A: Kills the recorded PID + any newly spawned PIDs captured for that item
       using `taskkill /F /T /PID` and `psutil` recursive tree termination.
     - Layer B: Closes matching top-level application/browser windows by title
       (`WM_CLOSE` via Win32 `EnumWindows`).
     - Layer C: For `folder` items, safely closes open File Explorer (`CabinetWClass`)
       windows via COM `Shell.Application.Windows()` + `WM_CLOSE` without killing
       the Windows desktop shell (`explorer.exe`).
     - Layer D: Force-kills target process image names (`taskkill /F /T /IM <image>`)
       for `app` and `terminal` items (`Code.exe`, `Docker Desktop.exe`, `Notion.exe`,
       `Spotify.exe`, `WindowsTerminal.exe`, etc.) and newly spawned browser processes
       for `url` items (`chrome.exe`, `msedge.exe`, `brave.exe`, `firefox.exe`).
  3. Final Stubborn-App Cleanup Sweep (`_final_cleanup_sweep`):
     At the end of the Undo loop, performs a dedicated sweep across all workspace
     image names, remaining open File Explorer folder windows, and newly spawned
     processes since `session.baseline_pids` so nothing launched by the workspace
     is left behind.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from automation.system import BROWSER_IMAGE_NAMES, SystemController
from core.event_bus import EventBus, Topics, get_event_bus


@dataclass
class LaunchedItemRecord:
    """Record of a single launched workspace item pushed onto the Undo stack."""

    item_id: str
    name: str
    detail: str
    kind: str
    target: str
    pid: Optional[int] = None
    spawned_pids: Set[int] = field(default_factory=set)
    image_names: List[str] = field(default_factory=list)
    launched_at: float = field(default_factory=time.time)


@dataclass
class WorkspaceSession:
    """Represents a launched workspace session that can be reversed via Undo."""

    name: str
    slug: str
    items: List[Dict[str, Any]]
    baseline_pids: Set[int] = field(default_factory=set)
    launched_records: List[LaunchedItemRecord] = field(default_factory=list)


class WorkspaceOrchestrator:
    """
    Thread-safe JSON Workspace Orchestrator with real-time HUD EventBus telemetry
    and aggressive multi-layer LIFO Undo + final cleanup sweep.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        base_dir: Optional[Path] = None,
        system_ctrl: Optional[SystemController] = None,
        event_bus: Optional[EventBus] = None,
        tts: Optional[Any] = None,
    ) -> None:
        self.config = config or {}
        self.base_dir = base_dir or Path.cwd()
        self.system_ctrl = system_ctrl or SystemController(config=self.config, base_dir=self.base_dir)
        self.bus = event_bus or get_event_bus()
        self.tts = tts
        self.logger = logging.getLogger("ev.workspace")

        auto_cfg = self.config.get("automation", {})
        rel_dir = str(auto_cfg.get("workspaces_dir", "data/workspaces"))
        self.workspaces_dir: Path = (
            (self.base_dir / rel_dir).resolve() if not Path(rel_dir).is_absolute() else Path(rel_dir)
        )
        self.workspaces_dir.mkdir(parents=True, exist_ok=True)

        self.default_workspace: str = str(auto_cfg.get("default_workspace", "coding"))
        self.sequential_delay_s: float = max(0.01, float(auto_cfg.get("sequential_delay_ms", 520)) / 1000.0)
        self.undo_delay_s: float = max(0.01, float(auto_cfg.get("undo_delay_ms", 150)) / 1000.0)
        self.max_parallel_group: int = max(1, min(4, int(auto_cfg.get("max_parallel_group", 2))))
        self.execute_os_actions: bool = bool(auto_cfg.get("execute_os_actions", True))
        self.force_close_browsers_on_undo: bool = bool(
            auto_cfg.get("force_close_browsers_on_undo", True)
        )

        self._lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._active_thread: Optional[threading.Thread] = None
        self._is_busy: bool = False
        self._undo_stack: List[WorkspaceSession] = []

    # Backward-compatible alias
    def load_workspace(self, name_or_slug: str) -> Optional[Dict[str, Any]]:
        return self.load_workspace_definition(name_or_slug)

    # =========================================================================
    # Workspace Discovery & JSON Loading
    # =========================================================================
    def list_workspaces(self) -> List[str]:
        """Return available workspace slugs from `data/workspaces/*.json`."""
        return sorted(p.stem.lower() for p in self.workspaces_dir.glob("*.json"))

    def load_workspace_definition(self, name_or_slug: str) -> Optional[Dict[str, Any]]:
        """Load and validate a workspace JSON file by slug or display name."""
        slug = (name_or_slug or self.default_workspace).strip().lower().replace("workspace", "").strip()
        if not slug:
            slug = self.default_workspace

        candidate = self.workspaces_dir / f"{slug}.json"
        if not candidate.exists():
            for p in self.workspaces_dir.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    if slug in str(data.get("name", "")).lower() or slug in p.stem.lower():
                        candidate = p
                        break
                except Exception:
                    continue

        if not candidate.exists():
            self.logger.warning("Workspace JSON not found for '%s' in %s", name_or_slug, self.workspaces_dir)
            return None

        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
            items = data.get("items", [])
            if not isinstance(items, list) or not items:
                return None
            return {
                "slug": candidate.stem.lower(),
                "name": str(data.get("name", f"{candidate.stem.title()} workspace")),
                "items": items,
            }
        except Exception as exc:
            self.logger.error("Failed to parse workspace file %s: %s", candidate, exc)
            return None

    # =========================================================================
    # Selective Item Filtering
    # =========================================================================
    def _filter_items(self, items: List[Dict[str, Any]], filter_tokens: List[str]) -> List[Dict[str, Any]]:
        """Filter workspace items by name, id, target, or category keywords."""
        if not filter_tokens:
            return items
        norm_tokens = [t.strip().lower() for t in filter_tokens if t.strip()]
        matched = []
        for item in items:
            name_l = item.get("name", "").lower()
            id_l = item.get("id", "").lower()
            kind_l = item.get("kind", "").lower()
            target_l = str(item.get("target", "")).lower()
            detail_l = str(item.get("detail", "")).lower()

            for tok in norm_tokens:
                if (
                    tok in name_l
                    or tok in id_l
                    or tok in target_l
                    or tok in detail_l
                    or (tok in ("browser", "web", "chrome", "edge", "internet") and (kind_l == "url" or "http" in target_l or "chrome" in target_l))
                    or (tok in ("code", "editor", "vscode", "vs code") and ("code" in id_l or "code" in target_l or "code" in name_l))
                    or (tok in ("terminal", "cmd", "shell", "powershell", "monitor") and (kind_l == "terminal" or "log" in id_l or "tail" in target_l))
                    or (tok in ("folder", "directory", "dir", "files", "project", "projects") and (kind_l == "folder" or "project" in id_l))
                ):
                    matched.append(item)
                    break
        return matched if matched else items

    # =========================================================================
    # Launch & Undo Public API
    # =========================================================================
    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._is_busy

    def launch_workspace(
        self,
        name_or_slug: Optional[str] = None,
        on_complete: Optional[Callable[[], None]] = None,
        item_filter: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """
        Start launching a workspace asynchronously on a background worker thread.
        Supports selective filtering of workspace items via `item_filter`.
        Publishes real-time `Topics.WORKSPACE_PROGRESS` events after every item.
        """
        with self._lock:
            if self._is_busy:
                return False, "A workspace operation is already in progress."

            ws_def = self.load_workspace_definition(name_or_slug or self.default_workspace)
            if ws_def is None:
                return False, f"Could not find workspace '{name_or_slug}'."

            self._is_busy = True
            self._cancel_event.clear()

        ws_name = ws_def["name"]
        items = ws_def["items"]
        if item_filter:
            filtered = self._filter_items(items, item_filter)
            if filtered:
                ws_def = dict(ws_def)
                ws_def["items"] = filtered
                items = filtered

        self._active_thread = threading.Thread(
            target=self._run_launch_worker,
            args=(ws_def, on_complete),
            name=f"EV-Workspace-{ws_def['slug']}",
            daemon=True,
        )
        self._active_thread.start()
        return True, f"Launching {ws_name.lower()} ({len(items)} items)."

    def undo_last_workspace(self) -> Tuple[bool, str]:
        """
        Reverse the most recently launched workspace in LIFO order, aggressively
        closing all spawned applications, folders, terminals, and browser windows.
        """
        with self._lock:
            if self._is_busy:
                self._cancel_event.set()

            if not self._undo_stack:
                # Even if stack is empty (e.g. after restart), synthesize a session from default workspace
                ws_def = self.load_workspace_definition(self.default_workspace)
                if ws_def is None:
                    return False, "No active workspace in the undo history."
                synth_session = WorkspaceSession(
                    name=ws_def["name"],
                    slug=ws_def["slug"],
                    items=ws_def["items"],
                    launched_records=[
                        LaunchedItemRecord(
                            item_id=str(it.get("id", "item")),
                            name=str(it.get("name", "item")),
                            detail=str(it.get("detail", "")),
                            kind=str(it.get("kind", "app")).lower(),
                            target=str(it.get("target") or it.get("detail") or it.get("name", "")),
                            image_names=self.system_ctrl.resolve_image_names_for_item(
                                str(it.get("id", "")),
                                str(it.get("name", "")),
                                str(it.get("target") or ""),
                                str(it.get("kind", "app")).lower(),
                            ),
                        )
                        for it in ws_def["items"]
                    ],
                )
                session = synth_session
            else:
                session = self._undo_stack.pop()

            self._is_busy = True
            self._cancel_event.clear()

        self._active_thread = threading.Thread(
            target=self._run_undo_worker,
            args=(session,),
            name=f"EV-Workspace-Undo-{session.slug}",
            daemon=True,
        )
        self._active_thread.start()
        return True, f"Reversing {session.name.lower()}."

    def cancel(self) -> None:
        """Abort an in-progress workspace launch or undo loop."""
        self._cancel_event.set()

    # =========================================================================
    # Background Execution Workers (Sequential + Bounded Parallel Groups)
    # =========================================================================
    def _run_launch_worker(
        self,
        ws_def: Dict[str, Any],
        on_complete: Optional[Callable[[], None]] = None,
    ) -> None:
        ws_name: str = ws_def["name"]
        slug: str = ws_def["slug"]
        items: List[Dict[str, Any]] = ws_def["items"]
        total = len(items)

        baseline_pids = self.system_ctrl.snapshot_running_pids() if self.execute_os_actions else set()
        session = WorkspaceSession(
            name=ws_name,
            slug=slug,
            items=items,
            baseline_pids=baseline_pids,
        )
        with self._lock:
            self._undo_stack.append(session)

        self._publish_progress(ws_name, "running", 0, total, items)
        self.bus.publish(Topics.ORB_STATE, {"state": "acting", "progress": 0.0}, source="workspace")

        completed = 0
        idx = 0

        try:
            while idx < total and not self._cancel_event.is_set():
                item = items[idx]
                parallel_group = item.get("parallel_group")

                batch: List[Dict[str, Any]] = [item]
                if parallel_group is not None:
                    while (
                        (idx + len(batch)) < total
                        and len(batch) < self.max_parallel_group
                        and items[idx + len(batch)].get("parallel_group") == parallel_group
                    ):
                        batch.append(items[idx + len(batch)])

                if len(batch) == 1:
                    rec = self._execute_single_item(batch[0])
                    session.launched_records.append(rec)
                    completed += 1
                    idx += 1
                    self._publish_progress(ws_name, "running", completed, total, items)
                    self.bus.publish(
                        Topics.ORB_STATE,
                        {"state": "acting", "progress": completed / float(total)},
                        source="workspace",
                    )
                    if idx < total and not self._cancel_event.is_set():
                        time.sleep(self.sequential_delay_s)
                else:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_parallel_group) as pool:
                        futures = [pool.submit(self._execute_single_item, b_item) for b_item in batch]
                        for fut in futures:
                            if self._cancel_event.is_set():
                                break
                            rec = fut.result()
                            session.launched_records.append(rec)
                            completed += 1
                            self._publish_progress(ws_name, "running", completed, total, items)
                            self.bus.publish(
                                Topics.ORB_STATE,
                                {"state": "acting", "progress": completed / float(total)},
                                source="workspace",
                            )
                    idx += len(batch)
                    if idx < total and not self._cancel_event.is_set():
                        time.sleep(self.sequential_delay_s)

            if not self._cancel_event.is_set():
                self._publish_progress(ws_name, "done", total, total, items)
                self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="workspace")
                done_msg = f"Workspace ready. {total} items launched."
                if self.tts is not None:
                    self.tts.speak(done_msg, block=False)
                else:
                    self.bus.publish(
                        Topics.CAPTION,
                        {"speaker": "ev", "text": done_msg, "hold_ms": 2400},
                        source="workspace",
                    )
                if on_complete is not None:
                    on_complete()
        except Exception as exc:
            self.logger.error("Workspace launch worker error: %s", exc, exc_info=True)
            self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="workspace")
        finally:
            with self._lock:
                self._is_busy = False

    def _run_undo_worker(self, session: WorkspaceSession) -> None:
        ws_name = session.name
        items = session.items
        total = max(1, len(items))
        remaining = len(session.launched_records)

        self._publish_progress(ws_name, "undoing", remaining, total, items)
        self.bus.publish(
            Topics.ORB_STATE,
            {"state": "acting", "progress": remaining / float(total)},
            source="workspace",
        )

        try:
            # Step 1: Reverse each item in LIFO order
            for rec in reversed(session.launched_records):
                if self._cancel_event.is_set():
                    break
                self._reverse_single_item(rec, baseline_pids=session.baseline_pids)
                remaining = max(0, remaining - 1)
                self._publish_progress(ws_name, "undoing", remaining, total, items)
                self.bus.publish(
                    Topics.ORB_STATE,
                    {"state": "acting", "progress": remaining / float(total)},
                    source="workspace",
                )
                time.sleep(self.undo_delay_s)

            # Step 2: Final aggressive cleanup sweep for stubborn apps, browsers & Explorer folders
            if not self._cancel_event.is_set() and self.execute_os_actions:
                self._final_cleanup_sweep(session)

            self._publish_progress(ws_name, "undone", 0, total, items)
            self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="workspace")
            undo_msg = "Workspace closed. Previous state restored."
            if self.tts is not None:
                self.tts.speak(undo_msg, block=False)
            else:
                self.bus.publish(
                    Topics.CAPTION,
                    {"speaker": "ev", "text": undo_msg, "hold_ms": 2400},
                    source="workspace",
                )
        except Exception as exc:
            self.logger.error("Workspace undo worker error: %s", exc, exc_info=True)
            self.bus.publish(Topics.ORB_STATE, {"state": "idle", "progress": 0.0}, source="workspace")
        finally:
            with self._lock:
                self._is_busy = False

    def _execute_single_item(self, item: Dict[str, Any]) -> LaunchedItemRecord:
        item_id = str(item.get("id", "item"))
        name = str(item.get("name", item_id))
        detail = str(item.get("detail", ""))
        kind = str(item.get("kind", "app")).lower()
        target = str(item.get("target") or detail or name)
        pid: Optional[int] = None
        spawned_pids: Set[int] = set()

        image_names = self.system_ctrl.resolve_image_names_for_item(item_id, name, target, kind)

        if self.execute_os_actions:
            before_pids = self.system_ctrl.snapshot_running_pids()
            if kind == "url":
                _, pid = self.system_ctrl.open_url(target)
            elif kind == "folder":
                _, pid = self.system_ctrl.open_folder(target)
            elif kind == "terminal":
                _, pid = self.system_ctrl.open_terminal_command(target)
            else:
                _, pid, _ = self.system_ctrl.open_application(target)

            after_pids = self.system_ctrl.snapshot_running_pids()
            spawned_pids = after_pids - before_pids
            if pid is not None:
                spawned_pids.add(pid)

        return LaunchedItemRecord(
            item_id=item_id,
            name=name,
            detail=detail,
            kind=kind,
            target=target,
            pid=pid,
            spawned_pids=spawned_pids,
            image_names=image_names,
        )

    def _reverse_single_item(
        self,
        rec: LaunchedItemRecord,
        baseline_pids: Optional[Set[int]] = None,
    ) -> None:
        """
        Aggressively close a single workspace item on Undo:
          1. Terminate the primary recorded PID and all captured `spawned_pids`
             via `taskkill /F /T /PID` and `psutil` child-tree termination.
          2. For `folder` items, safely close File Explorer windows matching the
             folder name (`Shell.Application` COM + `CabinetWClass` `WM_CLOSE`).
          3. Send `WM_CLOSE` to top-level windows matching the item's name or detail.
          4. Force-kill known process image names (`Code.exe`, `Notion.exe`,
             `Spotify.exe`, `Docker Desktop.exe`, `WindowsTerminal.exe`, browsers, etc.)
             via `taskkill /F /T /IM`.
        """
        if not self.execute_os_actions:
            return

        self.logger.info(
            "Undoing workspace item '%s' (kind=%s | pid=%s | images=%s)",
            rec.name,
            rec.kind,
            rec.pid,
            rec.image_names,
        )

        # 1. Kill primary PID and all newly spawned delta PIDs for this item
        if rec.pid is not None:
            self.system_ctrl.terminate_pid(rec.pid, force=True)
        for sp_pid in rec.spawned_pids:
            self.system_ctrl.terminate_pid(sp_pid, force=True)

        # 2. Special safe handling for File Explorer folders
        if rec.kind == "folder":
            self.system_ctrl.close_explorer_folder_windows(folder_hint=rec.target)
            self.system_ctrl.close_windows_by_title([rec.name, rec.detail])
            return

        # 3. Close top-level windows matching item title / detail (works for apps, terminals, and browser tabs)
        title_hints = [h for h in (rec.name, rec.detail, rec.item_id) if h and len(h) >= 3]
        if title_hints:
            self.system_ctrl.close_windows_by_title(title_hints)

        # 4. Kill by process image names (.exe)
        if rec.kind == "url":
            # For URLs, first kill any browser processes spawned after workspace baseline;
            # if force_close_browsers_on_undo is enabled, also ensure stubborn browser instances close
            if baseline_pids:
                self.system_ctrl.kill_by_image_names(BROWSER_IMAGE_NAMES, only_pids_not_in=baseline_pids)
            if self.force_close_browsers_on_undo:
                self.system_ctrl.kill_by_image_names(BROWSER_IMAGE_NAMES)
        else:
            if rec.image_names:
                self.system_ctrl.kill_by_image_names(rec.image_names)
            else:
                self.system_ctrl.close_application(rec.name)

    def _final_cleanup_sweep(self, session: WorkspaceSession) -> None:
        """
        Final cleanup sweep executed at the end of Undo to catch any stubborn
        multi-process applications (VS Code, Docker Desktop, Notion, Spotify,
        Windows Terminal, browsers, and open File Explorer folder windows) that
        respawned helper processes during shutdown.
        """
        self.logger.info("Running final Undo cleanup sweep for '%s'...", session.name)

        sweep_images: List[str] = []
        has_folders = False
        has_urls = False

        for rec in session.launched_records:
            if rec.kind == "folder":
                has_folders = True
            elif rec.kind == "url":
                has_urls = True
            for img in rec.image_names:
                if img not in sweep_images:
                    sweep_images.append(img)

        # Close any remaining File Explorer folder windows safely
        if has_folders:
            self.system_ctrl.close_explorer_folder_windows()

        # Include browsers if the workspace launched URLs and force_close_browsers_on_undo is True
        if has_urls and self.force_close_browsers_on_undo:
            for b_img in BROWSER_IMAGE_NAMES:
                if b_img not in sweep_images:
                    sweep_images.append(b_img)

        # Force-kill all collected workspace image names (`taskkill /F /T /IM`)
        if sweep_images:
            self.system_ctrl.kill_by_image_names(sweep_images)

    def _publish_progress(
        self,
        ws_name: str,
        status: str,
        completed: int,
        total: int,
        items: List[Dict[str, Any]],
    ) -> None:
        self.bus.publish(
            Topics.WORKSPACE_PROGRESS,
            {
                "name": ws_name,
                "status": status,
                "completed": completed,
                "total": total,
                "items": items,
            },
            source="workspace",
        )
