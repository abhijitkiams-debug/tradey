"""Alert delivery to a human (PRD §G, §14 "escalate urgently").

Channels are configured only through environment variables (they carry secrets):

  CLAUDETRADE_TELEGRAM_BOT_TOKEN + CLAUDETRADE_TELEGRAM_CHAT_ID   push to your phone via a Telegram bot
  CLAUDETRADE_ALERT_WEBHOOK_URL                                   POST JSON to any webhook (Slack, ntfy, ...)

Delivery runs on a background thread with short timeouts so a slow or failing channel can
never delay trading logic. Failures are written to the audit log. Which severities go out is
controlled by ``CLAUDETRADE_ALERT_MIN_SEVERITY`` (default HIGH).
"""

from __future__ import annotations

import json
import os
import queue
import threading
import urllib.request
from collections.abc import Callable
from typing import Any

SEVERITY_RANK = {"INFO": 0, "HIGH": 1, "CRITICAL": 2}


def _post(url: str, payload: dict[str, Any], timeout: float = 8.0) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-configured URL
        if r.status >= 300:
            raise RuntimeError(f"HTTP {r.status}")


def format_alert(a: Any, label: str) -> str:
    repeat = f" (repeat {a.sends - 1}, not yet acknowledged)" if a.sends > 1 else ""
    return f"[{label}] {a.severity}{repeat} {a.ts:%d %b %H:%M} IST\n{a.message}\nAlert id: {a.alert_id}"


class Notifier:
    def __init__(self, senders: list[Callable[[str, Any], None]], min_severity: str = "HIGH", label: str = "ClaudeTrade",
                 on_error: Callable[[str, Exception], None] | None = None, background: bool = True):
        self.senders = senders
        self.min_rank = SEVERITY_RANK[min_severity]
        self.label = label
        self.on_error = on_error
        self.background = background
        self.sent: list[str] = []
        self._q: queue.Queue = queue.Queue()
        if background and senders:
            threading.Thread(target=self._worker, name="claudetrade-notify", daemon=True).start()

    def __call__(self, alert: Any) -> None:
        if SEVERITY_RANK.get(alert.severity, 2) < self.min_rank or not self.senders:
            return
        text = format_alert(alert, self.label)
        if self.background:
            self._q.put((text, alert))
        else:
            self._deliver(text, alert)

    def _worker(self) -> None:
        while True:
            text, alert = self._q.get()
            self._deliver(text, alert)

    def _deliver(self, text: str, alert: Any) -> None:
        for send in self.senders:
            try:
                send(text, alert)
                self.sent.append(text)
            except Exception as e:  # noqa: BLE001 - try every channel
                if self.on_error:
                    self.on_error(getattr(send, "__name__", "channel"), e)


def telegram_sender(bot_token: str, chat_id: str) -> Callable[[str, Any], None]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    def telegram(text: str, alert: Any) -> None:
        _post(url, {"chat_id": chat_id, "text": text, "disable_notification": alert.severity == "INFO"})

    return telegram


def webhook_sender(url: str) -> Callable[[str, Any], None]:
    def webhook(text: str, alert: Any) -> None:
        _post(url, {"text": text, "severity": alert.severity, "alert_id": alert.alert_id, "message": alert.message})

    return webhook


def notifier_from_env(label: str, on_error: Callable[[str, Exception], None] | None = None) -> Notifier | None:
    senders = []
    tok, chat = os.environ.get("CLAUDETRADE_TELEGRAM_BOT_TOKEN"), os.environ.get("CLAUDETRADE_TELEGRAM_CHAT_ID")
    if tok and chat:
        senders.append(telegram_sender(tok, chat))
    if os.environ.get("CLAUDETRADE_ALERT_WEBHOOK_URL"):
        senders.append(webhook_sender(os.environ["CLAUDETRADE_ALERT_WEBHOOK_URL"]))
    if not senders:
        return None
    return Notifier(senders, os.environ.get("CLAUDETRADE_ALERT_MIN_SEVERITY", "HIGH"), label, on_error)
