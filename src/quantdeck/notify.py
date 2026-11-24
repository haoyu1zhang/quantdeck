"""Push notifications: Telegram, chat webhooks (Slack, Discord, WeCom, Feishu,
DingTalk) and email. Sending happens on a background thread so a slow or
unreachable endpoint never holds up trading."""

from __future__ import annotations

import json
import logging
import os
import queue
import smtplib
import threading
import time
import urllib.request
from email.message import EmailMessage

log = logging.getLogger(__name__)

EVENTS = ("start", "stop", "fill", "stop_hit", "halt", "error", "stale")


def _post_json(url: str, payload: dict, timeout: float = 10) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


class Channel:
    name = "base"

    def send(self, title: str, text: str) -> None:
        raise NotImplementedError


class Telegram(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str) -> None:
        self.token, self.chat_id = token, chat_id

    def send(self, title, text):
        _post_json(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            {"chat_id": self.chat_id, "text": f"{title}\n{text}", "disable_web_page_preview": True},
        )


class Webhook(Channel):
    name = "webhook"

    def __init__(self, url: str, kind: str = "generic") -> None:
        self.url, self.kind = url, kind

    def payload(self, title: str, text: str) -> dict:
        msg = f"{title}\n{text}"
        if self.kind == "slack":
            return {"text": msg}
        if self.kind == "discord":
            return {"content": msg[:2000]}
        if self.kind in ("wecom", "dingtalk"):
            return {"msgtype": "text", "text": {"content": msg}}
        if self.kind == "feishu":
            return {"msg_type": "text", "content": {"text": msg}}
        return {"title": title, "text": text}

    def send(self, title, text):
        _post_json(self.url, self.payload(title, text))


class Email(Channel):
    name = "email"

    def __init__(self, host, port, user, password, to, sender=None, starttls=True) -> None:
        self.host, self.port = host, int(port)
        self.user, self.password = user, password
        self.to = [a.strip() for a in to.split(",") if a.strip()]
        self.sender = sender or user
        self.starttls = starttls

    def send(self, title, text):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, self.sender, ", ".join(self.to)
        msg.set_content(text)
        if self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=15)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=15)
            if self.starttls:
                server.starttls()
        with server:
            if self.user:
                server.login(self.user, self.password)
            server.send_message(msg)


class Notifier:
    """Fan-out to channels from a background thread.

    Messages with the same key are held back for `cooldown` seconds, so a
    loop that errors every poll sends one message instead of hundreds.
    """

    def __init__(self, channels: list[Channel], events=EVENTS, prefix: str = "", cooldown: float = 300,
                 maxsize: int = 200) -> None:
        self.channels = channels
        self.events = set(events)
        self.prefix = prefix
        self.cooldown = cooldown
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    def notify(self, event: str, title: str, text: str = "", key: str | None = None) -> bool:
        if not self.channels or event not in self.events:
            return False
        key = key or f"{event}:{title}"
        now = time.monotonic()
        with self._lock:
            if now - self._last.get(key, -1e9) < self.cooldown and event not in ("fill", "stop_hit"):
                return False
            self._last[key] = now
        try:
            self._q.put_nowait((f"{self.prefix}{title}", text))
        except queue.Full:
            log.warning("notification queue full, dropping: %s", title)
            return False
        self._ensure_thread()
        return True

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._worker, name="notifier", daemon=True)
            self._thread.start()

    def _worker(self) -> None:
        while True:
            try:
                title, text = self._q.get(timeout=30)
            except queue.Empty:
                return
            for ch in self.channels:
                try:
                    ch.send(title, text)
                except Exception as e:
                    log.warning("%s notification failed: %s", ch.name, e)
            self._q.task_done()

    def flush(self, timeout: float = 10) -> None:
        """Wait (briefly) for queued messages, e.g. before the process exits."""
        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.05)


def channels_from_env(telegram=None, webhook=None, email=None) -> list[Channel]:
    """Build channels from environment variables.

    Each argument: None = use it if its variables are set, False = off,
    True = required. `webhook` may also be the webhook kind (slack, wecom, ...).
    """
    env = os.environ.get
    out: list[Channel] = []

    def want(flag, *names):
        have = all(env(n) for n in names)
        if flag and not have:
            raise RuntimeError(f"notifications need {', '.join(names)}")
        return have and flag is not False

    if want(telegram, "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        out.append(Telegram(env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")))
    if want(webhook, "NOTIFY_WEBHOOK_URL"):
        kind = webhook if isinstance(webhook, str) else env("NOTIFY_WEBHOOK_KIND", "generic")
        out.append(Webhook(env("NOTIFY_WEBHOOK_URL"), kind))
    if want(email, "SMTP_HOST", "NOTIFY_EMAIL_TO"):
        out.append(Email(env("SMTP_HOST"), env("SMTP_PORT", "587"), env("SMTP_USER", ""),
                         env("SMTP_PASSWORD", ""), env("NOTIFY_EMAIL_TO"), env("SMTP_FROM")))
    return out
