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


class TokenProvider:
    """Hands out Microsoft Graph access tokens and renews them as needed.

    `interactive` decides what happens when the saved sign-in no longer works:
    True  -> show a sign-in code in the console and wait for the user;
    False -> raise SignInRequired so the caller can stop cleanly.
    """

    def __init__(self, client_id: str, tenant_id: str, cache_path: Path,
                 show: Callable[[str], None], interactive: bool):
        self._lock = threading.Lock()
        self._show = show
        self.interactive = interactive
        self._app = msal.PublicClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            token_cache=PersistedTokenCache(_persistence(cache_path)),
        )
        self._token: str | None = None
        self._expires_at = 0.0

    def get(self, force_refresh: bool = False) -> str:
        with self._lock:
            if not force_refresh and self._token and time.time() < self._expires_at - 300:
                return self._token
            result = None
            accounts = self._app.get_accounts()
            if accounts:
                result = self._app.acquire_token_silent_with_error(
                    SCOPES, account=accounts[0], force_refresh=force_refresh)
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
        flow = self._app.initiate_device_flow(scopes=SCOPES)
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
        result = self._app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise SignInRequired(f"Microsoft sign-in did not complete: {_describe(result)}")
        log.info("Microsoft sign-in successful.")
        return result


def _describe(result: dict) -> str:
    parts = [str(result.get(k)) for k in ("error", "error_description", "suberror") if result.get(k)]
    text = " | ".join(parts) or str(result)
    return text.splitlines()[0][:300]
