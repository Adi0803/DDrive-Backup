"""Unit tests that need no server."""

import base64
import json
import os
import random
import re
from pathlib import Path

import pytest

from ddrive_backup.config import ConfigError, load_config
from ddrive_backup.graph import RemoteFile, RemoteFolder, quote_path
from ddrive_backup.names import key_for, onedrive_name_problem
from ddrive_backup.progress import EtaEstimator
from ddrive_backup.quickxorhash import EMPTY_HASH, QuickXorHash, hash_bytes
from ddrive_backup.scan import LocalFile, LocalScan, scan_local
from ddrive_backup.state import State
from ddrive_backup.sync import make_plan, plan_deletions

VECTORS = Path(__file__).with_name("quickxorhash_vectors.json")


def test_quickxorhash_matches_rclone_vectors():
    vectors = json.loads(VECTORS.read_text())["vectors"]
    assert len(vectors) >= 70
    for case in vectors:
        data = base64.b64decode(case["in"])
        assert hash_bytes(data) == case["out"], case["size"]
        h = QuickXorHash()
        i = 0
        while i < len(data):           # random chunking must not matter
            n = random.randint(1, 333)
            h.update(data[i:i + n])
            i += n
        assert h.b64digest() == case["out"]


def test_quickxorhash_empty_and_large_chunking():
    assert hash_bytes(b"") == EMPTY_HASH
    blob = random.randbytes(3 * 1024 * 1024 + 17)
    one = hash_bytes(blob)
    h = QuickXorHash()
    for i in range(0, len(blob), 1_000_003):
        h.update(memoryview(blob)[i:i + 1_000_003])
    assert h.b64digest() == one


def test_names():
    assert key_for("Tata K Block/A070.EXE") == key_for("tata k block/a070.exe")
    assert onedrive_name_problem("~$report.docx")
    assert onedrive_name_problem("desktop.ini")
    assert onedrive_name_problem("x_vti_y")
    assert onedrive_name_problem("normal file #1 50%.txt") is None
    assert quote_path("a b/c#d/50%.txt") == "a%20b/c%23d/50%25.txt"


def _write_config(tmp_path, **over):
    data = {"client_id": "c", "tenant_id": "t", "source_folder": "D:\\OneDrive Backup",
            "onedrive_folder": "D-Drive-Backup", "office_wifi_ssid": "Horizon 5G",
            "scan_interval_minutes": 10, "delete_remote_files": False, "dry_run": False}
    data.update(over)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data))
    return p


def test_config_accepts_old_file_and_defaults(tmp_path):
    cfg = load_config(_write_config(tmp_path))
    assert cfg.office_wifi_ssids == ["Horizon 5G"]
    assert cfg.mirror_deletions == "auto" and cfg.parallel_uploads == 4
    assert cfg.mirror_safety_limit_percent == 10


def test_config_errors(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, office_wifi_ssid="YOUR_OFFICE_WIFI_NAME"))
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, mirror_deletions="sometimes"))
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path, parallel_uploads=0))
    cfg = load_config(_write_config(tmp_path, office_wifi_ssid=""))
    assert cfg.any_network


def test_scan_skips_links_and_bad_names(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.txt").write_bytes(b"12345")
    (tmp_path / "~$lock.docx").write_bytes(b"x")
    (tmp_path / "Empty").mkdir()
    os.symlink(tmp_path / "a", tmp_path / "link")
    scan = scan_local(str(tmp_path), len("D-Drive-Backup"))
    assert set(f.path for f in scan.files.values()) == {"a/x.txt"}
    assert scan.total_bytes == 5
    assert set(scan.folders.values()) == {"a", "Empty"}
    assert "~$lock.docx" in scan.not_allowed
    assert scan.links_skipped == ["link"]


def test_scan_reports_unreadable_folder(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read everything")


def _local(files, folders=(), unreadable=None):
    scan = LocalScan()
    for path, size in files.items():
        scan.files[key_for(path)] = LocalFile(path, size, 1_000_000_000, 1_000_000_000)
    for f in folders:
        scan.folders[key_for(f)] = f
    scan.unreadable = unreadable or {}
    return scan


def test_plan_classifies_files(tmp_path):
    state = State(tmp_path / "s.json")
    local = _local({"a.txt": 5, "b.txt": 6, "c.txt": 7, "Sub/d.txt": 0, "e.txt": 9}, ["Sub"])
    remote_files = {
        key_for("a.txt"): RemoteFile("1", "a.txt", 5, "HASHA"),      # same size, no record -> check
        key_for("B.TXT"): RemoteFile("2", "B.TXT", 99, "HASHB"),     # size differs -> upload
        key_for("sub/d.txt"): RemoteFile("4", "sub/d.txt", 0, EMPTY_HASH),  # empty -> up to date
        key_for("e.txt"): RemoteFile("5", "e.txt", 9, "HASHE"),
    }
    state.set_file(key_for("e.txt"), 9, 1_000_000_000, "HASHE", "5")   # recorded -> up to date
    remote_folders = {"": RemoteFolder("root", ""), key_for("SUB"): RemoteFolder("s", "SUB")}
    plan = make_plan(local, remote_folders, remote_files, state)
    assert [lf.path for lf, _ in plan.to_check] == ["a.txt"]
    assert sorted((lf.path, why) for lf, why in plan.to_upload) == [("b.txt", "changed"), ("c.txt", "new")]
    assert plan.up_to_date == 2
    assert plan.folders_missing == []
    assert not plan.deletions


def test_plan_record_mismatch_rechecks(tmp_path):
    state = State(tmp_path / "s.json")
    local = _local({"a.txt": 5})
    state.set_file(key_for("a.txt"), 5, 1_000_000_000, "OLD", "1")
    remote = {key_for("a.txt"): RemoteFile("1", "a.txt", 5, "NEW")}   # changed in OneDrive
    plan = make_plan(local, {"": RemoteFolder("r", "")}, remote, state)
    assert [lf.path for lf, _ in plan.to_check] == ["a.txt"]


def test_deletions_topmost_folders_and_protection():
    local = _local({"keep/a.txt": 1, "x.txt": 1}, ["keep", "locked"],
                   unreadable={"locked/": "folder could not be read"})
    remote_folders = {"": RemoteFolder("r", ""), "keep": RemoteFolder("k", "keep"),
                      "gone": RemoteFolder("g", "gone"), "gone/deeper": RemoteFolder("gd", "gone/deeper"),
                      "locked": RemoteFolder("l", "locked")}
    remote_files = {k: RemoteFile(k, k, 10, "h") for k in
                    ["keep/a.txt", "keep/old.txt", "gone/1.txt", "gone/deeper/2.txt", "locked/secret.txt", "x.txt"]}
    d = plan_deletions(local, remote_folders, remote_files)
    assert [f.path for f in d.folders] == ["gone"]
    assert [f.path for f in d.files] == ["keep/old.txt"]
    assert d.file_count == 3            # gone/1, gone/deeper/2, keep/old


def test_deletions_nothing_when_source_unreadable():
    local = _local({}, unreadable={"./": "folder could not be read"})
    d = plan_deletions(local, {"": RemoteFolder("r", "")}, {"a": RemoteFile("1", "a", 1, "h")})
    assert not d


def _simulate(files, workers=4, mb_per_s=8.0, per_file=0.4, seed=1):
    """Discrete simulation of parallel uploads; returns list of (t, done_bytes, done_files)."""
    rnd = random.Random(seed)
    queue = list(files)
    active = []          # [remaining_overhead, remaining_bytes]
    t, dt = 0.0, 0.5
    done_b = done_f = 0
    samples = []
    while queue or active:
        while queue and len(active) < workers:
            active.append([per_file * rnd.uniform(0.7, 1.3), queue.pop(0)])
        streaming = [a for a in active if a[0] <= 0]
        share = mb_per_s * 1e6 * dt / max(1, len(streaming))
        for a in active:
            if a[0] > 0:
                a[0] -= dt
            else:
                sent = min(a[1], share)
                a[1] -= sent
                done_b += sent
        finished = [a for a in active if a[0] <= 0 and a[1] <= 0]
        for a in finished:
            active.remove(a)
            done_f += 1
        t += dt
        samples.append((t, done_b, done_f))
    return samples


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_eta_is_hidden_until_stable_and_then_reasonable(seed):
    rnd = random.Random(seed)
    # project-like mix: many small files with a few large archives in between
    files = [rnd.choice([rnd.randint(1_000, 200_000)] * 9 + [rnd.randint(50_000_000, 400_000_000)])
             for _ in range(1500)]
    total_b, total_f = sum(files), len(files)
    samples = _simulate(files, seed=seed)
    end = samples[-1][0]
    est = EtaEstimator()
    errors = []
    shown_before_60s = False
    for t, b, f in samples:
        est.add(t, b, f)
        eta = est.estimate(t, total_b - b, total_f - f)
        if eta is not None:
            if t < 60:
                shown_before_60s = True
            actual = end - t
            if actual > 120:
                errors.append(abs(eta - actual) / actual)
    assert not shown_before_60s
    assert errors, "ETA was never shown"
    errors.sort()
    median = errors[len(errors) // 2]
    assert median < 0.25, f"median ETA error {median:.0%}"
