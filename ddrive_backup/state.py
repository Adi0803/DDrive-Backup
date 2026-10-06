"""Remembers what was backed up, so unchanged files need no checking next time.

This file (backup_state.json) is only a shortcut: if it is lost, the next run
re-checks files against OneDrive's checksums and rebuilds it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

STATE_VERSION = 1


class State:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._dirty = False
        self._last_save = 0.0
        self.data = self._fresh()

    @staticmethod
    def _fresh() -> dict:
        return {"version": STATE_VERSION, "drive_id": None, "root_id": None,
                "first_complete_backup": None, "files": {}, "sessions": {}, "failures": {},
                "other_failures": {}}

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != STATE_VERSION:
                raise ValueError("unknown version")
            for k, v in self._fresh().items():
                data.setdefault(k, v)
            self.data = data
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s (%s); starting with an empty record.", self.path.name, exc)
            try:
                self.path.replace(self.path.with_suffix(".broken.json"))
            except OSError:
                pass

    def bind(self, drive_id: str, root_id: str) -> bool:
        """Tie the record to this OneDrive folder; reset it if the folder changed.
        Returns True if the record was reset."""
        with self._lock:
            d = self.data
            if d["drive_id"] == drive_id and d["root_id"] == root_id:
                return False
            had_data = bool(d["files"]) or d["first_complete_backup"]
            self.data = self._fresh()
            self.data["drive_id"], self.data["root_id"] = drive_id, root_id
            self._dirty = True
            return bool(had_data)

    # --- per-file record: [size, mtime_ns, quickXorHash, item_id] ---------------

    def file_record(self, key: str) -> list | None:
        with self._lock:
            return self.data["files"].get(key)

    def set_file(self, key: str, size: int, mtime_ns: int, qxh: str, item_id: str) -> None:
        with self._lock:
            self.data["files"][key] = [size, mtime_ns, qxh, item_id]
            self.data["failures"].pop(key, None)
            self._dirty = True

    def forget_files(self, keys) -> None:
        with self._lock:
            for k in keys:
                self.data["files"].pop(k, None)
            self._dirty = True

    def prune_files(self, keep_keys: set[str]) -> None:
        with self._lock:
            for k in [k for k in self.data["files"] if k not in keep_keys]:
                del self.data["files"][k]
            for k in [k for k in self.data["failures"] if k not in keep_keys]:
                del self.data["failures"][k]
            for k in [k for k in self.data["sessions"] if k not in keep_keys]:
                del self.data["sessions"][k]
            week_ago = time.time() - 7 * 24 * 3600
            others = self.data["other_failures"]
            for k in [k for k, v in others.items() if v.get("at", 0) < week_ago]:
                del others[k]
            self._dirty = True

    # --- unfinished large uploads --------------------------------------------

    def session_for(self, key: str, size: int, mtime_ns: int) -> str | None:
        with self._lock:
            s = self.data["sessions"].get(key)
        if s and s.get("size") == size and s.get("mtime_ns") == mtime_ns and s.get("saved", 0) > time.time() - 6 * 3600:
            return s.get("url")
        return None

    def set_session(self, key: str, url: str | None, size: int = 0, mtime_ns: int = 0) -> None:
        with self._lock:
            if url:
                self.data["sessions"][key] = {"url": url, "size": size, "mtime_ns": mtime_ns, "saved": time.time()}
            else:
                self.data["sessions"].pop(key, None)
            self._dirty = True
        self.save_soon()

    # --- things that failed (so the 10-minute check does not pop up for them) ---

    def set_failure(self, key: str, size: int, mtime_ns: int, reason: str, kind: str = "upload") -> None:
        """kind "unreadable": the local file could not be read (e.g. blocked by
        antivirus); it stays quiet until the file itself changes."""
        with self._lock:
            self.data["failures"][key] = {"size": size, "mtime_ns": mtime_ns, "reason": reason,
                                          "kind": kind, "at": time.time()}
            self._dirty = True

    def recent_failure(self, key: str, size: int, mtime_ns: int, within_seconds: float) -> bool:
        with self._lock:
            f = self.data["failures"].get(key)
        if not f or f["size"] != size or f["mtime_ns"] != mtime_ns:
            return False
        return f.get("kind") == "unreadable" or f["at"] > time.time() - within_seconds

    def set_other_failure(self, tag: str, reason: str) -> None:
        """Failures that are not about one local file: a folder that could not be
        created ("folder:<key>") or an item that could not be deleted ("delete:<id>")."""
        with self._lock:
            self.data["other_failures"][tag] = {"reason": reason, "at": time.time()}
            self._dirty = True

    def recent_other_failure(self, tag: str, within_seconds: float) -> bool:
        with self._lock:
            f = self.data["other_failures"].get(tag)
        return bool(f and f["at"] > time.time() - within_seconds)

    def clear_other_failure(self, tag: str) -> None:
        with self._lock:
            if self.data["other_failures"].pop(tag, None) is not None:
                self._dirty = True

    # --- run-level facts ------------------------------------------------------

    @property
    def first_complete_backup(self) -> str | None:
        return self.data.get("first_complete_backup")

    def mark_first_complete(self) -> None:
        with self._lock:
            self.data["first_complete_backup"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._dirty = True

    # --- saving -------------------------------------------------------------

    def save_soon(self, every_seconds: float = 30.0) -> None:
        if time.monotonic() - self._last_save >= every_seconds:
            self.save()

    def save(self) -> None:
        with self._save_lock:
            with self._lock:
                if not self._dirty:
                    return
                text = json.dumps(self.data, separators=(",", ":"))
                self._dirty = False
                self._last_save = time.monotonic()
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError as exc:
                log.warning("Could not save %s: %s", self.path.name, exc)
                with self._lock:
                    self._dirty = True
