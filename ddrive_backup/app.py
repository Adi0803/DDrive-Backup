"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from . import schedule
from .auth import SignInRequired, TokenProvider
from .config import Config, ConfigError, load_config
from .graph import FatalRunError, GraphClient, GraphError, RunStopped, Stopper
from .progress import Console
from .state import State
from .sync import BackupRun, summary_lines
from .winutils import SingleInstanceLock, current_wifi_ssids, open_in_new_window, wait_or_keypress

log = logging.getLogger("ddrive_backup")

EXIT_OK, EXIT_PROBLEMS, EXIT_CONFIG, EXIT_NOT_OFFICE, EXIT_BUSY, EXIT_STOPPED, EXIT_FAILED = 0, 1, 2, 3, 4, 5, 6
_STATUS_EXIT = {"complete": EXIT_OK, "nothing_to_do": EXIT_OK, "finished_with_problems": EXIT_PROBLEMS,
                "stopped": EXIT_STOPPED, "failed": EXIT_FAILED}
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
    return TokenProvider(cfg.client_id, cfg.tenant_id, base / "auth_cache_dpapi.bin", show, interactive)


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


def main(argv=None, *, script_path: str, tokens_factory=default_tokens, ssid_fn: SsidFn = current_wifi_ssids,
         launch_window: Callable[[list[str], str], None] = open_in_new_window) -> int:
    args = parse_args(argv)
    script = Path(script_path).resolve()
    base = script.parent
    hidden = args.scheduled
    console = Console(enabled=not hidden)
    setup_logging(base / "backup.log", None if hidden else console)

    if args.install_schedule or args.remove_schedule:
        return manage_schedule(args, base, script, console)

    lock = SingleInstanceLock(base / "backup.lock")
    if not lock.acquire():
        if not hidden:
            console.say("Another backup is already running (maybe in another window). Try again later.")
        return EXIT_BUSY
    code = EXIT_FAILED
    cfg = None
    try:
        try:
            cfg = load_config(base / "config.json")
        except ConfigError as exc:
            log.error("%s", exc)
            if not hidden:
                console.say(str(exc))
            return EXIT_CONFIG
        if args.dry_run:
            cfg.dry_run = True
        remove_old_plain_cache(base)
        if hidden:
            code = scheduled_check(cfg, base, tokens_factory, ssid_fn)
        else:
            code = run_backup(cfg, args, base, console, tokens_factory, ssid_fn)
    except KeyboardInterrupt:
        console.say("Stopped.")
        code = EXIT_STOPPED
    except Exception as exc:   # last resort: never die without a trace in the log
        log.exception("Unexpected error: %s", exc)
        console.say(f"Unexpected error: {exc} (details in backup.log)")
        code = EXIT_FAILED
    finally:
        lock.release()

    if code == _OPEN_WINDOW:
        close_logging()
        launch_window([str(script), "--window"], str(base))
        return EXIT_OK
    if args.window:
        seconds = cfg.window_close_seconds if cfg is not None else 30
        if seconds:
            console.say(f"\nThis window closes in {seconds} seconds (press any key to close it now).")
            wait_or_keypress(seconds)
    return code


def scheduled_check(cfg: Config, base: Path, tokens_factory, ssid_fn: SsidFn):
    """The hidden 10-minute check. Returns _OPEN_WINDOW when there is work."""
    on_office, why = office_wifi_status(cfg, False, ssid_fn)
    if not on_office:
        log.info("Scheduled check: %s; skipped.", why)
        return EXIT_OK
    tokens = tokens_factory(cfg, base, False, lambda text: None)
    stopper = Stopper()
    graph = GraphClient(tokens, stopper, wifi_problem_checker(cfg, False, ssid_fn))
    state = State(base / "backup_state.json")
    state.load()
    run = BackupRun(cfg, graph, state, Console(False), wifi_problem_checker(cfg, False, ssid_fn))
    try:
        tokens.get()
        plan = run.prepare(show_progress=False)
        state.save()
        if not run.has_work_for_scheduler(plan):
            log.info("Scheduled check: everything is backed up (%d files).", len(run.local.files))
            return EXIT_OK
        reason = (f"{len(plan.to_upload)} to upload, {len(plan.to_check)} to compare, "
                  f"{len(plan.folders_missing)} folders to create, {plan.deletions.file_count} deleted locally")
    except SignInRequired as exc:
        reason = str(exc)
    except (RunStopped, FatalRunError, GraphError, OSError) as exc:
        log.warning("Scheduled check could not finish: %s", exc)
        return EXIT_OK
    except Exception as exc:
        if exc.__class__.__module__.startswith("requests"):
            log.warning("Scheduled check could not reach OneDrive: %s", exc)
            return EXIT_OK
        raise
    log.info("Scheduled check: %s; opening the backup window.", reason)
    return _OPEN_WINDOW


def run_backup(cfg: Config, args, base: Path, console: Console, tokens_factory, ssid_fn: SsidFn) -> int:
    console.say("=" * 70)
    console.say(f"D-Drive OneDrive Backup   {time.strftime('%Y-%m-%d %H:%M')}")
    console.say(f"{cfg.source_folder}  ->  OneDrive/{cfg.onedrive_folder}" + ("   (DRY RUN)" if cfg.dry_run else ""))
    console.say("=" * 70)
    log.info("=" * 60)
    log.info("Backup run started (%s).", "opened by the 10-minute check" if args.window else "started by you")
    on_office, why = office_wifi_status(cfg, args.any_network, ssid_fn)
    if not on_office:
        console.say(f"Backup not started: {why}")
        console.say("(To run anyway, add --any-network.)")
        log.info("Backup not started: %s", why)
        return EXIT_NOT_OFFICE
    wifi_problem = wifi_problem_checker(cfg, args.any_network, ssid_fn)
    tokens = tokens_factory(cfg, base, True, console.say)
    try:
        tokens.get()
    except SignInRequired as exc:
        console.say(str(exc))
        log.error("%s", exc)
        return EXIT_FAILED
    tokens.interactive = False          # never wait for a sign-in in the middle of a run
    state = State(base / "backup_state.json")
    state.load()
    graph = GraphClient(tokens, Stopper(), wifi_problem)
    run = BackupRun(cfg, graph, state, console, wifi_problem, approve_deletions=args.approve_deletions)
    result = run.run()
    for line in summary_lines(result, cfg):
        console.say(line)
        for part in line.splitlines():
            if part.strip():
                log.info("%s", part)
    return _STATUS_EXIT[result.status]


def manage_schedule(args, base: Path, script: Path, console: Console) -> int:
    try:
        if args.remove_schedule:
            schedule.remove()
            console.say(f"Removed the scheduled task \"{schedule.TASK_NAME}\".")
            return EXIT_OK
        cfg = load_config(base / "config.json")
        schedule.install(str(script), str(base), cfg.scan_interval_minutes, cfg.source_folder)
        console.say(f"Installed the scheduled task \"{schedule.TASK_NAME}\": every {cfg.scan_interval_minutes} "
                    f"minutes it checks for the office Wi-Fi and opens the backup window when something changed.")
        console.say("To remove it later:  python DDriveOneDriveBackup.py --remove-schedule")
        return EXIT_OK
    except (ConfigError, RuntimeError, OSError) as exc:
        console.say(f"Could not change the schedule: {exc}")
        log.error("Could not change the schedule: %s", exc)
        return EXIT_FAILED
