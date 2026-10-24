"""Groww login that keeps itself fresh, so an unattended service doesn't go blind overnight.

Groww access tokens expire (daily, per Groww's flows). ``RefreshingGrowwClient`` stands in for
``growwapi.GrowwAPI``. It logs in again:

* every trading morning before the open (``refresh_at``), and
* once, straight away, when a call fails with an authentication error — then retries that call.
  An order rejected for authentication was not accepted by Groww, so retrying it is safe and
  keeps the same ``order_reference_id`` (duplicate protection still applies).

Re-logins are rate-limited (Groww allows 150 token requests a day). Only the TOTP flow (or an
API secret you approve daily on Groww Cloud) can refresh unattended; a pasted
``GROWW_ACCESS_TOKEN`` cannot, and ``claudetrade doctor`` warns about it.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import datetime, time, timedelta
from typing import Any

AUTH_ERRORS = {"GrowwAPIAuthenticationException", "GrowwAPIAuthorisationException"}


def login_from_env() -> tuple[Callable[[], Any], str]:
    """Return (login function, flow name) from GROWW_* environment variables."""
    from growwapi import GrowwAPI

    token = os.environ.get("GROWW_ACCESS_TOKEN")
    api_key = os.environ.get("GROWW_API_KEY")
    if api_key and os.environ.get("GROWW_TOTP_SECRET"):
        import pyotp

        def totp_login():
            return GrowwAPI(GrowwAPI.get_access_token(api_key=api_key, totp=pyotp.TOTP(os.environ["GROWW_TOTP_SECRET"]).now()))
        return totp_login, "totp"
    if api_key and os.environ.get("GROWW_API_SECRET"):
        def secret_login():
            return GrowwAPI(GrowwAPI.get_access_token(api_key=api_key, secret=os.environ["GROWW_API_SECRET"]))
        return secret_login, "api_secret"
    if token:
        return (lambda: GrowwAPI(token)), "static_token"
    raise RuntimeError("set GROWW_API_KEY + GROWW_TOTP_SECRET (recommended), GROWW_API_KEY + GROWW_API_SECRET, "
                       "or GROWW_ACCESS_TOKEN")


class RefreshingGrowwClient:
    def __init__(self, login: Callable[[], Any], clock: Callable[[], datetime], flow: str = "unknown",
                 refresh_at: time = time(8, 45), min_relogin_gap_s: int = 300):
        self._login, self._clock, self.flow = login, clock, flow
        self.refresh_at, self.min_gap = refresh_at, min_relogin_gap_s
        self._client: Any = None
        self._logged_in_at: datetime | None = None
        self._lock = threading.Lock()
        self.logins = 0
        self.last_error: str | None = None

    def _due(self, now: datetime) -> bool:
        if self._client is None or self._logged_in_at is None:
            return True
        todays_refresh = datetime.combine(now.date(), self.refresh_at, now.tzinfo)
        return self._logged_in_at < todays_refresh <= now

    def _relogin(self, force: bool = False) -> Any:
        with self._lock:
            now = self._clock()
            recent = self._logged_in_at and (now - self._logged_in_at) < timedelta(seconds=self.min_gap)
            if self._client is not None and (not force and not self._due(now) or force and recent):
                return self._client
            try:
                self._client = self._login()
            except Exception as e:
                self.last_error = f"login failed: {type(e).__name__}"  # never log the payload
                if self._client is None:
                    raise
                return self._client  # keep the old session; the next call may still work
            self._logged_in_at = now
            self.logins += 1
            self.last_error = None
            return self._client

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        target = getattr(self._relogin(), name)
        if not callable(target):
            return target

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return getattr(self._relogin(), name)(*args, **kwargs)
            except Exception as e:
                if type(e).__name__ not in AUTH_ERRORS:
                    raise
                before = self._client
                fresh = self._relogin(force=True)
                if fresh is before:
                    raise
                return getattr(fresh, name)(*args, **kwargs)
        return call
