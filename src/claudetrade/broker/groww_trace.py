"""HTTP-level trace of Groww API calls, safe to share with Groww support (and in chat).

``install()`` wraps ``requests.get/post/put`` (the growwapi SDK calls those) and prints one
line per call:

    POST /v1/token/api/access -> 200 SUCCESS  (req 3f1c…)        token exchange: body never shown
    GET  /v1/live-data/quote?exchange=NSE&segment=CASH&trading_symbol=ITC -> 403 FAILURE GA005 "…"

Never printed: the Authorization header, request bodies, the access token, TOTP codes or
secrets, or success payloads (they can contain balances). Only the method, path, non-secret
query parameters, HTTP status, Groww's status/error code/message and the request id.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlencode, urlparse

SECRET_PARAMS = {"token", "totp", "secret", "checksum", "api_key", "access_token"}


def _describe(method: str, url: str, kwargs: dict, resp, elapsed_ms: float) -> str:
    u = urlparse(url)
    params = {k: v for k, v in (kwargs.get("params") or {}).items() if v is not None and k.lower() not in SECRET_PARAMS}
    path = u.path + (f"?{urlencode(params, doseq=True)}" if params else "")
    req_id = (kwargs.get("headers") or {}).get("x-request-id", "")
    status, code, msg = "", "", ""
    try:
        body = resp.json()
        status = str(body.get("status", ""))
        err = body.get("error") or {}
        if isinstance(err, dict):
            code, msg = str(err.get("code", "") or ""), str(err.get("message", "") or "")
    except Exception:  # noqa: BLE001 - non-JSON body
        status = "non-JSON"
    line = f"{method:4} {path} -> {resp.status_code} {status}".rstrip()
    if code or msg:
        line += f" {code} \"{msg[:200]}\""
    if "/token/" in u.path:
        line += "  [token exchange: body/response never shown]"
    return line + f"  ({elapsed_ms:.0f} ms, req {req_id})"


def install(printer: Callable[[str], None] = print) -> Callable[[], None]:
    """Start tracing; returns a function that restores the original requests functions."""
    import requests

    originals = {m: getattr(requests, m) for m in ("get", "post", "put")}

    def wrap(method: str, fn):
        def traced(url, *args, **kwargs):
            host = urlparse(str(url)).hostname or ""
            if not (host == "groww.in" or host.endswith(".groww.in")):
                return fn(url, *args, **kwargs)
            t0 = time.monotonic()
            try:
                resp = fn(url, *args, **kwargs)
            except Exception as e:
                printer(f"{method.upper():4} {urlparse(str(url)).path} -> {type(e).__name__} (no response)")
                raise
            printer(_describe(method.upper(), str(url), kwargs, resp, (time.monotonic() - t0) * 1000))
            return resp
        return traced

    for m, fn in originals.items():
        setattr(requests, m, wrap(m, fn))

    def uninstall() -> None:
        for m, fn in originals.items():
            setattr(requests, m, fn)

    return uninstall


def public_ips() -> dict[str, str]:
    """This machine's public IPv4/IPv6 as seen from the internet (for static-IP questions)."""
    import urllib.request

    out = {}
    for label, url in (("ipv4", "https://api.ipify.org"), ("ipv6", "https://api64.ipify.org")):
        try:
            out[label] = urllib.request.urlopen(url, timeout=6).read().decode().strip()
        except Exception as e:  # noqa: BLE001
            out[label] = f"unavailable ({type(e).__name__})"
    return out


_last_logged: dict[tuple[str, str], float] = {}


def log_error(where: str, e: Exception, every_s: float = 300) -> None:
    """Service-log a Groww failure with its code/message (no secrets), at most once per
    ``every_s`` for the same call and code, so `docker compose logs` shows *why* without flooding."""
    import logging

    code = str(getattr(e, "code", "") or "")
    key = (where, code or type(e).__name__)
    now = time.monotonic()
    if now - _last_logged.get(key, -1e9) < every_s:
        return
    _last_logged[key] = now
    msg = str(getattr(e, "msg", "") or e)[:200]
    logging.getLogger("claudetrade.groww").warning("groww %s failed: %s%s%s", where, type(e).__name__,
                                                   f" [{code}]" if code else "", f" {msg}" if msg else "")
