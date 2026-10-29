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

    token = (os.environ.get("GROWW_ACCESS_TOKEN") or "").strip()
    api_key = (os.environ.get("GROWW_API_KEY") or "").strip()
    totp_secret = (os.environ.get("GROWW_TOTP_SECRET") or "").replace(" ", "").strip().upper()
    if api_key and totp_secret:
        import pyotp

        def totp_login():
            return GrowwAPI(GrowwAPI.get_access_token(api_key=api_key, totp=pyotp.TOTP(totp_secret).now()))
        return totp_login, "totp"
    if api_key and os.environ.get("GROWW_API_SECRET"):
        def secret_login():
            return GrowwAPI(GrowwAPI.get_access_token(api_key=api_key, secret=os.environ["GROWW_API_SECRET"].strip()))
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


def key_shape(value: str) -> str:
    """Describe a credential without revealing it: length, JWT parts, stray characters."""
    v = value or ""
    notes = [f"{len(v)} chars"]
    parts = v.split(".")
    notes.append(f"{len(parts)} dot-separated parts" + (" (JWT-shaped)" if len(parts) == 3 and all(parts) else ""))
    if v != v.strip():
        notes.append("has leading/trailing whitespace")
    if any(c.isspace() for c in v.strip()):
        notes.append("contains spaces/newlines")
    if len(v) in (1023, 1024):
        notes.append("exactly 1023/1024 chars: likely TRUNCATED by the terminal")
    if any(ord(c) > 127 for c in v):
        notes.append("contains non-ASCII characters (smart quotes?)")
    return ", ".join(notes)


def key_issued(value: str) -> str:
    """When a JWT-shaped key was issued/expires, from its (unsigned, non-secret) claims only.
    Lets you tell an old, revoked key from a new one without showing the key."""
    import base64
    import json
    from datetime import timezone
    from zoneinfo import ZoneInfo

    try:
        part = value.strip().split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        ist = ZoneInfo("Asia/Kolkata")
        fmt = lambda t: datetime.fromtimestamp(int(t), timezone.utc).astimezone(ist).strftime("%d %b %Y %H:%M IST")  # noqa: E731
        return f"issued {fmt(claims['iat'])}" + (f", expires {fmt(claims['exp'])}" if "exp" in claims else "")
    except Exception:  # noqa: BLE001
        return "issue time unreadable"


def clock_skew_seconds(url: str = "https://api.groww.in") -> float | None:
    """Local clock minus Groww's server clock (from the HTTP Date header); TOTP needs < ~30 s."""
    import urllib.error
    import urllib.request
    from datetime import timezone
    from email.utils import parsedate_to_datetime

    try:
        req = urllib.request.Request(url, method="HEAD")
        try:
            resp = urllib.request.urlopen(req, timeout=8)
            date = resp.headers.get("Date")
        except urllib.error.HTTPError as e:  # any status still carries a Date header
            date = e.headers.get("Date")
        if not date:
            return None
        return (datetime.now(timezone.utc) - parsedate_to_datetime(date)).total_seconds()
    except Exception:  # noqa: BLE001 - diagnostics only
        return None


def diagnose_login() -> list[tuple[str, str, str]]:
    """Step-by-step Groww login check for `claudetrade doctor`. Never prints secrets."""
    out: list[tuple[str, str, str]] = []
    key = os.environ.get("GROWW_API_KEY", "")
    secret = os.environ.get("GROWW_TOTP_SECRET", "")
    if key:
        out.append(("INFO", "GROWW_API_KEY shape", f"{key_shape(key)}; {key_issued(key)}"))
    skew = clock_skew_seconds()
    if skew is None:
        out.append(("WARN", "clock vs Groww", "could not read Groww's server time"))
    else:
        bad = abs(skew) > 20
        out.append(("FAIL" if bad else "PASS", "clock vs Groww", f"{skew:+.0f} s" + (
            " — TOTP codes will be rejected; sync the clock (Docker Desktop: restart it after the Mac sleeps)" if bad else "")))
    if not (key and secret):
        return out
    try:
        import pyotp
        from growwapi import GrowwAPI
    except ImportError as e:
        out.append(("FAIL", "groww libraries", str(e)))
        return out
    try:
        code = pyotp.TOTP(secret.strip().replace(" ", "").upper()).now()
    except Exception as e:  # noqa: BLE001
        out.append(("FAIL", "TOTP secret", f"cannot generate a code from GROWW_TOTP_SECRET ({type(e).__name__})"))
        return out
    try:
        GrowwAPI.get_access_token(api_key=key.strip(), totp=code)
        out.append(("PASS", "token exchange", "Groww accepted the TOTP token and code"))
    except Exception as e:  # noqa: BLE001
        code_ = str(getattr(e, "code", ""))
        msg = str(getattr(e, "msg", e))
        if code_ == "401":
            hint = ("Groww rejected the TOTP token itself (GROWW_API_KEY). Usual causes: it was cut short when pasted "
                    "(see the shape above), it is not the TOTP *token* (or was regenerated/revoked), or your Trading API "
                    "subscription is not active. Re-run: ./deploy/setup-local.sh --groww")
        elif code_ == "403":
            hint = "Groww refused access: check the Trading API subscription is active for this account"
        elif code_ == "400":
            hint = (f"Groww rejected the login: {msg}. Usual causes: the key was revoked/deleted on Groww Cloud "
                    "(check its issue time above), or the TOTP secret doesn't belong to this token. Create a new "
                    "TOTP key and run ./deploy/setup-local.sh --groww")
        else:
            hint = f"{type(e).__name__}: {msg}"
        out.append(("FAIL", "token exchange", hint))
    return out
