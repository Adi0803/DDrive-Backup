"""Console output: permanent messages plus a live progress block with two bars."""

from __future__ import annotations

import shutil
import sys
import threading
import time
from collections import deque
from typing import Callable

from .winutils import enable_ansi_console, set_console_title

BAR_WIDTH = 30


def fmt_bytes(n: float) -> str:
    if n >= 1000 ** 3:
        return f"{n / 1000 ** 3:,.2f} GB"
    if n >= 1000 ** 2:
        return f"{n / 1000 ** 2:,.1f} MB"
    return f"{n / 1000:,.0f} KB"


def n_files(n: int) -> str:
    return f"{n:,} file" + ("" if n == 1 else "s")


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def bar(fraction: float) -> str:
    fraction = min(1.0, max(0.0, fraction))
    filled = int(round(fraction * BAR_WIDTH))
    return "[" + "#" * filled + "." * (BAR_WIDTH - filled) + "]"


class Console:
    """Thread-safe console writer. Permanent lines go above a block of live
    progress lines that is redrawn in place (ANSI escape codes)."""

    def __init__(self, enabled: bool):
        self.enabled = bool(enabled) and sys.stdout is not None
        self.ansi = enable_ansi_console() if self.enabled else False
        self._lock = threading.RLock()
        self._block: list[str] = []
        self._drawn = 0
        self._last_plain = 0.0
        if self.enabled:
            try:
                sys.stdout.reconfigure(errors="replace")
            except (AttributeError, ValueError):
                pass

    def say(self, text: str = "") -> None:
        if not self.enabled:
            return
        with self._lock:
            self._erase()
            sys.stdout.write(text + "\n")
            self._draw()
            sys.stdout.flush()

    def show(self, lines: list[str], title: str | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._block = list(lines)
            if self.ansi:
                self._erase()
                self._draw()
                sys.stdout.flush()
            elif time.monotonic() - self._last_plain > 30 and lines:
                # Old console without cursor control: print a status line now and then.
                self._last_plain = time.monotonic()
                sys.stdout.write(" | ".join(l.strip() for l in lines[1:4]) + "\n")
                sys.stdout.flush()
        if title:
            set_console_title(title)

    def end_block(self) -> None:
        """Leave the last progress block on screen as normal text."""
        with self._lock:
            if self.enabled and self.ansi:
                self._erase()
                self._draw()
            self._block, self._drawn = [], 0

    def _erase(self) -> None:
        if self.ansi and self._drawn:
            sys.stdout.write(f"\x1b[{self._drawn}F\x1b[J")
            self._drawn = 0

    def _draw(self) -> None:
        if not self._block or not self.ansi:
            return
        width = max(40, shutil.get_terminal_size((100, 30)).columns - 1)
        sys.stdout.write("\n".join(line[:width] for line in self._block) + "\n")
        self._drawn = len(self._block)


class Ticker:
    """Calls `render()` every `interval` seconds on a background thread."""

    def __init__(self, render: Callable[[], None], interval: float = 0.5):
        self._render, self._interval = render, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="progress", daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)
        self._render()
        return False

    def _run(self):
        while not self._stop.wait(self._interval):
            try:
                self._render()
            except Exception:
                pass


class EtaEstimator:
    """Estimates the remaining time from how long recent data and recent files took.

    Model: seconds = MB x seconds_per_MB + files x seconds_per_file, fitted on
    5-second windows of history (recent minutes weigh more). An ETA is only reported when
    the measurements actually pin down the parts of the work that remain (for
    example, a run of tiny files says nothing about how long a 2 GB file will
    take) and recent predictions agree with each other. Otherwise estimate()
    returns None and the display says "estimating".
    """

    WINDOW = 5.0                 # seconds per sample window
    MIN_ELAPSED = 60.0           # never show an ETA before this
    HALF_LIFE = 300.0            # older windows count less
    PRIOR = (0.2, 0.3)           # ~5 MB/s and 0.3 s per file: a very weak tie-breaker only
    PRIOR_SECONDS = 0.5          # ... worth half a second of measurements
    MAX_RELATIVE_ERROR = 0.10    # one standard error must be within 10% of the ETA ...
    MAX_ABSOLUTE_ERROR = 20.0    # ... or within 20 seconds
    STABLE_SECONDS = 90.0        # predicted finish time must have been steady this long ...
    MAX_DRIFT = 0.15             # ... moving by less than 15% of the remaining time

    def __init__(self) -> None:
        self._start: float | None = None
        self._last: tuple[float, float, float] | None = None
        self._windows: deque = deque(maxlen=4000)       # (t_end, dt, dMB, dfiles)
        self._predictions: deque = deque(maxlen=400)    # (t, predicted_finish)

    def add(self, now: float, done_bytes: float, done_files: float) -> None:
        if self._start is None:
            self._start = now
            self._last = (now, done_bytes, done_files)
            return
        t0, b0, f0 = self._last
        if now - t0 >= self.WINDOW:
            self._windows.append((now, now - t0, (done_bytes - b0) / 1e6, done_files - f0))
            self._last = (now, done_bytes, done_files)

    def fit(self, now: float):
        """Returns ((sec_per_MB, sec_per_file), inverse information matrix, noise variance)."""
        s11 = s12 = s22 = r1 = r2 = sw = 0.0
        rows = []
        for t, dt, x1, x2 in self._windows:
            w = 0.5 ** ((now - t) / self.HALF_LIFE)
            rows.append((w, dt, x1, x2))
            s11 += w * x1 * x1
            s12 += w * x1 * x2
            s22 += w * x2 * x2
            r1 += w * x1 * dt
            r2 += w * x2 * dt
            sw += w
        if sw == 0:
            return None
        # The prior acts like two tiny extra windows (one of pure data, one of pure
        # files) so the maths stays solvable before both kinds of work were seen.
        p_mb, p_file = self.PRIOR
        m0, f0 = self.PRIOR_SECONDS / p_mb, self.PRIOR_SECONDS / p_file
        a11, a12, a22 = s11 + m0 * m0, s12, s22 + f0 * f0
        b1, b2 = r1 + m0 * m0 * p_mb, r2 + f0 * f0 * p_file
        det = a11 * a22 - a12 * a12
        if det <= 0:
            return None
        per_mb = (b1 * a22 - b2 * a12) / det
        per_file = (a11 * b2 - a12 * b1) / det
        if per_mb < 0 or per_file < 0:
            return None
        inv = (a22 / det, -a12 / det, a11 / det)
        resid = sum(w * (dt - per_mb * x1 - per_file * x2) ** 2 for w, dt, x1, x2 in rows) / sw
        noise = max(2.0 * resid, (0.1 * self.WINDOW) ** 2)   # be pessimistic about noise
        return (per_mb, per_file), inv, noise

    def estimate(self, now: float, remaining_bytes: float, remaining_files: float) -> float | None:
        if self._start is None or now - self._start < self.MIN_ELAPSED or len(self._windows) < 8:
            return None
        fitted = self.fit(now)
        if fitted is None:
            return None
        (per_mb, per_file), (i11, i12, i22), noise = fitted
        mb, files = remaining_bytes / 1e6, remaining_files
        eta = per_mb * mb + per_file * files
        # standard error of the prediction (how well the data pins it down)
        variance = noise * (mb * mb * i11 + 2 * mb * files * i12 + files * files * i22)
        error = variance ** 0.5
        self._predictions.append((now, now + eta))
        while self._predictions and self._predictions[0][0] < now - self.STABLE_SECONDS:
            self._predictions.popleft()
        if error > max(self.MAX_ABSOLUTE_ERROR, self.MAX_RELATIVE_ERROR * eta):
            return None
        # The predicted finish time must have held steady for a while.
        recent = [p for _, p in self._predictions]
        if len(recent) < 10 or now - self._predictions[0][0] < self.STABLE_SECONDS * 0.9:
            return None
        if max(recent) - min(recent) > max(30.0, self.MAX_DRIFT * eta):
            return None
        return eta


class TransferProgress:
    """Counters for one phase (checking or uploading), shared by worker threads."""

    def __init__(self, total_bytes: int, total_files: int):
        self.total_bytes, self.total_files = total_bytes, total_files
        self.done_bytes = 0
        self.done_files = 0
        self.failed = 0
        self.unreadable = 0
        self.start = time.monotonic()
        self.eta = EtaEstimator()
        self._current: dict[int, str] = {}
        self._lock = threading.Lock()
        self._speed_samples: deque = deque(maxlen=60)

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self.done_bytes += n

    def begin_file(self, path: str) -> None:
        with self._lock:
            self._current[threading.get_ident()] = path

    def end_file(self, leftover_bytes: int = 0, failed: bool = False, unreadable: bool = False) -> None:
        """leftover_bytes: bytes of a failed file that were never sent, counted as
        processed so the bars still reach 100%."""
        with self._lock:
            self._current.pop(threading.get_ident(), None)
            self.done_files += 1
            self.done_bytes += leftover_bytes
            self.failed += failed
            self.unreadable += unreadable

    def lines(self, heading: str, show_eta: bool = True) -> tuple[list[str], str]:
        now = time.monotonic()
        with self._lock:
            done_b, done_f = self.done_bytes, self.done_files
            current = list(self._current.values())
            failed, unreadable = self.failed, self.unreadable
        self.eta.add(now, done_b, done_f)
        self._speed_samples.append((now, done_b, done_f))
        t0, b0, f0 = self._speed_samples[0]
        span = now - t0
        mb_s = (done_b - b0) / 1e6 / span if span > 1 else 0.0
        files_s = (done_f - f0) / span if span > 1 else 0.0
        frac_b = done_b / self.total_bytes if self.total_bytes else 1.0
        frac_f = done_f / self.total_files if self.total_files else 1.0
        eta_text = ""
        if show_eta:
            if done_f >= self.total_files:
                eta_text = "   Done"
            else:
                eta = self.eta.estimate(now, self.total_bytes - done_b, self.total_files - done_f)
                eta_text = f"   ETA ~{fmt_duration(eta)}" if eta is not None else "   ETA: estimating..."
        now_line = "Now: " + (current[0] if current else "-")
        if len(current) > 1:
            now_line += f"   (+{len(current) - 1} more)"
        lines = [
            heading,
            f"Data  {bar(frac_b)} {frac_b * 100:5.1f}%   {fmt_bytes(done_b)} / {fmt_bytes(self.total_bytes)}",
            f"Files {bar(frac_f)} {frac_f * 100:5.1f}%   {done_f:,} / {self.total_files:,}",
            f"Speed {mb_s:.1f} MB/s  {files_s:.1f} files/s   Elapsed {fmt_duration(now - self.start)}{eta_text}",
            now_line,
            f"Skipped (unreadable): {unreadable}   Failed: {failed}",
        ]
        title = f"D-Drive Backup {frac_b * 100:.0f}%" + (f" - {eta_text.strip()}" if "~" in eta_text else "")
        return lines, title
