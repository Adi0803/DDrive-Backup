"""Microsoft Graph (OneDrive) client: retries, listing, folders, uploads, deletes."""

from __future__ import annotations

import email.utils
import logging
import os
import random
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter

from .names import key_for
from .quickxorhash import EMPTY_HASH, QuickXorHash
from .winutils import describe_os_error, long_path

log = logging.getLogger(__name__)

GRAPH_ROOT = os.environ.get("DDRIVE_GRAPH_URL", "https://graph.microsoft.com/v1.0").rstrip("/")

FRAGMENT_SIZE = 32 * 320 * 1024          # 10 MiB; fragments must be multiples of 320 KiB
LARGE_FILE_SIZE = 4 * FRAGMENT_SIZE      # 40 MiB: 'large' for scheduling; their upload sessions are remembered
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
MAX_HTTP_ATTEMPTS = 8
MAX_THROTTLE_SECONDS = 1800              # wait at most this long in total when OneDrive asks us to slow down
NETWORK_PATIENCE_SECONDS = 600           # keep retrying a dead connection this long
TIMEOUT = (30, 120)                      # (connect, read) seconds for normal calls
FRAGMENT_TIMEOUT = (30, 300)
LIST_SELECT = "id,name,size,file,folder,package,fileSystemInfo,eTag,cTag"


# ----------------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------------

class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status, self.code, self.message = status, code, message

    @classmethod
    def from_response(cls, resp: requests.Response) -> "GraphError":
        code, message = "", resp.text[:300]
        try:
            err = resp.json().get("error", {})
            code, message = err.get("code", ""), err.get("message", message)
        except ValueError:
            pass
        return cls(resp.status_code, code, message)


class RunStopped(Exception):
    """The run is being stopped on purpose (left office Wi-Fi, Ctrl+C, ...)."""


class FatalRunError(Exception):
    """Something that makes continuing pointless (OneDrive full, no network, ...)."""


class FileFailed(Exception):
    """This one file could not be backed up; carry on with the others."""


class LocalReadError(FileFailed):
    """The local file could not be read (locked, blocked by antivirus, ...)."""


class _SessionGone(Exception):
    pass


def error_for(resp: requests.Response) -> Exception:
    """The exception for a failed OneDrive answer. A full OneDrive stops the whole
    run (every other file would fail the same way)."""
    error = GraphError.from_response(resp)
    if resp.status_code == 507 or error.code in ("quotaLimitReached", "insufficientStorage"):
        return FatalRunError("Your OneDrive is full (no storage left). " + error.message)
    return error


class Stopper:
    """Shared stop signal for all threads."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.reason = ""

    def stop(self, reason: str) -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    @property
    def stopped(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise RunStopped(self.reason)

    def sleep(self, seconds: float) -> None:
        if self._event.wait(max(0.0, seconds)):
            raise RunStopped(self.reason)


def quote_path(path: str) -> str:
    return "/".join(quote(part, safe="") for part in path.split("/"))


def _retry_after_seconds(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(600.0, max(0.0, float(value)))
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value).timestamp()
            return min(600.0, max(0.0, when - time.time()))
        except (TypeError, ValueError):
            return None


def _backoff(attempt: int) -> float:
    return min(120.0, 2.0 ** attempt) * (0.75 + random.random() / 2)


@dataclass
class RemoteFile:
    id: str
    path: str
    size: int
    hash: str | None


@dataclass
class RemoteFolder:
    id: str
    path: str


# ----------------------------------------------------------------------------
# Client
# ----------------------------------------------------------------------------

class GraphClient:
    def __init__(self, tokens, stopper: Stopper, network_problem: Callable[[], str | None]):
        """network_problem() returns a reason to stop (e.g. "left office Wi-Fi") or None."""
        self.tokens = tokens
        self.stopper = stopper
        self.network_problem = network_problem
        self.drive_id = ""
        self._local = threading.local()
        self._pause_until = 0.0              # OneDrive asked all requests to wait until then
        self._pause_lock = threading.Lock()

    def throttled(self) -> bool:
        return time.monotonic() < self._pause_until

    def _pause_all(self, seconds: float) -> None:
        with self._pause_lock:
            self._pause_until = max(self._pause_until, time.monotonic() + seconds)

    # --- plumbing -----------------------------------------------------------

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    def request(self, method: str, url: str, *, json_body=None, data: bytes | None = None,
                headers: dict | None = None, auth: bool = True, ok=(200, 201, 202, 204),
                timeout=TIMEOUT) -> requests.Response:
        """Send one request, retrying throttling, server errors and network drops.
        `data` must be bytes so a retry can send exactly the same body again."""
        if not url.startswith(("http://", "https://")):
            url = GRAPH_ROOT + url
        attempt, network_waited, refreshed, force_refresh = 0, 0.0, False, False
        throttled_for = 0.0
        while True:
            self.stopper.check()
            wait_for = self._pause_until - time.monotonic()
            if wait_for > 0:                     # another request was told to slow down
                self.stopper.sleep(wait_for)
            attempt += 1
            hdrs = dict(headers or {})
            try:
                # Getting a token can need the network too (sign-in server), so it
                # goes through the same connection-problem handling.
                if auth:
                    hdrs["Authorization"] = "Bearer " + self.tokens.get(force_refresh=force_refresh)
                    force_refresh = False
                resp = self._session().request(method, url, json=json_body, data=data,
                                               headers=hdrs, timeout=timeout)
            except requests.RequestException as exc:
                network_waited = self.wait_for_network(exc, network_waited, attempt)
                continue
            network_waited = 0.0
            if resp.status_code == 401 and auth and not refreshed:
                refreshed = force_refresh = True
                continue
            if resp.status_code in RETRYABLE_STATUS:
                retry_after = _retry_after_seconds(resp)
                if (resp.status_code == 429 or retry_after) and throttled_for < MAX_THROTTLE_SECONDS:
                    # Normal throttling: OneDrive limits how many requests an app may
                    # make per minute. Everyone waits as asked, then carries on; this
                    # does not count as a failed attempt.
                    delay = retry_after or _backoff(min(attempt, 5))
                    throttled_for += delay
                    attempt -= 1
                    self._pause_all(delay)
                    log.info("OneDrive asked us to slow down (HTTP %s); waiting %.0f s.", resp.status_code, delay)
                    self.stopper.sleep(delay)
                    continue
                if attempt < MAX_HTTP_ATTEMPTS:
                    delay = retry_after or _backoff(attempt)
                    log.warning("OneDrive answered HTTP %s for %s %s; retrying in %.0f s.",
                                resp.status_code, method, _short(url), delay)
                    self.stopper.sleep(delay)
                    continue
            if resp.status_code in ok:
                return resp
            raise error_for(resp)

    def wait_for_network(self, exc: Exception, waited: float, attempt: int) -> float:
        """Called after a connection problem. Stops the run if we left the office
        network, gives up after NETWORK_PATIENCE_SECONDS, otherwise waits."""
        self.stopper.check()
        reason = self.network_problem()
        if reason:
            self.stopper.stop(reason)
            raise RunStopped(reason)
        if waited >= NETWORK_PATIENCE_SECONDS:
            raise FatalRunError(f"No working connection to OneDrive for {waited / 60:.0f} minutes ({_short_exc(exc)}).")
        delay = min(60.0, 2.0 ** min(attempt, 6))
        log.warning("Connection problem (%s); retrying in %.0f s.", _short_exc(exc), delay)
        self.stopper.sleep(delay)
        return waited + delay

    # --- drive and folders ----------------------------------------------------

    def get_drive(self) -> dict:
        drive = self.request("GET", "/me/drive?$select=id,driveType,quota").json()
        self.drive_id = drive["id"]
        return drive

    def get_item_by_path(self, path: str) -> dict | None:
        """`path` is relative to the OneDrive root, '/' separated."""
        resp = self.request("GET", f"/drives/{self.drive_id}/root:/{quote_path(path)}", ok=(200, 404))
        return resp.json() if resp.status_code == 200 else None

    def get_child(self, parent_id: str, name: str) -> dict | None:
        resp = self.request("GET", f"/drives/{self.drive_id}/items/{parent_id}:/{quote(name, safe='')}",
                            ok=(200, 404))
        return resp.json() if resp.status_code == 200 else None

    def create_folder(self, parent_id: str, name: str) -> dict:
        body = {"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"}
        try:
            return self.request("POST", f"/drives/{self.drive_id}/items/{parent_id}/children",
                                json_body=body, ok=(200, 201)).json()
        except GraphError as exc:
            if exc.status != 409:
                raise
        # Already exists (maybe with different upper/lower case): use it if it is a folder.
        existing = self.get_child(parent_id, name)
        if existing and "folder" in existing:
            return existing
        raise FileFailed(f"OneDrive already has a file named \"{name}\" where a folder is needed.")

    def ensure_folder_path(self, path: str, create: bool = True) -> dict | None:
        """Find (or create) a folder given its path from the OneDrive root."""
        item = self.get_item_by_path(path)
        if item is not None:
            if "folder" not in item:
                raise FatalRunError(f"\"{path}\" in OneDrive is a file, not a folder.")
            return item
        if not create:
            return None
        parent = self.request("GET", f"/drives/{self.drive_id}/root").json()
        for part in path.split("/"):
            child = self.get_child(parent["id"], part)
            if child is None:
                log.info("Creating OneDrive folder %s", part)
                child = self.create_folder(parent["id"], part)
            elif "folder" not in child:
                raise FatalRunError(f"\"{part}\" in OneDrive is a file, not a folder.")
            parent = child
        return parent

    def list_children(self, item_id: str) -> list[dict]:
        url = f"/drives/{self.drive_id}/items/{item_id}/children?$select={LIST_SELECT}"
        items: list[dict] = []
        while url:
            data = self.request("GET", url).json()
            items.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return items

    def list_tree(self, root_id: str, progress: Callable[[int, int], None] | None = None,
                  workers: int = 4) -> tuple[dict[str, RemoteFolder], dict[str, RemoteFile]]:
        """Everything below the backup folder, keyed by normalised relative path."""
        folders: dict[str, RemoteFolder] = {"": RemoteFolder(root_id, "")}
        files: dict[str, RemoteFile] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="list") as pool:
            pending = {pool.submit(self.list_children, root_id): ""}
            try:
                while pending:
                    done, _ = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:
                        parent = pending.pop(future)
                        for item in future.result():
                            path = f"{parent}/{item['name']}" if parent else item["name"]
                            if "folder" in item:
                                folders[key_for(path)] = RemoteFolder(item["id"], path)
                                pending[pool.submit(self.list_children, item["id"])] = path
                            else:
                                hashes = (item.get("file") or {}).get("hashes") or {}
                                files[key_for(path)] = RemoteFile(item["id"], path, int(item.get("size") or 0),
                                                                  hashes.get("quickXorHash"))
                        if progress:
                            progress(len(files), len(folders) - 1)
            except BaseException:
                self.stopper.stop(self.stopper.reason or "listing OneDrive failed")
                raise
        return folders, files

    def delete_item(self, item_id: str) -> None:
        """Moves the item to the OneDrive recycle bin."""
        self.request("DELETE", f"/drives/{self.drive_id}/items/{item_id}", ok=(204, 200, 404))

    # --- uploads ----------------------------------------------------------------

    def upload_file(self, parent_id: str, name: str, path: str, size: int, file_times: dict | None,
                    progress: Callable[[int], None], saved_session: str | None = None,
                    remember_session: Callable[[str | None], None] | None = None) -> tuple[dict, str]:
        """Upload one local file into folder `parent_id` under `name`, replacing any
        existing file. Returns (driveItem, local QuickXorHash). The hash OneDrive
        reports is checked against the bytes we read, so a damaged upload fails."""
        try:
            handle = open(long_path(path), "rb")
        except OSError as exc:
            raise LocalReadError(describe_os_error(path, exc)) from exc
        with handle:
            if size == 0:
                item = self.request("PUT", f"/drives/{self.drive_id}/items/{parent_id}:/{quote(name, safe='')}:/content",
                                    data=b"", headers={"Content-Type": "application/octet-stream"},
                                    ok=(200, 201)).json()
                return item, EMPTY_HASH
            upload = _FragmentUpload(self, handle, path, size, progress)
            return upload.run(parent_id, name, file_times, saved_session, remember_session)


class _FragmentUpload:
    """Uploads one file through a Graph upload session, resuming where OneDrive
    says it stopped after any interruption."""

    def __init__(self, client: GraphClient, handle, path: str, size: int, progress: Callable[[int], None]):
        self.client, self.handle, self.path, self.size = client, handle, path, size
        self.progress = progress
        self.hasher = QuickXorHash()
        self.hashed = 0          # bytes [0, hashed) are in self.hasher
        self.reported = 0        # bytes reported to the progress display

    # local file access
    def _read(self, offset: int, length: int) -> bytes:
        try:
            self.handle.seek(offset)
            data = self.handle.read(length)
        except OSError as exc:
            raise LocalReadError(describe_os_error(self.path, exc)) from exc
        if len(data) != length:
            raise LocalReadError("The file got shorter while it was being uploaded; it will be tried again next run.")
        return data

    def _hash_up_to(self, offset: int, chunk: bytes | None = None, chunk_at: int = -1) -> None:
        if offset < self.hashed:
            self.hasher, self.hashed = QuickXorHash(), 0
        while self.hashed < offset:
            if chunk is not None and chunk_at == self.hashed and self.hashed + len(chunk) <= offset:
                data = chunk
            else:
                data = self._read(self.hashed, min(FRAGMENT_SIZE, offset - self.hashed))
            self.hasher.update(data)
            self.hashed += len(data)

    def _report(self, accepted: int) -> None:
        if accepted != self.reported:
            self.progress(accepted - self.reported)
            self.reported = accepted

    # session handling
    def _create_session(self, parent_id: str, name: str, file_times: dict | None) -> str:
        url = f"/drives/{self.client.drive_id}/items/{parent_id}:/{quote(name, safe='')}:/createUploadSession"
        item: dict = {"@microsoft.graph.conflictBehavior": "replace"}
        if file_times:
            item["fileSystemInfo"] = file_times
        try:
            resp = self.client.request("POST", url, json_body={"item": item}, ok=(200,))
        except GraphError as exc:
            if exc.status != 400 or "fileSystemInfo" not in item:
                raise
            log.info("OneDrive refused the file dates of %s (%s); uploading without them.", self.path, exc.message)
            del item["fileSystemInfo"]
            resp = self.client.request("POST", url, json_body={"item": item}, ok=(200,))
        return resp.json()["uploadUrl"]

    def _status(self, upload_url: str) -> int | None:
        """Where OneDrive wants the next byte, or None if the session is gone."""
        resp = self.client.request("GET", upload_url, auth=False, ok=(200, 404))
        if resp.status_code == 404:
            return None
        ranges = resp.json().get("nextExpectedRanges") or []
        if not ranges:
            return self.size
        return int(str(ranges[0]).split("-")[0])

    def _committed_item(self, parent_id: str, name: str) -> dict | None:
        """After losing the reply to the last fragment: is the file already complete in OneDrive?"""
        item = self.client.get_child(parent_id, name)
        if not item or "file" not in item or int(item.get("size") or -1) != self.size:
            return None
        self._hash_up_to(self.size)
        remote_hash = ((item.get("file") or {}).get("hashes") or {}).get("quickXorHash")
        return item if remote_hash == self.hasher.b64digest() else None

    def run(self, parent_id: str, name: str, file_times: dict | None, saved_session: str | None,
            remember_session: Callable[[str | None], None] | None) -> tuple[dict, str]:
        upload_url, offset = None, 0
        if saved_session:
            try:
                offset = self._status(saved_session)
            except GraphError:
                offset = None
            if offset is not None:
                upload_url = saved_session
                log.info("Resuming earlier upload of %s at %.1f%%.", self.path, 100.0 * offset / self.size)
        restarts = 0
        while True:
            if upload_url is None:
                upload_url, offset = self._create_session(parent_id, name, file_times), 0
                if remember_session and self.size >= LARGE_FILE_SIZE:
                    remember_session(upload_url)
            try:
                item = self._send(upload_url, offset, parent_id, name)
                break
            except _SessionGone:
                restarts += 1
                if restarts > 2:
                    raise FileFailed("OneDrive kept discarding the upload; it will be tried again next run.")
                log.info("Upload session for %s expired; starting it again.", self.path)
                upload_url = None
                self._report(0)
            except (FileFailed, GraphError, FatalRunError):
                self._cancel(upload_url)
                if remember_session:
                    remember_session(None)
                raise
        if remember_session:
            remember_session(None)
        self._hash_up_to(self.size)
        local_hash = self.hasher.b64digest()
        if int(item.get("size") or -1) != self.size:
            raise FileFailed(f"OneDrive stored {item.get('size')} bytes instead of {self.size}.")
        remote_hash = ((item.get("file") or {}).get("hashes") or {}).get("quickXorHash")
        if not remote_hash:
            fetched = self.client.request(
                "GET", f"/drives/{self.client.drive_id}/items/{item['id']}?$select=id,size,file").json()
            remote_hash = ((fetched.get("file") or {}).get("hashes") or {}).get("quickXorHash")
        if not remote_hash:
            log.warning("OneDrive has no checksum for %s yet; the size matches and the checksum "
                        "will be compared on a later run.", self.path)
        elif remote_hash != local_hash:
            raise FileFailed("The copy in OneDrive does not match the local file (checksum differs).")
        return item, local_hash

    def _cancel(self, upload_url: str | None) -> None:
        if not upload_url:
            return
        try:
            self.client._session().delete(upload_url, timeout=TIMEOUT)
        except requests.RequestException:
            pass

    def _send(self, upload_url: str, offset: int, parent_id: str, name: str) -> dict:
        attempts = 0
        network_waited = 0.0
        while True:
            self.client.stopper.check()
            if offset >= self.size:
                # Everything was received but we never saw the final answer.
                item = self._committed_item(parent_id, name)
                if item is None:
                    raise _SessionGone()
                return item
            self._hash_up_to(offset)
            self._report(offset)
            length = min(FRAGMENT_SIZE, self.size - offset)
            chunk = self._read(offset, length)
            headers = {"Content-Length": str(length),
                       "Content-Range": f"bytes {offset}-{offset + length - 1}/{self.size}"}
            try:
                resp = self.client._session().put(upload_url, data=chunk, headers=headers,
                                                  timeout=FRAGMENT_TIMEOUT)
            except requests.RequestException as exc:
                network_waited = self.client.wait_for_network(exc, network_waited, attempts + 1)
                attempts += 1
                offset = self._resync(upload_url, parent_id, name)
                if offset is None:
                    raise _SessionGone()
                if isinstance(offset, dict):
                    return offset
                continue
            network_waited = 0.0
            code = resp.status_code
            if code in (200, 201, 202):
                self._hash_up_to(offset + length, chunk, offset)
                self._report(offset + length)
                attempts = 0
                if code != 202:
                    return resp.json()
                ranges = resp.json().get("nextExpectedRanges") or []
                offset = int(str(ranges[0]).split("-")[0]) if ranges else offset + length
                continue
            if code == 404:
                raise _SessionGone()
            if code == 416 or code in RETRYABLE_STATUS:
                attempts += 1
                if attempts > MAX_HTTP_ATTEMPTS:
                    raise FileFailed(f"OneDrive kept rejecting part of the file (HTTP {code}).")
                if code in RETRYABLE_STATUS:
                    self.client.stopper.sleep(_retry_after_seconds(resp) or _backoff(attempts))
                offset = self._resync(upload_url, parent_id, name)
                if offset is None:
                    raise _SessionGone()
                if isinstance(offset, dict):
                    return offset
                continue
            raise error_for(resp)

    def _resync(self, upload_url: str, parent_id: str, name: str):
        """Ask OneDrive where to continue. Returns an offset, a finished driveItem
        (the file was completed although we missed the reply), or None."""
        position = self._status(upload_url)
        if position is None:
            return self._committed_item(parent_id, name)
        return position


def _short(url: str) -> str:
    return url.split("?")[0][-120:]


def _short_exc(exc: Exception) -> str:
    return (type(exc).__name__ + ": " + str(exc))[:200]
