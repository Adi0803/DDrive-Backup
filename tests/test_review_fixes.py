"""Regression tests for the problems found in the code review of the rewrite."""

import json
import os
import random
import time

import pytest
import requests

import ddrive_backup.graph as graph_mod
import ddrive_backup.sync as sync_mod
from ddrive_backup import app
from ddrive_backup.auth import remove_stale_cache_lock
from ddrive_backup.scan import LocalFile
from tests.test_integration import OFFICE, ROOT, env, mock, sample_tree  # noqa: F401  (fixtures)


def pad(env, n=40):
    """Enough small files that a handful of deletions stays under the 10% limit."""
    for i in range(n):
        env.write(f"pad/{i}.txt", b"p%d" % i)


def age_problem_notice(env, seconds):
    path = env.base / "check_problem.json"
    data = json.loads(path.read_text())
    data["first"] -= seconds
    path.write_text(json.dumps(data))


# --- safety stop -------------------------------------------------------------

def test_safety_stop_also_counts_bytes(env):
    rnd = random.Random(5)
    pad(env)
    env.write("archives/big1.rar", rnd.randbytes(3_000_000))
    env.write("archives/big2.rar", rnd.randbytes(3_000_000))
    assert env.run() == app.EXIT_OK
    for p in (env.src / "archives").iterdir():
        p.unlink()
    (env.src / "archives").rmdir()
    assert env.run() == app.EXIT_PROBLEMS              # 2 files, but ~99% of the data
    assert "archives/big1.rar" in env.remote()
    assert env.run("--approve-deletions") == app.EXIT_OK
    assert env.remote() == env.local()


def test_safety_stop_message_gives_a_working_command(env, capsys):
    for i in range(10):
        env.write(f"f{i}.txt", b"x")
    assert env.run() == app.EXIT_OK
    for i in range(5):
        (env.src / f"f{i}.txt").unlink()
    capsys.readouterr()
    assert env.run() == app.EXIT_PROBLEMS
    out = capsys.readouterr().out
    assert f'cd /d "{env.base}"' in out and "DDriveOneDriveBackup.py --approve-deletions" in out


# --- first mirroring run is announced ------------------------------------------

def test_files_only_in_onedrive_are_announced_before_mirroring(env, capsys):
    pad(env)
    for i in range(3):
        env.mock.add_file(f"{ROOT}/old/renamed-{i}.txt", b"old")
    env.write("old/keep.txt", b"k")
    assert env.run("--dry-run") == app.EXIT_OK
    out = capsys.readouterr().out
    assert "In OneDrive but not in the local folder: 3 files" in out
    assert "Dry run finished - nothing was changed" in out
    assert env.run() == app.EXIT_OK
    out = capsys.readouterr().out
    assert "3 files" in out and "no longer in" in out and "recycle bin" in out
    assert env.run() == app.EXIT_OK                     # mirroring now on
    out = capsys.readouterr().out
    assert "Removing 3 files that you deleted locally" in out
    assert not any("renamed" in k for k in env.remote())


def test_dry_run_summary_is_consistent(env, capsys):
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    capsys.readouterr()
    assert env.run("--dry-run") == app.EXIT_OK
    out = capsys.readouterr().out
    assert "Already up to date:  8" in out
    assert "Uploaded:" not in out
    assert "Backup complete" not in out


# --- type conflicts never deadlock --------------------------------------------

def test_file_in_the_way_of_a_folder_is_replaced_on_the_first_run(env):
    env.mock.add_file(f"{ROOT}/Recipes", b"old single file")
    env.write("Recipes/a.txt", b"aaa")
    env.write("Recipes/b.txt", b"bbb")
    assert env.run() == app.EXIT_OK
    assert env.state()["first_complete_backup"]
    assert env.remote() == env.local()


# --- the hidden check: no endless pop-ups, no endless silence ---------------------

def test_failed_delete_does_not_reopen_the_window_every_check(env):
    pad(env)
    env.write("locked.docx", b"x")
    assert env.run() == app.EXIT_OK
    (env.src / "locked.docx").unlink()
    env.mock.fail_next("DELETE", r"/items/", 423, count=100)
    assert env.run("--window") == app.EXIT_PROBLEMS
    for _ in range(3):
        assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []


def test_unreadable_unchanged_file_stays_quiet_after_a_day(env, monkeypatch):
    sample_tree(env)
    env.write("Tata k block/A070/Old/Old.exe", b"MZ" + b"\0" * 100)
    real_open = open

    def fake_open(file, *args, **kwargs):
        if "Old.exe" in str(file):
            raise OSError(22, "Invalid argument")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(graph_mod, "open", fake_open, raising=False)
    assert env.run() == app.EXIT_PROBLEMS
    state_file = env.base / "backup_state.json"
    state = json.loads(state_file.read_text())
    for failure in state["failures"].values():
        failure["at"] -= 3 * 24 * 3600
    state_file.write_text(json.dumps(state))
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []


def test_lasting_problem_is_shown_once_it_persists(env):
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    (env.src).rename(env.src.with_name("renamed"))           # e.g. drive letter changed
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []                                # not after one check ...
    age_problem_notice(env, 31 * 60)
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1                            # ... but once it lasts
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1                            # and then at most once a day


def test_unknown_wifi_name_is_shown_once_it_persists(env):
    env.ssid = (None, "Network shell commands need location permission to access WLAN information.")
    assert env.run("--scheduled") == app.EXIT_OK
    age_problem_notice(env, 31 * 60)
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1
    shown = []
    app_wait = app.wait_or_keypress
    try:
        app.wait_or_keypress = lambda seconds: shown.append(seconds)
        assert env.run("--window") == app.EXIT_NOT_OFFICE
    finally:
        app.wait_or_keypress = app_wait
    assert shown == [None]                                 # window waits for a key press
    assert "Location" in env.log_text()


def test_dry_run_in_config_keeps_the_hidden_check_quiet(env):
    sample_tree(env)
    env.config(dry_run=True)
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []
    assert "dry_run" in env.log_text()


def test_no_second_window_while_one_is_open(env):
    from ddrive_backup.winutils import SingleInstanceLock
    sample_tree(env)
    lock = SingleInstanceLock(env.base / "window.lock")
    assert lock.acquire()
    try:
        assert env.run("--scheduled") == app.EXIT_OK
        assert env.windows == []
    finally:
        lock.release()
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1


def test_window_auto_closes_only_when_all_went_well(env, monkeypatch):
    waits = []
    monkeypatch.setattr(app, "wait_or_keypress", lambda seconds: waits.append(seconds))
    env.config(window_close_seconds=30)
    sample_tree(env)
    assert env.run("--window") == app.EXIT_OK
    assert waits == [30]


# --- moves, renames and odd names ---------------------------------------------------

def test_moved_file_keeps_old_copy_until_new_one_uploads(env, monkeypatch):
    pad(env)
    env.write("pad/filler.bin", b"f" * 100_000)            # keep the move under the 10% byte limit
    env.write("Line3/program.acd", b"PLC program" * 100)
    assert env.run() == app.EXIT_OK
    (env.src / "Line3").rename(env.src / "Line3 (2026)")
    real_open = open

    def locked(file, *args, **kwargs):
        if "Line3 (2026)" in str(file) and "program.acd" in str(file):
            raise PermissionError(13, "The process cannot access the file")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(graph_mod, "open", locked, raising=False)
    assert env.run() == app.EXIT_PROBLEMS
    assert "Line3/program.acd" in env.remote()             # old copy kept
    assert "Kept in OneDrive until" in env.log_text()
    monkeypatch.setattr(graph_mod, "open", real_open, raising=False)
    assert env.run() == app.EXIT_OK
    assert env.remote() == env.local()                     # moved for real now


def test_names_differing_only_in_case_are_reported(env):
    env.write("Straße.txt", b"one")
    env.write("STRASSE.txt", b"two")
    assert env.run() == app.EXIT_PROBLEMS
    assert len(env.remote()) == 1
    assert "upper/lower case" in env.log_text()


def test_links_are_reported(env, capsys):
    env.write("real/a.txt", b"a")
    os.symlink(env.src / "real", env.src / "LinkedProj")
    assert env.run() == app.EXIT_PROBLEMS
    assert "shortcut/link" in capsys.readouterr().out


# --- network, sign-in, OneDrive full, dates ----------------------------------------

class FlakyTokens:
    """Raises a connection error once when asked for a token, like MSAL does when
    the sign-in server cannot be reached at refresh time."""

    def __init__(self, inner, fail_on_call):
        self.inner, self.calls, self.fail_on_call = inner, 0, fail_on_call
        self.interactive = True

    def get(self, force_refresh=False):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise requests.ConnectionError("HTTPSConnectionPool(host='login.microsoftonline.com'): failed")
        return self.inner.get(force_refresh)


def test_network_error_while_renewing_sign_in_is_waited_out(env, monkeypatch):
    monkeypatch.setattr(graph_mod.GraphClient, "wait_for_network",
                        lambda self, exc, waited, attempt: waited + 1)
    sample_tree(env)
    env.tokens = FlakyTokens(env.tokens, fail_on_call=6)
    assert env.run() == app.EXIT_OK
    assert env.remote() == env.local()


def test_leaving_wifi_while_renewing_sign_in_is_a_clean_stop(env):
    sample_tree(env)
    env.tokens = FlakyTokens(env.tokens, fail_on_call=6)
    calls = {"n": 0}

    def ssid():
        calls["n"] += 1
        return OFFICE if calls["n"] <= 1 else (["Home WiFi"], "")

    code = app.main([], script_path=str(env.script), tokens_factory=lambda *a: env.tokens, ssid_fn=ssid,
                    launch_window=lambda a, c: None)
    assert code == app.EXIT_STOPPED
    assert "Left the office Wi-Fi" in env.log_text()


def test_onedrive_full_stops_the_run(env):
    env.config(parallel_uploads=1)
    env.mock.quota_total = 4 * 1024 * 1024
    for i in range(6):
        env.write(f"f{i}.bin", random.randbytes(3 * 1024 * 1024))
    assert env.run() == app.EXIT_FAILED
    assert "OneDrive is full" in env.log_text()
    sessions = [r for r in env.mock.requests_log if "createUploadSession" in r["path"]]
    assert len(sessions) <= 2


def test_dates_before_1970_do_not_break_uploads(env):
    times = sync_mod.file_times(LocalFile("x", 1, -19_800 * 10**9, -11_644_473_600 * 10**9))
    assert times == {"createdDateTime": "1601-01-01T00:00:00Z", "lastModifiedDateTime": "1969-12-31T18:30:00Z"}
    p = env.write("old.txt", b"old")
    os.utime(p, (-19_800, -19_800))
    assert env.run() == app.EXIT_OK
    assert "old.txt" in env.remote()


def test_missing_checksum_after_upload_is_logged(env, monkeypatch):
    real_item_json = type(env.mock)._item_json

    def without_hash(self, it, delta=False):
        d = real_item_json(self, it, delta)
        if "file" in d:
            d["file"] = {"mimeType": "application/octet-stream"}
        return d

    monkeypatch.setattr(type(env.mock), "_item_json", without_hash)
    env.write("a.bin", b"x" * 1000)
    assert env.run() == app.EXIT_OK
    assert "no checksum" in env.log_text()


# --- sign-in cache housekeeping ---------------------------------------------------

def test_stale_sign_in_lock_file_is_removed(env, tmp_path):
    cache = tmp_path / "auth_cache_dpapi.bin"
    lockfile = tmp_path / "auth_cache_dpapi.bin.lockfile"
    lockfile.write_text("left behind by a killed run")
    remove_stale_cache_lock(cache)
    assert not lockfile.exists()
    (env.base / "auth_cache_dpapi.bin.lockfile").write_text("stale")
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    assert not (env.base / "auth_cache_dpapi.bin.lockfile").exists()


def test_no_stale_progress_block_after_a_stop(env, monkeypatch, capsys):
    import ddrive_backup.progress as progress_mod
    monkeypatch.setattr(progress_mod, "enable_ansi_console", lambda: True)
    monkeypatch.setattr(app, "wait_or_keypress", lambda seconds: None)
    rnd = random.Random(2)
    for i in range(10):
        data = rnd.randbytes(1000)
        env.write(f"f{i}.bin", data)
        env.mock.add_file(f"{ROOT}/f{i}.bin", data)
    real = sync_mod.BackupRun._hash_local
    calls = {"n": 0}

    def hash_local(self, lf, on_bytes):
        calls["n"] += 1
        if calls["n"] == 4:
            self.stopper.stop("Left the office Wi-Fi (now: no Wi-Fi).")
        return real(self, lf, on_bytes)

    monkeypatch.setattr(sync_mod.BackupRun, "_hash_local", hash_local)
    capsys.readouterr()
    assert env.run("--window") == app.EXIT_STOPPED
    out = capsys.readouterr().out
    assert "comparing local files" not in out[out.index("Backup stopped"):]


# --- 4-hour pause after a backup, quiet local check, throttling ----------------------------

def _graph_requests(env):
    return [r for r in env.mock.requests_log if not r["path"].startswith("/upload/")]


def test_cooldown_after_a_backup(env):
    env.config(cooldown_hours=4)
    sample_tree(env)
    assert env.run() == app.EXIT_OK                        # manual backup starts the pause
    env.write("Projects/new.txt", b"new")
    mark = len(env.mock.requests_log)
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []                                # paused: nothing happens at all
    assert len(env.mock.requests_log) == mark               # not even a OneDrive request
    assert "next check after" in env.log_text()
    state_file = env.base / "backup_state.json"             # pretend the backup was 5 hours ago
    state = json.loads(state_file.read_text())
    state["last_backup_finished"] -= 5 * 3600
    state_file.write_text(json.dumps(state))
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1


def test_cooldown_ignores_a_finish_time_in_the_future(env):
    env.config(cooldown_hours=4)
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    state_file = env.base / "backup_state.json"
    state = json.loads(state_file.read_text())
    state["last_backup_finished"] += 3 * 24 * 3600          # clock was set back since then
    state_file.write_text(json.dumps(state))
    env.write("Projects/new.txt", b"new")
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1


def test_back_up_now_ignores_the_pause(env):
    env.config(cooldown_hours=4)
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    env.write("Projects/new.txt", b"new")
    assert env.run() == app.EXIT_OK                         # what "Back up now.bat" runs
    assert "Projects/new.txt" in env.remote()


def test_background_check_needs_no_onedrive_requests_when_nothing_changed(env):
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    mark = len(env.mock.requests_log)
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == [] and len(env.mock.requests_log) == mark
    env.write("Projects/new.txt", b"new")                    # a change on D: is noticed locally
    (env.src / "space name.txt").unlink()
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1 and len(env.mock.requests_log) == mark


def test_background_check_looks_at_onedrive_once_a_day(env):
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    env.mock.add_file(f"{ROOT}/added-in-onedrive.txt", b"x")    # changed directly in OneDrive
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []                                 # not noticed by the quick check ...
    state_file = env.base / "backup_state.json"
    state = json.loads(state_file.read_text())
    state["last_remote_check"] -= 25 * 3600
    state_file.write_text(json.dumps(state))
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1                             # ... but by the daily OneDrive check


def test_daily_check_in_sync_records_baseline_without_window(env):
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    state_file = env.base / "backup_state.json"
    state = json.loads(state_file.read_text())
    state["last_remote_check"] -= 25 * 3600
    state["folders"] = None                                  # e.g. state from the previous version
    state_file.write_text(json.dumps(state))
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []
    state = json.loads(state_file.read_text())
    assert state["folders"] and state["last_remote_check"] > time.time() - 60


def test_changed_settings_force_a_onedrive_check(env):
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    env.config(onedrive_folder="Other-Backup")
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1


def test_throttling_is_waited_out_quietly(env, capsys):
    sample_tree(env)
    env.mock.fail_next("GET", r"/children", 429, count=30, headers={"Retry-After": "1"})
    assert env.run() == app.EXIT_OK
    out = capsys.readouterr().out
    assert "HTTP 429" not in out
    assert "slow down" in env.log_text()
    assert env.remote() == env.local()
