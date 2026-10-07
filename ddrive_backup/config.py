"""Reads and checks config.json."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


# Keys from the previous version of the script that are no longer used.
_RETIRED_KEYS = {
    "delete_remote_files": "deletions are now controlled by \"mirror_deletions\" "
                           "(\"auto\" = switch on after the first complete backup)",
}


@dataclass
class Config:
    client_id: str
    tenant_id: str
    source_folder: str
    onedrive_folder: str
    office_wifi_ssids: list[str]
    scan_interval_minutes: int = 10
    mirror_deletions: str = "auto"            # "auto", "on" or "off"
    mirror_safety_limit_percent: float = 10.0
    parallel_uploads: int = 4
    dry_run: bool = False
    window_close_seconds: int = 30
    cooldown_hours: float = 4.0           # after a backup finishes, the background check waits this long

    @property
    def any_network(self) -> bool:
        return not self.office_wifi_ssids


def load_config(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object { ... }")

    def text(key: str, required: bool = True) -> str:
        value = raw.get(key, "")
        if not isinstance(value, str) or (required and not value.strip()):
            raise ConfigError(f"config.json: \"{key}\" must be a non-empty text value")
        return value.strip()

    # "office_wifi_ssid": a Wi-Fi name, a list of names, or "" to run on any network.
    if "office_wifi_ssid" not in raw:
        raise ConfigError("config.json: add \"office_wifi_ssid\" (your office Wi-Fi name, "
                          "or \"\" to back up on any network)")
    ssids = raw["office_wifi_ssid"]
    if isinstance(ssids, str):
        ssids = [ssids]
    if not isinstance(ssids, list) or not all(isinstance(s, str) for s in ssids):
        raise ConfigError("config.json: \"office_wifi_ssid\" must be a Wi-Fi name or a list of names")
    if any(s.strip() == "YOUR_OFFICE_WIFI_NAME" for s in ssids):
        raise ConfigError("config.json: set \"office_wifi_ssid\" to your office Wi-Fi name")
    ssids = [s.strip() for s in ssids if s.strip()]

    mirror = raw.get("mirror_deletions", "auto")
    if mirror is True:
        mirror = "on"
    elif mirror is False:
        mirror = "off"
    if mirror not in ("auto", "on", "off"):
        raise ConfigError("config.json: \"mirror_deletions\" must be \"auto\", \"on\" or \"off\"")

    def number(key: str, default, low, high, kind=int):
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high):
            raise ConfigError(f"config.json: \"{key}\" must be a number from {low} to {high}")
        return kind(value)

    dry_run = raw.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ConfigError("config.json: \"dry_run\" must be true or false")

    folder = text("onedrive_folder").replace("\\", "/").strip("/")
    if not folder:
        raise ConfigError("config.json: \"onedrive_folder\" must name a folder, e.g. \"D-Drive-Backup\"")

    for key, why in _RETIRED_KEYS.items():
        if key in raw:
            log.info("config.json: \"%s\" is no longer used: %s.", key, why)

    return Config(
        client_id=text("client_id"),
        tenant_id=text("tenant_id"),
        source_folder=text("source_folder"),
        onedrive_folder=folder,
        office_wifi_ssids=ssids,
        scan_interval_minutes=number("scan_interval_minutes", 10, 1, 1440),
        mirror_deletions=mirror,
        mirror_safety_limit_percent=number("mirror_safety_limit_percent", 10, 0, 100, float),
        parallel_uploads=number("parallel_uploads", 4, 1, 8),
        dry_run=dry_run,
        window_close_seconds=number("window_close_seconds", 30, 0, 3600),
        cooldown_hours=number("cooldown_hours", 4, 0, 168, float),
    )
