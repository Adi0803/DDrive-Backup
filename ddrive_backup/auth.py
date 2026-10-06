"""Microsoft sign-in (MSAL device-code flow) with an encrypted token cache."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

import msal
from msal_extensions import FilePersistence, PersistedTokenCache, build_encrypted_persistence
from msal_extensions.filelock import LockError
from msal_extensions.persistence import PersistenceError

log = logging.getLogger(__name__)

SCOPES = ["User.Read", "Files.ReadWrite"]


class SignInRequired(Exception):
    """Microsoft needs the user to sign in again (and we may not ask right now)."""


def _persistence(path: Path):
    try:
        # On Windows this encrypts the cache with DPAPI, tied to this Windows user.
        return build_encrypted_persistence(str(path))
    except Exception:
        if os.name == "nt":
            raise
        log.debug("Encrypted token cache unavailable on this OS; using a plain file (testing only).")
        return FilePersistence(str(path))


def remove_stale_cache_lock(cache_path: Path) -> None:
    """msal-extensions guards the cache with a "<cache>.lockfile". If a previous
    run was killed while saving, that file stays behind and blocks every later
    sign-in. Call this only while holding the backup's own single-instance lock."""
    lockfile = Path(str(cache_path) + ".lockfile")
    if lockfile.exists():
        try:
            lockfile.unlink()
            log.info("Removed a leftover sign-in lock file (%s).", lockfile.name)
        except OSError as exc:
            log.warning("Could not remove leftover %s: %s", lockfile.name, exc)


class TokenProvider:
    """Hands out Microsoft Graph access tokens and renews them as needed.

    `interactive` decides what happens when the saved sign-in no longer works:
    True  -> show a sign-in code in the console and wait for the user;
    False -> raise SignInRequired so the caller can stop cleanly.

    Network errors (requests exceptions) are passed on to the caller, which
    knows how to wait for the network.
    """

    def __init__(self, client_id: str, tenant_id: str, cache_path: Path,
                 show: Callable[[str], None], interactive: bool):
        self._lock = threading.Lock()
        self._show = show
        self.interactive = interactive
        self._client_id, self._tenant_id = client_id, tenant_id
        self._cache_path = cache_path
        self._app: msal.PublicClientApplication | None = None
        self._token: str | None = None
        self._expires_at = 0.0

    def _application(self) -> msal.PublicClientApplication:
        # Created on first use: MSAL contacts the sign-in server when it starts.
        if self._app is None:
            self._app = msal.PublicClientApplication(
                self._client_id,
                authority=f"https://login.microsoftonline.com/{self._tenant_id}",
                token_cache=PersistedTokenCache(_persistence(self._cache_path)),
            )
        return self._app

    def _reset_broken_cache(self, exc: Exception) -> None:
        log.warning("The saved sign-in could not be used (%s); a new sign-in is needed.", exc)
        remove_stale_cache_lock(self._cache_path)
        if self._cache_path.exists():
            try:
                os.replace(self._cache_path, self._cache_path.with_name(self._cache_path.stem + ".broken.bin"))
            except OSError as move_exc:
                log.warning("Could not move the broken sign-in file aside: %s", move_exc)
        self._app = None

    def get(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._token and time.time() < self._expires_at - 300:
                return self._token
            result = None
            for attempt in (1, 2):
                try:
                    app = self._application()
                    accounts = app.get_accounts()
                    if accounts:
                        result = app.acquire_token_silent_with_error(
                            SCOPES, account=accounts[0], force_refresh=force_refresh)
                    break
                except (PersistenceError, LockError) as exc:
                    if attempt == 2:
                        raise SignInRequired(f"The saved sign-in is damaged ({exc}).") from exc
                    self._reset_broken_cache(exc)
            if not result or "access_token" not in result:
                reason = _describe(result) if result else "no saved sign-in"
                if not self.interactive:
                    raise SignInRequired(f"Microsoft sign-in is needed ({reason}).")
                log.warning("Microsoft sign-in needed (%s).", reason)
                result = self._device_flow()
            self._token = result["access_token"]
            self._expires_at = time.time() + int(result.get("expires_in", 3600))
            return self._token

    def _device_flow(self) -> dict:
        app = self._application()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SignInRequired(f"Could not start Microsoft sign-in: {_describe(flow)}")
        url = flow.get("verification_uri", "https://microsoft.com/devicelogin")
        log.warning("Sign-in required: open %s and enter code %s", url, flow["user_code"])
        self._show(
            "\n" + "=" * 70 + "\n"
            "MICROSOFT SIGN-IN NEEDED\n\n"
            f"  1. Open  {url}\n"
            f"  2. Enter code  {flow['user_code']}\n"
            "  3. Sign in with your work account.\n\n"
            "The backup continues automatically after you sign in.\n" + "=" * 70 + "\n")
        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise SignInRequired(f"Microsoft sign-in did not complete: {_describe(result)}")
        log.info("Microsoft sign-in successful.")
        return result


def _describe(result: dict) -> str:
    parts = [str(result.get(k)) for k in ("error", "error_description", "suberror") if result.get(k)]
    text = " | ".join(parts) or str(result)
    return text.splitlines()[0][:300]
