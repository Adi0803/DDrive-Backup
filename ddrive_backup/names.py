"""Path keys and OneDrive naming rules."""

from __future__ import annotations

import re
import unicodedata


def key_for(path: str) -> str:
    """OneDrive and Windows ignore upper/lower case in names, so compare paths
    through this key, never through the raw text."""
    return unicodedata.normalize("NFC", path).casefold()


_RESERVED = {"con", "prn", "aux", "nul", ".lock", "desktop.ini",
             *(f"com{i}" for i in range(10)), *(f"lpt{i}" for i in range(10))}
_BAD_CHARS = re.compile(r'["*:<>?/\\|]')
MAX_PATH_CHARS = 400   # OneDrive limit for the whole decoded path


def onedrive_name_problem(name: str) -> str | None:
    """Why OneDrive for work or school will refuse this file/folder name, or None."""
    lower = name.casefold()
    if lower in _RESERVED:
        return f"OneDrive does not allow the name \"{name}\""
    if lower.startswith("~$"):
        return "OneDrive does not allow names starting with ~$ (Office temporary files)"
    if "_vti_" in lower:
        return "OneDrive does not allow \"_vti_\" in names"
    if _BAD_CHARS.search(name):
        return "the name contains a character OneDrive does not allow"
    if name != name.strip(" "):
        return "OneDrive does not allow names that start or end with a space"
    return None
