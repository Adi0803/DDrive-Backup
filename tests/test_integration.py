"""End-to-end tests of the backup against the mock OneDrive server."""

import builtins
import json
import os
import random
import threading
import time
from pathlib import Path

import pytest

import ddrive_backup.graph as graph_mod
import ddrive_backup.sync as sync_mod
from ddrive_backup import app
from ddrive_backup.auth import SignInRequired
from ddrive_backup.quickxorhash import hash_bytes
from tests.mock_graph import MockGraph

ROOT = "D-Drive-Backup"
OFFICE = (["Horizon 5G"], "")


class FakeTokens:
    def __init__(self, mock, fail=False):
        self.mock, self.fail = mock, fail
        self.interactive = True
        self.generation = 0
        self.refreshes = 0

    def get(self, force_refresh=False):
        if self.fail:
            raise SignInRequired("Microsoft sign-in is needed (test).")
        if force_refresh:
            self.refreshes += 1
            self.generation += 1
            self.mock.set_valid_tokens({f"test-token-{self.generation}"})
        return "test-token" if self.generation == 0 else f"test-token-{self.generation}"


@pytest.fixture
def mock(monkeypatch):
    server = MockGraph(page_size=3)
    server.start()
    monkeypatch.setattr(graph_mod, "GRAPH_ROOT", server.base_url)
    yield server
    server.stop()


class Env:
    def __init__(self, tmp_path, mock):
        self.base = tmp_path / "app"
        self.base.mkdir()
        self.src = tmp_path / "src"
        self.src.mkdir()
        self.mock = mock
        self.script = self.base / "DDriveOneDriveBackup.py"
        self.script.write_text("# test\n")
        self.ssid = OFFICE
        self.tokens = FakeTokens(mock)
        self.windows = []
        self.config(office_wifi_ssid="Horizon 5G")

    def config(self, **over):
        data = {"client_id": "c", "tenant_id": "t", "source_folder": str(self.src), "onedrive_folder": ROOT,
                "office_wifi_ssid": "Horizon 5G", "scan_interval_minutes": 10, "parallel_uploads": 3,
                "window_close_seconds": 0, "cooldown_hours": 0}
        data.update(over)
        (self.base / "config.json").write_text(json.dumps(data))

    def write(self, rel, data, mtime=None):
        p = self.src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p

    def run(self, *args):
        return app.main(list(args), script_path=str(self.script),
                        tokens_factory=lambda cfg, base, interactive, show: self.tokens,
                        ssid_fn=lambda: self.ssid,
                        launch_window=lambda argv, cwd: self.windows.append(argv))

    def remote(self):
        return {k[len(ROOT) + 1:]: v for k, v in self.mock.tree().items() if k.startswith(ROOT + "/")}

    def local(self):
        out = {}
        for p in self.src.rglob("*"):
            if p.is_file():
                out[p.relative_to(self.src).as_posix()] = p.read_bytes()
        return out

    def uploads(self, since=0):
        """Requests that send file content (simple PUT or session creation)."""
        return [r for r in self.mock.requests_log[since:]
                if (r["method"] == "POST" and "createUploadSession" in r["path"])
                or (r["method"] == "PUT" and r["path"].endswith(":/content"))]

    def state(self):
        return json.loads((self.base / "backup_state.json").read_text())

    def log_text(self):
        return (self.base / "backup.log").read_text(encoding="utf-8")


@pytest.fixture
def env(tmp_path, mock):
    return Env(tmp_path, mock)


def sample_tree(env):
    rnd = random.Random(7)
    env.write("empty.txt", b"")
    env.write("a#b.txt", b"hash sign")
    env.write("50%20off.pdf", b"percent sign")
    env.write("naïve café.txt", "unicode".encode())
    env.write("space name.txt", b"spaces")
    env.write("Projects/PLC/Machine1.acd", rnd.randbytes(70_000))
    env.write("Projects/HMI/Screen.mer", rnd.randbytes(5_000))
    env.write("Projects/big.rar", rnd.randbytes(3 * graph_mod.FRAGMENT_SIZE + 12345))   # 3+ fragments
    (env.src / "Empty Folder" / "Inner").mkdir(parents=True)


def test_first_backup_then_nothing_to_do(env):
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    assert env.remote() == env.local()
    assert {ROOT + "/Empty Folder", ROOT + "/Empty Folder/Inner"} <= env.mock.folders()
    st = env.state()
    assert st["first_complete_backup"]
    assert len(st["files"]) == 8
    # remote checksums equal local ones
    for rel, data in env.local().items():
        item = env.mock.get_item_by_path(f"{ROOT}/{rel}")
        assert item["file"]["hashes"]["quickXorHash"] == hash_bytes(data)
    # original modified time preserved in fileSystemInfo for session uploads
    item = env.mock.get_item_by_path(f"{ROOT}/Projects/big.rar")
    assert item["fileSystemInfo"]["lastModifiedDateTime"].startswith(
        time.strftime("%Y-%m-%dT%H:%M", time.gmtime((env.src / "Projects/big.rar").stat().st_mtime)))

    mark = len(env.mock.requests_log)
    assert env.run() == app.EXIT_OK
    assert env.uploads(mark) == []
    assert "Uploaded:            0 files" in env.log_text()


def test_existing_backup_is_adopted_by_checksum(env):
    """Files already in OneDrive (uploaded by the old script) are compared by
    checksum, not uploaded again; empty or different copies are re-uploaded."""
    rnd = random.Random(1)
    same = {f"dir{i}/f{i}.bin": rnd.randbytes(1000 + i) for i in range(20)}
    for rel, data in same.items():
        env.write(rel, data)
        env.mock.add_file(f"{ROOT}/{rel}", data)               # identical copy, upload-time dates
    env.write("damaged.bin", b"x" * 500)
    env.mock.add_file(f"{ROOT}/damaged.bin", b"")               # 0-byte copy from the old retry bug
    env.write("different.bin", b"A" * 300)
    env.mock.add_file(f"{ROOT}/different.bin", b"B" * 300)      # same size, other content
    env.mock.add_file(f"{ROOT}/only-in-onedrive.txt", b"old")   # deleted locally long ago

    mark = len(env.mock.requests_log)
    assert env.run() == app.EXIT_OK
    uploaded = {r["path"] for r in env.uploads(mark)}
    assert len(uploaded) == 2 and all("damaged" in p or "different" in p for p in uploaded)
    remote = env.remote()
    assert remote["damaged.bin"] == b"x" * 500 and remote["different.bin"] == b"A" * 300
    assert "only-in-onedrive.txt" in remote               # mirroring not on during the first run

    assert env.run() == app.EXIT_OK                       # second run: mirroring is now on
    assert "only-in-onedrive.txt" not in env.remote()
    assert any(i.get("name") == "only-in-onedrive.txt" for i in env.mock.recycle_bin)


def test_changes_are_mirrored(env):
    sample_tree(env)
    for i in range(30):                                    # keep deletions under the 10% safety limit
        env.write(f"pad/{i}.txt", b"p")
    assert env.run() == app.EXIT_OK
    p = env.src / "Projects/PLC/Machine1.acd"
    p.write_bytes(random.randbytes(70_000))                # same size, new content
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    env.write("Projects/new.txt", b"new file")
    (env.src / "space name.txt").unlink()
    (env.src / "Empty Folder" / "Inner").rmdir()
    assert env.run() == app.EXIT_OK
    assert env.remote() == env.local()
    assert ROOT + "/Empty Folder/Inner" not in env.mock.folders()


def test_safety_limit_blocks_mass_deletion_until_approved(env):
    for i in range(20):
        env.write(f"f{i}.txt", b"data %d" % i)
    assert env.run() == app.EXIT_OK
    for i in range(5):                                    # 25% of the files
        (env.src / f"f{i}.txt").unlink()
    assert env.run() == app.EXIT_PROBLEMS
    assert len(env.remote()) == 20
    assert "NOT deleting" in env.log_text() or "safety" in env.log_text()
    assert env.run("--approve-deletions") == app.EXIT_OK
    assert env.remote() == env.local()


def test_server_errors_and_lost_replies_are_survived(env, monkeypatch):
    monkeypatch.setattr(graph_mod, "FRAGMENT_TIMEOUT", (5, 2))
    monkeypatch.setattr(graph_mod, "TIMEOUT", (5, 2))
    monkeypatch.setattr(graph_mod, "_backoff", lambda attempt: 0.05)
    sample_tree(env)
    m = env.mock
    m.fail_next("GET", r"/children", 429, count=2, headers={"Retry-After": "1"})
    m.fail_next("POST", r"createUploadSession", 503, count=2, headers={"Retry-After": "1"})
    m.fail_next("PUT", r"^/upload/", 500, count=2)
    m.delay_next("PUT", r"^/upload/", 3, count=2)          # reply lost after the data was stored
    m.drop_next("PUT", r"^/upload/", count=1)
    assert env.run() == app.EXIT_OK
    assert env.remote() == env.local()


def test_unreadable_file_is_skipped_and_reported(env, monkeypatch):
    sample_tree(env)
    env.write("Tata k block/A070/Old/Old.exe", b"MZ" + b"\0" * 100)
    real_open = builtins.open

    def fake_open(file, *args, **kwargs):
        if "Old.exe" in str(file):
            raise OSError(22, "Invalid argument")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(graph_mod, "open", fake_open, raising=False)
    monkeypatch.setattr(sync_mod, "open", fake_open, raising=False)
    assert env.run() == app.EXIT_PROBLEMS
    remote = env.remote()
    assert "Tata k block/A070/Old/Old.exe" not in remote
    assert len(remote) == 8                                 # everything else made it
    assert "Old.exe" in env.log_text()
    assert env.state()["first_complete_backup"]            # unreadable files do not block mirroring
    # The hidden check does not open a window again just for that file.
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []


def test_leaving_wifi_stops_and_next_run_resumes(env, monkeypatch):
    monkeypatch.setattr(sync_mod, "WIFI_RECHECK_SECONDS", 0.2)
    rnd = random.Random(3)
    env.write("huge.bin", rnd.randbytes(6 * graph_mod.FRAGMENT_SIZE + 99))   # large: session remembered
    for i in range(10):
        env.write(f"s{i}.txt", b"x" * 1000)
    env.config(parallel_uploads=1)
    calls = {"n": 0}

    def ssid():
        calls["n"] += 1
        return OFFICE if calls["n"] <= 3 else (["Home WiFi"], "")

    env.ssid = None
    env.mock.delay_next("PUT", r"^/upload/", 0.4, count=100)
    code = app.main([], script_path=str(env.script), tokens_factory=lambda *a: env.tokens, ssid_fn=ssid,
                    launch_window=lambda a, c: None)
    assert code == app.EXIT_STOPPED
    assert "Left the office Wi-Fi" in env.log_text()
    env.mock.clear_faults()
    assert env.state()["sessions"], "unfinished large upload should be remembered"
    sessions_before = sum(1 for r in env.mock.requests_log if "createUploadSession" in r["path"] and "huge" in r["path"])
    env.ssid = OFFICE
    assert env.run() == app.EXIT_OK
    assert env.remote() == env.local()
    sessions_after = sum(1 for r in env.mock.requests_log if "createUploadSession" in r["path"] and "huge" in r["path"])
    assert sessions_after == sessions_before, "the large upload should resume, not restart"
    assert "Resuming earlier upload" in env.log_text()


def test_expired_token_is_refreshed_mid_run(env):
    sample_tree(env)
    env.mock.expire_tokens()
    assert env.run() == app.EXIT_OK
    assert env.tokens.refreshes >= 1
    assert env.remote() == env.local()


def test_not_on_office_wifi(env):
    sample_tree(env)
    env.ssid = (["Home WiFi"], "")
    assert env.run() == app.EXIT_NOT_OFFICE
    assert env.remote() == {}
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []
    env.ssid = (None, "Network shell commands need location permission to access WLAN information.")
    assert env.run() == app.EXIT_NOT_OFFICE
    assert "Location" in env.log_text() or "location" in env.log_text()


def test_scheduled_check_opens_window_only_when_needed(env):
    sample_tree(env)
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == [[str(env.script.resolve()), "--window"]]
    assert env.remote() == {}                              # the hidden check never uploads
    env.windows.clear()
    assert env.run("--window") == app.EXIT_OK              # what the window does
    assert env.run("--scheduled") == app.EXIT_OK
    assert env.windows == []                               # nothing changed: stays hidden
    env.write("Projects/another.txt", b"more")
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1
    env.windows.clear()
    env.tokens.fail = True                                 # sign-in needed -> window
    assert env.run("--scheduled") == app.EXIT_OK
    assert len(env.windows) == 1


def test_dry_run_changes_nothing(env):
    sample_tree(env)
    assert env.run("--dry-run") == app.EXIT_OK
    assert env.remote() == {}
    assert ROOT not in env.mock.folders()
    assert not env.uploads()


def test_case_and_type_conflicts(env):
    env.mock.add_folder(f"{ROOT}/Sub")
    env.mock.add_file(f"{ROOT}/Sub/a.txt", b"aaa")
    env.write("SUB/a.txt", b"aaa")                         # same folder, different case
    env.write("SUB/b.txt", b"bbb")
    for i in range(30):                                    # keep later deletions under the safety limit
        env.write(f"pad{i}.txt", b"p")
    assert env.run() == app.EXIT_OK
    assert {"Sub/a.txt", "Sub/b.txt"} <= set(env.remote())
    assert not any(k.lower().startswith("sub/") and not k.startswith("Sub/") for k in env.remote())
    # a OneDrive folder where there is now a local file of the same name
    env.mock.add_folder(f"{ROOT}/thing")
    env.mock.add_file(f"{ROOT}/thing/inside.txt", b"x")
    env.write("thing", b"now a file")
    assert env.run() == app.EXIT_OK                        # mirroring active after first complete run
    assert env.remote()["thing"] == b"now a file"
    assert "thing/inside.txt" not in env.remote()


def test_second_instance_is_refused(env):
    from ddrive_backup.winutils import SingleInstanceLock
    lock = SingleInstanceLock(env.base / "backup.lock")
    assert lock.acquire()
    try:
        assert env.run() == app.EXIT_BUSY
    finally:
        lock.release()


def test_old_plaintext_cache_is_removed(env):
    (env.base / "auth_cache.bin").write_bytes(b"secret")
    sample_tree(env)
    assert env.run() == app.EXIT_OK
    assert not (env.base / "auth_cache.bin").exists()
