"""Groww credentials saved from the browser (Setup page), so nobody has to edit files or paste
secrets into a terminal. Stored in the data volume, readable only by the app's user (0600);
never returned by any API, never logged. Environment variables, if set, still take priority."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _dir() -> Path:
    return Path(os.environ.get("CLAUDETRADE_SECRETS_DIR", "var/secrets"))


def load_groww() -> dict[str, str]:
    p = _dir() / "groww.json"
    try:
        d = json.loads(p.read_text())
        return {"api_key": str(d.get("api_key", "")), "totp_secret": str(d.get("totp_secret", ""))}
    except (OSError, ValueError):
        return {}


def save_groww(api_key: str, totp_secret: str) -> None:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    tmp = d / "groww.json.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"api_key": api_key.strip(), "totp_secret": totp_secret.replace(" ", "").strip().upper()}, fh)
    os.replace(tmp, d / "groww.json")


def has_groww() -> bool:
    if os.environ.get("GROWW_API_KEY") and (os.environ.get("GROWW_TOTP_SECRET") or os.environ.get("GROWW_API_SECRET")):
        return True
    if os.environ.get("GROWW_ACCESS_TOKEN"):
        return True
    g = load_groww()
    return bool(g.get("api_key") and g.get("totp_secret"))
