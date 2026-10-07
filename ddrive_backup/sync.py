"""One backup run: scan both sides, check, upload, mirror deletions, report."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .auth import SignInRequired
from .config import Config
from .graph import (FRAGMENT_SIZE, LARGE_FILE_SIZE, FatalRunError, FileFailed, GraphClient, GraphError,
                    LocalReadError, RemoteFile, RemoteFolder, RunStopped)
from .names import key_for
from .progress import Console, Ticker, TransferProgress, fmt_bytes, fmt_duration, n_files
from .quickxorhash import EMPTY_HASH, QuickXorHash
from .scan import LocalFile, LocalScan, local_path, scan_local
from .state import State
from .winutils import KeepAwake, describe_os_error, long_path, on_battery

log = logging.getLogger(__name__)

FAILURE_QUIET_SECONDS = 24 * 3600   # something that failed is retried by the 10-minute check after this
REMOTE_CHECK_SECONDS = 24 * 3600    # the background check looks at OneDrive itself at least this often
WIFI_RECHECK_SECONDS = 60


# ----------------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------------

@dataclass
class Deletions:
    folders: list[RemoteFolder] = field(default_factory=list)
    files: list[RemoteFile] = field(default_factory=list)
    file_count: int = 0                # files removed, including those inside folders
    byte_count: int = 0

    def __bool__(self) -> bool:
        return bool(self.folders or self.files)

    def items(self) -> list:
        return [*self.folders, *self.files]


def build_deletions(folders: list[RemoteFolder], files: list[RemoteFile],
                    remote_files: dict[str, RemoteFile]) -> Deletions:
    """A Deletions object with its file and byte counts filled in."""
    result = Deletions(folders=list(folders), files=list(files))
    for folder in result.folders:
        prefix = key_for(folder.path) + "/"
        inside = [f for k, f in remote_files.items() if k.startswith(prefix)]
        result.file_count += len(inside)
        result.byte_count += sum(f.size for f in inside)
    result.file_count += len(result.files)
    result.byte_count += sum(f.size for f in result.files)
    return result


@dataclass
class Plan:
    up_to_date: int = 0
    to_check: list[tuple[LocalFile, RemoteFile]] = field(default_factory=list)
    to_upload: list[tuple[LocalFile, str]] = field(default_factory=list)
    folders_missing: list[str] = field(default_factory=list)
    conflicts: Deletions = field(default_factory=Deletions)   # OneDrive items in the way of a local item of the other type
    deletions: Deletions = field(default_factory=Deletions)   # OneDrive items no longer present locally


def make_plan(local: LocalScan, remote_folders: dict[str, RemoteFolder],
              remote_files: dict[str, RemoteFile], state: State) -> Plan:
    plan = Plan()
    for key in sorted(local.files, key=lambda k: local.files[k].path):
        lf = local.files[key]
        rf = remote_files.get(key)
        record = state.file_record(key)
        if rf is None:
            plan.to_upload.append((lf, "new"))
        elif rf.size != lf.size:
            plan.to_upload.append((lf, "changed"))
        elif lf.size == 0:
            plan.up_to_date += 1
            if record is None:
                state.set_file(key, 0, lf.mtime_ns, EMPTY_HASH, rf.id)
        elif record and record[0] == lf.size and record[1] == lf.mtime_ns and (
                (rf.hash and record[2] == rf.hash) or (not rf.hash and record[3] == rf.id)):
            plan.up_to_date += 1
        elif rf.hash:
            plan.to_check.append((lf, rf))      # same size: compare checksums once
        else:
            plan.to_upload.append((lf, "cannot verify the OneDrive copy"))
    plan.folders_missing = sorted((rel for k, rel in local.folders.items() if k not in remote_folders),
                                  key=lambda p: (p.count("/"), p))
    gone = plan_deletions(local, remote_folders, remote_files)
    needed = set(local.files) | set(local.folders)
    in_the_way = lambda item: key_for(item.path) in needed      # noqa: E731
    plan.conflicts = build_deletions([f for f in gone.folders if in_the_way(f)],
                                     [f for f in gone.files if in_the_way(f)], remote_files)
    plan.deletions = build_deletions([f for f in gone.folders if not in_the_way(f)],
                                     [f for f in gone.files if not in_the_way(f)], remote_files)
    return plan


def plan_deletions(local: LocalScan, remote_folders: dict[str, RemoteFolder],
                   remote_files: dict[str, RemoteFile]) -> Deletions:
    """What exact mirroring would remove from OneDrive: items that no longer
    exist locally, never touching anything under a path we could not read."""
    protected = local.protected_keys()
    if any(p in ("", ".") for p in protected):
        return Deletions()   # the whole source folder was unreadable

    def is_protected(key: str) -> bool:
        return any(key == p or key.startswith(p + "/") or p.startswith(key + "/") for p in protected)

    def parent_exists_locally(key: str) -> bool:
        parent = key.rpartition("/")[0]
        return parent == "" or parent in local.folders

    folders = [folder for key, folder in sorted(remote_folders.items())
               if key and key not in local.folders and parent_exists_locally(key) and not is_protected(key)]
    files = [rf for key, rf in sorted(remote_files.items())
             if key not in local.files and parent_exists_locally(key) and not is_protected(key)]
    return build_deletions(folders, files, remote_files)


# ----------------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------------

@dataclass
class RunResult:
    status: str = "complete"           # complete | finished_with_problems | stopped | failed | dry_run
    message: str = ""
    local_files: int = 0
    local_bytes: int = 0
    up_to_date: int = 0
    uploaded: int = 0
    uploaded_bytes: int = 0
    deleted_files: int = 0
    folders_created: int = 0
    unreadable: dict[str, str] = field(default_factory=dict)
    not_allowed: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    links_skipped: list[str] = field(default_factory=list)
    deletions_blocked: int = 0
    held_back: int = 0
    pending_deletions: int = 0         # in OneDrive but not local, while mirroring is not on yet
    mirroring: str = ""
    approve_command: str = ""
    seconds: float = 0.0

    @property
    def needs_attention(self) -> bool:
        """Something the user has to read or decide (keep the window open)."""
        return self.status in ("failed", "finished_with_problems") or bool(self.deletions_blocked)


class BackupRun:
    def __init__(self, cfg: Config, graph: GraphClient, state: State, console: Console,
                 wifi_problem: Callable[[], str | None], approve_deletions: bool = False,
                 command: str = "DDriveOneDriveBackup.py"):
        self.cfg, self.graph, self.state, self.console = cfg, graph, state, console
        self.wifi_problem = wifi_problem
        self.approve_deletions = approve_deletions
        self.command = command
        self.stopper = graph.stopper
        self.result = RunResult()
        self._result_lock = threading.Lock()
        self.root_id = ""
        self.base = long_path(cfg.source_folder)
        self.local: LocalScan | None = None
        self.remote_folders: dict[str, RemoteFolder] = {}
        self.remote_files: dict[str, RemoteFile] = {}
        self.remote_bytes = 0
        self._folder_ids: dict[str, str] = {}
        self._folder_lock = threading.RLock()
        self._failed_local: list[LocalFile] = []

    # --- shared steps (also used by the hidden 10-minute check) ---------------

    def mirroring_active(self) -> bool:
        mode = self.cfg.mirror_deletions
        return mode == "on" or (mode == "auto" and bool(self.state.first_complete_backup))

    def deletions_allowed(self, deletions: Deletions) -> bool:
        """The safety stop: never remove more than the limit, counted both in
        files and in bytes, without explicit approval."""
        if self.approve_deletions or not deletions:
            return True
        share = self.cfg.mirror_safety_limit_percent / 100.0
        return (deletions.file_count <= share * len(self.remote_files)
                and deletions.byte_count <= share * self.remote_bytes)

    def prepare(self, show_progress: bool) -> Plan:
        """Find the OneDrive folder, scan both sides and work out what to do."""
        cfg, say = self.cfg, self.console.say
        if not os.path.isdir(self.base):
            raise FatalRunError(f"The folder to back up does not exist or is not reachable: {cfg.source_folder}")
        drive = self.graph.get_drive()
        root = self.graph.ensure_folder_path(cfg.onedrive_folder, create=not cfg.dry_run)
        self.root_id = root["id"] if root else ""
        if self.state.bind(drive["id"], self.root_id or "(not created yet)"):
            log.info("The OneDrive backup folder changed since the last run; its saved record was reset.")
        self.state.data["target"] = self.target()

        say(f"Scanning {cfg.source_folder} ...")
        scan_note = (lambda n: self.console.show([f"  {n_files(n)} found"])) if show_progress else None
        self.local = scan_local(cfg.source_folder, len(cfg.onedrive_folder), scan_note, self.stopper.check)
        if "./" in self.local.unreadable:
            raise FatalRunError("The folder to back up could not be read: " + self.local.unreadable["./"])
        self.console.end_block()
        say(f"  {n_files(len(self.local.files))}, {fmt_bytes(self.local.total_bytes)}")

        if self.root_id:
            say(f"Reading OneDrive/{cfg.onedrive_folder} ...")
            counts = [0, 0]

            def note(files: int, folders: int) -> None:
                counts[0], counts[1] = files, folders

            def show() -> None:
                extra = ("   (OneDrive asked us to slow down - waiting a moment, this is normal)"
                         if self.graph.throttled() else "")
                self.console.show([f"  {n_files(counts[0])} in {counts[1]:,} folders{extra}"])

            if show_progress:
                with Ticker(show):
                    self.remote_folders, self.remote_files = self.graph.list_tree(self.root_id, note)
            else:
                self.remote_folders, self.remote_files = self.graph.list_tree(self.root_id)
            self.console.end_block()
            say(f"  {n_files(len(self.remote_files))} already in OneDrive")
            self.state.mark_remote_checked()
        else:
            self.remote_folders, self.remote_files = {"": RemoteFolder("", "")}, {}
        self.remote_bytes = sum(f.size for f in self.remote_files.values())
        self._folder_ids = {k: f.id for k, f in self.remote_folders.items()}
        return make_plan(self.local, self.remote_folders, self.remote_files, self.state)

    def target(self) -> str:
        return f"{self.cfg.source_folder}|{self.cfg.onedrive_folder}"

    def needs_remote_check(self) -> bool:
        """Can the background check rely on the record of the last backup, or
        must it look at OneDrive itself (no record yet, settings changed, or the
        last look at OneDrive is older than REMOTE_CHECK_SECONDS)?"""
        state, now = self.state, time.time()
        last = state.last_remote_check
        return (not state.data.get("drive_id") or state.recorded_folders() is None
                or state.data.get("target") != self.target()
                or not last or now - last > REMOTE_CHECK_SECONDS or last > now + 300)

    def local_changes(self) -> str | None:
        """Compare the local folder with the record of the last backup without
        contacting OneDrive. Returns why a backup is needed, or None."""
        state, cfg = self.state, self.cfg
        if not os.path.isdir(self.base):
            raise FatalRunError(f"The folder to back up does not exist or is not reachable: {cfg.source_folder}")
        self.local = local = scan_local(cfg.source_folder, len(cfg.onedrive_folder), None, self.stopper.check)
        if "./" in local.unreadable:
            raise FatalRunError("The folder to back up could not be read: " + local.unreadable["./"])
        records = state.data["files"]
        changed = []
        for key, lf in local.files.items():
            record = records.get(key)
            if record and record[0] == lf.size and record[1] == lf.mtime_ns:
                continue
            if state.recent_failure(key, lf.size, lf.mtime_ns, FAILURE_QUIET_SECONDS):
                continue
            changed.append(lf.path)
        recorded = state.recorded_folders() or set()
        new_folders = [rel for key, rel in local.folders.items() if key not in recorded
                       and not state.recent_other_failure("folder:" + key, FAILURE_QUIET_SECONDS)]
        gone = [key for key in records if key not in local.files]
        gone_folders = [key for key in recorded if key not in local.folders]
        if changed or new_folders:
            return (f"{len(changed):,} new/changed {'file' if len(changed) == 1 else 'files'} and "
                    f"{len(new_folders):,} new {'folder' if len(new_folders) == 1 else 'folders'} on D:")
        if gone or gone_folders:
            notice = state.data.get("deletion_notice") or {}
            if notice.get("at", 0) > time.time() - FAILURE_QUIET_SECONDS:
                return None      # already held back by the safety stop recently
            return (f"{n_files(len(gone))} and {len(gone_folders):,} "
                    f"{'folder' if len(gone_folders) == 1 else 'folders'} deleted on D:")
        return None

    def record_folders_baseline(self) -> None:
        """Remember the local folders as backed up (after a run or a check that
        found everything in sync)."""
        failed = {key_for(p.rstrip("/")) for p in self.result.failed if p.endswith("/")}
        self.state.set_folders(k for k in self.local.folders if k not in failed)

    def has_work_for_scheduler(self, plan: Plan) -> bool:
        """Should the 10-minute check open a window? Leave out things that failed
        recently and have not changed, so a stubborn file or folder does not
        cause a pop-up every 10 minutes."""
        def fresh(lf: LocalFile) -> bool:
            return not self.state.recent_failure(key_for(lf.path), lf.size, lf.mtime_ns, FAILURE_QUIET_SECONDS)

        if any(fresh(lf) for lf, _ in plan.to_upload) or any(fresh(lf) for lf, _ in plan.to_check):
            return True
        if any(not self.state.recent_other_failure("folder:" + key_for(rel), FAILURE_QUIET_SECONDS)
               for rel in plan.folders_missing):
            return True
        wanted = build_deletions(plan.conflicts.folders, plan.conflicts.files, self.remote_files)
        if self.mirroring_active():
            wanted = build_deletions(wanted.folders + plan.deletions.folders,
                                     wanted.files + plan.deletions.files, self.remote_files)
        if not any(not self.state.recent_other_failure("delete:" + item.id, FAILURE_QUIET_SECONDS)
                   for item in wanted.items()):
            return False
        limited = self._limited(wanted, plan)
        if not limited or self.deletions_allowed(limited):
            return True
        notice = self.state.data.get("deletion_notice") or {}
        return not (notice.get("count") == limited.file_count
                    and notice.get("at", 0) > time.time() - FAILURE_QUIET_SECONDS)

    # --- full run ---------------------------------------------------------------

    def run(self) -> RunResult:
        started = time.monotonic()
        res = self.result
        try:
            with KeepAwake():
                monitor = _WifiMonitor(self.wifi_problem, self.stopper)
                monitor.start()
                try:
                    self._run_steps()
                finally:
                    monitor.stop()
        except RunStopped as exc:
            res.status, res.message = "stopped", str(exc) or "Stopped."
        except KeyboardInterrupt:
            self.stopper.stop("Stopped by you (Ctrl+C).")
            res.status, res.message = "stopped", "Stopped by you (Ctrl+C)."
        except SignInRequired as exc:
            res.status, res.message = "failed", str(exc) + " Run the backup again to sign in."
        except FatalRunError as exc:
            res.status, res.message = "failed", str(exc)
        except GraphError as exc:
            res.status, res.message = "failed", f"OneDrive error: {exc}"
        finally:
            self.console.end_block()
            res.seconds = time.monotonic() - started
            self.state.save()
        if res.status == "complete" and (res.failed or res.deletions_blocked or res.unreadable
                                         or res.not_allowed or res.links_skipped):
            res.status = "finished_with_problems"
        return res

    def _run_steps(self) -> None:
        cfg, res, say = self.cfg, self.result, self.console.say
        if on_battery():
            say("Note: running on battery. Windows may still go to sleep a few minutes after the screen "
                "turns off; if that happens, the backup simply continues the next time it runs.")
        plan = self.prepare(show_progress=True)
        local = self.local
        res.local_files, res.local_bytes = len(local.files), local.total_bytes
        res.unreadable.update(local.unreadable)
        res.not_allowed.update(local.not_allowed)
        res.links_skipped = list(local.links_skipped)
        for rel in local.links_skipped:
            log.warning("Not backed up (shortcut/link to another folder): %s", rel)
        mirroring = self.mirroring_active()
        res.mirroring = "on" if mirroring else ("off" if cfg.mirror_deletions == "off" else
                                               "starts after the first complete backup")
        res.approve_command = f"{self.command} --approve-deletions"

        if cfg.dry_run:
            res.up_to_date = plan.up_to_date
            self._report_dry_run(plan, mirroring)
            res.status, res.message = "dry_run", ""
            return

        if plan.to_check:
            self._check_phase(plan)
        res.up_to_date = plan.up_to_date

        # Items in the way of a local item of the other type are always replaced
        # (that is part of backing up); deletions of removed items only once
        # mirroring is on. A single OneDrive file that is now a local folder is
        # replaced like any overwritten file; everything else counts towards the
        # safety stop.
        now = plan.conflicts
        if mirroring:
            now = build_deletions(plan.conflicts.folders + plan.deletions.folders,
                                  plan.conflicts.files + plan.deletions.files, self.remote_files)
        limited = self._limited(now, plan)
        if limited and not self.deletions_allowed(limited):
            self._block_deletions(limited)
            now = build_deletions([], plan.conflicts.files, self.remote_files)
        conflicts_now = [i for i in now.items() if i in plan.conflicts.items()]
        if conflicts_now:
            self._delete_phase(build_deletions([i for i in conflicts_now if isinstance(i, RemoteFolder)],
                                               [i for i in conflicts_now if isinstance(i, RemoteFile)],
                                               self.remote_files), conflicts=True)

        if plan.to_upload:
            self._upload_phase(plan.to_upload)
        self._create_missing_folders(plan.folders_missing)

        rest = [i for i in now.items() if i not in conflicts_now]
        if rest:
            rest = self._hold_back_moved(rest)
            self._delete_phase(build_deletions([i for i in rest if isinstance(i, RemoteFolder)],
                                               [i for i in rest if isinstance(i, RemoteFile)], self.remote_files))

        self.state.prune_files(set(local.files))
        if not self.stopper.stopped:
            self.record_folders_baseline()
        if not mirroring and plan.deletions:
            self._report_pending_deletions(plan.deletions)
        if not res.failed and not self.stopper.stopped and not self.state.first_complete_backup:
            self.state.mark_first_complete()
            if cfg.mirror_deletions == "auto":
                res.mirroring = "on from the next run"
                say("\nFirst complete backup finished. From the next run, files you delete in "
                    f"{cfg.source_folder} will also be removed from OneDrive (to its recycle bin).")
                log.info("First complete backup finished; mirroring of deletions is now on.")

    def _limited(self, deletions: Deletions, plan: Plan) -> Deletions:
        """The part of `deletions` that the safety stop applies to."""
        return build_deletions(deletions.folders,
                               [f for f in deletions.files if f not in plan.conflicts.files], self.remote_files)

    def _block_deletions(self, deletions: Deletions) -> None:
        cfg, res = self.cfg, self.result
        res.deletions_blocked = deletions.file_count
        self.state.data["deletion_notice"] = {"count": deletions.file_count, "at": time.time()}
        self.console.say(
            f"\nNOT deleting {n_files(deletions.file_count)} ({fmt_bytes(deletions.byte_count)}) from OneDrive: "
            f"that is more than {cfg.mirror_safety_limit_percent:g}% of the backup. The list is in backup.log.\n"
            f"If you really removed them from {cfg.source_folder}, run this once in cmd:\n"
            f"  {res.approve_command}\n")
        log.warning("Mirroring safety stop: %d files / %s would be deleted (limit %g%% of %d files / %s).",
                    deletions.file_count, fmt_bytes(deletions.byte_count), cfg.mirror_safety_limit_percent,
                    len(self.remote_files), fmt_bytes(self.remote_bytes))
        for item in deletions.items():
            log.warning("  would delete: %s", item.path)

    def _report_pending_deletions(self, pending: Deletions) -> None:
        res = self.result
        res.pending_deletions = pending.file_count
        for item in pending.items():
            log.info("In OneDrive but not in the local folder: %s", item.path)
        if self.cfg.mirror_deletions == "off":
            return
        self.console.say(
            f"\n{n_files(pending.file_count)} ({fmt_bytes(pending.byte_count)}) are in OneDrive but no longer in "
            f"{self.cfg.source_folder}. Once mirroring is on (after the first complete backup) they will be "
            "moved to the OneDrive recycle bin. The list is in backup.log.")

    def _hold_back_moved(self, items: list) -> list:
        """Do not delete the old copy of a file that seems to have been moved or
        renamed (same name and size) when its new copy could not be uploaded."""
        if not self._failed_local:
            return items
        moved = {(lf.path.rpartition("/")[2].casefold(), lf.size) for lf in self._failed_local}

        def matches(rf: RemoteFile) -> bool:
            return (rf.path.rpartition("/")[2].casefold(), rf.size) in moved

        keep = []
        for item in items:
            if isinstance(item, RemoteFile):
                hold = matches(item)
            else:
                prefix = key_for(item.path) + "/"
                hold = any(k.startswith(prefix) and matches(f) for k, f in self.remote_files.items())
            if hold:
                keep.append(item)
                log.info("Kept in OneDrive until its new copy is uploaded: %s", item.path)
        self.result.held_back = len(keep)
        return [i for i in items if i not in keep]

    # --- phase: checksum comparison ---------------------------------------------

    def _check_phase(self, plan: Plan) -> None:
        items = plan.to_check
        total = sum(lf.size for lf, _ in items)
        self.console.say(f"\nChecking {n_files(len(items))} that are already in OneDrive "
                         f"({fmt_bytes(total)}). This reads them from {self.cfg.source_folder} once.")
        progress = TransferProgress(total, len(items))
        heading = "D-Drive Backup - comparing local files with the copies in OneDrive"
        with Ticker(lambda: self.console.show(*progress.lines(heading))):
            for lf, rf in items:
                self.stopper.check()
                key = key_for(lf.path)
                progress.begin_file(lf.path)
                read = 0

                def on_bytes(n: int) -> None:
                    nonlocal read
                    read += n
                    progress.add_bytes(n)

                try:
                    digest = self._hash_local(lf, on_bytes)
                except LocalReadError as exc:
                    self._file_unreadable(lf, str(exc))
                    progress.end_file(leftover_bytes=max(0, lf.size - read), unreadable=True)
                    continue
                if digest == rf.hash:
                    self.state.set_file(key, lf.size, lf.mtime_ns, digest, rf.id)
                    plan.up_to_date += 1
                else:
                    plan.to_upload.append((lf, "different from the copy in OneDrive"))
                progress.end_file()
                self.state.save_soon()
        self.console.end_block()
        plan.to_upload.sort(key=lambda item: item[0].path)

    def _hash_local(self, lf: LocalFile, on_bytes: Callable[[int], None]) -> str:
        path = local_path(self.base, lf.path)
        hasher = QuickXorHash()
        try:
            with open(path, "rb") as f:
                while True:
                    self.stopper.check()
                    data = f.read(FRAGMENT_SIZE)
                    if not data:
                        break
                    hasher.update(data)
                    on_bytes(len(data))
        except OSError as exc:
            raise LocalReadError(describe_os_error(path, exc)) from exc
        if hasher.length != lf.size:
            raise LocalReadError("The file changed while it was being checked; it will be checked again next run.")
        return hasher.b64digest()

    # --- phase: uploads -----------------------------------------------------------

    def _upload_phase(self, items: list[tuple[LocalFile, str]]) -> None:
        total = sum(lf.size for lf, _ in items)
        say = self.console.say
        say(f"\nUploading {len(items):,} new/changed {'file' if len(items) == 1 else 'files'} ({fmt_bytes(total)}) "
            f"to OneDrive/{self.cfg.onedrive_folder}")
        for lf, reason in items:
            log.info("To upload (%s): %s", reason, lf.path)
        progress = TransferProgress(total, len(items))
        heading = f"D-Drive Backup - uploading {len(items):,} new/changed {'file' if len(items) == 1 else 'files'}"
        # One worker prefers big files, the others take small ones. That keeps the
        # network busy while small files wait on per-file overhead, and keeps the
        # mix steady so the ETA stays meaningful.
        large = deque(lf for lf, _ in items if lf.size >= LARGE_FILE_SIZE)
        small = deque(lf for lf, _ in items if lf.size < LARGE_FILE_SIZE)
        queue_lock = threading.Lock()

        def worker(prefer_large: bool) -> None:
            while not self.stopper.stopped:
                with queue_lock:
                    queues = (large, small) if prefer_large else (small, large)
                    lf = next((q.popleft() for q in queues if q), None)
                if lf is None:
                    return
                self._upload_one(lf, progress)

        workers = self.cfg.parallel_uploads
        with Ticker(lambda: self.console.show(*progress.lines(heading))):
            pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="upload")
            pending = {pool.submit(worker, i == 0) for i in range(workers)}
            try:
                while pending:
                    done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:
                        exc = future.exception()
                        if exc is not None and not isinstance(exc, RunStopped):
                            if isinstance(exc, (FatalRunError, SignInRequired)):
                                self.stopper.stop(str(exc))
                            else:
                                log.error("Unexpected error while uploading", exc_info=exc)
                                self.stopper.stop(f"Unexpected error: {exc}")
                            raise exc
            except KeyboardInterrupt:
                self.stopper.stop("Stopped by you (Ctrl+C).")
                raise
            finally:
                if self.stopper.stopped:
                    with queue_lock:
                        large.clear()
                        small.clear()
                pool.shutdown(wait=True)
                self.state.save()
        self.console.end_block()
        self.stopper.check()

    def _upload_one(self, lf: LocalFile, progress: TransferProgress) -> None:
        if self.stopper.stopped:
            raise RunStopped(self.stopper.reason)
        key = key_for(lf.path)
        path = local_path(self.base, lf.path)
        progress.begin_file(lf.path)
        sent = 0

        def on_bytes(n: int) -> None:
            nonlocal sent
            sent += n
            progress.add_bytes(n)

        try:
            parent_rel, _, name = lf.path.rpartition("/")
            parent_id = self._folder_id(parent_rel)
            item, digest = self.graph.upload_file(
                parent_id, name, path, lf.size, file_times(lf), on_bytes,
                saved_session=self.state.session_for(key, lf.size, lf.mtime_ns),
                remember_session=lambda url: self.state.set_session(key, url, lf.size, lf.mtime_ns))
        except LocalReadError as exc:
            self._file_unreadable(lf, str(exc))
            progress.end_file(leftover_bytes=lf.size - sent, unreadable=True)
            return
        except (FileFailed, GraphError) as exc:
            self._file_failed(lf, str(exc))
            progress.end_file(leftover_bytes=lf.size - sent, failed=True)
            return
        except (RunStopped, FatalRunError):
            progress.end_file(leftover_bytes=0)
            raise
        except SignInRequired as exc:
            progress.end_file(leftover_bytes=0)
            self.stopper.stop(str(exc) + " Run the backup again to sign in.")
            raise RunStopped(self.stopper.reason) from exc
        try:
            st = os.stat(path)
            unchanged = st.st_size == lf.size and st.st_mtime_ns == lf.mtime_ns
        except OSError:
            unchanged = False
        if unchanged:
            self.state.set_file(key, lf.size, lf.mtime_ns, digest, item["id"])
        else:
            log.warning("Changed while uploading (will be uploaded again next run): %s", lf.path)
            self.state.forget_files([key])
        log.info("Uploaded: %s (%s)", lf.path, fmt_bytes(lf.size))
        with self._result_lock:
            self.result.uploaded += 1
            self.result.uploaded_bytes += lf.size
        progress.end_file(leftover_bytes=lf.size - sent)
        self.state.save_soon()

    def _folder_id(self, rel: str) -> str:
        """OneDrive id of the folder at relative path `rel`, creating it if needed."""
        if not rel:
            return self.root_id
        key = key_for(rel)
        with self._folder_lock:
            if key in self._folder_ids:
                return self._folder_ids[key]
            parent_rel, _, name = rel.rpartition("/")
            parent_id = self._folder_id(parent_rel)
            item = self.graph.create_folder(parent_id, name)
            log.info("Created OneDrive folder: %s", rel)
            self._folder_ids[key] = item["id"]
            self.state.clear_other_failure("folder:" + key)
            with self._result_lock:
                self.result.folders_created += 1
            return item["id"]

    def _create_missing_folders(self, folders: list[str]) -> None:
        for rel in folders:
            self.stopper.check()
            try:
                self._folder_id(rel)
            except (FileFailed, GraphError) as exc:
                self.result.failed[rel + "/"] = str(exc)
                self.state.set_other_failure("folder:" + key_for(rel), str(exc))
                log.error("Could not create OneDrive folder %s: %s", rel, exc)

    # --- phase: deletions ---------------------------------------------------------

    def _delete_phase(self, deletions: Deletions, conflicts: bool = False) -> None:
        if not deletions:
            return
        if conflicts:
            self.console.say(f"Replacing {len(deletions.items()):,} OneDrive items that are now a "
                             "different type (file <-> folder) locally (old ones go to the recycle bin)...")
        else:
            self.console.say(f"Removing {n_files(deletions.file_count)} that you deleted locally from OneDrive "
                             "(they go to the OneDrive recycle bin)...")
        for item in deletions.items():
            self.stopper.check()
            try:
                self.graph.delete_item(item.id)
            except GraphError as exc:
                self.result.failed[item.path] = f"could not delete from OneDrive: {exc}"
                self.state.set_other_failure("delete:" + item.id, str(exc))
                log.error("Could not delete %s from OneDrive: %s", item.path, exc)
                continue
            key = key_for(item.path)
            removed = [k for k in self.remote_files if k == key or k.startswith(key + "/")]
            with self._folder_lock:
                for k in [k for k in self._folder_ids if k == key or k.startswith(key + "/")]:
                    del self._folder_ids[k]
            self.result.deleted_files += len(removed)
            log.info("Deleted from OneDrive (%s): %s", "replaced" if conflicts else "mirror", item.path)

    # --- bookkeeping --------------------------------------------------------

    def _file_unreadable(self, lf: LocalFile, reason: str) -> None:
        log.error("Could not read %s: %s", lf.path, reason)
        with self._result_lock:
            self.result.unreadable[lf.path] = reason
            self._failed_local.append(lf)
        self.state.set_failure(key_for(lf.path), lf.size, lf.mtime_ns, reason, kind="unreadable")

    def _file_failed(self, lf: LocalFile, reason: str) -> None:
        log.error("Upload failed for %s: %s", lf.path, reason)
        with self._result_lock:
            self.result.failed[lf.path] = reason
            self._failed_local.append(lf)
        self.state.set_failure(key_for(lf.path), lf.size, lf.mtime_ns, reason)

    def _report_dry_run(self, plan: Plan, mirroring: bool) -> None:
        say = self.console.say
        say("\nDRY RUN - nothing will be changed.")
        say(f"  Up to date:        {plan.up_to_date:,}")
        say(f"  Would compare:     {n_files(len(plan.to_check))} by checksum")
        say(f"  Would upload:      {n_files(len(plan.to_upload))} "
            f"({fmt_bytes(sum(lf.size for lf, _ in plan.to_upload))})")
        say(f"  Folders to create: {len(plan.folders_missing):,}")
        if plan.conflicts:
            say(f"  Would replace:     {len(plan.conflicts.items()):,} OneDrive items that are now a different "
                "type (file <-> folder) locally")
        gone = plan.deletions
        if mirroring:
            say(f"  Would delete:      {n_files(gone.file_count)} ({fmt_bytes(gone.byte_count)}) from OneDrive")
        else:
            when = ("never: mirroring is off" if self.cfg.mirror_deletions == "off"
                    else "they will be deleted once mirroring is on (after the first complete backup)")
            say(f"  In OneDrive but not in the local folder: {n_files(gone.file_count)} "
                f"({fmt_bytes(gone.byte_count)}); {when}")
        wanted = build_deletions(plan.conflicts.folders + (gone.folders if mirroring else []),
                                 plan.conflicts.files + (gone.files if mirroring else []), self.remote_files)
        wanted = self._limited(wanted, plan)
        if wanted and not self.deletions_allowed(wanted):
            say(f"  Note: that is more than the {self.cfg.mirror_safety_limit_percent:g}% safety limit, so a real "
                "run would delete nothing until you approve it with --approve-deletions.")
        say("  The full lists are in backup.log.")
        for lf, reason in plan.to_upload:
            log.info("DRY RUN upload (%s): %s", reason, lf.path)
        for item in plan.conflicts.items():
            log.info("DRY RUN replace (different type): %s", item.path)
        for item in gone.items():
            log.info("DRY RUN %s: %s", "delete" if mirroring else "in OneDrive but not local", item.path)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def file_times(lf: LocalFile) -> dict | None:
    """The file's dates for OneDrive, or None if they cannot be expressed
    (computed without the C library, which on Windows rejects dates before 1970)."""
    def iso(ns: int) -> str:
        d = _EPOCH + timedelta(microseconds=ns // 1000)
        return f"{d.year:04d}-{d.month:02d}-{d.day:02d}T{d.hour:02d}:{d.minute:02d}:{d.second:02d}Z"
    try:
        return {"createdDateTime": iso(lf.ctime_ns), "lastModifiedDateTime": iso(lf.mtime_ns)}
    except (OverflowError, ValueError, OSError):
        return None


class _WifiMonitor:
    """Stops the run soon after the laptop leaves the office Wi-Fi."""

    def __init__(self, problem: Callable[[], str | None], stopper):
        self._problem, self._stopper = problem, stopper
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="wifi", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._done.set()

    def _loop(self) -> None:
        while not self._done.wait(WIFI_RECHECK_SECONDS):
            try:
                reason = self._problem()
            except Exception:
                reason = None
            if reason:
                self._stopper.stop(reason)
                log.warning(reason)
                return


def summary_lines(res: RunResult, cfg: Config) -> list[str]:
    title = {"complete": "Backup complete", "finished_with_problems": "Backup finished, with problems (see below)",
             "stopped": "Backup stopped", "failed": "Backup failed",
             "dry_run": "Dry run finished - nothing was changed"}[res.status]
    lines = ["", "=" * 70, title + (f": {res.message}" if res.message else ""), "=" * 70]
    if res.local_files:
        lines.append(f"Local folder:        {n_files(res.local_files)}, {fmt_bytes(res.local_bytes)}")
    lines.append(f"Already up to date:  {res.up_to_date:,}")
    if res.status != "dry_run":
        lines.append(f"Uploaded:            {n_files(res.uploaded)} ({fmt_bytes(res.uploaded_bytes)})")
    if res.folders_created:
        lines.append(f"Folders created:     {res.folders_created:,}")
    if res.deleted_files:
        lines.append(f"Removed from OneDrive: {n_files(res.deleted_files)} (in the OneDrive recycle bin)")
    if res.held_back:
        lines.append(f"Kept in OneDrive until their moved copy uploads: {res.held_back:,}")
    if res.pending_deletions and cfg.mirror_deletions != "off":
        lines.append(f"In OneDrive but no longer local: {n_files(res.pending_deletions)} "
                     "(removed once mirroring is on)")
    if res.deletions_blocked:
        lines.append(f"Deletions held back by the safety limit: {n_files(res.deletions_blocked)}. To allow them:")
        lines.append(f"   {res.approve_command}")
    for label, items in (("Could not read (skipped)", res.unreadable), ("Not allowed by OneDrive", res.not_allowed),
                         ("Failed", res.failed)):
        if items:
            lines.append(f"{label}: {len(items):,}")
            for path, reason in list(items.items())[:15]:
                lines.append(f"   {path}\n      -> {reason}")
            if len(items) > 15:
                lines.append(f"   ... and {len(items) - 15:,} more (see backup.log)")
    if res.links_skipped:
        lines.append(f"Not backed up (shortcut/link to another folder): {len(res.links_skipped):,}")
        lines += [f"   {p}" for p in res.links_skipped[:15]]
    if res.mirroring:
        lines.append(f"Mirroring of deletions: {res.mirroring}")
    lines.append(f"Took {fmt_duration(res.seconds)}")
    return lines
