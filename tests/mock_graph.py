"""In-process mock of the Microsoft Graph OneDrive for Business API.

Standard library only.  Intended for exercising a backup client end to end:

    g = MockGraph(page_size=200)
    g.start()
    ...  client talks to g.base_url ("http://127.0.0.1:<port>/v1.0") ...
    g.stop()

Behaviour follows the Graph docs for driveItem, createUploadSession,
PUT content, fileSystemInfo and delta; where those are silent it imitates
OneDrive for Business (case-insensitive / case-preserving names, second
precision timestamps, no cTag on folders, SharePoint name restrictions ...).
See ``MockGraph`` for the public API and test hooks.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import re
import socket
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

__all__ = ["MockGraph", "QuickXorHash", "quickxorhash_reference", "FRAGMENT_MULTIPLE"]


# --------------------------------------------------------------------------
# QuickXorHash: a direct port of Microsoft's published C# reference
# (QuickXorHash : HashAlgorithm; HashCore / HashFinal / Initialize).
# --------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1


class QuickXorHash:
    """Line-by-line port of the C# ``QuickXorHash`` reference class.

    ``update`` is ``HashCore`` (may be called repeatedly with arbitrary
    block sizes), ``digest`` is ``HashFinal``.  UInt64 arithmetic is emulated
    by masking to 64 bits.
    """

    BITS_IN_LAST_CELL = 32
    SHIFT = 11
    THRESHOLD = 600  # unused by the reference algorithm, kept for fidelity
    WIDTH_IN_BITS = 160

    def __init__(self) -> None:
        self.initialize()

    def initialize(self) -> None:
        self._data = [0] * ((self.WIDTH_IN_BITS - 1) // 64 + 1)  # ulong[3]
        self._shift_so_far = 0
        self._length_so_far = 0

    def update(self, array: bytes) -> None:
        """HashCore(array, ibStart=0, cbSize=len(array))."""
        array = bytes(array)
        cb_size = len(array)
        data = self._data
        current_shift = self._shift_so_far

        # The bitvector where we'll start xoring
        vector_array_index = current_shift // 64
        # The position within the bit vector at which we begin xoring
        vector_offset = current_shift % 64
        iterations = min(cb_size, self.WIDTH_IN_BITS)

        for i in range(iterations):
            is_last_cell = vector_array_index == len(data) - 1
            bits_in_vector_cell = self.BITS_IN_LAST_CELL if is_last_cell else 64

            # There's at least 2 bitvectors before we reach the end of the array
            if vector_offset <= bits_in_vector_cell - 8:
                cell = data[vector_array_index]
                for j in range(i, cb_size, self.WIDTH_IN_BITS):
                    cell = (cell ^ (array[j] << vector_offset)) & _MASK64
                data[vector_array_index] = cell
            else:
                index1 = vector_array_index
                index2 = 0 if is_last_cell else vector_array_index + 1
                low = bits_in_vector_cell - vector_offset

                xored_byte = 0
                for j in range(i, cb_size, self.WIDTH_IN_BITS):
                    xored_byte ^= array[j]
                data[index1] = (data[index1] ^ (xored_byte << vector_offset)) & _MASK64
                data[index2] = (data[index2] ^ (xored_byte >> low)) & _MASK64

            vector_offset += self.SHIFT
            while vector_offset >= bits_in_vector_cell:
                vector_array_index = 0 if is_last_cell else vector_array_index + 1
                vector_offset -= bits_in_vector_cell

        # Update the starting position in a circular shift pattern
        self._shift_so_far = (
            self._shift_so_far + self.SHIFT * (cb_size % self.WIDTH_IN_BITS)
        ) % self.WIDTH_IN_BITS
        self._length_so_far += cb_size

    def digest(self) -> bytes:
        """HashFinal(): 20 raw bytes."""
        data = self._data
        rgb = bytearray((self.WIDTH_IN_BITS - 1) // 8 + 1)
        # Block copy all our bitvectors to this byte array (little-endian, as
        # BitConverter.GetBytes on x86)
        for i in range(len(data) - 1):
            rgb[i * 8:i * 8 + 8] = data[i].to_bytes(8, "little")
        last = (len(data) - 1) * 8
        rgb[last:] = data[-1].to_bytes(8, "little")[: len(rgb) - last]
        # XOR the file length with the least significant bits (little-endian)
        length_bytes = (self._length_so_far & _MASK64).to_bytes(8, "little")
        for i in range(len(length_bytes)):
            rgb[(self.WIDTH_IN_BITS // 8) - len(length_bytes) + i] ^= length_bytes[i]
        return bytes(rgb)

    def b64digest(self) -> str:
        return base64.b64encode(self.digest()).decode("ascii")


def quickxorhash_reference(data: bytes) -> str:
    """Base64 QuickXorHash of ``data`` (the value Graph reports in
    ``file.hashes.quickXorHash``)."""
    h = QuickXorHash()
    h.update(data)
    return h.b64digest()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

FRAGMENT_MULTIPLE = 327680  # 320 KiB
_UTC = _dt.timezone.utc
_INVALID_NAME_CHARS = set('"*:<>?/\\|')
_RESERVED_NAMES = (
    {".lock", "con", "prn", "aux", "nul", "desktop.ini"}
    | {"com%d" % i for i in range(10)}
    | {"lpt%d" % i for i in range(10)}
)
_DT_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?"
    r"(Z|z|[+-]\d{2}:?\d{2})?$"
)
_CONTENT_RANGE_RE = re.compile(r"^\s*bytes\s+(\d+)-(\d+)/(\d+)\s*$", re.I)
_JSON_CT = (
    "application/json;odata.metadata=minimal;odata.streaming=true;"
    "IEEE754Compatible=false;charset=utf-8"
)
_DEFAULT_ERROR_CODES = {
    400: "invalidRequest",
    401: "InvalidAuthenticationToken",
    403: "accessDenied",
    404: "itemNotFound",
    405: "invalidRequest",
    409: "nameAlreadyExists",
    410: "resyncRequired",
    411: "invalidRequest",
    412: "resourceModified",
    413: "requestTooLarge",
    416: "invalidRange",
    423: "resourceLocked",
    429: "activityLimitReached",
    500: "generalException",
    501: "notSupported",
    502: "serviceNotAvailable",
    503: "serviceNotAvailable",
    504: "serviceNotAvailable",
    507: "quotaLimitReached",
    509: "activityLimitReached",
}


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_UTC)


def _fmt(dt: _dt.datetime) -> str:
    return dt.astimezone(_UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_ms(dt: _dt.datetime) -> str:
    dt = dt.astimezone(_UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def _now_iso() -> str:
    return _fmt(_utcnow())


def _normalize_dt(value) -> str:
    """Parse an ISO-8601 timestamp and return it as OneDrive for Business
    stores it: UTC, whole seconds, 'Z' suffix.  Raises ValueError."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    m = _DT_RE.match(value.strip())
    if not m:
        raise ValueError("bad timestamp %r" % (value,))
    y, mo, d, hh, mi, ss, _frac, tz = m.groups()
    dt = _dt.datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0), tzinfo=_UTC)
    if tz and tz not in ("Z", "z"):
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset = _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
        dt = dt - sign * offset
    return _fmt(dt)


def _new_item_id() -> str:
    raw = uuid.uuid4().bytes + uuid.uuid4().bytes[:4]  # 20 bytes -> 32 base32 chars
    return "01" + base64.b32encode(raw).decode("ascii")


def _b64url(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _unb64url(tok: str):
    pad = "=" * (-len(tok) % 4)
    return json.loads(base64.urlsafe_b64decode(tok + pad).decode())


class _CIHeaders(dict):
    """Plain dict of request headers (original case) with case-insensitive
    lookup through ``[]``, ``get`` and ``in``."""

    def _key(self, k):
        if isinstance(k, str):
            for kk in dict.keys(self):
                if kk.lower() == k.lower():
                    return kk
        return k

    def __getitem__(self, k):
        return dict.__getitem__(self, self._key(k))

    def get(self, k, default=None):
        return dict.get(self, self._key(k), default)

    def __contains__(self, k):
        return dict.__contains__(self, self._key(k))


class _GraphError(Exception):
    def __init__(self, status, code=None, message=None, headers=None, body=None):
        super().__init__(message or code)
        self.status = status
        self.code = code or _DEFAULT_ERROR_CODES.get(status, "generalException")
        self.message = message or self.code
        self.headers = dict(headers or {})
        self.body = body


def _not_found(what="The resource could not be found."):
    return _GraphError(404, "itemNotFound", what)


def _bad_request(msg="Invalid request"):
    return _GraphError(400, "invalidRequest", msg)


class _Item:
    __slots__ = (
        "id", "name", "parent", "is_folder", "children", "content", "qxh",
        "created", "modified", "fs_created", "fs_modified", "guid",
        "etag_v", "ctag_v", "seq",
    )

    def __init__(self, name, parent, is_folder):
        now = _now_iso()
        self.id = _new_item_id()
        self.name = name
        self.parent = parent
        self.is_folder = is_folder
        self.children = {} if is_folder else None  # lower(name) -> _Item
        self.content = b""
        self.qxh = quickxorhash_reference(b"")
        self.created = now
        self.modified = now
        self.fs_created = now
        self.fs_modified = now
        self.guid = str(uuid.uuid4()).upper()
        self.etag_v = 1
        self.ctag_v = 1
        self.seq = 0


class _Session:
    def __init__(self, sid, tempauth, base_id, segments, target_id, conflict,
                 fs_created, fs_modified, defer, lifetime):
        self.id = sid
        self.tempauth = tempauth
        self.base_id = base_id
        self.segments = segments
        self.target_id = target_id
        self.conflict = conflict
        self.fs_created = fs_created
        self.fs_modified = fs_modified
        self.defer = defer
        self.total = None
        self.buf = bytearray()
        self.lifetime = lifetime
        self.expires = _utcnow() + _dt.timedelta(seconds=lifetime)

    def touch(self):
        self.expires = _utcnow() + _dt.timedelta(seconds=self.lifetime)

    def ranges(self):
        if self.total is not None and len(self.buf) >= self.total:
            return []
        return ["%d-" % len(self.buf)]


class _Req:
    __slots__ = ("method", "raw_path", "raw_query", "params", "headers", "body", "body_len", "too_large")


# --------------------------------------------------------------------------
# HTTP plumbing
# --------------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True
    request_queue_size = 64

    def handle_error(self, request, client_address):  # never print tracebacks
        pass


class _ShortBody(Exception):
    pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockGraph/1.0"
    sys_version = ""
    timeout = 120  # idle keep-alive / slow body timeout (seconds)

    def log_message(self, format, *args):  # silence
        pass

    def setup(self):
        super().setup()
        self.server.mock._conns.add(self.connection)

    def finish(self):
        try:
            super().finish()
        except Exception:
            pass
        self.server.mock._conns.discard(self.connection)

    def handle(self):
        try:
            super().handle()
        except Exception:  # client went away, timeouts, ... never crash
            self.close_connection = True

    def do_GET(self):
        self._dispatch()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_GET

    # -- body reading -------------------------------------------------------

    def _read_exact(self, n, keep):
        chunks = []
        remaining = n
        while remaining > 0:
            part = self.rfile.read(min(remaining, 1 << 20))
            if not part:
                raise _ShortBody()
            if keep:
                chunks.append(part)
            remaining -= len(part)
        return b"".join(chunks)

    def _read_body(self, keep_limit):
        """Return (body_or_None, length, too_large).  Always consumes the
        whole body so the connection can be reused."""
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            chunks, total, keep = [], 0, True
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    raise _ShortBody()
                try:
                    size = int(line.split(b";")[0].strip(), 16)
                except ValueError:
                    raise _ShortBody()
                if size == 0:
                    while True:  # trailers
                        t = self.rfile.readline(65537)
                        if t in (b"\r\n", b"\n", b""):
                            break
                    break
                total += size
                if total > keep_limit:
                    keep = False
                    chunks = []
                data = self._read_exact(size, keep)
                if keep:
                    chunks.append(data)
                self.rfile.readline(65537)  # CRLF after chunk
            return (b"".join(chunks) if keep else None), total, not keep
        cl = self.headers.get("Content-Length")
        if cl is None:
            return b"", 0, False
        try:
            n = int(cl)
            if n < 0:
                raise ValueError
        except ValueError:
            raise _ShortBody()
        if n > keep_limit:
            self._read_exact(n, False)
            return None, n, True
        return self._read_exact(n, True), n, False

    # -- responses ------------------------------------------------------------

    def _send(self, status, headers, payload, close=False):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        no_body = status == 204 or status == 304 or 100 <= status < 200
        if not no_body:
            self.send_header("Content-Length", str(len(payload)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if payload and not no_body and self.command != "HEAD":
            self.wfile.write(payload)
        self.wfile.flush()

    def send_error(self, code, message=None, explain=None):
        """Errors raised by BaseHTTPRequestHandler itself (malformed request
        line, unsupported method, ...) also get a Graph-shaped JSON body."""
        try:
            body = json.dumps({"error": {
                "code": _DEFAULT_ERROR_CODES.get(code, "invalidRequest"),
                "message": message or explain or "HTTP %d" % code}}).encode("utf-8")
            self.send_response(code, message)
            self.send_header("Content-Type", _JSON_CT)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if self.command != "HEAD" and code >= 200 and code not in (204, 304):
                self.wfile.write(body)
        except Exception:
            self.close_connection = True

    def _hard_close(self):
        self.close_connection = True
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- main ---------------------------------------------------------------

    def _dispatch(self):
        mock = self.server.mock
        split = urlsplit(self.path)
        req = _Req()
        req.method = self.command.upper()
        req.raw_path = split.path
        req.raw_query = split.query
        try:
            req.params = {k: v[0] for k, v in parse_qs(split.query, keep_blank_values=True).items()}
        except Exception:
            req.params = {}
        req.headers = self.headers
        req.body, req.body_len, req.too_large = b"", 0, False

        fault = mock._take_fault(req.method, req.raw_path)
        entry = {
            "method": req.method,
            "path": req.raw_path,
            "query": req.raw_query,
            "params": dict(req.params),
            "headers": _CIHeaders(self.headers.items()),
            "status": None,
            "body_len": 0,
            "fault": fault["kind"] if fault else None,
            "time": time.time(),
        }

        if fault and fault["kind"] == "fail" and not fault["after_body"]:
            status, headers, payload = mock._fault_response(fault)
            entry["status"] = status
            mock._log(entry)
            self._send(status, headers, payload, close=True)
            try:  # lingering close: drain what the client is still sending
                self.connection.settimeout(5)
                entry["body_len"] = self._read_body(0)[1]
            except Exception:
                pass
            return

        try:
            req.body, req.body_len, req.too_large = self._read_body(mock._keep_limit())
        except (_ShortBody, OSError):
            entry["status"] = None
            entry["fault"] = entry["fault"] or "client-disconnect"
            mock._log(entry)
            self._hard_close()
            return
        entry["body_len"] = req.body_len

        if fault and fault["kind"] == "drop":
            if fault.get("process"):
                mock._process(req)
            mock._log(entry)
            self._hard_close()
            return

        if fault and fault["kind"] == "fail":
            status, headers, payload = mock._fault_response(fault)
        else:
            status, headers, payload = mock._process(req)
        cid = self.headers.get("client-request-id")
        if cid:
            headers.setdefault("client-request-id", cid)
        headers.setdefault("request-id", str(uuid.uuid4()))
        entry["status"] = status
        mock._log(entry)

        if fault and fault["kind"] == "delay":
            time.sleep(fault["seconds"])
        self._send(status, headers, payload)


# --------------------------------------------------------------------------
# the mock service
# --------------------------------------------------------------------------


class MockGraph:
    """Mock Graph / OneDrive for Business server.

    Public attributes: ``base_url`` (after ``start``), ``drive_id``,
    ``recycle_bin``, ``requests_log``, ``quota_total``, ``page_size``,
    ``simple_upload_limit`` (250 MiB), ``max_fragment_size`` (60 MiB),
    ``session_lifetime`` (seconds).
    """

    def __init__(self, page_size: int = 200, valid_tokens=None):
        self.page_size = int(page_size)
        self._valid_tokens = set(valid_tokens) if valid_tokens is not None else {"test-token"}
        self._expired_tokens = set()
        self._lock = threading.RLock()
        self._fault_lock = threading.Lock()
        self._faults = []
        self._conns = set()
        self._httpd = None
        self._thread = None
        self.base_url = None
        self.origin = None
        self.drive_id = "b!" + base64.urlsafe_b64encode(
            uuid.uuid4().bytes + uuid.uuid4().bytes + uuid.uuid4().bytes
        ).decode("ascii")
        self.user_id = str(uuid.uuid4())
        self.quota_total = 1 << 40
        self.simple_upload_limit = 250 * 1024 * 1024
        self.max_fragment_size = 60 * 1024 * 1024
        self.session_lifetime = 3600
        self.recycle_bin = []
        self.requests_log = []
        self.internal_errors = []  # tracebacks of unexpected exceptions inside the mock
        self._download_auth = uuid.uuid4().hex
        self._sessions = {}
        self._seq = 0
        self._tombstones = []  # (seq, json)
        self._delta_snapshots = {}
        self._root = _Item("root", None, True)
        self._items = {self._root.id: self._root}
        self._drive_created = _now_iso()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._httpd = _Server(("127.0.0.1", 0), _Handler)
        self._httpd.mock = self
        port = self._httpd.server_address[1]
        self.origin = "http://127.0.0.1:%d" % port
        self.base_url = self.origin + "/v1.0"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05},
            name="MockGraph-%d" % port, daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        for c in list(self._conns):
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    # -- auth helpers -----------------------------------------------------

    def expire_tokens(self) -> None:
        with self._lock:
            self._expired_tokens |= self._valid_tokens
            self._valid_tokens = set()

    def set_valid_tokens(self, tokens) -> None:
        with self._lock:
            self._valid_tokens = set(tokens)
            self._expired_tokens -= self._valid_tokens

    @property
    def valid_tokens(self):
        with self._lock:
            return set(self._valid_tokens)

    # -- fault injection ----------------------------------------------------

    def _add_fault(self, **f):
        re.compile(f["regex"])
        with self._fault_lock:
            self._faults.append(f)

    def fail_next(self, method: str, path_regex: str, status: int, count: int = 1,
                  headers: dict | None = None, body: dict | None = None,
                  after_body: bool = True) -> None:
        """Next ``count`` matching requests get ``status`` (Graph error body
        unless ``body`` is given) instead of normal handling.  ``method`` may
        be '*'.  ``path_regex`` is re.search()ed against the raw and the
        percent-decoded URL path (query excluded)."""
        self._add_fault(kind="fail", method=method.upper(), regex=path_regex, status=int(status),
                        count=int(count), headers=dict(headers or {}), body=body,
                        after_body=bool(after_body))

    def delay_next(self, method: str, path_regex: str, seconds: float, count: int = 1) -> None:
        """Process the request normally, then sleep before writing the reply."""
        self._add_fault(kind="delay", method=method.upper(), regex=path_regex,
                        seconds=float(seconds), count=int(count))

    def drop_next(self, method: str, path_regex: str, count: int = 1, process: bool = False) -> None:
        """Read the body, then close the socket without a response.  With
        ``process=True`` the request is applied first (reply lost)."""
        self._add_fault(kind="drop", method=method.upper(), regex=path_regex,
                        count=int(count), process=bool(process))

    def clear_faults(self) -> None:
        with self._fault_lock:
            self._faults.clear()

    def pending_faults(self) -> list:
        with self._fault_lock:
            return [dict(f) for f in self._faults]

    def _take_fault(self, method, raw_path):
        with self._fault_lock:
            if not self._faults:
                return None
            try:
                decoded = unquote(raw_path)
            except Exception:
                decoded = raw_path
            for f in self._faults:
                if f["count"] <= 0:
                    continue
                if f["method"] not in ("*", method):
                    continue
                if re.search(f["regex"], raw_path) or re.search(f["regex"], decoded):
                    f["count"] -= 1
                    if f["count"] <= 0:
                        self._faults.remove(f)
                    return dict(f)
        return None

    def _fault_response(self, fault):
        status = fault["status"]
        body = fault.get("body")
        if body is None:
            code = _DEFAULT_ERROR_CODES.get(status, "generalException")
            body = {"error": {"code": code, "message": "Injected failure (HTTP %d)" % status}}
        headers = {"Content-Type": _JSON_CT}
        headers.update(fault.get("headers") or {})
        payload = b"" if status == 204 else json.dumps(body).encode("utf-8")
        return status, headers, payload

    def _log(self, entry):
        self.requests_log.append(entry)

    def _keep_limit(self):
        return max(self.simple_upload_limit, self.max_fragment_size) + 1

    # -- test helpers -------------------------------------------------------

    def _split_path(self, path):
        return [p for p in str(path).replace("\\", "/").split("/") if p not in ("",)]

    def _helper_mkdirs(self, segments):
        cur = self._root
        for s in segments:
            nxt = cur.children.get(s.lower())
            if nxt is None:
                nxt = self._create_item(cur, s, True)
            elif not nxt.is_folder:
                raise ValueError("%r is a file" % _path_of(nxt))
            cur = nxt
        return cur

    def add_folder(self, path: str) -> dict:
        with self._lock:
            return self._item_json(self._helper_mkdirs(self._split_path(path)))

    def add_file(self, path: str, content: bytes, mtime: str | None = None) -> dict:
        if isinstance(content, str):
            content = content.encode("utf-8")
        content = bytes(content)
        segs = self._split_path(path)
        if not segs:
            raise ValueError("empty path")
        with self._lock:
            parent = self._helper_mkdirs(segs[:-1])
            fs_m = _normalize_dt(mtime) if mtime else None
            existing = parent.children.get(segs[-1].lower())
            if existing is not None:
                if existing.is_folder:
                    raise ValueError("%r is a folder" % _path_of(existing))
                self._set_content(existing, content, None, fs_m)
                return self._item_json(existing)
            item = self._create_item(parent, segs[-1], False, content, None, fs_m)
            return self._item_json(item)

    def tree(self) -> dict:
        with self._lock:
            out = {}
            for it in self._walk_subtree(self._root):
                if not it.is_folder:
                    out[_path_of(it)] = it.content
            return out

    def folders(self) -> set:
        with self._lock:
            return {_path_of(it) for it in self._walk_subtree(self._root)
                    if it.is_folder and it is not self._root}

    def get_item_by_path(self, path) -> dict | None:
        with self._lock:
            cur = self._root
            for s in self._split_path(path):
                if not cur.is_folder:
                    return None
                cur = cur.children.get(s.lower())
                if cur is None:
                    return None
            return self._item_json(cur)

    def get_item(self, item_id) -> dict | None:
        with self._lock:
            it = self._items.get(item_id)
            return self._item_json(it) if it else None

    @property
    def root_id(self) -> str:
        return self._root.id

    @property
    def quota_used(self) -> int:
        with self._lock:
            return self._tree_size(self._root)

    def upload_sessions(self) -> list:
        """Summaries of open upload sessions."""
        with self._lock:
            return [{"id": s.id, "uploadUrl": self._upload_url(s), "received": len(s.buf),
                     "total": s.total, "segments": list(s.segments), "target_id": s.target_id,
                     "expirationDateTime": _fmt_ms(s.expires)} for s in self._sessions.values()]

    def expire_upload_sessions(self) -> None:
        """Make every open upload session expired (subsequent use -> 404)."""
        with self._lock:
            for s in self._sessions.values():
                s.expires = _utcnow() - _dt.timedelta(seconds=1)

    # -- model internals ----------------------------------------------------

    def _bump(self, item):
        self._seq += 1
        it = item
        while it is not None:
            it.seq = self._seq
            it = it.parent

    def _create_item(self, parent, name, is_folder, content=b"", fs_c=None, fs_m=None):
        item = _Item(name, parent, is_folder)
        if not is_folder:
            item.content = bytes(content)
            item.qxh = quickxorhash_reference(item.content)
        if fs_c:
            item.fs_created = fs_c
        if fs_m:
            item.fs_modified = fs_m
        parent.children[name.lower()] = item
        self._items[item.id] = item
        self._bump(item)
        return item

    def _set_content(self, item, content, fs_c, fs_m):
        item.content = bytes(content)
        item.qxh = quickxorhash_reference(item.content)
        item.modified = _now_iso()
        item.etag_v += 1
        item.ctag_v += 1
        item.fs_modified = fs_m or item.modified
        if fs_c:
            item.fs_created = fs_c
        self._bump(item)

    def _walk_subtree(self, item):
        """Pre-order, children sorted case-insensitively."""
        stack = [item]
        while stack:
            it = stack.pop()
            yield it
            if it.is_folder:
                stack.extend(sorted(it.children.values(), key=lambda c: c.name.lower(), reverse=True))

    def _tree_size(self, item):
        if not item.is_folder:
            return len(item.content)
        return sum(self._tree_size(c) for c in item.children.values())

    def _upload_url(self, s):
        return "%s/upload/%s?guid=%%27%s%%27&overwrite=True&rename=False&dc=0&tempauth=%s" % (
            self.origin, s.id, uuid.UUID(s.id), s.tempauth)

    def _download_url(self, item):
        return "%s/download/%s?tempauth=%s" % (self.origin, item.id, self._download_auth)

    def _parent_ref(self, item):
        ref = {"driveId": self.drive_id, "driveType": "business"}
        if item.parent is not None:
            ref["id"] = item.parent.id
            ppath = _path_of(item.parent)
            ref["path"] = "/drive/root:" + ("/" + ppath if ppath else "")
        return ref

    def _item_json(self, it, delta=False):
        d = {
            "id": it.id,
            "name": it.name,
            "eTag": '"{%s},%d"' % (it.guid, it.etag_v),
            "createdDateTime": it.created,
            "lastModifiedDateTime": it.modified,
            "size": self._tree_size(it),
            "parentReference": self._parent_ref(it),
            "fileSystemInfo": {"createdDateTime": it.fs_created, "lastModifiedDateTime": it.fs_modified},
        }
        if it.is_folder:
            d["folder"] = {"childCount": len(it.children)}
            if it is self._root:
                d["root"] = {}
        else:
            if not delta:  # OneDrive for Business: no cTag on folders or in delta
                d["cTag"] = '"c:{%s},%d"' % (it.guid, it.ctag_v)
            d["file"] = {"mimeType": "application/octet-stream", "hashes": {"quickXorHash": it.qxh}}
            if self.origin:
                d["@microsoft.graph.downloadUrl"] = self._download_url(it)
        return d

    def _drive_json(self):
        used = self._tree_size(self._root)
        deleted = sum(e["size"] for e in self.recycle_bin)
        total = self.quota_total
        ratio = used / total if total else 1.0
        state = ("exceeded" if ratio >= 1 else "critical" if ratio >= 0.99
                 else "nearing" if ratio >= 0.9 else "normal")
        return {
            "id": self.drive_id,
            "driveType": "business",
            "name": "OneDrive",
            "createdDateTime": self._drive_created,
            "lastModifiedDateTime": _now_iso(),
            "owner": {"user": {"id": self.user_id, "displayName": "Test User",
                               "email": "test@example.com"}},
            "quota": {"total": total, "used": used, "remaining": max(total - used, 0),
                      "deleted": deleted, "state": state},
        }

    @staticmethod
    def _validate_name(name):
        if not name or name in (".", ".."):
            raise _bad_request("The name is empty or invalid.")
        if any(c in _INVALID_NAME_CHARS or ord(c) < 32 for c in name):
            raise _bad_request("The provided name cannot contain any illegal characters.")
        if name != name.strip(" "):
            raise _bad_request("Leading and trailing spaces are not allowed in names.")
        low = name.lower()
        if low in _RESERVED_NAMES or "_vti_" in low or name.startswith("~$"):
            raise _bad_request("The name %r is reserved." % name)

    def _check_path_len(self, parent, extra_segments):
        p = _path_of(parent)
        full = "/".join(([p] if p else []) + list(extra_segments))
        if len(full) > 400:
            raise _bad_request("The path is too long (max 400 characters).")

    def _unique_name(self, parent, name, is_folder):
        if is_folder or "." not in name.lstrip("."):
            stem, ext = name, ""
        else:
            stem, ext = name.rsplit(".", 1)
            ext = "." + ext
        n = 1
        while True:
            cand = "%s %d%s" % (stem, n, ext)
            if cand.lower() not in parent.children:
                return cand
            n += 1

    def _resolve(self, base, segments):
        cur = base
        for s in segments:
            if not cur.is_folder:
                raise _not_found()
            cur = cur.children.get(s.lower())
            if cur is None:
                raise _not_found()
        return cur

    def _walk_existing(self, base, dir_segments):
        """Deepest existing folder along dir_segments + the missing rest."""
        cur = base
        for i, s in enumerate(dir_segments):
            nxt = cur.children.get(s.lower())
            if nxt is None:
                return cur, list(dir_segments[i:])
            if not nxt.is_folder:
                raise _GraphError(409, "nameAlreadyExists",
                                  "A file with the name %r exists where a folder is required." % nxt.name)
            cur = nxt
        return cur, []

    def _check_quota(self, delta):
        if delta > 0 and self._tree_size(self._root) + delta > self.quota_total:
            raise _GraphError(507, "quotaLimitReached", "Insufficient Space Available")

    def _write_file(self, base, segments, content, conflict, fs_c, fs_m):
        """Create or replace base/segments with content.  Returns (status, item)."""
        if not base.is_folder:
            raise _bad_request("The parent item is not a folder.")
        name = segments[-1]
        self._validate_name(name)
        parent, missing = self._walk_existing(base, segments[:-1])
        for s in missing:
            self._validate_name(s)
        existing = None if missing else parent.children.get(name.lower())
        if existing is not None:
            if conflict == "rename":
                name = self._unique_name(parent, name, False)
                existing = None
            elif existing.is_folder or conflict == "fail":
                raise _GraphError(409, "nameAlreadyExists",
                                  "The specified item name already exists. Name: %s" % existing.name)
        self._check_path_len(parent, missing + [name])
        self._check_quota(len(content) - (len(existing.content) if existing else 0))
        for s in missing:
            parent = self._create_item(parent, s, True)
        if existing is not None:
            self._set_content(existing, content, fs_c, fs_m)
            return 200, existing
        return 201, self._create_item(parent, name, False, content, fs_c, fs_m)

    def _delete_item(self, item, recycle=True):
        """Detach item (and subtree); record delta tombstones; optionally put
        it in the recycle bin (DELETE does, conflict 'replace' does not)."""
        path = _path_of(item)
        snapshot = self._item_json(item)
        files, folders = {}, []
        subtree = list(self._walk_subtree(item))
        for it in subtree:
            if it.is_folder:
                folders.append(_path_of(it))
            else:
                files[_path_of(it)] = it.content
        del item.parent.children[item.name.lower()]
        self._bump(item.parent)
        for it in subtree:
            self._items.pop(it.id, None)
            self._seq += 1
            tomb = {"id": it.id, "deleted": {"state": "deleted"},
                    "parentReference": {"driveId": self.drive_id, "driveType": "business",
                                        "id": it.parent.id}}
            tomb["folder" if it.is_folder else "file"] = {}
            self._tombstones.append((self._seq, tomb))
        entry = {
            "id": item.id,
            "name": item.name,
            "path": path,
            "type": "folder" if item.is_folder else "file",
            "size": sum(len(c) for c in files.values()),
            "deletedDateTime": _now_iso(),
            "item": snapshot,
            "files": files,
            "folders": folders,
        }
        if not item.is_folder:
            entry["content"] = item.content
        if recycle:
            self.recycle_bin.append(entry)
        return entry

    def _check_if_match(self, req, item):
        im = req.headers.get("If-Match")
        if not im or im.strip() == "*":
            return
        tags = {('"{%s},%d"' % (item.guid, item.etag_v)), ('"c:{%s},%d"' % (item.guid, item.ctag_v))}
        cands = {t.strip() for t in im.split(",")} | {im.strip()}
        norm = {c if c.startswith('"') else '"%s"' % c for c in cands}
        if not (tags & norm):
            raise _GraphError(412, "resourceModified", "ETag does not match current item's value")

    # -- request processing ---------------------------------------------------

    def _json_response(self, status, obj, headers=None):
        h = {"Content-Type": _JSON_CT, "OData-Version": "4.0"}
        h.update(headers or {})
        return status, h, json.dumps(obj, ensure_ascii=False).encode("utf-8")

    def _error_response(self, e):
        if e.body is not None:
            body = e.body
        else:
            body = {"error": {"code": e.code, "message": e.message,
                              "innerError": {"date": _now_iso(), "request-id": str(uuid.uuid4())}}}
        return self._json_response(e.status, body, e.headers)

    def _process(self, req):
        try:
            with self._lock:
                p = req.raw_path
                if p.startswith("/upload/"):
                    return self._handle_upload(req)
                if p.startswith("/download/"):
                    return self._handle_download(req)
                if p == "/v1.0" or p.startswith("/v1.0/"):
                    self._check_auth(req)
                    if req.too_large:
                        raise _GraphError(413, "requestTooLarge", "The request body is too large.")
                    return self._route_api(req, p[len("/v1.0"):])
                raise _bad_request("Unsupported API version or route.")
        except _GraphError as e:
            return self._error_response(e)
        except Exception as e:  # pragma: no cover - mock bug guard
            tb = traceback.format_exc()
            self.internal_errors.append(tb)
            return self._error_response(_GraphError(500, "generalException", "mock internal error: %r" % (e,)))

    def _check_auth(self, req):
        auth = req.headers.get("Authorization")
        www = {"WWW-Authenticate": 'Bearer realm="", authorization_uri="https://login.microsoftonline.com/common/oauth2/authorize", client_id="00000003-0000-0000-c000-000000000000"'}
        if not auth:
            raise _GraphError(401, "InvalidAuthenticationToken", "Access token is empty.", www)
        parts = auth.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise _GraphError(401, "InvalidAuthenticationToken", "Access token validation failure.", www)
        tok = parts[1].strip()
        if tok in self._valid_tokens:
            return
        if tok in self._expired_tokens:
            raise _GraphError(401, "InvalidAuthenticationToken",
                              "Lifetime validation failed, the token is expired.", www)
        raise _GraphError(401, "InvalidAuthenticationToken",
                          "IDX14100: JWT is not well formed, there are no dots (.).", www)

    def _json_body(self, req, required=False):
        if not req.body:
            if required:
                raise _bad_request("A request body is required.")
            return {}
        try:
            obj = json.loads(req.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _bad_request("Invalid JSON in request body.")
        if not isinstance(obj, dict):
            raise _bad_request("Request body must be a JSON object.")
        return obj

    @staticmethod
    def _decode_segments(path_part):
        p = path_part
        if p.startswith("/"):
            p = p[1:]
        if p.endswith("/"):
            p = p[:-1]
        if p == "":
            return []
        segs = []
        for raw in p.split("/"):
            if raw == "":
                raise _bad_request("Empty path segment.")
            try:
                segs.append(unquote(raw, encoding="utf-8", errors="strict"))
            except UnicodeDecodeError:
                raise _bad_request("Path is not valid percent-encoded UTF-8.")
        return segs

    def _select(self, req):
        raw = req.params.get("$select", req.params.get("select"))
        if not raw:
            return None
        return {s.strip().lower() for s in raw.split(",") if s.strip()} | {"id", "name"}

    @staticmethod
    def _apply_select(d, select):
        if select is None:
            return d
        return {k: v for k, v in d.items() if k.lower() in select}

    def _route_api(self, req, rest):
        m = req.method
        if rest in ("/me", "/me/"):
            if m != "GET":
                raise _bad_request()
            return self._json_response(200, {
                "id": self.user_id, "displayName": "Test User",
                "userPrincipalName": "test@example.com", "mail": "test@example.com"})
        if rest == "/me/drive":
            if m != "GET":
                raise _bad_request()
            return self._json_response(200, self._drive_json())
        if rest == "/me/drives":
            if m != "GET":
                raise _bad_request()
            return self._json_response(200, {"value": [self._drive_json()]})
        mm = re.match(r"^/drives/([^/:]+)(.*)$", rest)
        if mm:
            if unquote(mm.group(1)) != self.drive_id:
                raise _not_found("The drive could not be found.")
            tail = mm.group(2)
            if tail in ("", "/"):
                if m != "GET":
                    raise _bad_request()
                return self._json_response(200, self._drive_json())
            return self._route_drive(req, tail)
        if rest.startswith("/me/drive/"):
            return self._route_drive(req, rest[len("/me/drive"):])
        raise _bad_request("Unsupported route.")

    def _route_drive(self, req, tail):
        mm = re.match(r"^/(?:(root)|items/([^/:]+))(.*)$", tail)
        if not mm:
            raise _bad_request("Unsupported route.")
        if mm.group(1):
            base = self._root
        else:
            iid = unquote(mm.group(2))
            base = self._root if iid.lower() == "root" else self._items.get(iid)
            if base is None:
                raise _not_found()
        rest = mm.group(3)
        segments = []
        if rest.startswith(":"):
            r2 = rest[1:]
            idx = r2.find(":")
            path_part, rest = (r2[:idx], r2[idx + 1:]) if idx >= 0 else (r2, "")
            segments = self._decode_segments(path_part)
        action = rest.rstrip("/") if rest not in ("", "/") else ""
        m = req.method

        if action == "":
            if m == "GET":
                item = self._resolve(base, segments)
                return self._json_response(200, self._apply_select(self._item_json(item), self._select(req)))
            if m == "PATCH":
                return self._patch(req, self._resolve(base, segments))
            if m == "DELETE":
                item = self._resolve(base, segments)
                if item is self._root:
                    raise _GraphError(403, "accessDenied", "The root folder cannot be deleted.")
                self._check_if_match(req, item)
                self._delete_item(item)
                return 204, {}, b""
        elif action == "/children":
            if m == "GET":
                return self._list_children(req, self._resolve(base, segments))
            if m == "POST":
                return self._create_child(req, self._resolve(base, segments))
        elif action == "/content":
            if m == "GET":
                item = self._resolve(base, segments)
                if item.is_folder:
                    raise _bad_request("Cannot download a folder.")
                return 302, {"Location": self._download_url(item), "Content-Type": "text/plain"}, b""
            if m == "PUT":
                return self._put_content(req, base, segments)
        elif action == "/createUploadSession":
            if m == "POST":
                return self._create_session(req, base, segments)
        elif action == "/delta":
            if m == "GET":
                item = self._resolve(base, segments)
                if item is not self._root:
                    raise _bad_request("Delta is only supported on the root folder.")
                return self._delta(req)
        raise _bad_request("Unsupported route.")

    # -- endpoints ------------------------------------------------------------

    def _list_children(self, req, folder):
        if not folder.is_folder:
            raise _bad_request("The item is not a folder.")
        select = self._select(req)
        page = self.page_size
        top = req.params.get("$top", req.params.get("top"))
        if top:
            try:
                top_i = int(top)
                if top_i < 1:
                    raise ValueError
            except ValueError:
                raise _bad_request("Invalid $top value.")
            page = min(page, top_i)
        items = sorted(folder.children.values(), key=lambda c: c.name.lower())
        tok = req.params.get("$skiptoken", req.params.get("skiptoken"))
        if tok:
            try:
                st = _unb64url(tok)
                after = st["after"]
                if st["f"] != folder.id:
                    raise ValueError
            except Exception:
                raise _bad_request("Invalid $skiptoken.")
            items = [c for c in items if c.name.lower() > after]
        chunk = items[:page]
        resp = {"value": [self._apply_select(self._item_json(c), select) for c in chunk]}
        if len(items) > page:
            q = []
            sel_raw = req.params.get("$select", req.params.get("select"))
            if sel_raw:
                q.append("$select=" + quote(sel_raw, safe=",@."))
            if top:
                q.append("$top=" + quote(top, safe=""))
            q.append("$skiptoken=" + _b64url({"f": folder.id, "after": chunk[-1].name.lower()}))
            resp["@odata.nextLink"] = self.origin + req.raw_path + "?" + "&".join(q)
        return self._json_response(200, resp)

    def _conflict_param(self, req, *candidates, default):
        for c in candidates:
            if c is not None:
                val = c
                break
        else:
            val = req.params.get("@microsoft.graph.conflictBehavior", default)
        if val not in ("fail", "replace", "rename"):
            raise _bad_request("Invalid @microsoft.graph.conflictBehavior %r." % (val,))
        return val

    def _parse_fsi(self, fsi):
        if fsi is None:
            return None, None
        if not isinstance(fsi, dict):
            raise _bad_request("fileSystemInfo must be an object.")
        try:
            c = _normalize_dt(fsi["createdDateTime"]) if fsi.get("createdDateTime") else None
            mo = _normalize_dt(fsi["lastModifiedDateTime"]) if fsi.get("lastModifiedDateTime") else None
        except ValueError as e:
            raise _bad_request("Invalid fileSystemInfo timestamp: %s" % e)
        return c, mo

    def _create_child(self, req, parent):
        if not parent.is_folder:
            raise _bad_request("The parent item is not a folder.")
        body = self._json_body(req, required=True)
        name = body.get("name")
        if not isinstance(name, str):
            raise _bad_request("The 'name' property is required.")
        self._validate_name(name)
        is_folder = "folder" in body
        if not is_folder and "file" not in body:
            raise _bad_request("Either a 'folder' or 'file' facet is required.")
        conflict = self._conflict_param(req, body.get("@microsoft.graph.conflictBehavior"), default="fail")
        fs_c, fs_m = self._parse_fsi(body.get("fileSystemInfo"))
        existing = parent.children.get(name.lower())
        if existing is not None:
            if conflict == "fail":
                raise _GraphError(409, "nameAlreadyExists",
                                  "The specified item name already exists. Name: %s" % existing.name)
            if conflict == "rename":
                name = self._unique_name(parent, name, is_folder)
            elif existing.is_folder and is_folder:  # replace on existing folder
                return self._json_response(200, self._item_json(existing))
            else:  # replace a file (or a folder by a file)
                self._delete_item(existing, recycle=False)
        self._check_path_len(parent, [name])
        item = self._create_item(parent, name, is_folder, b"", fs_c, fs_m)
        return self._json_response(201, self._item_json(item))

    def _put_content(self, req, base, segments):
        if req.too_large or req.body_len > self.simple_upload_limit:
            raise _GraphError(413, "requestTooLarge",
                              "The request body exceeds the 250 MB limit of a simple upload; use an upload session.")
        content = req.body or b""
        if segments:
            conflict = self._conflict_param(req, default="replace")
            status, item = self._write_file(base, segments, content, conflict, None, None)
            return self._json_response(status, self._item_json(item))
        if base.is_folder:
            raise _bad_request("Cannot upload content to a folder.")
        self._check_if_match(req, base)
        self._check_quota(len(content) - len(base.content))
        self._set_content(base, content, None, None)
        return self._json_response(200, self._item_json(base))

    def _patch(self, req, item):
        if item is self._root:
            raise _bad_request("The root folder cannot be modified.")
        self._check_if_match(req, item)
        body = self._json_body(req)
        fs_c, fs_m = self._parse_fsi(body.get("fileSystemInfo"))
        new_name = body.get("name")
        new_parent = None
        pref = body.get("parentReference")
        if isinstance(pref, dict) and (pref.get("id") or pref.get("path")):
            if pref.get("id"):
                pid = pref["id"]
                new_parent = self._root if pid.lower() == "root" else self._items.get(pid)
            else:
                p = pref["path"]
                for prefix in ("/drive/root:", "/drives/%s/root:" % self.drive_id):
                    if p.startswith(prefix):
                        p = p[len(prefix):]
                        break
                try:
                    new_parent = self._resolve(self._root, self._split_path(p))
                except _GraphError:
                    new_parent = None
            if new_parent is None:
                raise _not_found("The target parent could not be found.")
            if not new_parent.is_folder:
                raise _bad_request("The target parent is not a folder.")
            anc = new_parent
            while anc is not None:
                if anc is item:
                    raise _bad_request("Cannot move an item into itself.")
                anc = anc.parent
        if new_name is not None or new_parent is not None:
            name = new_name if new_name is not None else item.name
            if not isinstance(name, str):
                raise _bad_request("Invalid name.")
            self._validate_name(name)
            target = new_parent or item.parent
            conflict = self._conflict_param(req, body.get("@microsoft.graph.conflictBehavior"), default="fail")
            ex = target.children.get(name.lower())
            if ex is not None and ex is not item:
                if conflict == "fail":
                    raise _GraphError(409, "nameAlreadyExists",
                                      "The specified item name already exists. Name: %s" % ex.name)
                if conflict == "rename":
                    name = self._unique_name(target, name, item.is_folder)
                else:
                    self._delete_item(ex, recycle=False)
            del item.parent.children[item.name.lower()]
            self._bump(item.parent)
            item.name = name
            item.parent = target
            target.children[name.lower()] = item
        if fs_c:
            item.fs_created = fs_c
        if fs_m:
            item.fs_modified = fs_m
        item.etag_v += 1
        self._bump(item)
        return self._json_response(200, self._item_json(item))

    def _create_session(self, req, base, segments):
        body = self._json_body(req)
        props = body.get("item") or {}
        if not isinstance(props, dict):
            raise _bad_request("'item' must be an object.")
        conflict = self._conflict_param(
            req, props.get("@microsoft.graph.conflictBehavior"),
            body.get("@microsoft.graph.conflictBehavior"), default="fail")
        fs_c, fs_m = self._parse_fsi(props.get("fileSystemInfo"))
        target_id = None
        if segments:
            if not base.is_folder:
                raise _bad_request("The parent item is not a folder.")
            name = segments[-1]
            self._validate_name(name)
            body_name = props.get("name")
            if body_name is not None and (not isinstance(body_name, str) or body_name.lower() != name.lower()):
                raise _bad_request("item.name does not match the name in the URL.")
            parent, missing = self._walk_existing(base, segments[:-1])
            for s in missing:
                self._validate_name(s)
            self._check_path_len(parent, missing + [name])
            if not missing:
                ex = parent.children.get(name.lower())
                if ex is not None and conflict != "rename" and (ex.is_folder or conflict == "fail"):
                    raise _GraphError(409, "nameAlreadyExists",
                                      "The specified item name already exists. Name: %s" % ex.name)
        else:
            if base.is_folder:
                raise _bad_request("An upload session for an existing item requires a file.")
            self._check_if_match(req, base)
            target_id = base.id
        sid = uuid.uuid4().hex
        s = _Session(sid, uuid.uuid4().hex + uuid.uuid4().hex, base.id, list(segments), target_id,
                     conflict, fs_c, fs_m, bool(body.get("deferCommit")), self.session_lifetime)
        self._sessions[sid] = s
        return self._json_response(200, {
            "uploadUrl": self._upload_url(s),
            "expirationDateTime": _fmt_ms(s.expires),
            "nextExpectedRanges": ["0-"],
        })

    def _commit_session(self, s):
        content = bytes(s.buf)
        if s.target_id is not None:
            item = self._items.get(s.target_id)
            if item is None:
                raise _not_found("The item targeted by the upload session no longer exists.")
            self._check_quota(len(content) - len(item.content))
            self._set_content(item, content, s.fs_created, s.fs_modified)
            return 200, item
        base = self._items.get(s.base_id)
        if base is None:
            raise _not_found("The parent folder of the upload session no longer exists.")
        return self._write_file(base, s.segments, content, s.conflict, s.fs_created, s.fs_modified)

    def _handle_upload(self, req):
        mm = re.match(r"^/upload/([0-9a-f]{32})/?$", req.raw_path)
        if not mm:
            raise _not_found("The upload session was not found.")
        if req.headers.get("Authorization") is not None:
            raise _GraphError(401, "unauthenticated",
                              "The upload URL is pre-authenticated; do not send an Authorization header.")
        s = self._sessions.get(mm.group(1))
        if s is None:
            raise _not_found("The upload session was not found.")
        if req.params.get("tempauth") != s.tempauth:
            raise _GraphError(401, "unauthenticated", "Invalid or missing tempauth in upload URL.")
        if s.expires <= _utcnow():
            del self._sessions[s.id]
            raise _not_found("The upload session has expired.")
        m = req.method
        if m == "GET":
            return self._json_response(200, {"expirationDateTime": _fmt_ms(s.expires),
                                             "nextExpectedRanges": s.ranges()})
        if m == "DELETE":
            del self._sessions[s.id]
            return 204, {}, b""
        if m == "POST":  # explicit commit of a deferCommit session
            if not s.defer or s.total is None or len(s.buf) != s.total:
                raise _bad_request("The upload session is not ready to be committed.")
            status, item = self._commit_session(s)
            del self._sessions[s.id]
            return self._json_response(status, self._item_json(item))
        if m != "PUT":
            raise _bad_request("Unsupported method for an upload URL.")

        cr = req.headers.get("Content-Range")
        crm = _CONTENT_RANGE_RE.match(cr or "")
        if not crm:
            raise _bad_request("Missing or invalid Content-Range header (expected 'bytes a-b/total').")
        a, b, total = (int(x) for x in crm.groups())
        if req.too_large or req.body_len > self.max_fragment_size:
            raise _GraphError(413, "requestTooLarge", "Fragment exceeds the maximum fragment size.")
        body = req.body or b""
        if b < a or b - a + 1 != len(body):
            raise _bad_request("Content-Range does not match the length of the request body.")
        if total <= 0 or b >= total:
            raise _bad_request("Content-Range end is beyond the declared total size.")
        if s.total is not None and total != s.total:
            raise _bad_request("Declared total size %d differs from %d given earlier." % (total, s.total))
        received = len(s.buf)
        if s.total is not None and received >= s.total:
            raise _GraphError(416, "invalidRange", "All bytes have already been received.")
        if a < received:
            raise _GraphError(416, "invalidRange",
                              "The fragment starting at %d was already received; next expected %d." % (a, received))
        if a > received:
            raise _GraphError(416, "invalidRange",
                              "Fragments must be uploaded in order; next expected %d, got %d." % (received, a))
        final = b + 1 == total
        if not final and len(body) % FRAGMENT_MULTIPLE:
            raise _bad_request("Non-final fragment size must be a multiple of 320 KiB (327680 bytes).")
        s.total = total
        s.buf += body
        s.touch()
        if not final or s.defer:
            return self._json_response(202, {"expirationDateTime": _fmt_ms(s.expires),
                                             "nextExpectedRanges": s.ranges()})
        try:
            status, item = self._commit_session(s)
        except _GraphError:
            del s.buf[received:]  # final fragment not kept: it may be retried
            raise
        del self._sessions[s.id]
        return self._json_response(status, self._item_json(item))

    def _handle_download(self, req):
        mm = re.match(r"^/download/([^/]+)$", req.raw_path)
        if not mm or req.method not in ("GET", "HEAD"):
            raise _bad_request("Unsupported download request.")
        if req.params.get("tempauth") != self._download_auth:
            raise _GraphError(401, "unauthenticated", "Invalid download URL.")
        item = self._items.get(unquote(mm.group(1)))
        if item is None or item.is_folder:
            raise _not_found()
        data = item.content
        headers = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes",
                   "ETag": '"{%s},%d"' % (item.guid, item.etag_v)}
        rng = req.headers.get("Range")
        if rng:
            rm = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
            if rm and (rm.group(1) or rm.group(2)):
                if rm.group(1):
                    start = int(rm.group(1))
                    end = int(rm.group(2)) if rm.group(2) else len(data) - 1
                else:
                    start = max(len(data) - int(rm.group(2)), 0)
                    end = len(data) - 1
                end = min(end, len(data) - 1)
                if start >= len(data) or start > end:
                    headers["Content-Range"] = "bytes */%d" % len(data)
                    return 416, headers, b""
                headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, len(data))
                return 206, headers, data[start:end + 1]
        return 200, headers, data

    def _delta(self, req):
        select = self._select(req)
        delta_path = req.raw_path
        skip = req.params.get("$skiptoken", req.params.get("skiptoken"))
        if skip:
            key, _, pos = skip.partition(".")
            snap = self._delta_snapshots.get(key)
            try:
                pos = int(pos)
            except ValueError:
                snap = None
            if snap is None:
                raise _GraphError(410, "resyncRequired", "The delta page is no longer available.",
                                  {"Location": self.origin + delta_path})
        else:
            token = req.params.get("token")
            if token == "latest":
                return self._json_response(200, {
                    "value": [],
                    "@odata.deltaLink": "%s%s?token=%s" % (self.origin, delta_path, _b64url({"s": self._seq}))})
            since = None
            if token is not None:
                try:
                    since = int(_unb64url(token)["s"])
                except Exception:
                    raise _GraphError(410, "resyncRequired", "The delta token is invalid; resync.",
                                      {"Location": self.origin + delta_path})
            values = []
            if since is not None:
                values.extend(t for sq, t in self._tombstones if sq > since)
            for it in self._walk_subtree(self._root):
                if since is None or it.seq > since:
                    values.append(self._item_json(it, delta=True))
            key = uuid.uuid4().hex
            snap = {"values": values, "end": self._seq}
            self._delta_snapshots[key] = snap
            pos = 0
        chunk = snap["values"][pos:pos + self.page_size]
        resp = {"value": [self._apply_select(v, select) if "deleted" not in v else v for v in chunk]}
        nxt = pos + len(chunk)
        sel_raw = req.params.get("$select", req.params.get("select"))
        extra = ("$select=" + quote(sel_raw, safe=",@.") + "&") if sel_raw else ""
        if nxt < len(snap["values"]):
            resp["@odata.nextLink"] = "%s%s?%s$skiptoken=%s.%d" % (self.origin, delta_path, extra, key, nxt)
        else:
            resp["@odata.deltaLink"] = "%s%s?%stoken=%s" % (self.origin, delta_path, extra, _b64url({"s": snap["end"]}))
            self._delta_snapshots.pop(key, None)
        return self._json_response(200, resp)


def _path_of(item) -> str:
    parts = []
    while item is not None and item.parent is not None:
        parts.append(item.name)
        item = item.parent
    return "/".join(reversed(parts))
