"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from . import schedule
import requests

from .auth import SignInRequired, TokenProvider, remove_stale_cache_lock
from .config import Config, ConfigError, load_config
from .graph import FatalRunError, GraphClient, GraphError, RunStopped, Stopper
from .progress import Console
from .state import State
from .sync import BackupRun, summary_lines
from .winutils import (SingleInstanceLock, console_python, current_wifi_ssids, open_in_new_window,
                       set_console_title, wait_or_keypress)

log = logging.getLogger("ddrive_backup")

EXIT_OK, EXIT_PROBLEMS, EXIT_CONFIG, EXIT_NOT_OFFICE, EXIT_BUSY, EXIT_STOPPED, EXIT_FAILED = 0, 1, 2, 3, 4, 5, 6
_STATUS_EXIT = {"complete": EXIT_OK, "dry_run": EXIT_OK, "finished_with_problems": EXIT_PROBLEMS,
                "stopped": EXIT_STOPPED, "failed": EXIT_FAILED}
CACHE_NAME = "auth_cache_dpapi.bin"
_OPEN_WINDOW = "open-window"

SsidFn = Callable[[], tuple]


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="DDriveOneDriveBackup.py",
        description="Back up a local folder to your work OneDrive (one-way mirror).")
    p.add_argument("--dry-run", action="store_true", help="show what would happen without changing anything")
    p.add_argument("--approve-deletions", action="store_true",
                   help="allow this run to delete more than the safety limit from OneDrive")
    p.add_argument("--any-network", action="store_true", help="run even when not on the office Wi-Fi")
    p.add_argument("--install-schedule", action="store_true",
                   help="check every scan_interval_minutes in the background (Windows Task Scheduler)")
    p.add_argument("--remove-schedule", action="store_true", help="remove the background schedule")
    p.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)   # hidden 10-minute check
    p.add_argument("--window", action="store_true", help=argparse.SUPPRESS)      # window opened by the check
    return p.parse_args(argv)


class _ConsoleLogHandler(logging.Handler):
    def __init__(self, console: Console):
        super().__init__(logging.WARNING)
        self.console = console

    def emit(self, record):
        try:
            self.console.say(("WARNING: " if record.levelno == logging.WARNING else "") + record.getMessage())
        except Exception:
            pass


def setup_logging(log_path: Path, console: Console | None) -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_ddrive", False)]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8", delay=True)
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    file_handler._ddrive = True
    root.addHandler(file_handler)
    if console is not None and console.enabled:
        console_handler = _ConsoleLogHandler(console)
        console_handler.addFilter(lambda r: r.name.startswith("ddrive_backup"))
        console_handler._ddrive = True
        root.addHandler(console_handler)
    for noisy in ("urllib3", "requests", "msal", "msal_extensions"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def close_logging() -> None:
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_ddrive", False)]:
        root.removeHandler(handler)
        handler.close()


def office_wifi_status(cfg: Config, any_network: bool, ssid_fn: SsidFn) -> tuple[bool, str]:
    if any_network or cfg.any_network:
        return True, "any network allowed"
    ssids, details = ssid_fn()
    wanted = " / ".join(cfg.office_wifi_ssids)
    if ssids is None:
        hint = ""
        if "location" in (details or "").lower():
            hint = (" Windows only reveals the Wi-Fi name when location access is on: Settings > Privacy & "
                    "security > Location > turn on \"Let desktop apps access your location\".")
        return False, f"Could not find out which Wi-Fi this PC is using ({(details or '').strip()[:200]}).{hint}"
    if any(s in cfg.office_wifi_ssids for s in ssids):
        return True, f"on {wanted}"
    return False, f"not connected to the office Wi-Fi \"{wanted}\" (current: {', '.join(ssids) or 'no Wi-Fi'})"


def wifi_problem_checker(cfg: Config, any_network: bool, ssid_fn: SsidFn) -> Callable[[], str | None]:
    """For use during a run: only report a problem when we positively know we
    are on a different network (an unreadable status does not stop the run)."""
    def problem() -> str | None:
        if any_network or cfg.any_network:
            return None
        ssids, _ = ssid_fn()
        if ssids is None or any(s in cfg.office_wifi_ssids for s in ssids):
            return None
        return (f"Left the office Wi-Fi (now: {', '.join(ssids) or 'no Wi-Fi'}). "
                "The backup continues next time you are connected.")
    return problem


def default_tokens(cfg: Config, base: Path, interactive: bool, show: Callable[[str], None]):
    return TokenProvider(cfg.client_id, cfg.tenant_id, base / CACHE_NAME, show, interactive)


def remove_old_plain_cache(base: Path) -> None:
    for name in ("auth_cache.bin", "auth_cache.bin.lockfile"):
        old = base / name
        if old.exists():
            try:
                old.unlink()
                if name == "auth_cache.bin":
                    log.info("Removed the old unencrypted sign-in file auth_cache.bin "
                             "(you will be asked to sign in once; the new one is encrypted).")
            except OSError as exc:
                log.warning("Could not remove %s: %s", old, exc)


def run_command(base: Path, script: Path) -> str:
    """The exact cmd line that runs this backup, for messages to the user."""
    python = console_python()
    try:
        rel = os.path.relpath(python, base)
        if not rel.startswith(".."):
            python = rel
    except ValueError:            # different drive
        pass
    if " " in python:
        python = f'"{python}"'
    return f'cd /d "{base}" && {python} {script.name}'


class ProblemNotice:
    """Remembers a lasting problem seen by the hidden 10-minute check (Wi-Fi name
    unreadable, folder missing, config mistake, ...). If it is still there after
    PERSIST seconds, the check opens the window to show it - at most once per
    REPEAT seconds - instead of failing silently forever."""

    PERSIST = 30 * 60
    REPEAT = 24 * 3600

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.debug("Could not remove %s: %s", self.path, exc)

    def should_show(self, kind: str, message: str, immediate: bool = False) -> bool:
        now = time.time()
        data = self._load()
        if data.get("kind") != kind:
            data = {"kind": kind, "first": now, "shown": 0}
        data["message"] = message
        repeat = 3600 if immediate else self.REPEAT
        show = (immediate or now - data["first"] >= self.PERSIST) and now - data.get("shown", 0) >= repeat
        if show:
            data["shown"] = now
        try:
            self.path.write_text(json.dumps(data), encoding="utf-8")
        except OSError as exc:
            log.debug("Could not save %s: %s", self.path, exc)
        return show


def window_is_open(base: Path) -> bool:
    probe = SingleInstanceLock(base / "window.lock")
    if probe.acquire():
        probe.release()
        return False
    return True


def main(argv=None, *, script_path: str, tokens_factory=default_tokens, ssid_fn: SsidFn = current_wifi_ssids,
         launch_window: Callable[[list[str], str], None] = open_in_new_window) -> int:
    args = parse_args(argv)
    script = Path(script_path).resolve()
    base = script.parent
    hidden = args.scheduled
    console = Console(enabled=not hidden)
    setup_logging(base / "backup.log", None if hidden else console)
    command = run_command(base, script)

    if args.install_schedule or args.remove_schedule:
        return manage_schedule(args, base, script, console, command)

    window_lock = None
    if args.window:                       # tells the hidden check that a window is already open
        window_lock = SingleInstanceLock(base / "window.lock")
        if not window_lock.acquire():
            window_lock = None
    try:
        code, attention = _main_locked(args, base, console, command, tokens_factory, ssid_fn)
        if code == _OPEN_WINDOW:
            close_logging()
            launch_window([str(script), "--window"], str(base))
            return EXIT_OK
        if args.window:
            close_logging()
            if attention:
                set_console_title("D-Drive Backup - needs your attention")
                console.say("\nPress any key to close this window.")
                wait_or_keypress(None)
            else:
                seconds = _window_seconds(base)
                if seconds:
                    console.say(f"\nThis window closes in {seconds} seconds (press any key to close it now).")
                    wait_or_keypress(seconds)
        return code
    finally:
        if window_lock is not None:
            window_lock.release()


def _window_seconds(base: Path) -> int:
    try:
        return load_config(base / "config.json").window_close_seconds
    except ConfigError:
        return 30


def _main_locked(args, base: Path, console: Console, command: str, tokens_factory, ssid_fn) -> tuple:
    """Everything that needs the single-instance lock. Returns (exit code or
    _OPEN_WINDOW, whether the window should stay open for the user)."""
    hidden = args.scheduled
    lock = SingleInstanceLock(base / "backup.lock")
    if not lock.acquire():
        if not hidden:
            console.say("Another backup is already running (maybe in another window). Try again later.")
        return EXIT_BUSY, False
    try:
        remove_stale_cache_lock(base / CACHE_NAME)
        try:
            cfg = load_config(base / "config.json")
        except ConfigError as exc:
            log.error("%s", exc)
            if hidden:
                if ProblemNotice(base / "check_problem.json").should_show("config", str(exc)):
                    return _OPEN_WINDOW, False
                return EXIT_CONFIG, False
            console.say(str(exc))
            console.say(f"Fix config.json in {base} and try again.")
            return EXIT_CONFIG, True
        if args.dry_run:
            cfg.dry_run = True
        remove_old_plain_cache(base)
        if hidden:
            return scheduled_check(cfg, base, tokens_factory, ssid_fn), False
        return run_backup(cfg, args, base, console, command, tokens_factory, ssid_fn)
    except KeyboardInterrupt:
        console.say("Stopped.")
        return EXIT_STOPPED, False
    except Exception as exc:   # last resort: never die without a trace in the log
        log.exception("Unexpected error: %s", exc)
        console.say(f"Unexpected error: {exc} (details in backup.log)")
        return EXIT_FAILED, True
    finally:
        lock.release()


def scheduled_check(cfg: Config, base: Path, tokens_factory, ssid_fn: SsidFn):
    """The hidden 10-minute check. Returns _OPEN_WINDOW when there is work, when
    sign-in is needed, or when a lasting problem should be shown."""
    notice = ProblemNotice(base / "check_problem.json")
    if cfg.dry_run:
        log.info("Scheduled check: \"dry_run\" is true in config.json, so the background check does nothing.")
        return EXIT_OK
    ssids, details = ssid_fn() if not cfg.any_network else ([], "")
    on_office, why = office_wifi_status(cfg, False, lambda: (ssids, details))
    if not on_office:
        log.info("Scheduled check: %s; skipped.", why)
        if ssids is None and notice.should_show("wifi-unknown", why):
            return _launch_if_no_window(base, "the Wi-Fi name could not be read")
        if ssids is not None:
            notice.clear()
        return EXIT_OK
    if window_is_open(base):
        log.info("Scheduled check: a backup window is already open; skipped.")
        return EXIT_OK
    kind = message = None
    try:
        tokens = tokens_factory(cfg, base, False, lambda text: None)
        stopper = Stopper()
        graph = GraphClient(tokens, stopper, wifi_problem_checker(cfg, False, ssid_fn))
        state = State(base / "backup_state.json")
        state.load()
        run = BackupRun(cfg, graph, state, Console(False), wifi_problem_checker(cfg, False, ssid_fn))
        tokens.get()
        plan = run.prepare(show_progress=False)
        state.save()
        notice.clear()
        if not run.has_work_for_scheduler(plan):
            log.info("Scheduled check: everything is backed up (%d files).", len(run.local.files))
            return EXIT_OK
        reason = (f"{len(plan.to_upload)} to upload, {len(plan.to_check)} to compare, "
                  f"{len(plan.folders_missing)} folders to create, {plan.deletions.file_count} deleted locally")
        log.info("Scheduled check: %s; opening the backup window.", reason)
        return _OPEN_WINDOW
    except SignInRequired as exc:
        log.info("Scheduled check: %s", exc)
        if notice.should_show("sign-in", str(exc), immediate=True):
            return _OPEN_WINDOW
        return EXIT_OK
    except RunStopped as exc:
        log.info("Scheduled check stopped: %s", exc)
        return EXIT_OK
    except requests.RequestException as exc:
        log.warning("Scheduled check could not reach Microsoft/OneDrive: %s", exc)
        return EXIT_OK
    except GraphError as exc:
        if exc.status in (408, 429) or exc.status >= 500:
            log.warning("Scheduled check: OneDrive is busy (%s); trying again later.", exc)
            return EXIT_OK
        kind, message = f"onedrive-{exc.status}", f"OneDrive error: {exc}"
    except (FatalRunError, OSError) as exc:
        kind, message = "fatal:" + type(exc).__name__ + ":" + str(exc)[:80], str(exc)
    except Exception as exc:              # anything unexpected: log it, and show it if it keeps happening
        log.exception("Scheduled check failed unexpectedly: %s", exc)
        kind, message = "unexpected:" + type(exc).__name__, f"Unexpected error: {exc}"
    log.warning("Scheduled check could not finish: %s", message)
    if notice.should_show(kind, message):
        return _OPEN_WINDOW
    return EXIT_OK


def _launch_if_no_window(base: Path, why: str):
    if window_is_open(base):
        return EXIT_OK
    log.warning("Scheduled check: opening the window to show a lasting problem (%s).", why)
    return _OPEN_WINDOW


def run_backup(cfg: Config, args, base: Path, console: Console, command: str, tokens_factory,
               ssid_fn: SsidFn) -> tuple:
    console.say("=" * 70)
    console.say(f"D-Drive OneDrive Backup   {time.strftime('%Y-%m-%d %H:%M')}")
    console.say(f"{cfg.source_folder}  ->  OneDrive/{cfg.onedrive_folder}" + ("   (DRY RUN)" if cfg.dry_run else ""))
    console.say("=" * 70)
    log.info("=" * 60)
    log.info("Backup run started (%s).", "opened by the 10-minute check" if args.window else "started by you")
    ssids, details = ssid_fn() if not (args.any_network or cfg.any_network) else ([], "")
    on_office, why = office_wifi_status(cfg, args.any_network, lambda: (ssids, details))
    if not on_office:
        console.say(f"Backup not started: {why}")
        console.say("(To run anyway, add --any-network.)")
        log.info("Backup not started: %s", why)
        return EXIT_NOT_OFFICE, ssids is None
    wifi_problem = wifi_problem_checker(cfg, args.any_network, ssid_fn)
    try:
        tokens = tokens_factory(cfg, base, True, console.say)
        tokens.get()
    except SignInRequired as exc:
        console.say(str(exc))
        log.error("%s", exc)
        return EXIT_FAILED, True
    except requests.RequestException as exc:
        console.say("Could not reach the Microsoft sign-in server (is the internet connection working?). "
                    "The backup will try again later.")
        log.warning("Could not reach the Microsoft sign-in server: %s", exc)
        return EXIT_FAILED, False
    tokens.interactive = False          # never wait for a sign-in in the middle of a run
    state = State(base / "backup_state.json")
    state.load()
    graph = GraphClient(tokens, Stopper(), wifi_problem)
    run = BackupRun(cfg, graph, state, console, wifi_problem, approve_deletions=args.approve_deletions,
                    command=command)
    result = run.run()
    for line in summary_lines(result, cfg):
        console.say(line)
        for part in line.splitlines():
            if part.strip():
                log.info("%s", part)
    titles = {"complete": "done", "dry_run": "dry run done", "finished_with_problems": "done, with problems",
              "stopped": "stopped", "failed": "failed"}
    set_console_title(f"D-Drive Backup - {titles[result.status]}")
    return _STATUS_EXIT[result.status], result.needs_attention


def manage_schedule(args, base: Path, script: Path, console: Console, command: str) -> int:
    try:
        if args.remove_schedule:
            schedule.remove()
            console.say(f"Removed the scheduled task \"{schedule.TASK_NAME}\".")
            return EXIT_OK
        cfg = load_config(base / "config.json")
        schedule.install(str(script), str(base), cfg.scan_interval_minutes, cfg.source_folder)
        console.say(f"Installed the scheduled task \"{schedule.TASK_NAME}\": every {cfg.scan_interval_minutes} "
                    f"minutes it checks for the office Wi-Fi and opens the backup window when something changed.")
        console.say(f"To remove it later:  {command} --remove-schedule")
        return EXIT_OK
    except (ConfigError, RuntimeError, OSError) as exc:
        console.say(f"Could not change the schedule: {exc}")
        log.error("Could not change the schedule: %s", exc)
        return EXIT_FAILED
