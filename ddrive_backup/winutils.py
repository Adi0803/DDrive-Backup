"""Small Windows-specific helpers. Every function also works (as a no-op or a
simple fallback) on other systems so the backup logic can be tested anywhere."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = os.name == "nt"

# subprocess flags (only meaningful on Windows)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

ERROR_VIRUS_INFECTED = 225
ERROR_VIRUS_DELETED = 226


def long_path(path: str) -> str:
    """Return a path Windows accepts even when it is longer than 260 characters."""
    if not IS_WINDOWS:
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def describe_os_error(path: str, exc: OSError) -> str:
    """Explain why a local file cannot be read, in plain words.

    Python reports several different Windows errors (antivirus blocks, cloud
    placeholders, ...) as the meaningless "[Errno 22] Invalid argument". Ask
    Windows directly for the real reason.
    """
    if IS_WINDOWS:
        code = _open_error_code(path)
        if code:
            import ctypes

            message = ctypes.FormatError(code).strip()
            if code in (ERROR_VIRUS_INFECTED, ERROR_VIRUS_DELETED):
                return ("Blocked by antivirus (Windows Security): " + message +
                        " See Windows Security > Virus & threat protection > Protection history.")
            return f"{message} (Windows error {code})"
    winerror = getattr(exc, "winerror", None)
    text = exc.strerror or str(exc)
    return f"{text} (Windows error {winerror})" if winerror else text


def _open_error_code(path: str) -> int:
    """Try to open the file for reading with CreateFileW; return the Windows
    error code if that fails, else 0."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create_file.restype = wintypes.HANDLE
    GENERIC_READ = 0x80000000
    SHARE_ALL = 0x1 | 0x2 | 0x4
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000   # lets the call also open folders
    handle = create_file(long_path(path), GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING,
                         FILE_FLAG_BACKUP_SEMANTICS, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid):
        return ctypes.get_last_error()
    kernel32.CloseHandle(handle)
    return 0


class KeepAwake:
    """Ask Windows not to sleep because of inactivity while the backup runs.
    It cannot prevent sleep from closing the lid or the power button, and on
    battery many laptops ("Modern Standby") still sleep a few minutes after the
    screen turns off; uploads then resume on the next run."""

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __enter__(self):
        if IS_WINDOWS:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
        return self

    def __exit__(self, *exc):
        if IS_WINDOWS:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
        return False


def enable_ansi_console() -> bool:
    """Turn on ANSI escape codes (cursor movement) in the Windows console.
    Returns True when the progress display can redraw lines in place."""
    stream = sys.stdout
    if stream is None or not hasattr(stream, "isatty") or not stream.isatty():
        return False
    if not IS_WINDOWS:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        ok = bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
        _disable_quick_edit(kernel32)
        return ok
    except Exception:
        return False


def _disable_quick_edit(kernel32) -> None:
    """In the classic console a mouse click starts a text selection, which
    pauses all output - and with it the backup - until a key is pressed."""
    import ctypes
    from ctypes import wintypes

    handle = kernel32.GetStdHandle(-10)              # STD_INPUT_HANDLE
    mode = wintypes.DWORD()
    if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        ENABLE_QUICK_EDIT_MODE, ENABLE_EXTENDED_FLAGS = 0x0040, 0x0080
        kernel32.SetConsoleMode(handle, (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE)


def on_battery() -> bool:
    """True when a laptop is running on battery (False if unknown)."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

        status = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return False
        return status.ACLineStatus == 0
    except Exception:
        return False


def set_console_title(title: str) -> None:
    if IS_WINDOWS and sys.stdout is not None:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def current_wifi_ssids() -> tuple[list[str] | None, str]:
    """Names of the Wi-Fi networks this PC is connected to.

    Returns (ssids, details). ssids is None when Windows would not tell us
    (for example when Location access for apps is turned off), and details
    then contains netsh's message.
    """
    if not IS_WINDOWS:
        return None, "Wi-Fi detection is only available on Windows."
    try:
        result = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True,
                                timeout=30, creationflags=CREATE_NO_WINDOW)
    except Exception as exc:  # netsh missing, timeout, ...
        return None, f"Could not run netsh: {exc}"
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        text = result.stdout.decode("oem", errors="replace")
    ssids = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().upper() == "SSID":
            ssids.append(value.strip())
    if not ssids and ("location" in text.lower() or result.returncode != 0):
        return None, text.strip()
    return ssids, text.strip()


class SingleInstanceLock:
    """Makes sure only one backup runs at a time. The operating system releases
    the lock automatically if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> bool:
        f = open(self.path, "a+b")
        try:
            if IS_WINDOWS:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._file = f
        return True

    def release(self) -> None:
        f, self._file = self._file, None
        if f is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def console_python() -> str:
    """python.exe next to the running interpreter (pythonw.exe has no window)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        candidate = exe.with_name("python.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def windowless_python() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def open_in_new_window(args: list[str], cwd: str) -> None:
    """Start `python <args>` in its own new console window and return immediately."""
    subprocess.Popen([console_python(), *args], cwd=cwd, creationflags=CREATE_NEW_CONSOLE,
                     close_fds=True)


def wait_or_keypress(seconds: int | None) -> None:
    """Wait up to `seconds` (None = until a key is pressed), returning early if a
    key is pressed in the console. Keys pressed earlier during the run are ignored."""
    import time

    end = None if seconds is None else time.monotonic() + seconds
    if IS_WINDOWS:
        import msvcrt
        while msvcrt.kbhit():           # forget keys typed while the backup was running
            msvcrt.getwch()
        while end is None or time.monotonic() < end:
            if msvcrt.kbhit():
                msvcrt.getwch()
                return
            time.sleep(0.1)
    elif seconds is None:
        try:
            input()
        except (EOFError, OSError):
            pass
    else:
        time.sleep(max(0, seconds))
