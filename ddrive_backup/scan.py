"""Scans the local backup folder."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable

from .names import MAX_PATH_CHARS, key_for, onedrive_name_problem
from .winutils import describe_os_error, long_path

log = logging.getLogger(__name__)


@dataclass
class LocalFile:
    path: str          # relative, '/' separated, original case
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass
class LocalScan:
    files: dict[str, LocalFile] = field(default_factory=dict)        # key -> file
    folders: dict[str, str] = field(default_factory=dict)            # key -> relative path
    unreadable: dict[str, str] = field(default_factory=dict)         # relative path -> reason
    not_allowed: dict[str, str] = field(default_factory=dict)        # relative path -> reason
    links_skipped: list[str] = field(default_factory=list)
    total_bytes: int = 0

    def protected_keys(self) -> list[str]:
        """Paths that exist locally but are not backed up (unreadable, refused
        names, links). Mirroring must never delete their OneDrive copies."""
        return [key_for(p.rstrip("/")) for p in
                (*self.unreadable, *self.not_allowed, *self.links_skipped)]


def local_path(root: str, rel: str) -> str:
    return os.path.join(root, *rel.split("/")) if rel else root


def scan_local(root: str, remote_prefix_len: int, progress: Callable[[int], None] | None = None,
               should_stop: Callable[[], None] | None = None) -> LocalScan:
    """Walk `root` without following links. `remote_prefix_len` is the length of
    the OneDrive folder path, used to warn about OneDrive's path length limit."""
    result = LocalScan()
    base = long_path(root)
    stack = [""]
    while stack:
        if should_stop:
            should_stop()
        rel_dir = stack.pop()
        try:
            with os.scandir(local_path(base, rel_dir)) as it:
                entries = sorted(it, key=lambda e: e.name.casefold())
        except OSError as exc:
            result.unreadable[(rel_dir or ".") + "/"] = "folder could not be read: " + describe_os_error(
                local_path(base, rel_dir), exc)
            continue
        subdirs = []
        for entry in entries:
            rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
            try:
                if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                    result.links_skipped.append(rel)
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                problem = onedrive_name_problem(entry.name)
                if problem is None and remote_prefix_len + 1 + len(rel) > MAX_PATH_CHARS:
                    problem = f"the path is longer than OneDrive's {MAX_PATH_CHARS}-character limit"
                key = key_for(rel)
                if problem is None and (key in result.files or key in result.folders):
                    other = result.files[key].path if key in result.files else result.folders[key]
                    problem = (f"same name as \"{other}\" apart from upper/lower case or accents; "
                               "OneDrive can keep only one of them")
                if problem:
                    result.not_allowed[rel + ("/" if is_dir else "")] = problem
                    continue
                if is_dir:
                    result.folders[key] = rel
                    subdirs.append(rel)
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat(follow_symlinks=False)
                    birth = getattr(st, "st_birthtime_ns", None) or st.st_ctime_ns
                    result.files[key] = LocalFile(rel, st.st_size, st.st_mtime_ns, birth)
                    result.total_bytes += st.st_size
                    if progress and len(result.files) % 500 == 0:
                        progress(len(result.files))
            except OSError as exc:
                result.unreadable[rel] = describe_os_error(local_path(base, rel), exc)
        stack.extend(reversed(subdirs))
    if progress:
        progress(len(result.files))
    return result
