"""One backup run: scan both sides, check, upload, mirror deletions, report."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .auth import SignInRequired
from .config import Config
from .graph import (FRAGMENT_SIZE, LARGE_FILE_SIZE, FatalRunError, FileFailed, GraphClient, GraphError,
                    LocalReadError, RemoteFile, RemoteFolder, RunStopped)
from .names import key_for
from .progress import Console, Ticker, TransferProgress, fmt_bytes, fmt_duration
from .quickxorhash import EMPTY_HASH, QuickXorHash
from .scan import LocalFile, LocalScan, local_path, scan_local
from .state import State
from .winutils import KeepAwake, describe_os_error, long_path

log = logging.getLogger(__name__)

FAILURE_QUIET_SECONDS = 24 * 3600   # a file that failed is retried by the 10-minute check after this
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


@dataclass
class Plan:
    up_to_date: int = 0
    to_check: list[tuple[LocalFile, RemoteFile]] = field(default_factory=list)
    to_upload: list[tuple[LocalFile, str]] = field(default_factory=list)
    folders_missing: list[str] = field(default_factory=list)
    deletions: Deletions = field(default_factory=Deletions)


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
    plan.deletions = plan_deletions(local, remote_folders, remote_files)
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

    result = Deletions()
    for key, folder in sorted(remote_folders.items()):
        if key and key not in local.folders and parent_exists_locally(key) and not is_protected(key):
            result.folders.append(folder)
            inside = [f for k, f in remote_files.items() if k.startswith(key + "/")]
            result.file_count += len(inside)
            result.byte_count += sum(f.size for f in inside)
    for key, rf in sorted(remote_files.items()):
        if key not in local.files and parent_exists_locally(key) and not is_protected(key):
            result.files.append(rf)
            result.file_count += 1
            result.byte_count += rf.size
    return result


# ----------------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------------

@dataclass
class RunResult:
    status: str = "complete"           # complete | finished_with_problems | stopped | failed | nothing_to_do
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
    deletions_blocked: int = 0
    mirroring: str = ""
    seconds: float = 0.0


class BackupRun:
    def __init__(self, cfg: Config, graph: GraphClient, state: State, console: Console,
                 wifi_problem: Callable[[], str | None], approve_deletions: bool = False):
        self.cfg, self.graph, self.state, self.console = cfg, graph, state, console
        self.wifi_problem = wifi_problem
        self.approve_deletions = approve_deletions
        self.stopper = graph.stopper
        self.result = RunResult()
        self._result_lock = threading.Lock()
        self.root_id = ""
        self.local: LocalScan | None = None
        self.remote_folders: dict[str, RemoteFolder] = {}
        self.remote_files: dict[str, RemoteFile] = {}
        self._folder_ids: dict[str, str] = {}
        self._folder_lock = threading.RLock()

    # --- shared steps (also used by the hidden 10-minute check) ---------------

    def mirroring_active(self) -> bool:
        mode = self.cfg.mirror_deletions
        return mode == "on" or (mode == "auto" and bool(self.state.first_complete_backup))

    def deletions_allowed(self, deletions: Deletions) -> bool:
        limit = self.cfg.mirror_safety_limit_percent / 100.0 * len(self.remote_files)
        return self.approve_deletions or deletions.file_count <= limit

    def prepare(self, show_progress: bool) -> Plan:
        """Find the OneDrive folder, scan both sides and work out what to do."""
        cfg, say = self.cfg, self.console.say
        drive = self.graph.get_drive()
        root = self.graph.ensure_folder_path(cfg.onedrive_folder, create=not cfg.dry_run)
        self.root_id = root["id"] if root else ""
        if self.state.bind(drive["id"], self.root_id or "(not created yet)"):
            log.info("The OneDrive backup folder changed since the last run; its saved record was reset.")

        if not os.path.isdir(long_path(cfg.source_folder)):
            raise FatalRunError(f"The folder to back up does not exist or is not reachable: {cfg.source_folder}")
        say(f"Scanning {cfg.source_folder} ...")
        scan_note = (lambda n: self.console.show([f"  {n:,} files found"])) if show_progress else None
        self.local = scan_local(cfg.source_folder, len(cfg.onedrive_folder), scan_note, self.stopper.check)
        if "./" in self.local.unreadable:
            raise FatalRunError("The folder to back up could not be read: " + self.local.unreadable["./"])
        self.console.end_block()
        say(f"  {len(self.local.files):,} files, {fmt_bytes(self.local.total_bytes)}")

        if self.root_id:
            say(f"Reading OneDrive/{cfg.onedrive_folder} ...")
            list_note = (lambda f, d: self.console.show([f"  {f:,} files in {d:,} folders"])) if show_progress else None
            self.remote_folders, self.remote_files = self.graph.list_tree(self.root_id, list_note)
            self.console.end_block()
            say(f"  {len(self.remote_files):,} files already in OneDrive")
        else:
            self.remote_folders, self.remote_files = {"": RemoteFolder("", "")}, {}
        self._folder_ids = {k: f.id for k, f in self.remote_folders.items()}
        return make_plan(self.local, self.remote_folders, self.remote_files, self.state)

    def has_work_for_scheduler(self, plan: Plan) -> bool:
        """Should the 10-minute check open a window? Ignore files that failed
        recently and have not changed, so a stubborn file does not cause a
        pop-up every 10 minutes."""
        def fresh(lf: LocalFile) -> bool:
            return not self.state.recent_failure(key_for(lf.path), lf.size, lf.mtime_ns, FAILURE_QUIET_SECONDS)

        if any(fresh(lf) for lf, _ in plan.to_upload) or any(fresh(lf) for lf, _ in plan.to_check):
            return True
        if plan.folders_missing:
            return True
        if self.mirroring_active() and plan.deletions:
            if self.deletions_allowed(plan.deletions):
                return True
            notice = self.state.data.get("deletion_notice") or {}
            return not (notice.get("count") == plan.deletions.file_count
                        and notice.get("at", 0) > time.time() - FAILURE_QUIET_SECONDS)
        return False

    # --- full run ---------------------------------------------------------------

    def run(self) -> RunResult:
        started = time.monotonic()
        res, say = self.result, self.console.say
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
            res.seconds = time.monotonic() - started
            self.state.save()
        if res.status == "complete" and (res.failed or res.deletions_blocked or res.unreadable):
            res.status = "finished_with_problems"
        return res

    def _run_steps(self) -> None:
        cfg, res, say = self.cfg, self.result, self.console.say
        plan = self.prepare(show_progress=True)
        local = self.local
        res.local_files, res.local_bytes = len(local.files), local.total_bytes
        res.unreadable.update(local.unreadable)
        res.not_allowed.update(local.not_allowed)
        for rel in local.links_skipped:
            log.info("Not following shortcut/link: %s", rel)
        mirroring = self.mirroring_active()
        res.mirroring = "on" if mirroring else ("off" if cfg.mirror_deletions == "off" else
                                               "starts after the first complete backup")

        if cfg.dry_run:
            self._report_dry_run(plan, mirroring)
            res.status, res.message = "complete", "Dry run: nothing was changed."
            return

        if plan.to_check:
            self._check_phase(plan)
        res.up_to_date = plan.up_to_date

        deletions = plan.deletions if mirroring else Deletions()
        if deletions and not self.deletions_allowed(deletions):
            res.deletions_blocked = deletions.file_count
            self.state.data["deletion_notice"] = {"count": deletions.file_count, "at": time.time()}
            say(f"\nNOT deleting {deletions.file_count:,} files from OneDrive: that is more than "
                f"{cfg.mirror_safety_limit_percent:g}% of the backup. If you really removed them from "
                f"{cfg.source_folder}, run:\n  python DDriveOneDriveBackup.py --approve-deletions\n")
            log.warning("Mirroring safety stop: %d files would be deleted (limit %g%% of %d).",
                        deletions.file_count, cfg.mirror_safety_limit_percent, len(self.remote_files))
            for item in (deletions.folders + deletions.files)[:50]:
                log.warning("  would delete: %s", item.path)
            deletions = Deletions()
        if deletions:
            # Remove items whose name a local file or folder now needs (e.g. a
            # file that became a folder) before uploading.
            needed = set(local.files) | set(local.folders)
            first = Deletions(folders=[f for f in deletions.folders if key_for(f.path) in needed],
                              files=[f for f in deletions.files if key_for(f.path) in needed])
            self._delete_phase(first)

        if plan.to_upload:
            self._upload_phase(plan.to_upload)
        self._create_missing_folders(plan.folders_missing)

        if deletions:
            rest = Deletions(folders=[f for f in deletions.folders if f not in first.folders],
                             files=[f for f in deletions.files if f not in first.files])
            self._delete_phase(rest)

        self.state.prune_files(set(local.files))
        if not res.failed and not self.stopper.stopped:
            if not self.state.first_complete_backup:
                self.state.mark_first_complete()
                if cfg.mirror_deletions == "auto":
                    res.mirroring = "on from the next run"
                    say("\nFirst complete backup finished. From the next run, files you delete in "
                        f"{cfg.source_folder} will also be removed from OneDrive (to its recycle bin).")
                    log.info("First complete backup finished; mirroring of deletions is now on.")

    # --- phase: checksum comparison ---------------------------------------------

    def _check_phase(self, plan: Plan) -> None:
        items = plan.to_check
        total = sum(lf.size for lf, _ in items)
        self.console.say(f"\nChecking {len(items):,} files that are already in OneDrive "
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
        path = local_path(self.cfg.source_folder, lf.path)
        hasher = QuickXorHash()
        try:
            with open(long_path(path), "rb") as f:
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
        say(f"\nUploading {len(items):,} new/changed files ({fmt_bytes(total)}) "
            f"to OneDrive/{self.cfg.onedrive_folder}")
        for lf, reason in items:
            log.info("To upload (%s): %s", reason, lf.path)
        progress = TransferProgress(total, len(items))
        heading = f"D-Drive Backup - uploading {len(items):,} new/changed files"
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
        path = local_path(self.cfg.source_folder, lf.path)
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
                parent_id, name, path, lf.size, _file_times(lf), on_bytes,
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
        except RunStopped:
            progress.end_file(leftover_bytes=0)
            raise
        except SignInRequired as exc:
            self.stopper.stop(str(exc) + " Run the backup again to sign in.")
            raise RunStopped(self.stopper.reason) from exc
        try:
            st = os.stat(long_path(path))
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
                log.error("Could not create OneDrive folder %s: %s", rel, exc)

    # --- phase: mirror deletions ------------------------------------------------

    def _delete_phase(self, deletions: Deletions) -> None:
        if not deletions:
            return
        self.console.say(f"Removing {deletions.file_count:,} files that you deleted locally from OneDrive "
                         "(they go to the OneDrive recycle bin)...")
        for item in deletions.folders + deletions.files:
            self.stopper.check()
            try:
                self.graph.delete_item(item.id)
            except GraphError as exc:
                self.result.failed[item.path] = f"could not delete from OneDrive: {exc}"
                log.error("Could not delete %s from OneDrive: %s", item.path, exc)
                continue
            key = key_for(item.path)
            removed = [k for k in self.remote_files if k == key or k.startswith(key + "/")]
            with self._folder_lock:
                for k in [k for k in self._folder_ids if k == key or k.startswith(key + "/")]:
                    del self._folder_ids[k]
            self.result.deleted_files += len(removed)
            log.info("Deleted from OneDrive (mirror): %s", item.path)

    # --- bookkeeping --------------------------------------------------------

    def _file_unreadable(self, lf: LocalFile, reason: str) -> None:
        log.error("Could not read %s: %s", lf.path, reason)
        with self._result_lock:
            self.result.unreadable[lf.path] = reason
        self.state.set_failure(key_for(lf.path), lf.size, lf.mtime_ns, reason)

    def _file_failed(self, lf: LocalFile, reason: str) -> None:
        log.error("Upload failed for %s: %s", lf.path, reason)
        with self._result_lock:
            self.result.failed[lf.path] = reason
        self.state.set_failure(key_for(lf.path), lf.size, lf.mtime_ns, reason)

    def _report_dry_run(self, plan: Plan, mirroring: bool) -> None:
        say = self.console.say
        say("\nDRY RUN - nothing will be changed.")
        say(f"  Up to date:        {plan.up_to_date:,}")
        say(f"  Would compare:     {len(plan.to_check):,} files by checksum")
        say(f"  Would upload:      {len(plan.to_upload):,} files "
            f"({fmt_bytes(sum(lf.size for lf, _ in plan.to_upload))})")
        say(f"  Folders to create: {len(plan.folders_missing):,}")
        if mirroring:
            say(f"  Would delete:      {plan.deletions.file_count:,} files from OneDrive")
        for lf, reason in plan.to_upload:
            log.info("DRY RUN upload (%s): %s", reason, lf.path)
        for item in plan.deletions.folders + plan.deletions.files:
            log.info("DRY RUN %s: %s", "delete" if mirroring else "not in local folder", item.path)


def _file_times(lf: LocalFile) -> dict:
    def iso(ns: int) -> str:
        return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"createdDateTime": iso(lf.ctime_ns), "lastModifiedDateTime": iso(lf.mtime_ns)}


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
                log.warning(reason)
                self._stopper.stop(reason)
                return


def summary_lines(res: RunResult, cfg: Config) -> list[str]:
    title = {"complete": "Backup complete", "finished_with_problems": "Backup finished, with problems (see below)",
             "stopped": "Backup stopped", "failed": "Backup failed", "nothing_to_do": "Nothing to do"}[res.status]
    lines = ["", "=" * 70, title + (f": {res.message}" if res.message else ""), "=" * 70]
    if res.local_files:
        lines.append(f"Local folder:        {res.local_files:,} files, {fmt_bytes(res.local_bytes)}")
    lines += [f"Already up to date:  {res.up_to_date:,}",
              f"Uploaded:            {res.uploaded:,} files ({fmt_bytes(res.uploaded_bytes)})"]
    if res.folders_created:
        lines.append(f"Folders created:     {res.folders_created:,}")
    if res.deleted_files:
        lines.append(f"Removed from OneDrive (deleted locally): {res.deleted_files:,} files")
    if res.deletions_blocked:
        lines.append(f"Deletions held back by the safety limit: {res.deletions_blocked:,} files "
                     "(run with --approve-deletions to allow)")
    for label, items in (("Could not read (skipped)", res.unreadable), ("Not allowed by OneDrive", res.not_allowed),
                         ("Failed", res.failed)):
        if items:
            lines.append(f"{label}: {len(items):,}")
            for path, reason in list(items.items())[:15]:
                lines.append(f"   {path}\n      -> {reason}")
            if len(items) > 15:
                lines.append(f"   ... and {len(items) - 15:,} more (see backup.log)")
    if res.mirroring:
        lines.append(f"Mirroring of deletions: {res.mirroring}")
    lines.append(f"Took {fmt_duration(res.seconds)}")
    return lines
